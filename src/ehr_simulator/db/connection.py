"""SQLite connection management + db_path resolution.

``connect`` opens a single :class:`sqlite3.Connection` per app and applies
the three boot PRAGMAs (WAL journal, NORMAL synchronous, FK enforcement).
``check_same_thread=False`` is required because FastAPI dispatches handlers
on a threadpool; SQLite's serialized threading mode (build default since
3.5) makes the shared connection safe for the pilot's ≤1 concurrent
clinician.

``resolve_db_path`` is the four-tier precedence chain CLI overrides → env
var → study YAML → default. Non-CLI sources pass through
``_db_path_traversal_guard``: ``..`` segments and absolute paths outside
the CWD subtree are rejected as ``ConfigError`` (per review-fix R13). The
CLI flag bypasses the guard because it's an explicit operator decision.
"""

from __future__ import annotations

import os
import sqlite3
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from ehr_simulator.config.exceptions import ConfigError

if TYPE_CHECKING:
    from ehr_simulator.config.study import StudyConfig


class AccessMode(StrEnum):
    """Access mode for :func:`connect`.

    ``READ_ONLY`` is the S9c export connection: the file is opened via
    SQLite's ``mode=ro`` URI (it is never created), the boot journal/synchronous
    PRAGMAs are **not** touched, and ``PRAGMA query_only=ON`` fences any
    write. The file must already exist — a mistyped ``--db-path`` fails
    fast instead of creating a database.
    """

    READ_WRITE = "read-write"
    READ_ONLY = "read-only"


_DEFAULT_DB_PATH = Path("data/ehr_simulator.db")


def connect(
    db_path: Path,
    *,
    apply_pragmas: bool = True,
    access: AccessMode = AccessMode.READ_WRITE,
) -> sqlite3.Connection:
    """Open a connection, set the row factory, apply boot PRAGMAs."""
    if access is AccessMode.READ_ONLY:
        db_path = Path(db_path)
        if not db_path.exists():
            raise FileNotFoundError(f"read-only connect: database file does not exist: {db_path}")
        uri = f"file:{quote(db_path.as_posix(), safe='/')}?mode=ro"
        conn = sqlite3.connect(
            uri,
            uri=True,
            detect_types=sqlite3.PARSE_DECLTYPES,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        if apply_pragmas:
            conn.execute("PRAGMA query_only = ON")
        return conn
    conn = sqlite3.connect(
        db_path,
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    if apply_pragmas:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
    return conn


def resolve_db_path(
    study: StudyConfig | None,
    *,
    cli_override: Path | None = None,
) -> Path:
    """Resolve the DB path from CLI → env var → study YAML → default."""
    if cli_override is not None:
        return cli_override
    env_override = os.environ.get("EHR_SIM_DB_PATH")
    if env_override:
        return _db_path_traversal_guard(Path(env_override))
    if study is not None and getattr(study, "db_path", None) is not None:
        return study.db_path
    return _DEFAULT_DB_PATH


def _db_path_traversal_guard(p: Path) -> Path:
    """Reject ``..`` segments and absolute paths outside the CWD subtree.

    Applied to env-var and study-YAML inputs; the CLI flag bypasses it. The
    guard runs on the *original* path so a YAML-resolved absolute path
    (already canonicalized against the YAML dir) still gets a literal
    ``..`` check on its parts.
    """
    if ".." in p.parts:
        raise ConfigError(f"db_path must not contain '..' segments; got {p}")
    resolved = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    cwd = Path.cwd().resolve()
    try:
        resolved.relative_to(cwd)
    except ValueError as exc:
        raise ConfigError(
            f"db_path must be inside the project working directory; got {resolved}"
        ) from exc
    return resolved
