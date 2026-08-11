# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

"Project Fantasy" — a static-HTML engineering-management dashboard driven by a
local SQLite database. Python collectors pull Jira / GitHub / Google Calendar
data into `data/metrics.db`; generator scripts render that DB into static pages
under `reports/html/`; a small stdlib HTTP server (or GitHub Pages) serves them.
There is no web framework and no build step — the "frontend" is generated HTML.

This project is deliberately separate from the sibling **em-dashboard** repo:
they must share no data, cron jobs, or build processes. Project Fantasy serves
on **:8080**, em-dashboard on **:8070**. (Note: `run_jira_collector_agent.sh`
intentionally chains a call into em-dashboard's regen script — that is the one
sanctioned cross-repo touch, gated on `-x`, because both read the same DB and
must agree on which collection pass they reflect.)

## Common commands

```bash
pip3 install -r requirements.txt          # deps: pyyaml, ruamel.yaml, python-dateutil, requests
python3 scripts/init_database.py          # create/upgrade schema in data/metrics.db

# Full data refresh (what cron runs; also the "Refresh Data" button pipeline):
python3 scripts/jira_collector_agent.py   # fetch all FNTSY tickets/sprints → DB
python3 scripts/generate_html_report.py   # regenerate every page under reports/html/
./scripts/run_jira_collector_agent.sh     # collector + report regen, with log rotation

# Serve locally (single-user LAN server, no auth by design):
python3 scripts/serve_dashboard.py --port 8080

# Other collectors / generators:
python3 scripts/jira_hygiene_agent.py         # hygiene violations
python3 scripts/generate_hygiene_dashboard.py
python3 scripts/github_pr_agent.py            # GitHub PR metrics (needs `gh` auth)
python3 scripts/sync_calendar.py              # 1:1s + PTO from Google Calendar
python3 scripts/generate_logs_dashboard.py

# Data-quality checks (auto-remediating by default; use --no-fix / --dry-run):
python3 scripts/qa_agent.py            # checks the DB + regenerates broken HTML
python3 scripts/qa_review_agent.py     # reviews the *rendered* HTML like a human

# Deploy to GitHub Pages (copies reports/html/ → docs/, renames landing page):
./scripts/deploy_to_github_pages.sh "commit message"

# Also publish to alveus (internal static host) — copies docs/ into the alveus
# app folder; --push branches, commits and opens the auto-merging PR:
./scripts/sync_to_alveus.sh          # copy + show what changed
./scripts/sync_to_alveus.sh --push   # ...and open the PR
```

Two publish targets, both fed from `docs/`:
- **GitHub Pages** — `docs/` on `main`; the dashboard's Publish button does this
  (commits `docs/` and pushes `HEAD:main`). Note this site is **publicly reachable**.
- **alveus** — `apps/dbxt-fbg/fantasy-dashboard/` in `fanatics-gaming/alveus`, served
  at `/dbxt-fbg/fantasy-dashboard/`, internal-only. A separate step
  (`sync_to_alveus.sh`); the Publish button does *not* update it. The alveus copy is
  static-only, so the four server-backed controls (`/api/ask`,
  `/api/dependency-notes`, `/api/feature-work-status`, `/api/member`) are inert there,
  exactly as on Pages.

There is no unit-test suite. The `qa_agent.py` / `qa_review_agent.py` scripts are
the de-facto verification layer — run them after changing collectors or generators.

## Credentials

- Jira: `JIRA_EMAIL` + `JIRA_API_TOKEN` (env or `config/.env`). `src/utils/config.py`
  does `${VAR}` substitution inside YAML config values.
- GitHub: uses the `gh` CLI — `gh auth status` must pass.
- Google Calendar: OAuth token cached at `config/token.pickle` (creds in
  `config/google_credentials.json`).

## Architecture

Three layers, one DB:

1. **Collect** (`src/collectors/`, invoked by `scripts/*_agent.py`)
   - `jira_api_collector.py` — `JiraAPICollector` talks to the Jira REST/Agile
     API directly (not MCP). Key methods: `collect_sprint_data`,
     `list_active_future_sprints` (uses `board_id` so empty sprints still appear),
     `count_open_children` (parent-scoped query — see below), `search_issues`.
   - `github_collector.py`, `calendar_collector.py`.
   - `refresh_jira_data.py` loads a JSON snapshot of Jira issues into the DB in
     one `BEGIN IMMEDIATE` transaction, recording status transitions into
     `ticket_status_history` before wiping/reloading `tickets`.

2. **Store** (`src/database/`)
   - `schema.py` — canonical schema + `SCHEMA_VERSION` (currently 6; the version
     history comments explain *why* each table/column exists — read them before
     altering tables). `init_database`, `get_connection`.
   - `queries.py` — all read queries powering the dashboards (velocity, cycle
     time, PR review time, PTO-adjusted capacity, predictability, etc.). This is
     the largest file; add new metrics here rather than inlining SQL in generators.
   - DB path comes from `config.database.path` in `team_config.yaml`
     (`data/metrics.db`). Separate SQLite files back the QA agents
     (`data/qa_history.sqlite`, `data/qa_review_history.sqlite`).

