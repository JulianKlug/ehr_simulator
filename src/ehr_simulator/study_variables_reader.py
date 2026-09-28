"""S11l read side: gather one measured case's rows and derive its variables.

Service layer between the DAOs and the pure ``study_variables`` module
(no ``web/`` import). Everything is read inside one ``BEGIN … ROLLBACK``
snapshot, so a concurrent write cannot tear the inputs; nothing is ever
committed.
"""

from __future__ import annotations

import sqlite3

from ehr_simulator.config.snapshot import parse_questions_snapshot, parse_study_snapshot
from ehr_simulator.db import answers, arm_assignments, config_history, progress, telemetry
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db.arm_assignments import ARM_SOURCE_PHASE2
from ehr_simulator.study_variables import (
    AnswerCell,
    CaseInputs,
    CaseVariables,
    derive_case_variables,
)

__all__ = ["load_case_inputs", "load_case_variables"]

FIRST_T_INDEX = 0


def load_case_inputs(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> CaseInputs | None:
    """The inputs of one measured case, or ``None`` when the pair is not a
    ``phase2_randomized`` case (practice and Phase 1 are not observations).

    Callers own the read transaction (see :func:`load_case_variables`).
    """
    assignment = arm_assignments.fetch_activated_for_pair(conn, clinician_id, patient_id)
    if assignment is None or assignment.arm_source != ARM_SOURCE_PHASE2:
        return None

    history = config_history.fetch_version(conn, assignment.config_version or "")
    if history is None:
        return None
    study = parse_study_snapshot(history.study_json)
    questions = parse_questions_snapshot(history.questions_json)
    timepoints = tuple(float(t) for t in study.timepoints_minutes)
    t_index_of = {t: i for i, t in enumerate(timepoints)}

    lifecycle = lifecycle_dao.fetch(conn, clinician_id, patient_id)
    walk = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    cells = {
        (t_index_of[row.timepoint], row.question_id): AnswerCell(
            value=row.value, answer_source=row.answer_source, arm=row.arm
        )
        for row in answers.fetch_for_pair(conn, clinician_id, patient_id)
        if row.timepoint in t_index_of
    }
    return CaseInputs(
        study_id=study.study_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        arm=assignment.arm,
        config_version=assignment.config_version,
        config_hash=assignment.config_hash,
        timepoints=timepoints,
        questions=questions,
        telemetry=study.telemetry,
        case_state=lifecycle.state if lifecycle is not None else None,
        incomplete_reason=(
            str(lifecycle.incomplete_reason)
            if lifecycle is not None and lifecycle.incomplete_reason is not None
            else None
        ),
        # No progress row yet = a started case still at its first timepoint.
        unlocked_t_index=walk.unlocked_t_index if walk is not None else FIRST_T_INDEX,
        completed=walk is not None and walk.completed_at is not None,
        enter_t_indices=telemetry.enter_t_indices(conn, clinician_id, patient_id),
        answers=cells,
        renders=tuple(telemetry.load_render_rows(conn, clinician_id, patient_id)),
        telemetry_rows=tuple(telemetry.load_telemetry_rows(conn, clinician_id, patient_id)),
        session_hashes=telemetry.session_config_hashes(conn, clinician_id, patient_id),
    )


def load_case_variables(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> CaseVariables | None:
    """Derive one measured case's S11l variables from a read-only snapshot."""
    conn.execute("BEGIN")
    try:
        inputs = load_case_inputs(conn, clinician_id, patient_id)
    finally:
        conn.rollback()
    return None if inputs is None else derive_case_variables(inputs)
