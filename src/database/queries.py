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


# --- Delivery-excellence classification ------------------------------------
# Flow efficiency splits in-flight time into ACTIVE work vs. WAITING in a
# queue. A status is "active" when someone is hands-on-keyboard; "wait" when
# the ticket sits in a queue/handoff/blocked. Both are in-flight (a ticket
# only accrues flow time once it leaves the backlog), so this partitions
# IN_PROGRESS_STATUSES — open/closed/excluded states never accrue flow time.
ACTIVE_STATUSES: tuple = (
    'In Progress',
    'In Development',
    'In Review',          # human reviewing == active
    'In code review',
    'Testing in progress',
)
WAIT_STATUSES: tuple = (
    'Blocked',
    'Ready for Testing',        # done coding, queued for QA
    'Released to Test',
    'Ready for Prod Deployment',
    'Waiting for Customer',
)

# Canonical forward order of the delivery pipeline. A transition to a status
# with a LOWER index than the one it came from is "backward" == rework
# (e.g. In code review -> In Progress, Ready for Testing -> In Progress).
# Closed states sit at the end; backlog/open at the start.
_PIPELINE_ORDER = {
    'To Do': 0, 'Open': 0, 'Backlog': 0, 'Selected for Development': 0,
    'In Progress': 1, 'In Development': 1,
    'In code review': 2, 'In Review': 2,
    'Ready for Testing': 3,
    'Testing in progress': 4,
    'Released to Test': 5,
    'Ready for Prod Deployment': 6,
    'Released to Cert': 6,
    'Done': 7, 'Closed': 7, 'Resolved': 7, 'Released to Prod': 7,
}


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


def _is_working_day(d) -> bool:
    """Mon-Fri. Saturday = 5, Sunday = 6 in Python's weekday()."""
    return d.weekday() < 5


def working_days_between(start, end) -> int:
    """Count working days (Mon-Fri) in the inclusive range [start, end].

    Accepts date or datetime; 0 if end < start. Mirrors the generator's
    _working_days_between so capacity math is consistent across the codebase.
    """
    if end < start:
        return 0
    days = 0
    cur = start
    while cur <= end:
        if _is_working_day(cur):
            days += 1
        cur += timedelta(days=1)
    return days


def _overlap_working_days(span_start, span_end, win_start, win_end) -> int:
    """Working days of [span_start, span_end] that fall within [win_start, win_end]."""
    lo = max(span_start, win_start)
    hi = min(span_end, win_end)
    return working_days_between(lo, hi)


