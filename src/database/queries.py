"""
Common database query functions.
"""

import sqlite3
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging

from database.schema import get_connection
from utils.statuses import (
    CLOSED_STATUSES,
    IN_PROGRESS_STATUSES,
    sql_placeholders,
)

logger = logging.getLogger(__name__)


# Pre-built placeholder strings for the status buckets — saves recomputing
# in every cycle-time query. The lists themselves are tuples (immutable),
# so caching the placeholder string is safe.
_CLOSED_PH = sql_placeholders(CLOSED_STATUSES)
_INPROG_PH = sql_placeholders(IN_PROGRESS_STATUSES)


def _working_time_days(start: datetime, end: datetime) -> float:
    """Working-day (Mon-Fri) elapsed time between two datetimes, in days.

    Weekend hours between two weekday datetimes are excluded; fractional
    day portions on the endpoints are preserved. Used for cycle-time and
    PR-merge-time calculations so that work paused over a weekend doesn't
    inflate duration metrics.
    """
    if end <= start:
        return 0.0
    total = 0.0
    cur = start
    while cur < end:
        day_end = cur.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        segment_end = end if end < day_end else day_end
        if cur.weekday() < 5:  # 0-4 = Mon-Fri
            total += (segment_end - cur).total_seconds() / 86400
        cur = segment_end
    return total


def parse_iso_tz(value: str) -> Optional[datetime]:
    """Parse an ISO-ish timestamp from Jira/GitHub.

    Returns None on failure. Handles two quirks beyond stdlib fromisoformat:
      * trailing 'Z' for UTC (which fromisoformat 3.10 rejects);
      * Jira's -0700 (no colon) timezone format that some endpoints emit
        despite the docs saying otherwise.

    Public — callers in the generator and agents should use this instead of
    inlining `datetime.fromisoformat(s.replace('Z','+00:00'))` so the
    no-colon-tz case doesn't crash silently.
    """
    if not value:
        return None
    s = value
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    # Fix timezone format coming out of Jira: -0700 -> -07:00
    if len(s) >= 5 and (s[-5:-4] == '-' or s[-5:-4] == '+') and s[-3] != ':':
        s = s[:-2] + ':' + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# Backwards-compat alias — older call sites use the private name.
_parse_iso_tz = parse_iso_tz


def get_current_sprint(db_path: str, sprint_prefix: str = "FNTSY") -> Optional[Dict[str, Any]]:
    """
    Get the current active sprint matching the prefix.

    Args:
        db_path: Path to database
        sprint_prefix: Sprint name prefix to filter on

    Returns:
        Dict with sprint info, or None if not found
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                sprint_id, jira_sprint_id, sprint_name, state,
                start_date, end_date, goal
            FROM sprints
            WHERE state = 'active' AND sprint_name LIKE ?
            ORDER BY start_date DESC
            LIMIT 1
        """, (f"{sprint_prefix}%",))

        result = cursor.fetchone()
        return dict(result) if result else None
    finally:
        conn.close()


def get_sprint_metrics(db_path: str, sprint_id: int) -> Optional[Dict[str, Any]]:
    """
    Get the latest metrics snapshot for a sprint.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID

    Returns:
        Dict with sprint metrics
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                total_story_points,
                completed_story_points,
                remaining_story_points,
                total_tickets,
                open_tickets,
                closed_tickets,
                in_progress_tickets,
                snapshot_date,
                snapshot_timestamp
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
        """, (sprint_id,))

        result = cursor.fetchone()
        return dict(result) if result else None
    finally:
        conn.close()


