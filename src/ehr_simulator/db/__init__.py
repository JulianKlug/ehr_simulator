"""SQLite persistence package.

Pure-function DAOs over a single :class:`sqlite3.Connection` opened at app
boot. The connection is passed in by callers; no module-level singleton.

Public surface (built up across S6 commits):

- :func:`connect`, :func:`resolve_db_path` — connection management.
- :func:`apply_migrations`, :data:`MIGRATIONS`, :class:`Migration` — schema.
- :class:`DbError` — caller-handled integrity errors at DAO boundaries.
- :class:`StudyIdentityError` — S11a database/study identity mismatch.
- :mod:`study_identity` — S11a one-database-is-one-study binding.
- Per-table DAO modules: ``clinicians``, ``sessions``, ``arm_assignments``,
  ``answers``, ``events``, ``ingestion_issues``, ``progress`` (S9b),
  ``randomisation`` (S11c planned schedules).
- ``backup`` (commit 3) + ``cookies`` (commit 5a) re-exported once they land.
"""

from __future__ import annotations

from ehr_simulator.db import (
    answers,
    arm_assignments,
    backup,
    clinicians,
    config_history,
    cookies,
    events,
    ingestion_issues,
    progress,
    randomisation,
    sessions,
    study_identity,
)
from ehr_simulator.db.connection import AccessMode, connect, resolve_db_path
from ehr_simulator.db.exceptions import (
    ConfigurationActivationError,
    ConfigurationError,
    ConfigurationProvenanceError,
    DbError,
    RandomisationError,
    RandomisationIntegrityError,
    StaleConfigurationError,
    StudyIdentityError,
)
from ehr_simulator.db.migrations import MIGRATIONS, Migration, apply_migrations

__all__ = [
    "MIGRATIONS",
    "AccessMode",
    "ConfigurationActivationError",
    "ConfigurationError",
    "ConfigurationProvenanceError",
    "DbError",
    "Migration",
    "RandomisationError",
    "RandomisationIntegrityError",
    "StaleConfigurationError",
    "StudyIdentityError",
    "answers",
    "apply_migrations",
    "arm_assignments",
    "backup",
    "clinicians",
    "config_history",
    "connect",
    "cookies",
    "events",
    "ingestion_issues",
    "progress",
    "randomisation",
    "resolve_db_path",
    "sessions",
    "study_identity",
]
