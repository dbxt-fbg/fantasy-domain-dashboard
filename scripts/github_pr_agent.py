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


def main():
    args = _parse_args()
    config = load_config()
    setup_logging(config)

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
    logger.info("✅ GitHub PR collection completed successfully!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
