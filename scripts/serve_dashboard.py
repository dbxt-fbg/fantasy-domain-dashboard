#!/usr/bin/env python3
"""HTTP server for the dashboard.

Serves static files from reports/html/ on the local network. No auth —
this is a single-user LAN server by design. Write methods other than
a single narrow /api/member endpoint are blocked as a safety rail.

Usage:
  python3 scripts/serve_dashboard.py [--port 8080] [--host 0.0.0.0]

Bound to 0.0.0.0 by default so the LAN can reach it. On first launch macOS
will prompt to allow incoming connections; click Allow.
"""

import argparse
import json
import logging
import os
import re
import socket
import subprocess
import sys
import threading
import uuid
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger("serve_dashboard")


REPO_ROOT = Path(__file__).parent.parent
REPORTS_DIR = REPO_ROOT / "reports" / "html"
CONFIG_PATH = REPO_ROOT / "config" / "team_config.yaml"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_html_report.py"

def _initiative_key() -> str:
    """Top-level initiative key (jira.initiative_key) for UI labels. Read
    directly from the config file so this stays a dependency-free module;
    falls back to INIT-185 if config can't be read."""
    try:
        import yaml as _yaml
        cfg = _yaml.safe_load(CONFIG_PATH.read_text()) or {}
        return (cfg.get('jira') or {}).get('initiative_key') or "INIT-185"
    except Exception:
        return "INIT-185"


# Pipeline for the "Refresh Data" button. Each step pulls a fresh slice of
# Jira data and the final step regenerates every dashboard page from the
# updated DB + JSON snapshot. Adjust the weights (% of total) here so the
# progress bar moves in proportion to wall-clock cost — not step count.
REFRESH_STEPS = [
    {
        'id': 'jira_collector',
        'label': 'Fetching tickets from Jira',
        'script': 'jira_collector_agent.py',
        'weight': 60,
        'timeout': 600,
    },
    {
        'id': 'project_fantasy',
        'label': f'Refreshing {_initiative_key()} features',
        'script': 'sync_project_fantasy.py',
        'weight': 25,
        'timeout': 300,
    },
    {
        'id': 'generate_html',
        'label': 'Regenerating dashboards',
        'script': 'generate_html_report.py',
        'weight': 15,
        'timeout': 180,
    },
]

# Refresh job state. Keyed by job_id. Single-user dashboard so a small in-memory
# dict is enough — restarts wipe state, which is fine since the user has
# already seen the resulting page reload.
_REFRESH_JOBS: dict = {}
_REFRESH_LOCK = threading.Lock()

# Pipeline for the "Publish to GitHub" button. Copies the freshly-generated
# reports/html/ tree into docs/, rewrites the entry-point filename, and
# pushes to origin/main so GitHub Pages picks up the change. Steps run
# in-process (not as scripts), so the runner uses _run_publish_pipeline
# rather than the subprocess-based REFRESH_STEPS pattern.
PUBLISH_STEPS = [
    {'id': 'sync_docs',   'label': 'Copying dashboards into docs/',     'weight': 20},
    {'id': 'rewrite',     'label': 'Rewriting entry-point links',        'weight': 10},
    {'id': 'git_stage',   'label': 'Staging changes',                    'weight': 10},
    {'id': 'git_commit',  'label': 'Committing',                         'weight': 15},
    {'id': 'git_push',    'label': 'Pushing to origin/main',             'weight': 45},
]
_PUBLISH_JOBS: dict = {}
_PUBLISH_LOCK = threading.Lock()

ALLOWED_MEMBER_FIELDS = {"github_username", "jira_account_id", "level"}

# Valid level values — mirror utils.competencies.TITLE_TO_LEVEL.
VALID_LEVELS = {
    "",
    "Engineer I", "Engineer II", "Engineer III",
    "Senior Engineer", "Staff Engineer", "Senior Staff Engineer",
    "Principal Engineer", "Senior Principal Engineer",
    "Distinguished Engineer", "Senior VP Engineering",
}


def _dt_now_iso() -> str:
    from datetime import datetime as _dt, timezone as _tz
    return _dt.now(_tz.utc).isoformat()


