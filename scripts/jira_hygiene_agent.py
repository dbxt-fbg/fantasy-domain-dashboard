#!/usr/bin/env python3
"""
Jira Hygiene Agent - Checks ticket hygiene issues every 15 minutes.
Runs 6am-6pm Pacific time.
"""

import sys
import time
import logging
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from utils.qa_agent_core import (
    HistoryStore,
    RunReport,
    make_run_id,
)
from utils.statuses import IN_PROGRESS_STATUSES, EXCLUDED_STATUSES
from database.schema import get_connection, SCHEMA_SQL, ensure_hygiene_memory_columns
from collectors.jira_api_collector import JiraAPICollector

logger = logging.getLogger(__name__)


class JiraHygieneChecker:
    """Checks for Jira ticket hygiene issues."""

    def __init__(self, config):
        self.config = config
        self.db_path = config['database']['path']
        self.jira = JiraAPICollector(config)
        self.parent_field = None

    def discover_parent_field(self, issues):
        """
        Discover the custom field ID for 'Parent'.

        Args:
            issues: List of issue data from Jira

        Returns:
            Field ID (e.g., 'customfield_10014') or None
        """
        if self.parent_field:
            return self.parent_field

        # Look through issues to find a field with name 'Parent'
        for issue in issues[:10]:  # Check first 10 issues
            for field_key, field_value in issue.get('fields', {}).items():
                if field_key.startswith('customfield_'):
                    # Try to get field metadata (would need separate API call)
                    # For now, assume it's the first customfield that looks like a parent
                    if field_value and isinstance(field_value, dict):
                        if 'key' in field_value and 'fields' in field_value:
                            # This looks like a parent issue
                            self.parent_field = field_key
                            logger.info(f"Discovered parent field: {field_key}")
                            return field_key

        # Fallback: common parent field IDs
        common_parent_fields = ['customfield_10014', 'customfield_10008', 'parent']
        for field in common_parent_fields:
            self.parent_field = field
            logger.info(f"Using common parent field: {field}")
            return field

        return None

    def track_status_changes(self, issues):
        """
        Track when tickets enter/exit specific statuses.

        Args:
            issues: List of current issues from Jira
        """
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            now = datetime.now().isoformat()

            for issue in issues:
                ticket_key = issue['key']
                current_status = issue['fields']['status']['name']

                # Check if ticket is currently tracked in a status
                cursor.execute("""
                    SELECT status, entered_at
                    FROM status_changes
                    WHERE ticket_key = ? AND exited_at IS NULL
                """, (ticket_key,))

                tracked = cursor.fetchone()

                if tracked:
                    tracked_status, entered_at = tracked

                    # If status changed, mark old status as exited
                    if tracked_status != current_status:
                        cursor.execute("""
                            UPDATE status_changes
                            SET exited_at = ?
                            WHERE ticket_key = ? AND status = ? AND exited_at IS NULL
                        """, (now, ticket_key, tracked_status))

                        # Record new status entry
                        cursor.execute("""
                            INSERT OR IGNORE INTO status_changes
                            (ticket_key, status, entered_at)
                            VALUES (?, ?, ?)
                        """, (ticket_key, current_status, now))

                else:
                    # Start tracking this ticket in current status
                    cursor.execute("""
                        INSERT OR IGNORE INTO status_changes
                        (ticket_key, status, entered_at)
                        VALUES (?, ?, ?)
                    """, (ticket_key, current_status, now))

            conn.commit()

        finally:
            conn.close()

    def check_hygiene_issues(self, issues):
        """
        Check all hygiene rules and store issues.

        Args:
            issues: List of issues from Jira

        Returns:
            Dict with counts and lists of issues by type
        """
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        # Collect (issue_type, ticket_key) seen this run so we can reconcile
        # with the DB after all rules have evaluated. The `DELETE + re-INSERT`
        # pattern is gone: now we upsert (bump times_seen / last_seen_at)
        # for current issues and mark vanished ones as resolved.
        self._seen_this_run: set = set()

        try:

            # Load ignored epics — pulled from both the top-level
            # `ignored_epics:` block (preferred, shared with the dashboard)
            # and the legacy `hygiene.ignored_epics:` (kept for back-compat).
            from utils.ignored_epics import load_ignored_epics
            IGNORED_EPICS = load_ignored_epics(self.config)
            if IGNORED_EPICS:
                logger.info(f"Ignoring {len(IGNORED_EPICS)} epics from config: {IGNORED_EPICS}")

            hygiene_data = {
                'features_missing_requirements': [],
                'features_missing_designs': [],
                'features_missing_launch_phase': [],
                'features_missing_milestone': [],
                'epics_no_parent': [],
                'epics_no_description': [],
                'epics_no_prefix': [],
                'epics_no_designs': [],
                'epics_no_work_items': [],
                'epics_status_unknown': [],  # verification budget exhausted
                'epics_missing_acceptance_criteria': [],
                'epics_no_sprint': [],
                'epics_in_progress_no_assignee': [],
                'stories_no_parent': [],
                'stories_no_points': [],
                'stories_no_description': [],
                'code_review_24h': []
            }

            # 5-minute wall-clock budget for the per-epic child-count verification
            # phase. When Jira is slow, retries stack and the whole run exceeds the
            # 15-min cron window. After budget, unverified epics get flagged as
            # 'epics_status_unknown' instead of a false-positive 'no child stories'.
            import time as _time
            VERIFICATION_BUDGET_SECONDS = 300
            verification_start = _time.monotonic()
            verification_exhausted = False

            # Build a map of epic keys to their child stories from fetched issues
            # We'll verify any epics that appear to have 0 children with a direct query
            epic_children = {}
            for issue in issues:
                fields = issue['fields']
                issue_type = fields.get('issuetype', {}).get('name', '')
                status = fields.get('status', {}).get('name', '')
                parent = fields.get('parent')

                # Count child stories (excluding Abandoned/Duplicate)
                if issue_type == 'Story' and parent and isinstance(parent, dict):
                    if status not in ['Abandoned', 'Duplicate']:
                        parent_key = parent.get('key')
                        if parent_key:
                            epic_children[parent_key] = epic_children.get(parent_key, 0) + 1

            # Pre-pass: bulk-verify every epic that appears to have 0 children
            # using batched `parent IN (...)` queries instead of one Jira call
            # per epic. With ~500 zero-child epics × ~400ms each, the per-epic
            # path was burning ~3 minutes of wall clock. Chunked at 50 keys
            # per query keeps the JQL string well under any URL limit.
            verified_child_count: dict[str, int] = {}
            zero_child_epics = []
            for issue in issues:
                if issue['fields'].get('issuetype', {}).get('name') != 'Epic':
                    continue
                key = issue['key']
                if key in IGNORED_EPICS:
                    continue
                status = issue['fields'].get('status', {}).get('name', '')
                if status in ('Abandoned', 'Duplicate'):
                    continue
                if epic_children.get(key, 0) == 0:
                    zero_child_epics.append(key)

            if zero_child_epics:
                logger.info(
                    "Bulk-verifying child counts for %d zero-child epic(s) in chunks of 50",
                    len(zero_child_epics),
                )
                CHUNK = 50
                for i in range(0, len(zero_child_epics), CHUNK):
                    chunk = zero_child_epics[i:i + CHUNK]
                    if (_time.monotonic() - verification_start) > VERIFICATION_BUDGET_SECONDS:
                        verification_exhausted = True
                        logger.warning(
                            "Child-count verification budget (%ds) exhausted at chunk %d/%d.",
                            VERIFICATION_BUDGET_SECONDS,
                            i // CHUNK,
                            (len(zero_child_epics) + CHUNK - 1) // CHUNK,
                        )
                        break
                    keys_clause = ', '.join(chunk)
                    bulk_jql = (
                        f"parent in ({keys_clause}) "
                        f"AND type = Story AND status NOT IN (Abandoned, Duplicate)"
                    )
                    try:
                        # Pull only `parent` so we can count per-epic in Python.
                        # max_results sized for chunk × ~10 children per epic worst case.
                        bulk_data = self.jira.search_issues(bulk_jql, ['parent'], max_results=2000)
                        for child in bulk_data.get('issues') or []:
                            parent = child['fields'].get('parent')
                            if isinstance(parent, dict):
                                pkey = parent.get('key')
                                if pkey:
                                    verified_child_count[pkey] = verified_child_count.get(pkey, 0) + 1
                    except Exception as e:
                        # Don't fail the whole hygiene run on a chunk error —
                        # the per-epic fallback below will catch any unverified
                        # epics individually (with the same budget guard).
                        logger.warning(
                            "Bulk child verification failed for chunk %d-%d: %s",
                            i, i + len(chunk), e,
                        )

                # Anything in zero_child_epics that didn't appear in the bulk
                # results has confirmed 0 children. Prime the map so the main
                # loop can short-circuit without per-epic round-trips.
                for k in zero_child_epics:
                    verified_child_count.setdefault(k, 0)

            for issue in issues:
                key = issue['key']

                # Skip ignored epics
                if key in IGNORED_EPICS:
                    continue

                fields = issue['fields']
                issue_type = fields.get('issuetype', {}).get('name', '')
                status = fields.get('status', {}).get('name', '')
                summary = fields.get('summary', '')
                assignee = fields.get('assignee', {})
                assignee_name = assignee.get('displayName') if assignee else 'Unassigned'
                description = fields.get('description')

                # Check parent field - it's a dict with 'key' field when present
                parent = fields.get('parent')
                has_parent = parent and isinstance(parent, dict) and parent.get('key')
                parent_key = parent.get('key') if has_parent else None

                # Check story points - try main field and fallbacks
                story_points = fields.get(self.config['jira']['story_points_field'])
                if not story_points:
                    for fallback_field in self.config['jira'].get('story_points_fallback_fields', []):
                        story_points = fields.get(fallback_field)
                        if story_points:
                            break

                ticket_url = f"https://{self.config['jira']['cloud_id']}/browse/{key}"

                # Feature Hygiene Rules (for Features with parent INIT-185)
                # Skip abandoned and duplicate features
                if issue_type == 'Feature' and parent_key == 'INIT-185' and status not in ['Abandoned', 'Duplicate']:
                    # Convert description to string for checking
                    desc_text = str(description) if description else ''
                    desc_lower = desc_text.lower()

                    # 1. Missing Requirements - description is empty or minimal (< 50 chars)
                    if not description or len(desc_text.strip()) < 50:
                        self._store_hygiene_issue(
                            cursor, 'features_missing_requirements', key, summary, ticket_url,
                            assignee_name, status, 'Feature has missing or minimal requirements'
                        )
                        hygiene_data['features_missing_requirements'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                    # 2. Missing Designs - no Figma link and no screenshots
                    has_figma = 'figma.com' in desc_lower
                    has_screenshot = 'image' in desc_lower or 'screenshot' in desc_lower or '.png' in desc_lower or '.jpg' in desc_lower

                    # Also check customfield_11092 for Figma Design links
                    design_field = fields.get('customfield_11092')
                    if design_field and isinstance(design_field, list) and len(design_field) > 0:
                        has_figma = True

                    if not has_figma and not has_screenshot:
                        self._store_hygiene_issue(
                            cursor, 'features_missing_designs', key, summary, ticket_url,
                            assignee_name, status, 'Feature has no Figma link or screenshots'
                        )
                        hygiene_data['features_missing_designs'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                    # 3. Missing Launch Phase - check Launch custom field (customfield_10441)
                    launch_field = fields.get('customfield_10441')
                    # Launch field is a dict with 'value' when set (e.g., {'value': 'Alpha', 'id': '27620'})
                    has_launch = launch_field and isinstance(launch_field, dict) and launch_field.get('value')

                    if not has_launch:
                        self._store_hygiene_issue(
                            cursor, 'features_missing_launch_phase', key, summary, ticket_url,
                            assignee_name, status, 'Feature has no Launch Phase set'
                        )
                        hygiene_data['features_missing_launch_phase'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                    # 4. Missing Proposed Milestone (customfield_10646) — single-
                    # select; option dict like {"value": "30. Milestone 30 ..."}.
                    milestone_field = fields.get('customfield_10646')
                    has_milestone = (
                        milestone_field
                        and isinstance(milestone_field, dict)
                        and (milestone_field.get('value') or '').strip()
                    )
                    if not has_milestone:
                        self._store_hygiene_issue(
                            cursor, 'features_missing_milestone', key, summary, ticket_url,
                            assignee_name, status, 'Feature has no Proposed Milestone set'
                        )
                        hygiene_data['features_missing_milestone'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 1. Epics with no parent Feature
                if issue_type == 'Epic' and not has_parent and status not in ['Abandoned', 'Duplicate']:
                    self._store_hygiene_issue(
                        cursor, 'epics_no_parent', key, summary, ticket_url,
                        assignee_name, status, 'Epic has no parent Feature'
                    )
                    hygiene_data['epics_no_parent'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name, 'status': status
                    })

                # 2. Epics with no description (all epics, not just in progress)
                if issue_type == 'Epic' and not description and status not in ['Abandoned', 'Duplicate']:
                    self._store_hygiene_issue(
                        cursor, 'epics_no_description', key, summary, ticket_url,
                        assignee_name, status, 'Epic has no description'
                    )
                    hygiene_data['epics_no_description'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name, 'status': status
                    })

                # 3. Epics without [BE] or [FE] prefix
                if issue_type == 'Epic' and status not in ['Abandoned', 'Duplicate']:
                    if not (summary.startswith('[BE]') or summary.startswith('[FE]')):
                        self._store_hygiene_issue(
                            cursor, 'epics_no_prefix', key, summary, ticket_url,
                            assignee_name, status, 'Epic summary does not start with [BE] or [FE]'
                        )
                        hygiene_data['epics_no_prefix'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 4. [FE] Epics without Figma link
                if issue_type == 'Epic' and summary.startswith('[FE]') and status not in ['Abandoned', 'Duplicate']:
                    # Check if description contains a Figma link
                    has_figma = False
                    if description:
                        # Convert description to string if it's not already
                        desc_text = str(description).lower()
                        has_figma = 'figma.com' in desc_text

                    # Also check customfield_11092 for Figma Design links
                    design_field = fields.get('customfield_11092')
                    if design_field and isinstance(design_field, list) and len(design_field) > 0:
                        has_figma = True

                    if not has_figma:
                        self._store_hygiene_issue(
                            cursor, 'epics_no_designs', key, summary, ticket_url,
                            assignee_name, status, '[FE] Epic has no Figma link in description'
                        )
                        hygiene_data['epics_no_designs'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 5. Epics with no child stories.
                # First check the in-memory map. If 0, verify with a direct Jira
                # query — but only within a 5-minute wall-clock budget across
                # all epics. Once budget is exhausted, remaining unverified
                # 0-child epics get flagged as 'epics_status_unknown' rather
                # than 'no child stories' to avoid false positives under load.
                if issue_type == 'Epic' and status not in ['Abandoned', 'Duplicate']:
                    child_count = epic_children.get(key, 0)
                    verified = child_count > 0  # in-memory hit = no verification needed

                    if child_count == 0:
                        # Bulk-verified count from the pre-pass takes priority
                        # over per-epic API calls. Falls back to the legacy
                        # per-epic verification only if the pre-pass missed
                        # this key (e.g., budget exhausted before this chunk).
                        if key in verified_child_count:
                            child_count = verified_child_count[key]
                            verified = True
                        elif verification_exhausted:
                            self._store_hygiene_issue(
                                cursor, 'epics_status_unknown', key, summary, ticket_url,
                                assignee_name, status, 'Child verification skipped (Jira slow)'
                            )
                            hygiene_data['epics_status_unknown'].append({
                                'key': key, 'summary': summary, 'url': ticket_url,
                                'assignee': assignee_name, 'status': status,
                            })
                            continue
                        else:
                            # Pre-pass missed this one (chunk error). Fall back
                            # to a single direct query for this epic only.
                            child_jql = f"parent = {key} AND type = Story AND status NOT IN (Abandoned, Duplicate)"
                            try:
                                child_data = self.jira.search_issues(child_jql, ['key'], max_results=2)
                                returned = child_data.get('issues') or []
                                reported_total = child_data.get('total')
                                child_count = max(len(returned), reported_total or 0)
                                verified = True
                            except Exception as e:
                                logger.warning(f"Child verification failed for {key}: {e}")
                                self._store_hygiene_issue(
                                    cursor, 'epics_status_unknown', key, summary, ticket_url,
                                    assignee_name, status, f'Child verification error: {e}'
                                )
                                hygiene_data['epics_status_unknown'].append({
                                    'key': key, 'summary': summary, 'url': ticket_url,
                                    'assignee': assignee_name, 'status': status,
                                })
                                continue

                    if verified and child_count == 0:
                        self._store_hygiene_issue(
                            cursor, 'epics_no_work_items', key, summary, ticket_url,
                            assignee_name, status, 'Epic has no child stories'
                        )
                        hygiene_data['epics_no_work_items'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 6. Epics with missing Acceptance Criteria
                if issue_type == 'Epic' and status not in ['Abandoned', 'Duplicate']:
                    # Check if Acceptance Criteria field (customfield_10230) has meaningful content
                    # This field uses ADF (Atlassian Document Format) which is a JSON structure
                    ac_field = fields.get('customfield_10230')

                    # Check if AC field has actual content in the ADF structure
                    has_ac = False
                    if ac_field and isinstance(ac_field, dict):
                        # ADF structure: {"type": "doc", "version": 1, "content": [...]}
                        content = ac_field.get('content', [])
                        if content and isinstance(content, list) and len(content) > 0:
                            # Has content nodes - check if they're meaningful (not just empty paragraphs)
                            for node in content:
                                if isinstance(node, dict):
                                    node_type = node.get('type', '')
                                    # Consider it valid if it has headings, lists, or task lists
                                    if node_type in ['heading', 'bulletList', 'orderedList', 'taskList']:
                                        has_ac = True
                                        break
                                    # Or paragraphs with actual text content
                                    if node_type == 'paragraph':
                                        node_content = node.get('content', [])
                                        if node_content and len(node_content) > 0:
                                            has_ac = True
                                            break

                    if not has_ac:
                        self._store_hygiene_issue(
                            cursor, 'epics_missing_acceptance_criteria', key, summary, ticket_url,
                            assignee_name, status, 'Epic has missing or minimal Acceptance Criteria'
                        )
                        hygiene_data['epics_missing_acceptance_criteria'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 7. Epics not assigned to any sprint.
                # We pull the Sprint field as customfield_10020; Jira returns
                # a list of sprint dicts (active, future, or closed). Empty
                # list / None = epic has never been planned. We only flag
                # non-terminal epics so backlog grooming surfaces what
                # genuinely needs scheduling.
                if issue_type == 'Epic' and status not in EXCLUDED_STATUSES and status != 'Done':
                    sprint_field = fields.get('customfield_10020')
                    has_sprint = bool(sprint_field) and isinstance(sprint_field, list) and len(sprint_field) > 0
                    if not has_sprint:
                        self._store_hygiene_issue(
                            cursor, 'epics_no_sprint', key, summary, ticket_url,
                            assignee_name, status, 'Epic is not assigned to any sprint'
                        )
                        hygiene_data['epics_no_sprint'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'status': status
                        })

                # 8. Epics in progress with no assignee.
                # Uses the shared IN_PROGRESS_STATUSES set so this stays in
                # lockstep with the dashboard's "in flight" definition (which
                # includes Blocked, Ready for Testing, Released to Test, etc.).
                if issue_type == 'Epic' and status in IN_PROGRESS_STATUSES and (
                    not assignee or assignee_name in (None, '', 'Unassigned')
                ):
                    self._store_hygiene_issue(
                        cursor, 'epics_in_progress_no_assignee', key, summary, ticket_url,
                        assignee_name, status,
                        f'Epic is {status} but has no assignee'
                    )
                    hygiene_data['epics_in_progress_no_assignee'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name, 'status': status
                    })

                # 3. Stories in progress with no parent Epic
                if issue_type == 'Story' and status == 'In Progress' and not has_parent and status not in ['Abandoned', 'Duplicate']:
                    self._store_hygiene_issue(
                        cursor, 'stories_no_parent', key, summary, ticket_url,
                        assignee_name, status, 'Story in progress with no parent Epic'
                    )
                    hygiene_data['stories_no_parent'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name
                    })

                # 4. Stories in progress with no story points
                if issue_type == 'Story' and status == 'In Progress' and not story_points and status not in ['Abandoned', 'Duplicate']:
                    self._store_hygiene_issue(
                        cursor, 'stories_no_points', key, summary, ticket_url,
                        assignee_name, status, 'Story in progress with no story points'
                    )
                    hygiene_data['stories_no_points'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name
                    })

                # 5. Stories in progress with no description
                if issue_type == 'Story' and status == 'In Progress' and not description and status not in ['Abandoned', 'Duplicate']:
                    self._store_hygiene_issue(
                        cursor, 'stories_no_description', key, summary, ticket_url,
                        assignee_name, status, 'Story in progress with no description'
                    )
                    hygiene_data['stories_no_description'].append({
                        'key': key, 'summary': summary, 'url': ticket_url,
                        'assignee': assignee_name
                    })

                # 6. Tickets in Code Review for > 24 hours
                if status in ['In Code Review', 'In code review'] and status not in ['Abandoned', 'Duplicate']:
                    hours_in_review = self._get_hours_in_status(cursor, key, status)
                    if hours_in_review and hours_in_review > 24:
                        self._store_hygiene_issue(
                            cursor, 'code_review_24h', key, summary, ticket_url,
                            assignee_name, status, f'In code review for {hours_in_review:.1f} hours'
                        )
                        hygiene_data['code_review_24h'].append({
                            'key': key, 'summary': summary, 'url': ticket_url,
                            'assignee': assignee_name, 'hours': hours_in_review
                        })

            # ---- Reconciliation ----------------------------------------
            # Mark any previously-open issue we did NOT re-detect this run
            # as resolved. Bumps times_resolved so the QA agent can reason
            # about flapping (issue resolves + reappears often = flaky rule
            # or real chronic regression).
            resolved_count = self._mark_vanished_as_resolved(cursor)
            if resolved_count:
                logger.info(f"  Resolved (no longer detected): {resolved_count}")

            # Expose the run summary on the checker so main() can build a
            # structured RunReport without recomputing anything.
            self.last_run_summary = {
                "seen_this_run": len(self._seen_this_run),
                "resolved_this_run": resolved_count,
                "by_type": {k: len(v) for k, v in hygiene_data.items()},
            }

            conn.commit()

            # Log summary
            logger.info("Hygiene check complete:")
            logger.info(f"  Features missing requirements: {len(hygiene_data['features_missing_requirements'])}")
            logger.info(f"  Features missing designs: {len(hygiene_data['features_missing_designs'])}")
            logger.info(f"  Features missing launch phase: {len(hygiene_data['features_missing_launch_phase'])}")
            logger.info(f"  Features missing proposed milestone: {len(hygiene_data['features_missing_milestone'])}")
            logger.info(f"  Epics without parent: {len(hygiene_data['epics_no_parent'])}")
            logger.info(f"  Epics without description: {len(hygiene_data['epics_no_description'])}")
            logger.info(f"  Epics without [BE]/[FE] prefix: {len(hygiene_data['epics_no_prefix'])}")
            logger.info(f"  [FE] Epics without Figma link: {len(hygiene_data['epics_no_designs'])}")
            logger.info(f"  Epics without child stories: {len(hygiene_data['epics_no_work_items'])}")
            logger.info(f"  Epics missing acceptance criteria: {len(hygiene_data['epics_missing_acceptance_criteria'])}")
            logger.info(f"  Epics not assigned to a sprint: {len(hygiene_data['epics_no_sprint'])}")
            logger.info(f"  Epics in progress without assignee: {len(hygiene_data['epics_in_progress_no_assignee'])}")
            logger.info(f"  Stories in progress without parent: {len(hygiene_data['stories_no_parent'])}")
            logger.info(f"  Stories in progress without points: {len(hygiene_data['stories_no_points'])}")
            logger.info(f"  Stories in progress without description: {len(hygiene_data['stories_no_description'])}")
            logger.info(f"  Tickets in code review > 24h: {len(hygiene_data['code_review_24h'])}")

            return hygiene_data

        finally:
            conn.close()

    def _mark_vanished_as_resolved(self, cursor) -> int:
        """Mark any currently-open hygiene row that wasn't re-detected this
        run as resolved. Returns the count updated.

        Opens an issue = `resolved_at IS NULL`. If the (type, ticket) pair is
        not in `self._seen_this_run`, set resolved_at = now and bump
        times_resolved. We never delete rows — the memory is the point.
        """
        now_iso = datetime.now().isoformat(timespec='seconds')
        cursor.execute(
            "SELECT id, issue_type, ticket_key FROM hygiene_issues WHERE resolved_at IS NULL"
        )
        open_rows = cursor.fetchall()
        resolved_ids = [
            row['id'] for row in open_rows
            if (row['issue_type'], row['ticket_key']) not in self._seen_this_run
        ]
        if not resolved_ids:
            return 0
        placeholders = ','.join('?' * len(resolved_ids))
        cursor.execute(
            f"""UPDATE hygiene_issues
                SET resolved_at = ?, times_resolved = times_resolved + 1
                WHERE id IN ({placeholders})""",
            (now_iso, *resolved_ids),
        )
        return len(resolved_ids)

    def _store_hygiene_issue(self, cursor, issue_type, ticket_key, summary,
                            ticket_url, assignee, status, details):
        """Record a hygiene issue with cross-run memory.

        Upsert rules:
          * If the (issue_type, ticket_key) pair already exists and is open
            (resolved_at IS NULL), bump times_seen and refresh last_seen_at.
          * If it exists but was previously resolved, treat this as a
            regression: clear resolved_at, bump times_seen.
          * If it's brand new, insert with first_seen_at = now, times_seen = 1.

        `detected_at` is kept in sync with last_seen_at so existing dashboard
        generators that still read the old column continue to work.
        """
        now_iso = datetime.now().isoformat(timespec='seconds')
        # If this exact (type, ticket) pair has already been stored in the
        # current run, skip the bump — rules can call us multiple times per
        # ticket and we want times_seen to count runs, not calls.
        already_this_run = (issue_type, ticket_key) in self._seen_this_run
        self._seen_this_run.add((issue_type, ticket_key))
        cursor.execute(
            "SELECT id, resolved_at, times_seen FROM hygiene_issues "
            "WHERE issue_type = ? AND ticket_key = ?",
            (issue_type, ticket_key),
        )
        existing = cursor.fetchone()
        if existing is None:
            # INSERT OR REPLACE protects against a retry race: when the main
            # hygiene loop retries after a `database is locked`, the initial
            # partial pass may have already committed some rows. A plain
            # INSERT would blow up the UNIQUE(issue_type, ticket_key).
            cursor.execute(
                """INSERT OR REPLACE INTO hygiene_issues
                   (issue_type, ticket_key, ticket_summary, ticket_url,
                    assignee_display_name, status, details,
                    detected_at, first_seen_at, last_seen_at,
                    resolved_at, times_seen, times_resolved)
                   VALUES (?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?,
                           NULL, 1, 0)""",
                (issue_type, ticket_key, summary, ticket_url, assignee,
                 status, details, now_iso, now_iso, now_iso),
            )
        elif already_this_run:
            # Just refresh mutable fields; don't bump times_seen again.
            cursor.execute(
                """UPDATE hygiene_issues
                   SET ticket_summary = ?, ticket_url = ?,
                       assignee_display_name = ?, status = ?, details = ?,
                       detected_at = ?, last_seen_at = ?, resolved_at = NULL
                   WHERE id = ?""",
                (summary, ticket_url, assignee, status, details,
                 now_iso, now_iso, existing['id']),
            )
        else:
            # First sighting of this issue this run — bump counters.
            cursor.execute(
                """UPDATE hygiene_issues
                   SET ticket_summary = ?,
                       ticket_url = ?,
                       assignee_display_name = ?,
                       status = ?,
                       details = ?,
                       detected_at = ?,
                       last_seen_at = ?,
                       times_seen = times_seen + 1,
                       resolved_at = NULL
                   WHERE id = ?""",
                (summary, ticket_url, assignee, status, details,
                 now_iso, now_iso, existing['id']),
            )

    def _get_hours_in_status(self, cursor, ticket_key, status):
        """
        Get hours a ticket has been in a specific status.

        Args:
            cursor: Database cursor
            ticket_key: Jira ticket key
            status: Status name

        Returns:
            Hours in status, or None if not tracked yet
        """
        cursor.execute("""
            SELECT entered_at
            FROM status_changes
            WHERE ticket_key = ? AND status = ? AND exited_at IS NULL
        """, (ticket_key, status))

        result = cursor.fetchone()
        if not result:
            return None

        entered_at = datetime.fromisoformat(result[0])
        now = datetime.now()
        hours = (now - entered_at).total_seconds() / 3600

        return hours


def _record_hygiene_memory_to_history(history: HistoryStore, config: dict,
                                       run_id: str, run_started: str) -> dict:
    """After the hygiene pass, mirror current state into qa_agent_core's
    HistoryStore. Each currently-open (issue_type, ticket_key) pair becomes a
    tracked issue; resolved ones get marked resolved. This gives us the same
    age / flake / escalation signals the QA agent already enjoys.

    Returns a dict of rule-level counts {check_key: {"open": int, "resolved_this_run": int}}.
    """
    with get_connection(config['database']['path']) as conn:
        cur = conn.cursor()
        # Rebuild state per rule from hygiene_issues itself.
        cur.execute(
            """SELECT issue_type, ticket_key, ticket_summary,
                      first_seen_at, last_seen_at, resolved_at,
                      times_seen, times_resolved
               FROM hygiene_issues"""
        )
        rows = cur.fetchall()

    per_rule: dict = {}
    for r in rows:
        rule = r["issue_type"]
        ticket = r["ticket_key"]
        summary = r["ticket_summary"] or ""
        bucket = per_rule.setdefault(rule, {"open": 0, "resolved_this_run": 0})
        message = f"{ticket}: {summary[:80]}"

        # Always upsert so we capture last_seen.
        if r["resolved_at"] is None:
            bucket["open"] += 1
            # Severity heuristic: anything that's "no description",
            # "no acceptance criteria", "no points", "no parent" is a warning.
            # "code_review_24h" and "no_work_items" lean critical after ~3d open.
            severity = "warning"
            if rule in ("code_review_24h", "epics_no_work_items"):
                severity = "warning"
            history.upsert_issue(
                check_key=f"hygiene:{rule}",
                severity=severity,
                message=message,
            )
        else:
            # Resolved in a previous run or this one. Check if it transitioned
            # this run (last_seen_at != resolved_at).
            ik = HistoryStore.issue_key(f"hygiene:{rule}", message)
            # If the QA history already shows it open, mark resolved.
            open_keys = history.open_issue_keys_for_check(f"hygiene:{rule}")
            if ik in open_keys:
                history.mark_resolved(ik)
                bucket["resolved_this_run"] += 1

    return per_rule


def main():
    """Main entry point."""
    run_id = make_run_id()
    run_started = datetime.utcnow().isoformat() + "Z"
    t0 = time.monotonic()
    rc = 0

    try:
        logger.info("=" * 60)
        logger.info(f"Jira Hygiene Agent - {datetime.now()}")
        logger.info("=" * 60)

        config = load_config()
        setup_logging(config)

        # Ensure schema (hygiene tables etc) exists — idempotent via CREATE IF NOT EXISTS
        _conn = get_connection(config['database']['path'])
        _conn.executescript(SCHEMA_SQL)
        # Add memory columns to hygiene_issues if we're running against an old DB.
        ensure_hygiene_memory_columns(_conn)
        _conn.commit()
        _conn.close()

        checker = JiraHygieneChecker(config)

        # Fetch all FNTSY tickets
        logger.info("Fetching all FNTSY tickets...")
        # Combined JQL: both FNTSY tickets and FEAT/INIT-185 Features in a
        # single round-trip. Previously this issued two separate searches; the
        # second-of-two adds ~5s of API time per hygiene run.
        # Custom fields included for both projects:
        #   customfield_11092 — Design link (FNTSY + FEAT)
        #   customfield_10230 — Acceptance Criteria (FNTSY)
        #   customfield_10020 — Sprint (FNTSY)
        #   customfield_10441 — Launch Phase (FEAT)
        #   customfield_10646 — Proposed Milestone (FEAT)
        jql = (
            "(project = FNTSY) OR "
            "(project = FEAT AND type = Feature AND parent = INIT-185) "
            "ORDER BY created DESC"
        )
        fields = [
            'summary', 'status', 'assignee', 'issuetype', 'description',
            config['jira']['story_points_field'], 'parent',
            'customfield_11092',   # Design link
            'customfield_10230',   # Acceptance Criteria (ADF format)
            'customfield_10020',   # Sprint
            'customfield_10441',   # Launch Phase (FEAT)
            'customfield_10646',   # Proposed Milestone (FEAT)
        ]
        for fallback_field in config['jira'].get('story_points_fallback_fields', []):
            if fallback_field not in fields:
                fields.append(fallback_field)

        data = checker.jira.search_issues(jql, fields, max_results=10500)
        issues = data.get('issues', [])

        logger.info(f"Retrieved {len(issues)} tickets (FNTSY + FEAT/INIT-185 combined)")

        # Track status changes + run hygiene rules. Retry on transient
        # `database is locked` — concurrent GitHub PR / collector writes
        # occasionally win the race and we're resilient to that.
        import sqlite3 as _sqlite3
        import time as _time
        HYGIENE_MAX_ATTEMPTS = 5
        HYGIENE_BACKOFF_S = [15, 30, 60, 90, 120]
        hygiene_data = None
        for attempt in range(HYGIENE_MAX_ATTEMPTS):
            try:
                logger.info("Tracking status changes...")
                checker.track_status_changes(issues)
                logger.info("Checking hygiene rules...")
                hygiene_data = checker.check_hygiene_issues(issues)
                break
            except _sqlite3.OperationalError as oe:
                if "locked" not in str(oe).lower() or attempt == HYGIENE_MAX_ATTEMPTS - 1:
                    raise
                wait = HYGIENE_BACKOFF_S[attempt]
                logger.warning(
                    "Hygiene hit `database is locked` on attempt %d; retrying in %ds…",
                    attempt + 1, wait,
                )
                _time.sleep(wait)

        # ---- Agent memory: mirror into qa_agent_core HistoryStore ---------
        history = HistoryStore()
        per_rule = _record_hygiene_memory_to_history(history, config, run_id, run_started)

        # ---- Build a RunReport so logs_dashboard can render the history ---
        checks = []
        for rule, counts in sorted(per_rule.items()):
            # Status: 'passed' if no open issues for this rule, otherwise 'failed'.
            status = "passed" if counts["open"] == 0 else "failed"
            checks.append({
                "key": f"hygiene:{rule}",
                "invariant": f"no tickets should fail rule '{rule}'",
                "decision": "run",
                "decision_reason": "",
                "status": status,
                "issues_count": counts["open"],
                "duration_s": 0.0,
                "state_hash": "",
                "started_at": run_started,
                "resolved_this_run": counts["resolved_this_run"],
            })
        total_open = sum(c["issues_count"] for c in checks)
        total_resolved = sum(c.get("resolved_this_run", 0) for c in checks)
        report = RunReport(
            run_id=run_id,
            started_at=run_started,
            finished_at=datetime.utcnow().isoformat() + "Z",
            duration_s=round(time.monotonic() - t0, 2),
            git_sha="",
            checks=checks,
            fixes=[],  # this agent doesn't apply fixes yet
            summary={
                "source_agent": "hygiene",
                "total_rules": len(checks),
                "open_issues": total_open,
                "resolved_this_run": total_resolved,
            },
            budget={"used_s": round(time.monotonic() - t0, 2)},
            proposals=[],
        )
        report.write()
        logger.info("Run report written → data/qa_runs.jsonl  (run_id=%s)", run_id)

        logger.info("✅ Hygiene check complete!")

        return 0

    except Exception as e:
        logger.error(f"❌ Hygiene check failed: {e}", exc_info=True)
        rc = 1
        # Still try to write a run report so failures are visible in the UI.
        # If even that write fails, log it — silent suppression here was hiding
        # signal from the QA agent's run-report consumer.
        try:
            report = RunReport(
                run_id=run_id,
                started_at=run_started,
                finished_at=datetime.utcnow().isoformat() + "Z",
                duration_s=round(time.monotonic() - t0, 2),
                git_sha="",
                checks=[],
                fixes=[],
                summary={"source_agent": "hygiene", "error": str(e)[:500]},
                budget={"used_s": round(time.monotonic() - t0, 2)},
                proposals=[],
            )
            report.write()
        except Exception as inner:
            logger.error(
                "Could not write hygiene failure report: %s (original error: %s)",
                inner, e,
            )
        return rc


if __name__ == "__main__":
    sys.exit(main())
