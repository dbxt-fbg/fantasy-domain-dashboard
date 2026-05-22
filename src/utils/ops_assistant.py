"""Conversational assistant ("Fantasy Ops") for the EM dashboard.

A read-only chief-of-staff on top of the dashboard's live data. Takes a
user question, optionally routes through tool calls against the local
SQLite DB + agent run log, and returns prose.

Design constraints:
  * read-only: every tool reads data, none writes
  * model calls go through HTTP directly (no SDK dependency) so this
    module stands on its own
  * if ANTHROPIC_API_KEY is missing, the endpoint returns a canned
    "drop the key in config/.env" message — the widget still works
  * tool loop is bounded (MAX_TOOL_TURNS) so runaway plans can't blow
    up the latency budget
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths + config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_DIR = REPO_ROOT / "config"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
# Override via config/.env: ANTHROPIC_MODEL=claude-...
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
MAX_TOOL_TURNS = 6
MODEL_MAX_TOKENS = 1024

AGENT_NAME = "Fantasy Ops"


def _load_env_file() -> None:
    """Read config/.env into os.environ (idempotent)."""
    env_path = CONFIG_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def get_api_key() -> Optional[str]:
    _load_env_file()
    return os.environ.get("ANTHROPIC_API_KEY") or None


def _db_path() -> str:
    # Avoid yaml dependency here — the metrics.db is at a known path.
    return str(REPO_ROOT / "data" / "metrics.db")


_local = threading.local()


def _db_inode() -> int:
    """Inode of the current data/metrics.db. Used to detect file replacement
    after backup_db.py restoration so we can drop a stale cached connection
    pointing at an unlinked inode."""
    try:
        return os.stat(_db_path()).st_ino
    except OSError:
        return -1


def _db_conn() -> sqlite3.Connection:
    """Return a thread-local SQLite connection.

    Previously every tool call opened a new connection — for an /api/ask
    request that fires 6+ tools, that meant 6+ handshakes against the
    SQLite file. SQLite's `check_same_thread` keeps each web-server
    thread isolated, so a per-thread cached connection is safe and fast.

    The connection is invalidated and reopened when the DB file's inode
    changes — which happens when backup_db.py replaces the file, or when
    a manual rebuild swaps in a new metrics.db. Without that check the
    cached fd would silently serve from the unlinked inode (stale data
    or sqlite errors).
    """
    current_inode = _db_inode()
    conn = getattr(_local, "conn", None)
    cached_inode = getattr(_local, "inode", None)
    if conn is not None and cached_inode == current_inode:
        return conn

    # Inode mismatch (or first call): close the stale conn if any, reopen.
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    conn = sqlite3.connect(_db_path(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    _local.conn = conn
    _local.inode = current_inode
    return conn


def _close_thread_db_conn() -> None:
    """atexit hook — close the thread-local conn on process shutdown."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


import atexit as _atexit
_atexit.register(_close_thread_db_conn)


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = f"""You are {AGENT_NAME}, the engineering manager's chief of staff
for the Fantasy team dashboard.

Your job: answer questions about the team, the sprint, PRs, ticket hygiene,
and what the automation has been doing, using the tools available to you.

How you talk:
- Answer directly. No preamble, no "great question," no recap of the question.
- Be brief by default. One or two sentences unless asked for detail.
- Call tools when you need fresh data. Don't guess.
- Volunteer caveats when the data is weak, stale, or small-sample.
- Say "I don't know" and point to which tool would have the answer
  when you can't answer from what's in context.
- Blunt about systemic problems (stale data, blocked tickets). Softer
  when delivering feedback about specific people — frame as questions
  to investigate, not verdicts.
- Never invent names, metrics, or ticket keys. Only report what the
  tools return or the user told you.
- No sign-offs ("Let me know if you need anything!"). End when the answer ends.
- No emoji unless the user uses them first, with rare exceptions for
  ⚠ or 🚨 when something genuinely needs attention.

Who you're talking to: an engineering manager who knows the team,
codebase, and Jira. Skip explanations of jargon they already use.

Current date: {datetime.now().strftime("%B %d, %Y")}."""


