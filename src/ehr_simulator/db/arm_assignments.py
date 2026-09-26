"""arm_assignments DAO: assign + lock the AI / no-AI arm for a (clinician,
patient) pair.

S6 ships the ``phase1_stub`` assigner — always returns ``("no_ai",
"phase1_stub")``. The row is INSERT-OR-IGNOREd so the first call for a
``(clinician_id, patient_id)`` pair locks the assignment forever. S11 will
swap the body to a deterministic randomized assigner; the call signature
stays.

REGRESSION (test #11): switching to randomized in S11 must NOT rewrite
existing rows.

S11d adds the Phase 2 path: :func:`activate_planned_assignment` turns one
planned schedule item into an immutable ``phase2_randomized`` row (schema
triggers refuse UPDATE/DELETE on those rows). ``phase1_stub`` stays for
study mode without ``randomisation``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ehr_simulator.db import config_history
from ehr_simulator.db.answers import _require_version_provenance
from ehr_simulator.db.exceptions import CaseActivationError, ConfigurationProvenanceError
from ehr_simulator.db.randomisation import GeneratedSchedule, ScheduleItem

ARM_SOURCE_PHASE2 = "phase2_randomized"
ARM_AI = "ai"
ARM_NO_AI = "no_ai"


@dataclass(frozen=True)
class ArmAssignment:
    """One fully materialized ``arm_assignments`` row (S9c export reads all)."""

    clinician_id: str
    patient_id: str
    arm: str
    arm_source: str
    seed: int | None
    config_hash: str
    config_version: str | None = None


@dataclass(frozen=True)
class ActivatedAssignment:
    """One ``arm_assignments`` row with its S11d activation provenance.

    ``schedule_id``/``case_position``/``activated_at`` are ``None`` on
    legacy and ``phase1_stub`` rows.
    """

    clinician_id: str
    patient_id: str
    arm: str
    arm_source: str
    seed: int | None
    config_hash: str
    config_version: str | None
    schedule_id: str | None
    case_position: int | None
    assigned_at: datetime | str
    activated_at: datetime | str | None


_ACTIVATED_COLUMNS = (
    "clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version, "
    "schedule_id, case_position, assigned_at, activated_at"
)


def fetch_all(conn: sqlite3.Connection) -> tuple[ArmAssignment, ...]:
    """Every locked arm assignment, ordered by (clinician_id, patient_id).

    S9c read path: the export validates that all exported pairs hold an
    assignment and that the answer row arm matches; the stable order keeps
    re-runs deterministic.
    """
    rows = conn.execute(
        "SELECT clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version "
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
            config_version=row[6],
        )
        for row in rows
    )


def assigned_patient_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every patient holding an assignment, sorted; ``()`` before migration 1.

    S11b: a case pinned to an older configuration keeps its patient even
    after the active ``patient_ids`` drops it, so dataset loading needs this list.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'arm_assignments'"
    ).fetchone()
    if has_table is None:
        return ()

    rows = conn.execute(
        "SELECT DISTINCT patient_id FROM arm_assignments ORDER BY patient_id"
    ).fetchall()
    return tuple(row[0] for row in rows)


def assign_or_lookup(
    conn: sqlite3.Connection,
    clinician_id: str,
    patient_id: str,
    *,
    config_hash: str,
    config_version: str | None = None,
) -> tuple[str, str]:
    """Return ``(arm, arm_source)`` for the (clinician, patient) pair,
    creating the row if it doesn't exist. Existing rows are never rewritten.

    The new row's provenance (``config_version``, ``config_hash``) pins it
    to the configuration under which the case started (S11b); once the
    study has history, an omitted ``config_version`` is refused.
    """
    _require_version_provenance(conn, config_version)
    row = conn.execute(
        "SELECT arm, arm_source FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    if row is not None:
        return row[0], row[1]
    arm, arm_source = _phase1_stub()
    conn.execute(
        "INSERT OR IGNORE INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version) "
        "VALUES (?, ?, ?, ?, NULL, ?, ?)",
        (clinician_id, patient_id, arm, arm_source, config_hash, config_version),
    )
    conn.commit()
    fresh = conn.execute(
        "SELECT arm, arm_source FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return fresh[0], fresh[1]


def fetch_for_pair(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> ArmAssignment | None:
    """The pair's locked row (with provenance), or ``None`` if never assigned."""
    row = conn.execute(
        "SELECT clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version "
        "FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    if row is None:
        return None
    return ArmAssignment(
        clinician_id=row[0],
        patient_id=row[1],
        arm=row[2],
        arm_source=row[3],
        seed=row[4],
        config_hash=row[5],
        config_version=row[6],
    )


def fetch_activated_for_pair(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> ActivatedAssignment | None:
    """The pair's row with activation provenance, or ``None``."""
    row = conn.execute(
        f"SELECT {_ACTIVATED_COLUMNS} FROM arm_assignments "
        "WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else ActivatedAssignment(*row)


def list_for_clinician(
    conn: sqlite3.Connection, clinician_id: str
) -> tuple[ActivatedAssignment, ...]:
    """Every case the clinician holds: legacy rows first, then activation order."""
    rows = conn.execute(
        f"SELECT {_ACTIVATED_COLUMNS} FROM arm_assignments WHERE clinician_id = ? "
        "ORDER BY activated_at IS NOT NULL, activated_at, case_position, patient_id",
        (clinician_id,),
    ).fetchall()
    return tuple(ActivatedAssignment(*row) for row in rows)


def activated_positions(conn: sqlite3.Connection, schedule_id: str) -> frozenset[int]:
    """Schedule positions already consumed by an activation."""
    rows = conn.execute(
        "SELECT case_position FROM arm_assignments WHERE schedule_id = ?", (schedule_id,)
    ).fetchall()
    return frozenset(row[0] for row in rows)


def activated_arm_counts(conn: sqlite3.Connection) -> dict[str, tuple[int, int]]:
    """``{patient_id: (ai, no_ai)}`` over every realised Phase 2 activation.

    Study scope (one database is one study, S11a): all clinicians, all
    configuration versions. ``phase1_stub`` and legacy rows never count.
    """
    rows = conn.execute(
        "SELECT patient_id, "
        "SUM(CASE WHEN arm = ? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN arm = ? THEN 1 ELSE 0 END) "
        "FROM arm_assignments WHERE arm_source = ? GROUP BY patient_id",
        (ARM_AI, ARM_NO_AI, ARM_SOURCE_PHASE2),
    ).fetchall()
    return {row[0]: (row[1], row[2]) for row in rows}


def _require_stored_item(
    conn: sqlite3.Connection, clinician_id: str, schedule: GeneratedSchedule, item: ScheduleItem
) -> None:
    """The item is exactly the one stored in the clinician's own schedule."""
    if schedule.clinician_id != clinician_id:
        raise CaseActivationError(
            f"schedule {schedule.schedule_id[:12]}… belongs to another clinician"
        )

    row = conn.execute(
        "SELECT i.patient_id, i.planned_arm, i.assignment_seed "
        "FROM randomisation_schedule_items i "
        "JOIN randomisation_schedules s ON s.schedule_id = i.schedule_id "
        "WHERE i.schedule_id = ? AND i.case_position = ? AND s.clinician_id = ?",
        (schedule.schedule_id, item.case_position, clinician_id),
    ).fetchone()
    if row is None or tuple(row) != (item.patient_id, item.planned_arm, item.assignment_seed):
        raise CaseActivationError(
            f"position {item.case_position} is not a stored item of the clinician's schedule"
        )


def _is_exact_retry(
    existing: ActivatedAssignment,
    schedule: GeneratedSchedule,
    item: ScheduleItem,
    config_version: str,
    config_hash: str,
) -> bool:
    return (
        existing.arm_source == ARM_SOURCE_PHASE2
        and existing.arm == item.planned_arm
        and existing.seed == item.assignment_seed
        and existing.schedule_id == schedule.schedule_id
        and existing.case_position == item.case_position
        and existing.config_version == config_version
        and existing.config_hash == config_hash
    )


def activate_planned_assignment(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    schedule: GeneratedSchedule,
    item: ScheduleItem,
    config_version: str,
    config_hash: str,
    commit: bool = True,
) -> ActivatedAssignment:
    """Realise one planned item as an immutable ``phase2_randomized`` row.

    Arm and seed come from the item; ``config_version``/``config_hash`` are
    the configuration active at activation (the schedule keeps its own
    generation provenance). An exact retry returns the existing row.
    ``commit=False`` leaves the INSERT in the caller's transaction.

    Raises:
        CaseActivationError: the pair is already held with other data, the
            item is not the clinician's stored item, its position was
            activated for another patient, or the provenance is unknown.
    """
    _require_stored_item(conn, clinician_id, schedule, item)
    try:
        config_history.require_known(conn, config_version, config_hash)
    except ConfigurationProvenanceError as exc:
        raise CaseActivationError(str(exc)) from exc

    existing = fetch_activated_for_pair(conn, clinician_id, item.patient_id)
    if existing is not None:
        if _is_exact_retry(existing, schedule, item, config_version, config_hash):
            return existing

        raise CaseActivationError(
            f"clinician already holds patient {item.patient_id!r}; refusing a second activation"
        )

    if item.case_position in activated_positions(conn, schedule.schedule_id):
        raise CaseActivationError(
            f"schedule position {item.case_position} is already activated for another patient"
        )

    # One statement: both timestamps read the same CURRENT_TIMESTAMP.
    conn.execute(
        "INSERT INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version, "
        " schedule_id, case_position, assigned_at, activated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        (
            clinician_id,
            item.patient_id,
            item.planned_arm,
            ARM_SOURCE_PHASE2,
            item.assignment_seed,
            config_hash,
            config_version,
            schedule.schedule_id,
            item.case_position,
        ),
    )
    if commit:
        conn.commit()

    return fetch_activated_for_pair(conn, clinician_id, item.patient_id)  # type: ignore[return-value]


def _phase1_stub() -> tuple[str, str]:
    return "no_ai", "phase1_stub"