def get_sprint_burndown(db_path: str, sprint_id: int) -> List[Dict[str, Any]]:
    """
    Get burndown data (time series) for a sprint.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID

    Returns:
        List of daily snapshots
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                snapshot_date,
                remaining_story_points,
                completed_story_points,
                total_story_points,
                open_tickets,
                closed_tickets
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_date ASC
        """, (sprint_id,))

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_all_developers_metrics(db_path: str, sprint_id: int) -> List[Dict[str, Any]]:
    """
    Get per-developer sprint metrics for the Team Members page.

    Returns one row per distinct assignee on any non-Epic, non-excluded
    ticket in the sprint (Stories, Tasks, Bugs, Sub-tasks). Previously this
    read from `developer_snapshots` which only included Story-type tickets,
    so people assigned only Tasks/Bugs never showed up.

    Returns:
        List of developer metrics dicts with keys matching the snapshot
        table so existing callers don't need to change.
    """
    from utils.statuses import CLOSED_STATUSES, IN_PROGRESS_STATUSES, OPEN_STATUSES, EXCLUDED_STATUSES

    # Widen from "Story only" → work types a developer actually owns.
    # Epics are tracking summaries, so we still exclude them.
    INCLUDED_ISSUE_TYPES = ('Story', 'Task', 'Bug', 'Sub-task', 'Subtask')

    def _placeholders(seq):
        return ",".join("?" * len(seq))

    conn = get_connection(db_path)
    cursor = conn.cursor()

    type_ph = _placeholders(INCLUDED_ISSUE_TYPES)
    closed_ph = _placeholders(CLOSED_STATUSES)
    inprog_ph = _placeholders(IN_PROGRESS_STATUSES)
    todo_ph = _placeholders(OPEN_STATUSES)
    excl_ph = _placeholders(EXCLUDED_STATUSES)

    try:
        cursor.execute(
            f"""
            SELECT
                assignee_account_id AS developer_id,
                assignee_display_name AS developer_name,
                COALESCE(SUM(story_points), 0) AS assigned_story_points,
                COALESCE(SUM(CASE WHEN status IN ({closed_ph}) THEN story_points ELSE 0 END), 0) AS completed_story_points,
                COALESCE(SUM(CASE WHEN status NOT IN ({closed_ph}) AND status NOT IN ({excl_ph}) THEN story_points ELSE 0 END), 0) AS remaining_story_points,
                SUM(CASE WHEN status IN ({inprog_ph}) THEN 1 ELSE 0 END) AS tickets_in_progress,
                SUM(CASE WHEN status IN ({closed_ph}) THEN 1 ELSE 0 END) AS tickets_completed,
                SUM(CASE WHEN status IN ({todo_ph}) THEN 1 ELSE 0 END) AS tickets_todo,
                DATE('now') AS snapshot_date
            FROM tickets
            WHERE sprint_id = ?
              AND assignee_account_id IS NOT NULL
              AND issue_type IN ({type_ph})
              AND status NOT IN ({excl_ph})
            GROUP BY assignee_account_id, assignee_display_name
            HAVING (tickets_in_progress + tickets_completed + tickets_todo) > 0
            ORDER BY assignee_display_name
            """,
            list(CLOSED_STATUSES)  # completed_sp
            + list(CLOSED_STATUSES) + list(EXCLUDED_STATUSES)  # remaining_sp
            + list(IN_PROGRESS_STATUSES)
            + list(CLOSED_STATUSES)
            + list(OPEN_STATUSES)
            + [sprint_id]
            + list(INCLUDED_ISSUE_TYPES)
            + list(EXCLUDED_STATUSES),
        )

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_developer_tickets(db_path: str, sprint_id: int, developer_id: str) -> Dict[str, List[Dict[str, Any]]]:
    """
    Get all tickets for a developer, grouped by status.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        developer_id: Developer's Jira account ID

    Returns:
        Dict mapping status to list of tickets
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Same issue-type widening as get_all_developers_metrics: include
        # Stories, Tasks, Bugs, Sub-tasks so that Task-only folks still get
        # ticket accordions populated.
        cursor.execute("""
            SELECT
                ticket_key,
                summary,
                status,
                story_points,
                issue_type,
                priority,
                ticket_url,
                updated_at
            FROM tickets
            WHERE sprint_id = ? AND assignee_account_id = ?
              AND issue_type IN ('Story', 'Task', 'Bug', 'Sub-task', 'Subtask')
              AND status NOT IN ('Abandoned', 'Duplicate')
            ORDER BY status, updated_at DESC
        """, (sprint_id, developer_id))

        tickets_by_status = {}
        for row in cursor.fetchall():
            ticket = dict(row)
            status = ticket['status']
            if status not in tickets_by_status:
                tickets_by_status[status] = []
            tickets_by_status[status].append(ticket)

        return tickets_by_status
    finally:
        conn.close()


def get_tickets_by_status(db_path: str, sprint_id: int, status: str) -> List[Dict[str, Any]]:
    """
    Get all tickets in a sprint with a specific status.

    Prefer `get_tickets_for_sprint(... statuses=[...])` for batched fetches.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        status: Ticket status

    Returns:
        List of tickets
    """
    return get_tickets_for_sprint(db_path, sprint_id, statuses=[status])


def get_tickets_for_sprint(
    db_path: str,
    sprint_id: int,
    statuses: Optional[List[str]] = None,
    issue_type: Optional[str] = 'Story',
) -> List[Dict[str, Any]]:
    """
    Get all sprint tickets in one query, optionally filtered by a set of statuses.

    Replaces per-status loops that issued N separate queries. Each returned dict
    includes 'status' so callers can partition client-side.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        statuses: Optional iterable of statuses (e.g., CLOSED_STATUSES). When
            None or empty, all statuses are returned.
        issue_type: Filter by Jira issue type. Defaults to 'Story' to match the
            historical behavior of `get_tickets_by_status`. Pass None to disable.

    Returns:
        List of tickets, each with status included.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        clauses = ["sprint_id = ?"]
        params: List[Any] = [sprint_id]

        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

        if issue_type is not None:
            clauses.append("issue_type = ?")
            params.append(issue_type)

        sql = f"""
            SELECT
                ticket_key,
                summary,
                status,
                assignee_account_id,
                assignee_display_name,
                story_points,
                issue_type,
                priority,
                ticket_url,
                updated_at
            FROM tickets
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC
        """
        cursor.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_pr_metrics(db_path: str, github_username: str, days: int = 30) -> Dict[str, Any]:
    """
    Get PR metrics for a developer.

    Args:
        db_path: Path to database
        github_username: Developer's GitHub username
        days: How many days back to calculate average

    Returns:
        Dict with PR metrics
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Get latest open PR count
        cursor.execute("""
            SELECT open_pr_count, snapshot_timestamp
            FROM github_pr_snapshots
            WHERE developer_github_username = ?
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
        """, (github_username,))

        snapshot = cursor.fetchone()
        open_count = dict(snapshot)['open_pr_count'] if snapshot else 0

        # Calculate average time to merge for recent merged PRs.
        # Compute in Python so weekend hours can be excluded.
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT created_at, merged_at
            FROM github_prs
            WHERE author_github_username = ?
              AND state = 'merged'
              AND merged_at >= ?
              AND created_at IS NOT NULL
              AND merged_at IS NOT NULL
        """, (github_username, cutoff_date))

        merge_hours = []
        for row in cursor.fetchall():
            created = _parse_iso_tz(row['created_at'])
            merged = _parse_iso_tz(row['merged_at'])
            if not created or not merged:
                continue
            wd_hours = _working_time_days(created, merged) * 24
            if wd_hours > 0:
                merge_hours.append(wd_hours)

        avg_hours = sum(merge_hours) / len(merge_hours) if merge_hours else None
        merged_count = len(merge_hours)

        return {
            'github_username': github_username,
            'open_pr_count': open_count,
            'avg_hours_to_merge': round(avg_hours, 1) if avg_hours else None,
            'merged_pr_count_last_n_days': merged_count
        }
    finally:
        conn.close()