def get_pto_days_in_window(db_path: str, account_id: str,
                           win_start, win_end) -> int:
    """Working days of PTO a given engineer has within [win_start, win_end].

    Matches on jira_account_id. Sums per-span overlap with the window rather
    than trusting the stored day_count, since a span may only partially fall
    inside the window (e.g. a sprint boundary). Overlapping spans are clamped
    by date so a day off is never counted twice.

    win_start / win_end are date objects (inclusive). Returns 0 when there's
    no PTO, no table, or no account id.
    """
    if not account_id:
        return 0
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        try:
            cursor.execute(
                """
                SELECT start_date, end_date FROM pto
                 WHERE jira_account_id = ?
                   AND end_date >= ? AND start_date <= ?
                """,
                (account_id, win_start.isoformat(), win_end.isoformat()),
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            # pto table not present yet (pre-migration DB) — treat as no PTO.
            return 0
    finally:
        conn.close()

    # Collect the actual off-days into a set so overlapping spans don't
    # double-count. PTO volume per engineer is tiny, so this is cheap.
    off_days = set()
    for r in rows:
        s = parse_iso_tz(r['start_date']) or _date_from_iso(r['start_date'])
        e = parse_iso_tz(r['end_date']) or _date_from_iso(r['end_date'])
        if s is None or e is None:
            continue
        s = s.date() if isinstance(s, datetime) else s
        e = e.date() if isinstance(e, datetime) else e
        lo = max(s, win_start)
        hi = min(e, win_end)
        cur = lo
        while cur <= hi:
            if _is_working_day(cur):
                off_days.add(cur)
            cur += timedelta(days=1)
    return len(off_days)


def get_pto_spans_in_window(db_path: str, win_start, win_end) -> List[Dict[str, Any]]:
    """All PTO spans overlapping [win_start, win_end], for display.

    Returns dicts with developer_name, jira_account_id, start_date, end_date,
    summary, and working_days_in_window (the portion that lands in the window).
    Sorted by start date then name. Empty list when there's no PTO or no table.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        try:
            cursor.execute(
                """
                SELECT developer_name, jira_account_id, summary, start_date, end_date
                  FROM pto
                 WHERE end_date >= ? AND start_date <= ?
                 ORDER BY start_date, developer_name
                """,
                (win_start.isoformat(), win_end.isoformat()),
            )
            rows = [dict(r) for r in cursor.fetchall()]
        except sqlite3.OperationalError:
            return []
    finally:
        conn.close()

    out = []
    for r in rows:
        s = _date_from_iso(r['start_date'])
        e = _date_from_iso(r['end_date'])
        if s is None or e is None:
            continue
        r['working_days_in_window'] = _overlap_working_days(s, e, win_start, win_end)
        out.append(r)
    return out


def _date_from_iso(value: str):
    """Lenient YYYY-MM-DD -> date; None on failure."""
    try:
        from datetime import date as _date
        return _date.fromisoformat(value[:10])
    except (ValueError, TypeError):
        return None


def available_working_days(db_path: str, account_id: str,
                           win_start, win_end) -> int:
    """Working days an engineer is actually available in [win_start, win_end].

    = working_days_between(win_start, win_end) - PTO working days in window.
    Never negative. This is the single capacity primitive the dashboards build
    on: per-engineer sprint capacity, burndown slope, and velocity/throughput
    normalization all derive from it.
    """
    total = working_days_between(win_start, win_end)
    pto = get_pto_days_in_window(db_path, account_id, win_start, win_end)
    return max(0, total - pto)


def sprint_availability_factor(db_path: str, config: dict, sprint_id: int,
                               win_end_override=None) -> float:
    """Fraction of the team's working days actually available in a sprint.

    = sum(available_working_days) / sum(total_working_days) across every
    rostered engineer, over the sprint window (optionally clamped to
    win_end_override, e.g. 'today' for an in-flight sprint). 1.0 when there's
    no PTO, no roster, or no sprint dates — so capacity-normalized metrics
    equal their raw values until PTO data exists.

    This is the single multiplier Phase-4 normalization uses: a metric measured
    over a window where the team was at 80% availability is divided by 0.8 to
    express it per-available-capacity.
    """
    members = config.get('team_members', []) if config else []
    account_ids = [m.get('jira_account_id') for m in members if m.get('jira_account_id')]
    if not account_ids:
        return 1.0

    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT start_date, end_date FROM sprints WHERE sprint_id = ?",
            (sprint_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row or not row['start_date'] or not row['end_date']:
        return 1.0

    s = parse_iso_tz(row['start_date'])
    e = parse_iso_tz(row['end_date'])
    if not s or not e:
        return 1.0
    win_start, win_end = s.date(), e.date()
    if win_end_override is not None:
        wo = win_end_override.date() if isinstance(win_end_override, datetime) else win_end_override
        win_end = min(win_end, wo)

    total_wd = working_days_between(win_start, win_end)
    if total_wd <= 0:
        return 1.0
    full_capacity = total_wd * len(account_ids)
    available = sum(
        available_working_days(db_path, acct, win_start, win_end)
        for acct in account_ids
    )
    if full_capacity <= 0:
        return 1.0
    factor = available / full_capacity
    # Guard against absurd values; never amplify by more than ~3x downstream.
    return max(0.34, min(1.0, factor))


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


def get_team_throughput(db_path: str, sprint_id: int, days: int = 7,
                        config: dict = None) -> float:
    """
    Calculate team throughput (tickets completed per week).

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        days: Number of days to calculate over (default 7)
        config: Team roster. When supplied, throughput is expressed per
            AVAILABLE capacity — the rate is divided by the team's PTO-adjusted
            availability factor, so a week with half the team out reflects the
            true per-head pace rather than looking like a slowdown. Omit (or no
            PTO data) → factor 1.0 → unchanged.

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
    finally:
        conn.close()

    # Capacity-normalize: express per available head. Done outside the DB
    # block so the factor lookup uses its own connection.
    if config is not None:
        factor = sprint_availability_factor(
            db_path, config, sprint_id, win_end_override=datetime.now())
        if factor > 0:
            throughput = throughput / factor
    return round(throughput, 1)


def get_developer_throughput(db_path: str, sprint_id: int, developer_id: str,
                             days: int = 7, config: dict = None) -> float:
    """
    Calculate developer throughput (tickets completed per week).

    Args:
        db_path: Path to database
        sprint_id: Sprint ID
        developer_id: Developer's Jira account ID
        days: Number of days to calculate over (default 7)
        config: When supplied, normalize by THIS developer's PTO-adjusted
            availability so someone out part of the sprint isn't shown as slow.
            Omit (or no PTO) → unchanged.

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
    finally:
        conn.close()

    # Normalize by the individual's availability over the elapsed window.
    if config is not None:
        factor = _developer_availability_factor(
            db_path, sprint_id, developer_id, win_end_override=datetime.now())
        if factor > 0:
            throughput = throughput / factor
    return round(throughput, 1)


def _developer_availability_factor(db_path: str, sprint_id: int, account_id: str,
                                   win_end_override=None) -> float:
    """One engineer's available / total working days over a sprint window.

    1.0 when the engineer has no PTO, or the sprint has no dates. Mirrors
    sprint_availability_factor but for a single person (used for per-developer
    normalization). Clamped to [0.34, 1.0] to avoid runaway amplification.
    """
    if not account_id:
        return 1.0
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT start_date, end_date FROM sprints WHERE sprint_id = ?",
            (sprint_id,),
        )
        row = cursor.fetchone()
    finally:
        conn.close()
    if not row or not row['start_date'] or not row['end_date']:
        return 1.0
    s = parse_iso_tz(row['start_date'])
    e = parse_iso_tz(row['end_date'])
    if not s or not e:
        return 1.0
    win_start, win_end = s.date(), e.date()
    if win_end_override is not None:
        wo = win_end_override.date() if isinstance(win_end_override, datetime) else win_end_override
        win_end = min(win_end, wo)
    total_wd = working_days_between(win_start, win_end)
    if total_wd <= 0:
        return 1.0
    avail = available_working_days(db_path, account_id, win_start, win_end)
    return max(0.34, min(1.0, avail / total_wd))


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


def get_time_to_first_review(db_path: str, days: int = 30) -> Optional[Dict[str, Any]]:
    """Average working-hours from PR open to its first review, team-wide.

    "First review" = earliest github_reviews.submitted_at for a PR. Scoped to
    PRs merged within the window (so the sample is settled work, matching
    get_team_pr_review_time). Returns median too, since first-review latency
    is right-skewed and the mean alone overstates the typical wait.

    Returns None when no PR in the window has a recorded review.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        # One row per PR: its open time and the earliest review timestamp.
        cursor.execute("""
            SELECT p.created_at, MIN(r.submitted_at) AS first_review
            FROM github_prs p
            JOIN github_reviews r
              ON r.repository = p.repository AND r.pr_number = p.pr_number
            WHERE p.state = 'merged'
              AND p.merged_at >= ?
              AND p.created_at IS NOT NULL
            GROUP BY p.repository, p.pr_number
        """, (cutoff_date,))

        waits = []
        for row in cursor.fetchall():
            created = _parse_iso_tz(row['created_at'])
            first = _parse_iso_tz(row['first_review'])
            if not created or not first or first <= created:
                continue
            waits.append(_working_time_days(created, first) * 24)

        if not waits:
            return None
        waits.sort()
        n = len(waits)
        median = waits[n // 2] if n % 2 else (waits[n // 2 - 1] + waits[n // 2]) / 2
        return {
            'avg_hours': round(sum(waits) / n, 1),
            'median_hours': round(median, 1),
            'pr_count': n,
        }
    finally:
        conn.close()


def get_review_load_by_reviewer(db_path: str, days: int = 30) -> List[Dict[str, Any]]:
    """Per-reviewer review activity over the window, busiest first.

    Surfaces who carries review load — the counterpart to the existing
    author-side "PR Activity by Developer" table. Each row:
      reviewer, reviews (review submissions), approved, changes_requested,
      inline_comments, prs_reviewed (distinct PRs touched), share_pct
      (this reviewer's reviews as a % of all reviews in the window).

    A reviewer's multiple submissions on one PR each count toward `reviews`
    (that's genuine review effort); `prs_reviewed` dedupes to distinct PRs.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT
                reviewer_github_username AS reviewer,
                COUNT(*) AS reviews,
                SUM(CASE WHEN state = 'APPROVED' THEN 1 ELSE 0 END) AS approved,
                SUM(CASE WHEN state = 'CHANGES_REQUESTED' THEN 1 ELSE 0 END) AS changes_requested,
                SUM(inline_comment_count) AS inline_comments,
                COUNT(DISTINCT repository || '#' || pr_number) AS prs_reviewed
            FROM github_reviews
            WHERE submitted_at >= ?
            GROUP BY reviewer_github_username
            ORDER BY reviews DESC
        """, (cutoff_date,))
        rows = [dict(r) for r in cursor.fetchall()]
        total_reviews = sum(r['reviews'] for r in rows) or 1
        for r in rows:
            r['share_pct'] = round(r['reviews'] / total_reviews * 100, 1)
        return rows
    finally:
        conn.close()


# Status buckets that represent "waiting/blocked" time worth surfacing on its
# own. Kept narrow on purpose — these are states where a ticket is stalled on
# something external, not actively being worked.
_BLOCKED_STATUSES = ('Blocked', 'Waiting for Customer')


def get_time_in_status(db_path: str, days: int = 30) -> List[Dict[str, Any]]:
    """Average working-hours a ticket spends in each status, team-wide.

    Sourced from `status_changes` (which has a UNIQUE(ticket_key,status,
    entered_at) constraint, so its intervals are de-duplicated — unlike
    `ticket_status_history`, which re-inserts the same transition on every
    collector run). Only closed intervals (exited_at present) within the
    window are measured; an open interval has no duration yet.

    Returns a list ordered by the workflow pipeline, each:
      status, avg_hours, sample (closed intervals counted).
    Statuses with fewer than 3 samples are dropped — too few to average.
    """
    # Display order roughly follows the forward pipeline.
    order = [
        'Product Discovery', 'Engineering Unpacking', 'To Do', 'Committed',
        'In Progress', 'In Development', 'Blocked', 'In code review',
        'In Review', 'Ready for Testing', 'Testing in progress',
        'Released to Test', 'Ready for Prod Deployment',
    ]
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT status, entered_at, exited_at
            FROM status_changes
            WHERE exited_at IS NOT NULL
              AND entered_at >= ?
        """, (cutoff_date,))
        from collections import defaultdict
        durations: dict[str, list[float]] = defaultdict(list)
        for row in cursor.fetchall():
            entered = _parse_iso_tz(row['entered_at'])
            exited = _parse_iso_tz(row['exited_at'])
            if not entered or not exited or exited <= entered:
                continue
            durations[row['status']].append(_working_time_days(entered, exited) * 24)

        out = []
        for status in order:
            vals = durations.get(status, [])
            if len(vals) < 3:
                continue
            out.append({
                'status': status,
                'avg_hours': round(sum(vals) / len(vals), 1),
                'sample': len(vals),
            })
        return out
    finally:
        conn.close()


def get_status_churn(db_path: str, days: int = 30) -> Dict[str, Any]:
    """Count backward status transitions ("churn") team-wide in the window.

    Churn = a ticket moving BACKWARD in the pipeline: review/testing/done →
    back to in-progress/to-do. High churn signals unclear requirements or
    rework after review.

    Critical: `ticket_status_history` re-inserts the same transition on every
    collector run, so the raw table has ~80x duplicate rows. We DISTINCT on
    (ticket_key, old_status, new_status, changed_at) to recover the true
    distinct transition events before counting.

    Returns {total, review_bounces, reopens, by_transition: [...]}.
    """
    review_states = (
        'In Review', 'In code review', 'Testing in progress',
        'Ready for Testing', 'Released to Test',
    )
    done_states = CLOSED_STATUSES
    backward_to = ('To Do', 'Open', 'Backlog', 'In Progress', 'In Development')
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        # DISTINCT defuses the duplicate-row inflation; then count transitions.
        cursor.execute("""
            SELECT old_status, new_status, COUNT(*) AS c FROM (
                SELECT DISTINCT ticket_key, old_status, new_status, changed_at
                FROM ticket_status_history
                WHERE changed_at >= ?
            )
            GROUP BY old_status, new_status
        """, (cutoff_date,))
        review_bounces = 0   # review/testing -> back into dev/backlog
        reopens = 0          # done -> reopened
        by_transition = []
        for row in cursor.fetchall():
            old, new, c = row['old_status'], row['new_status'], row['c']
            if old in review_states and new in backward_to:
                review_bounces += c
                by_transition.append({'from': old, 'to': new, 'count': c})
            elif old in done_states and new not in done_states:
                reopens += c
                by_transition.append({'from': old, 'to': new, 'count': c})
        by_transition.sort(key=lambda x: -x['count'])
        return {
            'total': review_bounces + reopens,
            'review_bounces': review_bounces,
            'reopens': reopens,
            'by_transition': by_transition,
        }
    finally:
        conn.close()


def get_blocked_time(db_path: str, days: int = 30) -> Dict[str, Any]:
    """Total and average working-hours tickets spent Blocked/Waiting, team-wide.

    Sourced from `status_changes` (deduped intervals). Counts both closed
    blocked intervals (full duration) and currently-open ones (entered but
    not yet exited — measured up to now) so a ticket blocked right now still
    shows. Returns {total_hours, ticket_count, currently_blocked, avg_hours}.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        ph = sql_placeholders(_BLOCKED_STATUSES)
        cursor.execute(f"""
            SELECT ticket_key, status, entered_at, exited_at
            FROM status_changes
            WHERE status IN ({ph})
              AND entered_at >= ?
        """, (*_BLOCKED_STATUSES, cutoff_date))
        now = datetime.now()
        total_hours = 0.0
        tickets = set()
        currently_blocked = 0
        for row in cursor.fetchall():
            entered = _parse_iso_tz(row['entered_at'])
            if not entered:
                continue
            exited = _parse_iso_tz(row['exited_at']) if row['exited_at'] else None
            if exited is None:
                currently_blocked += 1
                # Match `now` tz-awareness to `entered` to avoid subtraction errors.
                end = now if entered.tzinfo is None else now.astimezone(entered.tzinfo)
            else:
                end = exited
            hrs = _working_time_days(entered, end) * 24
            if hrs > 0:
                total_hours += hrs
                tickets.add(row['ticket_key'])
        n = len(tickets)
        return {
            'total_hours': round(total_hours, 1),
            'ticket_count': n,
            'currently_blocked': currently_blocked,
            'avg_hours': round(total_hours / n, 1) if n else 0.0,
        }
    finally:
        conn.close()


def get_sprint_scope_change(db_path: str, sprint_id: int) -> Optional[Dict[str, Any]]:
    """Mid-sprint scope change: committed-at-start vs current/final total SP.

    Compares the FIRST snapshot's total_story_points (what was on the board
    when daily snapshotting began ~= sprint start) to the LATEST snapshot's
    total. A positive delta means scope was ADDED after planning; negative
    means work was removed/rescoped out.

    Returns None when the sprint has fewer than 2 snapshots (older sprints
    have a single backfilled row, so no start/end delta is computable — the
    caller should omit the indicator rather than show a misleading zero).

    Keys: start_sp, end_sp, delta_sp, start_tickets, end_tickets,
          delta_tickets, pct (delta as % of start).
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT total_story_points, total_tickets
            FROM sprint_snapshots
            WHERE sprint_id = ?
            ORDER BY snapshot_timestamp ASC
        """, (sprint_id,))
        rows = cursor.fetchall()
        if len(rows) < 2:
            return None
        first, last = rows[0], rows[-1]
        start_sp = first['total_story_points'] or 0
        end_sp = last['total_story_points'] or 0
        start_t = first['total_tickets'] or 0
        end_t = last['total_tickets'] or 0
        return {
            'start_sp': start_sp,
            'end_sp': end_sp,
            'delta_sp': round(end_sp - start_sp, 1),
            'start_tickets': start_t,
            'end_tickets': end_t,
            'delta_tickets': end_t - start_t,
            'pct': round((end_sp - start_sp) / start_sp * 100, 0) if start_sp > 0 else 0,
        }
    finally:
        conn.close()


