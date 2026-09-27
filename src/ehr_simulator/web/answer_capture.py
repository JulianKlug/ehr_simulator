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

S11h: one submission is one transaction over the whole branch::

    stored cell ─▶ evaluate (before) ─▶ target EDITABLE? else refuse
                ─▶ plan_submission ─▶ clinician write + dependent deletes /
                   rule upserts + one event each ─▶ single commit
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
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
from ehr_simulator.question_branching import (
    AnswerSource,
    BranchWrite,
    QuestionState,
    StoredAnswer,
    changed_question_ids,
    evaluate,
    plan_submission,
)
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


class QuestionNotEditableError(Exception):
    """S11h: the question is hidden or set automatically under the current branch."""

    def __init__(self, question_id: str, state: QuestionState) -> None:
        reason = "is not shown" if state is QuestionState.HIDDEN else "is set automatically"
        super().__init__(f"Question '{question_id}' {reason}")
        self.state = state


@dataclass(frozen=True)
class RecordedAnswer:
    outcome: AnswerOutcome
    changed_question_ids: tuple[str, ...]  # other questions whose branch state moved


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


def stored_cell(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    t_minutes: float,
    config_hash: str,
    config_version: str | None = None,
) -> dict[str, StoredAnswer]:
    """The cell's persisted answers with their source (S11h).

    Same S11b provenance lock as :func:`saved_answers`.
    """
    rows = answers.fetch_cell_answers(
        conn, clinician_id=clinician_id, patient_id=patient_id, timepoint=t_minutes
    )
    stale = sorted(
        qid
        for qid, row in rows.items()
        if row.config_version != config_version or row.config_hash != config_hash
    )
    if stale:
        raise ConfigurationProvenanceError(
            "stored answer provenance disagrees with the case configuration "
            f"(clinician={clinician_id}, patient={patient_id}, "
            f"timepoint={t_minutes}, question_ids={stale})"
        )
    return {
        qid: StoredAnswer(row.value, AnswerSource(row.answer_source)) for qid, row in rows.items()
    }


def record_answer(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_minutes: float,
    questions: Questions,
    question: Question,
    raw_values: list[str],
    client_ts: str | None,
    client_seq: str | None,
) -> RecordedAnswer:
    """Validate, then persist the answer and its branch consequences atomically.

    Raises:
        AnswerValidationError: the value does not fit the question.
        QuestionNotEditableError: hidden or derived under the current branch.
        ConfigurationProvenanceError: a stored row is pinned elsewhere.
    """
    stored = stored_cell(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_minutes=t_minutes,
        config_hash=ctx.config_hash,
        config_version=ctx.config_version,
    )
    before = evaluate(questions, {qid: row.value for qid, row in stored.items()})
    current = before.get(question.question_id)
    if current is None or current.state is not QuestionState.EDITABLE:
        raise QuestionNotEditableError(
            question.question_id, current.state if current else QuestionState.HIDDEN
        )

    # Validated only once the question is known to be editable (a malformed
    # value to a hidden or derived question is a 409, not a 422).
    value = serialize_answer(question, raw_values)

    by_id = {q.question_id: q for q in questions.questions}
    writes = plan_submission(questions, stored, question.question_id, value)
    clock = {
        "client_ts": normalize_client_ts(client_ts),
        "client_seq": normalize_client_seq(client_seq),
    }
    try:
        for write in writes:
            _apply_write(
                conn, app_state, ctx, clinician_id, patient_id, t_minutes, by_id, write, clock
            )
        conn.commit()
    except Exception:
        conn.rollback()
        get_logger().exception(
            "answer transaction rolled back (branch state restored)",
            event_kind="answer.rollback",
        )
        raise
    # Every submission commits at least its audit event, so it always counts.
    if app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1

    after_values = {qid: row.value for qid, row in stored.items()}
    for write in writes:
        if write.value is None:
            after_values.pop(write.question_id, None)
        else:
            after_values[write.question_id] = write.value
    after = evaluate(questions, after_values)
    return RecordedAnswer(
        outcome="cleared" if value is None else "saved",
        changed_question_ids=changed_question_ids(before, after, exclude=question.question_id),
    )


def _apply_write(
    conn: sqlite3.Connection,
    app_state: Any,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_minutes: float,
    by_id: dict[str, Question],
    write: BranchWrite,
    clock: dict[str, Any],
) -> None:
    """One uncommitted row change + its event."""
    cell = {
        "clinician_id": clinician_id,
        "patient_id": patient_id,
        "timepoint": t_minutes,
        "question_id": write.question_id,
        "config_hash": ctx.config_hash,
        "config_version": ctx.config_version,
        "observation_mode": ctx.observation_mode,
    }
    if write.value is None:
        deleted = answers.delete_one(conn, **cell, commit=False)
        detail: dict[str, Any] = {"deleted": deleted > 0}
    else:
        answers.upsert(
            conn,
            **cell,
            value=write.value,
            arm=ctx.arm,
            answer_source=str(write.source),
            derived_from_question_id=write.derived_from_question_id,
            commit=False,
        )
        detail = {"value_chars": len(write.value)}

    events.append(
        conn,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=t_minutes,
        kind="answer.clear" if write.value is None else "answer.upsert",
        payload={
            "question_id": write.question_id,
            "response_type": by_id[write.question_id].response_type,
            **detail,
            "source": str(write.source),
            "reason": str(write.reason),
        },
        **clock,
        app_state=app_state,
        commit=False,
    )
