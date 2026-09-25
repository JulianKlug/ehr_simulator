"""S11d: explicit Start case — the only path that realises a Phase 2 allocation.

::

    POST /case/start
        │
        ▼
    start_next_case ── open case? ── timed out ──► incomplete (own commit)
        │ none          └─ still open ──► resume (touch only)
        ▼
    clinician limit reached? ──► ClinicianLimitReachedError   (fast path)
        │
        ▼
    Phase A   stale-server check
              create_or_fetch_schedule          own commit; planned only,
              (allocation state read in lock)   consumes nothing
              plan missing replacements         S11f; own commits
        │
        ▼
    Phase B   BEGIN IMMEDIATE
              open case? (a racing Start won) ──► rollback, resume
              stale-server check
              clinician limit reached? ──► ClinicianLimitReachedError
              pending replacement? ── else first unactivated, unreserved item
                                    ── none ──► ScheduleExhaustedError
              schedule ↔ active config compatible
              activate assignment + lifecycle "active" + session
              + replacement activated_at + case.activated + session.start
              COMMIT                            rollback on any failure

S11e: an **open case** is a ``phase2_randomized`` assignment whose lifecycle
state is ``active`` or ``paused`` (a row missing lifecycle state falls back
to "progress not completed"). Completed and incomplete cases are closed; a
paused case still blocks Start case and is resumed only by an explicit
``POST /case/{pid}/resume``. Limits come from the **active** configuration.
S11f: a pending replacement plan is activated before the next ordinary item,
through the same checks; its reserved position is never taken otherwise.
Nothing here returns or renders the arm to the caller.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ehr_simulator.case_lifecycle import limit_reached
from ehr_simulator.db import arm_assignments, events, progress, sessions
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import randomisation as schedules
from ehr_simulator.db import replacements as replacements_dao
from ehr_simulator.db.arm_assignments import ARM_SOURCE_PHASE2, ActivatedAssignment
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.exceptions import CaseActivationError, ConfigurationProvenanceError
from ehr_simulator.db.randomisation import GeneratedSchedule, ScheduleItem
from ehr_simulator.db.replacements import ReplacementPlan
from ehr_simulator.logging import get_logger
from ehr_simulator.randomisation import (
    create_or_fetch_schedule,
    load_activated_allocation_state,
    require_schedule_compatible,
)
from ehr_simulator.replacement import plan_missing_replacements
from ehr_simulator.web import case_contact
from ehr_simulator.web.study_session import (
    NOT_STARTED_T_INDEX,
    is_phase2_mode,
    require_active_case,
    resolve_case_configuration,
)


class CaseStartRefusedError(Exception):
    """Start case refused without any activation write (route: 409)."""


class NotPhase2StudyError(CaseStartRefusedError):
    """The server is not in Phase 2 study mode."""


class ScheduleExhaustedError(CaseStartRefusedError):
    """Every schedule item is already activated; no further cases."""


class ClinicianLimitReachedError(CaseStartRefusedError):
    """The active configuration's per-clinician case limit is reached (S11e)."""


class StartOutcome(StrEnum):
    ACTIVATED = "activated"
    RESUMED = "resumed"


class IndexAction(StrEnum):
    START = "start"
    RESUME = "resume"
    RESUME_PAUSED = "resume_paused"
    LIMIT_REACHED = "limit_reached"
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


class ReplacementMarker(StrEnum):
    PENDING = "pending"
    REPLACED = "replaced"
    NONE = "none"


def replacement_markers(conn: sqlite3.Connection, clinician_id: str) -> dict[str, str]:
    """Per incomplete case: replacement pending / replaced / none (S11f index)."""
    plans = {
        p.original_patient_id: p for p in replacements_dao.list_for_clinician(conn, clinician_id)
    }
    markers: dict[str, str] = {}
    for pid, row in lifecycle_dao.list_for_clinician(conn, clinician_id).items():
        if row.state is not CaseState.INCOMPLETE:
            continue
        plan = plans.get(pid)
        if plan is None:
            markers[pid] = ReplacementMarker.NONE
        else:
            markers[pid] = (
                ReplacementMarker.PENDING if plan.is_pending else ReplacementMarker.REPLACED
            )
    return markers


def case_states(conn: sqlite3.Connection, clinician_id: str) -> dict[str, str]:
    """Lifecycle state per realised case (index markers, S11e)."""
    return {
        pid: str(row.state)
        for pid, row in lifecycle_dao.list_for_clinician(conn, clinician_id).items()
    }


def find_open_case(conn: sqlite3.Connection, clinician_id: str) -> ActivatedAssignment | None:
    """The clinician's active or paused Phase 2 case, if any (S11e)."""
    states = lifecycle_dao.list_for_clinician(conn, clinician_id)
    rows = progress.list_for_clinician(conn, clinician_id)
    for assignment in arm_assignments.list_for_clinician(conn, clinician_id):
        if assignment.arm_source != ARM_SOURCE_PHASE2:
            continue

        lifecycle = states.get(assignment.patient_id)
        if lifecycle is not None:
            if lifecycle.is_open:
                return assignment
            continue

        # Migration 8 backfills every realised case; progress decides only
        # for a row written outside the activation path.
        row = rows.get(assignment.patient_id)
        if row is None or row.completed_at is None:
            return assignment

    return None


