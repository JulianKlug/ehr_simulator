"""S11h: conditional question engine and the first use case question set.

Route tests run Phase 1 study mode (``study_synthetic.yaml``) on the
first use case questions — branching is not Phase 2 specific:

    deterioration_6h ── Yes ──▶ primary_cause shown + required
    good_outcome_3mo ── Yes ──▶ death_3mo = No (rule row, read only)
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ehr_simulator.config import (
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
    parse_questions_snapshot,
    render_questions_snapshot,
)
from ehr_simulator.config.questions import Questions
from ehr_simulator.question_branching import (
    AnswerSource,
    QuestionState,
    StoredAnswer,
    WriteReason,
    evaluate,
    plan_submission,
)
from ehr_simulator.web.app import app_from_study_config
from tests.conftest import _activate_configuration, _seed_clinician

REPO = Path(__file__).parent.parent
FIRST_USE_CASE = REPO / "configs" / "phase2_first_use_case_questions.yaml"
FIXTURES = Path(__file__).parent / "fixtures" / "study"
V1_QUESTIONS = FIXTURES / "questions.yaml"
STUDY = FIXTURES / "study_synthetic.yaml"
PID = "synth_001"
T0 = 0.0
ANSWER_URL = f"/patient/{PID}/timepoint/0/answer"
ADVANCE_URL = f"/patient/{PID}/timepoint/0/advance"
PAGE_URL = f"/patient/{PID}/timepoint/0"
HX = {"HX-Request": "true"}
HTTP_OK = 200
HTTP_CONFLICT = 409
EXPECTED_IDS = ["deterioration_6h", "confidence", "primary_cause", "good_outcome_3mo", "death_3mo"]
CAUSE = "Placeholder cause A"


@pytest.fixture(scope="module")
def fuc() -> Questions:
    return load_questions(FIRST_USE_CASE)


def _fuc_dict() -> dict[str, Any]:
    return yaml.safe_load(FIRST_USE_CASE.read_text())


def _questions(data: dict[str, Any]) -> Questions:
    return Questions.model_validate(data)


def _with(question_id: str, **changes: Any) -> dict[str, Any]:
    data = _fuc_dict()
    for q in data["questions"]:
        if q["question_id"] == question_id:
            q.update(changes)
    return data


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_v1_snapshot_round_trips_byte_identically() -> None:
    questions = load_questions(V1_QUESTIONS)
    rendered = render_questions_snapshot(questions)
    for key in ("display_if", "auto_value", "scale_labels"):
        assert key not in rendered
    assert render_questions_snapshot(parse_questions_snapshot(rendered)) == rendered


def test_v2_conditional_config_loads(fuc: Questions) -> None:
    assert fuc.schema_version == "2"
    cause = next(q for q in fuc.questions if q.question_id == "primary_cause")
    assert cause.display_if is not None
    assert cause.display_if.equals == "Yes"


def test_v2_snapshot_round_trips(fuc: Questions) -> None:
    rendered = render_questions_snapshot(fuc)
    assert parse_questions_snapshot(rendered) == fuc


@pytest.mark.parametrize(
    ("question_id", "changes", "match"),
    [
        ("primary_cause", {"display_if": {"question_id": "nope", "equals": "Yes"}}, "earlier"),
        (
            "primary_cause",
            {"display_if": {"question_id": "primary_cause", "equals": "Yes"}},
            "itself",
        ),
        (
            "confidence",
            {"display_if": {"question_id": "good_outcome_3mo", "equals": "Yes"}},
            "earlier",
        ),
        (
            "good_outcome_3mo",
            {"display_if": {"question_id": "confidence", "equals": "3"}},
            "categorical",
        ),
        (
            "primary_cause",
            {"display_if": {"question_id": "deterioration_6h", "equals": "Maybe"}},
            "not an option",
        ),
        (
            "death_3mo",
            {
                "auto_value": {
                    "when": {"question_id": "good_outcome_3mo", "equals": "Yes"},
                    "value": "Maybe",
                }
            },
            "not an option",
        ),
    ],
    ids=["unknown", "self", "forward", "non_categorical", "bad_equals", "bad_auto_value"],
)
def test_invalid_conditions_rejected(question_id: str, changes: dict, match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _questions(_with(question_id, **changes))


def test_auto_value_on_free_text_rejected() -> None:
    data = _fuc_dict()
    data["questions"].append(
        {
            "question_id": "notes",
            "prompt": "Notes",
            "response_type": "free-text",
            "auto_value": {
                "when": {"question_id": "deterioration_6h", "equals": "No"},
                "value": "x",
            },
        }
    )
    with pytest.raises(ValidationError, match="single-choice or numeric"):
        _questions(data)


def test_likert_auto_value_must_be_in_range() -> None:
    rule = {"when": {"question_id": "deterioration_6h", "equals": "No"}}
    _questions(_with("confidence", auto_value={**rule, "value": 3}))
    with pytest.raises(ValidationError, match="integer in 1..5"):
        _questions(_with("confidence", auto_value={**rule, "value": 9}))


def test_v2_fields_rejected_under_v1() -> None:
    data = _fuc_dict()
    data["schema_version"] = "1"
    with pytest.raises(ValidationError, match='schema_version: "2"'):
        _questions(data)


def test_scale_labels_wrong_length_rejected() -> None:
    with pytest.raises(ValidationError, match="one label per point"):
        _questions(_with("confidence", scale_labels=["a", "b"]))


def test_changing_a_condition_changes_config_hash(fuc: Questions) -> None:
    study = load_study_config(STUDY)
    changed = _questions(
        _with("primary_cause", display_if={"question_id": "deterioration_6h", "equals": "No"})
    )
    assert compute_config_hash_from_models(study, fuc) != compute_config_hash_from_models(
        study, changed
    )


# ---------------------------------------------------------------------------
# Evaluation (pure)
# ---------------------------------------------------------------------------


def _state(fuc: Questions, values: dict[str, Any], question_id: str) -> Any:
    item = evaluate(fuc, values).get(question_id)
    assert item is not None
    return item


def test_deterioration_yes_shows_required_cause(fuc: Questions) -> None:
    item = _state(fuc, {"deterioration_6h": "Yes"}, "primary_cause")
    assert item.state is QuestionState.EDITABLE
    assert item.required_now


@pytest.mark.parametrize("values", [{"deterioration_6h": "No"}, {}])
def test_deterioration_no_or_blank_hides_cause(fuc: Questions, values: dict) -> None:
    item = _state(fuc, values, "primary_cause")
    assert item.state is QuestionState.HIDDEN
    assert not item.required_now


def test_good_outcome_yes_derives_death_no(fuc: Questions) -> None:
    item = _state(fuc, {"good_outcome_3mo": "Yes", "death_3mo": "Yes"}, "death_3mo")
    assert item.state is QuestionState.DERIVED
    assert item.value == "No"
    assert not item.required_now


def test_good_outcome_no_requires_death(fuc: Questions) -> None:
    item = _state(fuc, {"good_outcome_3mo": "No"}, "death_3mo")
    assert item.state is QuestionState.EDITABLE
    assert item.required_now


def test_condition_on_hidden_source_is_false() -> None:
    data = _fuc_dict()
    data["questions"].append(
        {
            "question_id": "cause_detail",
            "prompt": "Detail",
            "response_type": "categorical",
            "options": ["a", "b"],
            "display_if": {"question_id": "primary_cause", "equals": CAUSE},
        }
    )
    questions = _questions(data)
    # A stale cause row does not open the chain while deterioration is No.
    values = {"deterioration_6h": "No", "primary_cause": CAUSE}
    assert evaluate(questions, values).get("cause_detail").state is QuestionState.HIDDEN


def test_plan_submission_reconciles_generically(fuc: Questions) -> None:
    stored = {
        "deterioration_6h": StoredAnswer("Yes", AnswerSource.CLINICIAN),
        "primary_cause": StoredAnswer(CAUSE, AnswerSource.CLINICIAN),
    }
    writes = plan_submission(fuc, stored, "deterioration_6h", "No")
    assert [(w.question_id, w.value, w.reason) for w in writes] == [
        ("deterioration_6h", "No", WriteReason.USER_CHANGE),
        ("primary_cause", None, WriteReason.BRANCH_INVALIDATED),
    ]


# ---------------------------------------------------------------------------
# Service + routes
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    db_path = tmp_path / "study.db"
    study = load_study_config(STUDY)
    clinician_id = _seed_clinician(db_path, study_id=study.study_id)
    _activate_configuration(db_path, STUDY, FIRST_USE_CASE, version="v1", description="s11h")
    app = app_from_study_config(
        STUDY,
        FIRST_USE_CASE,
        log_dir=tmp_path / "logs",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )
    with TestClient(app) as test_client:
        test_client.cookies.set("ehrsim_clinician_id", clinician_id)
        test_client.get(PAGE_URL)  # opens the session
        yield test_client


def _post(client: TestClient, question_id: str, value: str | None) -> Any:
    data = {"question_id": question_id}
    if value is not None:
        data["value"] = value
    return client.post(ANSWER_URL, data=data, headers=HX)


def _rows(client: TestClient) -> dict[str, tuple[str, str, str | None]]:
    rows = client.app.state.db.execute(
        "SELECT question_id, value, answer_source, derived_from_question_id FROM answers"
    ).fetchall()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _answer_events(client: TestClient) -> list[dict[str, Any]]:
    rows = client.app.state.db.execute(
        "SELECT kind, payload_json FROM events WHERE kind LIKE 'answer.%' ORDER BY event_id"
    ).fetchall()
    return [{"kind": r[0], **json.loads(r[1])} for r in rows]


def _slot_state(html: str, question_id: str) -> str | None:
    slot = BeautifulSoup(html, "html.parser").select_one(f"#q-slot-{question_id}")
    return slot["data-q-state"] if slot else None


def test_yes_then_no_deletes_saved_cause_with_audit_event(client: TestClient) -> None:
    assert _post(client, "deterioration_6h", "Yes").status_code == HTTP_OK
    assert _post(client, "primary_cause", CAUSE).status_code == HTTP_OK
    assert _post(client, "deterioration_6h", "No").status_code == HTTP_OK

    assert "primary_cause" not in _rows(client)
    clear = [e for e in _answer_events(client) if e["kind"] == "answer.clear"]
    assert clear == [
        {
            "kind": "answer.clear",
            "question_id": "primary_cause",
            "response_type": "categorical",
            "deleted": True,
            "source": "clinician",
            "reason": "branch_invalidated",
        }
    ]
    assert CAUSE not in json.dumps(_answer_events(client))


def _answer_required_except(client: TestClient, *skip: str) -> None:
    values = {
        "deterioration_6h": "No",
        "confidence": "3",
        "good_outcome_3mo": "No",
        "death_3mo": "Yes",
    }
    for qid, value in values.items():
        if qid not in skip:
            assert _post(client, qid, value).status_code == HTTP_OK


def test_hidden_cause_does_not_block_advance(client: TestClient) -> None:
    _answer_required_except(client)
    response = client.post(ADVANCE_URL, headers=HX)
    assert response.status_code == HTTP_OK


def test_post_to_hidden_cause_refused(client: TestClient) -> None:
    response = _post(client, "primary_cause", CAUSE)
    assert response.status_code == HTTP_CONFLICT
    assert "is not shown" in response.text
    assert _rows(client) == {}


def test_good_outcome_yes_writes_rule_death(client: TestClient) -> None:
    _post(client, "good_outcome_3mo", "Yes")
    assert _rows(client)["death_3mo"] == ("No", "rule", "good_outcome_3mo")
    rule_event = _answer_events(client)[-1]
    assert (rule_event["question_id"], rule_event["source"], rule_event["reason"]) == (
        "death_3mo",
        "rule",
        "auto_value",
    )


def test_rule_death_satisfies_gate(client: TestClient) -> None:
    _answer_required_except(client, "good_outcome_3mo", "death_3mo")
    _post(client, "good_outcome_3mo", "Yes")
    assert client.post(ADVANCE_URL, headers=HX).status_code == HTTP_OK


def test_post_to_derived_death_refused(client: TestClient) -> None:
    _post(client, "good_outcome_3mo", "Yes")
    response = _post(client, "death_3mo", "Yes")
    assert response.status_code == HTTP_CONFLICT
    assert "set automatically" in response.text
    assert _rows(client)["death_3mo"] == ("No", "rule", "good_outcome_3mo")


def test_yes_to_no_clears_rule_death_and_blocks(client: TestClient) -> None:
    _answer_required_except(client, "good_outcome_3mo", "death_3mo")
    _post(client, "good_outcome_3mo", "Yes")
    _post(client, "good_outcome_3mo", "No")

    assert "death_3mo" not in _rows(client)
    blocked = client.post(ADVANCE_URL, headers=HX)
    assert blocked.status_code == HTTP_CONFLICT
    assert _post(client, "death_3mo", "Yes").status_code == HTTP_OK
    assert client.post(ADVANCE_URL, headers=HX).status_code == HTTP_OK


def test_overwritten_clinician_death_is_not_restored(client: TestClient) -> None:
    _post(client, "good_outcome_3mo", "No")
    _post(client, "death_3mo", "Yes")
    _post(client, "good_outcome_3mo", "Yes")
    assert _rows(client)["death_3mo"] == ("No", "rule", "good_outcome_3mo")

    _post(client, "good_outcome_3mo", "No")
    assert "death_3mo" not in _rows(client)


def test_clearing_controlling_answer_reevaluates(client: TestClient) -> None:
    _post(client, "good_outcome_3mo", "Yes")
    _post(client, "good_outcome_3mo", None)
    assert _rows(client) == {}


def test_answer_response_swaps_changed_slots_only(client: TestClient) -> None:
    html = _post(client, "deterioration_6h", "Yes").text
    soup = BeautifulSoup(html, "html.parser")
    swapped = [s["id"] for s in soup.select("[hx-swap-oob].question-slot")]
    assert swapped == ["q-slot-primary_cause"]
    assert _slot_state(html, "primary_cause") == "editable"

    unchanged = _post(client, "confidence", "4").text
    assert not BeautifulSoup(unchanged, "html.parser").select(".question-slot")


def test_trigger_refuses_inconsistent_source_rows(client: TestClient) -> None:
    import sqlite3

    db = client.app.state.db
    with pytest.raises(sqlite3.IntegrityError, match="derived_from_question_id"):
        db.execute(
            "INSERT INTO answers (clinician_id, patient_id, timepoint, question_id, value, arm, "
            "config_hash, answer_source) VALUES ('c', 'p', 0, 'q', 'v', 'no_ai', 'h', 'rule')"
        )
    db.rollback()


# ---------------------------------------------------------------------------
# Atomicity
# ---------------------------------------------------------------------------


def _fail_on(monkeypatch: pytest.MonkeyPatch, target: str, question_id: str) -> None:
    """Make ``answer_capture.<target>`` raise for one dependent question."""
    from ehr_simulator.web import answer_capture

    module, name = target.split(".")
    owner = getattr(answer_capture, module)
    real = getattr(owner, name)

    def failing(*args: Any, **kwargs: Any) -> Any:
        payload = kwargs.get("payload") or {}
        if question_id in (kwargs.get("question_id"), payload.get("question_id")):
            raise RuntimeError("injected failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, failing)


@pytest.mark.parametrize(
    ("setup", "target", "failing_qid", "trigger"),
    [
        (
            [("deterioration_6h", "Yes"), ("primary_cause", CAUSE)],
            "answers.delete_one",
            "primary_cause",
            ("deterioration_6h", "No"),
        ),
        ([], "answers.upsert", "death_3mo", ("good_outcome_3mo", "Yes")),
        ([], "events.append", "death_3mo", ("good_outcome_3mo", "Yes")),
    ],
    ids=["dependent_delete", "rule_upsert", "event_append"],
)
def test_failed_dependent_write_rolls_back_everything(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    setup: list[tuple[str, str]],
    target: str,
    failing_qid: str,
    trigger: tuple[str, str],
) -> None:
    for qid, value in setup:
        _post(client, qid, value)
    before_rows = _rows(client)
    before_events = len(_answer_events(client))

    _fail_on(monkeypatch, target, failing_qid)
    with pytest.raises(RuntimeError, match="injected failure"):
        _post(client, *trigger)

    assert _rows(client) == before_rows
    assert len(_answer_events(client)) == before_events


def test_write_counter_bumped_once_after_commit(client: TestClient) -> None:
    before = client.app.state.write_counter
    _post(client, "good_outcome_3mo", "Yes")  # clinician row + rule row
    assert client.app.state.write_counter == before + 1


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_branch_state_survives_refresh(client: TestClient) -> None:
    _post(client, "deterioration_6h", "Yes")
    _post(client, "good_outcome_3mo", "Yes")
    html = client.get(PAGE_URL).text

    assert _slot_state(html, "primary_cause") == "editable"
    assert _slot_state(html, "death_3mo") == "derived"
    death = BeautifulSoup(html, "html.parser").select_one("#q-slot-death_3mo")
    assert death.select_one('input[value="No"]').has_attr("checked")
    assert death.select_one("fieldset").has_attr("disabled")
    assert "Set automatically" in death.text


def test_branch_state_survives_htmx_navigation(client: TestClient) -> None:
    _answer_required_except(client, "deterioration_6h")
    _post(client, "deterioration_6h", "Yes")
    _post(client, "primary_cause", CAUSE)
    assert client.post(ADVANCE_URL, headers=HX).status_code == HTTP_OK

    back = client.get(PAGE_URL, headers=HX).text
    assert _slot_state(back, "primary_cause") == "editable"  # state, read only in a locked pane
    pane = BeautifulSoup(back, "html.parser").select_one("#q-slot-primary_cause")
    assert pane.select_one(f'input[value="{CAUSE}"]').has_attr("checked")


def test_hidden_slot_is_empty(client: TestClient) -> None:
    html = client.get(PAGE_URL).text
    slot = BeautifulSoup(html, "html.parser").select_one("#q-slot-primary_cause")
    assert slot["data-q-state"] == "hidden"
    assert slot.select_one("form") is None


# ---------------------------------------------------------------------------
# First use case fixture
# ---------------------------------------------------------------------------


def test_fixture_holds_exactly_the_five_questions(fuc: Questions) -> None:
    assert [q.question_id for q in fuc.questions] == EXPECTED_IDS


def test_fixture_question_shapes(fuc: Questions) -> None:
    by_id = {q.question_id: q for q in fuc.questions}
    assert by_id["deterioration_6h"].options == ["Yes", "No"]
    confidence = by_id["confidence"]
    assert (confidence.response_type, confidence.scale_min, confidence.scale_max) == (
        "likert",
        1,
        5,
    )
    assert len(confidence.scale_labels) == 5
    assert by_id["primary_cause"].response_type == "categorical"
    assert by_id["primary_cause"].display_if is not None
    assert by_id["good_outcome_3mo"].options == ["Yes", "No"]
    assert by_id["death_3mo"].options == ["Yes", "No"]
    assert by_id["death_3mo"].auto_value.value == "No"


def test_fixture_drops_phase1_questions(fuc: Questions) -> None:
    ids = {q.question_id for q in fuc.questions}
    assert not ids & {"survives_hospital", "dead_6mo", "contributing_factors", "free_notes"}
    assert all(q.response_type not in {"free-text", "multi-select"} for q in fuc.questions)


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def test_v1_gating_unchanged() -> None:
    questions = load_questions(V1_QUESTIONS)
    required = [q.question_id for q in questions.questions if q.required]
    assert evaluate(questions, {}).remaining == tuple(required)


def test_v1_and_v2_pinned_cases_coexist(tmp_path: Path) -> None:
    db_path = tmp_path / "study.db"
    clinician_id = _seed_clinician(db_path, study_id=load_study_config(STUDY).study_id)

    def boot(questions_path: Path) -> TestClient:
        app = app_from_study_config(
            STUDY,
            questions_path,
            log_dir=tmp_path / "logs",
            db_path=db_path,
            backup_dir=tmp_path / "backups",
        )
        client = TestClient(app)
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        return client

    _activate_configuration(db_path, STUDY, V1_QUESTIONS, version="v1", description="v1")
    with boot(V1_QUESTIONS) as client:
        client.get(PAGE_URL)  # pins synth_001 to v1

    _activate_configuration(db_path, STUDY, FIRST_USE_CASE, version="v2", description="v2")
    with boot(FIRST_USE_CASE) as client:
        v1_page = client.get(PAGE_URL).text
        v2_page = client.get("/patient/synth_002/timepoint/0").text

    assert "q-slot-free_notes" in v1_page
    assert "q-slot-primary_cause" not in v1_page
    assert "q-slot-primary_cause" in v2_page
    assert "q-slot-free_notes" not in v2_page
