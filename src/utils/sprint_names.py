"""Single source of truth for rendering Jira sprint names.

Every page in the dashboard references the same sprints, but the formatting
used to drift across pages — page headers showed `FNTSY M30.2 Sprint 2026.11`,
the Gantt header showed `M30.3 FE`, the line chart axis showed `M30.3`, the
Epics accordion showed the raw Jira name with the FNTSY prefix, etc. Same
underlying data, three+ different formats.

Terminology — strict definitions used throughout:

  * **milestone** — the integer after `M`. Example: in `M30.3` the
                    milestone is `30` and the rendered form is `M30`.
                    A milestone holds 4 sprint slots (.1 / .2 / .3 / .4).
  * **slot**      — one sprint slot within a milestone. Renders as
                    `M{milestone}.{slot}`. Example: `M30.3`.
  * **role**      — `FE` / `BE` suffix from M30.3 onward; `None` for the
                    pre-split slots (M30.1, M30.2).

This module exposes four formatters:

  * **long**       — full canonical name, matches the Jira sprint name.
                     Use in page headers, <title>, accordion summaries.
                     Example: "FNTSY M30.3 Sprint 2026.12 FE"
  * **short**      — slot + role only, no week, no FNTSY prefix.
                     Use in Gantt cells, dense tables, compact tooltips.
                     Example: "M30.3 FE"
  * **slot**       — slot only, no role, no week. (Was previously called
                     "milestone" — that name was wrong.)
                     Example: "M30.3"
  * **milestone**  — milestone only — for charts that roll up across all
                     four slots in a milestone.
                     Example: "M30"

Synthesised placeholder sprints (filled in by _build_full_sprint_sequence to
keep FE/BE pairs symmetric) end with "(TBD FE)" or "(TBD BE)" and we surface
that in long form so the user can see "we're projecting this slot, no Jira
ticket exists yet."
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Canonical Jira sprint format: "FNTSY M<milestone>.<slot> Sprint <week> [role]"
# Examples:
#   FNTSY M30.1 Sprint 2026.10
#   FNTSY M30.3 Sprint 2026.12 FE
#   FNTSY M31.2 Sprint 2026.15
# Synthesised placeholders skip the week token:
#   FNTSY M31.4 (TBD FE)
_SPRINT_RE = re.compile(
    r"""
    ^\s*
    (?:FNTSY\s+)?                       # optional project prefix
    M(?P<milestone>\d+)\.(?P<slot>\d+)  # M30.3
    (?:\s+Sprint)?                      # literal "Sprint" keyword (optional —
                                        # absent in the short_label form
                                        # "M30.3 2026.12 FE" produced by the
                                        # sequence builder)
    (?:\s+(?P<week>\d{4}\.\d+))?        # optional week token "2026.12"
    (?:\s+(?P<role>FE|BE))?              # optional trailing role
    (?:\s+\(TBD(?:\s+(?P<tbd_role>FE|BE))?\))?  # synthesised placeholder
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedSprintName:
    """Structured pieces of a Jira sprint name."""
    milestone: int           # 30 from "M30.3"
    slot: int                # 3 from "M30.3"
    week: Optional[str]      # "2026.12" or None for placeholders
    role: Optional[str]      # "FE" | "BE" | None
    is_placeholder: bool     # True if name contained "(TBD …)"

    @property
    def slot_label(self) -> str:
        """The "M30.3" portion — milestone + slot, no role."""
        return f"M{self.milestone}.{self.slot}"

    @property
    def milestone_label(self) -> str:
        """The "M30" portion — milestone only, no slot, no role."""
        return f"M{self.milestone}"

    # Backwards-compat alias — old code called this milestone_slot.
    milestone_slot = slot_label


def parse_sprint_name(name: str) -> Optional[ParsedSprintName]:
    """Parse a sprint name into its components. Returns None on no match.

    Tolerates both real Jira names ("FNTSY M30.3 Sprint 2026.12 FE") and
    placeholder forms ("FNTSY M31.4 (TBD FE)") synthesised by the
    sequence builder.
    """
    if not name:
        return None
    m = _SPRINT_RE.match(name)
    if not m:
        return None
    role = (m.group('role') or m.group('tbd_role') or None)
    if role:
        role = role.upper()
    return ParsedSprintName(
        milestone=int(m.group('milestone')),
        slot=int(m.group('slot')),
        week=m.group('week'),
        role=role,
        is_placeholder=name.find('(TBD') != -1,
    )


def format_long(name: str) -> str:
    """Canonical long form — typically the raw Jira name itself.

    For real sprints we return the input unchanged (no FNTSY-stripping or
    "Sprint" removal — that prefix is part of the canonical name and matches
    Jira). For unparseable input we still return the input verbatim so the
    user sees what's in the DB rather than an empty string.

    Example: "FNTSY M30.3 Sprint 2026.12 FE" → "FNTSY M30.3 Sprint 2026.12 FE"
    """
    return (name or "").strip()


def format_short(name: str) -> str:
    """Compact form — slot label plus role, nothing else.

    Use in Gantt cells, dense tables, and any context where horizontal
    space is at a premium. Solo sprints (no role) collapse to the slot
    label alone ("M30.1").

    Examples:
      "FNTSY M30.3 Sprint 2026.12 FE" → "M30.3 FE"
      "FNTSY M30.1 Sprint 2026.10"     → "M30.1"
      "FNTSY M31.4 (TBD BE)"          → "M31.4 BE"
    """
    parsed = parse_sprint_name(name)
    if not parsed:
        return (name or "").strip()
    if parsed.role:
        return f"{parsed.slot_label} {parsed.role}"
    return parsed.slot_label


def format_slot(name: str) -> str:
    """Slot label only — milestone + slot, no role, no week.

    Use this on per-sprint x-axes / rollups (e.g. epic count per slot).

    Examples:
      "FNTSY M30.3 Sprint 2026.12 FE" → "M30.3"
      "FNTSY M30.1 Sprint 2026.10"     → "M30.1"
    """
    parsed = parse_sprint_name(name)
    if not parsed:
        return (name or "").strip()
    return parsed.slot_label


def format_milestone(name: str) -> str:
    """Milestone only — no slot, no role, no week.

    Use this on charts that roll up across an entire milestone (all four
    sprint slots combined).

    Examples:
      "FNTSY M30.3 Sprint 2026.12 FE" → "M30"
      "FNTSY M30.1 Sprint 2026.10"     → "M30"
    """
    parsed = parse_sprint_name(name)
    if not parsed:
        return (name or "").strip()
    return parsed.milestone_label
