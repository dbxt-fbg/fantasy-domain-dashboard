#!/usr/bin/env python3
"""
Unified Jira Collector Agent - Fetches all Jira data once per run.
Replaces duplicate API calls from Stories, Story Points, Epics, and Hygiene agents.
Runs every 15 minutes (6am-6pm PT).
"""

import sys
import logging
import time
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from database.schema import init_database

# Import the direct API collector
from collectors.jira_api_collector import JiraAPICollector

logger = logging.getLogger(__name__)

MAX_RETRIES = 6
RETRY_DELAY = 90  # 1.5 min between retries — budget is ~9 minutes total
                  # so concurrent hygiene + gh_pr runs can clear


def collect_and_process_jira_data():
    """
    Fetch Jira data once and process for all purposes.
    Returns True on success, False on failure.
    """
    try:
        logger.info("=" * 80)
        logger.info(f"Unified Jira Collector - {datetime.now()}")
        logger.info("=" * 80)

        # Load config and setup
        config = load_config()
        setup_logging(config)
        init_database(config)

        # Initialize Jira API collector
        logger.info("Connecting to Jira API...")
        jira = JiraAPICollector(config)

        # Fetch ALL sprint data ONCE
        logger.info("Fetching sprint data (stories, epics, hygiene, story points)...")
        sprint_data = jira.collect_sprint_data()

        logger.info(f"Retrieved {len(sprint_data.get('issues', []))} issues from Jira")

        # Process the data using existing refresh script
        # This updates tickets, sprints, and snapshots
        logger.info("Processing and storing data...")
        with open('/tmp/jira_unified.json', 'w') as f:
            import json
            json.dump(sprint_data, f)

        from refresh_jira_data import refresh_jira_data
        refresh_jira_data('/tmp/jira_unified.json')

        # Open child-issue counts per epic. Must be a dedicated parent-scoped
        # query: an epic's open children are often backlog items with no sprint,
        # so they never appear in the sprint-scoped pull above and can't be
        # counted from the tickets table. Source the epic list from the DB so
        # we cover every epic the Gantt renders (active/future AND closed
        # window), then persist counts into epic_open_children.
        try:
            from database.schema import get_connection
            db_path = config['database']['path']
            _conn = get_connection(db_path)
            _cur = _conn.cursor()
            _cur.execute("SELECT DISTINCT ticket_key FROM tickets WHERE issue_type = 'Epic'")
            epic_keys = [r['ticket_key'] for r in _cur.fetchall()]
            _conn.close()

            counts = jira.count_open_children(epic_keys)

            now_iso = datetime.now().isoformat()
            _conn = get_connection(db_path)
            _cur = _conn.cursor()
            for epic_key, open_count in counts.items():
                _cur.execute(
                    """
                    INSERT INTO epic_open_children (epic_key, open_count, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(epic_key) DO UPDATE SET
                        open_count = excluded.open_count,
                        updated_at = excluded.updated_at
                    """,
                    (epic_key, open_count, now_iso),
                )
            _conn.commit()
            _conn.close()
            logger.info(f"Persisted open-children counts for {len(counts)} epic(s)")
        except Exception as e:
            logger.warning(f"Open-children count step failed (non-fatal): {e}")

        # Backfill any sprint whose end_date has passed but is still labeled
        # active/future locally — otherwise refresh_jira_data wipes its tickets
        # on the next run. Non-fatal: log + continue if any one fails.
        try:
            from database.schema import get_connection
            from backfill_past_sprint import backfill_past_sprint
            db_path = config['database']['path']
            _conn = get_connection(db_path)
            _cur = _conn.cursor()
            _cur.execute(
                """
                SELECT jira_sprint_id, sprint_name FROM sprints
                 WHERE date(end_date) < date('now')
                   AND state IN ('active', 'future')
                """
            )
            stale = _cur.fetchall()
            _conn.close()
            for row in stale:
                try:
                    backfill_past_sprint(row['jira_sprint_id'], db_path=db_path)
                except Exception as e:
                    logger.warning(
                        f"Auto-backfill failed for {row['sprint_name']} "
                        f"(jira_sprint_id={row['jira_sprint_id']}): {e}"
                    )
        except Exception as e:
            logger.warning(f"Auto-backfill scan failed (non-fatal): {e}")

        # Re-pull live Jira state (status / assignee / summary) for tickets
        # in CLOSED sprints inside the dashboard's recent-sprint window.
        # refresh_jira_data only touches active/future sprints, so an epic
        # that moved to Done after its sprint closed otherwise sits stale
        # forever. This keeps the rendered status of closed-sprint tickets
        # aligned with Jira without reopening the historical metrics
        # (story_points, status_at_sprint_end stay frozen).
        try:
            from refresh_closed_sprint_tickets import refresh_closed_sprint_tickets
            refresh_closed_sprint_tickets()
        except Exception as e:
            logger.warning(f"Closed-sprint refresh failed (non-fatal): {e}")

        # Run discover_team_members and sync_project_fantasy on cooldowns —
        # these don't need to fire every 15 minutes. The collector itself runs
        # that often; these side scripts only need to keep up with rare config
        # drift. Stamp files in data/ track when they last ran.
        import subprocess
        from os.path import getmtime
        stamp_dir = Path(__file__).parent.parent / 'data' / '.agent_stamps'
        stamp_dir.mkdir(parents=True, exist_ok=True)
        now_ts = time.time()

        def _run_with_cooldown(script_name: str, cooldown_hours: float, timeout_s: int):
            stamp = stamp_dir / f"{script_name}.stamp"
            try:
                age_h = (now_ts - getmtime(stamp)) / 3600 if stamp.exists() else float('inf')
            except OSError:
                age_h = float('inf')
            if age_h < cooldown_hours:
                logger.info(
                    f"Skipping {script_name} (last ran {age_h:.1f}h ago, cooldown {cooldown_hours}h)"
                )
                return
            try:
                subprocess.run(
                    [sys.executable, str(Path(__file__).parent / script_name)],
                    check=False, timeout=timeout_s,
                )
            except Exception as e:
                logger.warning(f"{script_name} failed (non-fatal): {e}")
                return
            # Touch the stamp only after a successful subprocess.run. Previously
            # any failure to write the stamp was silently swallowed, which
            # opened the cooldown back up and re-ran the script every cycle.
            try:
                stamp.touch()
            except OSError as e:
                logger.error(
                    "Cannot write cooldown stamp %s: %s — cooldown will not engage. "
                    "Check permissions on data/.agent_stamps/.",
                    stamp, e,
                )

        # Team member roster changes rarely — once an hour is plenty.
        _run_with_cooldown('discover_team_members.py', cooldown_hours=1.0, timeout_s=120)
        # Project Fantasy INIT-185 subtree changes even more rarely.
        _run_with_cooldown('sync_project_fantasy.py', cooldown_hours=6.0, timeout_s=300)

        logger.info("✅ Unified Jira collection complete!")
        logger.info("Data available for: Stories, Epics, Story Points, Hygiene")
        return True

    except Exception as e:
        logger.error(f"❌ Failed to collect Jira data: {e}", exc_info=True)
        return False


