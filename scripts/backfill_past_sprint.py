#!/usr/bin/env python3
"""Backfill ticket data for a closed Jira sprint.

The unified collector only sees `openSprints() OR futureSprints()`, so once a
sprint closes its tickets get re-deleted by `refresh_jira_data` on every cron
run. This script re-fetches Stories+Bugs for a specific sprint by jira_sprint_id
and writes them back into the local DB. After persisting, it flips
`sprints.state='closed'` so the active-sprint DELETE no longer touches them.

Idempotent — safe to re-run.

Usage:
    python3 scripts/backfill_past_sprint.py --sprint-id 21193
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from utils.statuses import EXCLUDED_STATUSES
from database.schema import get_connection
from collectors.jira_api_collector import JiraAPICollector

logger = logging.getLogger(__name__)


def _ensure_status_at_sprint_end_column(cursor) -> None:
    """Idempotently add tickets.status_at_sprint_end if it isn't there yet.

    Retries briefly on `database is locked` because cron-driven writers
    (hygiene, collector, QA) may overlap with this script.
    """
    import sqlite3 as _sql
    import time as _time
    cursor.execute("PRAGMA table_info(tickets)")
    cols = {row[1] for row in cursor.fetchall()}
    if 'status_at_sprint_end' in cols:
        return
    for attempt in range(8):
        try:
            cursor.execute("ALTER TABLE tickets ADD COLUMN status_at_sprint_end TEXT")
            return
        except _sql.OperationalError as e:
            if 'locked' not in str(e).lower() or attempt == 7:
                raise
            _time.sleep(1.0 * (attempt + 1))


def _status_as_of(histories: list, current_status: str, cutoff_iso: str) -> str:
    """Return the ticket's status as of `cutoff_iso`.

    Walks the changelog in reverse-chronological order and rewinds any status
    transitions that happened *after* the cutoff. If a transition's `created`
    timestamp is on/before the cutoff we stop; nothing earlier matters.

    `histories` is the list returned by the Jira changelog endpoint; each item
    has `created` and an `items` list with field/fromString/toString. If none
    of the histories include a status change, the current status is the
    sprint-end status (the ticket has not moved since).
    """
    if not cutoff_iso:
        return current_status
    status_now = current_status
    sorted_h = sorted(histories, key=lambda h: h.get('created') or '', reverse=True)
    for h in sorted_h:
        created = h.get('created') or ''
        if created and created <= cutoff_iso:
            break  # this history is at/before sprint end — status_now is correct
        for item in h.get('items', []) or []:
            if item.get('field') == 'status':
                # Walking backward: revert to the value before this transition.
                from_str = item.get('fromString')
                if from_str is not None:
                    status_now = from_str
    return status_now


def backfill_past_sprint(jira_sprint_id: int, db_path: Optional[str] = None) -> int:
    """Backfill Story+Bug tickets for one closed sprint.

    Returns the number of tickets written.
    """
    config = load_config()
    db_path = db_path or config['database']['path']
    sprint_prefix = config['jira']['sprint_prefix']
    sp_field = config['jira']['story_points_field']
    sp_fallbacks = config['jira'].get('story_points_fallback_fields', [])

    jira = JiraAPICollector(config)

    excluded_jql = ','.join(f'"{s}"' for s in EXCLUDED_STATUSES)
    jql = (
        f'sprint = {jira_sprint_id} '
        f'AND project = {sprint_prefix} '
        f'AND type IN ("Story","Bug") '
        f'AND status NOT IN ({excluded_jql})'
    )
    fields = [
        'summary', 'status', 'assignee', 'issuetype', 'priority',
        'created', 'updated', sp_field, 'customfield_10020',
    ] + list(sp_fallbacks)

    logger.info(f"Backfill: fetching tickets for jira_sprint_id={jira_sprint_id}")
    data = jira.search_issues(jql, fields, max_results=2000, expand=['changelog'])
    issues = data.get('issues', [])
    logger.info(f"Backfill: Jira returned {len(issues)} issues")

    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        _ensure_status_at_sprint_end_column(cursor)

        cursor.execute(
            "SELECT sprint_id, sprint_name, end_date FROM sprints WHERE jira_sprint_id = ?",
            (jira_sprint_id,),
        )
        row = cursor.fetchone()
        if row is None:
            raise SystemExit(
                f"No sprints row for jira_sprint_id={jira_sprint_id}. "
                "Refuse to synthesize sprint metadata; let the collector seed it first."
            )
        sprint_id = row['sprint_id']
        sprint_name = row['sprint_name']
        cutoff = row['end_date'] or ''

        now = datetime.now().isoformat()
        written = 0
        for issue in issues:
            f = issue.get('fields', {})
            key = issue.get('key')
            issue_type = (f.get('issuetype') or {}).get('name', 'Unknown')
            if issue_type == 'Epic':
                continue  # belt-and-braces; JQL already excludes Epics

            assignee = f.get('assignee') or {}
            sp = f.get(sp_field)
            if sp is None:
                for fb in sp_fallbacks:
                    sp = f.get(fb)
                    if sp is not None:
                        break
            sp = float(sp) if sp is not None else 0.0

            current_status = (f.get('status') or {}).get('name', 'Unknown')
            histories = ((issue.get('changelog') or {}).get('histories')) or []
            status_at_end = _status_as_of(histories, current_status, cutoff)

            cursor.execute(
                """
                INSERT OR REPLACE INTO tickets (
                    ticket_key, sprint_id, summary, status, assignee_account_id,
                    assignee_display_name, story_points, issue_type, priority,
                    created_at, updated_at, ticket_url, first_seen_at, last_updated_at,
                    status_at_sprint_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, sprint_id, f.get('summary', ''),
                    current_status,
                    assignee.get('accountId'), assignee.get('displayName'),
                    sp, issue_type, (f.get('priority') or {}).get('name', 'Unknown'),
                    f.get('created', ''), f.get('updated', ''),
                    f"https://betfanatics.atlassian.net/browse/{key}",
                    now, now, status_at_end,
                ),
            )
            written += 1

        cursor.execute(
            "UPDATE sprints SET state = 'closed', last_updated_at = ? WHERE sprint_id = ?",
            (now, sprint_id),
        )
        conn.commit()
        logger.info(
            f"Backfilled {written} tickets for {sprint_name} "
            f"(sprint_id={sprint_id}); marked state=closed"
        )
        return written
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sprint-id', type=int, required=True,
                        help='Jira sprint ID (e.g. 21193 for M30.1)')
    parser.add_argument('--db-path', help='Optional override for the DB path')
    args = parser.parse_args()

    setup_logging({'logging': {'level': 'INFO', 'file': 'logs/backfill_past_sprint.log'}})
    backfill_past_sprint(args.sprint_id, db_path=args.db_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