class DashboardHandler(SimpleHTTPRequestHandler):
    """Handler for the dashboard.

    Single-user LAN server, no auth. Writes are blocked except for the
    /api/member endpoint, which rewrites config/team_config.yaml.
    """

    def do_GET(self):
        if self.path == "/api/health":
            return self._handle_health()
        if self.path == "/api/version":
            return self._handle_version()
        if self.path.startswith("/api/refresh/status"):
            return self._handle_refresh_status()
        if self.path.startswith("/api/publish/status"):
            return self._handle_publish_status()
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/member":
            return self._handle_member_edit()
        if self.path == "/api/ask":
            return self._handle_ask()
        if self.path == "/api/dependency-notes":
            return self._handle_dependency_notes()
        if self.path == "/api/feature-work-status":
            return self._handle_feature_work_status()
        if self.path == "/api/refresh":
            return self._handle_refresh_start()
        if self.path == "/api/publish":
            return self._handle_publish_start()
        self.send_error(405)

    def _handle_health(self):
        """Return basic liveness + freshness info."""
        from os.path import getmtime
        info = {"status": "ok", "now": _dt_now_iso()}
        try:
            db_path = REPO_ROOT / "data" / "metrics.db"
            info["db_ok"] = db_path.exists()
            if info["db_ok"]:
                info["db_size_bytes"] = db_path.stat().st_size
        except Exception as e:
            info["db_ok"] = False
            info["db_error"] = str(e)[:200]
        # Freshness signals from log mtimes — cheap, no DB hits.
        for label, fname in (
            ("last_collector_run", "jira_collector_agent.log"),
            ("last_qa_run",        "qa_agent.log"),
            ("last_hygiene_run",   "jira_hygiene_agent.log"),
        ):
            p = REPO_ROOT / "logs" / fname
            try:
                info[label] = int(getmtime(p)) if p.exists() else None
            except OSError:
                info[label] = None
        if not info["db_ok"]:
            return self._send_json(503, info)
        return self._send_json(200, info)

    def _handle_version(self):
        """Return commit SHA + schema version. No errors block — fields are best-effort."""
        info = {"now": _dt_now_iso()}
        try:
            sys.path.insert(0, str(REPO_ROOT / "src"))
            from database.schema import SCHEMA_VERSION
            info["schema_version"] = SCHEMA_VERSION
        except Exception:
            info["schema_version"] = None
        try:
            r = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=2,
            )
            info["git_sha"] = r.stdout.strip() if r.returncode == 0 else None
        except Exception:
            info["git_sha"] = None
        return self._send_json(200, info)

    def do_PUT(self):       self.send_error(405)
    def do_DELETE(self):    self.send_error(405)
    def do_PATCH(self):     self.send_error(405)

    def end_headers(self):
        # Dashboard assets regenerate often — force the browser to revalidate
        # on every reload rather than serving a stale cached copy.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        return super().end_headers()

    def _send_json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle_refresh_start(self):
        """POST /api/refresh — kick off a Jira → DB → HTML refresh in the
        background. Returns {job_id} immediately; the client polls
        /api/refresh/status?id=<job_id> for progress.

        Only one job runs at a time. A second POST while a job is still
        running returns the existing job_id so the modal stays in sync.
        """
        with _REFRESH_LOCK:
            for jid, job in _REFRESH_JOBS.items():
                if job.get('status') == 'running':
                    return self._send_json(200, {'job_id': jid, 'reused': True})

            job_id = uuid.uuid4().hex[:12]
            _REFRESH_JOBS[job_id] = {
                'status': 'running',
                'step_index': 0,
                'step_id': REFRESH_STEPS[0]['id'],
                'step_label': REFRESH_STEPS[0]['label'],
                'percent': 0,
                'started_at': _dt_now_iso(),
                'finished_at': None,
                'error': None,
                'log_tail': '',
            }

        thread = threading.Thread(
            target=_run_refresh_pipeline,
            args=(job_id,),
            name=f'refresh-{job_id}',
            daemon=True,
        )
        thread.start()
        logger.info("refresh: started job %s", job_id)
        return self._send_json(200, {'job_id': job_id, 'reused': False})

    def _handle_refresh_status(self):
        """GET /api/refresh/status?id=<job_id> — return the current state of
        the refresh job. Returns 404 if the job_id is unknown.
        """
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        job_id = (qs.get('id') or [''])[0].strip()
        if not job_id:
            return self._send_json(400, {'error': 'Missing id parameter.'})
        with _REFRESH_LOCK:
            job = _REFRESH_JOBS.get(job_id)
            if not job:
                return self._send_json(404, {'error': 'Unknown job id.'})
            payload = dict(job)
        payload['job_id'] = job_id
        return self._send_json(200, payload)

    def _handle_publish_start(self):
        """POST /api/publish — copy reports/html/ → docs/, rewrite the
        entry-point filename, then git add/commit/push so GitHub Pages
        rebuilds. Single-job-at-a-time semantics matching /api/refresh.
        """
        with _PUBLISH_LOCK:
            for jid, job in _PUBLISH_JOBS.items():
                if job.get('status') == 'running':
                    return self._send_json(200, {'job_id': jid, 'reused': True})
            job_id = uuid.uuid4().hex[:12]
            _PUBLISH_JOBS[job_id] = {
                'status': 'running',
                'step_index': 0,
                'step_id': PUBLISH_STEPS[0]['id'],
                'step_label': PUBLISH_STEPS[0]['label'],
                'percent': 0,
                'started_at': _dt_now_iso(),
                'finished_at': None,
                'error': None,
                'log_tail': '',
                'commit_sha': None,
                'pushed': False,
                'no_changes': False,
            }
        thread = threading.Thread(
            target=_run_publish_pipeline,
            args=(job_id,),
            name=f'publish-{job_id}',
            daemon=True,
        )
        thread.start()
        logger.info("publish: started job %s", job_id)
        return self._send_json(200, {'job_id': job_id, 'reused': False})

    def _handle_publish_status(self):
        """GET /api/publish/status?id=<job_id>."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        job_id = (qs.get('id') or [''])[0].strip()
        if not job_id:
            return self._send_json(400, {'error': 'Missing id parameter.'})
        with _PUBLISH_LOCK:
            job = _PUBLISH_JOBS.get(job_id)
            if not job:
                return self._send_json(404, {'error': 'Unknown job id.'})
            payload = dict(job)
        payload['job_id'] = job_id
        return self._send_json(200, payload)

    def _handle_member_edit(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 16 * 1024:
                return self._send_json(400, {"error": "Invalid request body size."})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send_json(400, {"error": "Body must be valid JSON."})

            name = (body.get("name") or "").strip()
            if not name:
                return self._send_json(400, {"error": "Missing member name."})

            updates = {}
            for field in ALLOWED_MEMBER_FIELDS:
                if field in body:
                    val = body.get(field)
                    val = "" if val is None else str(val).strip()
                    updates[field] = val

            if "level" in updates and updates["level"] not in VALID_LEVELS:
                return self._send_json(400, {"error": f"Unknown level: {updates['level']}"})

            # Validate github_username and jira_account_id against the live
            # services before writing. Empty values skip validation (so you
            # can clear a field).
            err = _validate_identities(
                updates.get("github_username"),
                updates.get("jira_account_id"),
            )
            if err:
                return self._send_json(400, {"error": err})

            updated = _write_member_config(name, updates)
            if not updated:
                return self._send_json(404, {"error": f"Member not found: {name}"})

            # Regenerate HTML so the new values appear on reload. Log and
            # swallow errors so the API stays responsive even if generation
            # fails — the YAML has already been written.
            try:
                subprocess.run(
                    ["python3", str(GENERATE_SCRIPT)],
                    cwd=str(REPO_ROOT),
                    check=True,
                    timeout=120,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except subprocess.CalledProcessError as e:
                logger.error("generate_html_report.py failed: %s", e.stderr.decode("utf-8", "replace")[:2000])
                return self._send_json(500, {"error": "Config saved, but HTML regeneration failed. Check serve_dashboard.log."})
            except subprocess.TimeoutExpired:
                logger.error("generate_html_report.py timed out")
                return self._send_json(500, {"error": "Config saved, but HTML regeneration timed out."})

            logger.info("Member updated: %s fields=%s", name, sorted(updates.keys()))
            return self._send_json(200, {"ok": True, "name": name, "updated": sorted(updates.keys())})
        except Exception as e:
            logger.exception("member edit failed")
            return self._send_json(500, {"error": f"Unexpected error: {e}"})

    def _handle_dependency_notes(self):
        """POST /api/dependency-notes — update the `notes` field for one
        dependency in config/dependencies.yaml.

        Body: {"key": "FNTSY-1234", "notes": "..."}.
        Response: 200 {ok: True} on success.

        Holds an flock on the YAML so concurrent saves serialize. New keys
        are appended (no need to pre-register a row in the file). Notes
        replace prior text in full — no history kept by design.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 64 * 1024:
                return self._send_json(400, {"error": "Invalid request body size."})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send_json(400, {"error": "Body must be valid JSON."})

            key = (body.get("key") or "").strip()
            notes = body.get("notes")
            if not key:
                return self._send_json(400, {"error": "Missing 'key'."})
            # Tight key whitelist — Jira keys are uppercase letters + digits + dash.
            if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key):
                return self._send_json(400, {"error": f"Invalid ticket key: {key!r}"})
            if notes is None:
                notes = ""
            if not isinstance(notes, str):
                return self._send_json(400, {"error": "'notes' must be a string."})

            ok = _update_dependency_notes(key, notes)
            if not ok:
                return self._send_json(500, {"error": "Could not write dependencies.yaml"})

            # Regenerate ONLY the Dependencies page in-process. Previously we
            # subprocessed the full generate_html_report.py for every save —
            # that re-rendered all ~25 pages and risked two saves' regens
            # interleaving. Calling the single function in-process is ~50ms
            # and Python's GIL serializes it with other request threads.
            try:
                _regen_dependencies_page()
            except Exception as e:
                # YAML is already saved; the next full regen cron picks it up.
                # Log loudly but don't fail the response.
                logger.warning("dependencies.html regen failed (YAML saved): %s", e)

            logger.info("Dependency notes saved: key=%s len=%d", key, len(notes))
            return self._send_json(200, {"ok": True, "key": key})
        except Exception as e:
            logger.exception("dependency notes save failed")
            return self._send_json(500, {"error": f"Unexpected error: {e}"})

    def _handle_feature_work_status(self):
        """POST /api/feature-work-status — toggle BE/FE work-complete flags
        for one feature in config/feature_work_status.yaml.

        Body: {"key": "FEAT-1234", "be_done": bool, "fe_done": bool}
        Either flag may be omitted; only the provided fields are updated, so
        the client can post just the one that changed.
        Response: 200 {ok: True} on success.

        Mirrors /api/dependency-notes: holds an flock on the YAML, regenerates
        features.html in-process so the next page load reflects the toggle.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 16 * 1024:
                return self._send_json(400, {"error": "Invalid request body size."})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send_json(400, {"error": "Body must be valid JSON."})

            key = (body.get("key") or "").strip()
            if not key:
                return self._send_json(400, {"error": "Missing 'key'."})
            if not re.match(r"^[A-Z][A-Z0-9_]*-\d+$", key):
                return self._send_json(400, {"error": f"Invalid ticket key: {key!r}"})

            updates = {}
            for field in ("be_done", "fe_done"):
                if field in body:
                    if not isinstance(body[field], bool):
                        return self._send_json(400, {"error": f"'{field}' must be a boolean."})
                    updates[field] = body[field]
            if not updates:
                return self._send_json(400, {"error": "Provide at least one of be_done / fe_done."})

            ok = _update_feature_work_status(key, updates)
            if not ok:
                return self._send_json(500, {"error": "Could not write feature_work_status.yaml"})

            try:
                _regen_features_page()
            except Exception as e:
                # YAML is saved; full regen cron picks it up.
                logger.warning("features.html regen failed (YAML saved): %s", e)

            logger.info(
                "Feature work status saved: key=%s updates=%s", key, sorted(updates.keys())
            )
            return self._send_json(200, {"ok": True, "key": key})
        except Exception as e:
            logger.exception("feature work status save failed")
            return self._send_json(500, {"error": f"Unexpected error: {e}"})

    def _handle_ask(self):
        """POST /api/ask — Fantasy Ops conversational endpoint.

        Body: {"question": str, "history": [optional list of prior turns]}
        Response: {reply, tool_calls, history_after, missing_api_key?}
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 256 * 1024:
                return self._send_json(400, {"error": "Invalid body size."})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send_json(400, {"error": "Body must be valid JSON."})

            question = (body.get("question") or "").strip()
            history = body.get("history") or []
            if not isinstance(history, list):
                history = []

            # Import lazily so missing deps don't crash the whole server.
            try:
                import sys as _sys
                _sys.path.insert(0, str(REPO_ROOT / "src"))
                from utils.ops_assistant import ask as _ask  # type: ignore
            except Exception as e:
                logger.exception("ops_assistant import failed")
                return self._send_json(500, {"error": f"assistant unavailable: {e}"})

            result = _ask(question, history=history)
            return self._send_json(200, result)
        except Exception as e:
            logger.exception("ask handler failed")
            return self._send_json(500, {"error": f"Unexpected error: {e}"})

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)


