"""``web/answer_capture`` service layer (specs/session-09a §9 #1-#10c).

Serialization per response type, the empty-means-clear rule, the two
browser clock-field normalizers, and the persist + event path.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from ehr_simulator.config import load_questions
from ehr_simulator.config.questions import Question, Questions
from ehr_simulator.db import answers, clinicians
from ehr_simulator.web.answer_capture import (
    FREE_TEXT_MAX_CHARS,
    AnswerValidationError,
    normalize_client_seq,
    normalize_client_ts,
    record_answer,
    saved_answers,
    serialize_answer,
)
from ehr_simulator.web.study_session import Frontier, bootstrap_session

_T = 60.0


class _AppState:
    write_counter = 0
    config_hash = "cfg"


_QUESTIONS_YAML = Path(__file__).parent / "fixtures" / "study" / "questions.yaml"


@pytest.fixture(scope="module")
def questions() -> Questions:
    return load_questions(_QUESTIONS_YAML)


def _q(questions: Questions, question_id: str) -> Question:
    return next(q for q in questions.questions if q.question_id == question_id)


# ---------------------------------------------------------------------------
# serialize_answer (#1-#6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(["No"], "No"), (["Maybe"], None), (["Yes", "No"], None), ([" Yes "], "Yes")],
    ids=["ok", "unknown", "two_values", "whitespace_ok"],
)
def test_serialize_categorical(questions: Questions, raw: list[str], expected: str | None) -> None:
    q = _q(questions, "deterioration_6h")
    if expected is None:
        with pytest.raises(AnswerValidationError):
            serialize_answer(q, raw)
        return
    assert serialize_answer(q, raw) == expected


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("1", True), ("5", True), ("0", False), ("6", False), ("x", False), ("3.0", False)],
    ids=["min", "max", "below", "above", "non_int", "float"],
)
def test_serialize_likert(questions: Questions, raw: str, ok: bool) -> None:
    q = _q(questions, "confidence")
    if not ok:
        with pytest.raises(AnswerValidationError):
            serialize_answer(q, [raw])
        return
    assert serialize_answer(q, [raw]) == raw


@pytest.mark.parametrize(
    ("raw", "ok"),
    [("0", True), ("100", True), ("101", False), ("-1", False), ("50.5", False), ("abc", False)],
    ids=["zero", "hundred", "above", "neg", "float", "text"],
)
def test_serialize_probability(questions: Questions, raw: str, ok: bool) -> None:
    q = _q(questions, "good_outcome_3mo")
    if not ok:
        with pytest.raises(AnswerValidationError):
            serialize_answer(q, [raw])
        return
    assert serialize_answer(q, [raw]) == raw


def test_serialize_multi_select_canonical_order(questions: Questions) -> None:
    q = _q(questions, "contributing_factors")
    assert serialize_answer(q, ["Labs", "Imaging"]) == '["Imaging","Labs"]'
    with pytest.raises(AnswerValidationError, match="duplicate"):
        serialize_answer(q, ["Imaging", "Imaging"])
    with pytest.raises(AnswerValidationError, match="unknown option"):
        serialize_answer(q, ["Nope"])


def test_serialize_free_text_strips_and_caps(questions: Questions) -> None:
    q = _q(questions, "free_notes")
    assert serialize_answer(q, ["  hello "]) == "hello"
    assert serialize_answer(q, ["x" * FREE_TEXT_MAX_CHARS]) == "x" * FREE_TEXT_MAX_CHARS
    # strip precedes the cap (review-fix R14)
    assert len(serialize_answer(q, [" " * 10 + "x" * FREE_TEXT_MAX_CHARS]) or "") == (
        FREE_TEXT_MAX_CHARS
    )
    with pytest.raises(AnswerValidationError, match="too long"):
        serialize_answer(q, ["x" * (FREE_TEXT_MAX_CHARS + 1)])


@pytest.mark.parametrize(
    "question_id",
    ["deterioration_6h", "good_outcome_3mo", "confidence", "contributing_factors", "free_notes"],
)
@pytest.mark.parametrize("raw", [[], [""], ["   "]], ids=["none", "empty", "blank"])
def test_serialize_empty_means_clear(
    questions: Questions, question_id: str, raw: list[str]
) -> None:
    assert serialize_answer(_q(questions, question_id), raw) is None


# ---------------------------------------------------------------------------
# record_answer + saved_answers (#7, #8, #9, #10c)
# ---------------------------------------------------------------------------


def _record(
    db: sqlite3.Connection,
    state: _AppState,
    questions: Questions,
    question_id: str,
    raw: list[str],
    *,
    client_ts: str | None = None,
    client_seq: str | None = None,
) -> tuple[str, str]:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    ctx = bootstrap_session(
        db, state, clinician_id=cid, patient_id="p1", frontier=Frontier(0, False)
    )
    outcome = record_answer(
        db,
        state,
        ctx=ctx,
        clinician_id=cid,
        patient_id="p1",
        t_minutes=_T,
        question=_q(questions, question_id),
        raw_values=raw,
        client_ts=client_ts,
        client_seq=client_seq,
    )
    return cid, outcome


def test_record_answer_upserts_and_emits_event(
    db: sqlite3.Connection, questions: Questions
) -> None:
    state = _AppState()
    cid, outcome = _record(
        db,
        state,
        questions,
        "deterioration_6h",
        ["No"],
        client_ts="2026-09-16T12:34:56.789Z",
        client_seq="7",
    )
    assert outcome == "saved"

    row = db.execute("SELECT value, arm, config_hash, timepoint FROM answers").fetchone()
    assert tuple(row) == ("No", "no_ai", "cfg", _T)

    ev = db.execute(
        "SELECT session_id, patient_id, timepoint, payload_json, client_ts, client_seq "
        "FROM events WHERE kind = 'answer.upsert'"
    ).fetchall()
    assert len(ev) == 1
    session_id, patient_id, timepoint, payload_json, client_ts, client_seq = ev[0]
    assert session_id is not None
    assert (patient_id, timepoint) == ("p1", _T)
    # PARSE_DECLTYPES already converted the TIMESTAMP column back to a datetime.
    assert str(client_ts) == "2026-09-16 12:34:56.789000"
    assert client_seq == 7
    payload = json.loads(payload_json)
    assert payload == {
        "question_id": "deterioration_6h",
        "response_type": "categorical",
        "value_chars": 2,
    }
    assert "value" not in payload
    assert cid


def test_record_answer_clear_deletes_row_and_emits_event(
    db: sqlite3.Connection, questions: Questions
) -> None:
    state = _AppState()
    _record(db, state, questions, "free_notes", ["some note"])
    _, outcome = _record(db, state, questions, "free_notes", [""])
    assert outcome == "cleared"
    assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0

    _, outcome_again = _record(db, state, questions, "free_notes", ["  "])
    assert outcome_again == "cleared"
    payloads = [
        json.loads(r[0])["deleted"]
        for r in db.execute(
            "SELECT payload_json FROM events WHERE kind = 'answer.clear' ORDER BY event_id"
        )
    ]
    assert payloads == [True, False]


def test_saved_answers_deserializes_per_type(db: sqlite3.Connection, questions: Questions) -> None:
    state = _AppState()
    cid, _ = _record(db, state, questions, "contributing_factors", ["Labs", "Imaging"])
    _record(db, state, questions, "good_outcome_3mo", ["65"])
    # A row from an older questions.yaml whose id no longer exists.
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=_T,
        question_id="retired_question",
        value="x",
        arm="no_ai",
        config_hash="cfg",
    )

    got = saved_answers(
        db,
        clinician_id=cid,
        patient_id="p1",
        t_minutes=_T,
        questions=questions,
        config_hash="cfg",
    )
    assert got == {"contributing_factors": ["Imaging", "Labs"], "good_outcome_3mo": "65"}


def test_saved_answers_warns_on_config_hash_drift(
    db: sqlite3.Connection, questions: Questions
) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=_T,
        question_id="confidence",
        value="3",
        arm="no_ai",
        config_hash="old",
    )
    common = {"clinician_id": cid, "patient_id": "p1", "t_minutes": _T, "questions": questions}

    with capture_logs() as cap:
        got = saved_answers(db, config_hash="new", **common)
    drift = [e for e in cap if e.get("event_kind") == "answer.config_hash.drift"]
    assert got == {"confidence": "3"}
    assert len(drift) == 1
    assert drift[0]["question_ids"] == ["confidence"]

    with capture_logs() as cap_same:
        saved_answers(db, config_hash="old", **common)
    assert not [e for e in cap_same if e.get("event_kind") == "answer.config_hash.drift"]


# ---------------------------------------------------------------------------
# Browser clock fields (#10, #10b)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-09-16T12:34:56.789Z", "2026-09-16 12:34:56.789000"),
        ("2026-09-16T14:34:56+02:00", "2026-09-16 12:34:56.000000"),
        ("2026-09-16 12:34:56", "2026-09-16 12:34:56.000000"),
        ("garbage", None),
        (None, None),
        ("", None),
    ],
    ids=["z", "offset", "naive", "garbage", "none", "empty"],
)
def test_normalize_client_ts(raw: str | None, expected: str | None) -> None:
    with capture_logs() as cap:
        assert normalize_client_ts(raw) == expected
    warned = any(e.get("event_kind") == "answer.client_ts.invalid" for e in cap)
    assert warned == (raw == "garbage")


@pytest.mark.parametrize(
    ("raw", "expected", "warns"),
    [
        ("7", 7, False),
        ("0", 0, False),
        ("abc", None, True),
        ("", None, False),
        (None, None, False),
        ("9" * 30, None, True),
        ("-1", None, True),
    ],
    ids=["int", "zero", "garbage", "empty", "none", "overflow", "negative"],
)
def test_normalize_client_seq(raw: str | None, expected: int | None, warns: bool) -> None:
    with capture_logs() as cap:
        assert normalize_client_seq(raw) == expected
    assert any(e.get("event_kind") == "answer.client_seq.invalid" for e in cap) == warns
