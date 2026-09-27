"""S11h question branching: pure evaluation and write planning.

One single pass in ``questions.yaml`` order, against the *effective* values
computed so far (a condition can only name an earlier question)::

    display_if false                  → HIDDEN    (no value, never required)
    else auto_value.when true         → DERIVED   (value = the rule value)
    else                              → EDITABLE  (value = the clinician's)

    required_now ⇔ EDITABLE and question.required

A condition on a hidden or unanswered source is false. Rule rows are fully
re-derivable, so write planning evaluates on clinician rows only and then
reconciles the stored cell (example: ``good_outcome_3mo`` Yes → No deletes
the rule ``death_3mo = No``; an older clinician death answer is never
restored).

The gate (``web/gating.py``), the pane renderer and answer capture all
evaluate through here; no condition logic lives anywhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ehr_simulator.config.questions import Condition, Question, Questions

__all__ = [
    "AnswerSource",
    "BranchWrite",
    "EvaluatedQuestion",
    "EvaluatedSet",
    "QuestionState",
    "StoredAnswer",
    "WriteReason",
    "changed_question_ids",
    "clinician_values",
    "evaluate",
    "plan_submission",
]


class QuestionState(StrEnum):
    HIDDEN = "hidden"
    EDITABLE = "editable"
    DERIVED = "derived"


class AnswerSource(StrEnum):
    CLINICIAN = "clinician"
    RULE = "rule"


class WriteReason(StrEnum):
    USER_CHANGE = "user_change"
    BRANCH_INVALIDATED = "branch_invalidated"
    AUTO_VALUE = "auto_value"


@dataclass(frozen=True)
class StoredAnswer:
    value: str  # the persisted (serialized) value
    source: AnswerSource


@dataclass(frozen=True)
class EvaluatedQuestion:
    question: Question
    state: QuestionState
    value: Any = None  # effective value; ``None`` = unanswered or hidden

    @property
    def question_id(self) -> str:
        return self.question.question_id

    @property
    def required_now(self) -> bool:
        return self.state is QuestionState.EDITABLE and self.question.required


@dataclass(frozen=True)
class EvaluatedSet:
    questions: tuple[EvaluatedQuestion, ...]

    def get(self, question_id: str) -> EvaluatedQuestion | None:
        return next((q for q in self.questions if q.question_id == question_id), None)

    @property
    def remaining(self) -> tuple[str, ...]:
        """Required-now question ids still unanswered, file order."""
        return tuple(q.question_id for q in self.questions if q.required_now and q.value is None)

    @property
    def required_count(self) -> int:
        return sum(1 for q in self.questions if q.required_now)


@dataclass(frozen=True)
class BranchWrite:
    """One planned change to the cell: ``value is None`` deletes the row."""

    question_id: str
    value: str | None
    source: AnswerSource
    reason: WriteReason
    derived_from_question_id: str | None = None


def _holds(condition: Condition, effective: Mapping[str, Any]) -> bool:
    return effective.get(condition.question_id) == condition.equals


def evaluate(questions: Questions, values: Mapping[str, Any]) -> EvaluatedSet:
    """Evaluate every question against ``{question_id: value}``."""
    effective: dict[str, Any] = {}
    evaluated: list[EvaluatedQuestion] = []
    for question in questions.questions:
        if question.display_if is not None and not _holds(question.display_if, effective):
            item = EvaluatedQuestion(question, QuestionState.HIDDEN)
        elif question.auto_value is not None and _holds(question.auto_value.when, effective):
            item = EvaluatedQuestion(question, QuestionState.DERIVED, question.auto_value.value)
        else:
            item = EvaluatedQuestion(
                question, QuestionState.EDITABLE, values.get(question.question_id)
            )

        if item.value is not None:
            effective[question.question_id] = item.value
        evaluated.append(item)
    return EvaluatedSet(tuple(evaluated))


def clinician_values(stored: Mapping[str, StoredAnswer]) -> dict[str, str]:
    """The clinician rows of a cell; rule rows are re-derived, never inputs."""
    return {qid: row.value for qid, row in stored.items() if row.source is AnswerSource.CLINICIAN}


def plan_submission(
    questions: Questions,
    stored: Mapping[str, StoredAnswer],
    question_id: str,
    value: str | None,
) -> list[BranchWrite]:
    """Every write one clinician submission implies, submitted answer first.

    The caller has already checked that ``question_id`` is EDITABLE; its
    own state cannot change (it depends only on earlier questions).
    """
    after = clinician_values(stored)
    if value is None:
        after.pop(question_id, None)
    else:
        after[question_id] = value

    writes = [BranchWrite(question_id, value, AnswerSource.CLINICIAN, WriteReason.USER_CHANGE)]
    for item in evaluate(questions, after).questions:
        qid = item.question_id
        row = stored.get(qid)
        if qid == question_id:
            continue

        if item.state is QuestionState.HIDDEN and row is not None:
            writes.append(BranchWrite(qid, None, row.source, WriteReason.BRANCH_INVALIDATED))
        elif item.state is QuestionState.DERIVED:
            rule = item.question.auto_value
            assert rule is not None  # DERIVED implies a rule
            if row != StoredAnswer(rule.value, AnswerSource.RULE):
                writes.append(
                    BranchWrite(
                        qid,
                        rule.value,
                        AnswerSource.RULE,
                        WriteReason.AUTO_VALUE,
                        derived_from_question_id=rule.when.question_id,
                    )
                )
        elif (
            item.state is QuestionState.EDITABLE
            and row is not None
            and row.source is AnswerSource.RULE
        ):
            writes.append(BranchWrite(qid, None, AnswerSource.RULE, WriteReason.BRANCH_INVALIDATED))
    return writes


def changed_question_ids(
    before: EvaluatedSet, after: EvaluatedSet, *, exclude: str
) -> tuple[str, ...]:
    """Questions whose state or value differs, file order, minus ``exclude``."""
    changed = []
    for old, new in zip(before.questions, after.questions, strict=True):
        if new.question_id == exclude:
            continue
        if (old.state, old.value) != (new.state, new.value):
            changed.append(new.question_id)
    return tuple(changed)
