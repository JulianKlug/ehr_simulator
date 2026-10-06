"""case_replacements DAO (S11f): sole writer of replacement plans.

One immutable plan per incomplete original case::

    original (incomplete) ──plan──► schedule item (unactivated, reserved)
                                        │ Start case
                                        ▼
                                    activated_at set once

A pending plan (``activated_at IS NULL``) reserves its schedule position so
ordinary scheduling never takes it. ``commit=False`` leaves a write in the
caller's transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ehr_simulator.db._timestamps import _as_utc, to_db_timestamp
from ehr_simulator.db.exceptions import CaseActivationError


@dataclass(frozen=True)
class ReplacementPlan:
    replacement_id: str
    clinician_id: str
    original_patient_id: str
    replacement_patient_id: str
    replacement_schedule_id: str
    replacement_case_position: int
    planned_arm: str
    generated_at: datetime
    activated_at: datetime | None

    @property
    def is_pending(self) -> bool:
        return self.activated_at is None


_COLUMNS = (
    "replacement_id, clinician_id, original_patient_id, replacement_patient_id, "
    "replacement_schedule_id, replacement_case_position, planned_arm, generated_at, "
    "activated_at"
)


def _row(row: tuple) -> ReplacementPlan:
    return ReplacementPlan(
        *row[:7],
        generated_at=_as_utc(row[7]),
        activated_at=_as_utc(row[8]),
    )


def fetch_for_original(
    conn: sqlite3.Connection, clinician_id: str, original_patient_id: str
) -> ReplacementPlan | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM case_replacements "
        "WHERE clinician_id = ? AND original_patient_id = ?",
        (clinician_id, original_patient_id),
    ).fetchone()
    return None if row is None else _row(row)


def list_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> tuple[ReplacementPlan, ...]:
    """Every plan, oldest first (the order Start case activates pending ones)."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM case_replacements WHERE clinician_id = ? "
        "ORDER BY generated_at, replacement_case_position",
        (clinician_id,),
    ).fetchall()
    return tuple(_row(row) for row in rows)


def list_all(conn: sqlite3.Connection) -> tuple[ReplacementPlan, ...]:
    """S11n: every plan, by clinician then generation order."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM case_replacements "
        "ORDER BY clinician_id, generated_at, replacement_case_position"
    ).fetchall()
    return tuple(_row(row) for row in rows)


def pending_for_clinician(
    conn: sqlite3.Connection, clinician_id: str
) -> tuple[ReplacementPlan, ...]:
    return tuple(p for p in list_for_clinician(conn, clinician_id) if p.is_pending)


def insert(conn: sqlite3.Connection, plan: ReplacementPlan, *, commit: bool = True) -> None:
    conn.execute(
        f"INSERT INTO case_replacements ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            plan.replacement_id,
            plan.clinician_id,
            plan.original_patient_id,
            plan.replacement_patient_id,
            plan.replacement_schedule_id,
            plan.replacement_case_position,
            plan.planned_arm,
            to_db_timestamp(plan.generated_at),
        ),
    )
    if commit:
        conn.commit()


def mark_activated(
    conn: sqlite3.Connection, replacement_id: str, *, now: datetime, commit: bool = True
) -> None:
    """Set ``activated_at`` once; a second activation is refused."""
    joined = conn.in_transaction
    cursor = conn.execute(
        "UPDATE case_replacements SET activated_at = ? "
        "WHERE replacement_id = ? AND activated_at IS NULL",
        (to_db_timestamp(now), replacement_id),
    )
    if cursor.rowcount == 1:
        if commit:
            conn.commit()
        return

    # A miss never commits the caller's pending writes; it only ends the
    # empty transaction its own UPDATE opened.
    if commit and not joined:
        conn.rollback()
    raise CaseActivationError(f"replacement {replacement_id} is unknown or already activated")
