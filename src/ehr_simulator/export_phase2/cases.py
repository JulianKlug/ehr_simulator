"""S11n one measured case: validated rows plus every derived research value."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ehr_simulator import timing
from ehr_simulator.answer_codec import AnswerValidationError, decode_stored_answer
from ehr_simulator.behavioral_timing import (
    ObservationTiming,
    build_timelines,
    derive_observation_timings,
)
from ehr_simulator.db import answers, progress, sessions
from ehr_simulator.db.arm_assignments import ActivatedAssignment
from ehr_simulator.db.case_lifecycle import CaseLifecycle
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.db.telemetry import RenderRow
from ehr_simulator.export_phase2.cells import _minutes, _ts
from ehr_simulator.export_phase2.model import Phase2ExportError
from ehr_simulator.export_phase2.registry import PinnedConfig, _pinned, _same_provenance
from ehr_simulator.panel_exposure import PanelSummary, derive_panel_summaries
from ehr_simulator.study_variables import CaseVariables, derive_case_variables
from ehr_simulator.study_variables_reader import load_case_inputs


@dataclass
class CaseRecord:
    assignment: ActivatedAssignment
    pinned: PinnedConfig
    lifecycle: CaseLifecycle | None
    variables: CaseVariables
    renders: tuple[RenderRow, ...]
    conflict_renders: tuple[RenderRow, ...]
    telemetry_rows: tuple[Any, ...]
    timings: dict[tuple[int, str], ObservationTiming]
    panels: dict[tuple[int, str, str], PanelSummary]
    s10: dict[float, timing.TimepointTiming]
    answer_rows: dict[tuple[int, str], answers.AnswerRow]
    recorded_at: dict[tuple[float, str], object]
    sessions: dict[str, sessions.SessionRow] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str]:
        return (self.assignment.clinician_id, self.assignment.patient_id)

    @property
    def sort_key(self) -> tuple[str, str, str]:
        return (self.assignment.clinician_id, _ts(self.assignment.activated_at), self.key[1])


def _answer_rows_by_cell(
    rows: Iterable[answers.AnswerRow], pinned: PinnedConfig, where: str
) -> dict[tuple[int, str], answers.AnswerRow]:
    t_index_of = {t: i for i, t in enumerate(pinned.timepoints)}
    by_id = {q.question_id: q for q in pinned.questions.questions}
    out: dict[tuple[int, str], answers.AnswerRow] = {}
    for row in rows:
        cell = f"{where}, timepoint {row.timepoint}, question {row.question_id!r}"
        if row.timepoint not in t_index_of:
            raise Phase2ExportError(f"answer of {cell} is at a timepoint the pinned config lacks")
        question = by_id.get(row.question_id)
        if question is None:
            raise Phase2ExportError(f"answer of {cell} names a question the pinned config lacks")
        try:
            decode_stored_answer(question, row.value)
        except AnswerValidationError as exc:
            raise Phase2ExportError(f"invalid persisted answer of {cell}: {exc}") from None
        out[(t_index_of[row.timepoint], row.question_id)] = row
    return out


def _case_record(
    conn: sqlite3.Connection,
    assignment: ActivatedAssignment,
    registry: Mapping[str, PinnedConfig],
    lifecycle: CaseLifecycle | None,
    case_sessions: Sequence[sessions.SessionRow],
    timing_events: Sequence[timing.TimingEvent],
) -> CaseRecord:
    where = f"case of patient {assignment.patient_id!r}, clinician {assignment.clinician_id}"
    pinned = _pinned(registry, assignment.config_version, assignment.config_hash, where)

    for session in case_sessions:
        _same_provenance(
            assignment, session.config_version, session.config_hash, f"session of {where}"
        )
    walk = progress.fetch(
        conn, clinician_id=assignment.clinician_id, patient_id=assignment.patient_id
    )
    if walk is not None:
        _same_provenance(assignment, walk.config_version, walk.config_hash, f"progress of {where}")

    rows = [
        r
        for r in answers.fetch_for_pair(conn, assignment.clinician_id, assignment.patient_id)
        if r.observation_mode == ObservationMode.MEASURED
    ]
    for row in rows:
        _same_provenance(assignment, row.config_version, row.config_hash, f"answer of {where}")
    answer_rows = _answer_rows_by_cell(rows, pinned, where)

    inputs = load_case_inputs(conn, assignment.clinician_id, assignment.patient_id)
    if inputs is None:  # unreachable after the checks above
        raise Phase2ExportError(f"{where} cannot be interpreted")
    # One timeline per render, shared by every derivation below.
    timelines = build_timelines(inputs.renders, inputs.telemetry_rows)
    variables = derive_case_variables(inputs, timelines)

    config = pinned.study.telemetry
    timings: dict[tuple[int, str], ObservationTiming] = {}
    panels: dict[tuple[int, str, str], PanelSummary] = {}
    if config is not None:
        timings = derive_observation_timings(
            inputs.renders,
            inputs.telemetry_rows,
            inactivity_threshold_seconds=config.inactivity_threshold_seconds,
            tab_audit=inputs.tab_audit,
            timelines=timelines,
        )
        panels = derive_panel_summaries(
            inputs.renders,
            inputs.telemetry_rows,
            viewport_threshold=config.panel_viewport_threshold,
            viewed_threshold_seconds=config.panel_viewed_threshold_seconds,
            tab_audit=inputs.tab_audit,
            timelines=timelines,
        )

    try:
        s10 = timing.derive_timepoint_timings(
            timing_events, clinician_id=assignment.clinician_id, patient_id=assignment.patient_id
        )
    except timing.TimingError as exc:
        raise Phase2ExportError(f"cannot derive timepoint timing of {where}: {exc}") from exc

    return CaseRecord(
        assignment=assignment,
        pinned=pinned,
        lifecycle=lifecycle,
        variables=variables,
        renders=tuple(inputs.renders),
        conflict_renders=tuple(inputs.conflict_renders),
        telemetry_rows=tuple(inputs.telemetry_rows),
        timings=timings,
        panels=panels,
        s10=s10,
        answer_rows=answer_rows,
        recorded_at=answers.fetch_recorded_at(conn, assignment.clinician_id, assignment.patient_id),
        sessions={s.session_id: s for s in case_sessions},
    )


def _keys(study_id: str, case: CaseRecord, t_index: int) -> tuple[str, ...]:
    a = case.assignment
    return (
        study_id,
        a.clinician_id,
        a.patient_id,
        str(t_index),
        _minutes(case.pinned.timepoints[t_index]),
        a.config_version or "",
        a.config_hash,
    )


def _conflict(case: CaseRecord, t_index: int, visit_kind: str | None = None) -> bool:
    return any(
        r.t_index == t_index and (visit_kind is None or r.visit_kind == visit_kind)
        for r in case.conflict_renders
    )