def get_review_metrics(db_path: str, github_username: str, days: int = 90) -> Dict[str, int]:
    """Return review + comment counts this person has left on others' PRs in the last N days.

    Keys: approvals, changes_requested, review_comments, pr_comments.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    try:
        cursor.execute(
            """
            SELECT
                SUM(CASE WHEN state = 'APPROVED' THEN 1 ELSE 0 END) AS approvals,
                SUM(CASE WHEN state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) AS changes_requested,
                COALESCE(SUM(inline_comment_count), 0) AS review_comments
            FROM github_reviews
            WHERE reviewer_github_username = ?
              AND submitted_at >= ?
            """,
            (github_username, cutoff),
        )
        row = cursor.fetchone()
        approvals = (dict(row).get('approvals') or 0) if row else 0
        changes_requested = (dict(row).get('changes_requested') or 0) if row else 0
        review_comments = (dict(row).get('review_comments') or 0) if row else 0

        cursor.execute(
            """
            SELECT COUNT(*) AS cnt
            FROM github_pr_comments
            WHERE commenter_github_username = ?
              AND created_at >= ?
            """,
            (github_username, cutoff),
        )
        pr_comments = (dict(cursor.fetchone()).get('cnt') or 0)

        return {
            'approvals': approvals,
            'changes_requested': changes_requested,
            'review_comments': review_comments,
            'pr_comments': pr_comments,
        }
    finally:
        conn.close()


def get_team_velocity(db_path: str, sprint_id: int) -> float:
    """
    Get team velocity (total completed story points) for a sprint.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID

    Returns:
        Total completed story points
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT completed_story_points
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
        """, (sprint_id,))

        result = cursor.fetchone()
        return dict(result)['completed_story_points'] if result else 0.0
    finally:
        conn.close()


def get_developer_velocity(db_path: str, developer_id: str, num_sprints: int = 3) -> List[Dict[str, Any]]:
    """
    Get historical velocity for a developer across recent sprints.

    Args:
        db_path: Path to database
        developer_id: Developer's Jira account ID
        num_sprints: Number of recent sprints to include

    Returns:
        List of velocity records
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                dv.sprint_id,
                s.sprint_name,
                dv.completed_story_points,
                dv.total_tickets_completed,
                dv.calculated_at
            FROM developer_velocity dv
            JOIN sprints s ON dv.sprint_id = s.sprint_id
            WHERE dv.developer_id = ?
            ORDER BY s.start_date DESC
            LIMIT ?
        """, (developer_id, num_sprints))

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_team_cycle_time(db_path: str, sprint_id: int) -> Optional[float]:
    """
    Calculate average cycle time (In Progress -> Done) for the team in days.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID

    Returns:
        Average cycle time in days, or None if no data
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Try to get from status history first.
        # Status buckets come from utils/statuses.py so a workflow that goes
        # 'Testing in progress' → 'Done' (skipping 'In Progress') still gets
        # a started_at — the previous hardcoded list silently dropped those.
        cursor.execute(f"""
            SELECT
                ticket_key,
                MIN(CASE WHEN new_status IN ({_INPROG_PH})
                    THEN changed_at END) as started_at,
                MAX(CASE WHEN new_status IN ({_CLOSED_PH})
                    THEN changed_at END) as completed_at
            FROM ticket_status_history
            WHERE sprint_id = ?
            GROUP BY ticket_key
            HAVING started_at IS NOT NULL AND completed_at IS NOT NULL
        """, (*IN_PROGRESS_STATUSES, *CLOSED_STATUSES, sprint_id))

        rows = cursor.fetchall()

        if rows:
            cycle_times = []
            for row in rows:
                row_dict = dict(row)
                started = _parse_iso_tz(row_dict['started_at'])
                completed = _parse_iso_tz(row_dict['completed_at'])
                if not started or not completed:
                    continue
                days = _working_time_days(started, completed)
                if days > 0:
                    cycle_times.append(days)

            if cycle_times:
                return sum(cycle_times) / len(cycle_times)

        # Fallback: estimate from ticket timestamps
        cursor.execute(f"""
            SELECT
                ticket_key,
                created_at,
                updated_at
            FROM tickets
            WHERE sprint_id = ?
              AND status IN ({_CLOSED_PH})
              AND created_at IS NOT NULL
              AND updated_at IS NOT NULL
        """, (sprint_id, *CLOSED_STATUSES))

        rows = cursor.fetchall()
        if rows:
            times = []
            for row in rows:
                row_dict = dict(row)
                created = _parse_iso_tz(row_dict['created_at'])
                updated = _parse_iso_tz(row_dict['updated_at'])
                if not created or not updated:
                    continue
                days_open = _working_time_days(created, updated)
                if days_open > 0:
                    times.append(days_open)
            # Estimate: assume ~60% of time is actual work time
            if times:
                return round(sum(times) / len(times) * 0.6, 1)

        return None
    finally:
        conn.close()