def get_pr_size_vs_merge_time(db_path: str, days: int = 30) -> List[Dict[str, Any]]:
    """Average merge time (working hours) bucketed by PR size.

    Pairs the two halves the PR page already shows separately — size
    distribution and merge time — to demonstrate the size→latency relationship
    with the team's own data. Buckets match get_pr_size_distribution exactly
    (XS <50, S 50-200, M 200-400, L 400-800, XL >800 lines changed).

    Each row: bucket, label, count, avg_hours (None if no merged PRs in that
    bucket had a usable created→merged span).
    """
    buckets = [
        ('XS', '<50', 0, 50),
        ('S', '50-200', 50, 200),
        ('M', '200-400', 200, 400),
        ('L', '400-800', 400, 800),
        ('XL', '>800', 800, float('inf')),
    ]
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff_date = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute("""
            SELECT lines_added, lines_deleted, created_at, merged_at
            FROM github_prs
            WHERE state = 'merged' AND merged_at >= ?
              AND lines_added IS NOT NULL AND lines_deleted IS NOT NULL
              AND created_at IS NOT NULL AND merged_at IS NOT NULL
        """, (cutoff_date,))

        from collections import defaultdict
        sizes: dict[str, list[float]] = defaultdict(list)
        for row in cursor.fetchall():
            total_lines = (row['lines_added'] or 0) + (row['lines_deleted'] or 0)
            created = _parse_iso_tz(row['created_at'])
            merged = _parse_iso_tz(row['merged_at'])
            if not created or not merged:
                continue
            hrs = _working_time_days(created, merged) * 24
            if hrs <= 0:
                continue
            for code, _label, lo, hi in buckets:
                if lo <= total_lines < hi:
                    sizes[code].append(hrs)
                    break

        out = []
        for code, label, _lo, _hi in buckets:
            vals = sizes.get(code, [])
            out.append({
                'bucket': code,
                'label': label,
                'count': len(vals),
                'avg_hours': round(sum(vals) / len(vals), 1) if vals else None,
            })
        return out
    finally:
        conn.close()


