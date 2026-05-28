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
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

logger = logging.getLogger("serve_dashboard")


REPO_ROOT = Path(__file__).parent.parent
REPORTS_DIR = REPO_ROOT / "reports" / "html"
CONFIG_PATH = REPO_ROOT / "config" / "team_config.yaml"
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate_html_report.py"

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
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/member":
            return self._handle_member_edit()
        if self.path == "/api/ask":
            return self._handle_ask()
        if self.path == "/api/dependency-notes":
            return self._handle_dependency_notes()
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

    # Default landing page: logs_dashboard.html. We accomplish this by
    # rewriting "/" before the handler reads a file.
    class RootRedirectHandler(DashboardHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.path = "/logs_dashboard.html"
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