3. **Render** (`scripts/generate_html_report.py` + `generate_*_dashboard.py`)
   - `generate_html_report.py` is a monolith: its `main()` (~line 7450) calls one
     `generate_*_html()` per page — project_fantasy, features, readiness, team,
     story_points, epics, past_sprints, delivery_excellence, pull_requests,
     stakeholders, dependencies, plus per-member `member_<First_Last>.html`.
   - All writes go through `utils.io.atomic_write`. Nav is centralized in
     `utils.nav.generate_nav_menu` — register new pages there.
   - `serve_dashboard.py` serves `reports/html/`; its "Refresh Data" button runs
     the `REFRESH_STEPS` pipeline. `docs/` is a *deploy copy* of `reports/html/`
     (GitHub Pages), with `project_fantasy.html` → `index.html`.

### Cross-cutting modules (`src/utils/`)
- `statuses.py` — the single source of truth for status buckets:
  `CLOSED_STATUSES`, `IN_PROGRESS_STATUSES`, `OPEN_STATUSES`, `EXCLUDED_STATUSES`,
  `bucket_for`, `sql_placeholders`. Hygiene/metrics logic must reference these,
  not hard-coded status strings — a rule that forgets to exclude terminal
  statuses flags completed tickets forever.
- `sprint_names.py` — sprint label formatting (long/short/slot/milestone).
- `config.py`, `logging_config.py`, `competencies.py`, `project_name.py`,
  `nav.py`, `io.py`.

### Config (`config/team_config.yaml`)
The one place to re-point the whole dashboard. Notable keys: `jira.project_key`
(FNTSY), `jira.board_id`, `jira.initiative_key` (INIT-185 — drives the Feature→
Epic→Story initiative views), `jira.story_points_field` (+ fallbacks),
`calendar.pto_calendar_id`, `github.organization`. `dependencies.yaml` and
`feature_work_status.yaml` are round-tripped with `ruamel.yaml` (comments/order
preserved) by the server's write endpoints.

## Gotchas (learned the hard way)

- **Jira `/search/jql` must paginate by `nextPageToken`**, not `startAt` — the
  `startAt` path silently duplicates and truncates the whole dataset.
- **Epic "Open children" counts come from a dedicated parent-scoped query**
  (`epic_open_children` table), not the sprint-scoped `tickets` table — backlog
  children have no sprint and are never collected into `tickets`.
- **`ticket_status_history` has ~55× duplicate rows** — always `SELECT DISTINCT`
  before computing time-in-status / flow metrics.
- **GitHub collector only queries open+merged PRs**, so PRs closed-without-merge
  go stale as `open`; `_reconcile_open_prs()` re-checks and fixes them.
- **Rolled-over Story/Bug rows get a `_s<sprint>` suffix** in their closed sprint
  so `refresh_jira_data` doesn't evict them from the Sprint Report.
- **Future-dated `dep-weekly-status` entries** are Jira date-picker typos: QA
  flags them and the generator clamps them to today.
- **`PRAGMA journal_mode = WAL` silently downgrades `synchronous` from FULL(2)
  to NORMAL(1)** — this SQLite build sets `SQLITE_DEFAULT_WAL_SYNCHRONOUS=1`, so
  enabling WAL changes durability as a side effect. Under NORMAL the WAL isn't
  fsynced per commit, and an abrupt power loss can leave a DB physically shorter
  than the page count in its own header ("database disk image is malformed").
  This happened to all three DBs on 2026-07-22. `get_connection` and
  `HistoryStore._conn` now set `synchronous = FULL` immediately after enabling
  WAL — keep that pairing in any new connection helper.
- **`synchronous` is per-connection, not stored in the file.** Checking it with
  the `sqlite3` CLI reports NORMAL even when the app sets FULL, which looks like
  a regression and isn't. Verify through `get_connection()`, not the CLI.
- **Don't open these DBs with `mode=ro`.** A WAL-mode database has to create its
  `-shm` file to open at all, so read-only fails with "unable to open database
  file" on a perfectly healthy DB whenever no `-shm` exists (e.g. after a clean
  shutdown). `PRAGMA quick_check` doesn't modify data — open read-write.

## Automation

Cron drives `run_jira_collector_agent.sh` (every 15 min, work hours). Log
rotation via `scripts/lib/rotate_log.sh`. `.claude/scheduled_tasks.json` is
currently empty. Logs land in `logs/`.

**The nightly backup is launchd, not cron** — see `config/launchd/README.md`.
cron skips jobs it missed while the Mac was asleep, which is how the 02:00
backup silently failed to run on 2026-07-21/22 and left no clean backup before
that day's corruption. Plists are version-controlled in `config/launchd/` and
must be copied to `~/Library/LaunchAgents/` and `launchctl load`ed to take
effect. Anything that must not silently skip a day belongs there, not in cron.

`qa_agent.py`'s first check is `database_integrity` (`PRAGMA quick_check` over
`metrics.db` plus both QA stores, every 5 min). It has no `state_hash_fn` on
purpose — corruption originates outside the data, so no input digest predicts
it and it must never be skipped as unchanged. Most data checks declare
`depends_on=("database_integrity",)` so a bad file reports as the root cause
instead of a wall of identical "malformed" failures.
