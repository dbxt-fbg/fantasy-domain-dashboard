"""Single source of truth for Jira status buckets.

Several code paths need to group ticket statuses into the same semantic
buckets (closed / in-progress / open / excluded). Keeping those lists
defined once here avoids drift like the earlier Testing-in-progress and
Waiting-for-Customer bugs where one file caught them and another didn't.
"""

from __future__ import annotations

from typing import Iterable, Tuple


# Tickets whose work is done. Counts toward velocity.
CLOSED_STATUSES: Tuple[str, ...] = (
    'Done',
    'Closed',
    'Resolved',
)

# Work is in flight. Counts toward WIP.
# Note: "Ready for Testing" and "Released to Test" are intentionally here — the
# work isn't shippable yet, so treating them as open/backlog (as the snapshot
# summaries previously did) understates in-flight work.
IN_PROGRESS_STATUSES: Tuple[str, ...] = (
    'In Progress',
    'In Development',
    'In Review',
    'In code review',
    'Blocked',
    'Testing in progress',
    'Ready for Testing',
    'Released to Test',
    'Ready for Prod Deployment',
    'Waiting for Customer',
)

# Not yet started. Counts toward backlog / remaining work.
OPEN_STATUSES: Tuple[str, ...] = (
    'To Do',
    'Open',
    'Backlog',
    'Selected for Development',
)

# Dropped from the sprint; never counted.
EXCLUDED_STATUSES: Tuple[str, ...] = (
    'Abandoned',
    'Duplicate',
)


# All statuses the QA agent recognizes. Anything outside this set becomes an
# info-level "unknown status" issue so we catch new workflow states early.
KNOWN_STATUSES: Tuple[str, ...] = (
    CLOSED_STATUSES + IN_PROGRESS_STATUSES + OPEN_STATUSES + EXCLUDED_STATUSES
)


def bucket_for(status: str) -> str:
    """Return 'closed' | 'in_progress' | 'open' | 'excluded' | 'unknown'."""
    if status in CLOSED_STATUSES:
        return 'closed'
    if status in IN_PROGRESS_STATUSES:
        return 'in_progress'
    if status in OPEN_STATUSES:
        return 'open'
    if status in EXCLUDED_STATUSES:
        return 'excluded'
    return 'unknown'


def sql_placeholders(statuses: Iterable[str]) -> str:
    """Return a ','-joined '?' string matching the statuses iterable length."""
    return ','.join('?' for _ in statuses)
