"""S11l: delivery, leakage, AI viewing, PP and missing response provenance.

Pure tests build ``CaseInputs`` directly (renders + S11j/S11k streams on the
first use case questions); integration tests run the real Phase 2 app and
``load_case_variables`` on its DB::

    render {ai} + ai panel mount ─► delivered     exposure >= 2 s ─► viewed
    AI: PP = delivered AND viewed                 no AI: PP = no leakage
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from ehr_simulator.behavioral_timing import TelemetryStatus
from ehr_simulator.config import load_questions
from ehr_simulator.config.study import TelemetryConfig
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.telemetry import RenderRow
from ehr_simulator.ingestion import load_synthetic
from ehr_simulator.study_variables import (
    AnswerCell,
    CaseInputs,
    FailureReason,
    MissingReason,
    ResponseStatus,
    derive_case_variables,
)
from ehr_simulator.study_variables_reader import load_case_variables
from ehr_simulator.web.panels import (
    InterventionContext,
    InterventionMode,
    ai_delivery,
    slice_to_timepoint,
)
from tests.conftest import answer_all_required, seed_progress
from tests.test_case_start import Harness, _start, _started_patient, harness  # noqa: F401
from tests.test_panel_exposure import PanelStream
from tests.test_telemetry import TELEMETRY, _event, _post, _view

REPO = Path(__file__).parent.parent
FUC_QUESTIONS = load_questions(REPO / "configs" / "example_phase2_questions.yaml")
TIMEPOINTS = (0.0, 60.0, 180.0)
AI, NO_AI = "ai", "no_ai"
HX = {"HX-Request": "true"}


def _render(
    render_id: str, t_index: int = 0, *, ai: str = "shown", visit_kind: str = "primary", **extra
) -> RenderRow:
    return RenderRow(
        event_id=int(render_id.lstrip("r") or 0),
        render_id=render_id,
        session_id="s1",
        clinician_id="c",
        patient_id="p",
        timepoint=TIMEPOINTS[t_index],
        payload={"t_index": t_index, "visit_kind": visit_kind, "ai": ai, **extra},
    )


def _ai_stream(
    render_id: str, seconds: float, *, exit_at: float = 30.0, seq: int = 1
) -> PanelStream:
    """A complete render whose AI panel is exposed for ``seconds`` (0 = mounted, unseen)."""
    stream = PanelStream(render_id, seq_start=seq).enter(0).mount(0, "ai")
    if seconds:
        stream.ratio(0, 0.5, "ai").ratio(seconds, 0.0, "ai")
    else:
        stream.ratio(0, 0.0, "ai")
    return stream.exit(exit_at)  # type: ignore[return-value]


def _inputs(**changes: Any) -> CaseInputs:
    base = CaseInputs(
        study_id="study",
        clinician_id="c",
        patient_id="p",
        arm=AI,
        config_version="v1",
        config_hash="h",
        timepoints=TIMEPOINTS,
        questions=FUC_QUESTIONS,
        telemetry=TelemetryConfig(**TELEMETRY),
        case_state=CaseState.COMPLETED,
        incomplete_reason=None,
        unlocked_t_index=2,
        completed=True,
        enter_t_indices=frozenset({0, 1, 2}),
        answers={},
        renders=(),
        telemetry_rows=(),
        session_hashes={"s1": "h"},
    )
    return replace(base, **changes)


def _obs(inputs: CaseInputs, t_index: int = 0):
    return derive_case_variables(inputs).observations[t_index]


def _with_streams(renders: list[RenderRow], streams: list[PanelStream], **changes: Any):
    rows = tuple(row for s in streams for row in s.rows)
    return _inputs(renders=tuple(renders), telemetry_rows=rows, **changes)


# ---------------------------------------------------------------------------
# Server delivery evidence (render payload)
# ---------------------------------------------------------------------------


def _slice(t_minutes: float = 0.0):
    return slice_to_timepoint(load_synthetic(), "synth_001", t_minutes, 0)


def test_ai_delivery_values_by_mode(study_fixture_dir: Path) -> None:
    from tests.test_intervention import _study

    study = _study(study_fixture_dir)
    dataset = load_synthetic()
    shown = InterventionContext(InterventionMode.AI, study.ai_intervention, dataset.ai_provenance)

    assert ai_delivery(_slice(), InterventionContext(InterventionMode.NO_AI)) == {"ai": "none"}
    assert ai_delivery(_slice(), InterventionContext(InterventionMode.LEGACY)) == {"ai": "legacy"}
    assert ai_delivery(_slice(), shown) == {"ai": "shown"}
    assert ai_delivery(_slice(), InterventionContext(InterventionMode.AI)) == {
        "ai": "unavailable",
        "ai_unavailable_reason": "not_configured",
    }
    assert ai_delivery(_slice(7.0), shown) == {
        "ai": "unavailable",
        "ai_unavailable_reason": "missing_row",
    }


def test_ai_delivery_marks_a_failed_renderer() -> None:
    failed = _slice()
    failed.panel_errors["ai"] = "boom"

    assert ai_delivery(failed, InterventionContext(InterventionMode.AI)) == {"ai": "error"}


# ---------------------------------------------------------------------------
# Delivery and failure
# ---------------------------------------------------------------------------


def test_shown_and_mounted_is_delivered() -> None:
    obs = _obs(_with_streams([_render("r1")], [_ai_stream("r1", 3)]))

    assert obs.ai_delivered is True
    assert obs.intervention_failure_reasons == ()


@pytest.mark.parametrize("reason", ["missing_row", "artifact_mismatch", "not_configured"])
def test_missing_artifact_is_not_delivered_and_keeps_ai(reason: str) -> None:
    render = _render("r1", ai="unavailable", ai_unavailable_reason=reason)
    obs = _obs(_with_streams([render], [PanelStream("r1").enter(0).exit(10)]))

    assert obs.arm == AI
    assert obs.ai_delivered is False
    assert obs.intervention_failure_reasons == (FailureReason.MISSING_ARTIFACT,)
    assert obs.pp_compliant is False


def test_render_error_is_render_failure() -> None:
    obs = _obs(_with_streams([_render("r1", ai="error")], []))

    assert obs.intervention_failure_reasons == (FailureReason.RENDER_FAILURE,)
    assert obs.ai_delivered is False and obs.arm == AI


def test_shown_without_mount_on_complete_telemetry_is_display_failure() -> None:
    stream = PanelStream("r1").enter(0).mount(0, "vitals").exit(10)
    obs = _obs(_with_streams([_render("r1")], [stream]))

    assert obs.intervention_failure_reasons == (FailureReason.DISPLAY_FAILURE,)
    assert obs.ai_delivered is False


def test_shown_without_browser_report_is_indeterminate() -> None:
    obs = _obs(_with_streams([_render("r1")], []))

    assert obs.ai_delivered is None
    assert obs.intervention_failure_reasons == ()
    assert obs.pp_compliant is False and obs.pp_determinate is False


def test_no_render_evidence_is_indeterminate() -> None:
    obs = _obs(_inputs())

    assert obs.ai_delivered is None


# ---------------------------------------------------------------------------
# Leakage
# ---------------------------------------------------------------------------


def test_no_ai_without_ai_evidence_is_compliant() -> None:
    stream = PanelStream("r1").enter(0).mount(0, "vitals").exit(10)
    obs = _obs(_with_streams([_render("r1", ai="none")], [stream], arm=NO_AI))

    assert obs.intervention_leakage is False
    assert obs.ai_delivered is False and obs.ai_viewed is None
    assert obs.pp_compliant is True and obs.pp_determinate is True


def test_no_ai_needs_no_telemetry_for_pp() -> None:
    obs = _obs(_with_streams([_render("r1", ai="none")], [], arm=NO_AI))

    assert obs.pp_compliant is True


def test_ai_panel_event_on_no_ai_is_leakage() -> None:
    obs = _obs(_with_streams([_render("r1", ai="none")], [_ai_stream("r1", 3)], arm=NO_AI))

    assert obs.arm == NO_AI
    assert obs.intervention_leakage is True
    assert obs.ai_delivered is True
    assert obs.ai_exposure_seconds == 3
    assert obs.pp_compliant is False


def test_ai_markup_on_no_ai_is_leakage_and_integrity_failure() -> None:
    obs = _obs(_with_streams([_render("r1", ai="shown")], [], arm=NO_AI))

    assert obs.intervention_leakage is True
    assert FailureReason.OTHER_INTEGRITY_FAILURE in obs.intervention_failure_reasons
    assert "AI rendered on a no AI case" in obs.integrity_warnings


def test_revisit_leakage_still_counts() -> None:
    revisit = _render("r2", visit_kind="revisit", ai="none")
    obs = _obs(
        _with_streams([_render("r1", ai="none"), revisit], [_ai_stream("r2", 1, seq=20)], arm=NO_AI)
    )

    assert obs.intervention_leakage is True


# ---------------------------------------------------------------------------
# AI viewed and PP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("seconds", "viewed"), [(1.99, False), (2.0, True), (5.5, True)])
def test_viewed_threshold_and_pp(seconds: float, viewed: bool) -> None:
    obs = _obs(_with_streams([_render("r1")], [_ai_stream("r1", seconds)]))

    assert obs.ai_viewed is viewed
    assert obs.ai_exposure_seconds == pytest.approx(seconds)
    assert obs.pp_compliant is viewed
    assert obs.pp_determinate is True


def test_separate_episodes_summing_to_two_seconds_are_viewed() -> None:
    stream = PanelStream("r1").enter(0).mount(0, "ai").ratio(0, 0.5, "ai").ratio(1, 0, "ai")
    stream.ratio(5, 0.5, "ai").ratio(6, 0.0, "ai").exit(10)
    obs = _obs(_with_streams([_render("r1")], [stream]))

    assert obs.ai_viewed is True and obs.ai_episode_count == 2


def test_exposure_does_not_carry_to_next_timepoint() -> None:
    renders = [_render("r1", 0), _render("r2", 1)]
    streams = [_ai_stream("r1", 1.5), _ai_stream("r2", 1.5, seq=20)]
    variables = derive_case_variables(_with_streams(renders, streams))

    assert [o.ai_viewed for o in variables.observations[:2]] == [False, False]


def test_passive_exposure_after_inactivity_counts() -> None:
    # No activity at all after entry; the panel stays in view for 100 s.
    obs = _obs(_with_streams([_render("r1")], [_ai_stream("r1", 100, exit_at=120)]))

    assert obs.ai_viewed is True


def test_revisit_exposure_never_satisfies_primary() -> None:
    renders = [_render("r1"), _render("r2", visit_kind="revisit")]
    streams = [_ai_stream("r1", 0), _ai_stream("r2", 10, seq=20)]
    obs = _obs(_with_streams(renders, streams))

    assert obs.ai_viewed is False
    assert obs.pp_compliant is False


def test_incomplete_below_threshold_is_indeterminate() -> None:
    stream = PanelStream("r1").enter(0).mount(0, "ai").ratio(0, 0.5, "ai").activity(1)
    obs = _obs(_with_streams([_render("r1")], [stream]))

    assert obs.ai_viewing_status is TelemetryStatus.INCOMPLETE
    assert obs.ai_viewed is None
    assert obs.pp_compliant is False and obs.pp_determinate is False


def test_multi_tab_is_indeterminate() -> None:
    other = PanelStream("r2", tab_id="tab-b").enter(0).mount(0, "ai").ratio(0, 0.5, "ai").exit(5)
    obs = _obs(_with_streams([_render("r1"), _render("r2")], [_ai_stream("r1", 5), other]))

    assert obs.ai_viewing_status is TelemetryStatus.MULTI_TAB
    assert obs.ai_exposure_seconds is None and obs.ai_viewed is None


def test_mixed_compliance_within_one_case() -> None:
    renders = [_render("r1", 0), _render("r2", 1), _render("r3", 2)]
    streams = [_ai_stream("r1", 3), _ai_stream("r2", 0, seq=20), _ai_stream("r3", 2, seq=40)]
    variables = derive_case_variables(_with_streams(renders, streams))

    assert [o.pp_compliant for o in variables.observations] == [True, False, True]
    summary = variables.summary
    assert summary.ai_viewed_any is True
    assert summary.ai_viewed_timepoints == 2
    assert summary.ai_exposure_seconds == pytest.approx(5)
    assert summary.ai_episode_count == 2
    assert summary.all_reached_pp_compliant is False


def test_unreached_timepoint_has_no_pp() -> None:
    inputs = _inputs(
        case_state=CaseState.ACTIVE,
        unlocked_t_index=0,
        completed=False,
        enter_t_indices=frozenset(),
    )

    assert _obs(inputs, 0).reached is False
    assert _obs(inputs, 0).pp_compliant is None


# ---------------------------------------------------------------------------
# Missing response provenance
# ---------------------------------------------------------------------------


def _responses(inputs: CaseInputs) -> dict[tuple[int, str], Any]:
    return {(r.t_index, r.question_id): r for r in derive_case_variables(inputs).responses}


def _answers(t_index: int, **values: str) -> dict[tuple[int, str], AnswerCell]:
    return {(t_index, qid): AnswerCell(v, "clinician", AI) for qid, v in values.items()}


def test_clinician_and_rule_answers_are_present() -> None:
    cells = _answers(0, deterioration_6h="No", good_outcome_3mo="Yes")
    cells[(0, "death_3mo")] = AnswerCell("No", "rule", AI)
    responses = _responses(_inputs(answers=cells))

    assert responses[(0, "deterioration_6h")].status is ResponseStatus.ANSWERED
    assert responses[(0, "death_3mo")].status is ResponseStatus.ANSWERED
    assert responses[(0, "death_3mo")].answer_source == "rule"


def test_hidden_question_is_not_applicable() -> None:
    responses = _responses(_inputs(answers=_answers(0, deterioration_6h="No")))

    assert responses[(0, "primary_cause")].status is ResponseStatus.NOT_APPLICABLE
    assert responses[(0, "primary_cause")].missing_reason is None


def test_completed_case_blank_question_is_reached_unanswered() -> None:
    responses = _responses(_inputs(answers=_answers(0, deterioration_6h="Yes")))

    item = responses[(0, "primary_cause")]
    assert item.status is ResponseStatus.MISSING
    assert item.missing_reason is MissingReason.REACHED_UNANSWERED


def test_incomplete_case_reasons_by_position() -> None:
    inputs = _inputs(
        case_state=CaseState.INCOMPLETE,
        incomplete_reason="reconnection_timeout",
        unlocked_t_index=1,
        completed=False,
        enter_t_indices=frozenset({0, 1}),
    )
    variables = derive_case_variables(inputs)
    reasons = {(r.t_index, r.question_id): r.missing_reason for r in variables.responses}

    assert reasons[(0, "confidence")] is MissingReason.REACHED_UNANSWERED
    assert reasons[(1, "confidence")] is MissingReason.CASE_ABANDONED
    assert reasons[(2, "confidence")] is MissingReason.TIMEPOINT_NEVER_REACHED
    assert variables.incomplete_reason == "reconnection_timeout"


def test_open_case_future_timepoint_is_pending() -> None:
    inputs = _inputs(case_state=CaseState.ACTIVE, unlocked_t_index=0, completed=False)
    responses = _responses(inputs)

    assert responses[(0, "confidence")].missing_reason is MissingReason.REACHED_UNANSWERED
    assert responses[(1, "confidence")].missing_reason is MissingReason.PENDING


def test_frontier_without_enter_evidence_is_not_reached() -> None:
    inputs = _inputs(
        case_state=CaseState.INCOMPLETE,
        unlocked_t_index=1,
        completed=False,
        enter_t_indices=frozenset({0}),
    )
    responses = _responses(inputs)

    assert responses[(1, "confidence")].missing_reason is MissingReason.TIMEPOINT_NEVER_REACHED


def test_derived_question_without_rule_row_is_unknown() -> None:
    responses = _responses(_inputs(answers=_answers(0, good_outcome_3mo="Yes")))

    assert responses[(0, "death_3mo")].missing_reason is MissingReason.UNKNOWN


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------


def test_answer_arm_mismatch_is_flagged() -> None:
    cells = {(0, "confidence"): AnswerCell("3", "clinician", NO_AI)}
    obs = _obs(_inputs(answers=cells))

    assert any("answer arm differs" in w for w in obs.integrity_warnings)


def test_invalid_telemetry_is_flagged() -> None:
    stream = PanelStream("r1").enter(10).state(5, visible=True, focused=False).exit(20)
    obs = _obs(_with_streams([_render("r1")], [stream]))

    assert "telemetry monotonic time runs backwards" in obs.integrity_warnings


def test_foreign_session_provenance_is_flagged() -> None:
    obs = _obs(_with_streams([_render("r1")], [], session_hashes={"s1": "other"}))

    assert any("provenance" in w for w in obs.integrity_warnings)


def test_outputs_carry_configuration_identity() -> None:
    variables = derive_case_variables(_inputs())

    assert (variables.config_version, variables.config_hash, variables.study_id) == (
        "v1",
        "h",
        "study",
    )


# ---------------------------------------------------------------------------
# Integration: the real app and loader
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry_study(harness: Harness):  # noqa: F811
    config = harness.variant("v2", telemetry=TELEMETRY)
    harness.activate(config)
    return harness, config


def _table_counts(client: Any) -> dict[str, int]:
    tables = ("events", "answers", "arm_assignments", "progress", "case_lifecycle", "sessions")
    db = client.app.state.db
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}


def _start_at_frontier(study: Harness, client: Any) -> tuple[str, str, str]:
    """Start the next case and GET its first timepoint: (patient, arm, render_id)."""
    patient_id = _started_patient(_start(client))
    render_id = _view(client.get(f"/patient/{patient_id}/timepoint/0").text)["data-render-id"]
    arm = {a.patient_id: a.arm for a in study.assignments()}[patient_id]
    return patient_id, arm, render_id


AI_MOUNT = {"panel_id": "ai", "expanded": True, "collapsible": True, "state": "loading"}


def _viewed_ai_batch(render_id: str) -> list[dict]:
    return [
        _event(render_id, 1, "browser.timepoint_enter"),
        _event(render_id, 2, "panel.mount", payload=AI_MOUNT),
        _event(
            render_id, 3, "panel.viewport", payload={"panel_id": "ai", "intersection_ratio": 0.8}
        ),
        _event(render_id, 60, "browser.timepoint_exit"),
    ]


def test_loader_classifies_both_arms_and_writes_nothing(telemetry_study: Any) -> None:
    study, config = telemetry_study
    with study.boot(config) as client:
        cases: dict[str, str] = {}
        for _ in range(2):
            patient_id, arm, render_id = _start_at_frontier(study, client)
            cases[arm] = patient_id
            if arm == AI:
                _post(client, _viewed_ai_batch(render_id))
            seed_progress(client, patient_id, len(TIMEPOINTS) - 1, completed=True)
        before = _table_counts(client)
        db = client.app.state.db
        ai_case = load_case_variables(db, study.clinician_id, cases[AI])
        no_ai_case = load_case_variables(db, study.clinician_id, cases[NO_AI])
        after = _table_counts(client)
        arms = {a.patient_id: a.arm for a in study.assignments()}

    assert before == after
    assert ai_case is not None and no_ai_case is not None
    assert (ai_case.arm, no_ai_case.arm) == (AI, NO_AI)
    assert arms == {cases[AI]: AI, cases[NO_AI]: NO_AI}

    ai_obs = ai_case.observations[0]
    assert ai_obs.ai_delivered is True
    assert ai_obs.ai_exposure_seconds == pytest.approx(5.7)  # viewport 300 ms → exit 6000 ms
    assert ai_obs.ai_viewed is True and ai_obs.pp_compliant is True

    no_ai_obs = no_ai_case.observations[0]
    assert no_ai_obs.intervention_leakage is False and no_ai_obs.pp_compliant is True
    assert ai_case.config_version == "v2"


def test_loader_ignores_non_phase2_pairs(telemetry_study: Any) -> None:
    study, config = telemetry_study
    with study.boot(config) as client:
        result = load_case_variables(client.app.state.db, study.clinician_id, "synth_001")

    assert result is None


def test_forged_mount_cannot_make_no_ai_case_ai(telemetry_study: Any) -> None:
    study, config = telemetry_study
    with study.boot(config) as client:
        patient_id, arm, render_id = _start_at_frontier(study, client)
        if arm == AI:  # block length 1: the next case holds the other arm
            seed_progress(client, patient_id, len(TIMEPOINTS) - 1, completed=True)
            patient_id, arm, render_id = _start_at_frontier(study, client)
        response = _post(client, [_event(render_id, 1, "panel.mount", payload=AI_MOUNT)])
        variables = load_case_variables(client.app.state.db, study.clinician_id, patient_id)

    assert arm == NO_AI
    assert response.status_code == 204
    assert variables is not None and variables.arm == NO_AI
    assert variables.observations[0].intervention_leakage is True
    assert variables.observations[0].pp_compliant is False


def test_loader_reads_progress_and_answers(telemetry_study: Any) -> None:
    study, config = telemetry_study
    with study.boot(config) as client:
        patient_id = _started_patient(_start(client))
        client.get(f"/patient/{patient_id}/timepoint/0")
        answer_all_required(client, patient_id, 0)
        client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        variables = load_case_variables(client.app.state.db, study.clinician_id, patient_id)

    assert variables is not None
    statuses = {(r.t_index, r.status) for r in variables.responses if r.required}
    assert (0, ResponseStatus.ANSWERED) in statuses
    t1 = [r for r in variables.responses if r.t_index == 1 and r.required]
    assert t1 and all(r.missing_reason is MissingReason.REACHED_UNANSWERED for r in t1)
    t2 = [r for r in variables.responses if r.t_index == 2]
    assert all(r.missing_reason is MissingReason.PENDING for r in t2)
    assert [o.reached for o in variables.observations] == [True, True, False]
