#!/usr/bin/env python3
"""GitHub PR Agent - Collects pull request metrics from GitHub.

By default, collects for every team member in team_config.yaml. Pass
--only <github_username> (repeatable) to target specific members — useful
for backfilling when an earlier run failed for one or two people.
"""

import argparse
import copy
import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from collectors.github_collector import GitHubCollector

logger = logging.getLogger(__name__)


def _parse_args():
    p = argparse.ArgumentParser(description="GitHub PR Agent.")
    p.add_argument(
        '--only',
        action='append',
        metavar='GITHUB_USERNAME',
        help='Restrict collection to one or more github_username values. Repeatable.',
    )
    return p.parse_args()


def _acquire_pr_lock():
    """Exclusive flock on data/.locks/github_pr_agent.lock so two runs can't overlap.

    A full run takes well over 10 minutes (a `gh search prs` per team member,
    then one `gh pr view` per still-open PR), which is longer than any sensible
    cron interval. Without this, a second instance starts while the first is
    mid-collection and both write the same github_prs rows.

    Mirrors _acquire_qa_lock in qa_agent.py. Returns the open handle (the caller
    keeps it alive for the process lifetime); None if another run holds it.
    """
    import fcntl
    lock_dir = Path(__file__).parent.parent / 'data' / '.locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    fh = open(lock_dir / 'github_pr_agent.lock', 'w')
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main():
    args = _parse_args()
    config = load_config()
    setup_logging(config)

    lock = _acquire_pr_lock()
    if lock is None:
        # Exit cleanly, not as an error — cron will fire again and the running
        # instance will have finished by then.
        logger.warning("Another github_pr_agent run is in progress — exiting.")
        return 0

    if args.only:
        # Filter the team list down to the requested members. We deep-copy so we
        # don't mutate the original config object in memory.
        requested = set(args.only)
        filtered = [
            m for m in config.get('team_members', [])
            if m.get('github_username') in requested
        ]
        missing = requested - {m.get('github_username') for m in filtered}
        if missing:
            logger.warning(
                "Requested github_usernames not found in config: %s",
                ", ".join(sorted(missing)),
            )
        if not filtered:
            logger.error("No matching team members; nothing to do.")
            return 1
        config = copy.deepcopy(config)
        config['team_members'] = filtered
        logger.info(
            "GitHub PR Agent (targeted) - %d member(s): %s",
            len(filtered),
            ", ".join(m.get('github_username', '?') for m in filtered),
        )
    else:
        logger.info("GitHub PR Agent - Collecting pull request metrics for full team...")

    gh = GitHubCollector(config)
    gh.collect_pr_metrics()

    # Don't report success when the stale-open reconcile couldn't reach GitHub.
    #
    # The reconcile deliberately leaves a row alone on a transient error, so it
    # can fail for every single PR and still return quietly. This agent used to
    # log "completed successfully" regardless: on 2026-06-26 it stopped being
    # able to authenticate, logged 1007 auth failures, exited 0 every time, and
    # nobody noticed until someone spotted fantasy-api#259 (merged) and #103
    # (closed) still showing as open on the Repositories page six weeks later.
    #
    # Partial failures stay a warning — a handful of transient errors is normal.
    # Total failure means the pass did nothing and the exit code should say so.
    summary = gh.reconcile_summary or {}
    checked = summary.get('checked', 0)
    unreachable = summary.get('unreachable', 0)

    if summary.get('error'):
        logger.error(
            "❌ Open-PR reconcile failed outright (%s) — closed/merged PRs may "
            "still show as open on the Repositories page.", summary['error'])
        return 3

    if checked and unreachable == checked:
        logger.error(
            "❌ Open-PR reconcile could not reach GitHub for any of its %d "
            "re-check(s). No PR state was updated, so closed/merged PRs will "
            "keep showing as open. Check `gh auth status` in the environment "
            "this agent runs in.", checked)
        return 3

    if unreachable:
        logger.warning(
            "⚠️  Open-PR reconcile completed with %d of %d re-check(s) "
            "unreachable; those rows kept their previous state.",
            unreachable, checked)

    logger.info(
        "✅ GitHub PR collection completed successfully! "
        "(reconcile: %d checked, %d corrected, %d unreachable)",
        checked, summary.get('reconciled', 0), unreachable)
    return 0


if __name__ == "__main__":
    sys.exit(main())
