"""S11a: one SQLite database holds exactly one study.

The ``study_identity`` table (migration 4) is a singleton — at most one
row, keyed by the fixed constant ``singleton = 1``:

.. sourcecode:: sql

    CREATE TABLE IF NOT EXISTS study_identity (
        singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
        study_id    TEXT NOT NULL UNIQUE,
        created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

Public API (all pure over a caller-passed, caller-controlled connection):

- :func:`fetch` — return the stored study_id, or ``None`` when the table
  is empty.
- :func:`bind` — establish or verify identity. Already matches → no-op;
  holds a different identity → :class:`StudyIdentityError`; unbound but
  holding persistent application data → :class:`StudyIdentityError`
  (refused silent adoption of a legacy database); unbound and empty →
  insert the identity row.
- :func:`require` — pure validation, never writes (safe on a read-only
  connection). Matches → OK; missing or different identity →
  :class:`StudyIdentityError`.
- :func:`adopt` — (S11b) the operator's explicit escape hatch for the
  case :func:`bind` refuses: claiming an unbound database that already
  persists clinician data. Only reachable through the CLI
  ``--adopt-study-id`` flag; it is never called implicitly.

Error messages carry at most the study ids involved — never clinical,
event, or clinician rows.
"""

from __future__ import annotations

import re
import sqlite3

from ehr_simulator.db.exceptions import StudyIdentityError

__all__ = [
    "STUDY_ID_PATTERN",
    "adopt",
    "bind",
    "fetch",
    "has_persistent_data",
    "require",
    "validate_study_id",
]

