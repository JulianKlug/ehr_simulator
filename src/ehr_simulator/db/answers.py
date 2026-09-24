"""answers DAO: upsert clinician responses keyed by the analysis cell.

The unique constraint ``ux_answers_cell (clinician_id, patient_id,
timepoint, question_id)`` is the load-bearing invariant: a network-retry
double-submit from the S9a auto-save path must NOT produce two rows.
``upsert`` is the only write API.

S9a adds the two siblings the answer-capture service needs:
``fetch_for_cell`` (pre-fill of one ``(clinician, patient, timepoint)``
cell) and ``delete_one`` (an empty submission clears the cell — no row is
how S9b reads "unanswered").
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ehr_simulator.db.config_history import has_any
from ehr_simulator.db.exceptions import ConfigurationProvenanceError


def _require_version_provenance(conn: sqlite3.Connection, config_version: str | None) -> None:
    """Write-side provenance guard (S11b): once the study has any activated
    configuration, every new case row must carry its ``config_version``.

    Raises:
        ConfigurationProvenanceError: ``config_version`` is NULL but
            ``configuration_history`` is non-empty.
    """
    if config_version is None and has_any(conn):
        raise ConfigurationProvenanceError(
            "refusing to write a case row without a config_version: the study "
            "has activated configurations in its history; every new case row "
            "must pin the version under which it was started"
        )


def _require_cell_provenance(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
    question_id: str,
    config_hash: str,
    config_version: str | None,
) -> None:
    """Refuse to touch a stored answer pinned to another configuration (S11b).

    Runs before any UPDATE or DELETE: e.g. a v1/h1 answer must not be
    overwritten or cleared by a v2/h2 write. No stored row → nothing to check.

    Raises:
        ConfigurationProvenanceError: stored (version, hash) differ from
            the write's.
    """
    row = conn.execute(
        "SELECT config_hash, config_version FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ? AND question_id = ?",
        (clinician_id, patient_id, timepoint, question_id),
    ).fetchone()
    if row is None:
        return

    if row[0] != config_hash or row[1] != config_version:
        raise ConfigurationProvenanceError(
            "refusing to modify an answer recorded under a different configuration "
            f"provenance (clinician={clinician_id}, patient={patient_id}, "
            f"timepoint={timepoint}, question_id={question_id}, "
            f"stored version={row[1]!r}, write version={config_version!r})"
        )


@dataclass(frozen=True)
class AnswerRow:
    """One fully materialized ``answers`` row (S9c export reads all of them)."""

    clinician_id: str
    patient_id: str
    timepoint: float
    question_id: str
    value: str
    arm: str
    config_hash: str
    config_version: str | None = None


def upsert(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
    question_id: str,
    value: str,
    arm: str,
    config_hash: str,
    config_version: str | None = None,
    write_counter: dict[str, int] | None = None,
    app_state: Any = None,
) -> None:
    """Insert-or-update one ``answers`` row.

    On conflict on ``ux_answers_cell``, ``value``/``arm`` are overwritten and
    ``ts_recorded`` is bumped to ``CURRENT_TIMESTAMP``. The provenance pair
    (``config_version``, ``config_hash``) is written on INSERT only and is
    never rewritten on conflict — a case row is pinned to the configuration
    under which it started (S11b).
    Increments ``app_state.write_counter`` after a successful execute so the
    shutdown-time backup gate (review-fix R8) trips on real research writes.

    Raises:
        ConfigurationProvenanceError (S11b): the study has activated
            configurations but ``config_version`` is omitted, or the stored
            row carries a different (version, hash) — nothing is written.
    """
    _require_version_provenance(conn, config_version)
    _require_cell_provenance(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=timepoint,
        question_id=question_id,
        config_hash=config_hash,
        config_version=config_version,
    )
    conn.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, "
        "config_hash, config_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(clinician_id, patient_id, timepoint, question_id) "
        "DO UPDATE SET value=excluded.value, arm=excluded.arm, "
        "ts_recorded=CURRENT_TIMESTAMP",
        (clinician_id, patient_id, timepoint, question_id, value, arm, config_hash, config_version),
    )
    conn.commit()
    if app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1


def fetch_all(conn: sqlite3.Connection) -> tuple[AnswerRow, ...]:
    """Every ``answers`` row, in a stable order.

    S9c read path: the export takes its own snapshot transaction around this;
    the ordering (analysis cell, then question id) is the one the pipeline
    expects for grouping and is what makes re-runs byte-stable.
    """
    rows = conn.execute(
        "SELECT clinician_id, patient_id, timepoint, question_id, value, "
        "arm, config_hash, config_version "
        "FROM answers "
        "ORDER BY clinician_id, patient_id, timepoint, question_id"
    ).fetchall()
    return tuple(
        AnswerRow(
            clinician_id=row[0],
            patient_id=row[1],
            timepoint=float(row[2]),
            question_id=row[3],
            value=row[4],
            arm=row[5],
            config_hash=row[6],
            config_version=row[7],
        )
        for row in rows
    )


def fetch_for_cell(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
) -> dict[str, tuple[str, str | None, str | None]]:
    """Return ``{question_id: (value, config_hash, config_version)}`` for one cell.

    Served by the implicit index behind ``ux_answers_cell`` — its
    ``(clinician_id, patient_id, timepoint)`` prefix matches the WHERE.
    ``config_hash`` / ``config_version`` ride along so the caller can detect
    rows recorded under a different study configuration (S9a §8.5, S11b §Provenance).
    """
    rows = conn.execute(
        "SELECT question_id, value, config_hash, config_version FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ?",
        (clinician_id, patient_id, timepoint),
    ).fetchall()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def delete_one(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
    question_id: str,
    config_hash: str,
    config_version: str | None,
    app_state: Any = None,
) -> int:
    """Delete one cell; return the rowcount (0 or 1).

    Bumps ``write_counter`` only when a row was actually removed.

    Raises:
        ConfigurationProvenanceError (S11b): the stored row carries a
            different (version, hash) — nothing is deleted.
    """
    _require_cell_provenance(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=timepoint,
        question_id=question_id,
        config_hash=config_hash,
        config_version=config_version,
    )
    cursor = conn.execute(
        "DELETE FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ? AND question_id = ?",
        (clinician_id, patient_id, timepoint, question_id),
    )
    conn.commit()
    deleted = cursor.rowcount
    if deleted > 0 and app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return deleted


def delete_after(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    min_timepoint_exclusive: float,
    app_state: Any = None,
) -> int:
    """Delete every answer of the pair strictly after a timepoint; return the rowcount.

    The S9b ``reset-progress`` CLI rewinds a walk to ``t_index = N`` and
    drops what was answered past it; answers *at* N survive and pre-fill the
    re-opened pane.
    """
    cursor = conn.execute(
        "DELETE FROM answers WHERE clinician_id = ? AND patient_id = ? AND timepoint > ?",
        (clinician_id, patient_id, min_timepoint_exclusive),
    )
    conn.commit()
    deleted = cursor.rowcount
    if deleted > 0 and app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return deleted
