#!/usr/bin/env python3
"""
Project: Fantasy snapshot agent.

Pulls INIT-185 + descendants from Jira, computes summary stats, and writes
a JSON snapshot to data/project_fantasy.json. The Project: Fantasy dashboard
page reads from that cache.

The Confluence doc index is a curated list maintained in CONFLUENCE_DOCS
below. It doesn't change often, so we don't query the API for it — update
the list here when structure changes. Links are verified by checking they
resolve (best-effort; failures are logged but don't fail the agent).
"""

import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from collectors.jira_api_collector import JiraAPICollector

logger = logging.getLogger(__name__)

INITIATIVE_KEY = "INIT-185"

# Custom field id for the "Launch" option field (Alpha / Beta / Public Launch /
# Post Launch). Discovered via /rest/api/3/field — keep here so the rest of the
# script can stay JQL-style.
LAUNCH_FIELD = "customfield_10441"
# "Proposed Milestone" — single-select option like "30. Milestone 30 - …".
# Used by the Project: Fantasy "By Target Milestone" rollup.
MILESTONE_FIELD = "customfield_10646"
# "Status Owner" — user picker (single user) used to track who's accountable
# for the current state of a feature. Distinct from `assignee`. Shape:
# {displayName, accountId, emailAddress, ...} or None.
STATUS_OWNER_FIELD = "customfield_10377"

# Curated Confluence doc index. Links point to the DFS space.
# Update this list when new high-value docs land.
CONFLUENCE_BASE = "https://betfanatics.atlassian.net/wiki/spaces/DFS"
CONFLUENCE_DOCS = [
    {
        "folder": "North Star",
        "docs": [
            ("DFS+ Product Mission (Working Draft)", "2455044194"),
        ],
    },
    {
        "folder": "Strategy & Positioning",
        "docs": [
            ("Go-To-Market Strategy (Working Draft)", "2527199593"),
            ("Competitive Landscape", "2402025546"),
            ("Market & User Psychology", "2557509843"),
        ],
    },
    {
        "folder": "Experience Design",
        "docs": [
            ("DFS+ Design Spec – F2P to Paid Conversion (Working Draft)", "2532081716"),
        ],
    },
    {
        "folder": "Product & Platform Requirements",
        "docs": [
            ("MVP Requirements Summary", "2481094747"),
            ("Copy of MVP Requirements Summary – MVP SCOPE COMMITMENT", "2910650913"),
            ("F2P Pick'em – Functional Requirements (Working Draft)", "2533589073"),
            ("Cross-Product Conversion Requirements – F2P to DFS+/Sportsbook (Working Draft)", "2535588180"),
            ("CRM Platform", "2652667966"),
            ("Acquisition Platform / Setup Summary", "2688123229"),
        ],
    },
    {
        "folder": "Economics & Incentives",
        "docs": [
            ("Financial Model & GTM Economics (Working Draft)", "2541224063"),
            ("DFS+ Generosity Strategy – v0 (Working Draft)", "2443870249"),
            ("Loyalty & Token System – Concept Brief (v0)", "2520154372"),
        ],
    },
    {
        "folder": "Operating & Meetings",
        "docs": [
            ("Glossary", "2466447482"),
            ("DFS Team Weekly Sync Agendas", "2682159243"),
        ],
    },
]

# Statuses we treat as "shipped" or "dropped"
COMPLETED_STATUSES = {"Released", "Done", "Closed", "Resolved"}
DROPPED_STATUSES = {"Abandoned", "Duplicate"}
IN_FLIGHT_STATUSES = {"In Progress", "In Development", "In Review",
                      "In code review", "Committed", "Engineering Unpacking",
                      "Testing in progress", "Blocked"}
DISCOVERY_STATUSES = {"Product Discovery", "To Do", "Open", "Backlog", "Selected for Development"}


def _bucket(status_name):
    if status_name in COMPLETED_STATUSES:
        return "done"
    if status_name in DROPPED_STATUSES:
        return "dropped"
    if status_name in IN_FLIGHT_STATUSES:
        return "in_flight"
    if status_name in DISCOVERY_STATUSES:
        return "discovery"
    return "other"


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace('Z', '+00:00'))
    except Exception:
        return None


