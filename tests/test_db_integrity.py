"""Schema integrity guards: recursive triggers, append-only tables,
phase2 promotion, NOT NULL on idempotent inserts, enum lockstep.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ehr_simulator.case_lifecycle import LifecyclePolicy, evaluate
from ehr_simulator.db import arm_assignments, case_lifecycle, clinicians, progress
from ehr_simulator.db.answers import ANSWER_SOURCE_CLINICIAN, ANSWER_SOURCE_RULE
from ehr_simulator.db.backup import create_backup
from ehr_simulator.db.case_lifecycle import CaseState, IncompleteReason
from ehr_simulator.db.observation import ObservationMode

PATIENT = "p1"
SCHEDULE = "sched1"
VERSION = "v1"
HASH = "h1"
PHASE1_STUB = "phase1_stub"


def _clinician(db: sqlite3.Connection) -> str:
    return clinicians.lookup_or_create(db, "Dr. Integrity")


def _insert_phase2(db: sqlite3.Connection, cid: str, patient_id: str = PATIENT) -> None:
    db.execute(
        "INSERT INTO arm_assignments (clinician_id, patient_id, arm, arm_source, seed, "
        "config_hash, config_version, schedule_id, case_position, activated_at) "
        "VALUES (?, ?, 'ai', ?, 7, ?, ?, ?, 1, CURRENT_TIMESTAMP)",
        (cid, patient_id, arm_assignments.ARM_SOURCE_PHASE2, HASH, VERSION, SCHEDULE),
    )
    db.commit()


def _seed_history(db: sqlite3.Connection) -> None:
    # One row in each append-only table (raw SQL: the DAOs need full configs).
    db.execute("INSERT INTO study_identity (singleton, study_id) VALUES (1, 'study')")
    db.execute(
        "INSERT INTO configuration_history (config_version, study_id, config_hash, "
        "change_description, study_json, questions_json) "
        "VALUES (?, 'study', ?, 'd', '{}', '{}')",
        (VERSION, HASH),
    )
    db.execute(
        "INSERT INTO randomisation_schedules (schedule_id, study_id, clinician_id, "
        "config_version, config_hash, algorithm_version, master_seed, derived_seed_hex, "
        "allocation_state_json, starting_ai_count, starting_no_ai_count, starting_arm, "
        "block_length, block_sequence_json) "
        "VALUES (?, 'study', 'c', ?, ?, 'a', 1, 'ff', '{}', 0, 0, 'ai', 2, '[]')",
        (SCHEDULE, VERSION, HASH),
    )
    db.execute(
        "INSERT INTO randomisation_schedule_items (schedule_id, case_position, patient_id, "
        "planned_arm, block_number, position_in_block, assignment_seed) "
        "VALUES (?, 1, ?, 'ai', 1, 1, 9)",
        (SCHEDULE, PATIENT),
    )
    cid = _clinician(db)
    db.execute(
        "INSERT INTO events (clinician_id, kind, payload_json) VALUES (?, 'session.start', '{}')",
        (cid,),
    )
    db.commit()


# ---------------------------------------------------------------------------
# 1. recursive_triggers: INSERT OR REPLACE fires the delete triggers
# ---------------------------------------------------------------------------


def test_insert_or_replace_cannot_overwrite_a_phase2_assignment(db: sqlite3.Connection) -> None:
    cid = _clinician(db)
    _insert_phase2(db, cid)

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "INSERT OR REPLACE INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, config_hash) VALUES (?, ?, 'no_ai', ?, ?)",
            (cid, PATIENT, PHASE1_STUB, HASH),
        )
    db.rollback()

    row = arm_assignments.fetch_for_pair(db, cid, PATIENT)
    assert row is not None and row.arm_source == arm_assignments.ARM_SOURCE_PHASE2


def test_insert_or_replace_cannot_overwrite_a_completed_lifecycle(db: sqlite3.Connection) -> None:
    cid = _clinician(db)
    _insert_phase2(db, cid)
    now = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    case_lifecycle.insert_active(db, clinician_id=cid, patient_id=PATIENT, now=now)
    case_lifecycle.complete(db, clinician_id=cid, patient_id=PATIENT, now=now)

    with pytest.raises(sqlite3.IntegrityError, match="permanent"):
        db.execute(
            "INSERT OR REPLACE INTO case_lifecycle "
            "(clinician_id, patient_id, state, state_changed_at, last_seen_at) "
            "VALUES (?, ?, 'active', '2026-01-01 11:00:00', '2026-01-01 11:00:00')",
            (cid, PATIENT),
        )
    db.rollback()

    assert case_lifecycle.fetch(db, cid, PATIENT).state is CaseState.COMPLETED


# ---------------------------------------------------------------------------
# 2. a non-phase2 row cannot be promoted by UPDATE
# ---------------------------------------------------------------------------


def test_phase1_row_cannot_be_promoted_to_phase2(db: sqlite3.Connection) -> None:
    cid = _clinician(db)
    arm_assignments.assign_or_lookup(db, cid, PATIENT, config_hash=HASH)

    with pytest.raises(sqlite3.IntegrityError, match="phase2_randomized"):
        db.execute(
            "UPDATE arm_assignments SET arm_source = ? WHERE clinician_id = ?",
            (arm_assignments.ARM_SOURCE_PHASE2, cid),
        )
    db.rollback()

    assert arm_assignments.fetch_for_pair(db, cid, PATIENT).arm_source == PHASE1_STUB


def test_phase1_row_still_takes_a_config_version_backfill(db: sqlite3.Connection) -> None:
    # S11b backfill updates config_version on legacy rows; it must keep working.
    cid = _clinician(db)
    arm_assignments.assign_or_lookup(db, cid, PATIENT, config_hash=HASH)

    db.execute(
        "UPDATE arm_assignments SET config_version = ? WHERE clinician_id = ?", (VERSION, cid)
    )
    db.commit()

    assert arm_assignments.fetch_for_pair(db, cid, PATIENT).config_version == VERSION


# ---------------------------------------------------------------------------
# 3. append-only tables refuse UPDATE and DELETE
# ---------------------------------------------------------------------------

_APPEND_ONLY_WRITES = (
    "UPDATE events SET kind = 'session.end'",
    "DELETE FROM events",
    "UPDATE configuration_history SET config_hash = 'forged'",
    "DELETE FROM configuration_history",
    "UPDATE randomisation_schedules SET master_seed = 2",
    "DELETE FROM randomisation_schedules",
    "UPDATE randomisation_schedule_items SET planned_arm = 'no_ai'",
    "DELETE FROM randomisation_schedule_items",
    "UPDATE study_identity SET study_id = 'other'",
    "DELETE FROM study_identity",
)


@pytest.mark.parametrize("statement", _APPEND_ONLY_WRITES)
def test_append_only_tables_refuse_update_and_delete(
    db: sqlite3.Connection, statement: str
) -> None:
    _seed_history(db)

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(statement)
    db.rollback()


# ---------------------------------------------------------------------------
# 8. idempotent inserts no longer swallow NOT NULL violations
# ---------------------------------------------------------------------------


def test_assign_or_lookup_null_config_hash_raises(db: sqlite3.Connection) -> None:
    cid = _clinician(db)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        arm_assignments.assign_or_lookup(db, cid, PATIENT, config_hash=None)  # type: ignore[arg-type]


def test_progress_unlock_null_config_hash_raises(db: sqlite3.Connection) -> None:
    cid = _clinician(db)

    with pytest.raises(sqlite3.IntegrityError, match="NOT NULL"):
        progress.unlock(
            db,
            clinician_id=cid,
            patient_id=PATIENT,
            from_t_index=0,
            to_t_index=1,
            config_hash=None,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# 6. lifecycle timestamps keep sub-second precision
# ---------------------------------------------------------------------------


def test_reconnection_timeout_does_not_fire_early_on_subsecond_last_seen(
    db: sqlite3.Connection,
) -> None:
    # last_seen 10:00:00.9 + grace 300 s → deadline 10:05:00.9; a contact
    # at 10:05:00.5 is inside the grace.
    cid = _clinician(db)
    _insert_phase2(db, cid)
    last_seen = datetime(2026, 1, 1, 10, 0, 0, 900_000, tzinfo=UTC)
    case_lifecycle.insert_active(db, clinician_id=cid, patient_id=PATIENT, now=last_seen)

    stored = case_lifecycle.fetch(db, cid, PATIENT)
    policy = LifecyclePolicy(reconnection_grace_seconds=300, pause_grace_seconds=None)
    contact = last_seen + timedelta(seconds=300) - timedelta(milliseconds=400)

    assert stored.last_seen_at == last_seen
    assert evaluate(stored, policy, contact) is None


def test_second_precision_lifecycle_rows_still_parse(db: sqlite3.Connection) -> None:
    cid = _clinician(db)
    _insert_phase2(db, cid)
    db.execute(
        "INSERT INTO case_lifecycle (clinician_id, patient_id, state, state_changed_at, "
        "last_seen_at) VALUES (?, ?, 'active', '2026-01-01 10:00:00', '2026-01-01 10:00:00')",
        (cid, PATIENT),
    )
    db.commit()

    stored = case_lifecycle.fetch(db, cid, PATIENT)

    assert stored.last_seen_at == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 11. CHECK / trigger literals stay in lockstep with the Python constants
# ---------------------------------------------------------------------------


def _check_literals(db: sqlite3.Connection, table: str, column: str) -> set[str]:
    """Quoted literals of ``<column> IN (...)`` in the table's stored DDL."""
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    match = re.search(rf"{column}\s+IN\s*\(([^)]*)\)", sql)
    assert match is not None, f"{table}.{column} has no IN (...) CHECK"
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _trigger_literals(db: sqlite3.Connection, table: str, column: str) -> set[str]:
    """Literals compared to ``<column>`` in every trigger on ``table``."""
    rows = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
    ).fetchall()
    return {lit for (sql,) in rows for lit in re.findall(rf"{column}\s*=\s*'([^']*)'", sql)}