# ---------------------------------------------------------------------------
# Tool implementations (read-only)
# ---------------------------------------------------------------------------

def _lookup_jira_id(name: str) -> Optional[str]:
    """Resolve a display name in team_config.yaml to its jira_account_id."""
    try:
        import yaml
        cfg = yaml.safe_load((CONFIG_DIR / "team_config.yaml").read_text()) or {}
    except Exception:
        return None
    low = name.strip().lower()
    for m in cfg.get("team_members", []) or []:
        if (m.get("name") or "").strip().lower() == low:
            return m.get("jira_account_id") or None
    return None


def _tool_get_team_roster(_args: Dict[str, Any]) -> Dict[str, Any]:
    """List every team member from team_config.yaml with their levels."""
    import yaml
    cfg_path = CONFIG_DIR / "team_config.yaml"
    if not cfg_path.exists():
        return {"error": "team_config.yaml not found"}
    cfg = yaml.safe_load(cfg_path.read_text()) or {}
    members = cfg.get("team_members", [])
    return {
        "count": len(members),
        "members": [
            {
                "name": m.get("name"),
                "level": m.get("level") or None,
                "github_username": m.get("github_username") or None,
                "jira_account_id": m.get("jira_account_id") or None,
            }
            for m in members
        ],
    }


def _tool_get_sprint_summary(_args: Dict[str, Any]) -> Dict[str, Any]:
    """Current sprint + tickets by bucket + story-point totals."""
    with _db_conn() as conn:
        cur = conn.cursor()
        # Prefer an active sprint; fall back to most recent by start_date.
        cur.execute(
            "SELECT sprint_id, sprint_name, start_date, end_date, state "
            "FROM sprints WHERE sprint_name LIKE 'FNTSY%' "
            "ORDER BY (CASE WHEN lower(state) = 'active' THEN 0 ELSE 1 END), "
            "         start_date DESC LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            return {"error": "no FNTSY sprints found"}
        sprint = dict(row)

        cur.execute(
            """SELECT status, COUNT(*) AS n, COALESCE(SUM(story_points),0) AS sp
               FROM tickets
               WHERE sprint_id = ?
                 AND issue_type IN ('Story','Task','Bug','Sub-task','Subtask')
               GROUP BY status""",
            (sprint["sprint_id"],),
        )
        by_status = [dict(r) for r in cur.fetchall()]

    return {"sprint": sprint, "by_status": by_status}


def _tool_get_member_metrics(args: Dict[str, Any]) -> Dict[str, Any]:
    """Per-member sprint metrics. args: name (required)."""
    name = (args.get("name") or "").strip()
    if not name:
        return {"error": "name required"}
    with _db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT sprint_id, sprint_name FROM sprints "
            "WHERE sprint_name LIKE 'FNTSY%' "
            "ORDER BY (CASE WHEN lower(state) = 'active' THEN 0 ELSE 1 END), "
            "         start_date DESC LIMIT 1"
        )
        s = cur.fetchone()
        if not s:
            return {"error": "no FNTSY sprint found"}
        sprint_id = s["sprint_id"]

        # Match by display name OR by jira_account_id resolved from team_config.
        jira_id = _lookup_jira_id(name)
        if jira_id:
            cur.execute(
                """SELECT status, issue_type, story_points, ticket_key, summary
                   FROM tickets
                   WHERE sprint_id = ?
                     AND (assignee_display_name = ? OR assignee_account_id = ?)
                     AND issue_type IN ('Story','Task','Bug','Sub-task','Subtask')""",
                (sprint_id, name, jira_id),
            )
        else:
            cur.execute(
                """SELECT status, issue_type, story_points, ticket_key, summary
                   FROM tickets
                   WHERE sprint_id = ? AND assignee_display_name = ?
                     AND issue_type IN ('Story','Task','Bug','Sub-task','Subtask')""",
                (sprint_id, name),
            )
        rows = [dict(r) for r in cur.fetchall()]

    if not rows:
        return {"sprint": s["sprint_name"], "name": name,
                "note": "no tickets in current sprint"}
    total_sp = sum((r.get("story_points") or 0) for r in rows)
    by_status: Dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "sprint": s["sprint_name"],
        "name": name,
        "total_tickets": len(rows),
        "total_story_points": total_sp,
        "by_status": by_status,
        "tickets": [
            {"key": r["ticket_key"], "status": r["status"],
             "issue_type": r["issue_type"],
             "story_points": r["story_points"],
             "summary": r["summary"]}
            for r in rows[:50]
        ],
    }


