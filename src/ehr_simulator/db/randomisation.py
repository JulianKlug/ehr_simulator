"""S11c: planned randomisation schedules DAO.

``randomisation_schedules`` holds at most one immutable schedule per
``(study_id, clinician_id)``; ``randomisation_schedule_items`` its ordered
patient/arm positions. **This module is the only writer to both tables.**

Planned only: nothing here activates an allocation or touches
``arm_assignments`` (S11d consumes items and copies ``assignment_seed``
into ``arm_assignments.seed``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ehr_simulator.db.exceptions import RandomisationIntegrityError

__all__ = [
    "GeneratedSchedule",
    "ScheduleItem",
    "StoredSchedule",
    "fetch_for_clinician",
    "insert_schedule",
    "list_schedules",
]

_SCHEDULE_COLUMNS = (
    "schedule_id, study_id, clinician_id, config_version, config_hash, "
    "algorithm_version, master_seed, derived_seed_hex, allocation_state_json, "
    "starting_ai_count, starting_no_ai_count, starting_arm, block_length, "
    "block_sequence_json, generated_at"
)

_ITEM_COLUMNS = (
    "case_position, patient_id, planned_arm, block_number, position_in_block, "
    "preceding_block_arm, planned_cases_since_ai, assignment_seed"
)


@dataclass(frozen=True)
class ScheduleItem:
    """One planned position (1-based ``case_position``)."""

    case_position: int
    patient_id: str
    planned_arm: str
    block_number: int
    position_in_block: int
    preceding_block_arm: str | None
    planned_cases_since_ai: int | None
    assignment_seed: int


@dataclass(frozen=True)
class GeneratedSchedule:
    """A complete schedule as generated — every persisted input and item."""

    schedule_id: str
    study_id: str
    clinician_id: str
    config_version: str
    config_hash: str
    algorithm_version: str
    master_seed: int
    derived_seed_hex: str
    allocation_state_json: str
    starting_ai_count: int
    starting_no_ai_count: int
    starting_arm: str
    block_length: int
    block_sequence_json: str
    items: tuple[ScheduleItem, ...]


@dataclass(frozen=True)
class StoredSchedule:
    """A persisted schedule plus its storage timestamp."""

    schedule: GeneratedSchedule
    generated_at: object  # datetime under PARSE_DECLTYPES; str on a bare connection


def _fetch_items(conn: sqlite3.Connection, schedule_id: str) -> tuple[ScheduleItem, ...]:
    rows = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM randomisation_schedule_items "
        "WHERE schedule_id = ? ORDER BY case_position",
        (schedule_id,),
    ).fetchall()
    return tuple(ScheduleItem(*row) for row in rows)


def _to_stored(conn: sqlite3.Connection, row: sqlite3.Row) -> StoredSchedule:
    schedule = GeneratedSchedule(*row[:14], items=_fetch_items(conn, row[0]))
    return StoredSchedule(schedule=schedule, generated_at=row[14])


def fetch_for_clinician(
    conn: sqlite3.Connection, study_id: str, clinician_id: str
) -> StoredSchedule | None:
    """The clinician's schedule, or ``None`` if none was generated."""
    row = conn.execute(
        f"SELECT {_SCHEDULE_COLUMNS} FROM randomisation_schedules "
        "WHERE study_id = ? AND clinician_id = ?",
        (study_id, clinician_id),
    ).fetchone()
    if row is None:
        return None

    return _to_stored(conn, row)


def list_schedules(conn: sqlite3.Connection, study_id: str) -> tuple[StoredSchedule, ...]:
    """Every schedule of the study, in generation order (ties by id)."""
    rows = conn.execute(
        f"SELECT {_SCHEDULE_COLUMNS} FROM randomisation_schedules "
        "WHERE study_id = ? ORDER BY generated_at, schedule_id",
        (study_id,),
    ).fetchall()
    return tuple(_to_stored(conn, row) for row in rows)


def insert_schedule(conn: sqlite3.Connection, schedule: GeneratedSchedule) -> StoredSchedule:
    """Persist ``schedule`` and its items in one transaction, then commit.

    An existing schedule for the clinician is never overwritten: identical
    → returned unchanged (nothing written); different →
    :class:`RandomisationIntegrityError`. Any failure rolls back, so no
    header ever exists without its items. Joins a transaction the caller
    already opened (e.g. ``BEGIN IMMEDIATE``) and commits it.
    """
    try:
        existing = fetch_for_clinician(conn, schedule.study_id, schedule.clinician_id)
        if existing is not None:
            if existing.schedule != schedule:
                raise RandomisationIntegrityError(
                    f"clinician {schedule.clinician_id!r} already holds schedule "
                    f"{existing.schedule.schedule_id[:12]}…; refusing to replace it "
                    f"with {schedule.schedule_id[:12]}…"
                )
            conn.rollback()
            return existing

        conn.execute(
            "INSERT INTO randomisation_schedules "
            "(schedule_id, study_id, clinician_id, config_version, config_hash, "
            "algorithm_version, master_seed, derived_seed_hex, allocation_state_json, "
            "starting_ai_count, starting_no_ai_count, starting_arm, block_length, "
            "block_sequence_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                schedule.schedule_id,
                schedule.study_id,
                schedule.clinician_id,
                schedule.config_version,
                schedule.config_hash,
                schedule.algorithm_version,
                schedule.master_seed,
                schedule.derived_seed_hex,
                schedule.allocation_state_json,
                schedule.starting_ai_count,
                schedule.starting_no_ai_count,
                schedule.starting_arm,
                schedule.block_length,
                schedule.block_sequence_json,
            ),
        )
        conn.executemany(
            f"INSERT INTO randomisation_schedule_items (schedule_id, {_ITEM_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    schedule.schedule_id,
                    item.case_position,
                    item.patient_id,
                    item.planned_arm,
                    item.block_number,
                    item.position_in_block,
                    item.preceding_block_arm,
                    item.planned_cases_since_ai,
                    item.assignment_seed,
                )
                for item in schedule.items
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return fetch_for_clinician(conn, schedule.study_id, schedule.clinician_id)  # type: ignore[return-value]
