#!/usr/bin/env python3
"""
Generate Agents and Logs Dashboard HTML page.
"""

import sys
import re
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.io import atomic_write as _atomic_write
from utils.nav import generate_nav_menu
from database.schema import get_connection


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agents and Logs</title>
    <link rel="stylesheet" href="assets/dashboard.css">
    <script src="assets/dashboard.js?v=2.0" defer></script>
    <!-- toggleAgentLogs / toggleLog / triggerAgent live in assets/dashboard.js -->
</head>
<body class="page-logs">
    <div class="container">
        {content}
    </div>
</body>
</html>
"""


def _describe_cron(expr: str) -> str:
    """Best-effort human-friendly description of the cron expressions we use.

    Covers the common patterns in our scheduled_tasks.json. Unknown patterns
    are returned as-is so the card still shows useful info.
    """
    if not expr:
        return 'no schedule'
    parts = expr.split()
    if len(parts) != 5:
        return expr
    minute, hour, dom, month, dow = parts
    if dom != '*' or month != '*' or dow != '*':
        return expr

    # Pattern: every 15 minutes around the clock
    if minute == '*/15' and hour == '*':
        return 'every 15 minutes'
    # Pattern: every N minutes, constrained window, e.g. */15 6-17
    if minute.startswith('*/') and hour != '*':
        step = minute[2:]
        return f'every {step} minutes during {hour}:00'
    # Pattern: once per hour at a specific minute
    if minute.isdigit() and hour == '*':
        mm = int(minute)
        return f'every hour at :{mm:02d}'
    # Pattern: top of every hour
    if minute == '0' and hour == '*':
        return 'every hour on the hour'
    return expr


# Reusable log-parsing helpers live in src/utils/agent_status.py so they can
# be imported by the QA agent or future CLI tools without pulling in this
# generator's HTML/render dependencies.
from utils.agent_status import parse_log_line, read_log_file


def get_agent_status(db_path):
    """Get status of agents from database."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    status = {
        'jira_collector': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'issues_collected': 0, 'tickets_in_db': 0, 'sprints_tracked': 0},
        'github_pr': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'pr_count': 0, 'developer_count': 0, 'review_count': 0, 'comment_count': 0},
        'qa': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'checks_passed': 0, 'checks_failed': 0, 'critical': 0, 'warning': 0, 'info': 0, 'issues': []},
        'calendar_sync': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'meetings_tracked': 0, 'team_members_with_1on1': 0},
        'project_fantasy': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'features': 0, 'epics': 0, 'stories': 0, 'at_risk': 0},
        'hygiene': {'last_run': None, 'last_duration': None, 'status': 'Unknown', 'open_issues': 0, 'by_type': {}},
        'db_backup': {'last_run': None, 'last_duration': None, 'status': 'Unknown'},
    }

    try:
        # Unified Jira Collector status
        cursor.execute("""
            SELECT MAX(last_updated_at), COUNT(*), COUNT(DISTINCT sprint_id)
            FROM tickets
        """)
        row = cursor.fetchone()
        if row and row[0]:
            status['jira_collector']['last_run'] = row[0]
            status['jira_collector']['tickets_in_db'] = row[1] or 0
            status['jira_collector']['sprints_tracked'] = row[2] or 0
            status['jira_collector']['status'] = 'Active'

        # GitHub PR agent status
        cursor.execute("""
            SELECT MAX(snapshot_timestamp), COUNT(DISTINCT developer_github_username)
            FROM github_pr_snapshots
        """)
        row = cursor.fetchone()
        if row and row[0]:
            status['github_pr']['last_run'] = row[0]
            status['github_pr']['developer_count'] = row[1] or 0
            status['github_pr']['status'] = 'Active'

            # Get total PR count and review/comment counts (added after reviews table landed)
            cursor.execute('SELECT COUNT(*) FROM github_prs')
            status['github_pr']['pr_count'] = cursor.fetchone()[0] or 0
            try:
                cursor.execute('SELECT COUNT(*) FROM github_reviews')
                status['github_pr']['review_count'] = cursor.fetchone()[0] or 0
                cursor.execute('SELECT COUNT(*) FROM github_pr_comments')
                status['github_pr']['comment_count'] = cursor.fetchone()[0] or 0
            except Exception:
                # Tables may not exist on older DBs; ignore
                pass

        # Hygiene agent status
        try:
            cursor.execute("""
                SELECT MAX(detected_at), COUNT(*) FROM hygiene_issues
            """)
            row = cursor.fetchone()
            if row and row[0]:
                status['hygiene']['last_run'] = row[0]
                status['hygiene']['open_issues'] = row[1] or 0
                status['hygiene']['status'] = 'Active'

                cursor.execute("""
                    SELECT issue_type, COUNT(*) FROM hygiene_issues
                    GROUP BY issue_type ORDER BY COUNT(*) DESC LIMIT 5
                """)
                status['hygiene']['by_type'] = {t: c for t, c in cursor.fetchall()}
        except Exception:
            pass


        # Calendar Sync agent status
        cursor.execute("""
            SELECT MAX(last_synced_at), COUNT(*), COUNT(DISTINCT developer_name)
            FROM one_on_one_meetings
        """)
        row = cursor.fetchone()
        if row and row[0]:
            status['calendar_sync']['last_run'] = row[0]
            status['calendar_sync']['meetings_tracked'] = row[1] or 0
            status['calendar_sync']['team_members_with_1on1'] = row[2] or 0
            status['calendar_sync']['status'] = 'Active'

        # Project: Fantasy snapshot status (file-based, not DB)
        snapshot_path = Path(__file__).parent.parent / "data" / "project_fantasy.json"
        if snapshot_path.exists():
            try:
                import json as _json_pf
                snap = _json_pf.loads(snapshot_path.read_text())
                status['project_fantasy']['last_run'] = snap.get('generated_at')
                s = snap.get('summary', {}) or {}
                status['project_fantasy']['features'] = s.get('features_total', 0)
                status['project_fantasy']['epics'] = s.get('epics_total', 0)
                status['project_fantasy']['stories'] = s.get('stories_total', 0)
                status['project_fantasy']['at_risk'] = s.get('at_risk_count', 0)
                status['project_fantasy']['status'] = 'Active'
            except Exception:
                status['project_fantasy']['status'] = 'Error'

        # QA agent status - read from log file since it doesn't write to DB
        qa_log = Path(__file__).parent.parent / "logs" / "qa_agent.log"
        # Make sure the issue list is always present so the renderer doesn't
        # explode when parsing fails or the agent has never run.
        status['qa']['issues'] = []
        if qa_log.exists():
            import re
            # Extract the first integer after a colon, ignoring trailing text like
            # "(ran 11 this pass in 0.4s)" that newer QA output appends.
            _LEADING_INT = re.compile(r":\s*(-?\d+)")

            def _leading_int(s: str) -> int:
                m = _LEADING_INT.search(s)
                return int(m.group(1)) if m else 0

            try:
                with open(qa_log, 'r') as f:
                    lines = f.readlines()
                    # Parse the last QA run summary
                    for i, line in enumerate(reversed(lines)):
                        if 'QA Agent -' in line:
                            # Found start of last run, parse summary
                            remaining = lines[-(i):] if i > 0 else []
                            for summary_line in remaining:
                                try:
                                    if 'Total Checks:' in summary_line:
                                        _ = _leading_int(summary_line)
                                    elif '✅ Passed:' in summary_line:
                                        status['qa']['checks_passed'] = _leading_int(summary_line)
                                    elif '❌ Failed:' in summary_line:
                                        status['qa']['checks_failed'] = _leading_int(summary_line)
                                    elif '🚨 Critical Issues:' in summary_line:
                                        status['qa']['critical'] = _leading_int(summary_line)
                                    elif '⚠️  Warnings:' in summary_line:
                                        status['qa']['warning'] = _leading_int(summary_line)
                                    elif 'ℹ️  Info:' in summary_line:
                                        status['qa']['info'] = _leading_int(summary_line)
                                    elif 'QA check complete' in summary_line:
                                        break
                                except Exception:
                                    # Don't let one weird line skip the timestamp assignment.
                                    continue
                            status['qa']['status'] = 'Active'
                            # Always set last_run from the QA Agent header line we matched.
                            try:
                                timestamp_str = line.split('QA Agent -')[1].strip()
                                status['qa']['last_run'] = timestamp_str
                            except Exception:
                                pass
                            # Pull issue lines from this run for the agent card.
                            # Format in the log: "INFO - 🚨 [Category] message"
                            # or "INFO - ⚠️ [Category] message". We capture severity,
                            # category, and message. Stop scanning once we hit the
                            # "QA check complete" terminator so we don't bleed into
                            # the next run's lines.
                            # ⚠️ and ℹ️ are emoji + variation-selector sequences;
                            # use alternation rather than a character class so the
                            # multi-codepoint glyphs match cleanly.
                            issue_pat = re.compile(
                                r'^\s*(?:INFO|WARNING|ERROR)\s+-\s+'
                                r'(?P<sev>🚨|⚠️|ℹ️)\s+'
                                r'\[(?P<cat>[^\]]+)\]\s+'
                                r'(?P<msg>.+?)\s*$'
                            )
                            issues = []
                            for ln in remaining:
                                if 'QA check complete' in ln:
                                    break
                                m = issue_pat.match(ln.rstrip('\n'))
                                if m:
                                    issues.append({
                                        'severity': m.group('sev'),
                                        'category': m.group('cat'),
                                        'message': m.group('msg'),
                                    })
                            status['qa']['issues'] = issues
                            break
            except Exception:
                pass  # If parsing fails, leave as Unknown

    finally:
        conn.close()

    # Compute "last run took N seconds" for each agent by pairing the most
    # recent start marker with the most recent completion marker in its log.
    _attach_last_durations(status)

    return status


