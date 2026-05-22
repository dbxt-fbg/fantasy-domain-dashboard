"""Agent-runtime infrastructure for the QA Agent.

This module is the new engine that makes `qa_agent.py` behave like a real
agent instead of a fixed pipeline. It provides:

  * Check         — a declarative record describing one invariant
  * CheckResult   — the typed outcome of running a check
  * RunReport     — structured per-run history persisted to qa_runs.jsonl
  * HistoryStore  — sqlite-backed memory of issues and fix outcomes
  * Planner       — picks which checks to run based on state-hash,
                    dependencies, cost budget, and flake/aging signals
  * Verifier      — re-runs a check after a fix, flags regressions
  * ProposalQueue — HITL approvals stored in qa_proposed_actions.jsonl
  * ToolCatalog   — first-class fix tools with arg schemas and rollback

The QA agent wires these together but keeps its 100 check *bodies* in
qa_agent.py; only the orchestration lives here. That keeps the blast
radius small and lets us evolve the agent layer without touching every
check.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = REPO_ROOT / "data"

RUN_LOG_PATH = DATA_DIR / "qa_runs.jsonl"
PROPOSALS_PATH = DATA_DIR / "qa_proposed_actions.jsonl"
HISTORY_DB_PATH = DATA_DIR / "qa_history.sqlite"


# ---------------------------------------------------------------------------
# Check / Result data classes
# ---------------------------------------------------------------------------

@dataclass
class Check:
    """Declarative description of one invariant.

    Attributes:
        key: stable id (e.g. "sp_math_balances"). Used for memory/state-hash.
        invariant: human-readable sentence stating what must remain true.
        fn: callable (no args) that runs the check; side-effect writes
            issues onto the agent instance that owns it.
        depends_on: list of other Check keys that must pass before this one
            is worth running. A failed upstream suppresses downstream work
            and surfaces the root cause in the RunReport.
        state_hash_fn: optional no-arg callable returning a short string
            that digests the inputs this check reads. If the digest hasn't
            changed since the last passing run, the planner may skip this
            check.
        estimated_cost_s: rough seconds-per-run, used for budgeting.
        tags: free-form labels ("fast", "hygiene", "deep") that let the
            planner (or CLI flags) include/exclude groups.
    """

    key: str
    invariant: str
    fn: Callable[[], None]
    depends_on: Sequence[str] = field(default_factory=tuple)
    state_hash_fn: Optional[Callable[[], str]] = None
    estimated_cost_s: float = 1.0
    tags: Sequence[str] = field(default_factory=tuple)
    # Some checks are *designed* to oscillate pass/fail with the underlying
    # state — html_reports passes when fresh, fails when stale; that pattern
    # is the signal we want to monitor, not flakiness. Setting this True
    # exempts the check from the planner's flake suppression.
    benign_alternator: bool = False


@dataclass
class CheckResult:
    """Typed outcome of running a single Check."""

    key: str
    started_at: str
    duration_s: float
    status: str  # "passed" | "failed" | "skipped" | "error" | "deferred"
    issues_count: int
    reason: str = ""  # why skipped / deferred / errored


# ---------------------------------------------------------------------------
# Run reports (task #55, #8)
# ---------------------------------------------------------------------------

@dataclass
class RunReport:
    """Structured record of one agent run. Written to qa_runs.jsonl."""

    run_id: str
    started_at: str
    finished_at: str = ""
    duration_s: float = 0.0
    git_sha: str = ""

    # Which checks ran and how they turned out
    checks: List[Dict[str, Any]] = field(default_factory=list)
    # Fixes applied, with plan/act/verify outcomes
    fixes: List[Dict[str, Any]] = field(default_factory=list)
    # Summary
    summary: Dict[str, Any] = field(default_factory=dict)
    # Budget accounting
    budget: Dict[str, Any] = field(default_factory=dict)
    # Actions deferred to HITL approval
    proposals: List[Dict[str, Any]] = field(default_factory=list)

    def write(self, path: Path = RUN_LOG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Append, one JSON object per line, for easy tailing.
        with path.open("a") as f:
            f.write(json.dumps(asdict(self)) + "\n")


def make_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# History store (task #56, #4)
# ---------------------------------------------------------------------------

HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS issue_history (
    issue_key     TEXT PRIMARY KEY,  -- e.g. "sp_math_balances:sprint-237"
    check_key     TEXT NOT NULL,
    severity      TEXT NOT NULL,
    message       TEXT NOT NULL,
    first_seen    TEXT NOT NULL,     -- ISO
    last_seen     TEXT NOT NULL,     -- ISO
    last_resolved TEXT,               -- ISO; null if never resolved
    seen_count    INTEGER NOT NULL DEFAULT 1,
    resolved_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fix_history (
    action_hash   TEXT NOT NULL,
    issue_key     TEXT NOT NULL,
    applied_at    TEXT NOT NULL,
    verified_at   TEXT,
    outcome       TEXT NOT NULL,     -- "verified" | "failed" | "reverted" | "pending"
    detail        TEXT,
    PRIMARY KEY (action_hash, applied_at)
);

CREATE TABLE IF NOT EXISTS check_runs (
    check_key    TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    status       TEXT NOT NULL,     -- "passed" | "failed" | "skipped" | "error" | "deferred"
    issues_count INTEGER NOT NULL DEFAULT 0,
    state_hash   TEXT,
    duration_s   REAL,
    PRIMARY KEY (check_key, run_id)
);

CREATE TABLE IF NOT EXISTS rejected_proposals (
    proposal_fingerprint TEXT PRIMARY KEY,
    rejected_at          TEXT NOT NULL,
    reason               TEXT
);

CREATE INDEX IF NOT EXISTS idx_issue_history_check ON issue_history(check_key);
CREATE INDEX IF NOT EXISTS idx_fix_history_issue ON fix_history(issue_key);
CREATE INDEX IF NOT EXISTS idx_check_runs_key ON check_runs(check_key, started_at DESC);
"""


