"""S11l observation variables: delivery, AI viewing, PP and missing responses (pure).

Spec: ``specs/session-11l-ai-viewed-and-pp.md``.

One measured Phase 2 case in, reproducible research variables out; nothing
is written and the ITT arm is only ever read::

    arm (ITT) ─────────────────────────────────────────────┐
    timepoint.render {ai} ─► delivery / failure / leakage ─┤
    S11k ai panel summary ─► exposure / viewed ────────────┼─► PP
    progress + S10 enter  ─► reached ──────────────────────┘
    answers + S11h branch ─► response provenance (never an inserted answer)

Integrity problems are flagged on the observation, never raised, so one bad
row cannot block an export.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from ehr_simulator.answer_codec import deserialize_answer
from ehr_simulator.behavioral_timing import TelemetryStatus, build_timeline, rows_by_render
from ehr_simulator.config.questions import Questions
from ehr_simulator.config.study import TelemetryConfig
from ehr_simulator.db.arm_assignments import ARM_AI
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.telemetry import RenderRow, TelemetryRow
from ehr_simulator.panel_exposure import PanelSummary, derive_panel_summaries
from ehr_simulator.question_branching import AnswerSource, QuestionState, evaluate

__all__ = [
    "AI_PANEL",
    "AnswerCell",
    "CaseInputs",
    "CaseSummary",
    "CaseVariables",
    "FailureReason",
    "MissingReason",
    "ObservationVariables",
    "ResponseProvenance",
    "ResponseStatus",
    "derive_case_variables",
]

AI_PANEL = "ai"
PRIMARY = "primary"

#: ``timepoint.render`` ``ai`` values (``web.panels.AIDelivery``).
AI_SHOWN = "shown"
AI_NONE = "none"
AI_UNAVAILABLE = "unavailable"
AI_ERROR = "error"

#: AI panel states in which the browser displayed the AI content.
_DISPLAYED_STATES = frozenset({"loading", "partial"})

#: ``ai_unavailable_reason`` values that mean the frozen artifact is missing.
_MISSING_ARTIFACT_REASONS = frozenset({"not_configured", "artifact_mismatch", "missing_row"})


class FailureReason(StrEnum):
    MISSING_ARTIFACT = "missing_artifact"
    RENDER_FAILURE = "render_failure"
    DISPLAY_FAILURE = "display_failure"
    OTHER_INTEGRITY_FAILURE = "other_integrity_failure"


class ResponseStatus(StrEnum):
    ANSWERED = "answered"
    NOT_APPLICABLE = "not_applicable"  # hidden by the S11h branch
    MISSING = "missing"


class MissingReason(StrEnum):
    TECHNICAL_FAILURE = "technical_failure"  # reserved: no producer yet
    CASE_ABANDONED = "case_abandoned"
    REACHED_UNANSWERED = "reached_unanswered"
    PENDING = "pending"
    TIMEPOINT_NEVER_REACHED = "timepoint_never_reached"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnswerCell:
    """One stored answer (persisted value) with the arm it was written under."""

    value: str
    answer_source: str
    arm: str


@dataclass(frozen=True)
class CaseInputs:
    """Everything one measured case derives from, already read from the DB.

    ``answers`` is keyed by ``(t_index, question_id)``; ``enter_t_indices``
    are the timepoints with an S10 ``timepoint.enter``; ``session_hashes``
    maps each session to its pinned ``config_hash``.
    """

    study_id: str
    clinician_id: str
    patient_id: str
    arm: str
    config_version: str | None
    config_hash: str
    timepoints: tuple[float, ...]
    questions: Questions
    telemetry: TelemetryConfig | None
    case_state: CaseState | None
    incomplete_reason: str | None
    unlocked_t_index: int | None
    completed: bool
    enter_t_indices: frozenset[int]
    answers: Mapping[tuple[int, str], AnswerCell]
    renders: Sequence[RenderRow]
    telemetry_rows: Sequence[TelemetryRow]
    session_hashes: Mapping[str, str]


# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationVariables:
    """One clinician × patient × timepoint. ``None`` means indeterminate."""

    t_index: int
    arm: str
    reached: bool
    ai_delivered: bool | None
    intervention_failure_reasons: tuple[FailureReason, ...]
    intervention_leakage: bool
    ai_exposure_seconds: float | None
    ai_viewed: bool | None
    ai_viewing_status: TelemetryStatus | None
    ai_episode_count: int
    pp_compliant: bool | None
    pp_determinate: bool
    integrity_warnings: tuple[str, ...]

    @property
    def intervention_failure(self) -> bool:
        return bool(self.intervention_failure_reasons)


@dataclass(frozen=True)
class ResponseProvenance:
    t_index: int
    question_id: str
    required: bool
    status: ResponseStatus
    answer_source: str | None
    missing_reason: MissingReason | None


@dataclass(frozen=True)
class CaseSummary:
    """Secondary convenience measures; the timepoint rows stay authoritative."""

    ai_viewed_any: bool
    ai_viewed_timepoints: int
    ai_exposure_seconds: float
    ai_episode_count: int
    all_reached_pp_compliant: bool
    failure_count: int
    leakage_count: int


@dataclass(frozen=True)
class CaseVariables:
    study_id: str
    clinician_id: str
    patient_id: str
    arm: str
    config_version: str | None
    config_hash: str
    case_state: CaseState | None
    incomplete_reason: str | None
    observations: tuple[ObservationVariables, ...]
    responses: tuple[ResponseProvenance, ...]
    summary: CaseSummary


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def _reached(inputs: CaseInputs, t_index: int) -> bool:
    """Passed through the gate, or the frontier with S10 presentation evidence."""
    if inputs.completed:
        return True
    if inputs.unlocked_t_index is None:
        return False
    if t_index < inputs.unlocked_t_index:
        return True
    return t_index == inputs.unlocked_t_index and t_index in inputs.enter_t_indices


def _ai_mounted(rows: Sequence[TelemetryRow], render_id: str) -> bool:
    return any(
        r.render_id == render_id
        and r.kind == "panel.mount"
        and r.payload.get("panel_id") == AI_PANEL
        and r.payload.get("state") in _DISPLAYED_STATES
        for r in rows
    )


def _has_ai_panel_event(rows: Sequence[TelemetryRow], render_ids: set[str]) -> bool:
    return any(
        r.render_id in render_ids
        and r.kind.startswith("panel.")
        and r.payload.get("panel_id") == AI_PANEL
        for r in rows
    )


def _delivery(
    inputs: CaseInputs,
    primary: Sequence[RenderRow],
    statuses: Mapping[str, TelemetryStatus],
) -> tuple[bool | None, list[FailureReason]]:
    """AI arm: ``(ai_delivered, failure reasons)`` over the primary renders."""
    reasons: list[FailureReason] = []
    delivered: bool | None = False
    for render in primary:
        ai = render.payload.get("ai")
        if ai == AI_UNAVAILABLE:
            reason = render.payload.get("ai_unavailable_reason")
            if reason in _MISSING_ARTIFACT_REASONS:
                reasons.append(FailureReason.MISSING_ARTIFACT)
            else:
                reasons.append(FailureReason.OTHER_INTEGRITY_FAILURE)
        elif ai == AI_ERROR:
            reasons.append(FailureReason.RENDER_FAILURE)
        elif ai == AI_SHOWN:
            if _ai_mounted(inputs.telemetry_rows, render.render_id):
                delivered = True
            elif statuses.get(render.render_id) is TelemetryStatus.COMPLETE:
                reasons.append(FailureReason.DISPLAY_FAILURE)
            elif delivered is False:
                delivered = None  # shown, but the browser's report is missing
        else:
            reasons.append(FailureReason.OTHER_INTEGRITY_FAILURE)

    if not primary:
        delivered = None
    return delivered, reasons


def _render_statuses(inputs: CaseInputs) -> dict[str, TelemetryStatus]:
    by_render = rows_by_render(inputs.telemetry_rows)
    return {
        r.render_id: build_timeline(r, by_render.get(r.render_id, [])).status
        for r in inputs.renders
    }


def _panel_summaries(inputs: CaseInputs) -> dict[tuple[int, str, str], PanelSummary]:
    if inputs.telemetry is None:
        return {}
    return derive_panel_summaries(
        inputs.renders,
        inputs.telemetry_rows,
        viewport_threshold=inputs.telemetry.panel_viewport_threshold,
        viewed_threshold_seconds=inputs.telemetry.panel_viewed_threshold_seconds,
    )


def _observation(
    inputs: CaseInputs,
    t_index: int,
    summaries: Mapping[tuple[int, str, str], PanelSummary],
    statuses: Mapping[str, TelemetryStatus],
) -> ObservationVariables:
    renders = [r for r in inputs.renders if r.t_index == t_index]
    primary = [r for r in renders if r.visit_kind == PRIMARY]
    ai_summary = summaries.get((t_index, PRIMARY, AI_PANEL))
    reached = _reached(inputs, t_index)
    warnings = _integrity_warnings(inputs, t_index, renders, statuses)
    is_ai = inputs.arm == ARM_AI

    reasons: list[FailureReason] = []
    exposure: float | None = None
    viewed: bool | None = None
    viewing_status: TelemetryStatus | None = None
    if is_ai:
        leakage = False
        delivered, reasons = _delivery(inputs, primary, statuses)
        if ai_summary is not None:
            exposure, viewed = ai_summary.qualifying_seconds, ai_summary.viewed
            viewing_status = ai_summary.status
    else:
        shown_markup = any(r.payload.get("ai") != AI_NONE for r in renders)
        panel_events = _has_ai_panel_event(inputs.telemetry_rows, {r.render_id for r in renders})
        leakage = shown_markup or panel_events
        delivered = leakage
        if shown_markup:
            reasons.append(FailureReason.OTHER_INTEGRITY_FAILURE)
        if panel_events and ai_summary is not None:
            exposure = ai_summary.qualifying_seconds

    pp_compliant: bool | None
    determinate = True
    if not reached:
        pp_compliant = None
    elif is_ai:
        pp_compliant = delivered is True and viewed is True
        determinate = delivered is not None and viewed is not None
    else:
        pp_compliant = not leakage

    return ObservationVariables(
        t_index=t_index,
        arm=inputs.arm,
        reached=reached,
        ai_delivered=delivered,
        intervention_failure_reasons=tuple(sorted(set(reasons))),
        intervention_leakage=leakage,
        ai_exposure_seconds=exposure,
        ai_viewed=viewed,
        ai_viewing_status=viewing_status,
        ai_episode_count=ai_summary.episode_count if ai_summary is not None else 0,
        pp_compliant=pp_compliant,
        pp_determinate=determinate,
        integrity_warnings=warnings,
    )


def _integrity_warnings(
    inputs: CaseInputs,
    t_index: int,
    renders: Sequence[RenderRow],
    statuses: Mapping[str, TelemetryStatus],
) -> tuple[str, ...]:
    warnings: list[str] = []
    mismatched = sorted(
        qid for (t, qid), cell in inputs.answers.items() if t == t_index and cell.arm != inputs.arm
    )
    if mismatched:
        warnings.append(f"answer arm differs from the assignment: {mismatched}")
    if any(statuses.get(r.render_id) is TelemetryStatus.INVALID for r in renders):
        warnings.append("telemetry monotonic time runs backwards")
    foreign = [
        r.render_id
        for r in renders
        if r.session_id is not None
        and inputs.session_hashes.get(r.session_id, inputs.config_hash) != inputs.config_hash
    ]
    if foreign:
        warnings.append("render session provenance differs from the case configuration")
    if inputs.arm != ARM_AI and any(r.payload.get("ai") != AI_NONE for r in renders):
        warnings.append("AI rendered on a no AI case")
    return tuple(warnings)


def _missing_reason(inputs: CaseInputs, t_index: int, reached: bool) -> MissingReason:
    """Precedence 3–8 of the spec (hidden and answered are handled first)."""
    state = inputs.case_state
    if reached and state is CaseState.INCOMPLETE and t_index == inputs.unlocked_t_index:
        return MissingReason.CASE_ABANDONED
    if reached:
        return MissingReason.REACHED_UNANSWERED
    if state in (CaseState.ACTIVE, CaseState.PAUSED):
        return MissingReason.PENDING
    if state is CaseState.INCOMPLETE:
        return MissingReason.TIMEPOINT_NEVER_REACHED
    return MissingReason.UNKNOWN


def _responses(inputs: CaseInputs) -> tuple[ResponseProvenance, ...]:
    by_id = {q.question_id: q for q in inputs.questions.questions}
    out: list[ResponseProvenance] = []
    for t_index in range(len(inputs.timepoints)):
        cells = {
            qid: cell for (t, qid), cell in inputs.answers.items() if t == t_index and qid in by_id
        }
        values = {
            qid: deserialize_answer(by_id[qid], cell.value)
            for qid, cell in cells.items()
            if cell.answer_source != AnswerSource.RULE
        }
        reached = _reached(inputs, t_index)
        for item in evaluate(inputs.questions, values).questions:
            qid = item.question.question_id
            cell = cells.get(qid)
            status, source, reason = ResponseStatus.MISSING, None, None
            if item.state is QuestionState.HIDDEN:
                status = ResponseStatus.NOT_APPLICABLE
            elif cell is not None:
                status, source = ResponseStatus.ANSWERED, cell.answer_source
            elif item.state is QuestionState.DERIVED:
                reason = MissingReason.UNKNOWN  # a rule row should exist
            else:
                reason = _missing_reason(inputs, t_index, reached)
            out.append(
                ResponseProvenance(
                    t_index=t_index,
                    question_id=qid,
                    required=item.question.required,
                    status=status,
                    answer_source=source,
                    missing_reason=reason,
                )
            )
    return tuple(out)


def _summary(observations: Sequence[ObservationVariables]) -> CaseSummary:
    reached = [o for o in observations if o.reached]
    return CaseSummary(
        ai_viewed_any=any(o.ai_viewed is True for o in observations),
        ai_viewed_timepoints=sum(1 for o in observations if o.ai_viewed is True),
        ai_exposure_seconds=sum(o.ai_exposure_seconds or 0.0 for o in observations),
        ai_episode_count=sum(o.ai_episode_count for o in observations),
        all_reached_pp_compliant=bool(reached) and all(o.pp_compliant for o in reached),
        failure_count=sum(1 for o in observations if o.intervention_failure),
        leakage_count=sum(1 for o in observations if o.intervention_leakage),
    )


def derive_case_variables(inputs: CaseInputs) -> CaseVariables:
    """All S11l variables of one measured case."""
    statuses = _render_statuses(inputs)
    summaries = _panel_summaries(inputs)
    observations = tuple(
        _observation(inputs, t_index, summaries, statuses)
        for t_index in range(len(inputs.timepoints))
    )
    return CaseVariables(
        study_id=inputs.study_id,
        clinician_id=inputs.clinician_id,
        patient_id=inputs.patient_id,
        arm=inputs.arm,
        config_version=inputs.config_version,
        config_hash=inputs.config_hash,
        case_state=inputs.case_state,
        incomplete_reason=inputs.incomplete_reason,
        observations=observations,
        responses=_responses(inputs),
        summary=_summary(observations),
    )