def _tool_get_hygiene_issues(args: Dict[str, Any]) -> Dict[str, Any]:
    """Currently-open hygiene issues, optionally filtered by type.
    args: issue_type (optional); limit (optional, default 50, max 200)."""
    itype = args.get("issue_type")
    limit = min(int(args.get("limit", 50) or 50), 200)
    with _db_conn() as conn:
        cur = conn.cursor()
        if itype:
            cur.execute(
                """SELECT issue_type, ticket_key, ticket_summary, ticket_url,
                          assignee_display_name, status, first_seen_at,
                          times_seen, times_resolved
                   FROM hygiene_issues
                   WHERE resolved_at IS NULL AND issue_type = ?
                   ORDER BY first_seen_at ASC LIMIT ?""",
                (itype, limit),
            )
        else:
            cur.execute(
                """SELECT issue_type, ticket_key, ticket_summary, ticket_url,
                          assignee_display_name, status, first_seen_at,
                          times_seen, times_resolved
                   FROM hygiene_issues
                   WHERE resolved_at IS NULL
                   ORDER BY first_seen_at ASC LIMIT ?""",
                (limit,),
            )
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT issue_type, COUNT(*) AS n FROM hygiene_issues "
            "WHERE resolved_at IS NULL GROUP BY issue_type"
        )
        counts = {r["issue_type"]: r["n"] for r in cur.fetchall()}
    return {"counts_by_type": counts, "sample": rows}


def _tool_get_recent_agent_runs(args: Dict[str, Any]) -> Dict[str, Any]:
    """Last N entries from data/qa_runs.jsonl, optionally filtered by
    source_agent. args: source_agent ("qa" or "hygiene"); limit (default 10)."""
    path = DATA_DIR / "qa_runs.jsonl"
    if not path.exists():
        return {"runs": []}
    limit = min(int(args.get("limit", 10) or 10), 40)
    source = args.get("source_agent")
    runs = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        rec_source = (rec.get("summary", {}) or {}).get("source_agent", "qa")
        if source and rec_source != source:
            continue
        runs.append({
            "run_id": rec.get("run_id"),
            "source_agent": rec_source,
            "started_at": rec.get("started_at"),
            "duration_s": rec.get("duration_s"),
            "summary": rec.get("summary"),
            "check_count": len(rec.get("checks", [])),
            "fix_count": len(rec.get("fixes", [])),
        })
    return {"runs": runs[-limit:][::-1]}


def _tool_get_github_pr_summary(args: Dict[str, Any]) -> Dict[str, Any]:
    """Open + recently-merged PR summary. args: days (default 30)."""
    days = min(int(args.get("days", 30) or 30), 180)
    cutoff = (datetime.now(timezone.utc).replace(hour=0, minute=0, second=0)
              .isoformat())
    with _db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT author_github_username, COUNT(*) AS open_count
               FROM github_prs
               WHERE state = 'open'
               GROUP BY author_github_username
               ORDER BY open_count DESC"""
        )
        open_by_author = [dict(r) for r in cur.fetchall()]
        cur.execute(
            """SELECT COUNT(*) AS merged_n,
                      AVG((julianday(merged_at) - julianday(created_at))*24) AS avg_hours_to_merge
               FROM github_prs
               WHERE state = 'merged'
                 AND merged_at >= datetime('now', ?)""",
            (f"-{days} days",),
        )
        merged = dict(cur.fetchone())
    return {
        "window_days": days,
        "open_by_author": open_by_author[:50],
        "merged": merged,
    }


def _tool_get_one_on_ones(_args: Dict[str, Any]) -> Dict[str, Any]:
    """List captured 1:1 meeting schedules."""
    with _db_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT developer_name, day_of_week, time_of_day,
                      duration_minutes, next_occurrence, last_synced_at
               FROM one_on_one_meetings
               ORDER BY developer_name"""
        )
        rows = [dict(r) for r in cur.fetchall()]
    return {"count": len(rows), "meetings": rows}