# Duplicate of :data:`ehr_simulator.config.study.STUDY_ID_PATTERN` — this layer
# must not import from ``config``. tests/test_db.py pins the two in lockstep.
STUDY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Tables excluded from the "is this database non-empty" check (schema
#: bookkeeping and the identity table itself).
_IGNORED_TABLES = ("schema_migrations", "study_identity")

#: The clinician-data tables :func:`adopt` probes. ``configuration_history``
#: and ``active_configuration`` are deliberately NOT here: a config
#: activation is bookkeeping, not case data.
_CASE_TABLES = ("clinicians", "sessions", "arm_assignments", "progress", "answers")


def validate_study_id(study_id: object) -> str:
    """Return ``study_id`` if it matches :data:`STUDY_ID_PATTERN`; raise otherwise.

    Raises:
        StudyIdentityError: the value is not a string matching the rule.
    """
    if not isinstance(study_id, str) or not STUDY_ID_PATTERN.fullmatch(study_id):
        raise StudyIdentityError(
            f"study_id must match {STUDY_ID_PATTERN.pattern} "
            "(a-z, 0-9, '-', '_'; must start alphanumeric; max 64 chars); "
            f"got {study_id!r}"
        )
    return study_id


def fetch(conn: sqlite3.Connection) -> str | None:
    """Return the database's stored study_id, or ``None`` when unbound.

    Raises:
        StudyIdentityError: the ``study_identity`` table does not exist
            (migration 4 not applied).
    """
    try:
        row = conn.execute("SELECT study_id FROM study_identity WHERE singleton = 1").fetchone()
    except sqlite3.OperationalError as exc:
        raise StudyIdentityError(
            "database has no study_identity table; apply migration 4 before any study operation"
        ) from exc
    return row[0] if row is not None else None


def has_persistent_data(conn: sqlite3.Connection) -> bool:
    """True when any persistent application table holds at least one row."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "AND name NOT IN ('schema_migrations', 'study_identity')"
    ).fetchall()
    for (name,) in rows:
        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()  # noqa: S608
        if n is not None and n[0] > 0:
            return True
    return False


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _case_data_exists(conn: sqlite3.Connection) -> bool:
    """True when any clinician-data table carries at least one row.

    Only tables that exist count (pre-S11b databases lack some of them,
    and a table whose columns predate S11b is still probed the same way).
    """
    for name in _CASE_TABLES:
        if not _table_exists(conn, name):
            continue
        if conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() is not None:  # noqa: S608
            return True
    return False


def bind(conn: sqlite3.Connection, study_id: object) -> None:
    """Bind ``conn``'s database to ``study_id``, or verify the existing binding.

    - identity already equals ``study_id`` → no change;
    - identity holds a different value → :class:`StudyIdentityError`;
    - unbound but any persistent application table has rows →
      :class:`StudyIdentityError` (no silent adoption of legacy data;
      S11b's explicit :func:`adopt` is the only escape hatch);
    - unbound and empty → insert the singleton row and commit.

    Raises:
        StudyIdentityError: malformed id, mismatch, or non-empty unbound DB.
    """
    bind_in_transaction(conn, study_id)
    conn.commit()


def bind_in_transaction(conn: sqlite3.Connection, study_id: object) -> None:
    """:func:`bind` without the commit: the insert joins the caller's open
    transaction, e.g. S11b activation commits identity + history + active
    pointer together, or rolls all three back.

    Raises:
        StudyIdentityError: malformed id, mismatch, or non-empty unbound DB.
    """
    valid = validate_study_id(study_id)
    existing = fetch(conn)
    if existing == valid:
        return
    if existing is not None:
        raise StudyIdentityError(
            f"database is already bound to study {existing!r}; refusing to "
            f"re-bind it to {valid!r} (one database holds exactly one study)"
        )
    if has_persistent_data(conn):
        raise StudyIdentityError(
            f"database contains application data but has no study identity; "
            f"refusing to claim it for study {valid!r}. S11a does not adopt "
            "non-empty legacy databases: use a fresh database for this study, "
            "or explicitly adopt it (S11b, CLI --adopt-study-id)"
        )
    conn.execute("INSERT INTO study_identity (singleton, study_id) VALUES (1, ?)", (valid,))


def adopt(conn: sqlite3.Connection, study_id: object) -> None:
    """Explicitly claim an unbound database for ``study_id`` (S11b).

    The operator's deliberate answer to :func:`bind`'s refusal of a
    non-empty legacy database — e.g., an S11a study database (already
    walked patients) being upgraded to S11b. Deliberately reachable only
    through the CLI ``--adopt-study-id`` flag, never implicitly.

    Rules:

    * an explicit, well-formed ``study_id`` is required;
    * the database must persist clinician data (``clinicians``,
      ``sessions``, ``arm_assignments``, ``progress`` or ``answers``) —
      an empty database should be :func:`bind`-ed, not adopted;
    * the database must have no identity yet (re-binding to a different
      study is still refused);
    * the caller is responsible for applying migrations first.

    Raises:
        StudyIdentityError: malformed id, missing case data, or an
            existing identity that does not match.
    """
    valid = validate_study_id(study_id)
    existing = fetch(conn)
    if existing == valid:
        return
    if existing is not None:
        raise StudyIdentityError(
            f"database is already bound to study {existing!r}; refusing to "
            f"re-bind it to {valid!r} (one database holds exactly one study)"
        )
    if not _case_data_exists(conn):
        raise StudyIdentityError(
            f"adopt: the database carries no clinician data (none of "
            f"{', '.join(_CASE_TABLES)} has rows); there is nothing to adopt — "
            "bind an empty database instead"
        )
    conn.execute("INSERT INTO study_identity (singleton, study_id) VALUES (1, ?)", (valid,))
    conn.commit()


def require(conn: sqlite3.Connection, study_id: object) -> None:
    """Pure read-only gate: the database must be bound to exactly ``study_id``.

    Never writes. Matches → OK. Missing identity or a different one →
    :class:`StudyIdentityError`.

    Raises:
        StudyIdentityError: malformed id, unbound database, or mismatch.
    """
    valid = validate_study_id(study_id)
    existing = fetch(conn)
    if existing is None:
        raise StudyIdentityError(
            f"database has no study identity; refusing to use it for study "
            f"{valid!r} (refuses unbound databases)"
        )
    if existing != valid:
        raise StudyIdentityError(
            f"database is bound to study {existing!r}, not {valid!r}; "
            "refusing to mix studies in one database"
        )
