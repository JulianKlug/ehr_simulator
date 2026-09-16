"""Answer capture service: the layer between the ``/answer`` route and the DAOs.

Owns the ``answers.value`` string contract (spec §6) that S9c's export and
S10's divergence view consume verbatim::

    response_type        stored value
    ─────────────────    ─────────────────────────────────────────
    categorical          the option, verbatim
    multi-select         JSON array in questions.yaml option order
    likert               str(int) within [scale_min, scale_max]
    probability-0-100    str(int) within [0, 100]
    free-text            stripped text, ≤ FREE_TEXT_MAX_CHARS

An empty submission (no non-blank values) means *clear*: the row is
deleted, so S9b's gate reads the cell as unanswered.

Event rows never carry the raw value (it lives in ``answers``; duplicating
free-text into ``payload_json`` would double the pseudonymization surface).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from ehr_simulator.config.questions import (
    CategoricalQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    Question,
    Questions,
)
from ehr_simulator.db import answers, events
from ehr_simulator.logging import get_logger
from ehr_simulator.web.study_session import SessionContext

FREE_TEXT_MAX_CHARS = 4000
PROBABILITY_MIN = 0
PROBABILITY_MAX = 100
CLIENT_SEQ_MIN = 0
CLIENT_SEQ_MAX = 2**63 - 1  # SQLite INTEGER ceiling
FREE_TEXT_AUTOSAVE_DELAY_MS = 1500

# Python's sqlite3 default ``timestamp`` converter (active under
# PARSE_DECLTYPES) only parses this shape; anything else makes later
# SELECTs on ``events`` raise.
_SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_INT_RE = re.compile(r"^[+-]?\d+$")

AnswerOutcome = Literal["saved", "cleared"]


class AnswerValidationError(ValueError):
    """The submitted value does not fit the question's response type."""


# ---------------------------------------------------------------------------
# Serialization (per response type)
# ---------------------------------------------------------------------------


def _single(values: list[str]) -> str:
    if len(values) != 1:
        raise AnswerValidationError(f"expected exactly one value, got {len(values)}")
    return values[0]


def _int_in_range(raw: str, lo: int, hi: int) -> int:
    if not _INT_RE.match(raw):
        raise AnswerValidationError(f"expected an integer, got {raw!r}")
    parsed = int(raw)
    if not lo <= parsed <= hi:
        raise AnswerValidationError(f"expected a value between {lo} and {hi}, got {parsed}")
    return parsed


def _serialize_categorical(question: Question, values: list[str]) -> str:
    assert isinstance(question, CategoricalQuestion)
    chosen = _single(values)
    if chosen not in question.options:
        raise AnswerValidationError(f"unknown option {chosen!r}")
    return chosen


def _serialize_multi_select(question: Question, values: list[str]) -> str:
    assert isinstance(question, MultiSelectQuestion)
    unknown = [v for v in values if v not in question.options]
    if unknown:
        raise AnswerValidationError(f"unknown option(s) {unknown!r}")
    if len(set(values)) != len(values):
        raise AnswerValidationError("duplicate options submitted")

    # Canonical order = questions.yaml order, never click order.
    canonical = [opt for opt in question.options if opt in values]
    return json.dumps(canonical, separators=(",", ":"))


def _serialize_likert(question: Question, values: list[str]) -> str:
    assert isinstance(question, LikertQuestion)
    return str(_int_in_range(_single(values), question.scale_min, question.scale_max))


def _serialize_probability(_question: Question, values: list[str]) -> str:
    return str(_int_in_range(_single(values), PROBABILITY_MIN, PROBABILITY_MAX))


def _serialize_free_text(_question: Question, values: list[str]) -> str:
    text = _single(values)
    if len(text) > FREE_TEXT_MAX_CHARS:
        raise AnswerValidationError(f"text too long ({len(text)} chars, max {FREE_TEXT_MAX_CHARS})")
    return text


_SERIALIZERS: dict[str, Callable[[Question, list[str]], str]] = {
    "categorical": _serialize_categorical,
    "multi-select": _serialize_multi_select,
    "likert": _serialize_likert,
    "probability-0-100": _serialize_probability,
    "free-text": _serialize_free_text,
}


