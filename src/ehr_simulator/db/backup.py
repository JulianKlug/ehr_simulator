"""SQLite online backup helper.

``create_backup`` uses :meth:`sqlite3.Connection.backup` (the online backup
API — blocking, safe under WAL) to copy the live DB to a timestamped file
in ``backup_dir``. The destination directory is auto-created.

At pilot scale (≤30 patients, ≤10 MB DB) the backup completes well inside
uvicorn's default 5s graceful timeout; researchers on larger DBs should
pass ``--graceful-timeout 60`` (review-fix R12 + spec §13).

Called from (a) ``ehr-simulator backup`` (unconditional) and (b) the
lifespan shutdown branch (only when ``app.state.write_counter > 0``, per
review-fix R8).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from ehr_simulator.logging import get_logger


def create_backup(db_path: Path, backup_dir: Path) -> Path:
    """Snapshot ``db_path`` into ``backup_dir / ehr_simulator_<utc>.db``.

    Returns the absolute path to the new backup file. Creates
    ``backup_dir`` if it doesn't exist.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"ehr_simulator_{stamp}.db"

    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    bytes_copied = dest.stat().st_size
    get_logger().info(
        "backup ok",
        event_kind="db.backup.ok",
        dest=str(dest),
        bytes_copied=bytes_copied,
    )
    return dest
