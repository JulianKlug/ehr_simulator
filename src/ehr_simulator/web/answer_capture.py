"""Answer capture service: the layer between the ``/answer`` route and the DAOs.

The encode/decode contract for ``answers.value`` (spec §6) lives in the
shared codec, :mod:`ehr_simulator.answer_codec` — one home for the UI-
facing direction (*serialize*) and the export-facing one (strict *decode*
used by :mod:`ehr_simulator.export`).
``serialize_answer`` / ``deserialize_answer`` /
``AnswerValidationError`` and the bounds constants are re-exposed here
so existing import sites (``web.routes``, ``web.gating``, the tests)
keep working unchanged.

An empty submission (no non-blank values) means *clear*: the row is
deleted, so S9b's gate reads the cell as unanswered.

Event rows never carry the raw value (it lives in ``answers``; duplicating
free-text into ``payload_json`` would double the pseudonymization surface).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from typing import Any, Literal

from ehr_simulator.answer_codec import (
    FREE_TEXT_MAX_CHARS as FREE_TEXT_MAX_CHARS,  # noqa: F401  (re-export)
)
from ehr_simulator.answer_codec import (
    PROBABILITY_MAX as PROBABILITY_MAX,  # noqa: F401  (re-export)
)
from ehr_simulator.answer_codec import (
    PROBABILITY_MIN as PROBABILITY_MIN,  # noqa: F401  (re-export)
)
from ehr_simulator.answer_codec import (
    AnswerValidationError as AnswerValidationError,  # noqa: F401  (re-export)
)
from ehr_simulator.answer_codec import (
    deserialize_answer,  # noqa: F401  (re-export)
    serialize_answer,
)
from ehr_simulator.config.questions import Question, Questions
from ehr_simulator.db import answers, events
from ehr_simulator.db.exceptions import ConfigurationProvenanceError
from ehr_simulator.logging import get_logger
from ehr_simulator.web.study_session import SessionContext

CLIENT_SEQ_MIN = 0
CLIENT_SEQ_MAX = 2**63 - 1  # SQLite INTEGER ceiling
FREE_TEXT_AUTOSAVE_DELAY_MS = 1500

# Python's sqlite3 default ``timestamp`` converter (active under
# PARSE_DECLTYPES) only parses this shape; anything else makes later
# SELECTs on ``events`` raise.
_SQLITE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_INT_RE = re.compile(r"^[+-]?\d+$")

AnswerOutcome = Literal["saved", "cleared"]


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
    config_version: str | None = None,
) -> dict[str, str | list[str]]:
    """Pre-fill mapping for one cell, keyed by ``question_id``.

    S11b provenance lock: every stored row must carry exactly the case's
    ``(config_version, config_hash)``. A row recorded under a different
    version or hash is an integrity error, not a warning — the case is
    pinned and its answers must be too.
    """
    rows = answers.fetch_for_cell(
        conn, clinician_id=clinician_id, patient_id=patient_id, timepoint=t_minutes
    )
    stale: list[str] = []
    for qid, (_value, row_hash, row_version) in rows.items():
        if row_version != config_version or row_hash != config_hash:
            stale.append(qid)
    if stale:
        raise ConfigurationProvenanceError(
            "stored answer provenance disagrees with the case configuration "
            f"(clinician={clinician_id}, patient={patient_id}, "
            f"timepoint={t_minutes}, question_ids={sorted(stale)})"
        )
    by_id = {q.question_id: q for q in questions.questions}
    known = {qid: row for qid, row in rows.items() if qid in by_id}

    return {qid: deserialize_answer(by_id[qid], value) for qid, (value, _h, _v) in known.items()}


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
        deleted = answers.delete_one(
            conn,
            **cell,
            config_hash=ctx.config_hash,
            config_version=ctx.config_version,
            app_state=app_state,
        )
        outcome: AnswerOutcome = "cleared"
        detail: dict[str, Any] = {"deleted": deleted > 0}
    else:
        answers.upsert(
            conn,
            **cell,
            value=value,
            arm=ctx.arm,
            config_hash=ctx.config_hash,
            config_version=ctx.config_version,
            app_state=app_state,
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