def get_hygiene_aging_summary(db_path: str) -> Dict[str, Any]:
    """Aging rollup for currently-open hygiene issues.

    Uses first_seen_at (clean: set once when an issue is first detected) — NOT
    times_seen, which is a per-run increment counter and inflates to the
    thousands, so it can't be read as "distinct re-flags."

    Returns {open_count, oldest_days, aged_14d, aged_7d, avg_days} over rows
    where resolved_at IS NULL.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("""
            SELECT first_seen_at FROM hygiene_issues
            WHERE resolved_at IS NULL AND first_seen_at IS NOT NULL
        """)
        now = datetime.now()
        ages = []
        for row in cursor.fetchall():
            fs = _parse_iso_tz(row['first_seen_at'])
            if not fs:
                continue
            # first_seen_at is naive local; match now's awareness if needed.
            end = now if fs.tzinfo is None else now.astimezone(fs.tzinfo)
            days = max(0, (end - fs).days)
            ages.append(days)
        if not ages:
            return {'open_count': 0, 'oldest_days': 0, 'aged_14d': 0,
                    'aged_7d': 0, 'avg_days': 0}
        return {
            'open_count': len(ages),
            'oldest_days': max(ages),
            'aged_14d': sum(1 for d in ages if d >= 14),
            'aged_7d': sum(1 for d in ages if d >= 7),
            'avg_days': round(sum(ages) / len(ages), 1),
        }
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


def get_developer_throughput_bulk(db_path: str, sprint_id: int, days: int = 7,
                                  config: dict = None) -> Dict[str, float]:
    """Return {developer_id → throughput per `days` window} in one query.

    When `config` is supplied, each developer's rate is normalized by their own
    PTO-adjusted availability (consistent with get_developer_throughput). No
    PTO / no config → unchanged.
    """
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
                rate = (completed / elapsed) * days
            else:
                out[dev_id] = float(completed) if completed > 0 else 0.0
                continue
            if config is not None:
                factor = _developer_availability_factor(
                    db_path, sprint_id, dev_id, win_end_override=datetime.now())
                if factor > 0:
                    rate = rate / factor
            out[dev_id] = round(rate, 1)
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


# ===========================================================================
# Delivery-excellence metrics
#
# These measure HOW WELL the team delivers (flow, rework, predictability),
# not WHAT is done. They read the transition log (ticket_status_history),
# which stores discrete events (old_status, new_status, changed_at). To get
# durations we reconstruct each ticket's timeline: a status is "held" from
# its entry transition until the next transition for that ticket.
# ===========================================================================

def _reconstruct_timelines(rows: List[Dict[str, Any]]) -> Dict[str, list]:
    """Group transition rows into per-ticket [(status, entered, exited)] spans.

    `rows` must be ordered by (ticket_key, changed_at). Each ticket's status
    is held from one transition's changed_at to the next; the final status
    has no exit (still open) and is dropped from duration math by the caller.
    """
    spans: Dict[str, list] = {}
    by_ticket: Dict[str, list] = {}
    for r in rows:
        by_ticket.setdefault(r['ticket_key'], []).append(r)
    for key, events in by_ticket.items():
        seq = []
        for i, ev in enumerate(events):
            entered = parse_iso_tz(ev['changed_at'])
            if entered is None:
                continue
            exited = None
            if i + 1 < len(events):
                exited = parse_iso_tz(events[i + 1]['changed_at'])
            seq.append((ev['new_status'], entered, exited))
        if seq:
            spans[key] = seq
    return spans


def _ticket_role_map(cursor, name_to_role: Dict[str, str]) -> Dict[str, str]:
    """Map ticket_key → 'BE'|'FE'|'other' via assignee's roster role.

    Tickets whose assignee isn't a rostered BE/FE land in 'other' so totals
    always reconcile (same convention as _REPORTED_ROLES in the generator).
    Bare-keys the lookup so epic `_s<sprint>` suffixed rows still resolve.
    """
    cursor.execute("SELECT ticket_key, assignee_display_name FROM tickets")
    out: Dict[str, str] = {}
    for r in cursor.fetchall():
        out[r['ticket_key']] = name_to_role.get(r['assignee_display_name'] or '', 'other')
    return out


def _flow_from_spans(spans: Dict[str, list]) -> Dict[str, Any]:
    """Aggregate active/wait working-time across a set of reconstructed
    ticket timelines. Shared by the team-wide and per-role rollups."""
    active_total = 0.0
    wait_total = 0.0
    wait_by_status: Dict[str, float] = {}
    tickets_counted = 0
    for _key, seq in spans.items():
        t_active = 0.0
        t_wait = 0.0
        for status, entered, exited in seq:
            if exited is None:
                continue  # current/open status — no measured duration
            dur = _working_time_days(entered, exited)
            if dur <= 0:
                continue
            if status in ACTIVE_STATUSES:
                t_active += dur
            elif status in WAIT_STATUSES:
                t_wait += dur
                wait_by_status[status] = wait_by_status.get(status, 0.0) + dur
        if t_active + t_wait > 0:
            active_total += t_active
            wait_total += t_wait
            tickets_counted += 1

    denom = active_total + wait_total
    efficiency = (active_total / denom * 100) if denom > 0 else None
    top_queue = None
    if wait_by_status:
        name, val = max(wait_by_status.items(), key=lambda kv: kv[1])
        top_queue = {'status': name, 'days': round(val, 1)}
    return {
        'efficiency_pct': round(efficiency, 1) if efficiency is not None else None,
        'active_days': round(active_total, 1),
        'wait_days': round(wait_total, 1),
        'tickets': tickets_counted,
        'wait_by_status': {k: round(v, 1) for k, v in sorted(
            wait_by_status.items(), key=lambda kv: -kv[1])},
        'top_queue': top_queue,
    }


def get_flow_efficiency(db_path: str, days: int = 90,
                        name_to_role: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Flow efficiency = active time / (active + wait) time, team-wide.

    Reconstructs ticket timelines from the transition log over the recent
    `days` window, sums working-time in ACTIVE vs WAIT statuses, and returns
    the ratio plus the raw split and a per-status wait breakdown (so the page
    can show WHERE the waiting happens). Backlog/closed time is ignored — a
    ticket only accrues flow time while in flight.

    When `name_to_role` (display_name → 'BE'|'FE') is given, the result also
    carries a `by_role` dict ({'BE': {...}, 'FE': {...}, 'other': {...}})
    splitting the same spans by each ticket's assignee role.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        # ticket_status_history re-inserts the same transition on every
        # collector run (~55x duplicate rows), so DISTINCT on the logical
        # event before reconstructing — same defence as get_status_churn.
        cursor.execute(
            """
            SELECT DISTINCT ticket_key, new_status, changed_at
            FROM ticket_status_history
            WHERE changed_at >= ?
            ORDER BY ticket_key, changed_at
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        spans = _reconstruct_timelines(rows)

        result = _flow_from_spans(spans)
        result['window_days'] = days

        if name_to_role is not None:
            role_of = _ticket_role_map(cursor, name_to_role)
            by_role: Dict[str, Any] = {}
            for role in ('BE', 'FE', 'other'):
                sub = {k: v for k, v in spans.items() if role_of.get(k.split('_s', 1)[0], 'other') == role}
                by_role[role] = _flow_from_spans(sub)
            result['by_role'] = by_role
        return result
    finally:
        conn.close()


