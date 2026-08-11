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
    #
    # src.backup() raises DatabaseError if the *source* is corrupt. Letting that
    # propagate leaves behind the 0-byte file that connect() just created, which
    # reads as "a backup happened" to anyone eyeballing data/backups/ — that is
    # how a corrupt source went unnoticed for 7 days (2026-07-22 → 07-29). Catch
    # it, delete the stub, and fail loudly with a nonzero exit instead.
    copy_error = None
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(backup_path))
        try:
            # progress=None runs the copy in one call; it's 3–5MB of data.
            src.backup(dst)
        except sqlite3.DatabaseError as exc:
            copy_error = exc
        finally:
            dst.close()
    finally:
        src.close()

    if copy_error is not None:
        # Every connection above is closed, so the partial file (0 bytes, or a
        # truncated copy if the source went bad mid-read) is safe to remove.
        backup_path.unlink(missing_ok=True)
        logger.error(
            "Backup FAILED — source DB is unreadable (%s): %s. Partial backup "
            "removed. Check the source with: sqlite3 %s 'PRAGMA integrity_check;'",
            db_path, copy_error, db_path,
        )
        return 3

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

    # Sweep stale SQLite sidecars. The online .backup + integrity_check above
    # can leave a -wal/-shm pair next to a backup; once every connection here
    # is closed they're stale (backups are never reopened except at restore),
    # yet they accumulate (44 observed). Safe to drop all of them — this runs
    # single-threaded after every connection above is closed.
    swept = 0
    for f in list(backup_dir.glob("metrics-*.db-wal")) + list(backup_dir.glob("metrics-*.db-shm")):
        f.unlink(missing_ok=True)
        swept += 1

    if swept:
        logger.info("Swept %d stale WAL/SHM sidecar(s)", swept)

    return 0


if __name__ == "__main__":
    sys.exit(main())
