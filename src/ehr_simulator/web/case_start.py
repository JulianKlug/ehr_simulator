"""S11d: explicit Start case — the only path that realises a Phase 2 allocation.

::

    POST /case/start
        │
        ▼
    start_next_case ── open case? ──yes──► resume (no write)
        │ no
        ▼
    Phase A   stale-server check
              create_or_fetch_schedule          own commit; planned only,
              (allocation state read in lock)   consumes nothing
        │
        ▼
    Phase B   BEGIN IMMEDIATE
              open case? (a racing Start won) ──► rollback, resume
              stale-server check
              first unactivated item ── none ──► ScheduleExhaustedError
              schedule ↔ active config compatible
              activate assignment + session + case.activated + session.start
              COMMIT                            rollback on any failure

An **open case** is a ``phase2_randomized`` assignment whose progress is not
completed (derived from assignment + progress, never from sessions). Nothing
here returns or renders the arm to the caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ehr_simulator.db import arm_assignments, events, progress, sessions
from ehr_simulator.db import randomisation as schedules
from ehr_simulator.db.arm_assignments import ARM_SOURCE_PHASE2, ActivatedAssignment
from ehr_simulator.db.exceptions import CaseActivationError, ConfigurationProvenanceError
from ehr_simulator.db.randomisation import GeneratedSchedule, ScheduleItem
from ehr_simulator.logging import get_logger
from ehr_simulator.randomisation import (
    create_or_fetch_schedule,
    load_activated_allocation_state,
    require_schedule_compatible,
)
from ehr_simulator.web.study_session import (
    NOT_STARTED_T_INDEX,
    is_phase2_mode,
    require_active_case,
)


class CaseStartRefusedError(Exception):
    """Start case refused without any activation write (route: 409)."""


class NotPhase2StudyError(CaseStartRefusedError):
    """The server is not in Phase 2 study mode."""


class ScheduleExhaustedError(CaseStartRefusedError):
    """Every schedule item is already activated; no further cases."""


class StartOutcome(StrEnum):
    ACTIVATED = "activated"
    RESUMED = "resumed"


class IndexAction(StrEnum):
    START = "start"
    RESUME = "resume"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class StartedCase:
    """Where the clinician continues — deliberately without the arm."""

    patient_id: str
    resume_t_index: int
    outcome: StartOutcome


@dataclass(frozen=True)
class CaseIndexState:
    action: IndexAction
    open_patient_id: str | None = None
    resume_t_index: int = NOT_STARTED_T_INDEX


def case_patient_ids(conn: sqlite3.Connection, clinician_id: str) -> list[str]:
    """Patients the clinician already holds as cases (index + jumper list)."""
    return [a.patient_id for a in arm_assignments.list_for_clinician(conn, clinician_id)]


def find_open_case(conn: sqlite3.Connection, clinician_id: str) -> ActivatedAssignment | None:
    """The clinician's activated, not completed Phase 2 case, if any."""
    rows = progress.list_for_clinician(conn, clinician_id)
    for assignment in arm_assignments.list_for_clinician(conn, clinician_id):
        if assignment.arm_source != ARM_SOURCE_PHASE2:
            continue

        row = rows.get(assignment.patient_id)
        if row is None or row.completed_at is None:
            return assignment

    return None


def index_state(conn: sqlite3.Connection, app_state: Any, clinician_id: str) -> CaseIndexState:
    """Which case action the study index offers. Pure read: never creates a schedule."""
    opened = find_open_case(conn, clinician_id)
    if opened is not None:
        return CaseIndexState(
            IndexAction.RESUME, opened.patient_id, _resume_t_index(conn, clinician_id, opened)
        )

    stored = schedules.fetch_for_clinician(conn, app_state.study.study_id, clinician_id)
    if stored is not None and _next_item(conn, stored.schedule) is None:
        return CaseIndexState(IndexAction.EXHAUSTED)

    return CaseIndexState(IndexAction.START)


