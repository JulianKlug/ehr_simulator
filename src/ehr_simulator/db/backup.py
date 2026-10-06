"""SQLite online backup helper.

``create_backup`` uses :meth:`sqlite3.Connection.backup` (the online backup
API — blocking, safe under WAL) to copy the live DB into ``backup_root``.
The destination directory is auto-created.

S11m backup identity: a study bound database is copied to::

    <backup_root>/<study_id>/study_<study_id>_schema_<N>_<UTC>.db

so two studies never share a directory, and the copy is reopened read only
to prove it carries the same ``study_identity`` and schema version. An
unbound database keeps the legacy ``<backup_root>/ehr_simulator_<UTC>.db``.
A destination is created exclusively: an existing file is never overwritten;
a second backup within the same second takes the next ``_<n>`` suffix.

At pilot scale (≤30 patients, ≤10 MB DB) the backup completes well inside
uvicorn's default 5s graceful timeout; researchers on larger DBs should
pass ``--graceful-timeout 60`` (review-fix R12 + spec §13).

Called from (a) ``ehr-simulator backup`` (unconditional) and (b) the
lifespan shutdown branch (only when ``app.state.write_counter > 0``, per
review-fix R8).
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ehr_simulator.db import migrations
from ehr_simulator.db.connection import AccessMode, connect
from ehr_simulator.db.exceptions import BackupIdentityError
from ehr_simulator.logging import get_logger

__all__ = ["BackupIdentity", "create_backup", "read_identity"]

_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: Same-second backups before giving up (``…Z.db``, ``…Z_2.db`` … ``…Z_99.db``).
_MAX_SAME_SECOND = 99


@dataclass(frozen=True)
class BackupIdentity:
    """What makes a copy attributable: ``study_id`` (``None`` = unbound) and
    the highest applied migration (``None`` = no schema_migrations)."""

    study_id: str | None
    schema_version: int | None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_identity(conn: sqlite3.Connection) -> BackupIdentity:
    """The study and schema generation stored in one database."""
    study_id = None
    if _table_exists(conn, "study_identity"):
        row = conn.execute("SELECT study_id FROM study_identity WHERE singleton = 1").fetchone()
        study_id = row[0] if row is not None else None

    return BackupIdentity(study_id=study_id, schema_version=migrations.schema_version(conn))


def _destination(backup_root: Path, identity: BackupIdentity, stamp: str) -> Path:
    if identity.study_id is None:
        return backup_root / f"ehr_simulator_{stamp}.db"
    name = f"study_{identity.study_id}_schema_{identity.schema_version}_{stamp}.db"
    return backup_root / identity.study_id / name


def _reserve(dest: Path) -> Path:
    """Create ``dest`` (or its first free ``_<n>`` sibling) exclusively.

    ``study_x_schema_13_20260927T140000Z.db`` taken → ``…Z_2.db``: a repeated
    backup never clobbers, and a restart within one second loses nothing.
    """
    for n in range(1, _MAX_SAME_SECOND + 1):
        candidate = dest if n == 1 else dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return candidate
    raise BackupIdentityError(f"backup destination already exists: {dest}")


def create_backup(
    db_path: Path, backup_root: Path, *, expected_study_id: str | None = None
) -> Path:
    """Snapshot ``db_path`` into ``backup_root`` under its study identity.

    Returns the absolute path of the new copy. ``expected_study_id`` (study
    mode callers) must equal the stored identity, else nothing is created.

    Raises:
        BackupIdentityError: identity mismatch, a DB without schema
            version, no free destination name, or a copy that does not
            reopen with the source's identity (the copy is then removed).
    """
    # Read only: a missing path raises instead of creating an empty file.
    src = connect(db_path, access=AccessMode.READ_ONLY)
    try:
        identity = read_identity(src)
        if expected_study_id is not None and identity.study_id != expected_study_id:
            raise BackupIdentityError(
                f"database {db_path} belongs to study {identity.study_id!r}, "
                f"not {expected_study_id!r}; no backup written"
            )
        if identity.schema_version is None:
            raise BackupIdentityError(f"database {db_path} has no schema version; no backup")

        stamp = datetime.now(UTC).strftime(_STAMP_FORMAT)
        dest = _destination(backup_root, identity, stamp)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest = _reserve(dest)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
                # One self-contained file: no -wal/-shm sidecars next to it.
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
            _verify_copy(dest, identity)
        except Exception:
            with contextlib.suppress(OSError):
                dest.unlink()
            raise
    finally:
        src.close()

    bytes_copied = dest.stat().st_size
    get_logger().info(
        "backup ok",
        event_kind="db.backup.ok",
        dest=str(dest),
        bytes_copied=bytes_copied,
        study_id=identity.study_id,
        schema_version=identity.schema_version,
    )
    return dest


def _verify_copy(dest: Path, expected: BackupIdentity) -> None:
    copy = connect(dest, access=AccessMode.READ_ONLY)
    try:
        found = read_identity(copy)
    finally:
        copy.close()
    if found != expected:
        raise BackupIdentityError(
            f"backup {dest} reopened as {found}, expected {expected}; copy removed"
        )
