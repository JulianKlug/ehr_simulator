"""S11f replacement planning: pure selection rule + one-transaction planner.

An incomplete case may receive one replacement: an unactivated item of the
clinician's own S11c schedule, realised earlier than planned. The original
schedule and the original case are never rewritten::

    incomplete original (arm A)
        │ plan_replacement (BEGIN IMMEDIATE, one commit)
        ▼
    candidates = unactivated, unreserved schedule items whose patient the
                 clinician never held and the active pool still lists
        │ lexicographic minimum of
        │   (arm ≠ A, clinician |ai-no_ai|, patient |ai-no_ai|,
        │    |position - next nominal position|, HMAC tie rank)
        ▼
    case_replacements row (pending) + case.replacement_planned

Example: schedule ``1 ai, 2 no_ai, 3 ai``; position 1 times out → position 3
(same arm) beats position 2 although 2 is closer in sequence.

Balance terms count activated cases only (the incomplete one included);
pending plans never count. Limits are not checked here — Start case
enforces them at activation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ehr_simulator.config.snapshot import parse_study_snapshot
from ehr_simulator.db import arm_assignments, config_history, events
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import randomisation as schedules
from ehr_simulator.db import replacements as replacements_dao
from ehr_simulator.db.arm_assignments import ARM_AI, ARM_SOURCE_PHASE2
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.exceptions import CaseLifecycleError, ConfigurationProvenanceError
from ehr_simulator.db.randomisation import GeneratedSchedule, ScheduleItem
from ehr_simulator.db.replacements import ReplacementPlan
from ehr_simulator.randomisation import (
    ActivatedAllocationState,
    load_activated_allocation_state,
    replacement_tie_rank,
)


@dataclass(frozen=True)
class ArmCounts:
    ai: int = 0
    no_ai: int = 0

    def projected_imbalance(self, arm: str) -> int:
        """``|ai - no_ai|`` after one more activation on ``arm``."""
        ai = self.ai + (1 if arm == ARM_AI else 0)
        no_ai = self.no_ai + (0 if arm == ARM_AI else 1)
        return abs(ai - no_ai)


# ---------------------------------------------------------------------------
# Pure selection
# ---------------------------------------------------------------------------


def replacement_id(
    *,
    study_id: str,
    clinician_id: str,
    original_patient_id: str,
    schedule_id: str,
    case_position: int,
) -> str:
    """SHA256 of the canonical plan identity: same inputs, same id."""
    payload = json.dumps(
        {
            "study_id": study_id,
            "clinician_id": clinician_id,
            "original_patient_id": original_patient_id,
            "replacement_schedule_id": schedule_id,
            "replacement_case_position": case_position,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def select_replacement(
    candidates: Iterable[ScheduleItem],
    *,
    original_patient_id: str,
    original_arm: str,
    clinician_counts: ArmCounts,
    patient_counts: Mapping[str, ArmCounts],
    next_nominal_position: int,
    schedule_key_hex: str,
) -> ScheduleItem | None:
    """The locked S11f rule: lexicographically smallest score tuple."""

    def score(item: ScheduleItem) -> tuple[int, int, int, int, bytes]:
        return (
            0 if item.planned_arm == original_arm else 1,
            clinician_counts.projected_imbalance(item.planned_arm),
            patient_counts.get(item.patient_id, ArmCounts()).projected_imbalance(item.planned_arm),
            abs(item.case_position - next_nominal_position),
            replacement_tie_rank(
                schedule_key_hex,
                original_patient_id=original_patient_id,
                case_position=item.case_position,
                patient_id=item.patient_id,
                planned_arm=item.planned_arm,
            ),
        )

    pool = list(candidates)
    return min(pool, key=score) if pool else None


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _replacements_enabled(conn: sqlite3.Connection, config_version: str, config_hash: str) -> bool:
    """The flag as pinned by the original case's activation configuration."""
    row = config_history.require_known(conn, config_version, config_hash)
    lifecycle = parse_study_snapshot(row.study_json).case_lifecycle
    return lifecycle is not None and lifecycle.replacement_cases_enabled


def _active_pool(conn: sqlite3.Connection) -> tuple[str, list[str]]:
    """``(study_id, patient_ids)`` of the database's active configuration."""
    active = config_history.fetch_active(conn)
    if active is None:
        raise ConfigurationProvenanceError("no active configuration to plan a replacement under")

    study = parse_study_snapshot(active.study_json)
    return study.study_id, list(study.patient_ids)


def _clinician_counts(conn: sqlite3.Connection, clinician_id: str) -> ArmCounts:
    phase2 = [
        a
        for a in arm_assignments.list_for_clinician(conn, clinician_id)
        if a.arm_source == ARM_SOURCE_PHASE2
    ]
    ai = sum(1 for a in phase2 if a.arm == ARM_AI)
    return ArmCounts(ai=ai, no_ai=len(phase2) - ai)


def _patient_counts(state: ActivatedAllocationState) -> dict[str, ArmCounts]:
    return {p.patient_id: ArmCounts(p.ai_count, p.no_ai_count) for p in state.patients}


