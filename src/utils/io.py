"""Shared file-IO helpers used by multiple generators / agents.

Keep tiny — anything domain-specific belongs in its own module.
"""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, content: str) -> None:
    """Write `content` to `path` atomically, skipping the write when the
    target's bytes already match.

    Writes to a sibling .tmp file first, then os.replace onto the target.
    os.replace is atomic on POSIX and Windows, so a browser refreshing
    mid-generation never sees a half-written page.

    The "skip when unchanged" path matters because the cron regenerates
    every page every 15 minutes — most of those rewrites would write the
    same bytes the file already had. Skipping saves syscalls and keeps
    file mtimes accurate (so the QA agent's HTML-staleness check can
    distinguish "the generator never ran" from "ran but nothing changed").
    """
    if path.exists():
        try:
            existing = path.read_text()
            if existing == content:
                # Content already correct — bump mtime so the QA agent's
                # "page hasn't been generated in N minutes" check still sees
                # this as a fresh run, but skip the actual rewrite so we
                # don't churn fsync.
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                return
        except OSError:
            pass  # fall through and rewrite
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(content)
    os.replace(tmp, path)
