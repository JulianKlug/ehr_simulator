"""answers DAO: upsert clinician responses keyed by the analysis cell.

The unique constraint ``ux_answers_cell (clinician_id, patient_id,
timepoint, question_id)`` is the load-bearing invariant: a network-retry
double-submit from the S9a auto-save path must NOT produce two rows.
``upsert`` is the only write API.

S9a adds the two siblings the answer-capture service needs:
``fetch_for_cell`` (pre-fill of one ``(clinician, patient, timepoint)``
cell) and ``delete_one`` (an empty submission clears the cell — no row is
how S9b reads "unanswered").

S11h: ``answer_source`` (``clinician`` | ``rule``) and
``derived_from_question_id`` record provenance; ``commit=False`` lets the
branch update of one submission commit as a single transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ehr_simulator.db._provenance import _require_version_provenance
from ehr_simulator.db.exceptions import ConfigurationProvenanceError
from ehr_simulator.db.observation import ObservationMode

ANSWER_SOURCE_CLINICIAN = "clinician"
ANSWER_SOURCE_RULE = "rule"


def _require_cell_provenance(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
    question_id: str,
    config_hash: str,
    config_version: str | None,
    observation_mode: ObservationMode = ObservationMode.MEASURED,
) -> None:
    """Refuse to touch a stored answer pinned to another configuration (S11b)
    or recorded under another observation mode (S11i).

    Runs before any UPDATE or DELETE: e.g. a v1/h1 answer must not be
    overwritten or cleared by a v2/h2 write. No stored row → nothing to check.

    Raises:
        ConfigurationProvenanceError: stored (version, hash) differ from
            the write's.
    """
    row = conn.execute(
        "SELECT config_hash, config_version, observation_mode FROM answers "
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
    if row[2] != observation_mode:
        raise ConfigurationProvenanceError(
            f"refusing to modify a {row[2]} answer as {observation_mode} "
            f"(clinician={clinician_id}, patient={patient_id}, question_id={question_id})"
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
    answer_source: str = ANSWER_SOURCE_CLINICIAN
    derived_from_question_id: str | None = None
    observation_mode: str = ObservationMode.MEASURED


@dataclass(frozen=True)
class CellAnswer:
    """One stored answer of a cell, with its provenance (S11h)."""

    value: str
    config_hash: str
    config_version: str | None
    answer_source: str
    derived_from_question_id: str | None


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
    answer_source: str = ANSWER_SOURCE_CLINICIAN,
    derived_from_question_id: str | None = None,
    commit: bool = True,
    observation_mode: ObservationMode = ObservationMode.MEASURED,
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
        observation_mode=observation_mode,
    )
    conn.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, "
        "config_hash, config_version, answer_source, derived_from_question_id, "
        "observation_mode) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(clinician_id, patient_id, timepoint, question_id) "
        "DO UPDATE SET value=excluded.value, arm=excluded.arm, "
        "answer_source=excluded.answer_source, "
        "derived_from_question_id=excluded.derived_from_question_id, "
        "ts_recorded=CURRENT_TIMESTAMP",
        (
            clinician_id,
            patient_id,
            timepoint,
            question_id,
            value,
            arm,
            config_hash,
            config_version,
            answer_source,
            derived_from_question_id,
            str(observation_mode),
        ),
    )
    if not commit:
        return  # the caller commits and bumps write_counter once

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
        f"SELECT {_ROW_COLUMNS} FROM answers "
        "ORDER BY clinician_id, patient_id, timepoint, question_id"
    ).fetchall()
    return tuple(_answer_row(row) for row in rows)


#: S11l: the ``AnswerRow`` columns, in field order.
_ROW_COLUMNS = (
    "clinician_id, patient_id, timepoint, question_id, value, arm, config_hash, "
    "config_version, answer_source, derived_from_question_id, observation_mode"
)


def _answer_row(row: tuple) -> AnswerRow:
    return AnswerRow(
        clinician_id=row[0],
        patient_id=row[1],
        timepoint=float(row[2]),
        question_id=row[3],
        value=row[4],
        arm=row[5],
        config_hash=row[6],
        config_version=row[7],
        answer_source=row[8],
        derived_from_question_id=row[9],
        observation_mode=row[10],
    )


def fetch_for_pair(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> tuple[AnswerRow, ...]:
    """S11l: every ``answers`` row of one clinician × patient."""
    rows = conn.execute(
        f"SELECT {_ROW_COLUMNS} FROM answers WHERE clinician_id = ? AND patient_id = ? "
        "ORDER BY timepoint, question_id",
        (clinician_id, patient_id),
    ).fetchall()
    return tuple(_answer_row(row) for row in rows)


def fetch_recorded_at(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> dict[tuple[float, str], object]:
    """S11n: ``{(timepoint, question_id): ts_recorded}`` of one clinician × patient."""
    rows = conn.execute(
        "SELECT timepoint, question_id, ts_recorded FROM answers "
        "WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchall()
    return {(float(r[0]), r[1]): r[2] for r in rows}


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


def fetch_cell_answers(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
) -> dict[str, CellAnswer]:
    """``{question_id: CellAnswer}`` for one cell, provenance included (S11h)."""
    rows = conn.execute(
        "SELECT question_id, value, config_hash, config_version, answer_source, "
        "derived_from_question_id FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ?",
        (clinician_id, patient_id, timepoint),
    ).fetchall()
    return {row[0]: CellAnswer(*row[1:]) for row in rows}


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
    commit: bool = True,
    observation_mode: ObservationMode = ObservationMode.MEASURED,
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
        observation_mode=observation_mode,
    )
    cursor = conn.execute(
        "DELETE FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ? AND question_id = ?",
        (clinician_id, patient_id, timepoint, question_id),
    )
    deleted = cursor.rowcount
    if not commit:
        return deleted  # the caller commits and bumps write_counter once

    conn.commit()
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
    commit: bool = True,
) -> int:
    """Delete every answer of the pair strictly after a timepoint; return the rowcount.

    The S9b ``reset-progress`` CLI rewinds a walk to ``t_index = N`` and
    drops what was answered past it; answers *at* N survive and pre-fill the
    re-opened pane. ``commit=False`` leaves the delete in the caller's
    transaction and skips the write-counter bump.
    """
    cursor = conn.execute(
        "DELETE FROM answers WHERE clinician_id = ? AND patient_id = ? AND timepoint > ?",
        (clinician_id, patient_id, min_timepoint_exclusive),
    )
    if not commit:
        return cursor.rowcount

    conn.commit()
    deleted = cursor.rowcount
    if deleted > 0 and app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return deleted
