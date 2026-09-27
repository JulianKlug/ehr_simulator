"""S11i: practice cases — a separate entry point that never touches allocation.

::

    POST /practice/start
        │
        ▼
    practice enabled in the ACTIVE configuration? ── no ──► PracticeRefusedError
        │
        ▼
    BEGIN IMMEDIATE
        open practice case? ──► rollback, resume it
        stale-server check (require_active_case)
        next configured practice patient not yet started ── none ──► refused
        practice_cases row + session (observation_mode=practice)
        + practice.started + session.start
    COMMIT                                             rollback on any failure

No schedule item, ``arm_assignments`` row, lifecycle row or replacement is
ever written, so measured balance, limits and ITT/PP never see practice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ehr_simulator.db import events, practice, progress, sessions
from ehr_simulator.db.exceptions import ConfigurationProvenanceError
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.web import case_contact
from ehr_simulator.web.study_session import (
    NOT_STARTED_T_INDEX,
    is_phase2_mode,
    require_active_case,
)


class PracticeRefusedError(Exception):
    """Practice start refused without any write (route: 409)."""


class PracticeAction(StrEnum):
    START = "start"
    RESUME = "resume"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class PracticeEntry:
    patient_id: str
    completed: bool
    resume_t_index: int


@dataclass(frozen=True)
class PracticeIndexState:
    action: PracticeAction
    entries: tuple[PracticeEntry, ...]
    open_patient_id: str | None = None
    resume_t_index: int = NOT_STARTED_T_INDEX


@dataclass(frozen=True)
class StartedPractice:
    patient_id: str
    resume_t_index: int


def _practice_ids(app_state: Any) -> tuple[str, ...]:
    if not is_phase2_mode(app_state):
        return ()
    return app_state.study.practice_patient_ids


def _resume_t_index(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> int:
    row = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    return NOT_STARTED_T_INDEX if row is None else row.unlocked_t_index


def practice_index_state(
    conn: sqlite3.Connection, app_state: Any, clinician_id: str
) -> PracticeIndexState | None:
    """The index's practice section; ``None`` when the active config has none. Pure read."""
    configured = _practice_ids(app_state)
    if not configured:
        return None

    cases = practice.list_for_clinician(conn, clinician_id)
    entries = tuple(
        PracticeEntry(
            c.patient_id,
            completed=not c.is_open,
            resume_t_index=_resume_t_index(conn, clinician_id, c.patient_id),
        )
        for c in cases
    )
    opened = next((e for e in entries if not e.completed), None)
    if opened is not None:
        return PracticeIndexState(
            PracticeAction.RESUME, entries, opened.patient_id, opened.resume_t_index
        )

    started = {c.patient_id for c in cases}
    if any(pid not in started for pid in configured):
        return PracticeIndexState(PracticeAction.START, entries)
    return PracticeIndexState(PracticeAction.EXHAUSTED, entries)


def start_practice_case(
    conn: sqlite3.Connection, app_state: Any, *, clinician_id: str
) -> StartedPractice:
    """Resume the open practice case or start the next configured one.

    Raises:
        PracticeRefusedError: practice disabled or every practice patient used.
        StaleConfigurationError: DB active configuration ≠ the running server.
    """
    configured = _practice_ids(app_state)
    if not configured:
        raise PracticeRefusedError("Practice cases are not enabled for this study")

    conn.execute("BEGIN IMMEDIATE")
    try:
        cases = practice.list_for_clinician(conn, clinician_id)
        opened = next((c for c in cases if c.is_open), None)
        if opened is not None:
            conn.rollback()
            return StartedPractice(
                opened.patient_id, _resume_t_index(conn, clinician_id, opened.patient_id)
            )

        active = require_active_case(conn, app_state)
        if active.config_version is None:
            raise ConfigurationProvenanceError(
                "practice cases require an activated configuration; run activate-config first"
            )
        started = {c.patient_id for c in cases}
        patient_id = next((pid for pid in configured if pid not in started), None)
        if patient_id is None:
            raise PracticeRefusedError("No further practice cases are available")

        arm = app_state.study.study_behaviour.practice.arm
        practice.start(
            conn,
            clinician_id=clinician_id,
            patient_id=patient_id,
            arm=arm,
            config_version=active.config_version,
            config_hash=active.config_hash,
            now=case_contact.now(app_state),
        )
        session_id = sessions.start_or_resume(
            conn,
            clinician_id,
            patient_id,
            arm=arm,
            config_hash=active.config_hash,
            config_version=active.config_version,
            commit=False,
            observation_mode=ObservationMode.PRACTICE,
        )
        for kind in ("practice.started", "session.start"):
            events.append(
                conn,
                session_id=session_id,
                clinician_id=clinician_id,
                patient_id=patient_id,
                timepoint=None,
                kind=kind,
                payload={"arm": arm},
                app_state=app_state,
                commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return StartedPractice(patient_id, NOT_STARTED_T_INDEX)
