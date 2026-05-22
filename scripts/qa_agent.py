#!/usr/bin/env python3
"""
QA Agent - Automated data quality checks with auto-remediation.

Runs regular checks on dashboard data to identify errors, inconsistencies,
and data quality issues, then fixes what it can.

Default behavior (no flags) applies:
  - local repairs: delete orphan rows, future-dated snapshots, regenerate
    missing/broken HTML reports.
  - pipeline reruns: re-run jira_collector / hygiene agent when snapshot
    math drifts or hygiene false positives are detected.

Jira-visible actions (posting nudge comments) stay opt-in via --fix-jira.

Opt-out flags:
  --no-fix            Report only; disable all auto-fixes.
  --no-fix-local      Skip local repairs.
  --no-fix-pipelines  Skip pipeline reruns (keeps local repairs).
  --dry-run           Show what fixes would run without executing them.
  --deep              Add API-backed hygiene accuracy checks (slower).
"""

import argparse
import hashlib
import os
import subprocess
import sys
import time
import logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.statuses import (
    CLOSED_STATUSES,
    IN_PROGRESS_STATUSES,
    OPEN_STATUSES,
    EXCLUDED_STATUSES,
    KNOWN_STATUSES,
    sql_placeholders,
)
from utils.qa_agent_core import (
    Check,
    HistoryStore,
    Planner,
    ProposalQueue,
    RunReport,
    ToolCatalog,
    ToolSpec,
    Verifier,
    make_run_id,
)
from database.schema import get_connection

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _cron_is_outside_window(cron: str, now) -> bool:
    """Return True when the cron has an hour range (e.g. `*/15 6-18 * * *`)
    and `now` is outside that range. Used to suppress liveness warnings for
    agents that are supposed to be idle overnight.
    """
    if not cron:
        return False
    parts = cron.split()
    if len(parts) != 5:
        return False
    hour_field = parts[1]
    if '-' not in hour_field:
        return False
    try:
        start, end = hour_field.split('-', 1)
        start, end = int(start), int(end)
    except ValueError:
        return False
    # Cron hour ranges are inclusive (e.g. 6-18 means fires at minute markers
    # from 06:00 through 18:59, stopping after 18:59).
    return not (start <= now.hour <= end)


def _expected_cron_interval_ms(cron: str):
    """Best-effort expected interval between fires, in milliseconds.

    Supports the cron shapes we actually use:
      */N * * * *     → every N minutes, 24/7
      */N H-H * * *   → every N minutes within hours H..H (returns N min during window)
      N * * * *       → every hour at minute N
      0 H * * *       → daily at hour H (uses 24h)
      M H * * *       → daily at H:M (uses 24h)

    Returns None if the pattern isn't recognized — caller should then skip
    the liveness check rather than guess.
    """
    if not cron:
        return None
    parts = cron.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != '*' or month != '*' or dow != '*':
        return None

    MIN = 60 * 1000
    HOUR = 60 * MIN
    DAY = 24 * HOUR

    # */N * * * * — every N minutes, any hour
    if minute.startswith('*/') and hour == '*':
        try:
            return int(minute[2:]) * MIN
        except ValueError:
            return None
    # */N H-H * * * — every N minutes during an hour window.
    # We still treat cadence as N minutes because that's the right staleness
    # threshold when the job is inside its window; missed fires outside the
    # window aren't a bug.
    if minute.startswith('*/') and '-' in hour:
        try:
            return int(minute[2:]) * MIN
        except ValueError:
            return None
    # M * * * * — every hour at minute M
    if minute.isdigit() and hour == '*':
        return HOUR
    # M H * * * — daily
    if minute.isdigit() and hour.isdigit():
        return DAY
    return None


