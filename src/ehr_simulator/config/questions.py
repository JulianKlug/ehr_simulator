"""Questions config Pydantic model — the canonical questions.yaml shape.

The 5 response-type primitives (``likert``, ``categorical``, ``multi-select``,
``probability-0-100``, ``free-text``) are expressed as a discriminated union
on ``response_type``. Pydantic surfaces "I expected one of {...} but got X"
as a single error at the right field path.

Per /plan-eng-review issue 2.3, ``options`` raises on duplicates rather than
silently deduping — a typo collapse like ``[Yes, yes, No]`` would otherwise
surface as a different error downstream.

``question_id`` matches ``^[a-z0-9_]+$`` (cell-injection guard for S9c CSV
export). ``schema_version`` is a string literal ``"1"`` (locks D6).

``required`` (S9b, default ``True``) marks the questions the advance gate
waits for; ``required: false`` opts a question out.

S11h ``schema_version: "2"`` adds branching, evaluated by
:mod:`ehr_simulator.question_branching`::

    display_if: {question_id: deterioration_6h, equals: "Yes"}   # else hidden
    auto_value: {when: {question_id: good_outcome_3mo, equals: "Yes"}, value: "No"}
    scale_labels: [...]                                          # likert, one per point

A condition names an *earlier* categorical question and one of its options.
The v2 fields are refused under ``"1"`` and omitted from serialization when
unset, so v1 snapshots re-render byte for byte.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    field_validator,
    model_serializer,
    model_validator,
)

ResponseType = Literal[
    "likert",
    "categorical",
    "multi-select",
    "probability-0-100",
    "free-text",
]

_QUESTION_ID_RE = re.compile(r"^[a-z0-9_]+$")

#: S11h: schema generation that introduced branching.
SCHEMA_VERSION_BRANCHING = "2"

#: Probability answers are integer percentages (mirrors ``answer_codec``,
#: which imports this module and so cannot be imported here).
_PROBABILITY_RANGE = (0, 100)

#: S11h fields omitted from serialization when unset (v1 byte stability).
_BRANCHING_FIELDS = ("display_if", "auto_value", "scale_labels")


class Condition(BaseModel):
    """Equality against one earlier categorical question (S11h)."""

    model_config = ConfigDict(extra="forbid")

    question_id: str
    equals: str

    @field_validator("equals", mode="before")
    @classmethod
    def _coerce_equals(cls, v: object) -> str:
        return _coerce_option(v)


class AutoValueRule(BaseModel):
    """S11h: the question takes ``value`` automatically while ``when`` holds."""

    model_config = ConfigDict(extra="forbid")

    when: Condition
    value: str

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, v: object) -> str:
        return _coerce_option(v)


class _QuestionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    prompt: str
    # S9b gating: only ``required`` questions block ``/advance``. StrictBool
    # so ``required: 1`` / ``"yes"`` are rejected rather than coerced — this
    # is a study-design switch. Defaulted, so no schema_version bump; it does
    # enter ``config_hash`` through ``model_dump_json``.
    required: StrictBool = True
    # S11h (schema v2): branching, see the module docstring.
    display_if: Condition | None = None
    auto_value: AutoValueRule | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_branching(self, handler: SerializerFunctionWrapHandler) -> Any:
        data = handler(self)
        if isinstance(data, dict):
            for key in _BRANCHING_FIELDS:
                if data.get(key, 0) is None:
                    data.pop(key)
        return data

    def uses_branching(self) -> bool:
        """Whether any schema v2 field is set."""
        return any(getattr(self, key, None) is not None for key in _BRANCHING_FIELDS)

    @field_validator("question_id")
    @classmethod
    def _question_id_format(cls, v: str) -> str:
        if not v:
            raise ValueError("question_id must be non-empty")
        if not _QUESTION_ID_RE.fullmatch(v):
            raise ValueError(
                f"question_id {v!r} must match [a-z0-9_]+ (lowercase, digits, underscore)"
            )
        return v

    @field_validator("prompt")
    @classmethod
    def _prompt_non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt must be non-empty")
        return v


class LikertQuestion(_QuestionBase):
    response_type: Literal["likert"]
    scale_min: StrictInt
    scale_max: StrictInt
    scale_min_label: str | None = None
    scale_max_label: str | None = None
    scale_labels: list[str] | None = None  # S11h: one label per point, scale order

    @model_validator(mode="after")
    def _scale_min_lt_max(self) -> LikertQuestion:
        if self.scale_min >= self.scale_max:
            raise ValueError(f"scale_min ({self.scale_min}) must be < scale_max ({self.scale_max})")
        if self.scale_labels is not None:
            points = self.scale_max - self.scale_min + 1
            if len(self.scale_labels) != points:
                raise ValueError(
                    f"scale_labels must hold one label per point ({points}); "
                    f"got {len(self.scale_labels)}"
                )
            if any(not label.strip() for label in self.scale_labels):
                raise ValueError("scale_labels must be non-blank")
        return self


def _coerce_option(value: object) -> str:
    """Coerce one option value to str.

    The config loader keeps ``Yes``/``No``/``On``/``Off`` as strings (YAML
    1.2 booleans only); a bare ``true``/``false`` still arrives as a bool
    and maps to ``Yes``/``No``. Numerics become their string form.
    """
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"option must be a string (got {type(value).__name__})")


def _validate_options(v: list[object], *, kind: str, reject_pipe: bool = False) -> list[str]:
    coerced = [_coerce_option(x) for x in v]
    if len(coerced) < 2:
        raise ValueError(f"{kind} options must have at least 2 entries")
    if len(set(coerced)) != len(coerced):
        seen: set[str] = set()
        dups: list[str] = []
        for opt in coerced:
            if opt in seen:
                dups.append(opt)
            seen.add(opt)
        raise ValueError(f"{kind} options must be unique; duplicates: {sorted(set(dups))}")
    if reject_pipe:
        for opt in coerced:
            if "|" in opt:
                raise ValueError(
                    f"{kind} option {opt!r} contains '|', which is the CSV delimiter "
                    "for selected options; pick option text without it"
                )
    return coerced


class CategoricalQuestion(_QuestionBase):
    response_type: Literal["categorical"]
    options: list[str]

    @field_validator("options", mode="before")
    @classmethod
    def _options_unique(cls, v: list[object]) -> list[str]:
        return _validate_options(v, kind="categorical")


class MultiSelectQuestion(_QuestionBase):
    response_type: Literal["multi-select"]
    options: list[str]

    @field_validator("options", mode="before")
    @classmethod
    def _options_unique(cls, v: list[object]) -> list[str]:
        return _validate_options(v, kind="multi-select", reject_pipe=True)


class ProbabilityQuestion(_QuestionBase):
    response_type: Literal["probability-0-100"]


class FreeTextQuestion(_QuestionBase):
    response_type: Literal["free-text"]


Question = Annotated[
    LikertQuestion
    | CategoricalQuestion
    | MultiSelectQuestion
    | ProbabilityQuestion
    | FreeTextQuestion,
    Field(discriminator="response_type"),
]


class Questions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1", "2"]
    questions: list[Question]

    @field_validator("questions")
    @classmethod
    def _questions_non_empty(cls, v: list[Question]) -> list[Question]:
        if not v:
            raise ValueError("questions must be non-empty")
        return v

    @model_validator(mode="after")
    def _question_ids_unique(self) -> Questions:
        ids = [q.question_id for q in self.questions]
        if len(set(ids)) != len(ids):
            seen: set[str] = set()
            dups: list[str] = []
            for qid in ids:
                if qid in seen:
                    dups.append(qid)
                seen.add(qid)
            raise ValueError(f"question_id must be unique; duplicates: {sorted(set(dups))}")
        return self

    @model_validator(mode="after")
    def _branching_valid(self) -> Questions:
        """S11h: v2 fields only under v2; conditions point backwards at a
        categorical question and one of its options; auto values fit."""
        seen: dict[str, Question] = {}
        for question in self.questions:
            if question.uses_branching() and self.schema_version != SCHEMA_VERSION_BRANCHING:
                raise ValueError(
                    f"question {question.question_id!r} uses display_if/auto_value/"
                    'scale_labels, which need schema_version: "2"'
                )

            conditions = []
            if question.display_if is not None:
                conditions.append(question.display_if)
            if question.auto_value is not None:
                conditions.append(question.auto_value.when)
                _check_auto_value(question)
            for condition in conditions:
                _check_condition(question.question_id, condition, seen)

            seen[question.question_id] = question
        return self


def _check_condition(owner: str, condition: Condition, earlier: dict[str, Question]) -> None:
    source_id = condition.question_id
    if source_id == owner:
        raise ValueError(f"question {owner!r}: a condition may not reference itself")
    source = earlier.get(source_id)
    if source is None:
        raise ValueError(
            f"question {owner!r}: condition source {source_id!r} must be an earlier question"
        )
    if not isinstance(source, CategoricalQuestion):
        raise ValueError(f"question {owner!r}: condition source {source_id!r} must be categorical")
    if condition.equals not in source.options:
        raise ValueError(
            f"question {owner!r}: {condition.equals!r} is not an option of {source_id!r}"
        )


def _check_auto_value(question: Question) -> None:
    """The rule value must be a value the question itself accepts."""
    value = question.auto_value.value  # type: ignore[union-attr]
    owner = question.question_id
    if isinstance(question, (MultiSelectQuestion, FreeTextQuestion)):
        raise ValueError(f"question {owner!r}: auto_value needs a single-choice or numeric type")
    if isinstance(question, CategoricalQuestion):
        if value not in question.options:
            raise ValueError(f"question {owner!r}: auto_value {value!r} is not an option")
        return

    lo, hi = (
        (question.scale_min, question.scale_max)
        if isinstance(question, LikertQuestion)
        else _PROBABILITY_RANGE
    )
    # Canonical base 10 integers only, like ``answer_codec`` ("-2" yes, "+1"/"01" no).
    try:
        parsed = int(value, 10)
    except ValueError:
        parsed = None
    if parsed is None or str(parsed) != value or not lo <= parsed <= hi:
        raise ValueError(
            f"question {owner!r}: auto_value {value!r} is not an integer in {lo}..{hi}"
        )
