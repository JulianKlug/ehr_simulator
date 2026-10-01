"""S11n linked Phase 2 research export: build and validate one bundle.

Spec: ``specs/session-11n-phase2-exports.md``.

Pure over one read snapshot: everything is read inside a single
``BEGIN … ROLLBACK``, validated, and returned as in-memory tables; file
output lives in ``export_bundle.py``. Every case is interpreted under the
configuration snapshot it was activated with, never the active one::

    configuration_history ─► registry {version: (hash, study, questions)}
    schedules ─┬─► randomisation_audit.csv  (planned vs realised, replacements)
    arm_assignments (phase2_randomized) ─► one CaseRecord per case
               │     S10 timing · S11j foreground/active · S11k panels
               │     S11l delivery / viewed / PP / missing responses
               ├─► timepoints.csv · answers.csv · panel_summaries.csv
               └─► behavioral_events.csv (raw source rows)
    configuration_history.csv · configuration_counts.csv · clinicians.csv (S11p)

The exporter derives nothing of its own: every research value comes from
``timing``, ``behavioral_timing``, ``panel_exposure`` or ``study_variables``.
Provenance and linkage failures raise :class:`Phase2ExportError`; S11l
integrity warnings are exported in ``integrity_warnings``. No ``web`` import.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ehr_simulator import timing
from ehr_simulator.answer_codec import (
    AnswerValidationError,
    decode_stored_answer,
    deserialize_answer,
)
from ehr_simulator.behavioral_timing import ObservationTiming, derive_observation_timings
from ehr_simulator.config.questions import Question, Questions
from ehr_simulator.config.snapshot import parse_questions_snapshot, parse_study_snapshot
from ehr_simulator.config.study import FreeTextExport, StudyConfig
from ehr_simulator.db import (
    answers,
    arm_assignments,
    clinician_profiles,
    clinicians,
    config_history,
    events,
    practice,
    progress,
    replacements,
    sessions,
    study_identity,
    telemetry,
)
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import randomisation as schedules_dao
from ehr_simulator.db.arm_assignments import ARM_AI, ActivatedAssignment
from ehr_simulator.db.case_lifecycle import CaseLifecycle, CaseState
from ehr_simulator.db.config_history import ConfigHistoryRow
from ehr_simulator.db.events import PROHIBITED_PAYLOAD_KEY, EventRow
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.db.practice import PracticeCase
from ehr_simulator.db.randomisation import ScheduleItem, StoredSchedule
from ehr_simulator.db.replacements import ReplacementPlan
from ehr_simulator.db.telemetry import RenderRow
from ehr_simulator.panel_exposure import PANEL_IDS, PanelSummary, derive_panel_summaries
from ehr_simulator.question_branching import AnswerSource, QuestionState, evaluate
from ehr_simulator.study_variables import (
    AI_PANEL,
    CaseVariables,
    ResponseProvenance,
    ResponseStatus,
    derive_case_variables,
)
from ehr_simulator.study_variables_reader import load_case_inputs

__all__ = [
    "EXPORT_SCHEMA_VERSION",
    "KeyfileRequest",
    "Phase2Bundle",
    "Phase2ExportError",
    "PracticeExport",
    "Table",
    "build_phase2_bundle",
]

EXPORT_SCHEMA_VERSION = "phase2_export_v1"

#: Event kinds carried by ``behavioral_events.csv`` (operational identity,
#: operator maintenance and practice events are excluded).
RESEARCH_EVENT_PREFIXES = (
    "answer.",
    "advance.",
    "timepoint.",
    "browser.",
    "panel.",
    "tab.",
    "case.",
    "session.",
)

PRIMARY = "primary"
REVISIT = "revisit"
NOT_CONFIGURED = "not_configured"
LIST_SEPARATOR = "|"
FREE_TEXT = "free-text"
TRUE = "true"
FALSE = "false"


class Phase2ExportError(ValueError):
    """The bundle cannot be produced faithfully; nothing is published.

    Messages name the failure shape and coordinates, never answer content
    or clinician names.
    """


class PracticeExport(StrEnum):
    EXCLUDE = "exclude"
    INCLUDE = "include"


class KeyfileRequest(StrEnum):
    NONE = "none"
    REQUESTED = "requested"


@dataclass(frozen=True)
class Table:
    """One bundle CSV: raw cells, guarded and quoted by the writer."""

    name: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Phase2Bundle:
    study_id: str
    source_schema_version: int
    config_versions: tuple[str, ...]
    practice_included: bool
    tables: tuple[Table, ...]
    clinician_ids: tuple[str, ...]
    keyfile_rows: tuple[tuple[str, str], ...] | None = None


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------

_TIMEPOINT_KEYS = (
    "study_id",
    "clinician_id",
    "patient_id",
    "t_index",
    "timepoint_minutes",
    "config_version",
    "config_hash",
)

TIMEPOINTS_HEADER = (
    *_TIMEPOINT_KEYS,
    "arm",
    "arm_source",
    "schedule_id",
    "case_position",
    "activated_at",
    "lifecycle_state",
    "case_completed_at",
    "case_incomplete_at",
    "incomplete_reason",
    "timepoint_reached",
    "timepoint_started_at",
    "timepoint_ended_at",
    "elapsed_seconds",
    "telemetry_status",
    "foreground_seconds",
    "active_seconds",
    "tab_conflict_detected",
    "ai_delivered",
    "ai_viewed",
    "ai_viewing_status",
    "ai_qualifying_seconds",
    "ai_episode_count",
    "intervention_failure",
    "intervention_failure_reasons",
    "intervention_leakage",
    "pp_compliant",
    "pp_determinate",
    "integrity_warnings",
)

ANSWERS_HEADER = (
    *_TIMEPOINT_KEYS,
    "question_id",
    "response_type",
    "branch_state",
    "required_now",
    "response_status",
    "response_value",
    "value_exported",
    "answer_source",
    "derived_from_question_id",
    "missing_reason",
    "ts_recorded",
)

PANELS_HEADER = (
    *_TIMEPOINT_KEYS,
    "visit_kind",
    "panel_id",
    "mounted",
    "telemetry_status",
    "qualifying_seconds",
    "viewed",
    "episode_count",
    "panel_open_count",
    "time_to_first_view_seconds",
    "first_view_client_ts",
    "last_view_client_ts",
    "tab_conflict_detected",
)

EVENTS_HEADER = (
    "study_id",
    "event_id",
    "session_id",
    "clinician_id",
    "patient_id",
    "timepoint",
    "t_index",
    "visit_kind",
    "config_version",
    "config_hash",
    "render_id",
    "tab_id",
    "kind",
    "client_ts",
    "server_ts",
    "client_seq",
    "client_mono_ms",
    "payload_json",
)

AUDIT_HEADER = (
    "study_id",
    "clinician_id",
    "schedule_id",
    "generation_config_version",
    "generation_config_hash",
    "generated_at",
    "algorithm_version",
    "master_seed",
    "derived_seed_hex",
    "allocation_state_json",
    "starting_ai_count",
    "starting_no_ai_count",
    "starting_arm",
    "block_length",
    "block_sequence_json",
    "case_position",
    "patient_id",
    "planned_arm",
    "block_number",
    "position_in_block",
    "preceding_block_arm",
    "planned_cases_since_ai",
    "assignment_seed",
    "activated",
    "activated_at",
    "activation_config_version",
    "activation_config_hash",
    "realised_arm",
    "lifecycle_state",
    "completed_at",
    "incomplete_at",
    "incomplete_reason",
    "replacement_id",
    "replaces_patient_id",
    "replacement_generated_at",
    "replacement_activated_at",
    "replaced_by_replacement_id",
    "replaced_by_patient_id",
)

HISTORY_HEADER = (
    "study_id",
    "config_version",
    "config_hash",
    "activated_at",
    "change_description",
    "change_reason",
    "study_json",
    "questions_json",
)

COUNTS_HEADER = (
    "study_id",
    "config_version",
    "config_hash",
    "scheduled_items_generated",
    "activated_cases",
    "completed_cases",
    "incomplete_cases",
    "open_cases",
    "expected_timepoints",
    "reached_primary_timepoints",
    "answer_rows_present",
)

PRACTICE_TIMEPOINTS_HEADER = (
    *_TIMEPOINT_KEYS,
    "observation_mode",
    "arm",
    "practice_started_at",
    "practice_completed_at",
    "timepoint_started_at",
    "timepoint_ended_at",
    "elapsed_seconds",
    "telemetry_status",
    "foreground_seconds",
    "active_seconds",
)

PRACTICE_ANSWERS_HEADER = (*ANSWERS_HEADER, "observation_mode")

#: S11p: one row per bundle clinician; never the name.
CLINICIANS_HEADER = (
    "study_id",
    "clinician_id",
    "profile_status",
    "professional_role",
    "years_of_practice",
    "country_of_practice",
    "primary_specialty",
)
PROFILE_COMPLETE = "complete"
PROFILE_MISSING = "missing"


# ---------------------------------------------------------------------------
# Cell formatting
# ---------------------------------------------------------------------------


def _bool(value: bool | None) -> str:
    if value is None:
        return ""
    return TRUE if value else FALSE


def _num(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("booleans are formatted with _bool")
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _minutes(value: float) -> str:
    return repr(float(value))


def _ts(value: object) -> str:
    """Stored timestamp → ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` (UTC)."""
    if value is None:
        return ""
    moment = value
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(moment, datetime):
        return str(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.isoformat() + "Z"


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _joined(values: Iterable[str]) -> str:
    return LIST_SEPARATOR.join(sorted(str(v) for v in values))


def _canonical_json(raw: str) -> str:
    return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Provenance registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PinnedConfig:
    row: ConfigHistoryRow
    study: StudyConfig
    questions: Questions

    @property
    def timepoints(self) -> tuple[float, ...]:
        return tuple(float(t) for t in self.study.timepoints_minutes)


def _registry(rows: Sequence[ConfigHistoryRow], study_id: str) -> dict[str, PinnedConfig]:
    registry: dict[str, PinnedConfig] = {}
    for row in rows:
        if row.study_id != study_id:
            raise Phase2ExportError(
                f"configuration {row.config_version!r} belongs to study {row.study_id!r}, "
                f"not {study_id!r}"
            )
        try:
            study = parse_study_snapshot(row.study_json)
            questions = parse_questions_snapshot(row.questions_json)
        except Exception as exc:  # noqa: BLE001 — any parse failure refuses
            raise Phase2ExportError(
                f"stored snapshot of configuration {row.config_version!r} no longer parses: "
                f"{type(exc).__name__}"
            ) from None
        registry[row.config_version] = PinnedConfig(row, study, questions)
    return registry


def _pinned(
    registry: Mapping[str, PinnedConfig],
    version: str | None,
    config_hash: str | None,
    where: str,
) -> PinnedConfig:
    if version is None:
        raise Phase2ExportError(f"{where} has no config_version")
    pinned = registry.get(version)
    if pinned is None:
        raise Phase2ExportError(f"{where} names unknown config_version {version!r}")
    if config_hash != pinned.row.config_hash:
        raise Phase2ExportError(
            f"{where} carries config_hash {str(config_hash)[:8]}… but version {version!r} "
            f"is registered as {pinned.row.config_hash[:8]}…"
        )
    return pinned


def _same_provenance(
    assignment: ActivatedAssignment, version: str | None, config_hash: str, where: str
) -> None:
    if version is None:
        raise Phase2ExportError(f"{where} has no config_version")
    if (version, config_hash) != (assignment.config_version, assignment.config_hash):
        raise Phase2ExportError(
            f"{where} was written under configuration {version!r}, but its case was "
            f"activated under {assignment.config_version!r}"
        )


def _has_prohibited_key(payload: Any) -> bool:
    if isinstance(payload, dict):
        return PROHIBITED_PAYLOAD_KEY in payload or any(
            _has_prohibited_key(v) for v in payload.values()
        )
    if isinstance(payload, list):
        return any(_has_prohibited_key(v) for v in payload)
    return False


# ---------------------------------------------------------------------------
# One measured case
# ---------------------------------------------------------------------------


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
    variables = derive_case_variables(inputs)

    config = pinned.study.telemetry
    timings: dict[tuple[int, str], ObservationTiming] = {}
    panels: dict[tuple[int, str, str], PanelSummary] = {}
    if config is not None:
        timings = derive_observation_timings(
            inputs.renders,
            inputs.telemetry_rows,
            inactivity_threshold_seconds=config.inactivity_threshold_seconds,
            tab_audit=inputs.tab_audit,
        )
        panels = derive_panel_summaries(
            inputs.renders,
            inputs.telemetry_rows,
            viewport_threshold=config.panel_viewport_threshold,
            viewed_threshold_seconds=config.panel_viewed_threshold_seconds,
            tab_audit=inputs.tab_audit,
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


def _timepoint_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    a = case.assignment
    life = case.lifecycle
    telemetry_on = case.pinned.study.telemetry is not None
    out = []
    for obs in case.variables.observations:
        t = obs.t_index
        s10 = case.s10.get(case.pinned.timepoints[t])
        primary = case.timings.get((t, PRIMARY))
        status = NOT_CONFIGURED
        if telemetry_on:
            status = str(primary.status) if primary is not None else "missing"
        out.append(
            (
                *_keys(study_id, case, t),
                a.arm,
                a.arm_source,
                _text(a.schedule_id),
                _num(a.case_position),
                _ts(a.activated_at),
                _text(life.state if life else None),
                _ts(life.completed_at if life else None),
                _ts(life.incomplete_at if life else None),
                _text(life.incomplete_reason if life else None),
                _bool(obs.reached),
                timing.format_ts(s10.started_at) if s10 else "",
                timing.format_ts(s10.ended_at) if s10 else "",
                _num(s10.elapsed_seconds) if s10 else "",
                status,
                _num(primary.foreground_seconds) if primary else "",
                _num(primary.active_seconds) if primary else "",
                _bool(obs.tab_conflict_detected),
                _bool(obs.ai_delivered),
                _bool(obs.ai_viewed),
                _text(obs.ai_viewing_status),
                _num(obs.ai_exposure_seconds),
                _num(obs.ai_episode_count),
                _bool(obs.intervention_failure),
                _joined(obs.intervention_failure_reasons),
                _bool(obs.intervention_leakage),
                _bool(obs.pp_compliant),
                _bool(obs.pp_determinate),
                _joined(obs.integrity_warnings),
            )
        )
    return out


def _exports_free_text(study: StudyConfig) -> bool:
    """S11i: a study declaring ``study_behaviour`` exports free text only
    under ``include_explicit``; legacy configs export it as before."""
    behaviour = study.study_behaviour
    return (
        behaviour is None or behaviour.free_text.routine_export is FreeTextExport.INCLUDE_EXPLICIT
    )


def _answer_cells(
    study_id: str,
    keys: tuple[str, ...],
    questions: Questions,
    study: StudyConfig,
    t_index: int,
    rows: Mapping[tuple[int, str], answers.AnswerRow],
    recorded_at: Mapping[tuple[float, str], object],
    responses: Mapping[tuple[int, str], ResponseProvenance] | None,
) -> list[tuple[str, ...]]:
    """One row per question of one timepoint, questions.yaml order."""
    by_id = {q.question_id: q for q in questions.questions}
    values = {
        qid: deserialize_answer(by_id[qid], row.value)
        for (t, qid), row in rows.items()
        if t == t_index and row.answer_source != AnswerSource.RULE
    }
    free_text = _exports_free_text(study)
    out = []
    for item in evaluate(questions, values).questions:
        question: Question = item.question
        qid = question.question_id
        row = rows.get((t_index, qid))
        provenance = responses.get((t_index, qid)) if responses is not None else None
        status = provenance.status if provenance else _practice_status(item, row)
        exported = row is not None and (question.response_type != FREE_TEXT or free_text)
        value = decode_stored_answer(question, row.value) if exported and row else ""
        out.append(
            (
                *keys,
                qid,
                question.response_type,
                str(item.state),
                _bool(item.required_now),
                str(status),
                value,
                _bool(exported) if row is not None else FALSE,
                _text(row.answer_source if row else None),
                _text(row.derived_from_question_id if row else None),
                _text(provenance.missing_reason if provenance else None),
                _ts(recorded_at.get((float(row.timepoint), qid))) if row else "",
            )
        )
    return out


def _practice_status(item: Any, row: answers.AnswerRow | None) -> ResponseStatus:
    if item.state is QuestionState.HIDDEN:
        return ResponseStatus.NOT_APPLICABLE
    return ResponseStatus.ANSWERED if row is not None else ResponseStatus.MISSING


def _answers_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    responses = {(r.t_index, r.question_id): r for r in case.variables.responses}
    out: list[tuple[str, ...]] = []
    for t_index in range(len(case.pinned.timepoints)):
        out.extend(
            _answer_cells(
                study_id,
                _keys(study_id, case, t_index),
                case.pinned.questions,
                case.pinned.study,
                t_index,
                case.answer_rows,
                case.recorded_at,
                responses,
            )
        )
    return out


def _has_ai_panel_events(case: CaseRecord, render_ids: set[str]) -> bool:
    return any(
        r.render_id in render_ids
        and r.kind.startswith("panel.")
        and r.payload.get("panel_id") == AI_PANEL
        for r in case.telemetry_rows
    )


def _panel_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    out = []
    no_ai = case.assignment.arm != ARM_AI
    kinds_order = {PRIMARY: 0, REVISIT: 1}
    keys = sorted(
        case.panels,
        key=lambda k: (k[0], kinds_order.get(k[1], len(kinds_order)), k[1], PANEL_IDS.index(k[2])),
    )
    for t_index, visit_kind, panel_id in keys:
        summary = case.panels[(t_index, visit_kind, panel_id)]
        if (
            no_ai
            and panel_id == AI_PANEL
            and not _has_ai_panel_events(case, set(summary.render_ids))
        ):
            continue  # no AI panel existed: nothing is fabricated
        out.append(
            (
                *_keys(study_id, case, t_index),
                visit_kind,
                panel_id,
                _bool(summary.mounted),
                str(summary.status),
                _num(summary.qualifying_seconds),
                _bool(summary.viewed),
                _num(summary.episode_count),
                _num(summary.panel_open_count),
                _num(summary.time_to_first_view_seconds),
                _text(summary.first_view_client_ts),
                _text(summary.last_view_client_ts),
                _bool(_conflict(case, t_index, visit_kind)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def _event_rows(
    study_id: str,
    rows: Sequence[EventRow],
    cases: Mapping[tuple[str, str], CaseRecord],
    all_sessions: Mapping[str, sessions.SessionRow],
) -> list[tuple[str, ...]]:
    visit_kinds = {
        r.render_id: str(json.loads(r.payload_json).get("visit_kind", ""))
        for r in rows
        if r.kind == telemetry.RENDER_KIND and r.render_id is not None
    }
    out = []
    for row in rows:
        if row.patient_id is None:
            continue  # clinician level: not a case event
        case = cases.get((row.clinician_id, row.patient_id))
        if case is None:
            continue  # practice or pre Phase 2 pair: not a measured case
        where = f"event {row.event_id} of patient {row.patient_id!r}"
        if row.session_id is not None:
            session = all_sessions.get(row.session_id)
            if session is None or (session.clinician_id, session.patient_id) != case.key:
                raise Phase2ExportError(f"{where} names a session of another case")
        t_index = ""
        if row.timepoint is not None:
            timepoints = case.pinned.timepoints
            if float(row.timepoint) not in timepoints:
                raise Phase2ExportError(f"{where} is at a timepoint the pinned config lacks")
            t_index = str(timepoints.index(float(row.timepoint)))
        payload = json.loads(row.payload_json)
        if _has_prohibited_key(payload):
            raise Phase2ExportError(f"{where} carries {PROHIBITED_PAYLOAD_KEY!r}")
        out.append(
            (
                study_id,
                str(row.event_id),
                _text(row.session_id),
                row.clinician_id,
                row.patient_id,
                _minutes(row.timepoint) if row.timepoint is not None else "",
                t_index,
                visit_kinds.get(row.render_id or "", ""),
                case.assignment.config_version or "",
                case.assignment.config_hash,
                _text(row.render_id),
                _text(row.tab_id),
                row.kind,
                _ts(row.client_ts) if row.client_ts else "",
                _ts(row.server_ts),
                _num(row.client_seq),
                _num(row.client_mono_ms),
                _canonical_json(row.payload_json),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Randomisation audit
# ---------------------------------------------------------------------------


def _audit_rows(
    study_id: str,
    schedules: Sequence[StoredSchedule],
    registry: Mapping[str, PinnedConfig],
    assignments: Sequence[ActivatedAssignment],
    lifecycles: Mapping[tuple[str, str], CaseLifecycle],
    plans: Sequence[ReplacementPlan],
) -> list[tuple[str, ...]]:
    items: dict[tuple[str, int], tuple[StoredSchedule, ScheduleItem]] = {}
    for stored in schedules:
        s = stored.schedule
        _pinned(registry, s.config_version, s.config_hash, f"schedule {s.schedule_id[:8]}…")
        for item in s.items:
            items[(s.schedule_id, item.case_position)] = (stored, item)

    realised: dict[tuple[str, int], ActivatedAssignment] = {}
    for a in assignments:
        where = f"assignment of patient {a.patient_id!r}, clinician {a.clinician_id}"
        if a.schedule_id is None or a.case_position is None:
            raise Phase2ExportError(f"{where} names no schedule item")
        found = items.get((a.schedule_id, a.case_position))
        if found is None:
            raise Phase2ExportError(f"{where} names a missing schedule item")
        stored, item = found
        if stored.schedule.clinician_id != a.clinician_id or item.patient_id != a.patient_id:
            raise Phase2ExportError(f"{where} disagrees with its schedule item")
        if item.planned_arm != a.arm:
            raise Phase2ExportError(f"{where} realised an arm other than the planned one")
        realised[(a.schedule_id, a.case_position)] = a

    held = {(a.clinician_id, a.patient_id) for a in assignments}
    for key in lifecycles:
        if key not in held:
            raise Phase2ExportError(f"lifecycle row of patient {key[1]!r} has no assignment")

    replacing: dict[tuple[str, int], ReplacementPlan] = {}
    replaced: dict[tuple[str, str], ReplacementPlan] = {}
    for plan in plans:
        where = f"replacement {plan.replacement_id[:8]}…"
        if (plan.clinician_id, plan.original_patient_id) not in held:
            raise Phase2ExportError(f"{where} replaces a case that was never activated")
        position = (plan.replacement_schedule_id, plan.replacement_case_position)
        if position not in items:
            raise Phase2ExportError(f"{where} points to a missing schedule item")
        stored, item = items[position]
        if (
            stored.schedule.clinician_id != plan.clinician_id
            or item.patient_id != plan.replacement_patient_id
            or item.planned_arm != plan.planned_arm
        ):
            raise Phase2ExportError(f"{where} disagrees with the schedule item it names")
        if plan.activated_at is not None and position not in realised:
            raise Phase2ExportError(f"{where} is activated but its case is missing")
        replacing[position] = plan
        replaced[(plan.clinician_id, plan.original_patient_id)] = plan

    out = []
    ordered = sorted(items.items(), key=lambda kv: (kv[1][0].schedule.clinician_id, kv[0]))
    for position, (stored, item) in ordered:
        s = stored.schedule
        a = realised.get(position)
        life = lifecycles.get((s.clinician_id, item.patient_id)) if a is not None else None
        into = replacing.get(position)
        out_of = replaced.get((s.clinician_id, item.patient_id)) if a is not None else None
        out.append(
            (
                study_id,
                s.clinician_id,
                s.schedule_id,
                s.config_version,
                s.config_hash,
                _ts(stored.generated_at),
                s.algorithm_version,
                str(s.master_seed),
                s.derived_seed_hex,
                _canonical_json(s.allocation_state_json),
                str(s.starting_ai_count),
                str(s.starting_no_ai_count),
                s.starting_arm,
                str(s.block_length),
                _canonical_json(s.block_sequence_json),
                str(item.case_position),
                item.patient_id,
                item.planned_arm,
                str(item.block_number),
                str(item.position_in_block),
                _text(item.preceding_block_arm),
                _num(item.planned_cases_since_ai),
                str(item.assignment_seed),
                _bool(a is not None),
                _ts(a.activated_at) if a else "",
                (a.config_version or "") if a else "",
                a.config_hash if a else "",
                a.arm if a else "",
                _text(life.state if life else None),
                _ts(life.completed_at if life else None),
                _ts(life.incomplete_at if life else None),
                _text(life.incomplete_reason if life else None),
                into.replacement_id if into else "",
                into.original_patient_id if into else "",
                _ts(into.generated_at) if into else "",
                _ts(into.activated_at) if into else "",
                out_of.replacement_id if out_of else "",
                out_of.replacement_patient_id if out_of else "",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Configuration files
# ---------------------------------------------------------------------------


def _history_order(rows: Sequence[ConfigHistoryRow]) -> list[ConfigHistoryRow]:
    return sorted(rows, key=lambda r: (_ts(r.activated_at), r.config_version))


def _history_rows(study_id: str, rows: Sequence[ConfigHistoryRow]) -> list[tuple[str, ...]]:
    return [
        (
            study_id,
            r.config_version,
            r.config_hash,
            _ts(r.activated_at),
            r.change_description,
            _text(r.change_reason),
            r.study_json,
            r.questions_json,
        )
        for r in _history_order(rows)
    ]


def _count_rows(
    study_id: str,
    history: Sequence[ConfigHistoryRow],
    schedules: Sequence[StoredSchedule],
    cases: Sequence[CaseRecord],
    answer_rows: Mapping[tuple[str, str], list[tuple[str, ...]]],
) -> list[tuple[str, ...]]:
    status_col = ANSWERS_HEADER.index("response_status")
    out = []
    for row in _history_order(history):
        version = row.config_version
        mine = [c for c in cases if c.assignment.config_version == version]
        states = [c.lifecycle.state if c.lifecycle else None for c in mine]
        out.append(
            (
                study_id,
                version,
                row.config_hash,
                str(
                    sum(
                        len(s.schedule.items)
                        for s in schedules
                        if s.schedule.config_version == version
                    )
                ),
                str(len(mine)),
                str(sum(1 for s in states if s is CaseState.COMPLETED)),
                str(sum(1 for s in states if s is CaseState.INCOMPLETE)),
                str(sum(1 for s in states if s in (CaseState.ACTIVE, CaseState.PAUSED))),
                str(sum(len(c.pinned.timepoints) for c in mine)),
                str(sum(1 for c in mine for o in c.variables.observations if o.reached)),
                str(
                    sum(
                        1
                        for c in mine
                        for r in answer_rows[c.key]
                        if r[status_col] == ResponseStatus.ANSWERED
                    )
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Practice
# ---------------------------------------------------------------------------


def _practice_tables(
    conn: sqlite3.Connection,
    study_id: str,
    cases: Sequence[PracticeCase],
    registry: Mapping[str, PinnedConfig],
    timing_events: Sequence[timing.TimingEvent],
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    timepoint_rows: list[tuple[str, ...]] = []
    answer_rows: list[tuple[str, ...]] = []
    mode = str(ObservationMode.PRACTICE)
    for case in cases:
        where = f"practice case of patient {case.patient_id!r}, clinician {case.clinician_id}"
        pinned = _pinned(registry, case.config_version, case.config_hash, where)
        rows = [
            r
            for r in answers.fetch_for_pair(conn, case.clinician_id, case.patient_id)
            if r.observation_mode == ObservationMode.PRACTICE
        ]
        for r in rows:
            if (r.config_version, r.config_hash) != (case.config_version, case.config_hash):
                raise Phase2ExportError(f"answer of {where} disagrees with its configuration")
        by_cell = _answer_rows_by_cell(rows, pinned, where)
        recorded = answers.fetch_recorded_at(conn, case.clinician_id, case.patient_id)
        try:
            s10 = timing.derive_timepoint_timings(
                timing_events, clinician_id=case.clinician_id, patient_id=case.patient_id
            )
        except timing.TimingError as exc:
            raise Phase2ExportError(f"cannot derive timing of {where}: {exc}") from exc

        timings: dict[tuple[int, str], ObservationTiming] = {}
        if pinned.study.telemetry is not None:
            timings = derive_observation_timings(
                telemetry.load_render_rows(conn, case.clinician_id, case.patient_id),
                telemetry.load_telemetry_rows(conn, case.clinician_id, case.patient_id),
                inactivity_threshold_seconds=pinned.study.telemetry.inactivity_threshold_seconds,
            )

        for t_index, minutes in enumerate(pinned.timepoints):
            keys = (
                study_id,
                case.clinician_id,
                case.patient_id,
                str(t_index),
                _minutes(minutes),
                case.config_version,
                case.config_hash,
            )
            tt = s10.get(minutes)
            primary = timings.get((t_index, PRIMARY))
            status = NOT_CONFIGURED
            if pinned.study.telemetry is not None:
                status = str(primary.status) if primary is not None else "missing"
            timepoint_rows.append(
                (
                    *keys,
                    mode,
                    case.arm,
                    _ts(case.started_at),
                    _ts(case.completed_at),
                    timing.format_ts(tt.started_at) if tt else "",
                    timing.format_ts(tt.ended_at) if tt else "",
                    _num(tt.elapsed_seconds) if tt else "",
                    status,
                    _num(primary.foreground_seconds) if primary else "",
                    _num(primary.active_seconds) if primary else "",
                )
            )
            cells = _answer_cells(
                study_id,
                keys,
                pinned.questions,
                pinned.study,
                t_index,
                by_cell,
                recorded,
                None,
            )
            answer_rows.extend((*cell, mode) for cell in cells)
    return timepoint_rows, answer_rows


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def _clinician_rows(
    conn: sqlite3.Connection, study_id: str, ids: tuple[str, ...]
) -> list[tuple[str, ...]]:
    """S11p characteristics as stored; blank with ``missing`` when none."""
    profiles = clinician_profiles.fetch_by_ids(conn, ids)
    out = []
    for clinician_id in ids:
        profile = profiles.get(clinician_id)
        if profile is None:
            out.append((study_id, clinician_id, PROFILE_MISSING, "", "", "", ""))
            continue
        out.append(
            (
                study_id,
                clinician_id,
                PROFILE_COMPLETE,
                profile.professional_role,
                _num(profile.years_of_practice),
                profile.country_of_practice,
                _text(profile.primary_specialty),
            )
        )
    return out


def _schema_version(conn: sqlite3.Connection) -> int:
    value = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    if value is None:
        raise Phase2ExportError("the database has no schema version")
    return int(value)


def build_phase2_bundle(
    conn: sqlite3.Connection,
    *,
    study_id: str,
    practice_export: PracticeExport = PracticeExport.EXCLUDE,
    keyfile: KeyfileRequest = KeyfileRequest.NONE,
) -> Phase2Bundle:
    """Read, validate and derive the whole bundle from one snapshot.

    Raises:
        Phase2ExportError: any provenance or linkage failure (nothing to publish).
    """
    if conn.in_transaction:
        raise Phase2ExportError("build_phase2_bundle takes its own snapshot; no open transaction")

    conn.execute("BEGIN")
    try:
        return _build(conn, study_id, practice_export, keyfile)
    finally:
        conn.rollback()


def _build(
    conn: sqlite3.Connection,
    study_id: str,
    practice_export: PracticeExport,
    keyfile: KeyfileRequest,
) -> Phase2Bundle:
    stored_id = study_identity.fetch(conn)
    if stored_id != study_id:
        raise Phase2ExportError(f"database belongs to study {stored_id!r}, not {study_id!r}")
    schema_version = _schema_version(conn)

    history = config_history.list_all(conn)
    registry = _registry(history, study_id)

    foreign = conn.execute(
        "SELECT COUNT(*) FROM randomisation_schedules WHERE study_id != ?", (study_id,)
    ).fetchone()[0]
    if foreign:
        raise Phase2ExportError(f"{foreign} randomisation schedule(s) belong to another study")
    schedules = schedules_dao.list_schedules(conn, study_id)
    assignments = arm_assignments.list_phase2(conn)
    lifecycles = lifecycle_dao.list_all(conn)
    plans = replacements.list_all(conn)
    audit = _audit_rows(study_id, schedules, registry, assignments, lifecycles, plans)

    all_sessions = {s.session_id: s for s in sessions.list_all(conn)}
    sessions_by_pair: dict[tuple[str, str], list[sessions.SessionRow]] = defaultdict(list)
    for s in all_sessions.values():
        if s.observation_mode == ObservationMode.MEASURED:
            sessions_by_pair[(s.clinician_id, s.patient_id)].append(s)
    timing_events = timing.fetch_timing_events(conn)

    cases = sorted(
        (
            _case_record(
                conn,
                a,
                registry,
                lifecycles.get((a.clinician_id, a.patient_id)),
                sessions_by_pair.get((a.clinician_id, a.patient_id), []),
                timing_events,
            )
            for a in assignments
        ),
        key=lambda c: c.sort_key,
    )
    by_key = {c.key: c for c in cases}

    answers_by_case = {c.key: _answers_rows(study_id, c) for c in cases}
    event_rows = _event_rows(
        study_id, events.list_by_prefix(conn, RESEARCH_EVENT_PREFIXES), by_key, all_sessions
    )

    tables = [
        Table(
            "timepoints.csv",
            TIMEPOINTS_HEADER,
            tuple(r for c in cases for r in _timepoint_rows(study_id, c)),
        ),
        Table(
            "answers.csv", ANSWERS_HEADER, tuple(r for c in cases for r in answers_by_case[c.key])
        ),
        Table(
            "panel_summaries.csv",
            PANELS_HEADER,
            tuple(r for c in cases for r in _panel_rows(study_id, c)),
        ),
        Table("behavioral_events.csv", EVENTS_HEADER, tuple(event_rows)),
        Table("randomisation_audit.csv", AUDIT_HEADER, tuple(audit)),
        Table("configuration_history.csv", HISTORY_HEADER, tuple(_history_rows(study_id, history))),
        Table(
            "configuration_counts.csv",
            COUNTS_HEADER,
            tuple(_count_rows(study_id, history, schedules, cases, answers_by_case)),
        ),
    ]

    clinician_ids = {c.assignment.clinician_id for c in cases}
    clinician_ids |= {s.schedule.clinician_id for s in schedules}
    included = practice_export is PracticeExport.INCLUDE
    if included:
        practice_cases = practice.list_all(conn)
        p_timepoints, p_answers = _practice_tables(
            conn, study_id, practice_cases, registry, timing_events
        )
        tables.append(
            Table("practice_timepoints.csv", PRACTICE_TIMEPOINTS_HEADER, tuple(p_timepoints))
        )
        tables.append(Table("practice_answers.csv", PRACTICE_ANSWERS_HEADER, tuple(p_answers)))
        clinician_ids |= {p.clinician_id for p in practice_cases}

    ids = tuple(sorted(clinician_ids))
    tables.insert(
        len(tables) - (2 if included else 0),
        Table("clinicians.csv", CLINICIANS_HEADER, tuple(_clinician_rows(conn, study_id, ids))),
    )
    keyfile_rows = None
    if keyfile is KeyfileRequest.REQUESTED:
        keyfile_rows = tuple(clinicians.fetch_by_ids(conn, ids)) if ids else ()
        if {cid for cid, _ in keyfile_rows} != set(ids):
            raise Phase2ExportError("clinicians lookup did not round-trip for the keyfile")

    return Phase2Bundle(
        study_id=study_id,
        source_schema_version=schema_version,
        config_versions=tuple(r.config_version for r in _history_order(history)),
        practice_included=included,
        tables=tuple(tables),
        clinician_ids=ids,
        keyfile_rows=keyfile_rows,
    )
