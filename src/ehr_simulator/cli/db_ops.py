"""``migrate`` and ``backup`` (S6): database maintenance."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import typer

from ehr_simulator.cli._common import LOG_DIR, app_typer


@app_typer.command()
def migrate(
    db_path: Path = typer.Option(
        Path("data/ehr_simulator.db"),
        "--db-path",
        help="Path to the SQLite DB to migrate.",
    ),
) -> None:
    """Apply all unapplied DB migrations + checkpoint the WAL.

    The post-migration ``PRAGMA wal_checkpoint(TRUNCATE)`` ensures a
    researcher who ``cp``'s the bare ``.db`` file afterwards doesn't lose
    un-checkpointed writes (review-fix R14).
    """
    from ehr_simulator.db import apply_migrations, connect
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        versions = apply_migrations(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    if versions:
        typer.echo(f"Applied migrations: {versions}")
    else:
        typer.echo("No migrations to apply.")


@app_typer.command()
def backup(
    db_path: Path = typer.Option(
        Path("data/ehr_simulator.db"),
        "--db-path",
        help="Path to the SQLite DB to back up.",
    ),
    backup_dir: Path = typer.Option(
        Path("data/backups"),
        "--backup-dir",
        help=(
            "Backup root. A study bound DB goes to <root>/<study_id>/"
            "study_<study_id>_schema_<N>_<UTC>.db. Auto-created."
        ),
    ),
) -> None:
    """Snapshot the SQLite DB into --backup-dir under its study identity."""
    from ehr_simulator.db.backup import create_backup, read_identity
    from ehr_simulator.db.exceptions import BackupIdentityError
    from ehr_simulator.logging import setup_logging

    setup_logging(LOG_DIR)
    if not db_path.exists():
        typer.echo(f"Error: db_path does not exist: {db_path}", err=True)
        raise typer.Exit(code=1)
    try:
        dest = create_backup(db_path, backup_dir)
    except (BackupIdentityError, OSError, sqlite3.Error) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Backup written to: {dest}")
    copy = sqlite3.connect(dest)
    try:
        identity = read_identity(copy)
    finally:
        copy.close()
    if identity.study_id is not None:
        typer.echo(f"Study: {identity.study_id}, schema version {identity.schema_version}")