def get_developer_cycle_per_point(db_path: str, sprint_id: int, developer_id: str) -> Optional[float]:
    """Return real avg days-per-point from ticket_status_history, or None
    if we don't have enough history for this developer yet.

    For each completed story with known SP:
      per_point = (completed_at - started_at_in_days) / story_points
    Then average across that developer's completed stories.

    Returns None when any of the following hold:
      - no ticket_status_history rows for this developer yet (brand-new collection)
      - no completed story has both a start and end transition recorded
      - every completed story has 0 SP
    Callers can fall back to a proxy (cycle_time / avg_sp_per_ticket).
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # Status sets come from utils/statuses.py — see get_team_cycle_time
        # for why the previous hardcoded list was undercounting.
        cursor.execute(f"""
            SELECT
                tsh.ticket_key,
                t.story_points,
                MIN(CASE WHEN tsh.new_status IN ({_INPROG_PH})
                    THEN tsh.changed_at END) AS started_at,
                MAX(CASE WHEN tsh.new_status IN ({_CLOSED_PH})
                    THEN tsh.changed_at END) AS completed_at
            FROM ticket_status_history tsh
            JOIN tickets t ON tsh.ticket_key = t.ticket_key AND tsh.sprint_id = t.sprint_id
            WHERE tsh.sprint_id = ?
              AND t.assignee_account_id = ?
              AND t.issue_type = 'Story'
            GROUP BY tsh.ticket_key, t.story_points
            HAVING started_at IS NOT NULL
               AND completed_at IS NOT NULL
               AND t.story_points IS NOT NULL
               AND t.story_points > 0
        """, (*IN_PROGRESS_STATUSES, *CLOSED_STATUSES, sprint_id, developer_id))

        per_point_values = []
        for row in cursor.fetchall():
            started = _parse_iso_tz(row['started_at'])
            finished = _parse_iso_tz(row['completed_at'])
            if not started or not finished:
                continue
            delta_days = _working_time_days(started, finished)
            if delta_days <= 0:
                continue
            per_point_values.append(delta_days / row['story_points'])

        if not per_point_values:
            return None
        return sum(per_point_values) / len(per_point_values)
    finally:
        conn.close()


def get_developer_cycle_time(db_path: str, sprint_id: int, developer_id: str) -> Optional[float]:
    """
    Calculate average cycle time for a specific developer in days.

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        developer_id: Developer's Jira account ID

    Returns:
        Average cycle time in days, or None if no data
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Try status history first
        cursor.execute(f"""
            SELECT
                tsh.ticket_key,
                MIN(CASE WHEN new_status IN ({_INPROG_PH})
                    THEN changed_at END) as started_at,
                MAX(CASE WHEN new_status IN ({_CLOSED_PH})
                    THEN changed_at END) as completed_at
            FROM ticket_status_history tsh
            JOIN tickets t ON tsh.ticket_key = t.ticket_key AND tsh.sprint_id = t.sprint_id
            WHERE tsh.sprint_id = ? AND t.assignee_account_id = ?
            GROUP BY tsh.ticket_key
            HAVING started_at IS NOT NULL AND completed_at IS NOT NULL
        """, (*IN_PROGRESS_STATUSES, *CLOSED_STATUSES, sprint_id, developer_id))

        rows = cursor.fetchall()

        if rows:
            cycle_times = []
            for row in rows:
                row_dict = dict(row)
                started = _parse_iso_tz(row_dict['started_at'])
                completed = _parse_iso_tz(row_dict['completed_at'])
                if not started or not completed:
                    continue
                days = _working_time_days(started, completed)
                if days > 0:
                    cycle_times.append(days)

            if cycle_times:
                return sum(cycle_times) / len(cycle_times)

        # Fallback: estimate from completed tickets
        cursor.execute(f"""
            SELECT
                ticket_key,
                created_at,
                updated_at
            FROM tickets
            WHERE sprint_id = ?
              AND assignee_account_id = ?
              AND status IN ({_CLOSED_PH})
              AND created_at IS NOT NULL
              AND updated_at IS NOT NULL
        """, (sprint_id, developer_id, *CLOSED_STATUSES))

        rows = cursor.fetchall()
        if rows:
            times = []
            for row in rows:
                row_dict = dict(row)
                created = _parse_iso_tz(row_dict['created_at'])
                updated = _parse_iso_tz(row_dict['updated_at'])
                if not created or not updated:
                    continue
                days_open = _working_time_days(created, updated)
                if days_open > 0:
                    times.append(days_open)
            if times:
                return round(sum(times) / len(times) * 0.6, 1)

        return None
    finally:
        conn.close()


