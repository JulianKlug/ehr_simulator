"""SQLite persistence package.

Pure-function DAOs over a single :class:`sqlite3.Connection` opened at app
boot. The connection is passed in by callers; no module-level singleton.

Public surface (built up across S6 commits):

- :func:`connect`, :func:`resolve_db_path` — connection management.
- :func:`apply_migrations`, :data:`MIGRATIONS`, :class:`Migration` — schema.
- :class:`DbError` — caller-handled integrity errors at DAO boundaries.
- Per-table DAO modules: ``clinicians``, ``sessions``, ``arm_assignments``,
  ``answers``, ``events``, ``ingestion_issues``, ``progress`` (S9b).
- ``backup`` (commit 3) + ``cookies`` (commit 5a) re-exported once they land.
"""

from __future__ import annotations

from ehr_simulator.db import (
    answers,
    arm_assignments,
    backup,
    clinicians,
    cookies,
    events,
    ingestion_issues,
    progress,
    sessions,
)
from ehr_simulator.db.connection import connect, resolve_db_path
from ehr_simulator.db.exceptions import DbError
from ehr_simulator.db.migrations import MIGRATIONS, Migration, apply_migrations

__all__ = [
    "MIGRATIONS",
    "DbError",
    "Migration",
    "answers",
    "apply_migrations",
    "arm_assignments",
    "backup",
    "clinicians",
    "connect",
    "cookies",
    "events",
    "ingestion_issues",
    "progress",
    "resolve_db_path",
    "sessions",
]
