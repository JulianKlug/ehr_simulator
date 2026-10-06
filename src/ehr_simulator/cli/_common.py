"""Shared CLI plumbing: the typer app, ``main`` and the study database gate."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import typer

if TYPE_CHECKING:
    import sqlite3

    from ehr_simulator.config import StudyConfig
    from ehr_simulator.db import AccessMode

#: Every command logs here (structlog JSONL, ``logging.setup_logging``).
LOG_DIR = Path("logs")

#: UTC stamp of default export file and directory names (``20260924T101500Z``).
EXPORT_STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

DB_PATH_HELP = "SQLite DB; defaults to the study's db_path / data/study_<study_id>.db."

#: Shared by both exports: same secret file, same pseudonyms.
PSEUDONYM_SECRET_HELP = (
    "32-byte mode-0600 secret keying the exported clinician pseudonyms "
    "(HMAC-SHA256); created when absent. Keep it out of every bundle."
)

app_typer: typer.Typer = typer.Typer(
    name="ehr-simulator",
    no_args_is_help=True,
    add_completion=False,
)


def main(argv: list[str] | None = None) -> None:
    """Console entry point (S10 §8: command failure must reach the OS).

    With ``standalone_mode=True`` typer/click translate ``Exit`` into
    ``SystemExit(1)`` (refusal) or ``SystemExit(2)`` (usage); success
    paths return ``None`` so direct ``cli.main([...])`` test ergonomics
    survive. A refusal surfaces as ``SystemExit`` with the exact code,
    which the installed console script propagates as the process status.
    """
    try:
        app_typer(args=argv, standalone_mode=True)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        if code == 0:
            return None
        raise SystemExit(code) from exc


class MissingDb(StrEnum):
    """Refusal wording for an absent database (each command keeps its own)."""

    DB_PATH = "db_path does not exist"
    DATABASE = "database not found"


def fail(exc: Exception) -> NoReturn:
    """Print ``Error: <exc>`` to stderr and exit 1."""
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=1) from exc


def load_study(study_path: Path) -> StudyConfig:
    """Load the study YAML; a ``ConfigError`` is exit 1."""
    from ehr_simulator.config import ConfigError, load_study_config

    try:
        return load_study_config(study_path)
    except ConfigError as exc:
        fail(exc)


def existing_db_path(
    study: StudyConfig, db_path: Path | None, missing: MissingDb = MissingDb.DB_PATH
) -> Path:
    """Resolve ``--db-path`` → ``EHR_SIM_DB_PATH`` → study YAML → default; must exist.

    A traversal-guard ``ConfigError`` propagates to the caller's handler.
    """
    from ehr_simulator.db import resolve_db_path

    target_db = resolve_db_path(study, cli_override=db_path)
    if not target_db.exists():
        typer.echo(f"Error: {missing}: {target_db}", err=True)
        raise typer.Exit(code=1)

    return target_db


@contextmanager
def study_db(
    study: StudyConfig, target_db: Path, access: AccessMode
) -> Iterator[sqlite3.Connection]:
    """Open ``target_db`` past the schema + study identity gates; always closed.

    A gate refusal is exit 1 before the body runs. Connection errors and
    the body's own exceptions propagate to the caller.
    """
    from ehr_simulator.cli_support import OperatorError, assert_schema_current
    from ehr_simulator.db import connect
    from ehr_simulator.db.exceptions import StudyIdentityError
    from ehr_simulator.db.study_identity import require as require_study_identity

    conn = connect(target_db, access=access)
    try:
        assert_schema_current(conn)
        require_study_identity(conn, study.study_id)
    except (OperatorError, StudyIdentityError) as exc:
        conn.close()
        fail(exc)

    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _open_study_db(
    study_path: Path, db_path: Path | None, access: AccessMode
) -> Iterator[tuple[StudyConfig, sqlite3.Connection]]:
    """Load the study, resolve its existing DB and open it through :func:`study_db`."""
    study = load_study(study_path)
    target_db = existing_db_path(study, db_path)
    with study_db(study, target_db, access) as conn:
        yield study, conn