def get_team_throughput(db_path: str, sprint_id: int, days: int = 7) -> float:
    """
    Calculate team throughput (tickets completed per week).

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        days: Number of days to calculate over (default 7)

    Returns:
        Tickets per period
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Try to get sprint start date
        cursor.execute("""
            SELECT start_date,
                   (julianday('now') - julianday(start_date)) as elapsed_days
            FROM sprints
            WHERE sprint_id = ? AND start_date IS NOT NULL
        """, (sprint_id,))

        result = cursor.fetchone()
        elapsed = None

        if result:
            elapsed = dict(result)['elapsed_days']

        # Fallback: use snapshot history to calculate elapsed days
        if not elapsed or elapsed <= 0:
            cursor.execute("""
                SELECT
                    (julianday(MAX(snapshot_date)) - julianday(MIN(snapshot_date))) as elapsed_days
                FROM sprint_snapshots
                WHERE sprint_id = ?
            """, (sprint_id,))

            result = cursor.fetchone()
            if result:
                elapsed = dict(result)['elapsed_days']

        # If still no elapsed time, return 0
        if not elapsed or elapsed <= 0:
            # Last resort: just count completed tickets (no rate)
            cursor.execute(f"""
                SELECT COUNT(*) as completed
                FROM tickets
                WHERE sprint_id = ? AND status IN ({_CLOSED_PH})
            """, (sprint_id, *CLOSED_STATUSES))

            completed = dict(cursor.fetchone())['completed']
            return float(completed) if completed > 0 else 0.0

        # Get completed tickets
        cursor.execute(f"""
            SELECT COUNT(*) as completed
            FROM tickets
            WHERE sprint_id = ? AND status IN ({_CLOSED_PH})
        """, (sprint_id, *CLOSED_STATUSES))

        completed = dict(cursor.fetchone())['completed']

        # Calculate throughput normalized to the period
        throughput = (completed / elapsed) * days if elapsed > 0 else 0
        return round(throughput, 1)
    finally:
        conn.close()


def get_developer_throughput(db_path: str, sprint_id: int, developer_id: str, days: int = 7) -> float:
    """
    Calculate developer throughput (tickets completed per week).

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        developer_id: Developer's Jira account ID
        days: Number of days to calculate over (default 7)

    Returns:
        Tickets per period
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Try sprint start date first
        cursor.execute("""
            SELECT start_date,
                   (julianday('now') - julianday(start_date)) as elapsed_days
            FROM sprints
            WHERE sprint_id = ? AND start_date IS NOT NULL
        """, (sprint_id,))

        result = cursor.fetchone()
        elapsed = None

        if result:
            elapsed = dict(result)['elapsed_days']

        # Fallback: use snapshot history
        if not elapsed or elapsed <= 0:
            cursor.execute("""
                SELECT
                    (julianday(MAX(snapshot_date)) - julianday(MIN(snapshot_date))) as elapsed_days
                FROM sprint_snapshots
                WHERE sprint_id = ?
            """, (sprint_id,))

            result = cursor.fetchone()
            if result:
                elapsed = dict(result)['elapsed_days']

        # If no elapsed time, just return count
        if not elapsed or elapsed <= 0:
            cursor.execute(f"""
                SELECT COUNT(*) as completed
                FROM tickets
                WHERE sprint_id = ?
                  AND assignee_account_id = ?
                  AND status IN ({_CLOSED_PH})
            """, (sprint_id, developer_id, *CLOSED_STATUSES))

            completed = dict(cursor.fetchone())['completed']
            return float(completed) if completed > 0 else 0.0

        cursor.execute(f"""
            SELECT COUNT(*) as completed
            FROM tickets
            WHERE sprint_id = ?
              AND assignee_account_id = ?
              AND status IN ({_CLOSED_PH})
        """, (sprint_id, developer_id, *CLOSED_STATUSES))

        completed = dict(cursor.fetchone())['completed']
        throughput = (completed / elapsed) * days if elapsed > 0 else 0
        return round(throughput, 1)
    finally:
        conn.close()


