#!/usr/bin/env python3
"""
Sync 1-on-1 meetings from Google Calendar.
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

    try:
        collector = CalendarCollector(config)
        collector.collect_one_on_one_meetings()
        logger.info("Calendar sync complete")
        return 0

    except Exception as e:
        logger.error(f"Calendar sync failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
