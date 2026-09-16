"""``POST /patient/{pid}/timepoint/{t}/answer`` + the questions pane on GET
(specs/session-09a-answer-capture.md §9 #17-#32d).

``study_client`` boots through ``app_from_study_config`` on the synthetic
study (timepoints 0/60/180 min) + the 7-question fixture; ``client`` is the
bare no-study app used for the 409 / no-pane branches.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from ehr_simulator.db import answers
from tests.conftest import seed_progress

PID = "synth_001"
# S9b: a fresh study DB's frontier is t=0 — the only timepoint that takes answers.
T_INDEX = 0
T_MINUTES = 0.0
FIXTURE_QUESTION_COUNT = 7


def _url(t_index: int = T_INDEX, pid: str = PID) -> str:
    return f"/patient/{pid}/timepoint/{t_index}/answer"


def _db(client: TestClient) -> sqlite3.Connection:
    return client.app.state.db  # type: ignore[attr-defined]


def _answer_rows(client: TestClient) -> list[tuple]:
    return [
        tuple(r)
        for r in _db(client).execute(
            "SELECT question_id, value, arm, config_hash, timepoint FROM answers"
        )
    ]


def _count(client: TestClient, table: str, where: str = "1=1") -> int:
    return _db(client).execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _state(html: str) -> str | None:
    span = BeautifulSoup(html, "html.parser").select_one(".answer-status")
    return None if span is None else span.get("data-state")


def _pane(client: TestClient, t_index: int = 0, **headers: str) -> BeautifulSoup:
    r = client.get(f"/patient/{PID}/timepoint/{t_index}", headers=headers)
    assert r.status_code == 200
    return BeautifulSoup(r.text, "html.parser")


# ---------------------------------------------------------------------------
# POST happy paths (#17, #18, #19, #22, #23, #27)
# ---------------------------------------------------------------------------


def test_post_answer_saves_row_with_arm_and_config_hash(study_client: TestClient) -> None:
    r = study_client.post(_url(), data={"question_id": "deterioration_6h", "value": "No"})
    assert r.status_code == 200
    assert _state(r.text) == "saved"
    assert "Saved" in r.text

    config_hash = study_client.app.state.config_hash  # type: ignore[attr-defined]
    assert _answer_rows(study_client) == [
        ("deterioration_6h", "No", "no_ai", config_hash, T_MINUTES)
    ]


def test_post_answer_twice_same_cell_one_row(study_client: TestClient) -> None:
    """REGRESSION: S6 #12 at the route level (network-retry double submit)."""
    study_client.post(_url(), data={"question_id": "deterioration_6h", "value": "Yes"})
    study_client.post(_url(), data={"question_id": "deterioration_6h", "value": "No"})
    rows = _answer_rows(study_client)
    assert len(rows) == 1
    assert rows[0][1] == "No"


def test_post_answer_multi_select_json_encoded(study_client: TestClient) -> None:
    r = study_client.post(
        _url(), data={"question_id": "contributing_factors", "value": ["Labs", "Imaging"]}
    )
    assert r.status_code == 200
    assert _answer_rows(study_client)[0][1] == '["Imaging","Labs"]'


def test_post_answer_empty_clears_and_returns_cleared(study_client: TestClient) -> None:
    study_client.post(_url(), data={"question_id": "free_notes", "value": "a note"})
    assert _count(study_client, "answers") == 1

    r = study_client.post(_url(), data={"question_id": "free_notes", "value": ""})
    assert r.status_code == 200
    assert _state(r.text) == "cleared"
    assert _count(study_client, "answers") == 0
    assert _count(study_client, "events", "kind = 'answer.clear'") == 1


def test_post_answer_emits_event_with_client_fields(study_client: TestClient) -> None:
    study_client.post(
        _url(),
        data={
            "question_id": "confidence",
            "value": "4",
            "client_ts": "2026-09-16T12:34:56.789Z",
            "client_seq": "11",
        },
    )
    row = (
        _db(study_client)
        .execute(
            "SELECT session_id, patient_id, timepoint, client_ts, client_seq, payload_json "
            "FROM events WHERE kind = 'answer.upsert'"
        )
        .fetchone()
    )
    session_id, patient_id, timepoint, client_ts, client_seq, payload_json = row
    assert session_id is not None
    assert (patient_id, timepoint, client_seq) == (PID, T_MINUTES, 11)
    assert str(client_ts) == "2026-09-16 12:34:56.789000"
    assert json.loads(payload_json)["question_id"] == "confidence"