def get_team_pr_review_time(db_path: str, days: int = 30) -> Optional[float]:
    """
    Calculate average PR review turnaround time for the team in hours.

    Args:
        db_path: Path to database
        days: Number of days to look back

    Returns:
        Average hours from creation to merge, or None if no data
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT created_at, merged_at
            FROM github_prs
            WHERE state = 'merged'
              AND merged_at >= ?
              AND created_at IS NOT NULL
              AND merged_at IS NOT NULL
        """, (cutoff_date,))

        hours = []
        for row in cursor.fetchall():
            created = _parse_iso_tz(row['created_at'])
            merged = _parse_iso_tz(row['merged_at'])
            if not created or not merged:
                continue
            wd_hours = _working_time_days(created, merged) * 24
            if wd_hours > 0:
                hours.append(wd_hours)

        if not hours:
            return None
        return round(sum(hours) / len(hours), 1)
    finally:
        conn.close()


def get_pr_approvals_by_developer(db_path: str, days: int = 30) -> List[Dict[str, Any]]:
    """
    Count PR approvals/reviews per developer.
    Note: This currently counts PRs created. PR review data would need separate collection.

    Args:
        db_path: Path to database
        days: Number of days to look back

    Returns:
        List of developers with their PR counts
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT
                author_github_username,
                COUNT(*) as pr_count,
                SUM(CASE WHEN state = 'merged' THEN 1 ELSE 0 END) as merged_count,
                AVG(
                    CASE WHEN state = 'merged' AND merged_at IS NOT NULL AND created_at IS NOT NULL
                    THEN (julianday(merged_at) - julianday(created_at)) * 24
                    ELSE NULL END
                ) as avg_hours_to_merge
            FROM github_prs
            WHERE created_at >= ?
            GROUP BY author_github_username
            ORDER BY pr_count DESC
        """, (cutoff_date,))

        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def get_sprint_commitment_accuracy(db_path: str, sprint_id: int) -> Dict[str, Any]:
    """
    Calculate sprint commitment accuracy (planned vs completed).

    Args:
        db_path: Path to database
        sprint_id: Sprint ID

    Returns:
        Dict with planned, completed, and accuracy percentage
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Get total tickets at sprint start (or first snapshot)
        cursor.execute("""
            SELECT total_tickets, closed_tickets
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_timestamp ASC
            LIMIT 1
        """, (sprint_id,))

        first_snapshot = cursor.fetchone()
        if not first_snapshot:
            return {'planned': 0, 'completed': 0, 'accuracy': 0}

        first = dict(first_snapshot)
        planned = first['total_tickets']

        # Get current completed count
        cursor.execute("""
            SELECT closed_tickets
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_timestamp DESC
            LIMIT 1
        """, (sprint_id,))

        latest_snapshot = cursor.fetchone()
        completed = dict(latest_snapshot)['closed_tickets'] if latest_snapshot else 0

        accuracy = (completed / planned * 100) if planned > 0 else 0

        return {
            'planned': planned,
            'completed': completed,
            'accuracy': round(accuracy, 1)
        }
    finally:
        conn.close()


def get_pr_size_distribution(db_path: str, days: int = 30) -> Dict[str, int]:
    """
    Get distribution of PR sizes (lines changed).

    Args:
        db_path: Path to database
        days: Number of days to look back

    Returns:
        Dict with size buckets and counts
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT lines_added, lines_deleted
            FROM github_prs
            WHERE state = 'merged' AND merged_at >= ?
              AND lines_added IS NOT NULL AND lines_deleted IS NOT NULL
        """, (cutoff_date,))

        distribution = {
            'xs': 0,  # <50 lines
            's': 0,   # 50-200 lines
            'm': 0,   # 200-400 lines
            'l': 0,   # 400-800 lines
            'xl': 0   # >800 lines
        }

        for row in cursor.fetchall():
            row_dict = dict(row)
            total_lines = row_dict['lines_added'] + row_dict['lines_deleted']

            if total_lines < 50:
                distribution['xs'] += 1
            elif total_lines < 200:
                distribution['s'] += 1
            elif total_lines < 400:
                distribution['m'] += 1
            elif total_lines < 800:
                distribution['l'] += 1
            else:
                distribution['xl'] += 1

        return distribution
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk variants — used by generate_team_members_html to avoid 7×N query loops.
# Each takes the sprint context once, runs a single grouped query, and returns
# a dict keyed by developer_id / github_username / developer_name so callers
# can look up per-person values in O(1).
# ---------------------------------------------------------------------------

def get_developer_tickets_bulk(db_path: str, sprint_id: int) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Return tickets grouped by (developer_id → status → list[ticket]).

    Mirrors get_developer_tickets but in a single query for the whole sprint.
    Hardcoded statuses replaced with EXCLUDED_STATUSES from utils.statuses.
    """
    from utils.statuses import EXCLUDED_STATUSES, sql_placeholders
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        excl_ph = sql_placeholders(EXCLUDED_STATUSES)
        cursor.execute(f"""
            SELECT
                assignee_account_id,
                ticket_key, summary, status, story_points,
                issue_type, priority, ticket_url, updated_at
            FROM tickets
            WHERE sprint_id = ?
              AND assignee_account_id IS NOT NULL
              AND issue_type IN ('Story', 'Task', 'Bug', 'Sub-task', 'Subtask')
              AND status NOT IN ({excl_ph})
            ORDER BY status, updated_at DESC
        """, (sprint_id, *EXCLUDED_STATUSES))

        out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        for row in cursor.fetchall():
            d = dict(row)
            dev_id = d.pop('assignee_account_id')
            status = d['status']
            out.setdefault(dev_id, {}).setdefault(status, []).append(d)
        return out
    finally:
        conn.close()


