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
from dataclasses import dataclass


@dataclass(frozen=True)
class ArmAssignment:
    """One fully materialized ``arm_assignments`` row (S9c export reads all)."""

    clinician_id: str
    patient_id: str
    arm: str
    arm_source: str
    seed: int | None
    config_hash: str


def fetch_all(conn: sqlite3.Connection) -> tuple[ArmAssignment, ...]:
    """Every locked arm assignment, ordered by (clinician_id, patient_id).

    S9c read path: the export validates that all exported pairs hold an
    assignment and that the answer row arm matches; the stable order keeps
    re-runs deterministic.
    """
    rows = conn.execute(
        "SELECT clinician_id, patient_id, arm, arm_source, seed, config_hash "
        "FROM arm_assignments ORDER BY clinician_id, patient_id"
    ).fetchall()
    return tuple(
        ArmAssignment(
            clinician_id=row[0],
            patient_id=row[1],
            arm=row[2],
            arm_source=row[3],
            seed=row[4],
            config_hash=row[5],
        )
        for row in rows
    )


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