class HistoryStore:
    """Cross-run memory for the QA agent."""

    def __init__(self, path: Path = HISTORY_DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(HISTORY_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 15000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ----- issue tracking -------------------------------------------------
    @staticmethod
    def issue_key(check_key: str, message: str) -> str:
        """A stable key identifying an issue across runs.

        We digest the check_key + message together so that a rephrased
        message produces a new row (wanted — the intent changed); same
        message on same check = same row.
        """
        h = hashlib.sha1(f"{check_key}|{message}".encode()).hexdigest()[:12]
        return f"{check_key}:{h}"

    def upsert_issue(self, check_key: str, severity: str, message: str) -> str:
        """Record an issue seen this run. Returns its issue_key."""
        key = self.issue_key(check_key, message)
        now = _now_iso()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT issue_key, seen_count FROM issue_history WHERE issue_key = ?",
                (key,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE issue_history
                       SET last_seen = ?, seen_count = seen_count + 1,
                           severity = ?, message = ?
                       WHERE issue_key = ?""",
                    (now, severity, message, key),
                )
            else:
                conn.execute(
                    """INSERT INTO issue_history (issue_key, check_key, severity,
                            message, first_seen, last_seen, seen_count, resolved_count)
                       VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
                    (key, check_key, severity, message, now, now),
                )
        return key

    def mark_resolved(self, issue_key: str) -> None:
        now = _now_iso()
        with self._conn() as conn:
            conn.execute(
                """UPDATE issue_history
                   SET last_resolved = ?, resolved_count = resolved_count + 1
                   WHERE issue_key = ?""",
                (now, issue_key),
            )

    def open_issue_keys_for_check(self, check_key: str) -> List[str]:
        """Issues seen at least once that haven't been marked resolved more
        times than they've been seen. Used to detect 'disappeared' issues
        so we can auto-resolve them."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT issue_key FROM issue_history
                   WHERE check_key = ?
                     AND (last_resolved IS NULL
                          OR datetime(last_resolved) < datetime(last_seen))""",
                (check_key,),
            ).fetchall()
        return [r["issue_key"] for r in rows]

    def issue_age_days(self, issue_key: str) -> Optional[int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT first_seen FROM issue_history WHERE issue_key = ?",
                (issue_key,),
            ).fetchone()
        if not row:
            return None
        first = datetime.fromisoformat(row["first_seen"])
        return (datetime.now(timezone.utc) - first).days

    # ----- fix tracking ---------------------------------------------------
    @staticmethod
    def action_hash(action: Dict[str, Any]) -> str:
        blob = json.dumps(action, sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def record_fix_attempt(self, action: Dict[str, Any], issue_key: str) -> str:
        """Insert a pending fix row; returns its action_hash."""
        h = self.action_hash(action)
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO fix_history
                       (action_hash, issue_key, applied_at, outcome)
                   VALUES (?, ?, ?, 'pending')""",
                (h, issue_key, _now_iso()),
            )
        return h

    def record_fix_outcome(self, action_hash: str, issue_key: str,
                            outcome: str, detail: str = "") -> None:
        """Update the most-recent pending row for (action_hash, issue_key)."""
        with self._conn() as conn:
            conn.execute(
                """UPDATE fix_history
                   SET verified_at = ?, outcome = ?, detail = ?
                   WHERE action_hash = ? AND issue_key = ?
                     AND outcome = 'pending'""",
                (_now_iso(), outcome, detail, action_hash, issue_key),
            )

    def recent_reverts_for_action(self, action: Dict[str, Any],
                                    lookback_days: int = 14) -> int:
        """Count how many times this action has been applied and subsequently
        seen the issue reappear. If > threshold, the planner can stop retrying.
        """
        h = self.action_hash(action)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS n FROM fix_history
                   WHERE action_hash = ? AND applied_at >= ?
                     AND outcome IN ('reverted','failed')""",
                (h, cutoff),
            ).fetchone()
        return int(row["n"] or 0)

    # ----- check runs (for flake detection, aging) ------------------------
    def record_check_run(self, check_key: str, run_id: str,
                          started_at: str, status: str,
                          issues_count: int, state_hash: str = "",
                          duration_s: float = 0.0) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO check_runs
                       (check_key, run_id, started_at, status,
                        issues_count, state_hash, duration_s)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (check_key, run_id, started_at, status, issues_count, state_hash, duration_s),
            )

    def last_passing_state_hash(self, check_key: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT state_hash FROM check_runs
                   WHERE check_key = ? AND status = 'passed' AND state_hash != ''
                   ORDER BY started_at DESC LIMIT 1""",
                (check_key,),
            ).fetchone()
        return row["state_hash"] if row and row["state_hash"] else None

    def recent_check_statuses(self, check_key: str, n: int = 10) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT status FROM check_runs
                   WHERE check_key = ?
                   ORDER BY started_at DESC LIMIT ?""",
                (check_key, n),
            ).fetchall()
        return [r["status"] for r in rows]

    def is_flaky(self, check_key: str, window: int = 10, flake_threshold: int = 5) -> bool:
        """A check is flaky if it has flipped pass/fail >= flake_threshold
        times in the last `window` runs.

        Threshold raised from 3 to 5 (≥2.5 cycles in 10 runs) because real
        state-tracking checks like html_reports legitimately alternate
        between pass (fresh) and fail (stale) on a normal cron schedule —
        the lower threshold was suppressing them within hours of activity,
        which is the opposite of what we want. Use Check.benign_alternator
        to fully exempt a check whose state changes are the signal.
        """
        statuses = self.recent_check_statuses(check_key, window)
        if len(statuses) < 4:
            return False
        flips = 0
        for a, b in zip(statuses, statuses[1:]):
            if (a == "passed") != (b == "passed"):
                flips += 1
        return flips >= flake_threshold

    # ----- rejected proposals --------------------------------------------
    def is_proposal_rejected(self, fingerprint: str,
                              cooldown_days: int = 90) -> bool:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """SELECT 1 FROM rejected_proposals
                   WHERE proposal_fingerprint = ? AND rejected_at >= ?""",
                (fingerprint, cutoff),
            ).fetchone()
        return row is not None

    def record_proposal_rejection(self, fingerprint: str, reason: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO rejected_proposals
                       (proposal_fingerprint, rejected_at, reason)
                   VALUES (?, ?, ?)""",
                (fingerprint, _now_iso(), reason),
            )


# ---------------------------------------------------------------------------
# Planner (task #58, #59, #61)
# ---------------------------------------------------------------------------

@dataclass
class PlanDecision:
    check: Check
    decision: str  # "run" | "skip_state_hash" | "skip_upstream_failed" | "defer_budget" | "skip_flaky"
    reason: str = ""
    state_hash: str = ""


class Planner:
    """Decides which checks to run and in what order.

    Uses:
      * dependency DAG for topological order
      * state-hash gating (skip if inputs unchanged since last pass)
      * time budget (defer expensive checks once budget is exhausted)
      * flake suppression (skip flaky checks unless user asked explicitly)
    """

    def __init__(self, checks: Sequence[Check], history: HistoryStore,
                 budget_s: Optional[float] = None,
                 force_all: bool = False,
                 include_flaky: bool = False):
        self.checks = list(checks)
        self.history = history
        self.budget_s = budget_s
        self.force_all = force_all  # bypass state-hash gating
        self.include_flaky = include_flaky

    def _topo_order(self) -> List[Check]:
        by_key = {c.key: c for c in self.checks}
        # Kahn's algorithm
        indegree = {c.key: 0 for c in self.checks}
        for c in self.checks:
            for dep in c.depends_on:
                if dep in by_key:
                    indegree[c.key] += 1
        queue = [c for c in self.checks if indegree[c.key] == 0]
        out: List[Check] = []
        while queue:
            # Stable: preserve declaration order within a level
            node = queue.pop(0)
            out.append(node)
            for other in self.checks:
                if node.key in other.depends_on:
                    indegree[other.key] -= 1
                    if indegree[other.key] == 0:
                        queue.append(other)
        if len(out) != len(self.checks):
            # Cycle — fall back to declaration order so we still run everything
            logger.warning("Planner: dependency cycle detected; falling back to declaration order")
            return self.checks
        return out

    def plan(self, failed_keys: Optional[set] = None) -> List[PlanDecision]:
        """Produce a plan. `failed_keys` is the running set of upstream checks
        that already failed in this run — called incrementally by the agent
        as it executes."""
        failed_keys = failed_keys or set()
        decisions: List[PlanDecision] = []
        budget_used = 0.0

        for c in self._topo_order():
            # 1) upstream suppression
            if any(dep in failed_keys for dep in c.depends_on):
                decisions.append(PlanDecision(c, "skip_upstream_failed",
                    reason=f"upstream failed: {','.join(d for d in c.depends_on if d in failed_keys)}"))
                continue

            # 2) flake suppression (unless overridden, or check is opted out).
            # benign_alternator=True checks always run because their pass/fail
            # state is itself the thing we're trying to observe.
            if (not self.include_flaky
                    and not getattr(c, 'benign_alternator', False)
                    and self.history.is_flaky(c.key)):
                decisions.append(PlanDecision(c, "skip_flaky",
                    reason="recent pass/fail thrashing — run with --include-flaky to force"))
                continue

            # 3) state-hash gating
            state_hash = ""
            if c.state_hash_fn and not self.force_all:
                try:
                    state_hash = c.state_hash_fn()
                except Exception as e:
                    logger.debug("state_hash_fn failed for %s: %s", c.key, e)
                    state_hash = ""
                if state_hash:
                    last = self.history.last_passing_state_hash(c.key)
                    if last == state_hash:
                        decisions.append(PlanDecision(c, "skip_state_hash",
                            reason=f"inputs unchanged since last pass ({state_hash[:8]})",
                            state_hash=state_hash))
                        continue

            # 4) budget
            if self.budget_s is not None and (budget_used + c.estimated_cost_s) > self.budget_s:
                decisions.append(PlanDecision(c, "defer_budget",
                    reason=f"estimated cost {c.estimated_cost_s:.1f}s exceeds remaining budget",
                    state_hash=state_hash))
                continue

            budget_used += c.estimated_cost_s
            decisions.append(PlanDecision(c, "run", state_hash=state_hash))

        return decisions


# ---------------------------------------------------------------------------
# Proposal queue (task #62)
# ---------------------------------------------------------------------------

class ProposalQueue:
    """HITL queue for fix proposals that are too risky for auto-apply.

    Proposals are appended to data/qa_proposed_actions.jsonl. Each has a
    stable `fingerprint` so rejecting one prevents re-proposal for 90 days.
    """

    def __init__(self, history: HistoryStore, path: Path = PROPOSALS_PATH):
        self.history = history
        self.path = path

    def proposal_fingerprint(self, action: Dict[str, Any], issue_message: str) -> str:
        blob = json.dumps({"action": action, "message": issue_message},
                          sort_keys=True, default=str)
        return hashlib.sha1(blob.encode()).hexdigest()[:12]

    def propose(self, action: Dict[str, Any], issue: Dict[str, Any],
                 confidence: float = 0.5, rationale: str = "") -> Optional[str]:
        """Write a proposal. Returns the fingerprint, or None if already rejected."""
        fp = self.proposal_fingerprint(action, issue.get("message", ""))
        if self.history.is_proposal_rejected(fp):
            return None
        record = {
            "fingerprint": fp,
            "proposed_at": _now_iso(),
            "action": action,
            "issue": {
                "check_key": issue.get("category", ""),
                "severity": issue.get("severity", ""),
                "message": issue.get("message", ""),
            },
            "confidence": confidence,
            "rationale": rationale,
            "status": "pending",
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        return fp

    def list_pending(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        seen_fps = set()
        # The jsonl is append-only, so walk it in reverse to find the latest
        # entry per fingerprint.
        for line in reversed(self.path.read_text().splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            fp = rec.get("fingerprint")
            if not fp or fp in seen_fps:
                continue
            seen_fps.add(fp)
            if rec.get("status") == "pending":
                out.append(rec)
        return list(reversed(out))

    def resolve(self, fingerprint: str, status: str, reason: str = "") -> bool:
        """Append a resolution record. status in {'approved','rejected'}."""
        if status not in {"approved", "rejected"}:
            raise ValueError(f"bad status: {status}")
        record = {
            "fingerprint": fingerprint,
            "resolved_at": _now_iso(),
            "status": status,
            "reason": reason,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")
        if status == "rejected":
            self.history.record_proposal_rejection(fingerprint, reason)
        return True

    def pop_approved(self) -> List[Dict[str, Any]]:
        """Return approved proposals that haven't been executed yet.

        Execution state is tracked by writing a follow-up 'executed' record
        to the same jsonl; here we return approved proposals whose
        fingerprint doesn't have a later 'executed' or 'rejected' entry.
        """
        if not self.path.exists():
            return []
        final_state: Dict[str, Dict[str, Any]] = {}
        records_by_fp: Dict[str, Dict[str, Any]] = {}
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            fp = rec.get("fingerprint")
            if not fp:
                continue
            final_state[fp] = rec
            # Keep the original proposal record with its action payload
            if "action" in rec:
                records_by_fp[fp] = rec
        return [
            records_by_fp[fp]
            for fp, rec in final_state.items()
            if rec.get("status") == "approved" and fp in records_by_fp
        ]

    def mark_executed(self, fingerprint: str, outcome: str, detail: str = "") -> None:
        record = {
            "fingerprint": fingerprint,
            "executed_at": _now_iso(),
            "status": "executed",
            "outcome": outcome,
            "detail": detail,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Tool catalog (task #63)
# ---------------------------------------------------------------------------

@dataclass
class ToolSpec:
    """First-class spec for a fix tool the engine can invoke."""

    name: str
    description: str
    handler: Callable[[Dict[str, Any]], str]  # returns human-readable result
    cost_estimate_s: float = 5.0
    idempotent: bool = True
    # Arg schema: arg_name -> (required, description)
    args_schema: Dict[str, Tuple[bool, str]] = field(default_factory=dict)


class ToolCatalog:
    """Registry of tools the FixEngine can pick from. Each tool knows its
    cost, idempotency, and expected args."""

    def __init__(self):
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def list_names(self) -> List[str]:
        return sorted(self._tools.keys())

    def describe_all(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "idempotent": s.idempotent,
                "cost_estimate_s": s.cost_estimate_s,
                "args": {k: {"required": req, "description": desc}
                          for k, (req, desc) in s.args_schema.items()},
            }
            for s in self._tools.values()
        ]


# ---------------------------------------------------------------------------
# Verifier (task #57)
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    outcome: str  # "verified" | "failed" | "regressed"
    detail: str = ""


class Verifier:
    """Re-runs a check after a fix to confirm the invariant now holds."""

    def __init__(self, check_by_key: Dict[str, Check]):
        self.check_by_key = check_by_key

    def verify(self, check_key: str, reset_issues: Callable[[], None],
                count_issues_for_key: Callable[[str], int]) -> VerifyResult:
        """Run the single check identified by `check_key`.

        `reset_issues` clears any issues recorded by the check from the agent
        so we only count what this verification pass emits.
        `count_issues_for_key` returns how many issues the check emitted.
        """
        check = self.check_by_key.get(check_key)
        if not check:
            return VerifyResult("failed", f"unknown check {check_key}")
        reset_issues()
        try:
            check.fn()
        except Exception as e:
            return VerifyResult("failed", f"verify check raised: {e}")
        remaining = count_issues_for_key(check_key)
        if remaining == 0:
            return VerifyResult("verified", "check now passes")
        return VerifyResult("regressed", f"{remaining} issue(s) still present after fix")
