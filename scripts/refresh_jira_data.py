#!/usr/bin/env python3
"""
Refresh Jira data from MCP tool results.
This script reads a JSON file with Jira issues and updates the database.
"""

import sys
import json
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.statuses import (
    CLOSED_STATUSES,
    IN_PROGRESS_STATUSES,
    OPEN_STATUSES,
    EXCLUDED_STATUSES,
    sql_placeholders,
)
from database.schema import get_connection, init_database
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def refresh_jira_data(jira_data_file: str):
    """
    Refresh all Jira data from a fresh query.

    Args:
        jira_data_file: Path to JSON file with Jira API response
    """
    config = load_config()
    db_path = config['database']['path']
    story_points_field = config['jira']['story_points_field']
    story_points_fallback_fields = config['jira'].get('story_points_fallback_fields', [])

    # Load Jira data
    with open(jira_data_file, 'r') as f:
        data = json.load(f)

    issues = data.get('issues', [])
    logger.info(f"Processing {len(issues)} issues from Jira")

    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Wrap the whole pipeline in one explicit transaction so a) readers
        # never see a half-loaded sprint, and b) the long write doesn't hold a
        # series of short locks that interleave with concurrent readers (the
        # hygiene agent + QA agent both query in the meantime).
        # BEGIN IMMEDIATE acquires the write lock up front, so concurrent
        # writers wait at busy_timeout instead of after we've already done work.
        cursor.execute("BEGIN IMMEDIATE")

        # Snapshot (ticket_key → status) for active/future sprints BEFORE we
        # wipe tickets, so we can record real status transitions into
        # ticket_status_history. That history powers accurate cycle-time-per-point
        # downstream (replaces the previous proxy).
        cursor.execute("""
            SELECT ticket_key, sprint_id, status FROM tickets
            WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state IN ('active', 'future'))
        """)
        prior_status = {
            (row['ticket_key'], row['sprint_id']): row['status']
            for row in cursor.fetchall()
        }

        # Clear existing ticket data for active and future sprints, but preserve sprints and historical snapshots
        cursor.execute("DELETE FROM tickets WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state IN ('active', 'future'))")
        # Don't delete historical snapshots - we'll update today's or insert new ones
        cursor.execute("DELETE FROM developer_snapshots WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state IN ('active', 'future')) AND snapshot_date = date('now')")
        # DON'T delete sprints - we need to preserve them to keep historical snapshots via foreign key
        logger.info("Cleared existing ticket data for active/future sprints (preserved sprints and historical snapshots)")

        # Process issues and extract sprint info
        sprint_map = {}
        tickets_by_sprint = {}

        for issue in issues:
            fields = issue.get('fields', {})
            key = issue.get('key')

            # Get sprint info (customfield_10020)
            sprint_data_list = fields.get('customfield_10020', [])
            if not sprint_data_list:
                logger.warning(f"Issue {key} has no sprint data, skipping")
                continue

            # Filter to only active/future FNTSY sprints
            relevant_sprints = []
            for sprint_data in sprint_data_list:
                sprint_id = sprint_data.get('id')
                sprint_name = sprint_data.get('name', '')
                state = sprint_data.get('state', '').lower()

                # Only process active and future FNTSY sprints
                if state not in ['active', 'future'] or not sprint_name.startswith('FNTSY'):
                    continue

                relevant_sprints.append({
                    'sprint_id': sprint_id,
                    'sprint_name': sprint_name,
                    'state': state,
                    'start_date': sprint_data.get('startDate'),
                    'end_date': sprint_data.get('endDate'),
                    'goal': sprint_data.get('goal', '')
                })

            if not relevant_sprints:
                continue

            # Determine which sprints to store this ticket in
            # For Stories: pick the most relevant sprint (active > earliest future)
            # For Epics: store in ALL relevant sprints (they span multiple sprints)
            issue_type_obj = fields.get('issuetype', {})
            issue_type = issue_type_obj.get('name', 'Unknown')

            sprints_to_store = []
            if issue_type == 'Epic' and len(relevant_sprints) > 1:
                # Store epics in ALL their sprints
                sprints_to_store = relevant_sprints
            else:
                # For stories and other types, pick one sprint
                if len(relevant_sprints) == 1:
                    sprints_to_store = [relevant_sprints[0]]
                else:
                    # Check for active sprint first
                    active_sprints = [s for s in relevant_sprints if s['state'] == 'active']
                    if active_sprints:
                        sprints_to_store = [active_sprints[0]]  # Should only be one active
                    else:
                        # Pick earliest future sprint
                        future_sprints = sorted(relevant_sprints, key=lambda s: s['start_date'] or '')
                        sprints_to_store = [future_sprints[0]]

            # Store sprint info and ticket for each selected sprint
            for selected_sprint in sprints_to_store:
                sprint_id = selected_sprint['sprint_id']

                if sprint_id not in sprint_map:
                    sprint_map[sprint_id] = {
                        'jira_sprint_id': sprint_id,
                        'sprint_name': selected_sprint['sprint_name'],
                        'state': selected_sprint['state'],
                        'start_date': selected_sprint['start_date'],
                        'end_date': selected_sprint['end_date'],
                        'goal': selected_sprint['goal']
                    }
                    tickets_by_sprint[sprint_id] = []

                # Parse ticket (moved inside loop so each epic can be stored in multiple sprints)
                assignee = fields.get('assignee', {})
                assignee_account_id = assignee.get('accountId') if assignee else None
                assignee_display_name = assignee.get('displayName') if assignee else None

                # Story points - try primary field first, then fallbacks
                story_points = fields.get(story_points_field)
                if story_points is None:
                    # Try fallback fields
                    for fallback_field in story_points_fallback_fields:
                        story_points = fields.get(fallback_field)
                        if story_points is not None:
                            break

                if story_points is not None:
                    story_points = float(story_points)
                else:
                    story_points = 0.0

                # Status
                status_obj = fields.get('status', {})
                status = status_obj.get('name', 'Unknown')

                # Priority
                priority_obj = fields.get('priority', {})
                priority = priority_obj.get('name', 'Unknown')

                ticket = {
                    'ticket_key': key,
                    'jira_sprint_id': sprint_id,
                    'summary': fields.get('summary', ''),
                    'status': status,
                    'assignee_account_id': assignee_account_id,
                    'assignee_display_name': assignee_display_name,
                    'story_points': story_points,
                    'issue_type': issue_type,
                    'priority': priority,
                    'created_at': fields.get('created', ''),
                    'updated_at': fields.get('updated', ''),
                    'ticket_url': f"https://betfanatics.atlassian.net/browse/{key}"
                }

                tickets_by_sprint[sprint_id].append(ticket)

        logger.info(f"Found {len(sprint_map)} active sprint(s)")

        # Store sprints in database
        sprint_id_map = {}      # Maps jira_sprint_id to internal sprint_id
        sprint_state_map = {}   # Maps jira_sprint_id to Jira state ('active' | 'future' | 'closed')
        now = datetime.now().isoformat()

        for jira_sprint_id, sprint_info in sprint_map.items():
            # Check if sprint already exists
            cursor.execute(
                "SELECT sprint_id, first_seen_at FROM sprints WHERE jira_sprint_id = ?",
                (jira_sprint_id,)
            )
            existing_sprint = cursor.fetchone()

            if existing_sprint:
                # Update existing sprint (preserve first_seen_at)
                sprint_id = existing_sprint[0]
                first_seen = existing_sprint[1]
                cursor.execute("""
                    UPDATE sprints
                    SET sprint_name = ?, state = ?, start_date = ?, end_date = ?, goal = ?, last_updated_at = ?
                    WHERE sprint_id = ?
                """, (
                    sprint_info['sprint_name'], sprint_info['state'],
                    sprint_info['start_date'], sprint_info['end_date'], sprint_info['goal'],
                    now, sprint_id
                ))
                logger.info(f"Updated sprint: {sprint_info['sprint_name']}")
            else:
                # Insert new sprint
                cursor.execute("""
                    INSERT INTO sprints (
                        jira_sprint_id, sprint_name, state, start_date, end_date, goal,
                        first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    jira_sprint_id, sprint_info['sprint_name'], sprint_info['state'],
                    sprint_info['start_date'], sprint_info['end_date'], sprint_info['goal'],
                    now, now
                ))
                cursor.execute("SELECT last_insert_rowid()")
                sprint_id = cursor.fetchone()[0]
                logger.info(f"Created sprint: {sprint_info['sprint_name']}")

            sprint_id_map[jira_sprint_id] = sprint_id
            sprint_state_map[jira_sprint_id] = sprint_info['state']


        # Store tickets
        for jira_sprint_id, tickets in tickets_by_sprint.items():
            sprint_id = sprint_id_map[jira_sprint_id]

            for ticket in tickets:
                # For epics, append sprint ID to ticket_key to allow same epic in multiple sprints
                # For other types (Stories), use ticket_key as-is (they only get one sprint)
                ticket_key_db = ticket['ticket_key']
                if ticket['issue_type'] == 'Epic':
                    ticket_key_db = f"{ticket['ticket_key']}_s{jira_sprint_id}"

                cursor.execute("""
                    INSERT OR REPLACE INTO tickets (
                        ticket_key, sprint_id, summary, status, assignee_account_id,
                        assignee_display_name, story_points, issue_type, priority,
                        created_at, updated_at, ticket_url, first_seen_at, last_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticket_key_db, sprint_id, ticket['summary'], ticket['status'],
                    ticket['assignee_account_id'], ticket['assignee_display_name'],
                    ticket['story_points'], ticket['issue_type'], ticket['priority'],
                    ticket['created_at'], ticket['updated_at'], ticket['ticket_url'],
                    now, now
                ))

            logger.info(f"Stored {len(tickets)} tickets for sprint {sprint_id}")

        # Record status transitions into ticket_status_history so downstream
        # metrics (cycle-time-per-point) have real data instead of proxies.
        transitions_written = 0
        for jira_sprint_id, tickets in tickets_by_sprint.items():
            sprint_id = sprint_id_map[jira_sprint_id]
            for ticket in tickets:
                ticket_key_db = ticket['ticket_key']
                if ticket['issue_type'] == 'Epic':
                    ticket_key_db = f"{ticket['ticket_key']}_s{jira_sprint_id}"
                old = prior_status.get((ticket_key_db, sprint_id))
                new = ticket['status']
                if old is not None and old != new:
                    cursor.execute("""
                        INSERT INTO ticket_status_history
                            (ticket_key, sprint_id, old_status, new_status, changed_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (ticket_key_db, sprint_id, old, new, now))
                    transitions_written += 1
        if transitions_written:
            logger.info(f"Recorded {transitions_written} status transition(s) in ticket_status_history")

        # Calculate sprint snapshots. Status buckets come from src/utils/statuses.py
        # so this query can't drift from the dashboard or QA checks.
        # Skip 'future' sprints — writing snapshots before a sprint starts produces
        # pre-sprint rows that anchor the burndown ideal line at a misleading value.
        closed_ph = sql_placeholders(CLOSED_STATUSES)
        inprog_ph = sql_placeholders(IN_PROGRESS_STATUSES)
        excl_ph = sql_placeholders(EXCLUDED_STATUSES)
        for jira_sprint_id, sprint_id in sprint_id_map.items():
            if sprint_state_map.get(jira_sprint_id) != 'active':
                continue
            cursor.execute(f"""
                SELECT
                    COALESCE(SUM(story_points), 0) as total_sp,
                    COALESCE(SUM(CASE WHEN status IN ({closed_ph}) THEN story_points ELSE 0 END), 0) as completed_sp,
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN status IN ({closed_ph}) THEN 1 ELSE 0 END) as closed_tickets,
                    SUM(CASE WHEN status IN ({inprog_ph}) THEN 1 ELSE 0 END) as in_progress_tickets
                FROM tickets
                WHERE sprint_id = ? AND issue_type = 'Story' AND status NOT IN ({excl_ph})
            """, (
                list(CLOSED_STATUSES)
                + list(CLOSED_STATUSES)
                + list(IN_PROGRESS_STATUSES)
                + [sprint_id]
                + list(EXCLUDED_STATUSES)
            ))

            row = cursor.fetchone()
            total_sp, completed_sp, total_tickets, closed_tickets, in_progress_tickets = row
            remaining_sp = total_sp - completed_sp
            open_tickets = total_tickets - (closed_tickets or 0) - (in_progress_tickets or 0)

            today = datetime.now().date().isoformat()
            timestamp = datetime.now().isoformat()

            cursor.execute("""
                INSERT OR REPLACE INTO sprint_snapshots (
                    sprint_id, snapshot_date, snapshot_timestamp,
                    total_story_points, completed_story_points, remaining_story_points,
                    total_tickets, open_tickets, closed_tickets, in_progress_tickets
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sprint_id, today, timestamp,
                total_sp, completed_sp, remaining_sp,
                total_tickets, open_tickets, closed_tickets, in_progress_tickets
            ))

            logger.info(f"Sprint snapshot: {closed_tickets}/{total_tickets} stories closed, {completed_sp}/{total_sp} SP")

        # Calculate developer snapshots — skip non-active sprints for the same
        # reason as the sprint-level loop above.
        for jira_sprint_id, sprint_id in sprint_id_map.items():
            if sprint_state_map.get(jira_sprint_id) != 'active':
                continue
            cursor.execute("""
                SELECT DISTINCT assignee_account_id, assignee_display_name
                FROM tickets
                WHERE sprint_id = ? AND assignee_account_id IS NOT NULL AND issue_type = 'Story'
            """, (sprint_id,))

            developers = cursor.fetchall()

            # Bucket lists parameterized from the shared taxonomy.
            open_ph_d = sql_placeholders(OPEN_STATUSES)
            inprog_ph_d = sql_placeholders(IN_PROGRESS_STATUSES)
            closed_ph_d = sql_placeholders(CLOSED_STATUSES)
            excl_ph_d = sql_placeholders(EXCLUDED_STATUSES)

            for dev_id, dev_name in developers:
                cursor.execute(f"""
                    SELECT
                        COALESCE(SUM(story_points), 0) as assigned_sp,
                        COALESCE(SUM(CASE WHEN status IN ({closed_ph_d}) THEN story_points ELSE 0 END), 0) as completed_sp,
                        SUM(CASE WHEN status IN ({closed_ph_d}) THEN 1 ELSE 0 END) as completed_tickets,
                        SUM(CASE WHEN status IN ({inprog_ph_d}) THEN 1 ELSE 0 END) as in_progress_tickets,
                        SUM(CASE WHEN status IN ({open_ph_d}) THEN 1 ELSE 0 END) as todo_tickets
                    FROM tickets
                    WHERE sprint_id = ? AND assignee_account_id = ? AND issue_type = 'Story'
                      AND status NOT IN ({excl_ph_d})
                """, (
                    list(CLOSED_STATUSES)
                    + list(CLOSED_STATUSES)
                    + list(IN_PROGRESS_STATUSES)
                    + list(OPEN_STATUSES)
                    + [sprint_id, dev_id]
                    + list(EXCLUDED_STATUSES)
                ))

                row = cursor.fetchone()
                assigned_sp, completed_sp, completed_tickets, in_progress_tickets, todo_tickets = row

                assigned_sp = assigned_sp or 0.0
                completed_sp = completed_sp or 0.0
                completed_tickets = completed_tickets or 0
                in_progress_tickets = in_progress_tickets or 0
                todo_tickets = todo_tickets or 0
                remaining_sp = assigned_sp - completed_sp

                today = datetime.now().date().isoformat()
                timestamp = datetime.now().isoformat()

                # Idempotent per (sprint_id, developer_id, snapshot_date):
                # the day-of-month DELETE above only clears active/future
                # sprints, so a replay on a closed sprint would otherwise
                # hit the UNIQUE constraint. INSERT OR REPLACE is safe here
                # because the row is a snapshot — last write wins.
                cursor.execute("""
                    INSERT OR REPLACE INTO developer_snapshots (
                        sprint_id, developer_id, developer_name, snapshot_date, snapshot_timestamp,
                        assigned_story_points, completed_story_points, remaining_story_points,
                        tickets_in_progress, tickets_completed, tickets_todo
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sprint_id, dev_id, dev_name, today, timestamp,
                    assigned_sp, completed_sp, remaining_sp,
                    in_progress_tickets, completed_tickets, todo_tickets
                ))

                logger.info(f"{dev_name}: {completed_tickets} done, {in_progress_tickets} in progress, {todo_tickets} todo")

        # Garbage-collect stale developer_snapshots for the active sprint:
        # any (sprint_id, developer_id) pair that no longer has tickets in
        # the current sprint should not appear on the Team Members page.
        # This prevents ghost rows from tickets that moved out of the sprint
        # or assignees who got reassigned.
        cursor.execute("""
            DELETE FROM developer_snapshots
            WHERE sprint_id IN (SELECT sprint_id FROM sprints WHERE state = 'active')
              AND (sprint_id, developer_id) NOT IN (
                  SELECT DISTINCT sprint_id, assignee_account_id
                  FROM tickets
                  WHERE assignee_account_id IS NOT NULL
                    AND issue_type = 'Story'
              )
        """)
        gc_rows = cursor.rowcount
        if gc_rows:
            logger.info(f"Garbage-collected {gc_rows} stale developer_snapshots rows (devs no longer in active sprint)")

        conn.commit()
        logger.info("✅ Jira data refresh complete!")

    except Exception:
        # Roll back the BEGIN IMMEDIATE so the DB stays consistent and any
        # partial DELETE/INSERT from this run is undone. Re-raise so the
        # collector agent's retry loop sees the failure.
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: refresh_jira_data.py <jira_json_file>")
        sys.exit(1)

    refresh_jira_data(sys.argv[1])