def _tool_get_project_snapshot(_args: Dict[str, Any]) -> Dict[str, Any]:
    """Project: Fantasy snapshot: initiative + rolled-up counts."""
    p = DATA_DIR / "project_fantasy.json"
    if not p.exists():
        return {"error": "no project_fantasy.json — run sync_project_fantasy.py"}
    d = json.loads(p.read_text())
    out = {
        "generated_at": d.get("generated_at"),
        "initiative": d.get("initiative", {}),
        "summary": d.get("summary", {}),
        "at_risk_count": len(d.get("at_risk", [])),
        "feature_count": len(d.get("features", [])),
        "epic_count": len(d.get("epics", [])),
        "story_count": len(d.get("stories", [])),
    }
    return out


def _tool_get_staleness(_args: Dict[str, Any]) -> Dict[str, Any]:
    """How old is each data source we depend on."""
    out = {}
    with _db_conn() as conn:
        cur = conn.cursor()
        # Last developer snapshot
        cur.execute("SELECT MAX(snapshot_timestamp) FROM developer_snapshots")
        out["developer_snapshots_latest"] = cur.fetchone()[0]
        # Last GitHub PR write
        cur.execute("SELECT MAX(updated_at) FROM github_prs")
        out["github_prs_latest"] = cur.fetchone()[0]
        # Last hygiene sweep
        cur.execute("SELECT MAX(last_seen_at) FROM hygiene_issues")
        out["hygiene_last_sweep"] = cur.fetchone()[0]
    # Last calendar sync
    try:
        with _db_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(last_synced_at) FROM one_on_one_meetings")
            out["calendar_last_sync"] = cur.fetchone()[0]
    except Exception:
        out["calendar_last_sync"] = None
    # Last project snapshot
    p = DATA_DIR / "project_fantasy.json"
    if p.exists():
        try:
            out["project_snapshot"] = json.loads(p.read_text()).get("generated_at")
        except Exception:
            pass
    return out


# ---------------------------------------------------------------------------
# Tool catalog (exposed to the model)
# ---------------------------------------------------------------------------

TOOLS: List[Dict[str, Any]] = [
    {
        "name": "get_team_roster",
        "description": "Return the list of team members from team_config.yaml.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_sprint_summary",
        "description": ("Current FNTSY sprint + ticket counts by status + "
                        "story-point totals."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_member_metrics",
        "description": ("Per-member metrics for the current sprint: ticket "
                        "counts by status, story points, and ticket list. "
                        "Use this when asked about a specific person."),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": ("Exact display name as it appears in "
                                    "team_config.yaml (e.g. 'Jacob Tabak')."),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_hygiene_issues",
        "description": ("Currently-open Jira hygiene issues. Optionally "
                        "filter by issue_type (e.g. 'stories_no_points', "
                        "'epics_no_description')."),
        "input_schema": {
            "type": "object",
            "properties": {
                "issue_type": {"type": "string"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 200},
            },
            "required": [],
        },
    },
    {
        "name": "get_recent_agent_runs",
        "description": ("Recent QA and Hygiene agent runs from "
                        "data/qa_runs.jsonl. Useful when asked 'what did "
                        "the agents do recently' or for data freshness."),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_agent": {
                    "type": "string",
                    "enum": ["qa", "hygiene"],
                    "description": "Filter to one agent (optional).",
                },
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 40},
            },
            "required": [],
        },
    },
    {
        "name": "get_github_pr_summary",
        "description": ("GitHub PR overview: open PRs per author + merged "
                        "count / average merge time over the last N days."),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "default": 30, "minimum": 1, "maximum": 180},
            },
            "required": [],
        },
    },
    {
        "name": "get_one_on_ones",
        "description": "Captured 1:1 meeting schedules from Google Calendar.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_project_snapshot",
        "description": ("Project: Fantasy (INIT-185) rolled-up counts + "
                        "at-risk feature list."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_staleness",
        "description": ("Timestamps for each data source we depend on. "
                        "Use when asked how fresh the data is."),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]


TOOL_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "get_team_roster":        _tool_get_team_roster,
    "get_sprint_summary":     _tool_get_sprint_summary,
    "get_member_metrics":     _tool_get_member_metrics,
    "get_hygiene_issues":     _tool_get_hygiene_issues,
    "get_recent_agent_runs":  _tool_get_recent_agent_runs,
    "get_github_pr_summary":  _tool_get_github_pr_summary,
    "get_one_on_ones":        _tool_get_one_on_ones,
    "get_project_snapshot":   _tool_get_project_snapshot,
    "get_staleness":          _tool_get_staleness,
}


