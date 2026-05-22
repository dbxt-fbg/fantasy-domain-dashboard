"""
Database schema definition and initialization.
"""

import sqlite3
from pathlib import Path
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Bump whenever a schema-affecting change ships. Currently informational —
# there's no migration runner that gates on this — but tracking it surfaces
# drift between the canonical schema and live DBs.
#
# History:
#   1 — Initial schema (sprints, tickets, snapshots, github, hygiene, 1-on-1).
#   2 — Added: status_at_sprint_end column on tickets;
#       composite indexes idx_tickets_sprint_status_type and
#       idx_ticket_status_history_sprint;
#       hygiene_issues memory columns (first_seen_at, last_seen_at, etc.).
#   3 — Added: is_placeholder column on sprints so synthesized FE/BE
#       placeholders (filled in for missing slots) persist between runs
#       and share one source of truth with real Jira sprints.
SCHEMA_VERSION = 3

SCHEMA_SQL = """
-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Tracks active and historical sprints. is_placeholder flags rows that
-- the dashboard synthesised to fill missing FE/BE slots — those rows have
-- a synthetic negative jira_sprint_id (Jira's are always positive) and
-- carry computed start/end dates rather than fetched ones.
CREATE TABLE IF NOT EXISTS sprints (
    sprint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    jira_sprint_id INTEGER UNIQUE NOT NULL,
    sprint_name TEXT NOT NULL,
    state TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    goal TEXT,
    is_placeholder INTEGER NOT NULL DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL
);

-- Daily snapshots of sprint metrics (for burndown)
CREATE TABLE IF NOT EXISTS sprint_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL,
    snapshot_date TEXT NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    total_story_points REAL DEFAULT 0,
    completed_story_points REAL DEFAULT 0,
    remaining_story_points REAL DEFAULT 0,
    total_tickets INTEGER DEFAULT 0,
    open_tickets INTEGER DEFAULT 0,
    closed_tickets INTEGER DEFAULT 0,
    in_progress_tickets INTEGER DEFAULT 0,
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id),
    UNIQUE(sprint_id, snapshot_date)
);

-- Track individual tickets (for clickable lists)
CREATE TABLE IF NOT EXISTS tickets (
    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT UNIQUE NOT NULL,
    sprint_id INTEGER,
    summary TEXT NOT NULL,
    status TEXT NOT NULL,
    status_at_sprint_end TEXT,
    assignee_account_id TEXT,
    assignee_display_name TEXT,
    story_points REAL DEFAULT 0,
    issue_type TEXT,
    priority TEXT,
    created_at TEXT,
    updated_at TEXT,
    ticket_url TEXT,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id)
);

-- Historical status changes for tickets
CREATE TABLE IF NOT EXISTS ticket_status_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    sprint_id INTEGER,
    old_status TEXT,
    new_status TEXT,
    changed_at TEXT NOT NULL,
    FOREIGN KEY (ticket_key) REFERENCES tickets(ticket_key),
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id)
);

-- Daily snapshots of individual developer metrics
CREATE TABLE IF NOT EXISTS developer_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL,
    developer_id TEXT NOT NULL,
    developer_name TEXT NOT NULL,
    snapshot_date TEXT NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    assigned_story_points REAL DEFAULT 0,
    completed_story_points REAL DEFAULT 0,
    remaining_story_points REAL DEFAULT 0,
    tickets_in_progress INTEGER DEFAULT 0,
    tickets_completed INTEGER DEFAULT 0,
    tickets_todo INTEGER DEFAULT 0,
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id),
    UNIQUE(sprint_id, developer_id, snapshot_date)
);

-- Track developer velocity across sprints
CREATE TABLE IF NOT EXISTS developer_velocity (
    velocity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sprint_id INTEGER NOT NULL,
    developer_id TEXT NOT NULL,
    developer_name TEXT NOT NULL,
    completed_story_points REAL DEFAULT 0,
    total_tickets_completed INTEGER DEFAULT 0,
    calculated_at TEXT NOT NULL,
    FOREIGN KEY (sprint_id) REFERENCES sprints(sprint_id),
    UNIQUE(sprint_id, developer_id)
);

-- GitHub PR metrics (point-in-time snapshots)
CREATE TABLE IF NOT EXISTS github_pr_snapshots (
    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_timestamp TEXT NOT NULL,
    developer_github_username TEXT NOT NULL,
    developer_name TEXT,
    open_pr_count INTEGER NOT NULL DEFAULT 0,
    pr_details TEXT,
    UNIQUE(snapshot_timestamp, developer_github_username)
);

-- Historical PR data for calculating averages
CREATE TABLE IF NOT EXISTS github_prs (
    pr_id INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_number INTEGER NOT NULL,
    repository TEXT NOT NULL,
    author_github_username TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    merged_at TEXT,
    closed_at TEXT,
    pr_url TEXT,
    lines_added INTEGER DEFAULT 0,
    lines_deleted INTEGER DEFAULT 0,
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    UNIQUE(repository, pr_number)
);

-- Reviews and comments on GitHub PRs, keyed by reviewer
CREATE TABLE IF NOT EXISTS github_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    pr_url TEXT,
    reviewer_github_username TEXT NOT NULL,
    state TEXT NOT NULL,          -- APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED | PENDING
    inline_comment_count INTEGER NOT NULL DEFAULT 0,
    submitted_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(repository, pr_number, reviewer_github_username, submitted_at)
);

-- Issue-style comments on GitHub PRs (non-inline), keyed by commenter
CREATE TABLE IF NOT EXISTS github_pr_comments (
    comment_id INTEGER PRIMARY KEY,   -- GitHub's own comment id
    repository TEXT NOT NULL,
    pr_number INTEGER NOT NULL,
    pr_url TEXT,
    commenter_github_username TEXT NOT NULL,
    created_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

-- Hygiene tracking: per-status duration and currently-flagged issues.
-- Previously defined in src/database/hygiene_schema.py; merged here so the
-- full schema is one file.
CREATE TABLE IF NOT EXISTS status_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_key TEXT NOT NULL,
    status TEXT NOT NULL,
    entered_at TIMESTAMP NOT NULL,
    exited_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(ticket_key, status, entered_at)
);

CREATE TABLE IF NOT EXISTS hygiene_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_type TEXT NOT NULL,
    ticket_key TEXT NOT NULL,
    ticket_summary TEXT,
    ticket_url TEXT,
    assignee_display_name TEXT,
    status TEXT,
    details TEXT,
    -- `detected_at` is kept for backward-compat with the existing dashboard
    -- generators; agent-level memory uses the columns below.
    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    first_seen_at TIMESTAMP,       -- first run where this (type, ticket) was detected
    last_seen_at  TIMESTAMP,       -- most recent run where it was detected
    resolved_at   TIMESTAMP,       -- last time it disappeared; NULL when currently open
    times_seen    INTEGER DEFAULT 1,
    times_resolved INTEGER DEFAULT 0,
    UNIQUE(issue_type, ticket_key)
);

-- Recurring 1-on-1 meetings from Google Calendar
CREATE TABLE IF NOT EXISTS one_on_one_meetings (
    meeting_id INTEGER PRIMARY KEY AUTOINCREMENT,
    developer_name TEXT NOT NULL,
    jira_account_id TEXT,
    github_username TEXT,
    event_id TEXT,
    summary TEXT NOT NULL,
    recurrence_rule TEXT,
    day_of_week TEXT,
    time_of_day TEXT,
    duration_minutes INTEGER,
    next_occurrence TEXT,
    last_synced_at TEXT NOT NULL,
    UNIQUE(developer_name)
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_tickets_sprint ON tickets(sprint_id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets(assignee_account_id);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
CREATE INDEX IF NOT EXISTS idx_tickets_sprint_status_type ON tickets(sprint_id, status, issue_type);
CREATE INDEX IF NOT EXISTS idx_sprint_snapshots_date ON sprint_snapshots(sprint_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_developer_snapshots_date ON developer_snapshots(sprint_id, developer_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_ticket_status_history_sprint ON ticket_status_history(sprint_id, ticket_key);
CREATE INDEX IF NOT EXISTS idx_github_prs_author ON github_prs(author_github_username);
CREATE INDEX IF NOT EXISTS idx_github_prs_merged ON github_prs(merged_at);
CREATE INDEX IF NOT EXISTS idx_github_reviews_reviewer ON github_reviews(reviewer_github_username, submitted_at);
CREATE INDEX IF NOT EXISTS idx_github_pr_comments_commenter ON github_pr_comments(commenter_github_username, created_at);
CREATE INDEX IF NOT EXISTS idx_one_on_one_developer ON one_on_one_meetings(developer_name);
CREATE INDEX IF NOT EXISTS idx_status_changes_ticket ON status_changes(ticket_key, status);
CREATE INDEX IF NOT EXISTS idx_hygiene_issues_type ON hygiene_issues(issue_type);
"""


