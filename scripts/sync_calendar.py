#!/usr/bin/env python3
"""
Sync 1-on-1 meetings and engineer PTO from Google Calendar.
"""

import sys
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging
from collectors.calendar_collector import CalendarCollector


def main():
    """Sync calendar meetings."""
    config = load_config()
    # Pin this script's Python-logger output to its own log file so the
    # Agents dashboard shows the right history for Calendar Sync rather
    # than whichever module last wrote to the shared collector.log.
    config = {**config, 'logging': {**config.get('logging', {}),
                                     'file': str(Path(__file__).parent.parent / 'logs' / 'calendar_sync.log')}}
    setup_logging(config)
    logger = logging.getLogger(__name__)

    logger.info("Starting Google Calendar sync")

    collector = CalendarCollector(config)
    # Meetings and PTO are independent feeds — a failure in one shouldn't lose
    # the other. Track both so the exit code reflects any failure.
    failures = []

    try:
        collector.collect_one_on_one_meetings()
    except Exception as e:
        logger.error(f"1-on-1 meeting sync failed: {e}", exc_info=True)
        failures.append("meetings")

    try:
        collector.collect_pto()
    except Exception as e:
        logger.error(f"PTO sync failed: {e}", exc_info=True)
        failures.append("pto")

    if failures:
        logger.error("Calendar sync finished with failures: %s", ", ".join(failures))
        return 1

    logger.info("Calendar sync complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