def eligible_candidates(
    conn: sqlite3.Connection,
    schedule: GeneratedSchedule,
    *,
    clinician_id: str,
    active_patient_ids: Iterable[str],
) -> tuple[list[ScheduleItem], int | None]:
    """Candidate items + the next nominal (lowest unactivated, unreserved) position."""
    consumed = arm_assignments.activated_positions(conn, schedule.schedule_id)
    reserved = {
        p.replacement_case_position
        for p in replacements_dao.pending_for_clinician(conn, clinician_id)
        if p.replacement_schedule_id == schedule.schedule_id
    }
    held = {a.patient_id for a in arm_assignments.list_for_clinician(conn, clinician_id)}
    pool = set(active_patient_ids)

    open_items = [i for i in schedule.items if i.case_position not in consumed | reserved]
    next_nominal = min((i.case_position for i in open_items), default=None)
    candidates = [i for i in open_items if i.patient_id not in held and i.patient_id in pool]
    return candidates, next_nominal


def _plan_locked(
    conn: sqlite3.Connection, *, clinician_id: str, original_patient_id: str, now: datetime
) -> ReplacementPlan | None:
    case = lifecycle_dao.fetch(conn, clinician_id, original_patient_id)
    if case is None or case.state is not CaseState.INCOMPLETE:
        found = "no lifecycle row" if case is None else f"state {case.state}"
        raise CaseLifecycleError(f"only incomplete cases can be replaced; found {found}")

    original = arm_assignments.fetch_activated_for_pair(conn, clinician_id, original_patient_id)
    if original is None or original.config_version is None or original.schedule_id is None:
        raise ConfigurationProvenanceError("the incomplete case has no Phase 2 provenance")
    if not _replacements_enabled(conn, original.config_version, original.config_hash):
        return None

    existing = replacements_dao.fetch_for_original(conn, clinician_id, original_patient_id)
    if existing is not None:
        return existing

    study_id, active_patient_ids = _active_pool(conn)
    stored = schedules.fetch_for_clinician(conn, study_id, clinician_id)
    if stored is None:
        raise ConfigurationProvenanceError("the clinician holds a case but no schedule")

    schedule = stored.schedule
    candidates, next_nominal = eligible_candidates(
        conn, schedule, clinician_id=clinician_id, active_patient_ids=active_patient_ids
    )
    chosen = select_replacement(
        candidates,
        original_patient_id=original_patient_id,
        original_arm=original.arm,
        clinician_counts=_clinician_counts(conn, clinician_id),
        patient_counts=_patient_counts(load_activated_allocation_state(conn, active_patient_ids)),
        next_nominal_position=next_nominal or 0,
        schedule_key_hex=schedule.derived_seed_hex,
    )
    if chosen is None:
        return None

    plan = ReplacementPlan(
        replacement_id=replacement_id(
            study_id=study_id,
            clinician_id=clinician_id,
            original_patient_id=original_patient_id,
            schedule_id=schedule.schedule_id,
            case_position=chosen.case_position,
        ),
        clinician_id=clinician_id,
        original_patient_id=original_patient_id,
        replacement_patient_id=chosen.patient_id,
        replacement_schedule_id=schedule.schedule_id,
        replacement_case_position=chosen.case_position,
        planned_arm=chosen.planned_arm,
        generated_at=now,
        activated_at=None,
    )
    replacements_dao.insert(conn, plan, commit=False)
    events.append(
        conn,
        session_id=None,
        clinician_id=clinician_id,
        patient_id=original_patient_id,
        timepoint=None,
        kind="case.replacement_planned",
        payload={
            "replacement_id": plan.replacement_id,
            "replacement_patient_id": plan.replacement_patient_id,
            "replacement_case_position": plan.replacement_case_position,
        },
        commit=False,
    )
    return plan


def plan_replacement(
    conn: sqlite3.Connection, *, clinician_id: str, original_patient_id: str, now: datetime
) -> ReplacementPlan | None:
    """Plan (or return) the replacement of one incomplete case; one commit.

    ``None``: replacements are disabled for the case, or no eligible
    candidate remains — nothing is written.

    Raises:
        CaseLifecycleError: the original case is not incomplete.
        ConfigurationProvenanceError: the case or configuration is unknown.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan = _plan_locked(
            conn, clinician_id=clinician_id, original_patient_id=original_patient_id, now=now
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return plan


def plan_missing_replacements(
    conn: sqlite3.Connection, *, clinician_id: str, now: datetime
) -> None:
    """Plan for every incomplete case without a plan (heals a crash window)."""
    planned = {
        p.original_patient_id for p in replacements_dao.list_for_clinician(conn, clinician_id)
    }
    for patient_id, case in lifecycle_dao.list_for_clinician(conn, clinician_id).items():
        if case.state is CaseState.INCOMPLETE and patient_id not in planned:
            plan_replacement(
                conn, clinician_id=clinician_id, original_patient_id=patient_id, now=now
            )
