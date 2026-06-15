#!/usr/bin/env python3
"""Refresh the live Jira state of tickets that live in closed sprints.

Why this exists
---------------
The unified collector (``jira_collector_agent.py`` → ``refresh_jira_data.py``)
only writes tickets that belong to active or future sprints. Once a sprint
closes, its rows in ``tickets`` are frozen — no later collector cycle ever
revisits them. That's intentional for things like story_points and the
status-at-sprint-end snapshot used to compute velocity.

But the Epics dashboard (and a few other views) also surface tickets from
closed sprints in the recent window, and PMs reasonably expect the rendered
status of those tickets to track Jira. Today they don't: an epic that moved
to Done after its sprint closed keeps showing whatever status it had at
sprint close, forever.

This script closes the gap: after every collector run, re-fetch the current
Jira state of every ticket whose ``sprint_id`` lives in the recent
dashboard window AND whose sprint is closed, and update only the columns
PMs see drift in:

  * ``status``
  * ``summary``
  * ``assignee_account_id`` / ``assignee_display_name``
  * ``last_updated_at``

We deliberately do NOT touch ``story_points``, ``sprint_id``, or
``status_at_sprint_end`` — those are historical artifacts of sprint close
and changing them retroactively would corrupt velocity / cycle-time metrics.

Idempotent. Safe to run on every collector cycle. Skips cleanly when Jira
creds aren't set so the rest of the pipeline isn't blocked.

Usage
-----
    python3 scripts/refresh_closed_sprint_tickets.py
    python3 scripts/refresh_closed_sprint_tickets.py --window 8
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config  # noqa: E402
from utils.logging_config import setup_logging  # noqa: E402
from database.schema import get_connection  # noqa: E402
from collectors.jira_api_collector import JiraAPICollector  # noqa: E402

logger = logging.getLogger(__name__)


def _closed_sprint_ticket_keys(db_path: str, window: int) -> tuple[list[str], list[int]]:
    """Return (ticket_keys, sprint_ids) for the ``window`` most-recent CLOSED
    sprints the dashboards actually surface.

    This MUST select among *closed* sprints, not "the N newest sprints of any
    state". The collector ingests the whole Jira board, so the newest sprints
    by ``start_date`` are all ``future`` (months out) — a naive
    ``ORDER BY start_date DESC LIMIT N`` never reaches a single closed sprint,
    and this refresh silently no-ops. The Epics gantt shows the 2 most-recent
    closed FNTSY sprints and Past Sprints shows up to 12, so we select the
    closed sprints directly (prefix-filtered, recency-ordered) and default the
    window to the deepest of those views."""
    config = load_config()
    sprint_prefix = config['jira']['sprint_prefix']

    with get_connection(db_path) as conn:
        # Closed = explicitly state='closed', OR ended in the past without an
        # active/future flag (defensive against a missed close transition).
        # Order by recency and take the deepest window any dashboard surfaces.
        today_iso = datetime.now().date().isoformat()
        rows = conn.execute(
            """
            SELECT sprint_id, state, end_date
            FROM sprints
            WHERE is_placeholder = 0
              AND sprint_name LIKE ? || '%'
              AND (
                    state = 'closed'
                    OR (end_date IS NOT NULL
                        AND substr(end_date, 1, 10) < ?
                        AND state NOT IN ('active', 'future'))
                  )
            ORDER BY COALESCE(end_date, start_date) DESC
            LIMIT ?
            """,
            (sprint_prefix, today_iso, window),
        ).fetchall()

        closed_sprint_ids = [r['sprint_id'] for r in rows]

        if not closed_sprint_ids:
            return [], []

        placeholders = ','.join('?' * len(closed_sprint_ids))
        ticket_rows = conn.execute(
            f"""
            SELECT ticket_key
            FROM tickets
            WHERE sprint_id IN ({placeholders})
            """,
            closed_sprint_ids,
        ).fetchall()

    # Strip the `_s<sprint_id>` suffix the collector adds when an epic spans
    # multiple sprints — Jira only knows the bare key. We dedupe across
    # suffix variants, then map back to all rows when we update.
    bare_keys = sorted({r['ticket_key'].split('_s')[0] for r in ticket_rows})
    return bare_keys, closed_sprint_ids


def refresh_closed_sprint_tickets(window: int = 12, db_path: Optional[str] = None) -> int:
    """Re-pull live Jira status/summary/assignee for closed-sprint tickets in
    the dashboard's recent-sprint window. Returns the number of DB rows
    updated.
    """
    config = load_config()
    db_path = db_path or config['database']['path']

    keys, sprint_ids = _closed_sprint_ticket_keys(db_path, window)
    if not keys:
        logger.info("No closed-sprint tickets in the recent %d-sprint window — nothing to refresh.", window)
        return 0

    logger.info(
        "Refreshing %d closed-sprint tickets across %d sprints (window=%d).",
        len(keys), len(sprint_ids), window,
    )

    jira = JiraAPICollector(config)

    # JQL "key in (...)" is the cheap path. Jira's /search/jql endpoint
    # returns 100 issues per page; chunk to that exact size so each query
    # fits in a single page and we never depend on the new endpoint's
    # nextPageToken pagination (which `search_issues` doesn't support).
    fields = ['summary', 'status', 'assignee']
    chunk_size = 100
    by_key: dict[str, dict] = {}
    for i in range(0, len(keys), chunk_size):
        chunk = keys[i: i + chunk_size]
        jql = "key in (" + ",".join(chunk) + ")"
        try:
            data = jira.search_issues(jql, fields, max_results=chunk_size)
        except Exception as e:
            logger.warning("Jira lookup failed for chunk starting %s: %s", chunk[0], e)
            continue
        for issue in data.get('issues') or []:
            k = issue.get('key')
            if k:
                by_key[k] = issue.get('fields') or {}

    if not by_key:
        logger.warning("Jira returned no issues — leaving DB untouched.")
        return 0

    now_iso = datetime.now().isoformat()
    updated = 0
    with get_connection(db_path) as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            for bare_key, fields_map in by_key.items():
                status = (fields_map.get('status') or {}).get('name')
                summary = fields_map.get('summary') or ''
                assignee = fields_map.get('assignee') or {}
                acct = assignee.get('accountId')
                disp = assignee.get('displayName')
                if not status:
                    continue

                # Update every row with this bare key (epics may have multiple
                # `_s<sprint_id>` siblings). Constrain to closed-sprint rows
                # only so a key that also lives in an active sprint isn't
                # double-touched here — the active row is owned by
                # refresh_jira_data and was just re-inserted.
                placeholders = ','.join('?' * len(sprint_ids))
                cur.execute(
                    f"""
                    UPDATE tickets
                       SET status = ?,
                           summary = ?,
                           assignee_account_id = ?,
                           assignee_display_name = ?,
                           last_updated_at = ?
                     WHERE (ticket_key = ? OR ticket_key LIKE ?)
                       AND sprint_id IN ({placeholders})
                    """,
                    (status, summary, acct, disp, now_iso,
                     bare_key, bare_key + '_s%', *sprint_ids),
                )
                updated += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info("Updated %d closed-sprint ticket rows.", updated)
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--window', type=int, default=12,
                        help='How many recent CLOSED sprints to consider (default 12 — '
                             'matches the deepest dashboard window, Past Sprints).')
    parser.add_argument('--db-path', help='Optional override for the DB path.')
    args = parser.parse_args()

    setup_logging({'logging': {
        'level': 'INFO',
        'file': 'logs/refresh_closed_sprint_tickets.log',
    }})
    refresh_closed_sprint_tickets(window=args.window, db_path=args.db_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
