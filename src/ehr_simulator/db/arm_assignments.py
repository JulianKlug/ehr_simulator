"""arm_assignments DAO: assign + lock the AI / no-AI arm for a (clinician,
patient) pair.

S6 ships the ``phase1_stub`` assigner — always returns ``("no_ai",
"phase1_stub")``. The row is INSERT-OR-IGNOREd so the first call for a
``(clinician_id, patient_id)`` pair locks the assignment forever. S11 will
swap the body to a deterministic randomized assigner; the call signature
stays.

REGRESSION (test #11): switching to randomized in S11 must NOT rewrite
existing rows.
"""

from __future__ import annotations

import sqlite3


def assign_or_lookup(
    conn: sqlite3.Connection,
    clinician_id: str,
    patient_id: str,
    *,
    config_hash: str,
) -> tuple[str, str]:
    """Return ``(arm, arm_source)`` for the (clinician, patient) pair,
    creating the row if it doesn't exist. Existing rows are never rewritten.
    """
    row = conn.execute(
        "SELECT arm, arm_source FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    arm, arm_source = _phase1_stub()
    conn.execute(
        "INSERT OR IGNORE INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash) "
        "VALUES (?, ?, ?, ?, NULL, ?)",
        (clinician_id, patient_id, arm, arm_source, config_hash),
    )
    conn.commit()
    fresh = conn.execute(
        "SELECT arm, arm_source FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return fresh[0], fresh[1]


def _phase1_stub() -> tuple[str, str]:
    return "no_ai", "phase1_stub"