def serialize_answer(question: Question, raw_values: list[str]) -> str | None:
    """Validate + canonicalize; ``None`` means "clear the cell".

    Raises :class:`AnswerValidationError` on anything the question's
    response type does not accept.
    """
    values = [v.strip() for v in raw_values if v.strip()]
    if not values:
        return None
    return _SERIALIZERS[question.response_type](question, values)


def deserialize_answer(question: Question, value: str) -> str | list[str]:
    """Inverse of :func:`serialize_answer` for pre-fill (multi-select → list)."""
    if question.response_type != "multi-select":
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return []
    return [str(v) for v in decoded] if isinstance(decoded, list) else []


# ---------------------------------------------------------------------------
# Browser-side clock fields
# ---------------------------------------------------------------------------


def normalize_client_ts(raw: str | None) -> str | None:
    """ISO-8601 from the browser → the only shape sqlite3's converter parses.

    A bad clock field never rejects an answer: NULL + WARNING.
    """
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        get_logger().warning(
            "client_ts unparseable", event_kind="answer.client_ts.invalid", raw=raw
        )
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.strftime(_SQLITE_TIMESTAMP_FORMAT)


def normalize_client_seq(raw: str | None) -> int | None:
    """Per-tab counter from the browser → int within SQLite's INTEGER range, or NULL."""
    if not raw:
        return None
    if not _INT_RE.match(raw):
        get_logger().warning(
            "client_seq unparseable", event_kind="answer.client_seq.invalid", raw=raw
        )
        return None
    seq = int(raw)
    if not CLIENT_SEQ_MIN <= seq <= CLIENT_SEQ_MAX:
        get_logger().warning(
            "client_seq out of range", event_kind="answer.client_seq.invalid", raw=raw
        )
        return None
    return seq


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def saved_answers(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    t_minutes: float,
    questions: Questions,
    config_hash: str,
) -> dict[str, str | list[str]]:
    """Pre-fill mapping for one cell, keyed by ``question_id``.

    Rows for question ids not in the running config are dropped. Rows
    recorded under a different ``config_hash`` are still returned (they are
    that clinician's answers) but produce one ``answer.config_hash.drift``
    WARNING per render — the detector for a mid-pilot config edit.
    """
    rows = answers.fetch_for_cell(
        conn, clinician_id=clinician_id, patient_id=patient_id, timepoint=t_minutes
    )
    by_id = {q.question_id: q for q in questions.questions}
    known = {qid: row for qid, row in rows.items() if qid in by_id}

    stale = sorted(qid for qid, (_value, row_hash) in known.items() if row_hash != config_hash)
    if stale:
        get_logger().warning(
            "pre-filled answers were recorded under a different config",
            event_kind="answer.config_hash.drift",
            question_ids=stale,
            row_config_hashes=sorted({known[qid][1] for qid in stale}),
            live_config_hash=config_hash,
        )

    return {qid: deserialize_answer(by_id[qid], value) for qid, (value, _h) in known.items()}


def record_answer(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_minutes: float,
    question: Question,
    raw_values: list[str],
    client_ts: str | None,
    client_seq: str | None,
) -> AnswerOutcome:
    """Validate, persist (upsert or delete), and emit the matching event row."""
    value = serialize_answer(question, raw_values)
    cell = {
        "clinician_id": clinician_id,
        "patient_id": patient_id,
        "timepoint": t_minutes,
        "question_id": question.question_id,
    }

    if value is None:
        deleted = answers.delete_one(conn, **cell, app_state=app_state)
        outcome: AnswerOutcome = "cleared"
        detail: dict[str, Any] = {"deleted": deleted > 0}
    else:
        answers.upsert(
            conn, **cell, value=value, arm=ctx.arm, config_hash=ctx.config_hash, app_state=app_state
        )
        outcome = "saved"
        detail = {"value_chars": len(value)}

    events.append(
        conn,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=t_minutes,
        kind="answer.clear" if value is None else "answer.upsert",
        payload={
            "question_id": question.question_id,
            "response_type": question.response_type,
            **detail,
        },
        client_ts=normalize_client_ts(client_ts),
        client_seq=normalize_client_seq(client_seq),
        app_state=app_state,
    )
    return outcome