def _limit_reached(conn: sqlite3.Connection, app_state: Any, clinician_id: str) -> bool:
    counts = lifecycle_dao.counts_for_clinician(conn, clinician_id)
    return limit_reached(counts, app_state.study.case_lifecycle)


def _refuse_at_limit(conn: sqlite3.Connection, app_state: Any, clinician_id: str) -> None:
    if _limit_reached(conn, app_state, clinician_id):
        raise ClinicianLimitReachedError("no further cases can be started")


def _open_case_after_timeouts(
    conn: sqlite3.Connection, app_state: Any, clinician_id: str
) -> tuple[ActivatedAssignment, case_contact.ContactResult] | None:
    """The open case once the lazy timeout had its say; a timed-out one closes."""
    opened = find_open_case(conn, clinician_id)
    if opened is None:
        return None

    pinned = resolve_case_configuration(
        conn, app_state, clinician_id=clinician_id, patient_id=opened.patient_id
    )
    contact = case_contact.check(
        conn, app_state, clinician_id=clinician_id, patient_id=opened.patient_id, case=pinned
    )
    if contact.access is case_contact.CaseAccess.INCOMPLETE:
        return None

    return opened, contact


def index_state(conn: sqlite3.Connection, app_state: Any, clinician_id: str) -> CaseIndexState:
    """Which case action the study index offers. Pure read: never creates a schedule."""
    opened = find_open_case(conn, clinician_id)
    if opened is not None:
        lifecycle = lifecycle_dao.fetch(conn, clinician_id, opened.patient_id)
        paused = lifecycle is not None and lifecycle.state is CaseState.PAUSED
        return CaseIndexState(
            IndexAction.RESUME_PAUSED if paused else IndexAction.RESUME,
            opened.patient_id,
            _resume_t_index(conn, clinician_id, opened),
        )

    if _limit_reached(conn, app_state, clinician_id):
        return CaseIndexState(IndexAction.LIMIT_REACHED)

    stored = schedules.fetch_for_clinician(conn, app_state.study.study_id, clinician_id)
    if stored is not None and _next_activation(conn, stored.schedule, clinician_id) is None:
        return CaseIndexState(IndexAction.EXHAUSTED)

    return CaseIndexState(IndexAction.START)


def start_next_case(conn: sqlite3.Connection, app_state: Any, *, clinician_id: str) -> StartedCase:
    """Resume the open case or activate the next planned one (see module diagram).

    Raises:
        NotPhase2StudyError / ScheduleExhaustedError: refused, nothing written.
        ClinicianLimitReachedError: the active per-clinician limit is reached.
        StaleConfigurationError: DB active configuration ≠ the running server.
        ScheduleIncompatibleError: the schedule no longer fits the active config.
        ConfigurationProvenanceError / RandomisationIntegrityError: integrity.
    """
    if not is_phase2_mode(app_state):
        raise NotPhase2StudyError("Start case needs a study with randomisation settings")

    held = _open_case_after_timeouts(conn, app_state, clinician_id)
    if held is not None:
        opened, contact = held
        case_contact.touch(conn, app_state, contact)
        return _resumed(conn, clinician_id, opened)

    _refuse_at_limit(conn, app_state, clinician_id)

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
    plan_missing_replacements(conn, clinician_id=clinician_id, now=case_contact.now(app_state))
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
        _refuse_at_limit(conn, app_state, clinician_id)
        upcoming = _next_activation(conn, schedule, clinician_id)
        if upcoming is None:
            raise ScheduleExhaustedError("no further cases: every scheduled case is activated")

        item, plan = upcoming
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
        lifecycle_dao.insert_active(
            conn,
            clinician_id=clinician_id,
            patient_id=item.patient_id,
            now=case_contact.now(app_state),
            commit=False,
        )
        activated_payload: dict[str, Any] = {
            "schedule_id": schedule.schedule_id,
            "case_position": item.case_position,
            "arm": assignment.arm,
        }
        if plan is not None:
            replacements_dao.mark_activated(
                conn, plan.replacement_id, now=case_contact.now(app_state), commit=False
            )
            activated_payload["replacement_id"] = plan.replacement_id
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
            activated_payload,
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


def _next_activation(
    conn: sqlite3.Connection, schedule: GeneratedSchedule, clinician_id: str
) -> tuple[ScheduleItem, ReplacementPlan | None] | None:
    """The oldest pending replacement, else the first unactivated, unreserved item."""
    by_position = {i.case_position: i for i in schedule.items}
    pending = [
        p
        for p in replacements_dao.pending_for_clinician(conn, clinician_id)
        if p.replacement_schedule_id == schedule.schedule_id
    ]
    if pending:
        plan = pending[0]
        return by_position[plan.replacement_case_position], plan

    consumed = arm_assignments.activated_positions(conn, schedule.schedule_id)
    item = next((i for i in schedule.items if i.case_position not in consumed), None)
    return None if item is None else (item, None)


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