def test_post_answer_bootstraps_session_when_get_skipped(study_client: TestClient) -> None:
    study_client.post(_url(), data={"question_id": "confidence", "value": "2"})
    study_client.post(_url(), data={"question_id": "confidence", "value": "3"})
    assert _count(study_client, "sessions") == 1
    assert _count(study_client, "arm_assignments") == 1
    assert _count(study_client, "events", "kind = 'session.start'") == 1


# ---------------------------------------------------------------------------
# POST rejections (#20, #21, #21b, #24, #25, #26, #32d)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question_id", "value"),
    [
        ("deterioration_6h", "Maybe"),
        ("confidence", "9"),
        ("good_outcome_3mo", "101"),
        ("free_notes", "x" * 4001),
    ],
    ids=["bad_option", "likert_9", "prob_101", "free_text_too_long"],
)
def test_post_answer_invalid_value_422_no_row(
    study_client: TestClient, question_id: str, value: str
) -> None:
    r = study_client.post(_url(), data={"question_id": question_id, "value": value})
    assert r.status_code == 422
    assert _state(r.text) == "error"
    assert BeautifulSoup(r.text, "html.parser").get_text(strip=True)
    assert _count(study_client, "answers") == 0


def test_post_answer_unknown_question_422(study_client: TestClient) -> None:
    r = study_client.post(_url(), data={"question_id": "nope", "value": "1"})
    assert r.status_code == 422
    assert _state(r.text) == "error"
    assert "Unknown question &#39;nope&#39;" in r.text or "Unknown question 'nope'" in r.text
    assert _count(study_client, "answers") == 0


def test_post_answer_missing_question_id_422(study_client: TestClient) -> None:
    r = study_client.post(_url(), data={"value": "No"})
    assert r.status_code == 422
    assert "Missing question_id" in r.text
    assert _count(study_client, "answers") == 0

    # Sent twice: first wins, one row.
    r2 = study_client.post(
        _url(), data={"question_id": ["deterioration_6h", "survives_hospital"], "value": "No"}
    )
    assert r2.status_code == 200
    assert [row[0] for row in _answer_rows(study_client)] == ["deterioration_6h"]


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_post_answer_no_cookie_redirects(anonymous_client: TestClient, htmx: bool) -> None:
    headers = {"HX-Request": "true"} if htmx else {}
    r = anonymous_client.post(
        _url(),
        data={"question_id": "deterioration_6h", "value": "No"},
        headers=headers,
        follow_redirects=False,
    )
    if htmx:
        assert r.status_code == 200
        assert r.headers["HX-Redirect"] == "/login"
        return
    assert r.status_code == 303
    assert r.headers["location"] == "/login"


def test_post_answer_no_questions_configured_409(client: TestClient) -> None:
    r = client.post(_url(), data={"question_id": "deterioration_6h", "value": "No"})
    assert r.status_code == 409
    assert _state(r.text) == "error"
    assert "No questions configured" in r.text
    assert _count(client, "answers") == 0


@pytest.mark.parametrize(
    ("pid", "t_index", "fragment"),
    [
        ("synth_999", 0, "not part of this study"),
        (PID, 99, "out of range"),
    ],
    ids=["patient_not_in_study", "t_index_out_of_range"],
)
def test_post_answer_bad_target_404(
    study_client: TestClient, pid: str, t_index: int, fragment: str
) -> None:
    r = study_client.post(_url(t_index, pid), data={"question_id": "confidence", "value": "3"})
    assert r.status_code == 404
    assert _state(r.text) == "error"
    assert fragment in r.text
    assert _count(study_client, "answers") == 0
    assert _count(study_client, "sessions") == 0


def test_post_answer_bad_target_unknown_patient_404(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path, study_fixture_dir: Path
) -> None:
    """A patient listed in the study but absent from the dataset (dataset-
    membership branch, distinct from study-membership)."""
    import yaml

    from ehr_simulator.web.app import app_from_study_config
    from tests.conftest import _seed_clinician

    study = yaml.safe_load((study_fixture_dir / "study_synthetic.yaml").read_text())
    study["patient_ids"].append("ghost_patient")
    study_path = tmp_log_dir.parent / "study.yaml"
    study_path.write_text(yaml.safe_dump(study))

    cid = _seed_clinician(tmp_db_path)
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as c:
        c.cookies.set("ehrsim_clinician_id", cid)
        r = c.post(_url(0, "ghost_patient"), data={"question_id": "confidence", "value": "3"})
        assert r.status_code == 404
        assert "not found" in r.text
        assert _count(c, "sessions") == 0


