"""Per-(clinician, patient) session bootstrap + walk frontier (S6 §7.3, S9a,
S9b, S11b).

A study "session" is the window during which one clinician walks one
patient's timepoints. S9a opens it lazily on first patient contact; S9b's
final ``/advance`` closes it (``ended_at``).

Two halves, split on purpose (S9b review-fix R21)::

    read_frontier(conn, app_state, clinician_id, patient_id)   PURE READ
        └─ progress.fetch → Frontier(unlocked_t_index, completed)
           (clamped to the study's last index; drift → WARNING)

    bootstrap_session(conn, app_state, clinician_id, patient_id,
                      frontier, case)                           WRITES
        ├─ arm_assignments.assign_or_lookup   (locks the arm; stub → no_ai)
        ├─ sessions.find_open                 (resume?)
        │     ├─ None + completed → sessions.find_latest (re-point, no insert)
        │     └─ None → sessions.start_or_resume + events "session.start"
        └─ SessionContext(session_id, arm, config_hash, frontier)

S11b pins every case to its configuration activation:

    CaseConfiguration
        = (config_version, config_hash, study, questions)

    resolve_case_configuration(conn, app_state, clinician_id, patient_id)
        existing assignment → that version's immutable snapshots
        no assignment yet   → the active configuration (stale-server guard:
                              the DB's active version/hash must still equal
                              app.state, or a new case creation is refused)

A "new case" is created only when the pair has no assignment row; every
write in ``bootstrap_session`` carries the case's ``config_version`` +
``config_hash``, and an existing session row whose provenance disagrees with
the case is a :class:`ConfigurationProvenanceError` (integrity, not a
fallback).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ehr_simulator.config.questions import Questions
from ehr_simulator.config.snapshot import (
    parse_questions_snapshot,
    parse_study_snapshot,
)
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db import arm_assignments, config_history, events, progress, sessions
from ehr_simulator.db.exceptions import (
    ConfigurationProvenanceError,
    StaleConfigurationError,
)
from ehr_simulator.logging import get_logger

NOT_STARTED_T_INDEX = 0


@dataclass(frozen=True)
class Frontier:
    unlocked_t_index: int
    completed: bool


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    arm: str
    config_hash: str
    config_version: str | None = None
    frontier: Frontier = Frontier(unlocked_t_index=NOT_STARTED_T_INDEX, completed=False)


@dataclass(frozen=True)
class CaseConfiguration:
    """The immutable configuration a case runs under (S11b).

    ``config_version is None`` marks a legacy S11a case (activation history
    does not exist yet); its snapshot is the boot-time supplied models.
    """

    config_version: str | None
    config_hash: str
    study: StudyConfig
    questions: Questions

    @property
    def timepoints(self) -> tuple[float, ...]:
        return tuple(self.study.timepoints_minutes)

    @property
    def patient_ids(self) -> tuple[str, ...]:
        return tuple(self.study.patient_ids)


def _legacy_or_raise(app_state: Any) -> tuple[StudyConfig, Questions]:
    study = getattr(app_state, "study", None)
    questions = getattr(app_state, "questions", None)
    if not isinstance(study, StudyConfig) or not isinstance(questions, Questions):
        raise ConfigurationProvenanceError(
            "cannot resolve the case configuration: the app is not in study mode"
        )
    return study, questions


def case_from_history_row(row: object) -> tuple[StudyConfig, Questions]:
    """Parse one history row's stored snapshots back into the strict models.

    Raises:
        ConfigValidationError: snapshot does not re-validate / re-render —
            a stored-state integrity failure, never a fallback.
    """
    return parse_study_snapshot(row.study_json), parse_questions_snapshot(row.questions_json)  # type: ignore[union-attr]


def _resolve_existing_case(
    conn: sqlite3.Connection,
    app_state: Any,
    clinician_id: str,
    patient_id: str,
) -> CaseConfiguration:
    """The case a pair already holds: its version's pinned snapshots."""
    assignment = arm_assignments.fetch_for_pair(conn, clinician_id, patient_id)
    assert assignment is not None  # guarded by the caller
    if assignment.config_version is None:
        if config_history.has_any(conn):
            # S11b upgrade rule: once activation history exists, a provenance
            # row without a version is an integrity error. Do not guess.
            raise ConfigurationProvenanceError(
                "assignment provenance is missing config_version while a "
                "configuration history exists "
                f"(clinician={clinician_id}, patient={patient_id})"
            )
        live_hash = getattr(app_state, "config_hash", None)
        if not isinstance(live_hash, str):
            raise ConfigurationProvenanceError(
                "legacy case cannot be resolved without a study-mode config "
                f"(clinician={clinician_id}, patient={patient_id})"
            )
        study, questions = _legacy_or_raise(app_state)
        return CaseConfiguration(
            config_version=None,
            config_hash=assignment.config_hash,
            study=study,
            questions=questions,
        )
    row = config_history.require_known(conn, assignment.config_version, assignment.config_hash)
    study, questions = case_from_history_row(row)
    return CaseConfiguration(
        config_version=assignment.config_version,
        config_hash=assignment.config_hash,
        study=study,
        questions=questions,
    )