def _format_duration(seconds: float) -> str:
    """Pretty-print a duration like '2.4s', '47s', '3m 12s', '1h 5m'."""
    if seconds < 1:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    mins, secs = divmod(int(round(seconds)), 60)
    if mins < 60:
        return f"{mins}m {secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins:02d}m"


def _parse_wall_clock(token: str):
    """Parse `Fri May 15 09:00:01 PDT 2026` (wrapper format)."""
    from datetime import datetime as _dt
    # %Z is unreliable on macOS; strip the tz token before parsing.
    parts = token.strip().split()
    parts = [p for p in parts if not (len(p) in (3, 4) and p.isupper())]
    cleaned = ' '.join(parts)
    try:
        return _dt.strptime(cleaned, '%a %b %d %H:%M:%S %Y')
    except ValueError:
        return None


def _parse_iso_log(token: str):
    """Parse `2026-05-15 09:04:47` from python-formatted log lines."""
    from datetime import datetime as _dt
    try:
        return _dt.strptime(token.strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def _last_run_duration(log_path, start_re, end_re, parser):
    """Walk a log file backward to find the most recent (start, end) pair.

    `start_re` and `end_re` each match a candidate boundary line and put the
    parseable timestamp token in their first capture group. `parser(token)`
    converts that token to a datetime. Returns seconds (float) or None.
    """
    import re as _re
    from pathlib import Path as _P
    p = _P(log_path)
    if not p.exists():
        return None
    try:
        with open(p, 'r', errors='replace') as f:
            lines = f.readlines()
    except Exception:
        return None

    end_dt = start_dt = None
    # Find the most recent end first, then the start that precedes it.
    end_idx = None
    for i in range(len(lines) - 1, -1, -1):
        m = end_re.search(lines[i])
        if m:
            end_dt = parser(m.group(1)) if m.groups() else None
            end_idx = i
            break
    if end_idx is None:
        return None
    for i in range(end_idx, -1, -1):
        m = start_re.search(lines[i])
        if m:
            start_dt = parser(m.group(1)) if m.groups() else None
            break
    if not start_dt or not end_dt:
        return None
    delta = (end_dt - start_dt).total_seconds()
    if delta < 0:
        return None
    return delta


def _attach_last_durations(status):
    """Populate status[<agent>]['last_duration'] from per-agent log files.

    Each agent has its own start/end markers — wrappers print
    `=== <Name> starting at <date> ===` / `✅ <Name> completed successfully`
    while the python scripts emit ISO-prefixed lines. Best-effort; failures
    leave the field as None and the UI will skip the row.
    """
    import re as _re
    from pathlib import Path as _P

    logs_dir = _P(__file__).parent.parent / 'logs'

    # Wrapper-format agents: line is `=== <Name> starting at <wall-clock> ===`
    # and the corresponding success line is `✅ <Name> completed successfully`.
    # The wrapper writes the success line right after the python script exits
    # cleanly, so the file's mtime is a fine proxy for the end timestamp when
    # the success line lacks one. We still pair lines so a crash mid-run isn't
    # counted as "very long."
    wrapper_specs = [
        ('qa',              'qa_agent.log',              'QA Agent'),
        ('github_pr',       'github_pr_agent.log',       'GitHub PR Agent'),
        ('jira_collector',  'jira_collector_agent.log',  'Jira Collector'),
    ]
    for key, fname, name in wrapper_specs:
        path = logs_dir / fname
        if not path.exists():
            continue
        try:
            with open(path, 'r', errors='replace') as f:
                text = f.read()
        except Exception:
            continue
        starts = list(_re.finditer(rf'=== {_re.escape(name)} starting at (.+?) ===', text))
        ends = list(_re.finditer(rf'✅ {_re.escape(name)} completed successfully', text))
        if not starts or not ends:
            continue
        # Pair the latest start with the latest end that follows it.
        last_start = starts[-1]
        last_end = ends[-1]
        if last_end.start() <= last_start.start():
            continue
        start_dt = _parse_wall_clock(last_start.group(1))
        if not start_dt:
            continue
        # End timestamp: there's no wall-clock on the success line, so use the
        # log's mtime (refreshed each time the wrapper writes to it).
        try:
            end_ts = path.stat().st_mtime
            from datetime import datetime as _dt
            end_dt = _dt.fromtimestamp(end_ts)
        except Exception:
            continue
        delta = (end_dt - start_dt).total_seconds()
        if 0 <= delta < 24 * 3600:
            status[key]['last_duration'] = delta

    # Hygiene: pair the latest "Searching Jira with JQL: project = FNTSY"
    # full-pull line with the "✅ Hygiene check complete!" line. Both are
    # python-formatted. (collector.log is the file the hygiene agent writes.)
    coll = logs_dir / 'collector.log'
    if coll.exists():
        try:
            with open(coll, 'r', errors='replace') as f:
                text = f.read()
        except Exception:
            text = ''
        # We use "Logging configured successfully" or first JQL search as the
        # starting marker and "✅ Hygiene check complete!" as the end.
        ts_re = _re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        lines = text.splitlines()
        end_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if 'Hygiene check complete!' in lines[i] or '✅ Hygiene check complete' in lines[i]:
                end_idx = i
                break
        if end_idx is not None:
            # Walk back to find the most recent "Logging configured successfully"
            start_idx = None
            for j in range(end_idx, -1, -1):
                if 'Logging configured successfully' in lines[j] or 'Starting Jira hygiene check' in lines[j]:
                    start_idx = j
                    break
            if start_idx is not None:
                start_m = ts_re.match(lines[start_idx])
                # End line itself often lacks ISO prefix; pull from neighbor up to 5 lines before.
                end_m = ts_re.match(lines[end_idx])
                if not end_m:
                    for k in range(end_idx, max(end_idx - 6, -1), -1):
                        end_m = ts_re.match(lines[k])
                        if end_m:
                            break
                if start_m and end_m:
                    s = _parse_iso_log(start_m.group(1))
                    e = _parse_iso_log(end_m.group(1))
                    if s and e:
                        delta = (e - s).total_seconds()
                        if 0 <= delta < 24 * 3600:
                            status['hygiene']['last_duration'] = delta

    # Calendar sync: "Starting Google Calendar sync" → "Calendar sync complete"
    # The logger emits each event twice — once with the python-style ISO prefix
    # and once via a stdout handler without one. We require the ISO-prefixed
    # variant so duration math gets a real timestamp on both ends.
    cal = logs_dir / 'calendar_sync.log'
    if cal.exists():
        try:
            with open(cal, 'r', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            lines = []
        ts_re = _re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        end_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if 'Calendar sync complete' in lines[i] and ts_re.match(lines[i]):
                end_idx = i
                break
        if end_idx is not None:
            start_idx = None
            for j in range(end_idx, -1, -1):
                if 'Starting Google Calendar sync' in lines[j] and ts_re.match(lines[j]):
                    start_idx = j
                    break
            if start_idx is not None:
                sm = ts_re.match(lines[start_idx])
                em = ts_re.match(lines[end_idx])
                if sm and em:
                    s = _parse_iso_log(sm.group(1))
                    e = _parse_iso_log(em.group(1))
                    if s and e:
                        delta = (e - s).total_seconds()
                        if 0 <= delta < 24 * 3600:
                            status['calendar_sync']['last_duration'] = delta

    # DB backup: "Backing up …" → "Backup OK …" — same python ISO format.
    bk = logs_dir / 'backup_db.log'
    if bk.exists():
        try:
            with open(bk, 'r', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            lines = []
        ts_re = _re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        end_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if 'Backup OK' in lines[i]:
                end_idx = i
                break
        if end_idx is not None:
            start_idx = None
            for j in range(end_idx, -1, -1):
                if 'Backing up' in lines[j]:
                    start_idx = j
                    break
            if start_idx is not None:
                sm = ts_re.match(lines[start_idx])
                em = ts_re.match(lines[end_idx])
                if sm and em:
                    s = _parse_iso_log(sm.group(1))
                    e = _parse_iso_log(em.group(1))
                    if s and e:
                        delta = (e - s).total_seconds()
                        if 0 <= delta < 24 * 3600:
                            status['db_backup']['last_duration'] = delta

    # Project: Fantasy snapshot — written from sync_project_fantasy.py.
    # The first/last log line per run wraps the sync; not every line is
    # interesting, so use the simplest pair: file mtime ≈ end, and the
    # most-recent "Starting Project: Fantasy sync" if present.
    pf = logs_dir / 'sync_project_fantasy.log'
    if pf.exists():
        try:
            with open(pf, 'r', errors='replace') as f:
                lines = f.readlines()
        except Exception:
            lines = []
        ts_re = _re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
        end_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if ts_re.match(lines[i]):
                end_idx = i
                break
        if end_idx is not None:
            start_idx = None
            for j in range(end_idx, -1, -1):
                if 'Starting Project: Fantasy' in lines[j] or 'Starting Fantasy' in lines[j] \
                        or 'Starting project_fantasy' in lines[j]:
                    start_idx = j
                    break
            if start_idx is not None:
                sm = ts_re.match(lines[start_idx])
                em = ts_re.match(lines[end_idx])
                if sm and em:
                    s = _parse_iso_log(sm.group(1))
                    e = _parse_iso_log(em.group(1))
                    if s and e:
                        delta = (e - s).total_seconds()
                        if 0 <= delta < 24 * 3600:
                            status['project_fantasy']['last_duration'] = delta


def _load_agent_runs(n: int = 10):
    """Load and partition recent agent runs by source_agent."""
    import json as _json
    run_log = Path(__file__).parent.parent / "data" / "qa_runs.jsonl"
    if not run_log.exists():
        return {"qa": [], "hygiene": []}
    runs = []
    for line in run_log.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            runs.append(_json.loads(line))
        except Exception:
            continue
    # Split by source_agent. QA's older records don't carry source_agent — those
    # default to "qa" so history stays continuous.
    buckets = {"qa": [], "hygiene": []}
    for r in runs:
        src = (r.get("summary", {}) or {}).get("source_agent", "qa")
        if src == "hygiene":
            buckets["hygiene"].append(r)
        else:
            buckets["qa"].append(r)
    buckets["qa"] = buckets["qa"][-n:][::-1]
    buckets["hygiene"] = buckets["hygiene"][-n:][::-1]
    return buckets


def _render_qa_agent_panel(n: int = 10) -> str:
    """Render Recent Runs panels for every agent that writes to qa_runs.jsonl.

    Panels are separated by source_agent so QA and hygiene runs don't
    interleave. Each panel shows per-check decision pills and per-fix
    outcomes so the agent's behavior is inspectable, not a black box.
    """
    import html as _html
    buckets = _load_agent_runs(n=n)
    if not any(buckets.values()):
        return """
            <h2 style="font-size: 18px; color: #f1f5f9; margin: 32px 0 20px 0;">🤖 Agent Runs</h2>
            <div class="intro-banner">No agent runs yet. Run <code>python3 scripts/qa_agent.py</code> or <code>python3 scripts/jira_hygiene_agent.py</code> to populate.</div>
        """

    def _fmt_ts(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return dt.astimezone().strftime("%b %d, %H:%M:%S")
        except Exception:
            return iso

    def _render_run_card(r: dict) -> str:
        summary = r.get("summary", {})
        budget = r.get("budget", {})
        fixes = r.get("fixes", [])
        checks = r.get("checks", [])
        source = (summary or {}).get("source_agent", "qa")

        decisions = {"run": 0, "skip_state_hash": 0, "skip_upstream_failed": 0,
                      "skip_flaky": 0, "defer_budget": 0}
        for c in checks:
            decisions[c.get("decision", "run")] = decisions.get(c.get("decision", "run"), 0) + 1
        ran = decisions.get("run", 0)
        skipped = sum(v for k, v in decisions.items() if k != "run")

        # Status badge per agent.
        if source == "hygiene":
            open_issues = summary.get("open_issues", 0)
            resolved = summary.get("resolved_this_run", 0)
            if summary.get("error"):
                badge_cls, badge_text = "status-critical", "errored"
            elif open_issues == 0:
                badge_cls, badge_text = "status-success", "all clean"
            elif open_issues > 20:
                badge_cls, badge_text = "status-critical", f"{open_issues} open"
            else:
                badge_cls, badge_text = "status-warning", f"{open_issues} open"
        else:
            critical = summary.get("critical", 0)
            failed = summary.get("failed", 0)
            if critical > 0:
                badge_cls, badge_text = "status-critical", f"{critical} critical"
            elif failed > 0:
                badge_cls, badge_text = "status-warning", f"{failed} failed"
            else:
                badge_cls, badge_text = "status-success", "all passed"

        # Inline per-check pills
        pill_html_parts = []
        for c in checks:
            dec = c.get("decision", "run")
            status = c.get("status", "passed")
            if dec == "run" and status == "passed":
                bg, fg, tip = "#064e3b", "#6ee7b7", "passed"
            elif dec == "run" and status == "failed":
                bg, fg, tip = "#7f1d1d", "#fca5a5", "failed"
            elif dec == "run" and status == "error":
                bg, fg, tip = "#7f1d1d", "#fca5a5", "error"
            elif dec == "skip_state_hash":
                bg, fg, tip = "#1e293b", "#94a3b8", "skipped: inputs unchanged"
            elif dec == "skip_upstream_failed":
                bg, fg, tip = "#78350f", "#fcd34d", "skipped: upstream failed"
            elif dec == "skip_flaky":
                bg, fg, tip = "#312e81", "#a5b4fc", "skipped: flaky"
            elif dec == "defer_budget":
                bg, fg, tip = "#1e3a8a", "#93c5fd", "deferred: over budget"
            else:
                bg, fg, tip = "#334155", "#cbd5e1", dec
            # Hygiene checks list counts in `issues_count`; show inline.
            count_suffix = ""
            if source == "hygiene" and c.get("issues_count"):
                count_suffix = f" ({c['issues_count']})"
            reason = c.get("decision_reason") or tip
            pill_html_parts.append(
                f'<span title="{_html.escape(c["key"])} — {_html.escape(reason)}" '
                f'style="display:inline-block; background:{bg}; color:{fg}; '
                f'padding: 1px 8px; border-radius: 3px; font-size: 10px; '
                f'font-family: var(--font-mono); margin: 2px 3px 2px 0;">'
                f'{_html.escape(c["key"])}{count_suffix}</span>'
            )

        fix_html_parts = []
        for f in fixes:
            action = f.get("action", {}) or {}
            outcome = f.get("outcome", "")
            atype = action.get("type", "?")
            if outcome == "verified":
                color, icon = "var(--success-text)", "✓"
            elif outcome == "regressed":
                color, icon = "var(--warning-text)", "↻"
            elif outcome == "failed":
                color, icon = "var(--danger-text)", "✗"
            elif outcome == "proposed":
                color, icon = "var(--accent-text)", "?"
            elif outcome == "escalated":
                color, icon = "var(--warning-text)", "!"
            else:
                color, icon = "var(--text-muted)", "·"
            fix_html_parts.append(
                f'<div style="font-family: var(--font-mono); font-size: 11px; color: {color};">'
                f'  {icon} {_html.escape(atype)} → {_html.escape(outcome)}'
                f'{": " + _html.escape(str(f.get("detail",""))[:120]) if f.get("detail") else ""}'
                f'</div>'
            )

        budget_html = ""
        if budget:
            bs = budget.get("budget_s")
            us = budget.get("used_s")
            if bs is not None:
                budget_html = (f'<span style="color: var(--text-muted); font-size: 11px;">'
                                f'budget {bs:.0f}s · used {us:.1f}s</span>')
            elif us is not None:
                budget_html = (f'<span style="color: var(--text-muted); font-size: 11px;">'
                                f'took {us:.1f}s</span>')

        started = _fmt_ts(r.get("started_at", ""))
        sha = r.get("git_sha", "")
        sha_html = (f'<span style="color: var(--text-faint); font-family: var(--font-mono); '
                     f'font-size: 11px;">{_html.escape(sha)}</span>' if sha else '')

        # Source-specific subtitle
        if source == "hygiene":
            open_issues = summary.get("open_issues", 0)
            resolved = summary.get("resolved_this_run", 0)
            rules = summary.get("total_rules", len(checks))
            subtitle = (f"{rules} rule(s) evaluated · {open_issues} open · "
                        f"{resolved} resolved this run · {budget_html}")
        else:
            subtitle = (f"{ran} check(s) ran · {skipped} skipped/deferred · "
                        f"{len(fixes)} fix action(s) · {budget_html}")

        return f"""
            <div class="status-card" style="margin-bottom: 12px;">
                <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; flex-wrap: wrap;">
                    <div>
                        <h3 style="margin: 0;">🧠 Run {_html.escape(r.get('run_id',''))}  <span style="color: var(--text-muted); font-size: 13px; font-weight: 400;">· {started}</span>  {sha_html}</h3>
                        <div style="color: var(--text-muted); font-size: 12px; margin-top: 4px;">
                            {subtitle}
                        </div>
                    </div>
                    <span class="status-badge {badge_cls}">{_html.escape(badge_text)}</span>
                </div>
                <div style="margin-top: 8px;">{''.join(pill_html_parts)}</div>
                {('<div style="margin-top: 8px;">' + ''.join(fix_html_parts) + '</div>') if fix_html_parts else ''}
            </div>
        """

    sections = []
    panel_defs = [
        ("qa",      "🧠 QA Agent — Recent Runs",
         "State-hash gating, verify-after-fix, and HITL proposals."),
        ("hygiene", "🧼 Hygiene Agent — Recent Runs",
         "Each pill is one hygiene rule. Count shows currently-open issues."),
    ]
    for src_key, title, blurb in panel_defs:
        runs = buckets.get(src_key, [])
        if not runs:
            continue
        rows_html = [_render_run_card(r) for r in runs]
        sections.append(f"""
            <h2 style="font-size: 18px; color: #f1f5f9; margin: 32px 0 12px 0;">{title}</h2>
            <div style="color: var(--text-muted); font-size: 12px; margin-bottom: 12px;">
                Last {len(runs)} runs · {blurb}
            </div>
            <div>
                {''.join(rows_html)}
            </div>
        """)

    return "".join(sections)


def generate_logs_dashboard(config: dict, output_path: Path):
    """Generate automation logs dashboard HTML."""
    db_path = config['database']['path']
    logs_dir = Path(__file__).parent.parent / "logs"

    # Get agent status
    agent_status = get_agent_status(db_path)

    # Load scheduled-task definitions so each agent card shows the real cron
    # cadence (label was drifting from reality before — single source of truth now).
    scheduled_tasks_path = Path(__file__).parent.parent / ".claude" / "scheduled_tasks.json"
    scheduled = {}
    try:
        import json as _json
        raw = _json.loads(scheduled_tasks_path.read_text())
        for t in raw.get('tasks', []):
            scheduled[t['id']] = t.get('cron', '')
    except Exception:
        pass

    def _schedule_for(cron_id: str, fallback: str) -> str:
        """Human-friendly label. Returns the fallback if the id isn't scheduled."""
        cron = scheduled.get(cron_id)
        if not cron:
            return fallback
        return f"cron `{cron}` · {_describe_cron(cron)}"

    nav_menu = generate_nav_menu('logs')

    # Define agents with their details (sorted alphabetically by name)
    agents = [
        {
            'id': 'calendar-sync',
            'name': 'Calendar Sync',
            'icon': '📅',
            'schedule': _schedule_for('calendar_sync', 'On demand'),
            'log_file': 'calendar_sync.log',
            'trigger': 'calendar-sync',
            'status_class': '',
            'details': lambda: [
                f"Last Run: {agent_status['calendar_sync']['last_run'] if agent_status['calendar_sync']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['calendar_sync']['last_duration'])}" if agent_status['calendar_sync'].get('last_duration') is not None else "Duration: —",
                f"1-on-1 Meetings Tracked: {agent_status['calendar_sync']['meetings_tracked']}",
                f"Team Members: {agent_status['calendar_sync']['team_members_with_1on1']}",
                "Syncs recurring 1-on-1 meetings from Google Calendar"
            ],
            'badge': lambda: ('active' if agent_status['calendar_sync']['status'] == 'Active' else 'idle', agent_status['calendar_sync']['status'])
        },
        {
            'id': 'project-fantasy',
            'name': 'Project: Fantasy Snapshot',
            'icon': '🎯',
            'schedule': _schedule_for('project_fantasy_snapshot', 'On demand (python3 scripts/sync_project_fantasy.py)'),
            'log_file': 'sync_project_fantasy.log',
            'trigger': 'project-fantasy',
            'status_class': '',
            'details': lambda: [
                f"Last Run: {agent_status['project_fantasy']['last_run'] if agent_status['project_fantasy']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['project_fantasy']['last_duration'])}" if agent_status['project_fantasy'].get('last_duration') is not None else "Duration: —",
                f"Features: {agent_status['project_fantasy']['features']}",
                f"Epics: {agent_status['project_fantasy']['epics']}",
                f"Stories: {agent_status['project_fantasy']['stories']}",
                f"At Risk: {agent_status['project_fantasy']['at_risk']}",
                "Pulls INIT-185 and descendants from Jira into data/project_fantasy.json"
            ],
            'badge': lambda: ('active' if agent_status['project_fantasy']['status'] == 'Active' else 'idle', agent_status['project_fantasy']['status'])
        },
        {
            'id': 'jira-collector',
            'name': 'Jira Collector',
            'icon': '🔄',
            'schedule': _schedule_for('jira_collector', 'On demand'),
            'log_file': 'jira_collector_agent.log',
            'trigger': 'jira-collector',
            'status_class': '',
            'details': lambda: [
                f"Last Run: {agent_status['jira_collector']['last_run'] if agent_status['jira_collector']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['jira_collector']['last_duration'])}" if agent_status['jira_collector'].get('last_duration') is not None else "Duration: —",
                f"Tickets in Database: {agent_status['jira_collector']['tickets_in_db']}",
                f"Sprints Tracked: {agent_status['jira_collector']['sprints_tracked']}",
                "Collects: Stories, Epics, Story Points, Hygiene"
            ],
            'badge': lambda: ('active' if agent_status['jira_collector']['status'] == 'Active' else 'idle', agent_status['jira_collector']['status'])
        },
        {
            'id': 'qa',
            'name': 'QA Agent',
            'icon': '🔬',
            'schedule': _schedule_for('qa_agent_auto_fix', 'On demand') + ' · use --deep for Jira-backed hygiene checks',
            'log_file': 'qa_agent.log',
            'trigger': 'qa',
            'status_class': lambda: 'error' if agent_status['qa']['critical'] > 0 else ('warning' if agent_status['qa']['warning'] > 0 else ''),
            'details': lambda: [
                f"Last Run: {agent_status['qa']['last_run'] if agent_status['qa']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['qa']['last_duration'])}" if agent_status['qa'].get('last_duration') is not None else "Duration: —",
                f"Checks: {agent_status['qa']['checks_passed']} passed, {agent_status['qa']['checks_failed']} failed",
                f"Issues: 🚨 {agent_status['qa']['critical']} critical, ⚠️ {agent_status['qa']['warning']} warnings, ℹ️ {agent_status['qa']['info']} info",
            ] + (
                [f"{iss['severity']} [{iss['category']}] {iss['message']}" for iss in agent_status['qa'].get('issues', [])]
                or (["No issues from the last run."] if agent_status['qa']['last_run'] else [])
            ) + [
                "Local consistency + (with --deep) Jira-backed hygiene validation"
            ],
            'badge': lambda: ('active' if agent_status['qa']['status'] == 'Active' else 'idle', agent_status['qa']['status'])
        },
        {
            'id': 'github-pr',
            'name': 'GitHub PR Agent',
            'icon': '🐙',
            'schedule': _schedule_for('github_pr_agent', 'On demand'),
            'log_file': 'github_pr_agent.log',
            'trigger': 'team-member',
            'status_class': '',
            'details': lambda: [
                f"Last Run: {agent_status['github_pr']['last_run'] if agent_status['github_pr']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['github_pr']['last_duration'])}" if agent_status['github_pr'].get('last_duration') is not None else "Duration: —",
                f"PRs Tracked: {agent_status['github_pr']['pr_count']}",
                f"Reviews Captured: {agent_status['github_pr']['review_count']}",
                f"PR Comments Captured: {agent_status['github_pr']['comment_count']}",
                f"Developers: {agent_status['github_pr']['developer_count']}",
                "Pulls PRs, reviews, and comments from GitHub for each team member"
            ],
            'badge': lambda: ('active' if agent_status['github_pr']['status'] == 'Active' else 'idle', agent_status['github_pr']['status'])
        },
        {
            'id': 'hygiene',
            'name': 'Jira Hygiene Agent',
            'icon': '🧹',
            'schedule': _schedule_for('jira_hygiene_agent', 'On demand'),
            'log_file': 'jira_hygiene_agent.log',
            'trigger': 'hygiene',
            'status_class': lambda: 'warning' if agent_status['hygiene']['open_issues'] > 50 else '',
            'details': lambda: [
                f"Last Run: {agent_status['hygiene']['last_run'] if agent_status['hygiene']['last_run'] else 'Never'}",
                f"Duration: {_format_duration(agent_status['hygiene']['last_duration'])}" if agent_status['hygiene'].get('last_duration') is not None else "Duration: —",
                f"Open Issues: {agent_status['hygiene']['open_issues']}",
                *[f"  · {t}: {c}" for t, c in list(agent_status['hygiene']['by_type'].items())[:5]],
                "Checks Features, Epics, and Stories for missing fields, parents, descriptions, etc."
            ],
            'badge': lambda: ('active' if agent_status['hygiene']['status'] == 'Active' else 'idle', agent_status['hygiene']['status'])
        }
    ]

    # Pre-resolve per-agent status so we can render the sub-nav and the cards
    # from the same source. We map each badge/status-class pair to one of the
    # three coloured pill buckets used on the Team Members page.
    def _pill_status(badge_class, status_class):
        # status_class wins when set (it reflects error/warning escalation from
        # the card itself). Otherwise fall back to the badge.
        if status_class == 'error':
            return 'needs-attention'
        if status_class == 'warning':
            return 'at-risk'
        if badge_class == 'active':
            return 'on-track'
        if badge_class == 'idle':
            return 'at-risk'
        return 'needs-attention'

    sorted_agents = sorted(agents, key=lambda x: x['name'])
    resolved = []  # list of (agent, status_class, details, badge_class, badge_text, pill_status)
    for agent in sorted_agents:
        sc = agent['status_class']() if callable(agent['status_class']) else agent['status_class']
        det = agent['details']()
        bc, bt = agent['badge']()
        ps = _pill_status(bc, sc)
        resolved.append((agent, sc, det, bc, bt, ps))

    pill_counts = {'on-track': 0, 'at-risk': 0, 'needs-attention': 0}
    for _, _, _, _, _, ps in resolved:
        pill_counts[ps] = pill_counts.get(ps, 0) + 1

    # Sub-nav (pills) — mirrors the Team Members sub-nav layout/CSS so the
    # 6-column grid and colour-by-status styling work automatically.
    pills_html = ''
    for agent, _sc, _det, _bc, _bt, ps in resolved:
        anchor = f"agent-card-{agent['id']}"
        pills_html += (
            f'<a href="#{anchor}" class="member-pill {ps}">'
            f'<span class="dot"></span>{agent["icon"]} {agent["name"]}</a>'
        )

    agent_sub_nav = f"""
        <nav class="sub-nav member-sub-nav">
            <div class="member-sub-nav-inner">
                <div class="member-sub-nav-legend">
                    <span><span class="legend-dot on-track"></span>On Track · {pill_counts['on-track']}</span>
                    <span><span class="legend-dot at-risk"></span>At Risk · {pill_counts['at-risk']}</span>
                    <span><span class="legend-dot needs-attention"></span>Needs Attention · {pill_counts['needs-attention']}</span>
                </div>
                <div class="member-pills">
                    {pills_html}
                </div>
            </div>
        </nav>
    """

    # Build HTML
    content = f"""
        <header>
            <h1>🤖 Agents and Logs</h1>
            <div class="subtitle">Agent Activity & System Logs • Updated {datetime.now().strftime('%B %d, %Y at %H:%M')}</div>
        </header>
        {nav_menu}
        {agent_sub_nav}
        <div class="content">
            <h2 style="font-size: 18px; color: #f1f5f9; margin-bottom: 20px;">Agent Status</h2>
            <div class="agent-status">
    """

    # Generate agent cards in alphabetical order (reuse the resolved list)
    for agent, status_class, details, badge_class, badge_text, _pill in resolved:
        # Read log file
        log_path = logs_dir / agent['log_file']
        lines = read_log_file(log_path, max_lines=100)

        anchor = f"agent-card-{agent['id']}"
        content += f"""
                <div id="{anchor}" class="status-card {status_class}" style="scroll-margin-top: 20px;">
                    <h3>{agent['icon']} {agent['name']}</h3>
                    <div class="status-detail">Schedule: {agent['schedule']}</div>
        """

        for detail in details:
            content += f'                    <div class="status-detail">{detail}</div>\n'

        content += f"""
                    <span class="status-badge {badge_class}">{badge_text}</span>
                    <div class="agent-buttons">
                        <button type="button" class="btn primary" onclick="triggerAgent('{agent['trigger']}')">▶ Run Now</button>
                        <button type="button" class="btn secondary" onclick="toggleAgentLogs('{agent['id']}')">Show Logs</button>
                    </div>
                    <div id="{agent['id']}-logs" class="agent-logs">
                        <div class="log-container">
        """

        if lines:
            for line in lines:
                css_class, formatted_line = parse_log_line(line.strip())
                content += f'                            <div class="log-line {css_class}">{formatted_line}</div>\n'
        else:
            content += '                            <div class="empty-state">No logs available yet</div>\n'

        content += """
                        </div>
                    </div>
                </div>
        """

    # Close the agent-status grid and append the QA Agent history panel.
    content += "            </div>\n"  # closes .agent-status
    content += _render_qa_agent_panel()

    # Write HTML file
    html = HTML_TEMPLATE.format(content=content)
    _atomic_write(output_path, html)
    print(f"✅ Logs dashboard generated: {output_path}")


def main():
    """Main entry point."""
    config = load_config()
    output_path = Path(__file__).parent.parent / "reports" / "html" / "logs_dashboard.html"

    generate_logs_dashboard(config, output_path)


if __name__ == "__main__":
    main()
