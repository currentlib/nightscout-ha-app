import glob
import logging
import os
from datetime import datetime, timezone

import aiosqlite

from app.config import settings

logger = logging.getLogger(__name__)

_PREFIX = "diabetes_"
_SUFFIX = ".db"


async def backup_database():
    """Daily local SQLite snapshot via VACUUM INTO, with retention cleanup.
    No-ops once DATABASE_URL points at a non-sqlite database (e.g. after a future
    Postgres migration) — this job should quietly stop doing anything at that point,
    not error. Off-server push (S3/GCS/etc.) is intentionally not implemented here;
    it needs a provider + credentials chosen first.
    """
    if not settings.DATABASE_URL.startswith("sqlite"):
        logger.debug("backup_database: DATABASE_URL is not sqlite, skipping.")
        return

    try:
        # Everything after the first "///" is the file path, regardless of which sqlite
        # driver prefix is configured (sqlite:///, sqlite+aiosqlite://, ...) — this also
        # naturally handles the absolute-path four-slash form, since the extra leading
        # "/" there just becomes part of the returned path.
        db_path = settings.DATABASE_URL.split("///", 1)[1]

        os.makedirs(settings.BACKUP_DIR, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(settings.BACKUP_DIR, f"{_PREFIX}{timestamp}{_SUFFIX}")

        # isolation_level=None -> autocommit; VACUUM INTO cannot run inside a transaction.
        async with aiosqlite.connect(db_path, isolation_level=None) as conn:
            # backup_path is built entirely from BACKUP_DIR (operator config) and a
            # server-generated timestamp, never external/user input.
            await conn.execute(f"VACUUM INTO '{backup_path}'")

        logger.info(f"Database backup created: {backup_path}")

        cutoff = datetime.now(timezone.utc).timestamp() - (settings.BACKUP_RETENTION_DAYS * 86400)
        pattern = os.path.join(settings.BACKUP_DIR, f"{_PREFIX}*{_SUFFIX}")
        for old_file in glob.glob(pattern):
            if os.path.getmtime(old_file) < cutoff:
                os.remove(old_file)
                logger.info(f"Deleted expired backup: {old_file}")

    except Exception as e:
        logger.error(f"Database backup job failed: {e}")