class QAAgent:
    """Agent for automated data quality assurance."""

    def __init__(self, config, deep=False):
        self.config = config
        self.db_path = config['database']['path']
        # Deep mode enables API-backed checks (e.g. verifying hygiene flags against
        # live Jira). These are slower (~30-60s) so the default fast run skips them.
        self.deep = deep
        self.issues = []
        self.stats = {
            'critical': 0,
            'warning': 0,
            'info': 0,
            'checks_passed': 0,
            'checks_failed': 0
        }
        self._jira = None  # lazy-constructed JiraAPICollector for deep checks
        # Agent-runtime extras — set by run_all_checks as each Check executes.
        self._active_check_key: str = ""
        self._check_pass_counted: bool = False

    def _jira_client(self):
        if self._jira is None:
            # Import lazily so the default fast run doesn't touch Jira deps
            from collectors.jira_api_collector import JiraAPICollector
            self._jira = JiraAPICollector(self.config)
        return self._jira

    def add_issue(self, severity, category, message, fix_suggestion=None, action=None):
        """Add an issue to the report.

        `action` is an optional dict the FixEngine knows how to execute. Shape:
          {'type': '<handler name>', **kwargs}
        Example:
          {'type': 'delete_ticket', 'ticket_key': 'FNTSY-123'}
        """
        self.issues.append({
            'severity': severity,
            'category': category,
            'message': message,
            'fix': fix_suggestion,
            'action': action,
            'timestamp': datetime.now().isoformat(),
            # Auto-tag with the active Check's key so downstream (history,
            # verifier) can attribute the issue to the right invariant. Falls
            # back to 'category' for legacy call sites.
            'check_key': self._active_check_key or category,
        })
        self.stats[severity] += 1
        self.stats['checks_failed'] += 1

    def check_passed(self):
        """Record a successful check."""
        self.stats['checks_passed'] += 1
        self._check_pass_counted = True

    def issues_for_check(self, check_key):
        return [i for i in self.issues if i.get('check_key') == check_key]

    def reset_issues_for_check(self, check_key):
        """Remove issues emitted by a specific Check (used by the Verifier
        so re-running the check doesn't double-count stale rows)."""
        kept = []
        dropped = 0
        for i in self.issues:
            if i.get('check_key') == check_key:
                self.stats[i['severity']] = max(0, self.stats[i['severity']] - 1)
                dropped += 1
                continue
            kept.append(i)
        self.issues = kept
        if dropped:
            self.stats['checks_failed'] = max(0, self.stats['checks_failed'] - dropped)

    def check_story_points_consistency(self):
        """Validate story points add up correctly."""
        logger.info("Checking story points consistency...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check sprint snapshots vs actual ticket totals.
            # Status sets come from src/utils/statuses.py so this query stays
            # in sync with the dashboard generators and the burndown math.
            closed_ph = sql_placeholders(CLOSED_STATUSES)
            excl_ph = sql_placeholders(EXCLUDED_STATUSES)
            cursor.execute(f"""
                SELECT
                    s.sprint_name,
                    ss.total_story_points,
                    ss.completed_story_points,
                    ss.remaining_story_points,
                    COALESCE(SUM(t.story_points), 0) as actual_total,
                    COALESCE(SUM(CASE WHEN t.status IN ({closed_ph}) THEN t.story_points ELSE 0 END), 0) as actual_completed
                FROM sprint_snapshots ss
                JOIN sprints s ON ss.sprint_id = s.sprint_id
                LEFT JOIN tickets t ON t.sprint_id = s.sprint_id AND t.issue_type = 'Story' AND t.status NOT IN ({excl_ph})
                WHERE s.state IN ('active', 'future')
                GROUP BY s.sprint_id, s.sprint_name, ss.snapshot_id
                HAVING ss.snapshot_timestamp = (
                    SELECT MAX(snapshot_timestamp) FROM sprint_snapshots WHERE sprint_id = s.sprint_id
                )
            """, (*CLOSED_STATUSES, *EXCLUDED_STATUSES))

            for row in cursor.fetchall():
                sprint_name, snapshot_total, snapshot_completed, snapshot_remaining, actual_total, actual_completed = row

                # Check if snapshot total matches actual
                if abs(snapshot_total - actual_total) > 0.1:
                    self.add_issue(
                        'warning',
                        'Story Points',
                        f"{sprint_name}: Snapshot total ({snapshot_total:.1f} SP) doesn't match actual tickets ({actual_total:.1f} SP)",
                        "Run daily_burndown_agent.py to refresh snapshots"
                    )
                else:
                    self.check_passed()

                # Check if completed + remaining = total
                if abs((snapshot_completed + snapshot_remaining) - snapshot_total) > 0.1:
                    self.add_issue(
                        'critical',
                        'Story Points',
                        f"{sprint_name}: Math error - Completed ({snapshot_completed:.1f}) + Remaining ({snapshot_remaining:.1f}) ≠ Total ({snapshot_total:.1f})",
                        "Check refresh_jira_data.py snapshot calculation logic"
                    )
                else:
                    self.check_passed()

            # Check for negative story points
            cursor.execute("""
                SELECT ticket_key, story_points
                FROM tickets
                WHERE story_points < 0
            """)
            negative_sp = cursor.fetchall()
            if negative_sp:
                for ticket_key, sp in negative_sp:
                    self.add_issue(
                        'critical',
                        'Story Points',
                        f"{ticket_key}: Negative story points ({sp})",
                        f"UPDATE tickets SET story_points = 0 WHERE ticket_key = '{ticket_key}'"
                    )
            else:
                self.check_passed()

            # Check story points dashboard breakdown consistency. Lists come
            # from src/utils/statuses.py so they can't drift from the dashboard.
            closed_ph = sql_placeholders(CLOSED_STATUSES)
            inprog_ph = sql_placeholders(IN_PROGRESS_STATUSES)
            open_ph = sql_placeholders(OPEN_STATUSES)
            excl_ph = sql_placeholders(EXCLUDED_STATUSES)
            params = (
                list(CLOSED_STATUSES)
                + list(IN_PROGRESS_STATUSES)
                + list(OPEN_STATUSES)
                + list(EXCLUDED_STATUSES)
            )
            cursor.execute(f"""
                SELECT
                    s.sprint_name,
                    COALESCE(SUM(CASE WHEN t.status IN ({closed_ph}) THEN t.story_points ELSE 0 END), 0) as closed_sp,
                    COALESCE(SUM(CASE WHEN t.status IN ({inprog_ph}) THEN t.story_points ELSE 0 END), 0) as in_progress_sp,
                    COALESCE(SUM(CASE WHEN t.status IN ({open_ph}) THEN t.story_points ELSE 0 END), 0) as open_sp,
                    COALESCE(SUM(CASE WHEN t.status IN ({excl_ph}) THEN t.story_points ELSE 0 END), 0) as excluded_sp,
                    COALESCE(SUM(t.story_points), 0) as total_sp
                FROM sprints s
                LEFT JOIN tickets t ON t.sprint_id = s.sprint_id AND t.issue_type = 'Story'
                WHERE s.state = 'active'
                GROUP BY s.sprint_id, s.sprint_name
            """, params)

            for row in cursor.fetchall():
                sprint_name, closed_sp, in_progress_sp, open_sp, excluded_sp, total_sp = row
                breakdown_sum = closed_sp + in_progress_sp + open_sp

                # The dashboard now recomputes Total SP from the three active buckets
                # (Closed + In Progress + Open) and excludes Abandoned/Duplicate. So we
                # check that those three buckets account for all non-excluded SP.
                non_excluded_total = total_sp - excluded_sp
                if abs(breakdown_sum - non_excluded_total) > 0.1:
                    self.add_issue(
                        'critical',
                        'Story Points',
                        f"{sprint_name}: Active-status breakdown ({breakdown_sum:.1f}) doesn't match non-excluded total ({non_excluded_total:.1f}). Closed ({closed_sp:.1f}) + In Progress ({in_progress_sp:.1f}) + Open ({open_sp:.1f}); Abandoned/Duplicate={excluded_sp:.1f}, DB total={total_sp:.1f}",
                        "A ticket status is not mapped in qa_agent.py / generate_html_report.py"
                    )
                else:
                    self.check_passed()

        finally:
            conn.close()

    def check_ticket_integrity(self):
        """Validate ticket data integrity."""
        logger.info("Checking ticket integrity...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check for duplicate ticket keys
            cursor.execute("""
                SELECT ticket_key, COUNT(*) as count
                FROM tickets
                GROUP BY ticket_key
                HAVING count > 1
            """)
            duplicates = cursor.fetchall()
            if duplicates:
                for ticket_key, count in duplicates:
                    self.add_issue(
                        'critical',
                        'Ticket Integrity',
                        f"Duplicate ticket key: {ticket_key} appears {count} times",
                        f"Review tickets table - may be caused by epic multi-sprint storage"
                    )
            else:
                self.check_passed()

            # Check for tickets with invalid sprint references
            cursor.execute("""
                SELECT t.ticket_key
                FROM tickets t
                LEFT JOIN sprints s ON t.sprint_id = s.sprint_id
                WHERE s.sprint_id IS NULL
            """)
            orphaned = cursor.fetchall()
            if orphaned:
                for (ticket_key,) in orphaned:
                    self.add_issue(
                        'critical',
                        'Ticket Integrity',
                        f"{ticket_key}: References non-existent sprint",
                        f"DELETE FROM tickets WHERE ticket_key = '{ticket_key}'",
                        action={'type': 'delete_orphan_ticket', 'ticket_key': ticket_key, 'scope': 'local'},
                    )
            else:
                self.check_passed()

            # Check for unknown statuses — anything outside KNOWN_STATUSES is
            # a new workflow state we should add to src/utils/statuses.py.
            cursor.execute("""
                SELECT DISTINCT status
                FROM tickets
            """)
            for (status,) in cursor.fetchall():
                if status not in KNOWN_STATUSES:
                    # Critical because any unknown status silently falls out of
                    # CLOSED/IN_PROGRESS/OPEN buckets and undercounts SP until
                    # src/utils/statuses.py gets updated.
                    self.add_issue(
                        'critical',
                        'Ticket Integrity',
                        f"Unknown status '{status}' — tickets with this status are invisible to SP buckets until it's added to src/utils/statuses.py.",
                        "Add the status to CLOSED_STATUSES, IN_PROGRESS_STATUSES, OPEN_STATUSES, or EXCLUDED_STATUSES in src/utils/statuses.py"
                    )

        finally:
            conn.close()

    def check_sprint_data_quality(self):
        """Validate sprint data."""
        logger.info("Checking sprint data quality...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check for invalid sprint dates
            cursor.execute("""
                SELECT sprint_name, start_date, end_date
                FROM sprints
                WHERE start_date >= end_date
            """)
            invalid_dates = cursor.fetchall()
            if invalid_dates:
                for sprint_name, start, end in invalid_dates:
                    self.add_issue(
                        'critical',
                        'Sprint Data',
                        f"{sprint_name}: Invalid dates - start ({start}) >= end ({end})",
                        "Check Jira sprint configuration"
                    )
            else:
                self.check_passed()

            # Check for active sprints with no tickets
            cursor.execute("""
                SELECT s.sprint_name, COUNT(t.ticket_id) as ticket_count
                FROM sprints s
                LEFT JOIN tickets t ON t.sprint_id = s.sprint_id
                WHERE s.state = 'active'
                GROUP BY s.sprint_id, s.sprint_name
                HAVING ticket_count = 0
            """)
            empty_sprints = cursor.fetchall()
            if empty_sprints:
                for sprint_name, count in empty_sprints:
                    self.add_issue(
                        'warning',
                        'Sprint Data',
                        f"{sprint_name}: Active sprint has no tickets",
                        "Run daily_burndown_agent.py to refresh data"
                    )
            else:
                self.check_passed()

            # Check for sprint_snapshots that predate the sprint's own start
            # date. These are collected while the sprint is in "future" state
            # and will anchor the burndown ideal line at a misleadingly low SP
            # value, producing false "X SP behind" pace readings.
            cursor.execute("""
                SELECT s.sprint_name, MIN(ss.snapshot_date) as earliest_snap, s.start_date
                FROM sprint_snapshots ss
                JOIN sprints s ON ss.sprint_id = s.sprint_id
                WHERE s.state = 'active'
                GROUP BY ss.sprint_id
                HAVING date(earliest_snap) < date(s.start_date)
            """)
            pre_sprint_snaps = cursor.fetchall()
            if pre_sprint_snaps:
                for sprint_name, earliest, sprint_start in pre_sprint_snaps:
                    self.add_issue(
                        'warning',
                        'Sprint Burndown',
                        f"{sprint_name}: sprint_snapshots contain pre-sprint rows "
                        f"(earliest: {earliest}, sprint starts: {sprint_start[:10]}). "
                        "The burndown ideal line is anchored at sprint start — pre-sprint rows are ignored in charts.",
                        "No action needed — the generator already filters these out. "
                        "Optionally delete pre-sprint rows: DELETE FROM sprint_snapshots WHERE sprint_id=? AND snapshot_date < sprint.start_date"
                    )
            else:
                self.check_passed()

            # Check for active sprints with stories but zero story points
            cursor.execute("""
                SELECT
                    s.sprint_name,
                    COUNT(CASE WHEN t.issue_type = 'Story' THEN 1 END) as story_count,
                    SUM(CASE WHEN t.issue_type = 'Story' THEN t.story_points ELSE 0 END) as total_sp
                FROM sprints s
                JOIN tickets t ON t.sprint_id = s.sprint_id
                WHERE s.state = 'active'
                GROUP BY s.sprint_id, s.sprint_name
                HAVING story_count > 0 AND total_sp = 0
            """)
            zero_sp_sprints = cursor.fetchall()
            if zero_sp_sprints:
                for sprint_name, story_count, total_sp in zero_sp_sprints:
                    self.add_issue(
                        'critical',
                        'Sprint Data',
                        f"{sprint_name}: Has {story_count} stories but ALL have 0 story points!",
                        "Story points are not set in Jira. Update tickets with story point estimates or check if customfield_10016 is the correct field"
                    )
            else:
                self.check_passed()

        finally:
            conn.close()

    def check_epic_consistency(self):
        """Validate epic data."""
        logger.info("Checking epic consistency...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check for epics with duplicate base keys
            cursor.execute("""
                SELECT
                    CASE
                        WHEN ticket_key LIKE '%_s%' THEN SUBSTR(ticket_key, 1, INSTR(ticket_key, '_s') - 1)
                        ELSE ticket_key
                    END as base_key,
                    COUNT(*) as count,
                    GROUP_CONCAT(ticket_key) as all_keys
                FROM tickets
                WHERE issue_type = 'Epic'
                GROUP BY base_key
            """)
            for base_key, count, all_keys in cursor.fetchall():
                # This is expected for epics in multiple sprints
                if count > 4:  # Alert if same epic in more than 4 sprints
                    self.add_issue(
                        'info',
                        'Epic Consistency',
                        f"{base_key}: Appears in {count} sprints - may be spreading too thin",
                        "Review epic scope and sprint assignments"
                    )
                else:
                    self.check_passed()

        finally:
            conn.close()

    def check_developer_metrics(self):
        """Validate developer metrics consistency."""
        logger.info("Checking developer metrics...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check if team members in config exist in tickets
            team_member_ids = [m['jira_account_id'] for m in self.config.get('team_members', [])]

            cursor.execute("""
                SELECT DISTINCT assignee_account_id
                FROM tickets
                WHERE assignee_account_id IS NOT NULL
                    AND sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active')
            """)

            active_assignees = [row[0] for row in cursor.fetchall()]
            unknown_assignees = [a for a in active_assignees if a not in team_member_ids and a]

            if unknown_assignees:
                self.add_issue(
                    'warning',
                    'Developer Metrics',
                    f"Found {len(unknown_assignees)} assignees not in team config",
                    "Add missing team members to config/team_config.yaml"
                )
            else:
                self.check_passed()

            # Check for developer snapshots without matching tickets
            cursor.execute("""
                SELECT ds.developer_name, ds.assigned_story_points
                FROM developer_snapshots ds
                LEFT JOIN tickets t ON t.assignee_account_id = ds.developer_id
                    AND t.sprint_id = ds.sprint_id
                WHERE ds.sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active')
                GROUP BY ds.developer_id, ds.developer_name, ds.assigned_story_points
                HAVING COUNT(t.ticket_id) = 0 AND ds.assigned_story_points > 0
            """)

            mismatched = cursor.fetchall()
            if mismatched:
                for dev_name, sp in mismatched:
                    self.add_issue(
                        'warning',
                        'Developer Metrics',
                        f"{dev_name}: Snapshot shows {sp:.1f} SP but no tickets found",
                        "Run jira_collector_agent.py to refresh snapshots",
                        action={'type': 'rerun_jira_collector', 'scope': 'pipelines'},
                    )
            else:
                self.check_passed()

            # Check for inconsistency between snapshot counts and actual ticket counts.
            # Bucket lists come from src/utils/statuses.py so they can't drift.
            open_ph = sql_placeholders(OPEN_STATUSES)
            inprog_ph = sql_placeholders(IN_PROGRESS_STATUSES)
            closed_ph = sql_placeholders(CLOSED_STATUSES)
            params = list(OPEN_STATUSES) + list(IN_PROGRESS_STATUSES) + list(CLOSED_STATUSES)
            cursor.execute(f"""
                SELECT
                    ds.developer_name,
                    ds.tickets_todo,
                    ds.tickets_in_progress,
                    ds.tickets_completed,
                    COUNT(CASE WHEN t.status IN ({open_ph}) THEN 1 END) as actual_todo,
                    COUNT(CASE WHEN t.status IN ({inprog_ph}) THEN 1 END) as actual_in_progress,
                    COUNT(CASE WHEN t.status IN ({closed_ph}) THEN 1 END) as actual_completed
                FROM developer_snapshots ds
                JOIN tickets t ON t.assignee_account_id = ds.developer_id
                    AND t.sprint_id = ds.sprint_id
                    AND t.issue_type = 'Story'
                    AND t.status NOT IN ('Abandoned', 'Duplicate')
                WHERE ds.sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active')
                    AND ds.snapshot_date = date('now')
                GROUP BY ds.developer_id, ds.developer_name, ds.tickets_todo, ds.tickets_in_progress, ds.tickets_completed
                HAVING ds.tickets_todo != actual_todo
                    OR ds.tickets_in_progress != actual_in_progress
                    OR ds.tickets_completed != actual_completed
            """, params)

            inconsistencies = cursor.fetchall()
            if inconsistencies:
                for dev_name, snap_todo, snap_prog, snap_done, act_todo, act_prog, act_done in inconsistencies:
                    self.add_issue(
                        'critical',
                        'Developer Metrics',
                        f"{dev_name}: Snapshot counts don't match tickets (TODO: {snap_todo} vs {act_todo}, In Progress: {snap_prog} vs {act_prog}, Done: {snap_done} vs {act_done})",
                        "Regenerate snapshots by running jira_collector_agent.py",
                        action={'type': 'rerun_jira_collector', 'scope': 'pipelines'},
                    )
            else:
                self.check_passed()

        finally:
            conn.close()

    def check_temporal_consistency(self):
        """Validate time-based data consistency."""
        logger.info("Checking temporal consistency...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()

        try:
            # Check for future-dated snapshots
            cursor.execute("""
                SELECT s.sprint_name, ss.snapshot_date
                FROM sprint_snapshots ss
                JOIN sprints s ON ss.sprint_id = s.sprint_id
                WHERE date(ss.snapshot_date) > date('now')
            """)
            future_snapshots = cursor.fetchall()
            if future_snapshots:
                for sprint_name, snapshot_date in future_snapshots:
                    self.add_issue(
                        'critical',
                        'Temporal Consistency',
                        f"{sprint_name}: Future-dated snapshot ({snapshot_date})",
                        "Check system clock or delete invalid snapshot",
                        action={'type': 'delete_future_snapshot', 'snapshot_date': snapshot_date, 'scope': 'local'},
                    )
            else:
                self.check_passed()

            # Check for stale data (no updates in 24 hours)
            cursor.execute("""
                SELECT MAX(snapshot_timestamp) as last_update
                FROM sprint_snapshots
                WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active')
            """)
            row = cursor.fetchone()
            if row and row[0]:
                last_update = datetime.fromisoformat(row[0])
                hours_since_update = (datetime.now() - last_update).total_seconds() / 3600
                if hours_since_update > 24:
                    # Attach a pipeline action so the FixEngine can self-heal
                    # by re-running the Jira Collector next auto-fix pass.
                    self.add_issue(
                        'warning',
                        'Temporal Consistency',
                        f"Data is stale - last update was {hours_since_update:.1f} hours ago",
                        "Rerun jira_collector_agent.py to refresh snapshots.",
                        action={'type': 'rerun_jira_collector', 'scope': 'pipelines'},
                    )
                else:
                    self.check_passed()

        finally:
            conn.close()

    def check_html_reports(self):
        """Validate HTML reports are generated and don't contain template errors."""
        logger.info("Checking HTML reports...")

        report_dir = Path(__file__).parent.parent / "reports" / "html"
        required_reports = [
            'team_dashboard.html',
            'story_points_dashboard.html',
            'epics_dashboard.html',
            'past_sprints_dashboard.html',
            'team_members_dashboard.html',
            'pull_requests_dashboard.html',
            'logs_dashboard.html'
        ]

        for report_name in required_reports:
            report_path = report_dir / report_name

            # Check if file exists
            if not report_path.exists():
                self.add_issue(
                    'warning',
                    'HTML Reports',
                    f"Missing report: {report_name}",
                    "Run generate_html_report.py to regenerate reports",
                    action={'type': 'regenerate_reports', 'scope': 'local'},
                )
                continue

            # Check file size (should be > 1KB)
            file_size = report_path.stat().st_size
            if file_size < 1024:
                self.add_issue(
                    'warning',
                    'HTML Reports',
                    f"{report_name} is suspiciously small ({file_size} bytes)",
                    "Report may be empty or failed to generate properly",
                    action={'type': 'regenerate_reports', 'scope': 'local'},
                )
                continue

            # Check page is not stale (collector runs every 15 min; allow 30 min before flagging)
            age_minutes = (datetime.now().timestamp() - report_path.stat().st_mtime) / 60
            if age_minutes > 30:
                self.add_issue(
                    'critical',
                    'HTML Reports',
                    f"{report_name} is stale — last generated {age_minutes:.0f} minutes ago",
                    "run_jira_collector_agent.sh should regenerate HTML after each collection; check cron and logs",
                    action={'type': 'regenerate_reports', 'scope': 'local'},
                )
                continue

            # Check for template variable remnants or common errors
            content = report_path.read_text()

            # Check for Python f-string remnants
            # Only flag {variable} patterns, not ${variable} (JavaScript template literals)
            if '{' in content and '}' in content:
                import re
                # Match {variable} but NOT ${variable} (JavaScript) or CSS values
                # Look for {word} that's NOT preceded by $
                suspicious_patterns = re.findall(r'(?<!\$)\{([a-z_][a-z0-9_]*)\}', content, re.IGNORECASE)
                if suspicious_patterns:
                    # Filter out common false positives (CSS, JS variables)
                    filtered = [v for v in suspicious_patterns if not v.startswith('0x') and len(v) > 1]
                    if filtered:
                        self.add_issue(
                            'critical',
                            'HTML Reports',
                            f"{report_name} contains unevaluated template variables: {', '.join(set(filtered[:5]))}",
                            "Check generate_html_report.py for missing variable definitions",
                            action={'type': 'regenerate_reports', 'scope': 'local'},
                        )
                        continue

            self.check_passed()

    def check_hygiene_tracking(self):
        """Validate hygiene issues are being tracked."""
        logger.info("Checking hygiene tracking...")

        # Check if hygiene_issues table exists and has data
        try:
            conn = get_connection(self.db_path)
            cursor = conn.cursor()

            try:
                # Check total hygiene issues
                cursor.execute("SELECT COUNT(*) FROM hygiene_issues")
                total_issues = cursor.fetchone()[0]

                if total_issues == 0:
                    self.add_issue(
                        'warning',
                        'Hygiene Tracking',
                        'No hygiene issues found in database',
                        'Run jira_hygiene_agent.py to populate hygiene data',
                        action={'type': 'rerun_hygiene_agent', 'scope': 'pipelines'},
                    )
                else:
                    self.check_passed()

                # Check that Feature hygiene rules are being tracked
                cursor.execute("""
                    SELECT COUNT(*) FROM hygiene_issues
                    WHERE issue_type LIKE 'features_%'
                """)
                feature_issues = cursor.fetchone()[0]

                # We expect at least some Feature hygiene issues since we have FEAT project with INIT-185 parent
                if feature_issues == 0:
                    self.add_issue(
                        'warning',
                        'Hygiene Tracking',
                        'No Feature hygiene issues tracked (expected Features with parent INIT-185)',
                        'Verify jira_hygiene_agent.py is fetching FEAT project tickets'
                    )
                else:
                    self.check_passed()

            finally:
                conn.close()

        except Exception as e:
            self.add_issue(
                'critical',
                'Hygiene Tracking',
                f'Hygiene database error: {str(e)}',
                'Check if hygiene tables are initialized'
            )

    def check_github_collection_saturation(self):
        """Warn if any GitHub PR collection result is pegged at the cap.

        `gh search prs` returns at most 1000 rows. If a developer has
        exactly that many merged PRs in the lookback window (or any
        --reviewed-by / --commenter result at cap), we're silently
        truncating and undercounting their activity.
        """
        logger.info("Checking for GitHub collection saturation...")
        CAP = 1000
        conn = get_connection(self.db_path)
        cursor = conn.cursor()
        try:
            # Author-side saturation: merged PRs in last 90 days per user.
            cursor.execute("""
                SELECT author_github_username, COUNT(*) AS n
                FROM github_prs
                WHERE state = 'merged'
                  AND merged_at >= date('now', '-90 days')
                GROUP BY author_github_username
                HAVING n >= ?
            """, (CAP,))
            saturated_authors = cursor.fetchall()
            if saturated_authors:
                for login, n in saturated_authors:
                    self.add_issue(
                        'warning',
                        'GitHub Collection',
                        f"{login} has {n} merged PRs (at or above {CAP}-row cap). Counts may be truncated.",
                        "Paginate in github_collector._get_merged_prs instead of relying on --limit."
                    )
            else:
                self.check_passed()

            # Reviewer-side saturation: reviews per user.
            cursor.execute("""
                SELECT reviewer_github_username, COUNT(*) AS n
                FROM github_reviews
                GROUP BY reviewer_github_username
                HAVING n >= ?
            """, (CAP,))
            saturated_reviewers = cursor.fetchall()
            if saturated_reviewers:
                for login, n in saturated_reviewers:
                    self.add_issue(
                        'warning',
                        'GitHub Collection',
                        f"{login} has {n} reviews captured (at or above {CAP}-row cap). Counts may be truncated.",
                        "Paginate in github_collector._search_prs_for_reviewer."
                    )
            else:
                self.check_passed()
        finally:
            conn.close()

    def check_logs_page_accuracy(self):
        """Validate that reports/html/logs_dashboard.html reflects current reality.

        The Logs page is generated from scheduled_tasks.json + DB + log files.
        If it's been rendered after state changed (new cron added, agent last-run
        timestamp updated), the page can lie silently. This check compares what
        the page *should* say today against what the page *does* say today:

          1. Every essential agent has a card on the page.
          2. Every scheduled agent displays its real cron — not 'On demand'.
          3. The page isn't stale by more than a render cycle (~2h).
        """
        logger.info("Checking logs page accuracy...")
        page_path = Path(__file__).parent.parent / "reports" / "html" / "logs_dashboard.html"
        if not page_path.exists():
            self.add_issue(
                'critical',
                'Logs Page',
                "logs_dashboard.html is missing.",
                "Run generate_logs_dashboard.py to create it.",
                action={'type': 'regenerate_reports', 'scope': 'local'},
            )
            return

        html = page_path.read_text()

        # Load scheduled tasks so we know what the page *should* show.
        scheduled_path = Path(__file__).parent.parent / ".claude" / "scheduled_tasks.json"
        scheduled_crons = {}
        if scheduled_path.exists():
            try:
                import json as _json
                data = _json.loads(scheduled_path.read_text())
                for t in data.get('tasks', []):
                    scheduled_crons[t['id']] = t.get('cron', '')
            except Exception:
                pass

        # (Task id in scheduled_tasks.json, human-readable agent name shown on the page)
        # Keep this list in sync with generate_logs_dashboard.py's agent definitions.
        expected_cards = [
            ('jira_hygiene_agent',  'Jira Hygiene Agent'),
            ('jira_collector',      'Jira Collector'),
            ('calendar_sync',       'Calendar Sync'),
            ('qa_agent_auto_fix',   'QA Agent'),
            ('github_pr_agent',     'GitHub PR Agent'),
            ('db_backup',           None),  # db_backup has no card yet — skip card check
        ]

        # 1) Coverage: every essential agent has a card on the page.
        # Card headings include an emoji prefix (e.g. "🧹 Jira Hygiene Agent"),
        # so we match on `<name></h3>` instead of the raw substring.
        for task_id, card_name in expected_cards:
            if card_name is None:
                continue
            if f"{card_name}</h3>" not in html:
                self.add_issue(
                    'critical',
                    'Logs Page',
                    f"'{card_name}' card is missing from logs_dashboard.html.",
                    "Add the agent definition in generate_logs_dashboard.py and regenerate.",
                    action={'type': 'regenerate_reports', 'scope': 'local'},
                )
            else:
                self.check_passed()

        # 2) Cron accuracy: if an agent is scheduled, its cron string must appear
        # on the page. If the page says 'On demand' for a scheduled agent, the
        # page is stale relative to scheduled_tasks.json.
        for task_id, cron in scheduled_crons.items():
            if not cron:
                continue
            card_name = next((n for tid, n in expected_cards if tid == task_id and n), None)
            if not card_name:
                continue
            # The generator renders the cron literal in a backtick code span.
            if cron not in html:
                self.add_issue(
                    'warning',
                    'Logs Page',
                    f"'{card_name}' is scheduled ({cron}) but its cron isn't on the logs page — page is stale.",
                    "Regenerate: python3 scripts/generate_logs_dashboard.py",
                    action={'type': 'regenerate_reports', 'scope': 'local'},
                )
            else:
                self.check_passed()

        # 3) Each agent card's `log_file` must exist and show recent activity.
        # Hunts down the common failure where a card points at a typo'd or
        # shared log file (e.g. 'hygiene_agent.log' instead of
        # 'jira_hygiene_agent.log', or the multi-script 'collector.log').
        # Pulls the per-agent `log_file` setting straight from the generator.
        try:
            gen_src = (Path(__file__).parent / "generate_logs_dashboard.py").read_text()
            import re as _re
            card_logs = _re.findall(
                r"'id'\s*:\s*'([\w\-]+)'[^}]*?'name'\s*:\s*'([^']+)'[^}]*?'log_file'\s*:\s*'([^']+)'",
                gen_src, flags=_re.DOTALL,
            )
        except Exception:
            card_logs = []

        logs_dir = Path(__file__).parent.parent / "logs"
        # Log files shared across multiple scripts — pointing a card at one
        # of these leaks unrelated lines into the agent's card.
        SHARED_LOG_FILES = {"collector.log", "cron.log"}
        for agent_id, card_name, log_file in card_logs:
            log_path = logs_dir / log_file
            if not log_path.exists():
                self.add_issue(
                    'warning',
                    'Logs Page',
                    f"'{card_name}' card points at {log_file} but no such file exists under logs/.",
                    f"Update generate_logs_dashboard.py or the agent's script to emit {log_file}.",
                )
                continue
            if log_file in SHARED_LOG_FILES:
                self.add_issue(
                    'warning',
                    'Logs Page',
                    f"'{card_name}' card points at the shared {log_file} — log lines from "
                    f"unrelated scripts will leak into its card.",
                    f"Give {card_name}'s script its own log file and update its log_file mapping.",
                )
                continue
            # Recent activity: if the agent has a scheduled cron, the log should
            # be no older than ~2 expected intervals. No cron => on-demand; skip.
            cron = scheduled_crons.get(agent_id.replace('-', '_'), '')
            if not cron:
                # generate_logs_dashboard's id uses hyphens in some places but
                # scheduled_tasks.json uses underscores. Try a couple of lookups.
                for task_id in scheduled_crons:
                    if task_id.replace('_', '-') == agent_id:
                        cron = scheduled_crons[task_id]
                        break
            if not cron:
                self.check_passed()
                continue
            expected_ms = _expected_cron_interval_ms(cron)
            if not expected_ms:
                self.check_passed()
                continue
            # Check if the cron is currently in its off-window (e.g., `*/15 6-18 * * *`
            # at 2 AM) — we can't fault the log for being quiet during scheduled quiet hours.
            try:
                if _cron_is_outside_window(cron, datetime.now()):
                    self.check_passed()
                    continue
            except Exception:
                pass
            age_s = datetime.now().timestamp() - log_path.stat().st_mtime
            # Allow 2× the expected interval before flagging — agents jitter.
            threshold_s = (expected_ms / 1000) * 2
            if age_s > threshold_s:
                self.add_issue(
                    'warning',
                    'Logs Page',
                    f"'{card_name}' log {log_file} is {age_s/3600:.1f}h old but cron '{cron}' "
                    f"expects runs every ~{expected_ms/60000:.0f}min.",
                    "Check whether the agent is actually running; view logs tail on the Agents page.",
                )
            else:
                self.check_passed()

        # 4) Overall staleness: if the page itself hasn't been rebuilt in >2h,
        # every timestamp on it is suspect. QA's local-fix scope regenerates
        # reports; we just need to flag the condition.
        page_age_hours = (datetime.now().timestamp() - page_path.stat().st_mtime) / 3600
        if page_age_hours > 2:
            self.add_issue(
                'warning',
                'Logs Page',
                f"logs_dashboard.html hasn't been regenerated in {page_age_hours:.1f}h — last-run timestamps may be stale.",
                "Regenerate: python3 scripts/generate_logs_dashboard.py",
                action={'type': 'regenerate_reports', 'scope': 'local'},
            )
        else:
            self.check_passed()

    def check_agent_schedules(self):
        """Validate that essential agents are actually scheduled and firing on time.

        Catches two failure modes:
          1. An agent we consider essential isn't in .claude/scheduled_tasks.json
             at all (so it only runs when someone remembers to trigger it).
          2. An agent IS scheduled but hasn't fired in more than ~3× its expected
             cadence — indicating the cron itself is stuck.
        """
        logger.info("Checking scheduled-agent coverage...")
        scheduled_tasks_path = Path(__file__).parent.parent / ".claude" / "scheduled_tasks.json"
        if not scheduled_tasks_path.exists():
            self.add_issue(
                'critical',
                'Agent Schedules',
                "scheduled_tasks.json not found — no agents are scheduled.",
                "Restore .claude/scheduled_tasks.json from backup or re-add schedules."
            )
            return

        try:
            import json as _json
            # Be lenient about trailing NULs that some writers leave in the
            # file — strict json.loads otherwise rejects with "Extra data".
            raw = scheduled_tasks_path.read_text().rstrip('\x00').rstrip()
            tasks_data = _json.loads(raw)
        except Exception as e:
            self.add_issue(
                'critical',
                'Agent Schedules',
                f"Could not parse scheduled_tasks.json: {e}",
                "Check JSON syntax in .claude/scheduled_tasks.json"
            )
            return

        # Self-heal: refresh lastFiredAt from each agent's actual log mtime so
        # the liveness check below reflects reality, not a stale Claude-Code
        # scheduler value. Cron is the source of truth; the JSON is just a
        # cache. Only writes when something actually changed and the file
        # parsed cleanly. Failures here are non-fatal.
        try:
            self._refresh_last_fired(tasks_data, scheduled_tasks_path)
        except Exception as e:
            logger.warning(f"Could not refresh lastFiredAt (non-fatal): {e}")

        tasks_by_id = {t['id']: t for t in tasks_data.get('tasks', [])}

        # Agents we consider essential for the dashboard to stay accurate.
        # If any of these aren't scheduled, it's a warning — they might be
        # intentionally on-demand, but the manager should confirm.
        essential = {
            'jira_hygiene_agent':  'Refreshes hygiene_issues every 15 min during work hours.',
            'jira_collector':      'Refreshes tickets / sprints / snapshots — backing data for Stories, Story Points, Epics, Team Members.',
            'calendar_sync':       'Refreshes 1-on-1 meeting metadata shown on Team Members page.',
            'qa_agent_auto_fix':   'This agent itself, running on a cadence.',
            'github_pr_agent':     'Refreshes GitHub PRs, reviews, and comments.',
            'db_backup':           'Nightly SQLite backup.',
        }

        # 1) Coverage: every essential agent must be scheduled.
        for agent_id, purpose in essential.items():
            if agent_id not in tasks_by_id:
                self.add_issue(
                    'warning',
                    'Agent Schedules',
                    f"Essential agent '{agent_id}' has no cron entry. {purpose}",
                    "Add a task entry for this agent in .claude/scheduled_tasks.json."
                )
            else:
                self.check_passed()

        # 2) Liveness: any scheduled task whose lastFiredAt is much older than
        # its cron cadence probably has a stuck cron. Compute expected interval
        # from the cron expression (we only handle patterns we actually use).
        # For windowed crons (`*/15 6-18 * * *`) we also skip the check when
        # we're currently outside the active window — a legitimate gap, not a bug.
        now_dt = datetime.now()
        now_ms = int(now_dt.timestamp() * 1000)
        for task_id, task in tasks_by_id.items():
            last = task.get('lastFiredAt')
            cron = task.get('cron', '')
            if not last:
                continue  # Never fired yet — flagged by coverage check, not liveness
            if _cron_is_outside_window(cron, now_dt):
                # Outside the cron's active hour window — not expected to fire.
                self.check_passed()
                continue
            expected_interval_ms = _expected_cron_interval_ms(cron)
            if expected_interval_ms is None:
                continue
            age_ms = now_ms - last
            if age_ms > expected_interval_ms * 3:
                age_hours = age_ms / 3_600_000
                self.add_issue(
                    'critical',
                    'Agent Schedules',
                    f"Cron task '{task_id}' ({cron}) hasn't fired in {age_hours:.1f}h — expected every ~{expected_interval_ms / 60_000:.0f} min.",
                    "Check the cron daemon and .claude/scheduled_tasks.json integrity."
                )
            else:
                self.check_passed()

    def _refresh_last_fired(self, tasks_data: dict, sched_path) -> None:
        """Update each task's lastFiredAt from its real log file's mtime.

        cron is the source of truth for these jobs; the JSON is just a cache
        Claude Code's old in-process scheduler used. Without this, the
        liveness check below would forever report stale data.

        Mapping rules:
          * task id → log file most directly written by that agent
          * jira_hygiene_agent intentionally points at collector.log because
            that's where the hygiene script writes (see scripts/jira_hygiene_agent.py)
        """
        import json as _json
        repo_root = Path(__file__).parent.parent
        log_for = {
            'calendar_sync':      repo_root / 'logs' / 'calendar_sync.log',
            'qa_agent_auto_fix':  repo_root / 'logs' / 'qa_agent.log',
            'github_pr_agent':    repo_root / 'logs' / 'github_pr_agent.log',
            'db_backup':          repo_root / 'logs' / 'backup_db.log',
            'jira_collector':     repo_root / 'logs' / 'jira_collector_agent.log',
            'jira_hygiene_agent': repo_root / 'logs' / 'collector.log',
        }
        changed = False
        for task in tasks_data.get('tasks', []):
            log = log_for.get(task.get('id'))
            if not log or not log.exists():
                continue
            ts_ms = int(log.stat().st_mtime * 1000)
            if task.get('lastFiredAt') != ts_ms:
                task['lastFiredAt'] = ts_ms
                changed = True
        if not changed:
            return
        # Atomic write with retry on a brief contention window.
        import time as _time
        for attempt in range(5):
            try:
                tmp = sched_path.with_suffix(sched_path.suffix + '.tmp')
                tmp.write_text(_json.dumps(tasks_data, indent=2) + '\n')
                os.replace(tmp, sched_path)
                return
            except OSError:
                if attempt == 4:
                    raise
                _time.sleep(0.2 * (attempt + 1))

    # ----- Deep (Jira-backed) checks ---------------------------------------
    # These re-verify hygiene-agent output by hitting Jira directly. They catch
    # the kinds of bugs that let false positives accumulate silently (e.g. the
    # capped-batch issue we had on "Epics without Child Stories" earlier).

    def check_hygiene_accuracy_acceptance_criteria(self):
        """For each epic flagged as missing acceptance criteria, fetch the AC
        custom field from Jira and mark the flag a false positive if it has
        meaningful content.
        """
        logger.info("Validating hygiene flags: epics_missing_acceptance_criteria (deep)...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT ticket_key FROM hygiene_issues
                WHERE issue_type = 'epics_missing_acceptance_criteria'
                ORDER BY ticket_key
            """)
            flagged_keys = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

        if not flagged_keys:
            self.check_passed()
            return

        jira = self._jira_client()
        false_positives = []
        batch_size = 50
        for i in range(0, len(flagged_keys), batch_size):
            batch = flagged_keys[i:i + batch_size]
            jql = f"key in ({','.join(batch)})"
            try:
                data = jira.search_issues(jql, ['customfield_10230', 'summary'], max_results=batch_size)
            except Exception as e:
                self.add_issue(
                    'warning',
                    'Hygiene Accuracy',
                    f'Could not validate AC batch starting at {batch[0]}: {e}',
                    'Check Jira credentials and rate limits'
                )
                continue

            for issue in data.get('issues', []):
                key = issue['key']
                ac_field = issue['fields'].get('customfield_10230')
                if self._has_valid_acceptance_criteria(ac_field):
                    false_positives.append(key)

        if false_positives:
            # One rollup issue so the list stays readable
            preview = ', '.join(false_positives[:5])
            extra = f' (+{len(false_positives) - 5} more)' if len(false_positives) > 5 else ''
            self.add_issue(
                'critical',
                'Hygiene Accuracy',
                f"{len(false_positives)} epic(s) flagged as missing AC but actually have AC content: {preview}{extra}",
                'Review acceptance-criteria detection logic in jira_hygiene_agent.py',
                action={'type': 'rerun_hygiene_agent', 'scope': 'pipelines'},
            )
        else:
            self.check_passed()

    def check_hygiene_accuracy_no_work_items(self):
        """For each epic flagged as having no child stories, query Jira directly
        and mark the flag a false positive if children exist.
        """
        logger.info("Validating hygiene flags: epics_no_work_items (deep)...")
        conn = get_connection(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT ticket_key FROM hygiene_issues
                WHERE issue_type = 'epics_no_work_items'
                ORDER BY ticket_key
            """)
            flagged_keys = [row[0] for row in cursor.fetchall()]
        finally:
            conn.close()

        if not flagged_keys:
            self.check_passed()
            return

        jira = self._jira_client()
        false_positives = []
        for epic_key in flagged_keys:
            jql = f"parent = {epic_key} AND type = Story AND status NOT IN (Abandoned, Duplicate)"
            try:
                data = jira.search_issues(jql, ['key'], max_results=5)
            except Exception as e:
                logger.warning(f"Could not verify children for {epic_key}: {e}")
                continue
            if data.get('issues'):
                false_positives.append(epic_key)

        if false_positives:
            preview = ', '.join(false_positives[:5])
            extra = f' (+{len(false_positives) - 5} more)' if len(false_positives) > 5 else ''
            self.add_issue(
                'critical',
                'Hygiene Accuracy',
                f"{len(false_positives)} epic(s) flagged as having no child stories but actually have children: {preview}{extra}",
                'Review child-story detection logic in jira_hygiene_agent.py',
                action={'type': 'rerun_hygiene_agent', 'scope': 'pipelines'},
            )
        else:
            self.check_passed()

    @staticmethod
    def _has_valid_acceptance_criteria(ac_field):
        """Return True if the AC custom field (Atlassian Document Format) has
        meaningful content: headings, lists, or a paragraph with >10 chars of text.
        """
        if not ac_field or not isinstance(ac_field, dict):
            return False
        content = ac_field.get('content', [])
        if not content or not isinstance(content, list):
            return False
        for node in content:
            if not isinstance(node, dict):
                continue
            node_type = node.get('type', '')
            if node_type in ('heading', 'bulletList', 'orderedList', 'taskList'):
                return True
            if node_type == 'paragraph':
                for text_node in (node.get('content') or []):
                    if isinstance(text_node, dict) and text_node.get('type') == 'text':
                        if len((text_node.get('text') or '').strip()) > 10:
                            return True
        return False

    # ------------------------------------------------------------------
    # State-hash helpers — cheap digests used by the Planner to skip
    # checks whose inputs haven't changed since the last passing run.
    # ------------------------------------------------------------------
    def _sh_db_counts(self, table: str, where: str = "") -> str:
        """Digest a table's row-count + max(updated_at|created_at). Cheap."""
        try:
            with get_connection(self.db_path) as conn:
                cur = conn.cursor()
                ts_col = None
                for cand in ("updated_at", "created_at", "snapshot_timestamp", "snapshot_date"):
                    try:
                        cur.execute(f"SELECT MAX({cand}) FROM {table} {where} LIMIT 1")
                        ts_col = cand
                        break
                    except Exception:
                        continue
                cur.execute(f"SELECT COUNT(*) FROM {table} {where}")
                row_count = cur.fetchone()[0]
                max_ts = ""
                if ts_col:
                    cur.execute(f"SELECT MAX({ts_col}) FROM {table} {where}")
                    r = cur.fetchone()
                    max_ts = str(r[0] or "")
        except Exception:
            return ""
        return hashlib.sha1(f"{table}|{row_count}|{max_ts}".encode()).hexdigest()[:12]

    def _sh_distribution(self, table: str, group_col: str, where: str = "") -> str:
        """Digest a (col → count) distribution. Catches single-row status
        flips that don't change row count or max(timestamp).
        """
        try:
            with get_connection(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT {group_col}, COUNT(*) FROM {table} {where} GROUP BY {group_col}"
                )
                items = sorted(f"{k}:{v}" for k, v in cur.fetchall())
        except Exception:
            return ""
        return hashlib.sha1("|".join(items).encode()).hexdigest()[:12]

    def _sh_tickets(self) -> str:
        # Row count + max(updated_at) misses single-ticket status flips; the
        # status distribution catches them.
        return (
            self._sh_db_counts("tickets") + "|"
            + self._sh_distribution("tickets", "status")
        )

    def _sh_developer_snapshots(self) -> str:
        return self._sh_db_counts("developer_snapshots")

    def _sh_sprints(self) -> str:
        return (
            self._sh_db_counts("sprints") + "|"
            + self._sh_db_counts("sprint_snapshots") + "|"
            + self._sh_distribution("sprints", "state")
        )

    def _sh_hygiene(self) -> str:
        return (
            self._sh_db_counts("hygiene_issues") + "|"
            + self._sh_distribution("hygiene_issues", "issue_type")
        )

    def _sh_html_reports(self) -> str:
        """Digest of HTML file mtimes + sizes.

        mtime alone misses regenerations that touch the file but produce
        identical bytes; size catches those without forcing us to read+hash
        every page on each QA pass.
        """
        html_dir = Path(__file__).parent.parent / "reports" / "html"
        if not html_dir.exists():
            return ""
        parts = []
        for p in sorted(html_dir.glob("*.html")):
            try:
                st = p.stat()
                parts.append(f"{p.name}:{st.st_mtime_ns}:{st.st_size}")
            except Exception:
                pass
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:12]

    def _sh_scheduled_tasks(self) -> str:
        p = Path(__file__).parent.parent / ".claude" / "scheduled_tasks.json"
        try:
            return hashlib.sha1(p.read_bytes()).hexdigest()[:12]
        except Exception:
            return ""

    def registered_checks(self):
        """Return the declarative Check objects for the agent.

        Each wraps one of this agent's `check_*` methods. The `depends_on`
        graph lets the Planner skip downstream checks when their prerequisite
        fails (root-cause-first reporting).
        """
        return [
            Check(
                key="sprint_data_quality",
                invariant="every sprint has start/end dates and no future snapshots",
                fn=self.check_sprint_data_quality,
                state_hash_fn=self._sh_sprints,
                estimated_cost_s=1.0,
                tags=("fast", "sprints"),
                # Sprint state legitimately oscillates as sprints open/close
                # and snapshots get added — that's the signal, not flakiness.
                benign_alternator=True,
            ),
            Check(
                key="ticket_integrity",
                invariant="every ticket row references an existing sprint",
                fn=self.check_ticket_integrity,
                depends_on=("sprint_data_quality",),
                state_hash_fn=self._sh_tickets,
                estimated_cost_s=1.5,
                tags=("fast", "tickets"),
            ),
            Check(
                key="story_points_consistency",
                invariant="closed + in_progress + open + excluded == total per sprint",
                fn=self.check_story_points_consistency,
                depends_on=("ticket_integrity",),
                state_hash_fn=self._sh_tickets,
                estimated_cost_s=2.0,
                tags=("fast", "sp"),
            ),
            Check(
                key="epic_consistency",
                invariant="epic rollups match their child story counts",
                fn=self.check_epic_consistency,
                depends_on=("ticket_integrity",),
                state_hash_fn=self._sh_tickets,
                estimated_cost_s=1.5,
                tags=("fast", "epics"),
            ),
            Check(
                key="developer_metrics",
                invariant="per-dev snapshots exist for everyone with sprint work",
                fn=self.check_developer_metrics,
                depends_on=("ticket_integrity",),
                state_hash_fn=self._sh_developer_snapshots,
                estimated_cost_s=1.5,
                tags=("fast", "developers"),
                # Developers join/leave sprints; snapshots fill in over hours
                # — alternating pass/fail tracks real state, not flake.
                benign_alternator=True,
            ),
            Check(
                key="temporal_consistency",
                invariant="no timestamps are in the future",
                fn=self.check_temporal_consistency,
                state_hash_fn=self._sh_tickets,
                estimated_cost_s=0.8,
                tags=("fast",),
            ),
            Check(
                key="html_reports",
                invariant="every dashboard page is present and recently regenerated",
                fn=self.check_html_reports,
                state_hash_fn=self._sh_html_reports,
                estimated_cost_s=0.5,
                tags=("fast", "ui"),
                # The whole point of this check is to catch staleness. It's
                # *expected* to alternate between pass (just regen'd) and
                # fail (cron missed a beat). Suppressing it as flake hides
                # the exact failure mode it's designed to surface.
                benign_alternator=True,
            ),
            Check(
                key="hygiene_tracking",
                invariant="the hygiene_issues table exists and is populated when expected",
                fn=self.check_hygiene_tracking,
                state_hash_fn=self._sh_hygiene,
                estimated_cost_s=0.7,
                tags=("fast", "hygiene"),
            ),
            Check(
                key="github_collection_saturation",
                invariant="github collection doesn't look stuck at the --limit ceiling",
                fn=self.check_github_collection_saturation,
                estimated_cost_s=0.4,
                tags=("fast", "github"),
            ),
            Check(
                key="agent_schedules",
                invariant="every scheduled agent fired within its expected window",
                fn=self.check_agent_schedules,
                state_hash_fn=self._sh_scheduled_tasks,
                estimated_cost_s=0.3,
                tags=("fast", "agents"),
            ),
            Check(
                key="logs_page_accuracy",
                invariant="the logs page's schedule labels match scheduled_tasks.json",
                fn=self.check_logs_page_accuracy,
                depends_on=("html_reports",),
                state_hash_fn=self._sh_html_reports,
                estimated_cost_s=0.4,
                tags=("fast", "ui"),
                # Edits to scheduled_tasks.json take a regen cycle to reflect
                # on the logs page — alternation is normal, not flake.
                benign_alternator=True,
            ),
            Check(
                key="hygiene_accuracy_acceptance_criteria",
                invariant="hygiene AC flags agree with the live Jira ticket body",
                fn=self.check_hygiene_accuracy_acceptance_criteria,
                depends_on=("hygiene_tracking",),
                estimated_cost_s=25.0,  # API-backed; only runs in --deep
                tags=("deep", "hygiene"),
            ),
            Check(
                key="hygiene_accuracy_no_work_items",
                invariant="hygiene no-child-work flags agree with Jira child counts",
                fn=self.check_hygiene_accuracy_no_work_items,
                depends_on=("hygiene_tracking",),
                estimated_cost_s=25.0,
                tags=("deep", "hygiene"),
            ),
        ]

    def run_all_checks(self, history=None, planner_opts=None, run_id=None):
        """Execute the agent's checks through the Planner.

        Returns a list of (Check, decision, CheckResult) tuples used to build
        the RunReport.
        """
        from utils.qa_agent_core import CheckResult  # local to avoid circular

        planner_opts = planner_opts or {}
        logger.info("=" * 60)
        logger.info(f"QA Agent - {datetime.now()}")
        logger.info("=" * 60)

        checks = self.registered_checks()
        if not self.deep:
            # Filter deep-tagged checks out of the plan (they need --deep to be useful)
            checks = [c for c in checks if "deep" not in c.tags]
        self._checks_by_key = {c.key: c for c in checks}

        planner = Planner(
            checks=checks,
            history=history or HistoryStore(),
            budget_s=planner_opts.get("budget_s"),
            force_all=planner_opts.get("force_all", False),
            include_flaky=planner_opts.get("include_flaky", False),
        )

        run_id = run_id or make_run_id()
        failed_keys: set = set()
        results: list = []
        t0_total = time.monotonic()

        # Incremental planning: after each run we re-ask the planner so
        # upstream failures cascade to downstream skips.
        processed: set = set()
        while True:
            plan = planner.plan(failed_keys=failed_keys)
            next_decision = None
            for decision in plan:
                if decision.check.key in processed:
                    continue
                next_decision = decision
                break
            if next_decision is None:
                break
            check = next_decision.check
            processed.add(check.key)

            if next_decision.decision != "run":
                logger.info("  ⊘ skip %s — %s", check.key, next_decision.reason)
                result = CheckResult(
                    key=check.key,
                    started_at=datetime.utcnow().isoformat() + "Z",
                    duration_s=0.0,
                    status="skipped" if next_decision.decision != "defer_budget" else "deferred",
                    issues_count=0,
                    reason=next_decision.reason,
                )
                results.append((check, next_decision, result))
                if history:
                    history.record_check_run(
                        check_key=check.key,
                        run_id=run_id,
                        started_at=result.started_at,
                        status=result.status,
                        issues_count=0,
                        state_hash=next_decision.state_hash,
                    )
                continue

            logger.info("Running check: %s", check.key)
            self._active_check_key = check.key
            self._check_pass_counted = False
            before_count = len(self.issues)
            t0 = time.monotonic()
            status = "passed"
            try:
                check.fn()
            except Exception as e:
                logger.error("Check %s raised: %s", check.key, e, exc_info=True)
                status = "error"
            duration = time.monotonic() - t0
            self._active_check_key = ""
            new_issues = [i for i in self.issues[before_count:]
                          if i.get('check_key') == check.key]
            if status == "passed" and new_issues:
                status = "failed"
                failed_keys.add(check.key)
            elif status == "passed":
                # Make sure the pass was counted (legacy checks call check_passed
                # only when they explicitly pass; don't double-count)
                if not self._check_pass_counted:
                    self.check_passed()

            result = CheckResult(
                key=check.key,
                started_at=datetime.utcnow().isoformat() + "Z",
                duration_s=duration,
                status=status,
                issues_count=len(new_issues),
                reason="",
            )
            results.append((check, next_decision, result))
            if history:
                history.record_check_run(
                    check_key=check.key,
                    run_id=run_id,
                    started_at=result.started_at,
                    status=result.status,
                    issues_count=result.issues_count,
                    state_hash=next_decision.state_hash,
                    duration_s=duration,
                )
                # Memory: record each issue seen; auto-resolve ones that
                # disappeared since last run.
                seen_keys = set()
                for issue in new_issues:
                    seen_keys.add(
                        history.upsert_issue(
                            check_key=check.key,
                            severity=issue['severity'],
                            message=issue['message'],
                        )
                    )
                if status == "passed":
                    # Every previously-open issue for this check is now resolved
                    for k in history.open_issue_keys_for_check(check.key):
                        history.mark_resolved(k)
                else:
                    # Resolve any open-in-history but not-seen-this-run issues
                    for k in history.open_issue_keys_for_check(check.key):
                        if k not in seen_keys:
                            history.mark_resolved(k)

        total_duration = time.monotonic() - t0_total

        # ---- Summary ----
        total_checks = self.stats['checks_passed'] + self.stats['checks_failed']
        logger.info("")
        logger.info("=" * 60)
        logger.info("QA SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Total Checks: {total_checks}  (ran {len(results)} this pass in {total_duration:.1f}s)")
        logger.info(f"✅ Passed: {self.stats['checks_passed']}")
        logger.info(f"❌ Failed: {self.stats['checks_failed']}")
        logger.info("")
        logger.info(f"🚨 Critical Issues: {self.stats['critical']}")
        logger.info(f"⚠️  Warnings: {self.stats['warning']}")
        logger.info(f"ℹ️  Info: {self.stats['info']}")

        if self.issues:
            logger.info("")
            logger.info("ISSUES FOUND:")
            logger.info("-" * 60)
            for issue in sorted(self.issues, key=lambda x: {'critical': 0, 'warning': 1, 'info': 2}[x['severity']]):
                severity_icon = {'critical': '🚨', 'warning': '⚠️', 'info': 'ℹ️'}[issue['severity']]
                age_note = ""
                if history and issue.get('check_key'):
                    ik = HistoryStore.issue_key(issue['check_key'], issue['message'])
                    age = history.issue_age_days(ik)
                    if age is not None and age >= 3:
                        age_note = f" (persistent · age {age}d)"
                logger.info(f"{severity_icon} [{issue['category']}] {issue['message']}{age_note}")
                if issue['fix']:
                    logger.info(f"   Fix: {issue['fix']}")
                logger.info("")
        else:
            logger.info("")
            logger.info("✅ All checks passed! No issues found.")

        logger.info("=" * 60)
        logger.info("QA check complete")
        logger.info("=" * 60)

        return results


class FixEngine:
    """Applies automated remediations for issues the QA agent finds.

    Fixes are grouped into three opt-in scopes:
      - local: only touches our own SQLite + generated HTML. Safe to run often.
      - pipelines: re-runs collector/report scripts. Slower; mutates our data.
      - jira: posts comments on Jira tickets. Visible to others; deduped.
    """

    PROJECT_ROOT = Path(__file__).parent.parent
    SCRIPTS = PROJECT_ROOT / "scripts"

    def __init__(self, config, scopes, dry_run=False,
                 history=None, verifier=None, tools=None, proposals=None,
                 run_id=""):
        self.config = config
        self.db_path = config['database']['path']
        self.scopes = set(scopes)
        self.dry_run = dry_run
        self.applied = []
        self.skipped = []
        self.errors = []
        # Agent-runtime extras. All optional — FixEngine still works without
        # them (callers that use the old API get the original behavior, just
        # with no history/verify/HITL).
        self.history = history
        self.verifier = verifier
        self.tools = tools
        self.proposals = proposals
        self.run_id = run_id
        # Structured record of what we did, for the RunReport.
        self.actions_record = []
        # Ensure a single rerun per script per session — many issues share a fix
        self._scripts_run = set()
        self._reports_regenerated = False
        # Threshold: stop retrying an action that keeps reverting.
        self.MAX_RECENT_REVERTS = 2

    def apply(self, issues):
        """Apply remediations for any issue with an action whose scope is enabled.

        For each issue+action:
          1. Skip if scope isn't enabled.
          2. If `propose` scope is declared, write a proposal instead of acting.
          3. Check history — if this exact action keeps reverting, stop trying.
          4. Record the attempt, run the handler, then verify by re-running
             the originating check. Persist the outcome.
        """
        # First: execute any proposals that were approved in a previous run.
        self._drain_approved_proposals()

        # Dedupe actions (e.g. 10 issues all asking for a jira_collector rerun)
        seen_dedupe_keys = set()
        for issue in issues:
            action = issue.get('action')
            if not action:
                continue
            scope = action.get('scope')
            if scope == 'propose' and self.proposals:
                self._route_to_proposals(action, issue)
                continue
            if scope not in self.scopes:
                self.skipped.append((issue['message'], f"scope={scope} not enabled"))
                continue
            handler_name = f"_do_{action['type']}"
            handler = getattr(self, handler_name, None)
            if not handler:
                self.errors.append((issue['message'], f"unknown action type '{action['type']}'"))
                continue
            dedupe_key = (action['type'], tuple(sorted((k, v) for k, v in action.items() if k != 'type')))
            if dedupe_key in seen_dedupe_keys:
                continue
            seen_dedupe_keys.add(dedupe_key)

            # Memory: back off if this action has kept reverting.
            if self.history:
                reverts = self.history.recent_reverts_for_action(action)
                if reverts >= self.MAX_RECENT_REVERTS:
                    msg = (f"action '{action['type']}' reverted {reverts}x recently — "
                           f"escalating root-cause instead of retrying")
                    self.skipped.append((issue['message'], msg))
                    self.actions_record.append({
                        "action": action,
                        "issue_message": issue['message'],
                        "outcome": "escalated",
                        "detail": msg,
                    })
                    continue

            issue_key = ""
            action_hash = ""
            if self.history and issue.get('check_key'):
                issue_key = HistoryStore.issue_key(issue['check_key'], issue['message'])
                action_hash = self.history.record_fix_attempt(action, issue_key)

            try:
                handler(action, issue)
            except Exception as e:
                self.errors.append((issue['message'], str(e)))
                logger.error(f"Fix failed for '{issue['message']}': {e}")
                if self.history and action_hash:
                    self.history.record_fix_outcome(action_hash, issue_key, "failed", str(e))
                self.actions_record.append({
                    "action": action,
                    "issue_message": issue['message'],
                    "outcome": "failed",
                    "detail": str(e),
                })
                continue

            # Verify: re-run the originating check. If the issue is gone,
            # mark verified; if it persisted, mark reverted (so the planner
            # can back off next time).
            verify_outcome = "skipped"
            verify_detail = ""
            if self.verifier and issue.get('check_key'):
                ck = issue['check_key']
                try:
                    res = self.verifier.verify(
                        check_key=ck,
                        reset_issues=lambda: self._reset_issues_for(ck),
                        count_issues_for_key=self._count_issues_for,
                    )
                    verify_outcome, verify_detail = res.outcome, res.detail
                except Exception as e:
                    verify_outcome = "failed"
                    verify_detail = f"verifier raised: {e}"
            if self.history and action_hash:
                # regressed counts as reverted for the back-off heuristic
                outcome_for_history = {
                    "verified": "verified",
                    "regressed": "reverted",
                    "failed": "failed",
                    "skipped": "pending",
                }.get(verify_outcome, verify_outcome)
                self.history.record_fix_outcome(action_hash, issue_key,
                                                 outcome_for_history, verify_detail)

            self.actions_record.append({
                "action": action,
                "issue_message": issue['message'],
                "outcome": verify_outcome,
                "detail": verify_detail,
            })

        # Jira-scope: post hygiene nudge comments (driven by hygiene_issues table,
        # not by QA issue rows, since qa_agent only tracks counts)
        if 'jira' in self.scopes:
            self._post_hygiene_nudges()

    # ----- helpers for verify-after-fix ------------------------------------
    _agent_ref = None  # set by FixEngine.attach_agent

    def attach_agent(self, agent):
        """Give the FixEngine access to the live agent so verification can
        reset + re-run specific checks."""
        self._agent_ref = agent

    def _reset_issues_for(self, check_key):
        if self._agent_ref:
            self._agent_ref.reset_issues_for_check(check_key)

    def _count_issues_for(self, check_key):
        if not self._agent_ref:
            return 0
        return len(self._agent_ref.issues_for_check(check_key))

    def _route_to_proposals(self, action, issue):
        confidence = float(action.get("confidence", 0.5))
        rationale = action.get("rationale", "")
        fp = self.proposals.propose(action, issue, confidence=confidence, rationale=rationale)
        if fp is None:
            self.skipped.append((issue['message'], "previously rejected — suppressed"))
        else:
            self.applied.append(f"proposed: {action['type']} ({fp}) — awaiting approval")
            self.actions_record.append({
                "action": action,
                "issue_message": issue['message'],
                "outcome": "proposed",
                "fingerprint": fp,
            })

    def _drain_approved_proposals(self):
        """Execute any proposals that were approved since last run."""
        if not self.proposals:
            return
        for rec in self.proposals.pop_approved():
            action = rec.get("action", {})
            fp = rec["fingerprint"]
            if action.get("scope") not in self.scopes and action.get("scope") != "propose":
                # Still need the scope enabled to actually execute approved actions
                self.skipped.append((f"approved proposal {fp}",
                                     f"scope={action.get('scope')} not enabled in this run"))
                continue
            handler = getattr(self, f"_do_{action.get('type')}", None)
            if not handler:
                self.errors.append((f"approved proposal {fp}",
                                    f"unknown action type '{action.get('type')}'"))
                continue
            try:
                handler(action, {"message": rec.get("issue", {}).get("message", ""),
                                 "check_key": rec.get("issue", {}).get("check_key", "")})
                self.applied.append(f"executed approved proposal {fp}")
                self.proposals.mark_executed(fp, outcome="applied")
            except Exception as e:
                self.errors.append((f"approved proposal {fp}", str(e)))
                self.proposals.mark_executed(fp, outcome="failed", detail=str(e))

    # ----- local scope ------------------------------------------------------
    def _do_delete_orphan_ticket(self, action, issue):
        ticket_key = action['ticket_key']
        if self.dry_run:
            self.applied.append(f"[dry-run] DELETE orphan ticket {ticket_key}")
            return
        with self._db() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM tickets WHERE ticket_key = ?", (ticket_key,))
            deleted = cur.rowcount
            conn.commit()
        self.applied.append(f"deleted {deleted} orphan ticket row(s) for {ticket_key}")

    def _do_delete_future_snapshot(self, action, issue):
        snapshot_date = action['snapshot_date']
        if self.dry_run:
            self.applied.append(f"[dry-run] DELETE future-dated snapshot {snapshot_date}")
            return
        with self._db() as conn:
            cur = conn.cursor()
            cur.execute(
                "DELETE FROM sprint_snapshots WHERE snapshot_date = ?",
                (snapshot_date,),
            )
            deleted = cur.rowcount
            conn.commit()
        self.applied.append(f"deleted {deleted} future-dated snapshot row(s) for {snapshot_date}")

    def _do_regenerate_reports(self, action, issue):
        if self._reports_regenerated:
            return
        self._run_script("generate_html_report.py", label="regenerate HTML reports")
        self._reports_regenerated = True

    # ----- pipelines scope --------------------------------------------------
    def _do_rerun_jira_collector(self, action, issue):
        if "jira_collector_agent.py" not in self._scripts_run:
            self._run_script("jira_collector_agent.py", label="refresh Jira data")
            self._scripts_run.add("jira_collector_agent.py")
        # Reports are now stale — queue a regen
        self._do_regenerate_reports(action, issue)

    def _do_rerun_hygiene_agent(self, action, issue):
        if "jira_hygiene_agent.py" not in self._scripts_run:
            self._run_script("jira_hygiene_agent.py", label="refresh hygiene data")
            self._scripts_run.add("jira_hygiene_agent.py")

    # ----- jira scope -------------------------------------------------------
    def _post_hygiene_nudges(self):
        """Post a comment on each unique Jira ticket with an outstanding hygiene issue.
        Dedupes via a sentinel tag in the comment body so reruns don't spam the ticket.
        """
        try:
            from collectors.jira_api_collector import JiraAPICollector
        except Exception as e:
            self.errors.append(("jira-nudges", f"cannot import JiraAPICollector: {e}"))
            return

        jira = JiraAPICollector(self.config)
        with self._db() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ticket_key, GROUP_CONCAT(issue_type, '|') AS issue_types,
                       MAX(assignee_display_name) AS assignee
                FROM hygiene_issues
                GROUP BY ticket_key
                """
            )
            rows = cur.fetchall()

        if not rows:
            return

        sentinel = "<!-- em-dashboard-qa-nudge -->"
        posted = 0
        already = 0
        failed = 0
        for ticket_key, issue_types_csv, assignee in rows:
            issue_types = issue_types_csv.split('|') if issue_types_csv else []
            readable = self._humanize_hygiene_types(issue_types)
            if not readable:
                continue
            body = (
                f"{sentinel}\n"
                f"Hygiene check flagged the following on this ticket: "
                f"{'; '.join(readable)}. "
                f"Please update when you get a chance — "
                f"[EM Dashboard]."
            )
            if self.dry_run:
                self.applied.append(f"[dry-run] would comment on {ticket_key}: {'; '.join(readable)}")
                continue
            try:
                if self._jira_has_nudge(jira, ticket_key, sentinel):
                    already += 1
                    continue
                self._jira_post_comment(jira, ticket_key, body)
                posted += 1
            except Exception as e:
                failed += 1
                logger.warning(f"Could not comment on {ticket_key}: {e}")

        summary = f"hygiene nudges — posted: {posted}, already present: {already}, failed: {failed}"
        self.applied.append(summary)

    @staticmethod
    def _humanize_hygiene_types(issue_types):
        mapping = {
            'features_missing_requirements': 'missing requirements',
            'features_missing_designs': 'missing designs',
            'features_missing_launch_phase': 'missing launch phase',
            'features_missing_milestone': 'missing proposed milestone',
            'epics_no_parent': 'missing parent',
            'epics_no_description': 'missing description',
            'epics_no_prefix': 'missing [BE]/[FE] prefix',
            'epics_no_designs': 'missing Figma link',
            'epics_no_work_items': 'no child stories',
            'epics_missing_acceptance_criteria': 'missing acceptance criteria',
            'epics_no_sprint': 'not assigned to a sprint',
            'epics_in_progress_no_assignee': 'in progress with no assignee',
            'stories_no_parent': 'missing parent epic',
            'stories_no_points': 'missing story points',
            'stories_no_description': 'missing description',
            'code_review_24h': 'in code review > 24h',
        }
        return [mapping[t] for t in issue_types if t in mapping]

    def _jira_has_nudge(self, jira, ticket_key, sentinel):
        resp = jira.session.get(f"{jira.base_url}/issue/{ticket_key}/comment", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for c in data.get('comments', []):
            body = c.get('body', '')
            # body may be ADF (dict) or string depending on API version
            if isinstance(body, dict):
                body = str(body)
            if sentinel in body:
                return True
        return False

    def _jira_post_comment(self, jira, ticket_key, body_text):
        # Jira v3 requires Atlassian Document Format. Wrap the text in a minimal ADF doc.
        payload = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": body_text}]}
                ],
            }
        }
        resp = jira.session.post(f"{jira.base_url}/issue/{ticket_key}/comment", json=payload, timeout=30)
        resp.raise_for_status()

    # ----- helpers ----------------------------------------------------------
    # Per-script subprocess budgets. Anything beyond this gets killed and
    # logged as a failure rather than blocking the QA loop. Picked to be ~3×
    # each script's normal runtime so transient slowness doesn't trigger,
    # but a hung process can't keep QA's overall budget pinned.
    _SCRIPT_TIMEOUTS_S: dict = {
        "generate_html_report.py": 90,
        "jira_collector_agent.py": 240,
        "jira_hygiene_agent.py":   240,
    }
    _DEFAULT_SCRIPT_TIMEOUT_S = 180

    def _run_script(self, script_name, label=None):
        script_path = self.SCRIPTS / script_name
        label = label or script_name
        if self.dry_run:
            self.applied.append(f"[dry-run] run {script_name}")
            return
        timeout_s = self._SCRIPT_TIMEOUTS_S.get(script_name, self._DEFAULT_SCRIPT_TIMEOUT_S)
        logger.info(f"→ {label} (running {script_name}, timeout {timeout_s}s)")
        try:
            result = subprocess.run(
                [sys.executable, str(script_path)],
                cwd=self.PROJECT_ROOT,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            # Don't propagate — record + continue so other QA fixes still run.
            self.errors.append((script_name, f"timeout after {timeout_s}s"))
            logger.warning(f"❌ {script_name} timed out after {timeout_s}s — continuing")
            return
        except Exception as e:  # OSError, etc.
            self.errors.append((script_name, f"could not launch: {e}"))
            logger.warning(f"❌ failed to launch {script_name}: {e}")
            return

        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip()[-500:]
            self.errors.append((script_name, f"exit {result.returncode}: {tail}"))
            logger.warning(f"❌ {script_name} exited {result.returncode}: {tail}")
            return
        self.applied.append(f"ran {script_name}")

    def _db(self):
        conn = get_connection(self.db_path)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def report(self):
        logger.info("")
        logger.info("=" * 60)
        logger.info("FIX SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Applied: {len(self.applied)}")
        for line in self.applied:
            logger.info(f"  ✓ {line}")
        if self.skipped:
            logger.info(f"Skipped: {len(self.skipped)} (scope not enabled)")
        if self.errors:
            logger.info(f"Errors: {len(self.errors)}")
            for msg, err in self.errors:
                logger.info(f"  ✗ {msg} — {err}")


def _build_tool_catalog(engine):
    """Register the built-in fix tools. Each points at an existing
    FixEngine handler so the catalog is purely descriptive for now."""
    tools = ToolCatalog()
    tools.register(ToolSpec(
        name="delete_orphan_ticket",
        description="Delete a ticket row whose sprint_id no longer resolves.",
        handler=lambda a: engine._do_delete_orphan_ticket(a, {"message": ""}),
        cost_estimate_s=0.2,
        idempotent=True,
        args_schema={"ticket_key": (True, "The ticket key to delete")},
    ))
    tools.register(ToolSpec(
        name="delete_future_snapshot",
        description="Delete a sprint_snapshots row dated after today.",
        handler=lambda a: engine._do_delete_future_snapshot(a, {"message": ""}),
        cost_estimate_s=0.2,
        idempotent=True,
        args_schema={"snapshot_date": (True, "ISO date of the snapshot to drop")},
    ))
    tools.register(ToolSpec(
        name="regenerate_reports",
        description="Re-run scripts/generate_html_report.py to refresh the HTML.",
        handler=lambda a: engine._do_regenerate_reports(a, {"message": ""}),
        cost_estimate_s=15.0,
        idempotent=True,
    ))
    tools.register(ToolSpec(
        name="rerun_jira_collector",
        description="Re-run scripts/jira_collector_agent.py to refresh Jira data.",
        handler=lambda a: engine._do_rerun_jira_collector(a, {"message": ""}),
        cost_estimate_s=45.0,
        idempotent=True,
    ))
    tools.register(ToolSpec(
        name="rerun_hygiene_agent",
        description="Re-run scripts/jira_hygiene_agent.py to refresh hygiene data.",
        handler=lambda a: engine._do_rerun_hygiene_agent(a, {"message": ""}),
        cost_estimate_s=60.0,
        idempotent=True,
    ))
    return tools


def _parse_args():
    p = argparse.ArgumentParser(description="QA Agent — check dashboard data quality and auto-fix.")
    p.add_argument('--deep', action='store_true',
                   help='Enable API-backed hygiene accuracy checks (slower, ~30-60s).')
    # Fix behavior — default is local + pipelines. Jira writes stay opt-in so
    # nothing becomes visible to the rest of the team without explicit consent.
    p.add_argument('--no-fix', action='store_true',
                   help='Report only; disable all auto-fixes.')
    p.add_argument('--fix-jira', action='store_true',
                   help='Additionally post hygiene nudge comments on Jira tickets.')
    p.add_argument('--no-fix-pipelines', action='store_true',
                   help='Skip pipeline-rerun fixes (keeps only local repairs).')
    p.add_argument('--no-fix-local', action='store_true',
                   help='Skip local repairs (unusual — generally keep these on).')
    p.add_argument('--dry-run', action='store_true',
                   help='Show what fixes would run without executing them.')

    # Agent-runtime controls (new)
    p.add_argument('--budget-seconds', type=float, default=None,
                   help='Per-run time budget. Checks that would exceed it are deferred.')
    p.add_argument('--force-all', action='store_true',
                   help='Ignore state-hash gating; run every check this pass.')
    p.add_argument('--include-flaky', action='store_true',
                   help='Run checks that the flake detector has been suppressing.')
    p.add_argument('--list-proposals', action='store_true',
                   help='Print pending HITL proposals and exit.')
    p.add_argument('--approve', metavar='FINGERPRINT',
                   help='Approve a pending proposal (it will run on the next session).')
    p.add_argument('--reject', metavar='FINGERPRINT',
                   help='Reject a pending proposal. Suppressed for 90 days.')
    p.add_argument('--reject-reason', default='',
                   help='Optional reason attached to --reject.')
    p.add_argument('--list-tools', action='store_true',
                   help='Print the tool catalog and exit.')

    return p.parse_args()


def _cmd_list_proposals(history, proposals):
    pending = proposals.list_pending()
    if not pending:
        print("No pending proposals.")
        return 0
    print(f"{len(pending)} pending proposal(s):\n")
    for p in pending:
        act = p.get("action", {})
        print(f"  [{p['fingerprint']}]  {act.get('type')}  conf={p.get('confidence', 0):.2f}")
        print(f"      issue: {p.get('issue', {}).get('message', '')}")
        if p.get("rationale"):
            print(f"      rationale: {p['rationale']}")
        print(f"      proposed_at: {p.get('proposed_at')}")
        print()
    print("Approve:  python3 scripts/qa_agent.py --approve <fingerprint>")
    print("Reject:   python3 scripts/qa_agent.py --reject  <fingerprint> [--reject-reason TEXT]")
    return 0


def _cmd_list_tools(tools):
    for spec in tools.describe_all():
        print(f"\n{spec['name']}  (cost ~{spec['cost_estimate_s']}s, idempotent={spec['idempotent']})")
        print(f"  {spec['description']}")
        if spec['args']:
            for arg, meta in spec['args'].items():
                req = "required" if meta['required'] else "optional"
                print(f"    - {arg} ({req}): {meta['description']}")
    return 0


def _acquire_qa_lock():
    """Exclusive flock on data/.locks/qa_agent.lock so two QA runs can't overlap.

    QA runs every 5 min via cron, but its FixEngine subprocess calls
    (jira_collector_agent.py, generate_html_report.py) can take longer than
    that on a slow run. Without this guard, a second QA instance starts
    before the first finishes, and they trip over each other writing to
    the same DB rows.

    Returns the open file handle (caller keeps it alive); None if held.
    """
    import fcntl
    lock_dir = Path(__file__).parent.parent / 'data' / '.locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / 'qa_agent.lock'
    fh = open(lock_path, 'w')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main():
    """Run the QA agent."""
    args = _parse_args()
    scopes = set()
    if not args.no_fix:
        if not args.no_fix_local:
            scopes.add('local')
        if not args.no_fix_pipelines:
            scopes.add('pipelines')
        if args.fix_jira:
            scopes.add('jira')

    # Subcommands that don't talk to the DB (approve/reject/list-proposals)
    # are safe to run alongside another instance — only acquire the lock for
    # an actual check pass. Subcommand short-circuits below run before this
    # block via early `return` if/when we detect them, but they share the
    # same `try: config = load_config()` initializer; the lock here applies
    # to the full main() body.
    lock = _acquire_qa_lock()
    if lock is None:
        # Already running — exit cleanly without raising. Cron will fire us
        # again in 5 min and the previous run will have finished by then.
        logger.warning("Another qa_agent instance is running — exiting.")
        return

    try:
        config = load_config()

        # Agent-runtime infrastructure
        history = HistoryStore()
        proposals = ProposalQueue(history)

        # Subcommands that short-circuit a normal run
        if args.approve:
            proposals.resolve(args.approve, "approved")
            print(f"approved {args.approve}")
            return
        if args.reject:
            proposals.resolve(args.reject, "rejected", reason=args.reject_reason)
            print(f"rejected {args.reject}")
            return
        if args.list_proposals:
            _cmd_list_proposals(history, proposals)
            return

        agent = QAAgent(config, deep=args.deep)

        run_id = make_run_id()
        run_started = datetime.utcnow().isoformat() + "Z"
        t0 = time.monotonic()

        planner_opts = {
            "budget_s": args.budget_seconds,
            "force_all": args.force_all,
            "include_flaky": args.include_flaky,
        }
        check_results = agent.run_all_checks(history=history,
                                              planner_opts=planner_opts,
                                              run_id=run_id)

        engine = None
        if scopes:
            engine = FixEngine(
                config, scopes=scopes, dry_run=args.dry_run,
                history=history, proposals=proposals,
                verifier=Verifier(check_by_key={c.key: c for c in agent.registered_checks()}),
                run_id=run_id,
            )
            engine.attach_agent(agent)
            engine.tools = _build_tool_catalog(engine)
            if args.list_tools:
                _cmd_list_tools(engine.tools)
                return
            logger.info("")
            logger.info(f"Applying fixes (scopes: {', '.join(sorted(scopes))}"
                        f"{' · dry-run' if args.dry_run else ''})")
            engine.apply(agent.issues)
            engine.report()
        else:
            if args.list_tools:
                dummy = FixEngine(config, scopes=set(), dry_run=True)
                _cmd_list_tools(_build_tool_catalog(dummy))
                return
            logger.info("")
            logger.info("Auto-fix disabled (--no-fix or all scopes skipped). Report only.")

        # Persist run history
        report = RunReport(
            run_id=run_id,
            started_at=run_started,
            finished_at=datetime.utcnow().isoformat() + "Z",
            duration_s=time.monotonic() - t0,
            git_sha=_git_sha_safe(),
            checks=[{
                "key": r[0].key,
                "invariant": r[0].invariant,
                "decision": r[1].decision,
                "decision_reason": r[1].reason,
                "status": r[2].status,
                "issues_count": r[2].issues_count,
                "duration_s": r[2].duration_s,
                "state_hash": r[1].state_hash,
                "started_at": r[2].started_at,
            } for r in check_results],
            fixes=(engine.actions_record if engine else []),
            summary={
                "total_checks": agent.stats['checks_passed'] + agent.stats['checks_failed'],
                "passed": agent.stats['checks_passed'],
                "failed": agent.stats['checks_failed'],
                "critical": agent.stats['critical'],
                "warning": agent.stats['warning'],
                "info": agent.stats['info'],
            },
            budget={
                "budget_s": args.budget_seconds,
                "used_s": round(time.monotonic() - t0, 2),
                "force_all": args.force_all,
                "include_flaky": args.include_flaky,
            },
            proposals=proposals.list_pending(),
        )
        report.write()
        logger.info("")
        logger.info(f"Run report written → data/qa_runs.jsonl  (run_id={run_id})")

    except Exception as e:
        logger.error(f"QA agent failed: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Closing the file handle releases the flock.
        try:
            lock.close()
        except Exception:
            pass


def _git_sha_safe() -> str:
    try:
        from utils.qa_agent_core import _git_sha
        return _git_sha()
    except Exception:
        return ""


if __name__ == "__main__":
    main()
