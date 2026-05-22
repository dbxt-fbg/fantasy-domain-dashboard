"""Pure helpers for agent log parsing and run-duration math.

Extracted from generate_logs_dashboard.py — that file was 1100 lines because
it interleaved log-file IO, regex parsing, and HTML rendering. These pieces
have no SQL or HTML dependencies, so isolating them here makes them testable
and reusable from the QA agent / future tooling.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional, Tuple


def parse_log_line(line: str) -> Tuple[str, str]:
    """Classify a log line for CSS coloring on the logs dashboard.

    Returns (severity_class, line). Severity is a coarse bucket — the dashboard
    only uses it to pick a color, not for any analytical decision.
    """
    if 'ERROR' in line or 'Failed' in line or '❌' in line:
        return 'error', line
    if 'WARNING' in line or 'Retry' in line:
        return 'warning', line
    if 'SUCCESS' in line or '✅' in line or 'complete' in line.lower():
        return 'success', line
    return 'info', line


def read_log_file(log_path: Path, max_lines: int = 200) -> List[str]:
    """Return the last N lines of a log file, or [] if the file is missing.

    Errors are surfaced as a single-element list so the caller can render the
    message to the dashboard rather than raising.
    """
    if not log_path.exists():
        return []
    try:
        with open(log_path, 'r') as f:
            return f.readlines()[-max_lines:]
    except Exception as e:
        return [f"Error reading log: {e}"]


def format_duration(seconds: float) -> str:
    """Render a duration in seconds as `Xm Ys` / `Hh Mm`.

    Used by the logs dashboard's "Last Run Duration" cells.
    """
    if seconds <= 0:
        return "—"
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m {s}s"
    h, rem = divmod(int(seconds), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m"


def parse_wall_clock(token: str) -> Optional[datetime]:
    """Parse a `date(1)` style stamp like 'Tue May 20 11:30:00 PDT 2026'."""
    try:
        # Strip the timezone abbreviation; %Z handles inconsistently.
        parts = token.split()
        if len(parts) >= 6:
            sanitized = ' '.join(parts[:4] + parts[5:])
            return datetime.strptime(sanitized, '%a %b %d %H:%M:%S %Y')
    except Exception:
        pass
    return None


def parse_iso_log(token: str) -> Optional[datetime]:
    """Parse a logger-style ISO stamp like '2026-05-20 11:30:00,123'."""
    try:
        head = token.split(',')[0]
        return datetime.strptime(head, '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None


def last_run_duration(
    log_path: Path,
    start_re,
    end_re,
    parser: Callable[[str], Optional[datetime]],
) -> Optional[float]:
    """Scan a log file backwards for the last `end_re` match and find the most
    recent `start_re` before it. Returns elapsed seconds or None if either
    bookend is missing / unparsable.
    """
    if not log_path.exists():
        return None
    try:
        with open(log_path, 'r') as f:
            lines = f.readlines()
    except Exception:
        return None

    last_end = None
    for line in reversed(lines):
        m = end_re.search(line)
        if m:
            last_end = parser(m.group(1))
            if last_end:
                break

    if not last_end:
        return None

    last_start = None
    for line in reversed(lines):
        m = start_re.search(line)
        if not m:
            continue
        candidate = parser(m.group(1))
        if not candidate:
            continue
        if candidate <= last_end:
            last_start = candidate
            break

    if not last_start:
        return None

    delta = (last_end - last_start).total_seconds()
    return delta if delta >= 0 else None
