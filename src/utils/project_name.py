"""Resolve the human-facing initiative name for dashboard branding.

The dashboard brands itself "Project: {Name}". The name is derived from the
top-level initiative's Jira summary (captured in the project snapshot), so
re-pointing `jira.initiative_key` re-brands the whole dashboard automatically.

The summary often already starts with "Project" (e.g. "Project Final Fantasy"),
so we strip a leading "Project"/"Project:" before composing the label to avoid
"Project: Project Final Fantasy".
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

# Used when the snapshot is missing/unreadable so the dashboard still brands
# itself "Project: Fantasy" exactly as it did before this was made dynamic.
_DEFAULT_NAME = "Fantasy"

_SNAPSHOT_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "project_fantasy.json"

# Leading "Project" as a whole word, optionally followed by ":" and spaces.
_LEADING_PROJECT = re.compile(r"^\s*project\b\s*:?\s*", re.IGNORECASE)


def _strip_leading_project(summary: str) -> str:
    """Drop a leading 'Project'/'Project:' so it isn't doubled in the label."""
    return _LEADING_PROJECT.sub("", summary).strip()


@lru_cache(maxsize=1)
def project_name(snapshot_path: str | None = None) -> str:
    """Initiative name for branding, e.g. "Final Fantasy".

    Reads `initiative.summary` from the project snapshot and strips a leading
    "Project" prefix. Falls back to "Fantasy" when the snapshot is absent,
    malformed, or has no usable summary.
    """
    path = Path(snapshot_path) if snapshot_path else _SNAPSHOT_PATH
    try:
        snap = json.loads(path.read_text())
        summary = ((snap.get("initiative") or {}).get("summary") or "").strip()
    except Exception:
        return _DEFAULT_NAME
    if not summary:
        return _DEFAULT_NAME
    stripped = _strip_leading_project(summary)
    return stripped or summary


def project_label(snapshot_path: str | None = None) -> str:
    """The full branding string, e.g. "Project: Final Fantasy"."""
    return f"Project: {project_name(snapshot_path)}"