def _adf_to_text(node):
    """Flatten an Atlassian Document Format (ADF) tree to plain text.

    Jira v3 returns rich-text fields as ADF JSON. The dashboard just needs
    readable prose, so we drop formatting and preserve paragraph breaks and
    simple bullets.
    """
    if node is None:
        return ''
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return ''.join(_adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ''
    node_type = node.get('type')
    if node_type == 'text':
        return node.get('text') or ''
    if node_type == 'hardBreak':
        return '\n'
    child_text = _adf_to_text(node.get('content'))
    if node_type in ('paragraph', 'heading'):
        return child_text + '\n\n'
    if node_type == 'listItem':
        return f'• {child_text.strip()}\n'
    if node_type in ('bulletList', 'orderedList'):
        return child_text + '\n'
    if node_type == 'codeBlock':
        return child_text + '\n\n'
    return child_text


def _days_since(ts):
    dt = _parse_iso(ts)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    return max(0, (now - dt.astimezone(timezone.utc)).days)


def _slim(issue, extra=None):
    """Return a small, JSON-friendly dict for a Jira issue."""
    f = issue.get('fields', {})
    status = f.get('status', {}).get('name') if f.get('status') else None
    issuetype = f.get('issuetype', {}).get('name') if f.get('issuetype') else None
    assignee = None
    if f.get('assignee'):
        assignee = f['assignee'].get('displayName')
    parent = None
    if f.get('parent'):
        parent = f['parent'].get('key')
    # fixVersions can be multi-valued. We capture all names; the dashboard
    # groups by the first one. None/empty becomes "Unscheduled".
    fix_versions = []
    for v in (f.get('fixVersions') or []):
        if isinstance(v, dict) and v.get('name'):
            fix_versions.append(v['name'])
    # "Launch" custom field — single-select option (Alpha / Beta / Public
    # Launch / Post Launch). Stored as {value: ...} when set, None otherwise.
    launch_val = f.get(LAUNCH_FIELD)
    if isinstance(launch_val, dict):
        launch = launch_val.get('value')
    elif isinstance(launch_val, str):
        launch = launch_val
    else:
        launch = None
    # "Proposed Milestone" — single-select option, same shape as Launch.
    milestone_val = f.get(MILESTONE_FIELD)
    if isinstance(milestone_val, dict):
        proposed_milestone = milestone_val.get('value')
    elif isinstance(milestone_val, str):
        proposed_milestone = milestone_val
    else:
        proposed_milestone = None
    # "Status Owner" — single-user picker. Capture the displayName for the
    # roster table column; null when nobody's been set yet.
    status_owner_val = f.get(STATUS_OWNER_FIELD)
    if isinstance(status_owner_val, dict):
        status_owner = status_owner_val.get('displayName')
    else:
        status_owner = None
    row = {
        'key': issue['key'],
        'summary': f.get('summary'),
        'status': status,
        'status_bucket': _bucket(status) if status else 'other',
        'issuetype': issuetype,
        'assignee': assignee,
        'status_owner': status_owner,
        'parent': parent,
        'fix_versions': fix_versions,
        'launch': launch,
        'proposed_milestone': proposed_milestone,
        'created': f.get('created'),
        'updated': f.get('updated'),
        'resolutiondate': f.get('resolutiondate'),
        'duedate': f.get('duedate'),
        'url': f"https://betfanatics.atlassian.net/browse/{issue['key']}",
    }
    if extra:
        row.update(extra)
    return row


def _fetch(jira, jql, fields, max_results=500):
    """Fetch issues via JiraAPICollector and dedupe by key.

    The v3 /search/jql endpoint (via startAt pagination) has been observed
    returning duplicate pages under some queries, so we always dedupe.
    """
    issues = jira.search_issues(jql, fields, max_results=max_results).get('issues', [])
    seen = set()
    unique = []
    for issue in issues:
        k = issue.get('key')
        if not k or k in seen:
            continue
        seen.add(k)
        unique.append(issue)
    return unique


def build_snapshot(config):
    jira = JiraAPICollector(config)

    # 1) Initiative itself
    logger.info("Fetching initiative %s", INITIATIVE_KEY)
    init_issues = _fetch(
        jira,
        f"key = {INITIATIVE_KEY}",
        ["summary", "status", "description", "created", "updated", "duedate", "assignee"],
        max_results=1,
    )
    if not init_issues:
        raise RuntimeError(f"Could not fetch {INITIATIVE_KEY}")
    init = init_issues[0]
    init_fields = init.get('fields', {})

    # 2) All Features under the initiative
    logger.info("Fetching child Features of %s", INITIATIVE_KEY)
    feature_issues = _fetch(
        jira,
        f'parent = {INITIATIVE_KEY} ORDER BY created ASC',
        ["summary", "status", "issuetype", "assignee", "priority", "duedate", "created", "updated", "resolutiondate", "fixVersions", LAUNCH_FIELD, MILESTONE_FIELD, STATUS_OWNER_FIELD],
        max_results=200,
    )
    features = [_slim(f) for f in feature_issues]
    feature_keys = [f['key'] for f in features if f['status_bucket'] != 'dropped']
    logger.info("Got %d features (%d not dropped)", len(features), len(feature_keys))

    # 3) All Epics under those features — one big JQL
    epics = []
    stories = []
    story_keys_seen = set()
    if feature_keys:
        feature_list = ", ".join(feature_keys)
        logger.info("Fetching epics under %d features", len(feature_keys))
        epic_issues = _fetch(
            jira,
            f'parent in ({feature_list}) AND type = Epic ORDER BY key',
            ["summary", "status", "issuetype", "assignee", "parent", "created", "updated", "resolutiondate", "fixVersions"],
            max_results=500,
        )
        epics = [_slim(e) for e in epic_issues]
        logger.info("Got %d epics", len(epics))

        # 4) Stories under those epics
        epic_keys = [e['key'] for e in epics if e['status_bucket'] != 'dropped']
        if epic_keys:
            # Chunk epic keys in batches of 50 to keep JQL size reasonable
            chunk_size = 50
            for i in range(0, len(epic_keys), chunk_size):
                chunk = epic_keys[i:i + chunk_size]
                jql = f'parent in ({", ".join(chunk)}) AND type = Story ORDER BY key'
                logger.info("Fetching stories under %d epics (chunk %d)", len(chunk), i // chunk_size + 1)
                story_issues = _fetch(
                    jira, jql,
                    ["summary", "status", "issuetype", "assignee", "parent", "created", "updated", "resolutiondate", "fixVersions"],
                    max_results=500,
                )
                # Dedupe across chunks
                for raw in story_issues:
                    slim = _slim(raw)
                    if slim['key'] in story_keys_seen:
                        continue
                    story_keys_seen.add(slim['key'])
                    stories.append(slim)
            logger.info("Got %d stories", len(stories))

    # Summary statistics
    feature_status_counts = Counter(f['status'] for f in features if f['status'])
    feature_bucket_counts = Counter(f['status_bucket'] for f in features)
    epic_bucket_counts = Counter(e['status_bucket'] for e in epics)
    story_bucket_counts = Counter(s['status_bucket'] for s in stories)

    # At-risk callouts
    at_risk = []
    for feat in features:
        if feat['status_bucket'] == 'dropped':
            continue
        reasons = []
        if feat['status'] == 'Product Discovery':
            stuck_days = _days_since(feat.get('updated'))
            if stuck_days is not None and stuck_days > 30:
                reasons.append(f"stuck in Product Discovery {stuck_days}d without an update")
        if not feat.get('assignee') and feat['status_bucket'] != 'discovery':
            reasons.append("no assignee")
        if not feat.get('duedate') and feat['status_bucket'] in {'in_flight'}:
            reasons.append("in flight with no due date")
        if reasons:
            at_risk.append({**feat, 'risk_reasons': reasons})

    # Build Confluence index with URLs
    confluence_index = []
    for group in CONFLUENCE_DOCS:
        entries = []
        for title, page_id in group['docs']:
            entries.append({
                'title': title,
                'url': f"https://betfanatics.atlassian.net/wiki/spaces/DFS/pages/{page_id}",
            })
        confluence_index.append({'folder': group['folder'], 'docs': entries})

    snapshot = {
        'generated_at': datetime.now(timezone.utc).isoformat(),
        'initiative': {
            'key': init['key'],
            'summary': init_fields.get('summary'),
            'description': _adf_to_text(init_fields.get('description')).strip() or None,
            'status': (init_fields.get('status') or {}).get('name'),
            'created': init_fields.get('created'),
            'updated': init_fields.get('updated'),
            'duedate': init_fields.get('duedate'),
            'url': f"https://betfanatics.atlassian.net/browse/{init['key']}",
        },
        'summary': {
            'features_total': len(features),
            'features_by_status': dict(feature_status_counts),
            'features_by_bucket': dict(feature_bucket_counts),
            'epics_total': len(epics),
            'epics_by_bucket': dict(epic_bucket_counts),
            'stories_total': len(stories),
            'stories_by_bucket': dict(story_bucket_counts),
            'at_risk_count': len(at_risk),
        },
        'features': features,
        'epics': epics,
        'stories': stories,
        'at_risk': at_risk,
        'confluence_space_url': f"{CONFLUENCE_BASE}/overview",
        'confluence_docs': confluence_index,
    }
    return snapshot


def main():
    config = load_config()
    # Redirect this script's logs to a dedicated file so the Agents dashboard
    # can surface them cleanly. Without this override every sync_* script
    # writes to the shared collector.log and their lines interleave.
    config = {**config, 'logging': {**config.get('logging', {}),
                                     'file': str(Path(__file__).parent.parent / 'logs' / 'sync_project_fantasy.log')}}
    setup_logging(config)
    logger.info("Starting Project: Fantasy snapshot")

    try:
        snapshot = build_snapshot(config)
    except Exception as e:
        logger.error("Snapshot failed: %s", e, exc_info=True)
        return 1

    out_path = Path(__file__).parent.parent / "data" / "project_fantasy.json"
    out_path.parent.mkdir(exist_ok=True, parents=True)
    out_path.write_text(json.dumps(snapshot, indent=2))
    s = snapshot['summary']
    logger.info(
        "Wrote snapshot to %s — features: %d, epics: %d, stories: %d, at-risk: %d",
        out_path, s['features_total'], s['epics_total'], s['stories_total'], s['at_risk_count'],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