def _run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if not handler:
        return {"error": f"unknown tool: {name}"}
    try:
        return handler(args or {})
    except Exception as e:
        logger.exception("tool %s failed", name)
        return {"error": f"tool raised: {e}"}


# ---------------------------------------------------------------------------
# HTTP call to Anthropic
# ---------------------------------------------------------------------------

def _call_model(api_key: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    payload = {
        "model": DEFAULT_MODEL,
        "max_tokens": MODEL_MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": TOOLS,
        "messages": messages,
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"Anthropic API {resp.status_code}: {resp.text[:500]}")
    return resp.json()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def ask(question: str, history: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Answer a question. Returns {reply, tool_calls, history_after}.

    `history` is the client-held conversation transcript (list of
    Claude-style messages: {"role": "user"|"assistant", "content": ...}).
    The client is responsible for passing it back each turn.
    """
    question = (question or "").strip()
    if not question:
        return {"reply": "Give me a question.", "tool_calls": [], "history_after": history or []}

    api_key = get_api_key()
    if not api_key:
        return {
            "reply": (f"{AGENT_NAME} needs an Anthropic API key. "
                      f"Add `ANTHROPIC_API_KEY=sk-...` to `config/.env` "
                      f"and restart the dashboard server."),
            "tool_calls": [],
            "history_after": history or [],
            "missing_api_key": True,
        }

    # Build the messages list: prior history + this turn's user message.
    messages: List[Dict[str, Any]] = list(history or [])
    messages.append({"role": "user", "content": question})

    tool_calls_trace: List[Dict[str, Any]] = []
    final_text = ""

    for turn in range(MAX_TOOL_TURNS):
        resp = _call_model(api_key, messages)
        stop = resp.get("stop_reason")
        content = resp.get("content") or []

        # Collect assistant output blocks
        assistant_blocks: List[Dict[str, Any]] = []
        tool_uses: List[Dict[str, Any]] = []
        for block in content:
            btype = block.get("type")
            if btype == "text":
                assistant_blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype == "tool_use":
                tool_uses.append(block)
                assistant_blocks.append({
                    "type": "tool_use",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "input": block.get("input", {}),
                })

        # Persist the assistant turn exactly as returned, per Anthropic API spec
        messages.append({"role": "assistant", "content": assistant_blocks})

        if stop != "tool_use" or not tool_uses:
            # Final answer. Concatenate text blocks.
            final_text = "\n".join(
                b.get("text", "") for b in assistant_blocks if b.get("type") == "text"
            ).strip()
            break

        # Run the requested tools, appending a user message with tool_result blocks
        tool_results: List[Dict[str, Any]] = []
        for call in tool_uses:
            name = call.get("name", "")
            args = call.get("input", {}) or {}
            result = _run_tool(name, args)
            tool_calls_trace.append({
                "name": name,
                "input": args,
                "output_preview": _preview(result),
            })
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": json.dumps(result, default=str)[:6000],
            })
        messages.append({"role": "user", "content": tool_results})
    else:
        # Hit the turn cap without converging
        final_text = (final_text or
                      f"I called tools several times without reaching an answer. "
                      f"Try rephrasing or narrowing the question.")

    return {
        "reply": final_text,
        "tool_calls": tool_calls_trace,
        "history_after": messages,
    }


def _preview(obj: Any, limit: int = 240) -> str:
    try:
        s = json.dumps(obj, default=str)
    except Exception:
        s = str(obj)
    return s[:limit] + ("…" if len(s) > limit else "")
