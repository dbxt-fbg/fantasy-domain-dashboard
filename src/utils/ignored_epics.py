"""Single source of truth for "don't surface this epic anywhere" suppression.

A handful of FNTSY epics exist for org/onboarding bookkeeping (each new hire
gets a personal onboarding epic, etc.) and aren't real product work. They
were originally hidden only from the hygiene agent. This module extends that
suppression to every other page that lists epics, so a single edit to
`config/team_config.yaml` removes them everywhere instead of needing 5+ pages
to coordinate.

Config shape (lives under `hygiene:` for legacy reasons; we'll accept either
location so existing edits keep working):

    hygiene:
      ignored_epics:
        - FNTSY-128
        - FNTSY-368
        ...

Or — preferred for new entries — at the top level:

    ignored_epics:
      - FNTSY-128
      ...

Use:
    from utils.ignored_epics import load_ignored_epics, filter_ignored
    ignored = load_ignored_epics(config)
    visible_epics = filter_ignored(rows, ignored)

Both forms can coexist; the helper unions them.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence


def load_ignored_epics(config: dict) -> set[str]:
    """Read the ignored-epics set from a loaded config dict.

    Looks in two places (top-level `ignored_epics` and `hygiene.ignored_epics`)
    and returns the union, normalised to upper-case strings. Returns an empty
    set if neither key is present.
    """
    out: set[str] = set()
    for value in (
        (config or {}).get('ignored_epics') or [],
        ((config or {}).get('hygiene') or {}).get('ignored_epics') or [],
    ):
        for key in value or []:
            if isinstance(key, str) and key.strip():
                out.add(key.strip().upper())
    return out


def filter_ignored(
    rows: Iterable[Any],
    ignored: Sequence[str] | set[str],
    *,
    key_field: str = 'ticket_key',
) -> list[Any]:
    """Drop any row whose `key_field` value is in the ignored set.

    Handles both dict-shaped rows and sqlite3.Row by indexing with `[]`.
    Tickets with the `_s<sprint_id>` suffix (epics duplicated across sprints
    by refresh_jira_data) are matched on their bare key, so adding
    "FNTSY-368" to the ignored list also suppresses "FNTSY-368_s21193".
    Returns a new list — never mutates the input.
    """
    if not ignored:
        return list(rows)
    blocked = {k.upper() for k in ignored}
    out = []
    for row in rows:
        try:
            raw = row[key_field]
        except (KeyError, IndexError, TypeError):
            out.append(row)
            continue
        if not raw:
            out.append(row)
            continue
        # Strip any "_s<sprint_id>" cross-sprint suffix before comparing.
        bare = raw.split('_s', 1)[0].upper()
        if bare in blocked:
            continue
        out.append(row)
    return out


def is_ignored(ticket_key: str, ignored: Sequence[str] | set[str]) -> bool:
    """True if `ticket_key` (with or without `_s<sprint_id>` suffix) is hidden."""
    if not ticket_key or not ignored:
        return False
    blocked = {k.upper() for k in ignored}
    bare = ticket_key.split('_s', 1)[0].upper()
    return bare in blocked
