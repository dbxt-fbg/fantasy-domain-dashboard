#!/usr/bin/env python3
"""Nightly SQLite backup with 30-day retention.

Uses sqlite3's online .backup API so we don't need to pause writers —
any concurrent hygiene/QA/collector run will continue without locks.

Writes to data/backups/metrics-YYYYMMDD.db, prunes backups older than
BACKUP_RETENTION_DAYS.
"""

import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from utils.config import load_config
from utils.logging_config import setup_logging

logger = logging.getLogger(__name__)

BACKUP_RETENTION_DAYS = 30


def main():
    config = load_config()
    setup_logging(config)

    db_path = Path(config['database']['path'])
    if not db_path.exists():
        logger.error("Source DB does not exist: %s", db_path)
        return 1

    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(exist_ok=True)

    today = datetime.now().strftime('%Y%m%d')
    backup_path = backup_dir / f"metrics-{today}.db"

    logger.info("Backing up %s → %s", db_path, backup_path)

    # Use online backup API. Opens a fresh connection (not WAL-shared) so we
    # don't interfere with anything the running agents are doing.
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            # progress=None runs the copy in one call; it's 3–5MB of data.
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    size_kb = backup_path.stat().st_size // 1024

    # Verify the backup is a valid SQLite file before declaring success. A
    # silently corrupt backup is worse than no backup — we'd discover it
    # only at restore time. PRAGMA integrity_check returns 'ok' on a clean DB.
    verifier = sqlite3.connect(str(backup_path))
    try:
        result = verifier.execute("PRAGMA integrity_check").fetchone()
    finally:
        verifier.close()
    if not result or result[0] != 'ok':
        logger.error("Backup integrity_check FAILED for %s: %s", backup_path, result)
        backup_path.unlink(missing_ok=True)
        return 2

    logger.info("Backup OK (%d KB, integrity_check passed)", size_kb)

    # Prune old backups
    cutoff = datetime.now() - timedelta(days=BACKUP_RETENTION_DAYS)
    pruned = 0
    for f in backup_dir.glob("metrics-*.db"):
        try:
            # Parse date from filename — safer than relying on mtime
            stem = f.stem.replace("metrics-", "")
            file_date = datetime.strptime(stem, "%Y%m%d")
        except ValueError:
            # Unexpected filename, leave it alone
            continue
        if file_date < cutoff:
            f.unlink()
            pruned += 1

    if pruned:
        logger.info("Pruned %d backup(s) older than %d days", pruned, BACKUP_RETENTION_DAYS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