def _acquire_collector_lock():
    """Acquire an exclusive POSIX flock on data/.locks/jira_collector.lock.

    Returns the open file handle (caller must keep it alive for the duration
    of the run; closing it releases the lock). Returns None when another
    instance holds the lock — caller should exit early.

    With MAX_RETRIES=6 × 90s sleep, a slow run can stretch past the next
    cron firing (15 min). Without this guard, two collector instances would
    write to the same tickets table concurrently.
    """
    import fcntl
    lock_dir = Path(__file__).parent.parent / 'data' / '.locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / 'jira_collector.lock'
    fh = open(lock_path, 'w')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main():
    """Main entry point with retry logic."""
    setup_logging({'logging': {'level': 'INFO', 'file': 'logs/jira_collector_agent.log'}})

    lock = _acquire_collector_lock()
    if lock is None:
        logger.warning("Another jira_collector instance is running — exiting.")
        return 0

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            if MAX_RETRIES > 1:
                logger.info(f"Attempt {attempt}/{MAX_RETRIES}")

            success = collect_and_process_jira_data()

            if success:
                logger.info("Jira collector agent complete.")
                return 0

            if attempt < MAX_RETRIES:
                logger.warning(f"Retrying in {RETRY_DELAY} seconds...")
                time.sleep(RETRY_DELAY)
            else:
                logger.error(f"Failed after {MAX_RETRIES} attempts.")
                return 1

        return 1
    finally:
        # Closing the handle releases the flock.
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