def start_next_case(conn: sqlite3.Connection, app_state: Any, *, clinician_id: str) -> StartedCase:
    """Resume the open case or activate the next planned one (see module diagram).

    Raises:
        NotPhase2StudyError / ScheduleExhaustedError: refused, nothing written.
        StaleConfigurationError: DB active configuration ≠ the running server.
        ScheduleIncompatibleError: the schedule no longer fits the active config.
        ConfigurationProvenanceError / RandomisationIntegrityError: integrity.
    """
    if not is_phase2_mode(app_state):
        raise NotPhase2StudyError("Start case needs a study with randomisation settings")

    opened = find_open_case(conn, clinician_id)
    if opened is not None:
        return _resumed(conn, clinician_id, opened)

    # Phase A: the schedule (planned only) commits on its own.
    study = app_state.study
    active = require_active_case(conn, app_state)
    if active.config_version is None:
        raise ConfigurationProvenanceError(
            "Phase 2 requires an activated configuration; run activate-config first"
        )

    stored = create_or_fetch_schedule(
        conn,
        study=study,
        config_version=active.config_version,
        config_hash=active.config_hash,
        clinician_id=clinician_id,
        load_allocation_state=lambda c: load_activated_allocation_state(c, study.patient_ids),
    )
    return _activate_next(conn, app_state, clinician_id=clinician_id, schedule=stored.schedule)


def _activate_next(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    schedule: GeneratedSchedule,
) -> StartedCase:
    """Phase B: one write transaction; any failure rolls every write back."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        opened = find_open_case(conn, clinician_id)
        if opened is not None:
            conn.rollback()
            return _resumed(conn, clinician_id, opened)

        active = require_active_case(conn, app_state)
        item = _next_item(conn, schedule)
        if item is None:
            raise ScheduleExhaustedError("no further cases: every scheduled case is activated")

        require_schedule_compatible(schedule, app_state.study, item)
        if arm_assignments.fetch_for_pair(conn, clinician_id, item.patient_id) is not None:
            raise CaseActivationError(
                f"schedule position {item.case_position} names a patient the clinician "
                "already holds; refusing a second activation"
            )

        assignment = arm_assignments.activate_planned_assignment(
            conn,
            clinician_id=clinician_id,
            schedule=schedule,
            item=item,
            config_version=active.config_version,  # type: ignore[arg-type]
            config_hash=active.config_hash,
            commit=False,
        )
        session_id = sessions.start_or_resume(
            conn,
            clinician_id,
            item.patient_id,
            arm=assignment.arm,
            config_hash=assignment.config_hash,
            config_version=assignment.config_version,
            commit=False,
        )
        _append(
            conn,
            session_id,
            clinician_id,
            item.patient_id,
            "case.activated",
            {
                "schedule_id": schedule.schedule_id,
                "case_position": item.case_position,
                "arm": assignment.arm,
            },
        )
        _append(
            conn,
            session_id,
            clinician_id,
            item.patient_id,
            "session.start",
            {"arm": assignment.arm},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        get_logger().warning(
            "start case rolled back; nothing activated", event_kind="case.start.rollback"
        )
        raise

    # Bumped once, post-commit (the commit=False writes skip their own bumps).
    app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return StartedCase(item.patient_id, NOT_STARTED_T_INDEX, StartOutcome.ACTIVATED)


def _append(
    conn: sqlite3.Connection,
    session_id: str,
    clinician_id: str,
    patient_id: str,
    kind: events.EventKind,
    payload: dict[str, Any],
) -> None:
    events.append(
        conn,
        session_id=session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=None,
        kind=kind,
        payload=payload,
        commit=False,
    )


def _next_item(conn: sqlite3.Connection, schedule: GeneratedSchedule) -> ScheduleItem | None:
    """First item in ``case_position`` order not yet activated."""
    consumed = arm_assignments.activated_positions(conn, schedule.schedule_id)
    return next((i for i in schedule.items if i.case_position not in consumed), None)


def _resume_t_index(
    conn: sqlite3.Connection, clinician_id: str, assignment: ActivatedAssignment
) -> int:
    row = progress.fetch(conn, clinician_id=clinician_id, patient_id=assignment.patient_id)
    return NOT_STARTED_T_INDEX if row is None else row.unlocked_t_index


def _resumed(
    conn: sqlite3.Connection, clinician_id: str, assignment: ActivatedAssignment
) -> StartedCase:
    return StartedCase(
        assignment.patient_id,
        _resume_t_index(conn, clinician_id, assignment),
        StartOutcome.RESUMED,
    )