def _rework_from_rows(rows: list) -> Dict[str, Any]:
    """Compute rework rate + top backward hops from deduped transition rows.
    Shared by the team-wide and per-role rollups."""
    seen_tickets = set()
    rework_tickets = set()
    hop_counts: Dict[str, int] = {}
    for r in rows:
        old, new = r['old_status'], r['new_status']
        o_idx = _PIPELINE_ORDER.get(old)
        n_idx = _PIPELINE_ORDER.get(new)
        if o_idx is None or n_idx is None:
            continue
        seen_tickets.add(r['ticket_key'])
        if n_idx < o_idx:
            rework_tickets.add(r['ticket_key'])
            hop = f"{old} → {new}"
            hop_counts[hop] = hop_counts.get(hop, 0) + 1
    total = len(seen_tickets)
    rate = (len(rework_tickets) / total * 100) if total else None
    top_hops = sorted(hop_counts.items(), key=lambda kv: -kv[1])[:6]
    return {
        'rework_pct': round(rate, 1) if rate is not None else None,
        'rework_tickets': len(rework_tickets),
        'total_tickets': total,
        'top_hops': [{'hop': h, 'count': c} for h, c in top_hops],
    }


def get_rework_rate(db_path: str, days: int = 90,
                    name_to_role: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Rework rate = share of tickets that moved BACKWARD at least once.

    A backward transition is one whose destination sits earlier in the
    canonical pipeline than its origin (e.g. In code review -> In Progress,
    Ready for Testing -> Testing in progress). Pure quality signal: high
    rework = work bouncing back, a leading indicator of escaped defects.
    Returns the rate plus the most common backward hops so the page can name
    the bounce points. With `name_to_role`, also returns a `by_role` split.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        # DISTINCT defuses the duplicate-row inflation (see get_status_churn)
        # — otherwise hop counts are ~55x too high.
        cursor.execute(
            """
            SELECT DISTINCT ticket_key, old_status, new_status, changed_at
            FROM ticket_status_history
            WHERE changed_at >= ? AND old_status IS NOT NULL
            ORDER BY ticket_key, changed_at
            """,
            (cutoff,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

        result = _rework_from_rows(rows)
        result['window_days'] = days

        if name_to_role is not None:
            role_of = _ticket_role_map(cursor, name_to_role)
            by_role: Dict[str, Any] = {}
            for role in ('BE', 'FE', 'other'):
                sub = [r for r in rows if role_of.get(r['ticket_key'].split('_s', 1)[0], 'other') == role]
                by_role[role] = _rework_from_rows(sub)
            result['by_role'] = by_role
        return result
    finally:
        conn.close()


def get_predictability(db_path: str, sprint_prefix: str, num_sprints: int = 8,
                       config: dict = None) -> Dict[str, Any]:
    """Delivery predictability across recent CLOSED sprints.

    Two signals:
      * say/do — completed vs committed story count per sprint (uses the
        existing commitment-accuracy logic), averaged.
      * velocity stability — coefficient of variation (stdev/mean) of
        completed SP across sprints. LOWER is better; a steady team is more
        predictable than a high-but-erratic one.

    When `config` (the roster) is supplied, the velocity-stability signal uses
    CAPACITY-NORMALIZED completed SP — each sprint's completed SP is divided by
    that sprint's PTO-adjusted availability factor — so a sprint where the team
    was half-out reads as "lower capacity," not "erratic delivery." Say/Do is
    left as raw completed-vs-committed (it's already a self-normalizing ratio).
    No PTO / no config → unchanged. NOTE: enabling this changes the historical
    velocity_cov numbers shown on the Delivery page.
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            SELECT sprint_id, sprint_name
            FROM sprints
            WHERE sprint_name LIKE ? || '%'
              AND COALESCE(is_placeholder, 0) = 0
              AND state = 'closed'
            ORDER BY COALESCE(end_date, start_date) DESC
            LIMIT ?
            """,
            (sprint_prefix, num_sprints),
        )
        sprints = [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()

    rows = []
    for s in sprints:
        sid = s['sprint_id']
        commit = get_sprint_commitment_accuracy(db_path, sid)
        # Completed SP for the sprint, from the latest snapshot.
        vel = get_team_velocity(db_path, sid)
        # Capacity-normalized velocity for the stability signal: a low-
        # availability sprint produces less, which is expected, not erratic.
        norm_vel = vel or 0
        if config is not None and norm_vel:
            factor = sprint_availability_factor(db_path, config, sid)
            if factor > 0:
                norm_vel = norm_vel / factor
        # Sprint-track role from the trailing BE/FE suffix (M30.3 onward).
        name = s['sprint_name']
        role = 'BE' if name.rstrip().endswith(' BE') else 'FE' if name.rstrip().endswith(' FE') else 'none'
        rows.append({
            'sprint_id': sid,
            'sprint_name': name,
            'role': role,
            'planned': commit.get('planned', 0),
            'completed': commit.get('completed', 0),
            'accuracy': commit.get('accuracy', 0),
            'completed_sp': vel or 0,
            'completed_sp_norm': round(norm_vel, 1),
        })

    rows.reverse()  # chronological for charting

    def _rollup(subset: list) -> Dict[str, Any]:
        accuracies = [r['accuracy'] for r in subset if r['planned']]
        sps = [r['completed_sp'] for r in subset if r['completed_sp']]
        # Stability is measured on capacity-normalized velocity when available
        # (falls back to raw when no PTO/config, so the key is always present).
        sps_norm = [r.get('completed_sp_norm', r['completed_sp']) for r in subset if r['completed_sp']]
        say_do = round(sum(accuracies) / len(accuracies), 1) if accuracies else None
        cov = None
        if len(sps_norm) >= 2:
            mean = sum(sps_norm) / len(sps_norm)
            if mean > 0:
                var = sum((x - mean) ** 2 for x in sps_norm) / len(sps_norm)
                cov = round((var ** 0.5) / mean * 100, 1)
        return {
            'say_do_avg': say_do,
            'velocity_cov_pct': cov,  # lower = steadier (capacity-normalized)
            'velocity_mean': round(sum(sps) / len(sps), 1) if sps else None,
        }

    result = {'sprints': rows}
    result.update(_rollup(rows))
    # Per-track rollups — BE and FE run as separate sprints, so this is a
    # genuine track split (not a re-bucketing of the same sprints).
    result['by_role'] = {
        'BE': _rollup([r for r in rows if r['role'] == 'BE']),
        'FE': _rollup([r for r in rows if r['role'] == 'FE']),
    }
    return result