def test_post_answer_404_body_is_status_fragment_not_error_flash(
    study_client: TestClient,
) -> None:
    post = study_client.post(_url(99), data={"question_id": "confidence", "value": "3"})
    get = study_client.get(f"/patient/{PID}/timepoint/99")
    assert post.status_code == get.status_code == 404
    assert 'data-state="error"' in post.text
    assert "error-flash" not in post.text
    assert 'class="error-flash"' in get.text
    assert "Timepoint t_index=99 out of range (valid: 0…2)" in get.text


# ---------------------------------------------------------------------------
# GET: pane rendering + pre-fill + bootstrap (#28-#32c)
# ---------------------------------------------------------------------------


def test_get_patient_renders_questions_pane(study_client: TestClient) -> None:
    soup = _pane(study_client, T_INDEX)
    assert soup.select_one("#questions-pane") is not None
    forms = soup.select("form.question")
    assert len(forms) == FIXTURE_QUESTION_COUNT
    expected_types = {
        "deterioration_6h": "categorical",
        "good_outcome_3mo": "probability-0-100",
        "confidence": "likert",
        "contributing_factors": "multi-select",
        "free_notes": "free-text",
    }
    for form in forms:
        assert form["hx-post"] == _url(T_INDEX)
        qid = form["data-question-id"]
        if qid in expected_types:
            assert form["data-response-type"] == expected_types[qid]


def test_questions_pane_hx_trigger_per_response_type(study_client: TestClient) -> None:
    for form in _pane(study_client).select("form.question"):
        trigger = form["hx-trigger"]
        assert "submit" in trigger
        assert form["hx-target"] == "find .answer-status"
        if form["data-response-type"] == "free-text":
            assert "input changed delay:1500ms" in trigger
        else:
            assert trigger == "change, submit"


def _seed_cell(client: TestClient, clinician_id: str, t_minutes: float) -> None:
    common = {
        "clinician_id": clinician_id,
        "patient_id": PID,
        "timepoint": t_minutes,
        "arm": "no_ai",
        "config_hash": "h",
    }
    db = _db(client)
    answers.upsert(db, question_id="deterioration_6h", value="No", **common)
    answers.upsert(db, question_id="good_outcome_3mo", value="65", **common)
    answers.upsert(db, question_id="contributing_factors", value='["Imaging","Labs"]', **common)
    answers.upsert(db, question_id="free_notes", value="typed earlier", **common)


def test_get_patient_prefills_saved_answers(
    study_client: TestClient, study_clinician_id: str
) -> None:
    _seed_cell(study_client, study_clinician_id, T_MINUTES)
    soup = _pane(study_client, T_INDEX)

    radio = soup.select_one('form[data-question-id="deterioration_6h"] input[value="No"]')
    assert radio is not None and radio.has_attr("checked")
    unchecked = soup.select_one('form[data-question-id="deterioration_6h"] input[value="Yes"]')
    assert not unchecked.has_attr("checked")

    number = soup.select_one('form[data-question-id="good_outcome_3mo"] input[type="number"]')
    assert number["value"] == "65"

    checked = {
        box["value"]
        for box in soup.select('form[data-question-id="contributing_factors"] input[checked]')
    }
    assert checked == {"Imaging", "Labs"}

    textarea = soup.select_one('form[data-question-id="free_notes"] textarea')
    assert textarea.get_text() == "typed earlier"

    states = {
        form["data-question-id"]: form.select_one(".answer-status")["data-state"]
        for form in soup.select("form.question")
    }
    saved = {k for k, v in states.items() if v == "saved"}
    assert saved == {"deterioration_6h", "good_outcome_3mo", "contributing_factors", "free_notes"}
    assert all(v == "blank" for k, v in states.items() if k not in saved)


def test_get_patient_prefill_does_not_leak_other_timepoints(
    study_client: TestClient, study_clinician_id: str
) -> None:
    """REGRESSION: the "data ≤ t" invariant applied to the clinician's own answers."""
    _seed_cell(study_client, study_clinician_id, 180.0)  # t_index=2
    soup = _pane(study_client, 0)
    pane = soup.select_one("#questions-pane")
    assert not pane.select("input[checked]")
    assert all(not (i.get("value") or "") for i in pane.select('input[type="number"]'))
    assert all(not t.get_text(strip=True) for t in pane.select("textarea"))
    assert {s["data-state"] for s in pane.select(".answer-status")} == {"blank"}


