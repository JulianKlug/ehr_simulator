"""answers DAO: upsert clinician responses keyed by the analysis cell.

The unique constraint ``ux_answers_cell (clinician_id, patient_id,
timepoint, question_id)`` is the load-bearing invariant: a network-retry
double-submit from the S9a auto-save path must NOT produce two rows.
``upsert`` is the only write API.

S6 ships the upsert + the regression test (test #12). No S6 route writes
to ``answers`` yet; S9a wires the POST endpoint.
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