def ensure_ticket_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add status_at_sprint_end to tickets if missing.

    The column was originally added by backfill_past_sprint.py at runtime;
    this brings the canonical schema in sync so freshly-initialised DBs
    have it too, and existing DBs get it backfilled here on first startup.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(tickets)")
    existing = {row[1] for row in cur.fetchall()}
    if not existing:
        return  # table doesn't exist yet; SCHEMA_SQL will create it.
    if "status_at_sprint_end" not in existing:
        try:
            cur.execute("ALTER TABLE tickets ADD COLUMN status_at_sprint_end TEXT")
            conn.commit()
        except sqlite3.OperationalError:
            # Race with another initialiser — safe to ignore; column will exist either way.
            pass


def ensure_sprint_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add is_placeholder to sprints if missing.

    Synthesised FE/BE placeholders that fill in missing slots are now
    persisted as sprint rows (with negative synthetic jira_sprint_id) so
    every page reads dates from one source. This adds the flag column
    on existing DBs without disturbing real Jira sprints.
    """
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(sprints)")
    existing = {row[1] for row in cur.fetchall()}
    if not existing:
        return
    if "is_placeholder" not in existing:
        try:
            cur.execute(
                "ALTER TABLE sprints ADD COLUMN is_placeholder INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass


def ensure_hygiene_memory_columns(conn: sqlite3.Connection) -> None:
    """Idempotent migration: add memory columns to hygiene_issues if missing.

    Older DBs were created with just detected_at. The memory-aware hygiene
    agent needs first_seen_at / last_seen_at / resolved_at / times_seen /
    times_resolved so it can reason about issue age across runs.

    Designed to be safe under concurrent writers:
      * Reads PRAGMA info first to see whether any work is needed.
      * Skips the backfill UPDATE entirely if all columns already exist AND
        every row has a first/last_seen_at set (the steady state).
      * If contended (database is locked), retries a few times with small
        backoffs instead of crashing the agent that calls us.
    """
    import time as _time

    cur = conn.cursor()
    cur.execute("PRAGMA table_info(hygiene_issues)")
    existing = {row[1] for row in cur.fetchall()}
    if not existing:
        # Table doesn't exist yet; the schema script will create it.
        return
    target = {
        "first_seen_at": "TIMESTAMP",
        "last_seen_at":  "TIMESTAMP",
        "resolved_at":   "TIMESTAMP",
        "times_seen":    "INTEGER DEFAULT 1",
        "times_resolved": "INTEGER DEFAULT 0",
    }
    missing = [n for n in target if n not in existing]

    # Fast path: migration already applied. Confirm no backfill is needed
    # before we take a write lock. A cheap EXISTS query is almost free.
    if not missing:
        try:
            cur.execute(
                "SELECT 1 FROM hygiene_issues "
                "WHERE first_seen_at IS NULL OR last_seen_at IS NULL LIMIT 1"
            )
            if cur.fetchone() is None:
                return  # nothing to do
        except sqlite3.OperationalError:
            # If we can't even read (locked), don't block startup — the
            # next agent to run will finish the job.
            return

    # Slow path: do the ALTER + backfill with short retries so a concurrent
    # writer doesn't fail the whole agent startup.
    for attempt in range(5):
        try:
            for name in missing:
                cur.execute(f"ALTER TABLE hygiene_issues ADD COLUMN {name} {target[name]}")
            cur.execute(
                "UPDATE hygiene_issues "
                "SET first_seen_at = COALESCE(first_seen_at, detected_at), "
                "    last_seen_at  = COALESCE(last_seen_at,  detected_at) "
                "WHERE first_seen_at IS NULL OR last_seen_at IS NULL"
            )
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == 4:
                raise
            _time.sleep(0.5 * (attempt + 1))
    # Unreachable — loop either returns or raises


def get_connection(db_path: str) -> sqlite3.Connection:
    """
    Get a database connection.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        sqlite3.Connection: Database connection
    """
    # 30s timeout so concurrent agents (hygiene, calendar, GitHub, cron) don't
    # 'database is locked' each other when they overlap. WAL mode lets readers and
    # a single writer coexist; combined with busy_timeout this covers both long
    # writers (hygiene) and scheduled cron overlap.
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row  # Enable column access by name
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_database(config: dict) -> None:
    """
    Initialize the database schema.

    Args:
        config: Configuration dictionary containing database path
    """
    db_path = config['database']['path']

    # Ensure parent directory exists
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initializing database at {db_path}")

    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        # Execute schema
        cursor.executescript(SCHEMA_SQL)

        # Idempotent migrations for tables created before newer columns existed.
        ensure_ticket_columns(conn)
        ensure_sprint_columns(conn)
        ensure_hygiene_memory_columns(conn)

        # Check/update schema version
        cursor.execute("SELECT version FROM schema_version WHERE version = ?", (SCHEMA_VERSION,))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,)
            )
            logger.info(f"Applied schema version {SCHEMA_VERSION}")

        conn.commit()
        logger.info("Database initialized successfully")

    except Exception as e:
        conn.rollback()
        logger.error(f"Failed to initialize database: {e}")
        raise
    finally:
        conn.close()


def get_schema_version(db_path: str) -> Optional[int]:
    """
    Get the current schema version.

    Args:
        db_path: Path to the SQLite database file

    Returns:
        int: Current schema version, or None if not set
    """
    conn = get_connection(db_path)
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT MAX(version) as version FROM schema_version")
        result = cursor.fetchone()
        return result['version'] if result else None
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