def test_get_patient_bootstraps_session_and_binds_arm(
    study_client: TestClient, tmp_log_dir: Path
) -> None:
    _pane(study_client, 0)
    assert _count(study_client, "sessions") == 1
    assert _count(study_client, "arm_assignments") == 1
    assert _count(study_client, "events", "kind = 'session.start'") == 1

    lines = [
        json.loads(line)
        for line in (tmp_log_dir / "current.jsonl").read_text().splitlines()
        if line.strip()
    ]
    request_lines = [entry for entry in lines if entry.get("path") == f"/patient/{PID}/timepoint/0"]
    assert request_lines
    assert request_lines[-1]["arm"] == "no_ai"

    _pane(study_client, 0)
    assert _count(study_client, "sessions") == 1
    assert _count(study_client, "events", "kind = 'session.start'") == 1


def test_get_patient_without_study_has_no_pane(client: TestClient) -> None:
    soup = _pane(client, 0)
    assert soup.select_one("#questions-pane") is None
    assert _count(client, "sessions") == 0


def test_get_patient_htmx_partial_includes_pane(study_client: TestClient) -> None:
    r = study_client.get(f"/patient/{PID}/timepoint/0", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert "<html" not in r.text
    assert 'id="questions-pane"' in r.text


# ---------------------------------------------------------------------------
# S9b: gate on /answer + OOB advance CTA + pane modes (spec §9 #21-#25)
# ---------------------------------------------------------------------------


def _cta(html: str):
    return BeautifulSoup(html, "html.parser").select_one("#advance-form")


def test_post_answer_200_includes_oob_advance_cta(study_client: TestClient) -> None:
    r = study_client.post(_url(), data={"question_id": "deterioration_6h", "value": "No"})
    assert r.status_code == 200
    assert _state(r.text) == "saved"
    cta = _cta(r.text)
    assert cta is not None
    assert cta["hx-swap-oob"] == "true"
    assert cta["data-remaining"] == "5"

    cleared = study_client.post(_url(), data={"question_id": "deterioration_6h", "value": ""})
    assert _state(cleared.text) == "cleared"
    assert _cta(cleared.text)["data-remaining"] == "6"


@pytest.mark.parametrize(
    ("unlocked", "completed", "t_index"),
    [(1, False, 0), (1, False, 2), (2, True, 2)],
    ids=["past", "future", "completed"],
)
def test_post_answer_locked_timepoint_409(
    study_client: TestClient, unlocked: int, completed: bool, t_index: int
) -> None:
    seed_progress(study_client, PID, unlocked, completed=completed)
    r = study_client.post(_url(t_index), data={"question_id": "deterioration_6h", "value": "No"})
    assert r.status_code == 409
    assert _state(r.text) == "error"
    assert "Timepoint locked" in r.text
    assert _cta(r.text) is None
    assert _count(study_client, "answers") == 0
    assert _count(study_client, "events", "kind LIKE 'answer.%'") == 0


def test_post_answer_422_has_no_oob_cta(study_client: TestClient) -> None:
    r = study_client.post(_url(), data={"question_id": "confidence", "value": "9"})
    assert r.status_code == 422
    assert _state(r.text) == "error"
    assert _cta(r.text) is None


def test_get_patient_pane_open_has_advance_cta_with_remaining(study_client: TestClient) -> None:
    soup = _pane(study_client, 0)
    assert soup.select_one("#questions-pane")["data-mode"] == "open"
    cta = soup.select_one("#advance-form")
    assert cta["data-remaining"] == "6"
    assert cta["data-first-unanswered"] == "deterioration_6h"
    assert cta["hx-sync"] == "this:drop"
    assert not cta.has_attr("hx-swap-oob")
    btn = soup.select_one("#advance-btn")
    assert btn["aria-disabled"] == "true"
    assert btn["aria-describedby"] == "advance-hint"
    assert "6 unanswered" in btn.get_text()
    assert soup.select_one("#advance-hint") is not None
    assert not soup.select("fieldset[disabled]")


def test_get_patient_pane_locked_renders_disabled_fieldsets(study_client: TestClient) -> None:
    seed_progress(study_client, PID, 1)
    soup = _pane(study_client, 0)
    pane = soup.select_one("#questions-pane")
    assert pane["data-mode"] == "locked"
    forms = pane.select("form.question")
    assert len(forms) == FIXTURE_QUESTION_COUNT
    assert all(f.select_one("fieldset").has_attr("disabled") for f in forms)
    assert all(not f.has_attr("hx-post") and not f.has_attr("hx-trigger") for f in forms)
    assert pane.select_one(".pane-lock-note")["role"] == "note"
    link = pane.select_one(".resume-link")
    assert link["href"].endswith("/timepoint/1?chrome=epic")
    assert pane.select_one("#advance-form") is None