def get_pr_metrics_bulk(db_path: str, github_usernames: List[str], days: int = 30) -> Dict[str, Dict[str, Any]]:
    """Return {github_username → pr_metrics} in two queries instead of 2×N."""
    if not github_usernames:
        return {}
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        ph = ",".join("?" for _ in github_usernames)
        # Latest open PR snapshot per developer.
        cursor.execute(f"""
            SELECT s.developer_github_username, s.open_pr_count
            FROM github_pr_snapshots s
            JOIN (
                SELECT developer_github_username, MAX(snapshot_timestamp) AS latest_ts
                FROM github_pr_snapshots
                WHERE developer_github_username IN ({ph})
                GROUP BY developer_github_username
            ) latest
              ON latest.developer_github_username = s.developer_github_username
             AND latest.latest_ts = s.snapshot_timestamp
        """, github_usernames)
        open_counts = {row['developer_github_username']: row['open_pr_count'] for row in cursor.fetchall()}

        # Recent merged PRs in the window — compute merge times in Python.
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute(f"""
            SELECT author_github_username, created_at, merged_at
            FROM github_prs
            WHERE author_github_username IN ({ph})
              AND state = 'merged'
              AND merged_at >= ?
              AND created_at IS NOT NULL
              AND merged_at IS NOT NULL
        """, (*github_usernames, cutoff))

        merge_buckets: Dict[str, List[float]] = {u: [] for u in github_usernames}
        for row in cursor.fetchall():
            created = _parse_iso_tz(row['created_at'])
            merged = _parse_iso_tz(row['merged_at'])
            if not created or not merged:
                continue
            wd_hours = _working_time_days(created, merged) * 24
            if wd_hours > 0:
                merge_buckets[row['author_github_username']].append(wd_hours)

        result: Dict[str, Dict[str, Any]] = {}
        for u in github_usernames:
            hrs = merge_buckets[u]
            avg = (sum(hrs) / len(hrs)) if hrs else None
            result[u] = {
                'github_username': u,
                'open_pr_count': open_counts.get(u, 0),
                'avg_hours_to_merge': round(avg, 1) if avg else None,
                'merged_pr_count_last_n_days': len(hrs),
            }
        return result
    finally:
        conn.close()


def get_review_metrics_bulk(db_path: str, github_usernames: List[str], days: int = 90) -> Dict[str, Dict[str, int]]:
    """Return {github_username → review_metrics} in two queries instead of 2×N."""
    if not github_usernames:
        return {}
    conn = get_connection(db_path)
    cursor = conn.cursor()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    try:
        ph = ",".join("?" for _ in github_usernames)
        cursor.execute(f"""
            SELECT
                reviewer_github_username,
                SUM(CASE WHEN state = 'APPROVED' THEN 1 ELSE 0 END) AS approvals,
                SUM(CASE WHEN state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) AS changes_requested,
                COALESCE(SUM(inline_comment_count), 0) AS review_comments
            FROM github_reviews
            WHERE reviewer_github_username IN ({ph})
              AND submitted_at >= ?
            GROUP BY reviewer_github_username
        """, (*github_usernames, cutoff))
        reviews = {r['reviewer_github_username']: dict(r) for r in cursor.fetchall()}

        cursor.execute(f"""
            SELECT commenter_github_username, COUNT(*) AS cnt
            FROM github_pr_comments
            WHERE commenter_github_username IN ({ph})
              AND created_at >= ?
            GROUP BY commenter_github_username
        """, (*github_usernames, cutoff))
        comments = {r['commenter_github_username']: r['cnt'] for r in cursor.fetchall()}

        result = {}
        for u in github_usernames:
            row = reviews.get(u, {})
            result[u] = {
                'approvals': int(row.get('approvals') or 0),
                'changes_requested': int(row.get('changes_requested') or 0),
                'review_comments': int(row.get('review_comments') or 0),
                'pr_comments': int(comments.get(u, 0)),
            }
        return result
    finally:
        conn.close()


