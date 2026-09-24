"""S11b: configuration version history + the active-configuration pointer.

``configuration_history`` is the append-only register of explicit
activations — one row per ``config_version`` carrying the immutable study
and question snapshots (:mod:`ehr_simulator.config.snapshot`).
``active_configuration`` is the two-column singleton naming the one
currently-active version. **This module is the only writer to both
tables; every other read goes through the public API here.**

Case provenance anchors (``arm_assignments``, ``sessions``, ``progress``,
``answers``) carry a nullable ``config_version``; once history exists a
missing one is an integrity error the service layer refuses — never
guessed (spec §S11a database upgrade).

Atomicity: :func:`activate` performs all of its writes (optional S11a
backfill, the history INSERT, the active pointer) and commits exactly
once; a ``sqlite3.Error`` mid-way rolls the whole activation back and
nothing partial becomes visible.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ehr_simulator.config.exceptions import ConfigValidationError
from ehr_simulator.config.questions import Questions
from ehr_simulator.config.snapshot import (
    parse_study_snapshot,
    render_questions_snapshot,
    render_study_snapshot,
    validate_config_version,
    validate_description,
    validate_reason,
)
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db.exceptions import (
    ConfigurationActivationError,
    ConfigurationProvenanceError,
)

__all__ = [
    "ConfigHistoryRow",
    "activate",
    "fetch_active",
    "fetch_version",
    "has_any",
    "list_all",
    "require_known",
]

#: The four case-provenance tables (S11a upgrade backfill target, spec
#: §S11a database upgrade). Module constant — never interpolated operator
#: input, so the f-strings are safe.
_PROVENANCE_TABLES = ("arm_assignments", "sessions", "progress", "answers")

_SELECT_COLUMNS = (
    "config_version, study_id, config_hash, activated_at, "
    "change_description, change_reason, study_json, questions_json"
)


@dataclass(frozen=True)
class ConfigHistoryRow:
    """One fully materialized ``configuration_history`` row.

    ``study_json`` / ``questions_json`` are the stored canonical snapshots;
    re-validate them with :mod:`ehr_simulator.config.snapshot` before use.
    """

    config_version: str
    study_id: str
    config_hash: str
    activated_at: object  # datetime under PARSE_DECLTYPES; str on a bare connection
    change_description: str
    change_reason: str | None
    study_json: str
    questions_json: str


def _to_row(row: sqlite3.Row | None) -> ConfigHistoryRow | None:
    if row is None:
        return None
    return ConfigHistoryRow(
        config_version=row[0],
        study_id=row[1],
        config_hash=row[2],
        activated_at=row[3],
        change_description=row[4],
        change_reason=row[5],
        study_json=row[6],
        questions_json=row[7],
    )


def has_any(conn: sqlite3.Connection) -> bool:
    """True once at least one activation is registered.

    The write/read-side provenance guards use this: while history is empty
    (fresh database or plain S11a upgrade), NULL ``config_version`` rows on
    the provenance tables are legal; the instant history exists, a row
    missing its version is an integrity error.
    """
    n = conn.execute("SELECT COUNT(*) FROM configuration_history").fetchone()[0]
    return n > 0


def fetch_active(conn: sqlite3.Connection) -> ConfigHistoryRow | None:
    """The currently-active activation (join through the singleton)."""
    row = conn.execute(
        f"SELECT h.{_SELECT_COLUMNS} "
        "FROM active_configuration a "
        "JOIN configuration_history h ON h.config_version = a.config_version "
        "WHERE a.singleton = 1"
    ).fetchone()
    return _to_row(row)


def fetch_version(conn: sqlite3.Connection, config_version: str) -> ConfigHistoryRow | None:
    """Return the activation registered under ``config_version``, or ``None``."""
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM configuration_history WHERE config_version = ?",
        (config_version,),
    ).fetchone()
    return _to_row(row)


def list_all(conn: sqlite3.Connection) -> tuple[ConfigHistoryRow, ...]:
    """Every activation, in activation order (ties broken by label)."""
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM configuration_history ORDER BY activated_at, config_version"
    ).fetchall()
    return tuple(_to_row(row) for row in rows)


def require_known(
    conn: sqlite3.Connection, config_version: str, config_hash: str
) -> ConfigHistoryRow:
    """Return the activation for a (version, hash) pair the case pins to.

    Raises:
        ConfigurationProvenanceError: unknown ``config_version``, or a
            known version registered under a different ``config_hash``
            (serving a case under the wrong configuration is an integrity
            failure, never a fallback).
    """
    row = fetch_version(conn, config_version)
    if row is None:
        raise ConfigurationProvenanceError(
            f"unknown config_version {config_version!r}: no such activation in this database"
        )
    if row.config_hash != config_hash:
        raise ConfigurationProvenanceError(
            f"config_version {config_version!r} was registered under config_hash "
            f"{row.config_hash[:12]}…, not the case's {config_hash[:12]}…; "
            "refusing to serve the case (provenance mismatch)"
        )
    return row


# ---------------------------------------------------------------------------
# Activation — the only write path for both tables
# ---------------------------------------------------------------------------


def _set_active(conn: sqlite3.Connection, config_version: str) -> None:
    """Point the singleton at ``config_version`` (insert or move the pointer)."""
    conn.execute(
        "INSERT INTO active_configuration (singleton, config_version) VALUES (1, ?) "
        "ON CONFLICT(singleton) DO UPDATE SET config_version = excluded.config_version",
        (config_version,),
    )


def _check_existing_history(
    conn: sqlite3.Connection,
    study: StudyConfig,
    dataset_check_version: str,
) -> sqlite3.Row | None:
    """Validate an activation against the history already registered.

    Enforces the dataset invariant (``study.dataset`` may not change within
    one study), refuses a history belonging to another study id, and
    re-parses every stored study snapshot — an unparseable snapshot is a
    stored-state integrity failure and no new activation may ride on top of
    it. Returns the row already registered under ``dataset_check_version``
    (the caller handles the no-op / collision decision).
    """
    prior = None
    for row in conn.execute(f"SELECT {_SELECT_COLUMNS} FROM configuration_history").fetchall():
        if row[0] == dataset_check_version:
            prior = row
        if row[1] != study.study_id:
            raise ConfigurationActivationError(
                f"configuration history belongs to study {row[1]!r}, not {study.study_id!r}; "
                "one database holds exactly one study"
            )
        try:
            stored = parse_study_snapshot(row[6])
        except Exception as exc:  # pydantic.ValidationError and ConfigValidationError
            raise ConfigurationActivationError(
                f"stored snapshot for {row[0]!r} no longer parses ({exc!r}); "
                "refusing to activate on top of a corrupted history"
            ) from exc
        if stored.dataset != study.dataset:
            raise ConfigurationActivationError(
                f"study.dataset may not change within one study: history {row[0]!r} "
                f"was activated on {stored.dataset!r}, the new activation uses {study.dataset!r}"
            )
    return prior


def _s11a_backfill(conn: sqlite3.Connection, config_version: str, config_hash: str) -> int:
    """S11a database upgrade: backfill ``config_version`` when unambiguous.

    Runs only when ``configuration_history`` is empty (the caller
    guarantees this). Collects the distinct non-NULL ``config_hash`` values
    across the four provenance tables:

    - none → nothing to backfill;
    - exactly one and it equals the new activation's hash → set
      ``config_version`` on every provenance row still missing one;
    - anything else → :class:`ConfigurationActivationError`.

    Existing ``config_hash`` values are never rewritten. Returns the number
    of rows backfilled.
    """
    hashes: set[str] = set()
    for table in _PROVENANCE_TABLES:
        rows = conn.execute(
            f"SELECT DISTINCT config_hash FROM {table} WHERE config_hash IS NOT NULL"
        ).fetchall()
        hashes.update(h[0] for h in rows)

    if len(hashes) > 1:
        raise ConfigurationActivationError(
            f"the database carries {len(hashes)} distinct historical config_hash values; "
            "the version mapping is ambiguous, so the first activation cannot backfill "
            "config_version (refusing; use a fresh database or resolve the history manually)"
        )
    if not hashes:
        return 0
    if hashes != {config_hash}:
        raise ConfigurationActivationError(
            f"the database carries one historical config_hash ({next(iter(hashes))[:12]}…) "
            f"that does not match the activation ({config_hash[:12]}…); "
            "refusing to backfill config_version onto rows recorded under a "
            "different configuration"
        )

    backfilled = 0
    for table in _PROVENANCE_TABLES:
        cursor = conn.execute(
            f"UPDATE {table} SET config_version = ? "
            f"WHERE config_version IS NULL AND config_hash = ?",
            (config_version, config_hash),
        )
        backfilled += cursor.rowcount
    return backfilled


def activate(
    conn: sqlite3.Connection,
    *,
    study_id: str,
    config_version: str,
    config_hash: str,
    description: str,
    reason: str | None,
    study: StudyConfig,
    questions: Questions,
) -> ConfigHistoryRow:
    """Register an activation and make it active — or refuse without writing.

    Decision table (spec §Explicit configuration activation):

    ===========================  =======================================
    existing state               result
    ===========================  =======================================
    new version                  register and activate
    same version, same hash AND  no-op (active pointer (re)landed on the
    metadata exact               existing row and returned)
    same version, different hash or metadata → refuse
    new version, reused hash     allowed (a rollback must use a new version)
    ===========================  =======================================

    Raises:
        ConfigurationActivationError: invalid activation metadata, a
            colliding/reused version label, a dataset change within one
            study, an unparseable stored snapshot, or an ambiguous S11a
            provenance backfill. Nothing is written when this is raised.
    """
    if study_id != study.study_id:
        raise ConfigurationActivationError(
            f"study_id {study_id!r} does not match the loaded study config "
            f"({study.study_id!r}); refusing to register the activation under the wrong study"
        )
    if not isinstance(config_hash, str) or not config_hash:
        raise ConfigurationActivationError(
            f"config_hash must be a non-empty string, got {config_hash!r}"
        )
    try:
        version = validate_config_version(config_version)
        clean_description = validate_description(description)
        clean_reason = validate_reason(reason)
    except ConfigValidationError as exc:
        raise ConfigurationActivationError(str(exc)) from exc

    try:
        existing = conn.execute(f"SELECT {_SELECT_COLUMNS} FROM configuration_history").fetchall()

        if existing:
            prior = _check_existing_history(conn, study, version)
            if prior is not None:
                same_metadata = (
                    prior[2] == config_hash
                    and prior[4] == clean_description
                    and prior[5] == clean_reason
                )
                if not same_metadata:
                    raise ConfigurationActivationError(
                        f"config_version {version!r} is already registered with a different "
                        "config_hash or metadata; refusing to reuse the label "
                        "(a rollback must use a new config_version)"
                    )
                _set_active(conn, version)
                conn.commit()
                return _to_row(prior)  # type: ignore[return-value]
        else:
            _s11a_backfill(conn, version, config_hash)

        # The history row must exist before the active pointer references it
        # (``active_configuration.config_version`` is a foreign key).
        conn.execute(
            "INSERT INTO configuration_history "
            "(config_version, study_id, config_hash, change_description, change_reason, "
            "study_json, questions_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                study_id,
                config_hash,
                clean_description,
                clean_reason,
                render_study_snapshot(study),
                render_questions_snapshot(questions),
            ),
        )
        _set_active(conn, version)
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        raise

    return fetch_version(conn, version)  # type: ignore[return-value]
