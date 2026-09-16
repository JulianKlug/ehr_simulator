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
from typing import Any


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
    write_counter: dict[str, int] | None = None,
    app_state: Any = None,
) -> None:
    """Insert-or-update one ``answers`` row.

    On conflict on ``ux_answers_cell``, ``value``/``arm``/``config_hash`` are
    overwritten and ``ts_recorded`` is bumped to ``CURRENT_TIMESTAMP``.
    Increments ``app_state.write_counter`` after a successful execute so the
    shutdown-time backup gate (review-fix R8) trips on real research writes.
    """
    conn.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(clinician_id, patient_id, timepoint, question_id) "
        "DO UPDATE SET value=excluded.value, arm=excluded.arm, "
        "config_hash=excluded.config_hash, ts_recorded=CURRENT_TIMESTAMP",
        (clinician_id, patient_id, timepoint, question_id, value, arm, config_hash),
    )
    conn.commit()
    if app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1


def fetch_for_cell(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
) -> dict[str, tuple[str, str]]:
    """Return ``{question_id: (value, config_hash)}`` for one cell.

    Served by the implicit index behind ``ux_answers_cell`` — its
    ``(clinician_id, patient_id, timepoint)`` prefix matches the WHERE.
    ``config_hash`` rides along so the caller can detect rows recorded under
    a different study config (S9a §8.5).
    """
    rows = conn.execute(
        "SELECT question_id, value, config_hash FROM answers "
        "WHERE clinician_id = ? AND patient_id = ? AND timepoint = ?",
        (clinician_id, patient_id, timepoint),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def delete_one(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
    question_id: str,
    app_state: Any = None,
) -> int:
    """Delete one cell; return the rowcount (0 or 1).

    Bumps ``write_counter`` only when a row was actually removed.
    """
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