def _bulk_cycle_data(db_path: str, sprint_id: int) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch ticket-status-history rows for every developer in the sprint at once.

    Returns {developer_id → [{ticket_key, story_points, started_at, completed_at}]}.
    Used by both bulk cycle-time and bulk cycle-per-point.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            SELECT
                t.assignee_account_id AS developer_id,
                tsh.ticket_key,
                t.story_points,
                t.issue_type,
                MIN(CASE WHEN tsh.new_status IN ({_INPROG_PH})
                    THEN tsh.changed_at END) AS started_at,
                MAX(CASE WHEN tsh.new_status IN ({_CLOSED_PH})
                    THEN tsh.changed_at END) AS completed_at
            FROM ticket_status_history tsh
            JOIN tickets t
              ON tsh.ticket_key = t.ticket_key
             AND tsh.sprint_id  = t.sprint_id
            WHERE tsh.sprint_id = ?
              AND t.assignee_account_id IS NOT NULL
            GROUP BY t.assignee_account_id, tsh.ticket_key, t.story_points, t.issue_type
        """, (*IN_PROGRESS_STATUSES, *CLOSED_STATUSES, sprint_id))
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in cursor.fetchall():
            d = dict(row)
            out.setdefault(d['developer_id'], []).append(d)
        return out
    finally:
        conn.close()


def get_developer_cycle_time_bulk(db_path: str, sprint_id: int) -> Dict[str, Optional[float]]:
    """Return {developer_id → avg cycle days} from one query."""
    by_dev = _bulk_cycle_data(db_path, sprint_id)
    out: Dict[str, Optional[float]] = {}
    for dev_id, rows in by_dev.items():
        cycles = []
        for r in rows:
            started = _parse_iso_tz(r['started_at'])
            completed = _parse_iso_tz(r['completed_at'])
            if not started or not completed:
                continue
            d = _working_time_days(started, completed)
            if d > 0:
                cycles.append(d)
        out[dev_id] = (sum(cycles) / len(cycles)) if cycles else None
    return out


def get_developer_cycle_per_point_bulk(db_path: str, sprint_id: int) -> Dict[str, Optional[float]]:
    """Return {developer_id → avg days/point} from one query (Stories with SP only)."""
    by_dev = _bulk_cycle_data(db_path, sprint_id)
    out: Dict[str, Optional[float]] = {}
    for dev_id, rows in by_dev.items():
        per_point = []
        for r in rows:
            if r.get('issue_type') != 'Story' or not r.get('story_points') or r['story_points'] <= 0:
                continue
            started = _parse_iso_tz(r['started_at'])
            completed = _parse_iso_tz(r['completed_at'])
            if not started or not completed:
                continue
            d = _working_time_days(started, completed)
            if d > 0:
                per_point.append(d / r['story_points'])
        out[dev_id] = (sum(per_point) / len(per_point)) if per_point else None
    return out


def get_developer_throughput_bulk(db_path: str, sprint_id: int, days: int = 7) -> Dict[str, float]:
    """Return {developer_id → throughput per `days` window} in one query."""
    from utils.statuses import CLOSED_STATUSES, sql_placeholders
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        # Sprint elapsed (working-day-agnostic; matches the existing function).
        cursor.execute("""
            SELECT julianday('now') - julianday(start_date) AS elapsed_days
            FROM sprints WHERE sprint_id = ? AND start_date IS NOT NULL
        """, (sprint_id,))
        row = cursor.fetchone()
        elapsed = (dict(row).get('elapsed_days') if row else None) or 0

        if elapsed <= 0:
            cursor.execute("""
                SELECT julianday(MAX(snapshot_date)) - julianday(MIN(snapshot_date)) AS elapsed_days
                FROM sprint_snapshots WHERE sprint_id = ?
            """, (sprint_id,))
            row = cursor.fetchone()
            elapsed = (dict(row).get('elapsed_days') if row else None) or 0

        closed_ph = sql_placeholders(CLOSED_STATUSES)
        cursor.execute(f"""
            SELECT assignee_account_id, COUNT(*) AS completed
            FROM tickets
            WHERE sprint_id = ?
              AND assignee_account_id IS NOT NULL
              AND status IN ({closed_ph})
            GROUP BY assignee_account_id
        """, (sprint_id, *CLOSED_STATUSES))
        completed_per_dev = {row['assignee_account_id']: row['completed'] for row in cursor.fetchall()}

        out: Dict[str, float] = {}
        for dev_id, completed in completed_per_dev.items():
            if elapsed > 0:
                out[dev_id] = round((completed / elapsed) * days, 1)
            else:
                out[dev_id] = float(completed) if completed > 0 else 0.0
        return out
    finally:
        conn.close()


def get_one_on_one_meetings_bulk(db_path: str) -> Dict[str, Dict[str, Any]]:
    """Return {developer_name → meeting_dict} for every recorded 1-on-1."""
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT developer_name, summary, day_of_week, time_of_day,
                   duration_minutes, next_occurrence, last_synced_at
            FROM one_on_one_meetings
        """)
        return {row['developer_name']: dict(row) for row in cursor.fetchall()}
    finally:
        conn.close()


def get_one_on_one_meeting(db_path: str, developer_name: str) -> Optional[Dict[str, Any]]:
    """
    Get 1-on-1 meeting details for a developer.

    Args:
        db_path: Path to database
        developer_name: Developer's name

    Returns:
        Dict with meeting details, or None if no meeting found
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            SELECT
                summary,
                day_of_week,
                time_of_day,
                duration_minutes,
                next_occurrence,
                last_synced_at
            FROM one_on_one_meetings
            WHERE developer_name = ?
        """, (developer_name,))

        result = cursor.fetchone()
        return dict(result) if result else None
    finally:
        conn.close()
