"""practice_cases DAO (S11i): the sole writer of practice cases.

A practice case is one (clinician, practice patient) pair with a fixed arm,
pinned to the configuration it started under. It never enters schedules,
``arm_assignments`` or ``case_lifecycle``, so no measured count, balance or
replacement can see it. Schema triggers refuse deletes and any update but
a first ``completed_at``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ehr_simulator.db._timestamps import to_db_timestamp


@dataclass(frozen=True)
class PracticeCase:
    clinician_id: str
    patient_id: str
    arm: str
    config_version: str
    config_hash: str
    started_at: datetime
    completed_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        return self.completed_at is None


_COLUMNS = "clinician_id, patient_id, arm, config_version, config_hash, started_at, completed_at"


def fetch(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> PracticeCase | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM practice_cases WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else PracticeCase(*row)


def list_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> tuple[PracticeCase, ...]:
    """The clinician's practice cases, oldest first."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM practice_cases WHERE clinician_id = ? ORDER BY started_at, rowid",
        (clinician_id,),
    ).fetchall()
    return tuple(PracticeCase(*row) for row in rows)


def list_all(conn: sqlite3.Connection) -> tuple[PracticeCase, ...]:
    """S11n: every practice case, by clinician then start order."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM practice_cases ORDER BY clinician_id, started_at, rowid"
    ).fetchall()
    return tuple(PracticeCase(*row) for row in rows)


def fetch_all_pairs(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    """Every ``(clinician_id, patient_id)`` practice pair (export exclusion)."""
    rows = conn.execute("SELECT clinician_id, patient_id FROM practice_cases").fetchall()
    return frozenset((row[0], row[1]) for row in rows)


def start(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    arm: str,
    config_version: str,
    config_hash: str,
    now: datetime,
) -> None:
    """Insert one practice case (uncommitted: the caller's transaction owns it)."""
    conn.execute(
        "INSERT INTO practice_cases "
        "(clinician_id, patient_id, arm, config_version, config_hash, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (clinician_id, patient_id, arm, config_version, config_hash, to_db_timestamp(now)),
    )


def mark_completed(
    conn: sqlite3.Connection, *, clinician_id: str, patient_id: str, now: datetime
) -> bool:
    """Set ``completed_at`` once (uncommitted); ``False`` if not an open practice case."""
    cursor = conn.execute(
        "UPDATE practice_cases SET completed_at = ? "
        "WHERE clinician_id = ? AND patient_id = ? AND completed_at IS NULL",
        (to_db_timestamp(now), clinician_id, patient_id),
    )
    return cursor.rowcount == 1


def held_patient_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every practice patient any clinician holds; ``()`` before migration 11.

    Boot reads this read-only, possibly on a database not yet migrated.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'practice_cases'"
    ).fetchone()
    if exists is None:
        return ()
    rows = conn.execute("SELECT DISTINCT patient_id FROM practice_cases ORDER BY 1").fetchall()
    return tuple(row[0] for row in rows)