def _active_case_or_raise(conn: sqlite3.Connection, app_state: Any) -> CaseConfiguration:
    """The currently active configuration — the one new cases will pin.

    Stale-server guard: if the database's active version/hash no longer
    equals ``app.state`` (an activation happened out-of-band), a new case
    creation is refused and a restart is required. Existing pinned cases
    are unaffected (they resolve through ``_resolve_existing_case``).
    """
    live_version = getattr(app_state, "config_version", None)
    live_hash = getattr(app_state, "config_hash", None)
    study, questions = _legacy_or_raise(app_state)
    if not isinstance(live_hash, str):
        # Non study mode / legacy boot: no activation history to pin to.
        return CaseConfiguration(
            config_version=None,
            config_hash=live_hash if isinstance(live_hash, str) else "",
            study=study,
            questions=questions,
        )
    if live_version is None:
        active = config_history.fetch_active(conn)
        if active is None:
            # No history recorded yet: this is a legacy (pre-activation)
            # boot; the supplied models are the configuration.
            return CaseConfiguration(
                config_version=None, config_hash=live_hash, study=study, questions=questions
            )
        # History exists but the runtime never validated one: stale.
        raise StaleConfigurationError(
            "the running server has no validated active configuration while the "
            "database holds one; restart the application"
        )
    active = config_history.fetch_active(conn)
    if active is None:
        raise StaleConfigurationError(
            "the database has no active configuration but the runtime believes "
            "one is active; restart the application"
        )
    if active.config_version != live_version or active.config_hash != live_hash:
        raise StaleConfigurationError(
            "the database active configuration "
            f"({active.config_version}/{active.config_hash[:12]}...) differs from "
            f"the running server ({live_version}/{live_hash[:12]}...); "
            "refusing to create a new case — restart the application"
        )
    return CaseConfiguration(
        config_version=active.config_version,
        config_hash=active.config_hash,
        study=study,
        questions=questions,
    )


def resolve_case_configuration(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
) -> CaseConfiguration:
    """Resolve the immutable configuration for a (clinician, patient) case.

    Existing assignments use their stored version/hash snapshots forever;
    unassigned pairs pin the still-active configuration (stale-server guard
    included). See the module docstring for the failure modes.
    """
    if arm_assignments.fetch_for_pair(conn, clinician_id, patient_id) is not None:
        return _resolve_existing_case(conn, app_state, clinician_id, patient_id)
    return _active_case_or_raise(conn, app_state)


def read_frontier(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    timepoints: Sequence[float] | None = None,
) -> Frontier:
    """The pair's walk frontier; a missing row is "not started".

    Two WARNING-only guards: a frontier past the case's last index (the
    timepoint list changed under a live DB) is clamped, and a row recorded
    under a different ``config_hash`` is reported. Neither can 500.
    """
    row = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    if row is None:
        return Frontier(unlocked_t_index=NOT_STARTED_T_INDEX, completed=False)

    unlocked = row.unlocked_t_index
    # The test stub has no study_timepoints; skip the clamp rather than crash.
    if timepoints is None:
        timepoints = getattr(app_state, "study_timepoints", None)
    if timepoints:
        last = len(timepoints) - 1
        if unlocked > last:
            get_logger().warning(
                "progress beyond study timepoints; clamped",
                event_kind="progress.clamped",
                unlocked_t_index=unlocked,
                last_t_index=last,
            )
            unlocked = last

    live_hash: str | None = getattr(app_state, "config_hash", None)
    if live_hash is not None and row.config_hash != live_hash:
        get_logger().warning(
            "progress recorded under a different config",
            event_kind="progress.config_hash.drift",
            row_config_hash=row.config_hash,
            live_config_hash=live_hash,
        )

    return Frontier(unlocked_t_index=unlocked, completed=row.completed_at is not None)


def bootstrap_session(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    frontier: Frontier,
    case: CaseConfiguration | None = None,
) -> SessionContext:
    """Return the pair's session, creating it (once) if needed.

    S11b: when ``case`` is supplied it is the case's pinned configuration —
    the assignment, session and progress rows all carry its
    version/hash. A stored session row whose provenance disagrees with the
    case is a :class:`ConfigurationProvenanceError`; progress provenance is
    pinned the same way on creation and never touched afterwards.

    A completed walk re-points at its last (closed) session instead of
    opening one nothing could ever close (review-fix R12).
    """
    if case is None:
        case = resolve_case_configuration(
            conn, app_state, clinician_id=clinician_id, patient_id=patient_id
        )
    config_hash = case.config_hash
    config_version = case.config_version
    arm, _source = arm_assignments.assign_or_lookup(
        conn, clinician_id, patient_id, config_hash=config_hash, config_version=config_version
    )

    session_id = sessions.find_open(conn, clinician_id, patient_id)
    is_new_session = session_id is None
    if is_new_session and frontier.completed:
        session_id = sessions.find_latest(conn, clinician_id, patient_id)
        is_new_session = False
    if session_id is not None:
        stored = sessions.fetch_for_pair(conn, clinician_id, patient_id)
        if stored is not None and (
            stored.config_hash != config_hash or stored.config_version != config_version
        ):
            raise ConfigurationProvenanceError(
                "session provenance disagrees with the case configuration "
                f"(clinician={clinician_id}, patient={patient_id})"
            )
    else:
        session_id = sessions.start_or_resume(
            conn,
            clinician_id,
            patient_id,
            arm=arm,
            config_hash=config_hash,
            config_version=config_version,
        )

    prog = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    if prog is not None and (
        prog.config_hash != config_hash or prog.config_version != config_version
    ):
        raise ConfigurationProvenanceError(
            "progress provenance disagrees with the case configuration "
            f"(clinician={clinician_id}, patient={patient_id})"
        )

    if is_new_session:
        events.append(
            conn,
            session_id=session_id,
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoint=None,
            kind="session.start",
            payload={"arm": arm},
            app_state=app_state,
        )

    return SessionContext(
        session_id=session_id,
        arm=arm,
        config_hash=config_hash,
        config_version=config_version,
        frontier=frontier,
    )