def _validate_identities(github_username: Optional[str], jira_account_id: Optional[str]) -> Optional[str]:
    """Return an error string if validation fails, else None.

    - github_username: checked via `gh api /users/<login>` (200 = exists).
      Only runs when the caller actually provides a non-empty value; absent
      or empty skips validation so clearing is allowed.
    - jira_account_id: checked via Jira REST `/rest/api/3/user?accountId=<id>`
      using JIRA_EMAIL + JIRA_API_TOKEN basic auth (same creds the collector
      uses). Account IDs containing disallowed characters are rejected before
      hitting the network.
    """
    import re

    if github_username:
        gh_err = _validate_github_username(github_username)
        if gh_err:
            return gh_err

    if jira_account_id:
        # Jira account IDs look like "712020:<uuid>" or "<24-hex>". Anything
        # with whitespace or control chars is a typo.
        if not re.fullmatch(r"[A-Za-z0-9:_\-]+", jira_account_id):
            return f"Jira account ID contains invalid characters: {jira_account_id!r}"
        jira_err = _validate_jira_account_id(jira_account_id)
        if jira_err:
            return jira_err

    return None


def _find_gh_binary() -> Optional[str]:
    """Locate the gh CLI. launchd doesn't include Homebrew's bin in PATH."""
    import shutil
    for cand in (shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh"):
        if cand and Path(cand).exists():
            return cand
    return None


def _validate_github_username(username: str) -> Optional[str]:
    """Return error string if GitHub user doesn't exist, else None."""
    # Basic shape check to dodge obviously-wrong inputs before hitting the API.
    import re
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?", username):
        return f"GitHub username is not a valid login: {username!r}"

    gh = _find_gh_binary()
    if not gh:
        logger.warning("gh CLI not installed; skipping GitHub validation")
        return None  # don't block edits if the CLI is missing locally

    try:
        proc = subprocess.run(
            [gh, "api", f"/users/{username}", "--silent"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return "GitHub lookup timed out. Try again in a moment."

    if proc.returncode == 0:
        return None
    stderr = (proc.stderr or "").strip()
    if "HTTP 404" in stderr or "Not Found" in stderr:
        return f"GitHub user not found: {username}"
    if "HTTP 401" in stderr or "authentication" in stderr.lower():
        logger.warning("gh CLI auth issue during validation: %s", stderr[:200])
        return "GitHub validation failed: gh CLI is not authenticated (run `gh auth login`)."
    return f"GitHub validation failed: {stderr[:200] or 'unknown error'}"


def _validate_jira_account_id(account_id: str) -> Optional[str]:
    """Return error string if Jira account ID doesn't resolve, else None."""
    # Reuse creds-loading logic already used by the Jira API collector.
    env_file = REPO_ROOT / "config" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    email = os.environ.get("JIRA_EMAIL")
    token = os.environ.get("JIRA_API_TOKEN")
    if not email or not token:
        logger.warning("Jira credentials not set; skipping Jira validation")
        return None  # don't block edits when creds are missing

    # Read cloud_id from the team config so we target the right instance.
    import yaml
    try:
        cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
        cloud_id = (cfg.get("jira") or {}).get("cloud_id", "betfanatics.atlassian.net")
    except Exception:
        cloud_id = "betfanatics.atlassian.net"

    try:
        import requests
    except ImportError:
        logger.warning("requests not available; skipping Jira validation")
        return None

    url = f"https://{cloud_id}/rest/api/3/user"
    try:
        resp = requests.get(
            url,
            params={"accountId": account_id},
            auth=(email, token),
            headers={"Accept": "application/json"},
            timeout=15,
        )
    except requests.RequestException as e:
        return f"Jira validation failed: {e}"

    if resp.status_code == 200:
        return None
    if resp.status_code == 404:
        return f"Jira user not found for account ID: {account_id}"
    if resp.status_code in (401, 403):
        logger.warning("Jira validation auth issue: %s %s", resp.status_code, resp.text[:200])
        return "Jira validation failed: API token is not authorized (check JIRA_API_TOKEN)."
    return f"Jira validation failed: HTTP {resp.status_code} {resp.text[:200]}"


def _write_member_config(name: str, updates: dict) -> bool:
    """Update the team_config.yaml entry for `name`. Returns True if found.

    Only ALLOWED_MEMBER_FIELDS are written. Edits happen line-by-line so
    comments, quoting style, and field order are preserved. Writes via a
    temp file + os.replace for atomicity, and keeps a timestamped backup
    alongside the config.

    Two simultaneous PUTs would otherwise race: both read the raw config,
    both compute edits, last one wins (silent data loss). An advisory
    flock on a sibling .lock file serializes them — the second waits until
    the first finishes its read-modify-write cycle.
    """
    import fcntl
    import re
    from datetime import datetime as _dt

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(str(CONFIG_PATH))

    lock_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + '.lock')
    with open(lock_path, 'w') as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
        return _write_member_config_locked(name, updates)


def _write_member_config_locked(name: str, updates: dict) -> bool:
    """Inner implementation — caller must hold the team_config.yaml.lock flock."""
    import re
    from datetime import datetime as _dt

    raw = CONFIG_PATH.read_text()
    lines = raw.splitlines(keepends=True)

    # Find the start and end of the target member's block. A member starts
    # at a line matching `- name: "<name>"` (or unquoted); the block ends
    # at the next sibling list item or end of file.
    name_line_idx = None
    name_pattern = re.compile(r'^(\s*)-\s+name:\s*["\']?(.+?)["\']?\s*(?:#.*)?$')
    target_indent = None
    for i, line in enumerate(lines):
        m = name_pattern.match(line.rstrip("\n"))
        if m and m.group(2).strip() == name:
            name_line_idx = i
            target_indent = m.group(1)
            break
    if name_line_idx is None:
        return False

    # Block ends at the next list item at the same indent, or next top-level
    # key (indent <= target_indent's length minus the "- " prefix, which is 2).
    # Simpler rule: block ends at the next line that starts with `<indent>- `
    # or a line whose leading whitespace is shorter than `len(target_indent) + 2`
    # (i.e., out of the list).
    block_end = len(lines)
    item_prefix = target_indent + "- "
    child_indent_len = len(target_indent) + 2
    for j in range(name_line_idx + 1, len(lines)):
        line = lines[j]
        if not line.strip():  # blank lines belong to the block
            continue
        leading = len(line) - len(line.lstrip(" "))
        if line.startswith(item_prefix):
            block_end = j
            break
        if leading < child_indent_len:
            block_end = j
            break

    # Collect existing field lines inside the block and map to their index.
    field_line_pat = re.compile(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$')
    block_indent = " " * child_indent_len
    existing_field_indices = {}  # field -> line index
    for j in range(name_line_idx + 1, block_end):
        m = field_line_pat.match(lines[j].rstrip("\n"))
        if not m:
            continue
        if len(m.group(1)) != child_indent_len:
            continue
        existing_field_indices[m.group(2)] = j

    existing_value_pat = re.compile(r'^\s*[A-Za-z_][A-Za-z0-9_]*:\s*(.*)$')

    def _prior_quote_style(idx: int) -> Optional[str]:
        """Return "\"", "'", or None for the existing field's value quoting."""
        m = existing_value_pat.match(lines[idx].rstrip("\n"))
        if not m:
            return None
        v = m.group(1).strip()
        # Strip trailing comment so we look only at the value token.
        v = re.sub(r"\s+#.*$", "", v)
        if v.startswith('"') and v.endswith('"'):
            return '"'
        if v.startswith("'") and v.endswith("'"):
            return "'"
        return None

    def _format_value(field: str, val: str, prior_style: Optional[str] = None) -> str:
        # jira_account_id always contains ':' so it must be quoted.
        needs_quote = (
            field == "jira_account_id"
            or ":" in val
            or val == ""
            or (val and val[0] in "&*!|>%@`?-{[")
        )
        if needs_quote:
            # Prefer the prior style if present and compatible; default to ".
            q = prior_style if prior_style in ('"', "'") else '"'
            if q == '"':
                escaped = val.replace("\\", "\\\\").replace('"', '\\"')
                return f'"{escaped}"'
            # single quotes: YAML escape is ''
            escaped = val.replace("'", "''")
            return f"'{escaped}'"
        # Not strictly required — honor the prior quoting style if any.
        if prior_style in ('"', "'"):
            return _format_value(field, val, prior_style=None) if False else (
                f'"{val}"' if prior_style == '"' else f"'{val}'"
            )
        return val

    # Apply updates. Rewriting lines in place preserves order and comments.
    new_lines = list(lines)
    additions = []  # formatted lines for fields not previously present
    for field, val in updates.items():
        if val == "":
            # Remove the field if it exists; otherwise nothing to do.
            if field in existing_field_indices:
                idx = existing_field_indices[field]
                new_lines[idx] = None  # mark for deletion
            continue

        prior_style = _prior_quote_style(existing_field_indices[field]) if field in existing_field_indices else None
        formatted = _format_value(field, val, prior_style=prior_style)
        new_line = f"{block_indent}{field}: {formatted}\n"

        if field in existing_field_indices:
            idx = existing_field_indices[field]
            # Preserve a trailing comment on the existing line, if any.
            existing = lines[idx]
            comment_match = re.search(r"(\s+#.*)$", existing.rstrip("\n"))
            if comment_match:
                new_line = new_line.rstrip("\n") + comment_match.group(1) + "\n"
            new_lines[idx] = new_line
        else:
            additions.append(new_line)

    # Drop lines marked for deletion.
    new_lines = [ln for ln in new_lines if ln is not None]

    # Insert any new fields just after the member's name line. Find the new
    # name line index (may have shifted if fields were dropped above — but
    # since dropped lines are only inside this block, name line index is
    # still name_line_idx).
    if additions:
        insert_at = name_line_idx + 1
        new_lines = new_lines[:insert_at] + additions + new_lines[insert_at:]

    # Backup + atomic write — keep only the most recent N backups so this
    # directory doesn't accumulate indefinitely (was ~30 stale files).
    backup_dir = CONFIG_PATH.parent / "backups"
    backup_dir.mkdir(exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%dT%H%M%S")
    (backup_dir / f"team_config-{stamp}.yaml").write_text(raw)

    MAX_BACKUPS = 10
    backups = sorted(backup_dir.glob("team_config-*.yaml"))
    for stale in backups[:-MAX_BACKUPS]:
        try:
            stale.unlink()
        except OSError:
            pass

    tmp_path = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    tmp_path.write_text("".join(new_lines))
    os.replace(tmp_path, CONFIG_PATH)
    return True


# ---------------------------------------------------------------------------
# Dependencies YAML writer — used by /api/dependency-notes
# ---------------------------------------------------------------------------

def _dependencies_path() -> Path:
    return REPO_ROOT / "config" / "dependencies.yaml"


def _regen_dependencies_page() -> None:
    """Render reports/html/dependencies.html in-process from the current YAML.

    Loads `generate_html_report.generate_dependencies_html` lazily so this
    server module stays importable without the heavy generator dependencies.
    Used by /api/dependency-notes after a save so the user sees their note
    on the next page load without waiting for the next cron.
    """
    sys.path.insert(0, str(REPO_ROOT / "src"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    # Force reimport so we pick up generator hot-fixes without restart.
    import importlib
    import generate_html_report as _ghr
    importlib.reload(_ghr)
    from utils.config import load_config
    config = load_config()
    out = REPO_ROOT / "reports" / "html" / "dependencies.html"
    _ghr.generate_dependencies_html(config, out)


def _update_dependency_notes(key: str, notes: str) -> bool:
    """Update (or insert) the `notes` field for a single dependency.

    Uses ruamel.yaml-style round-trip if available — otherwise falls back to
    PyYAML, which strips comments. We prefer not to lose the schema comment
    block at the top, so we round-trip with ruamel when present.

    Concurrent calls are serialized via flock on a sibling .lock file.
    Re-raises only on truly unexpected errors; returns False on validation
    or filesystem failures so the caller emits a clean 500.
    """
    import fcntl
    path = _dependencies_path()
    if not path.exists():
        # Create a minimal file so subsequent saves work even from an empty start.
        path.write_text("dependencies: []\n")

    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

            # Try ruamel first (preserves comments + ordering); fall back to
            # PyYAML if ruamel isn't installed in this venv.
            try:
                from ruamel.yaml import YAML  # type: ignore
                yaml = YAML()
                yaml.preserve_quotes = True
                yaml.width = 4096
                with open(path) as f:
                    data = yaml.load(f) or {}
            except ImportError:
                import yaml as _yaml
                yaml = None
                with open(path) as f:
                    data = _yaml.safe_load(f) or {}

            deps = data.get("dependencies") or []
            updated = False
            for entry in deps:
                if (entry.get("key") or "").strip() == key:
                    entry["notes"] = notes
                    updated = True
                    break
            if not updated:
                # Append a new row so future page renders include it.
                deps.append({"key": key, "notes": notes})
                data["dependencies"] = deps

            tmp = path.with_suffix(path.suffix + ".tmp")
            if yaml is not None:
                with open(tmp, "w") as f:
                    yaml.dump(data, f)
            else:
                import yaml as _yaml
                with open(tmp, "w") as f:
                    _yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            os.replace(tmp, path)
            return True
    except Exception as e:
        logger.exception("dependency notes write failed for %s: %s", key, e)
        return False


# ---------------------------------------------------------------------------
# Feature work-status YAML writer — used by /api/feature-work-status
# ---------------------------------------------------------------------------

def _feature_work_status_path() -> Path:
    return REPO_ROOT / "config" / "feature_work_status.yaml"


def _regen_features_page() -> None:
    """Render reports/html/features.html in-process from the current YAML +
    project_fantasy.json snapshot. Used by /api/feature-work-status after a
    save so the toggle's persistence is visible on the next page load."""
    sys.path.insert(0, str(REPO_ROOT / "src"))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import importlib
    import generate_html_report as _ghr
    importlib.reload(_ghr)
    out = REPO_ROOT / "reports" / "html" / "features.html"
    _ghr.generate_features_html(out)


def _update_feature_work_status(key: str, updates: dict) -> bool:
    """Update (or insert) the BE/FE flags for one feature in
    config/feature_work_status.yaml. `updates` is a partial dict containing
    any subset of {be_done, fe_done}; missing fields are left unchanged.

    Concurrent calls are serialized via flock on a sibling .lock file. Uses
    ruamel.yaml when available so the schema-comment header at the top of
    the file survives round-trips; falls back to PyYAML otherwise.
    """
    import fcntl
    path = _feature_work_status_path()
    if not path.exists():
        path.write_text("features: []\n")

    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        with open(lock_path, "w") as lock_fh:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)

            try:
                from ruamel.yaml import YAML  # type: ignore
                yaml = YAML()
                yaml.preserve_quotes = True
                yaml.width = 4096
                with open(path) as f:
                    data = yaml.load(f) or {}
            except ImportError:
                import yaml as _yaml
                yaml = None
                with open(path) as f:
                    data = _yaml.safe_load(f) or {}

            features = data.get("features") or []
            updated = False
            for entry in features:
                if (entry.get("key") or "").strip() == key:
                    for field, value in updates.items():
                        entry[field] = value
                    updated = True
                    break
            if not updated:
                # New row — fill any unspecified flag with False so the YAML
                # always has the full schema. Keeps hand-editing predictable.
                row = {"key": key, "be_done": False, "fe_done": False}
                row.update(updates)
                features.append(row)
                data["features"] = features

            tmp = path.with_suffix(path.suffix + ".tmp")
            if yaml is not None:
                with open(tmp, "w") as f:
                    yaml.dump(data, f)
            else:
                import yaml as _yaml
                with open(tmp, "w") as f:
                    _yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            os.replace(tmp, path)
            return True
    except Exception as e:
        logger.exception("feature work status write failed for %s: %s", key, e)
        return False


def _run_refresh_pipeline(job_id: str) -> None:
    """Run the refresh pipeline end-to-end and stream progress into
    _REFRESH_JOBS[job_id]. Designed to run in a daemon thread.

    Each step is a separate Python script. We launch with the same Python
    interpreter that's running the server so virtualenv selection is
    consistent. stderr is captured and surfaced in the job state on failure.
    Cumulative progress is computed from REFRESH_STEPS' weight values.
    """
    weights_total = sum(s['weight'] for s in REFRESH_STEPS) or 1
    cumulative = 0
    python_bin = sys.executable

    def _set(**fields):
        with _REFRESH_LOCK:
            _REFRESH_JOBS[job_id].update(fields)

    try:
        for idx, step in enumerate(REFRESH_STEPS):
            _set(
                step_index=idx,
                step_id=step['id'],
                step_label=step['label'],
                percent=int((cumulative / weights_total) * 100),
            )
            script_path = REPO_ROOT / 'scripts' / step['script']
            logger.info("refresh %s: step %s (%s)", job_id, step['id'], script_path.name)

            try:
                proc = subprocess.run(
                    [python_bin, str(script_path)],
                    cwd=str(REPO_ROOT),
                    capture_output=True,
                    text=True,
                    timeout=step['timeout'],
                )
            except subprocess.TimeoutExpired as e:
                msg = f"{step['script']} timed out after {step['timeout']}s"
                logger.error("refresh %s: %s", job_id, msg)
                _set(
                    status='failed',
                    error=msg,
                    log_tail=(e.stderr or '')[-2000:] if hasattr(e, 'stderr') and e.stderr else '',
                    finished_at=_dt_now_iso(),
                )
                return

            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or '')[-2000:]
                logger.error(
                    "refresh %s: %s exited %d. tail=%s",
                    job_id, step['script'], proc.returncode, tail[-400:],
                )
                _set(
                    status='failed',
                    error=f"{step['script']} failed with exit code {proc.returncode}",
                    log_tail=tail,
                    finished_at=_dt_now_iso(),
                )
                return

            cumulative += step['weight']
            _set(percent=int((cumulative / weights_total) * 100))

        _set(
            status='done',
            percent=100,
            step_index=len(REFRESH_STEPS) - 1,
            step_id='done',
            step_label='Refresh complete',
            finished_at=_dt_now_iso(),
        )
        logger.info("refresh %s: done", job_id)
    except Exception as e:
        logger.exception("refresh %s: unexpected error", job_id)
        _set(
            status='failed',
            error=f'Unexpected error: {e}',
            finished_at=_dt_now_iso(),
        )


def _run_publish_pipeline(job_id: str) -> None:
    """Sync reports/html/ → docs/, rewrite project_fantasy.html links to
    index.html, then git add/commit/push. Mirrors the manual flow documented
    in GITHUB_PAGES_SETUP.md so GitHub Pages picks up the new dashboards.
    """
    import shutil
    weights_total = sum(s['weight'] for s in PUBLISH_STEPS) or 1
    cumulative = 0
    docs_dir = REPO_ROOT / 'docs'
    src_dir = REPO_ROOT / 'reports' / 'html'

    def _set(**fields):
        with _PUBLISH_LOCK:
            _PUBLISH_JOBS[job_id].update(fields)

    def _enter(idx: int):
        nonlocal cumulative
        step = PUBLISH_STEPS[idx]
        _set(
            step_index=idx,
            step_id=step['id'],
            step_label=step['label'],
            percent=int((cumulative / weights_total) * 100),
        )

    def _advance(idx: int):
        nonlocal cumulative
        cumulative += PUBLISH_STEPS[idx]['weight']
        _set(percent=int((cumulative / weights_total) * 100))

    def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
        return subprocess.run(
            ['git', *args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    try:
        # 1. sync reports/html → docs (preserves README.md / .nojekyll already in docs/)
        _enter(0)
        if not src_dir.exists():
            _set(status='failed', error=f'Source not found: {src_dir}', finished_at=_dt_now_iso())
            return
        docs_dir.mkdir(exist_ok=True)
        for entry in src_dir.iterdir():
            dest = docs_dir / entry.name
            if entry.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(entry, dest)
            else:
                shutil.copy2(entry, dest)
        _advance(0)

        # 2. rename project_fantasy.html → index.html and rewrite internal links
        _enter(1)
        pf = docs_dir / 'project_fantasy.html'
        idx_html = docs_dir / 'index.html'
        if pf.exists():
            if idx_html.exists():
                idx_html.unlink()
            pf.rename(idx_html)
        for html in docs_dir.rglob('*.html'):
            try:
                text = html.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError):
                continue
            if 'project_fantasy.html' in text:
                html.write_text(text.replace('project_fantasy.html', 'index.html'), encoding='utf-8')
        _advance(1)

        # 3. git add docs/
        _enter(2)
        r = _git('add', 'docs')
        if r.returncode != 0:
            _set(status='failed', error='git add failed', log_tail=(r.stderr or r.stdout)[-2000:], finished_at=_dt_now_iso())
            return
        _advance(2)

        # 4. git commit (skip cleanly if there's nothing to commit)
        _enter(3)
        diff = _git('diff', '--cached', '--quiet')
        if diff.returncode == 0:
            _set(no_changes=True)
            cumulative += PUBLISH_STEPS[3]['weight'] + PUBLISH_STEPS[4]['weight']
            _set(
                status='done',
                percent=100,
                step_index=len(PUBLISH_STEPS) - 1,
                step_id='done',
                step_label='Already up to date — nothing to publish',
                finished_at=_dt_now_iso(),
            )
            logger.info("publish %s: no changes", job_id)
            return
        msg = 'Refresh GitHub Pages dashboards'
        r = _git('commit', '-m', msg)
        if r.returncode != 0:
            _set(status='failed', error='git commit failed', log_tail=(r.stderr or r.stdout)[-2000:], finished_at=_dt_now_iso())
            return
        sha = _git('rev-parse', '--short', 'HEAD')
        _set(commit_sha=sha.stdout.strip() if sha.returncode == 0 else None)
        _advance(3)

        # 5. git push
        #
        # We push HEAD:main from whatever branch the user happens to be on, so
        # the push is only valid while origin/main is an ancestor of HEAD. If
        # main advanced independently (another clone, a web edit, a second
        # Publish from elsewhere) git rejects it as a non-fast-forward and the
        # bare 'git push failed' gave no hint about what to do. Pre-flight the
        # check so the UI can name the actual problem and the fix.
        _enter(4)
        fetched = _git('fetch', 'origin', 'main', timeout=120)
        if fetched.returncode != 0:
            # Non-fatal: offline or transient. Fall through and let push decide.
            logger.warning("publish %s: pre-push fetch failed: %s", job_id, (fetched.stderr or '').strip()[:300])
        else:
            ff = _git('merge-base', '--is-ancestor', 'origin/main', 'HEAD')
            if ff.returncode != 0:
                behind = _git('rev-list', '--count', 'HEAD..origin/main')
                n = behind.stdout.strip() or '?'
                _set(
                    status='failed',
                    error=(
                        f'Cannot publish: origin/main has {n} commit(s) this branch '
                        f'does not contain, so pushing would not be a fast-forward. '
                        f'Reconcile first, e.g.:  git fetch origin && '
                        f'git merge origin/main  — then Publish again.'
                    ),
                    log_tail=f'HEAD..origin/main = {n} commit(s)\n'
                             + _git('log', '--oneline', 'HEAD..origin/main').stdout[-1500:],
                    finished_at=_dt_now_iso(),
                )
                logger.error("publish %s: refusing non-fast-forward push (behind %s)", job_id, n)
                return

        r = _git('push', 'origin', 'HEAD:main', timeout=300)
        if r.returncode != 0:
            _set(status='failed', error='git push failed', log_tail=(r.stderr or r.stdout)[-2000:], finished_at=_dt_now_iso())
            return
        _set(pushed=True)
        _advance(4)

        _set(
            status='done',
            percent=100,
            step_index=len(PUBLISH_STEPS) - 1,
            step_id='done',
            step_label='Published to GitHub Pages',
            finished_at=_dt_now_iso(),
        )
        logger.info("publish %s: done", job_id)
    except subprocess.TimeoutExpired as e:
        logger.exception("publish %s: timeout", job_id)
        _set(status='failed', error=f'Timed out: {e}', finished_at=_dt_now_iso())
    except Exception as e:
        logger.exception("publish %s: unexpected error", job_id)
        _set(status='failed', error=f'Unexpected error: {e}', finished_at=_dt_now_iso())


def _lan_ip() -> str:
    """Best-effort discovery of the Mac's LAN IP for the startup banner."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # doesn't actually send; picks the right interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address. Use 127.0.0.1 for loopback-only.")
    args = parser.parse_args()

    # Logging — write to logs/serve_dashboard.log; also print banner to stdout
    log_path = Path(__file__).parent.parent / "logs" / "serve_dashboard.log"
    log_path.parent.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Serve only from reports/html/ — chdir so SimpleHTTPRequestHandler
    # doesn't accidentally expose the rest of the repo.
    if not REPORTS_DIR.exists():
        logger.error("Reports directory missing: %s", REPORTS_DIR)
        return 2
    os.chdir(REPORTS_DIR)

    # Default landing page: project_fantasy.html (the dashboard home that
    # every page's nav links to). reports/html/ has no index.html — only
    # the published docs/ copy renames project_fantasy.html → index.html
    # at deploy time. Rewrite "/" so SimpleHTTPRequestHandler doesn't 404.
    class RootRedirectHandler(DashboardHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.path = "/project_fantasy.html"
            return super().do_GET()

    server = HTTPServer((args.host, args.port), RootRedirectHandler)
    ip = _lan_ip()
    logger.info("=" * 60)
    logger.info("Dashboard server listening on http://%s:%d/", args.host, args.port)
    logger.info("  LAN URL:      http://%s:%d/", ip, args.port)
    logger.info("  Localhost:    http://127.0.0.1:%d/", args.port)
    logger.info("  Auth:         disabled (single-user LAN)")
    logger.info("  Serving from: %s", REPORTS_DIR)
    logger.info("=" * 60)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