def test_schema_literals_match_python_constants(db: sqlite3.Connection) -> None:
    arms = {arm_assignments.ARM_AI, arm_assignments.ARM_NO_AI}
    modes = {str(m) for m in ObservationMode}

    assert _check_literals(db, "case_lifecycle", "state") == {str(s) for s in CaseState}
    assert _check_literals(db, "case_lifecycle", "incomplete_reason") == {
        str(r) for r in IncompleteReason
    }
    assert _check_literals(db, "sessions", "observation_mode") == modes
    assert _check_literals(db, "progress", "observation_mode") == modes
    assert _check_literals(db, "answers", "observation_mode") == modes
    assert _check_literals(db, "answers", "answer_source") == {
        ANSWER_SOURCE_CLINICIAN,
        ANSWER_SOURCE_RULE,
    }
    assert _check_literals(db, "randomisation_schedules", "starting_arm") == arms
    assert _check_literals(db, "randomisation_schedule_items", "planned_arm") == arms
    assert _check_literals(db, "case_replacements", "planned_arm") == arms
    assert _check_literals(db, "practice_cases", "arm") == arms
    assert _trigger_literals(db, "arm_assignments", "arm_source") == {
        arm_assignments.ARM_SOURCE_PHASE2
    }
    assert _trigger_literals(db, "clinician_profiles", "arm_source") == {
        arm_assignments.ARM_SOURCE_PHASE2
    }
    assert _trigger_literals(db, "answers", "answer_source") == {ANSWER_SOURCE_RULE}


# ---------------------------------------------------------------------------
# 13. backup never creates its source
# ---------------------------------------------------------------------------


def test_backup_of_missing_db_raises_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        create_backup(missing, tmp_path / "backups")

    assert not missing.exists()
