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
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ResponseType = Literal[
    "likert",
    "categorical",
    "multi-select",
    "probability-0-100",
    "free-text",
]

_QUESTION_ID_RE = re.compile(r"^[a-z0-9_]+$")


class _QuestionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_id: str
    prompt: str

    @field_validator("question_id")
    @classmethod
    def _question_id_format(cls, v: str) -> str:
        if not v:
            raise ValueError("question_id must be non-empty")
        if not _QUESTION_ID_RE.match(v):
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
    scale_min: int
    scale_max: int
    scale_min_label: str | None = None
    scale_max_label: str | None = None

    @model_validator(mode="after")
    def _scale_min_lt_max(self) -> LikertQuestion:
        if self.scale_min >= self.scale_max:
            raise ValueError(f"scale_min ({self.scale_min}) must be < scale_max ({self.scale_max})")
        return self


def _coerce_option(value: object) -> str:
    """Coerce one option value to str.

    YAML 1.1 (pyyaml's default) parses bare ``Yes``/``No``/``On``/``Off`` as
    booleans, so ``options: [Yes, No, Unknown]`` would otherwise reach
    Pydantic as ``[True, False, "Unknown"]`` and fail. Coerce numerics +
    booleans to their canonical YAML token (``Yes``/``No``) here so the
    user-friendly fixture syntax round-trips unchanged.
    """
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"option must be a string (got {type(value).__name__})")


def _validate_options(v: list[object], *, kind: str) -> list[str]:
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
        return _validate_options(v, kind="multi-select")


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

    schema_version: Literal["1"]
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
