"""Canonical answer encode/decode codec — one home for the ``answers.value`` contract.

The contract (session-09b spec §6, read back verbatim by session-09c)::

    response_type        stored value
    ─────────────────    ─────────────────────────────────────────
    categorical          the option, verbatim
    multi-select         JSON array in questions.yaml option order
    likert               str(int) within [scale_min, scale_max]
    probability-0-100    str(int) within [0, 100]
    free-text            stripped text, ≤ FREE_TEXT_MAX_CHARS

Two directions:

- :func:`serialize_answer` — the lenient, UI-facing encoder. Strips and
  canonicalizes a submitted form value; ``None`` means "clear the cell".
- :func:`decode_stored_answer` — the strict, export-facing decoder.
  Re-validates a *persisted* string against the **current** questions.yaml
  and returns the CSV cell (multi-select → pipe-delimited, in
  questions.yaml order). Rejections name the shape of the failure — never
  the stored content — so nothing sensitive can leak into stderr, log
  lines, or keyfiles; the caller (the export service) adds the patient /
  clinician / timepoint / question coordinates.

This module is pure: it knows nothing of SQL, the web layer, or files,
and it imports neither ``ehr_simulator.web`` nor ``ehr_simulator.export``.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from ehr_simulator.config.questions import (
    CategoricalQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    Question,
)

__all__ = [
    "FREE_TEXT_MAX_CHARS",
    "PROBABILITY_MAX",
    "PROBABILITY_MIN",
    "AnswerValidationError",
    "decode_stored_answer",
    "deserialize_answer",
    "serialize_answer",
]

# Write-contract bounds (one numeric source of truth for both the UI
# serializer and the strict decoder).
FREE_TEXT_MAX_CHARS = 4000
PROBABILITY_MIN = 0
PROBABILITY_MAX = 100


class AnswerValidationError(ValueError):
    """The value does not fit the question's response type.

    Message carries the shape of the failure; coordinates are added by
    the caller that knows them.
    """


# ---------------------------------------------------------------------------
# Serialize (lenient, UI-facing)
# ---------------------------------------------------------------------------


def _single(values: list[str]) -> str:
    if len(values) != 1:
        raise AnswerValidationError(f"expected exactly one value, got {len(values)}")
    return values[0]


def _int_in_range(raw: str, lo: int, hi: int) -> int:
    try:
        parsed = int(raw, 10)
    except ValueError:
        raise AnswerValidationError(f"expected an integer, got {raw!r}") from None
    if str(parsed) != raw:
        raise AnswerValidationError(f"expected a bare integer, got {raw!r}")
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
    """Validate + canonicalize a submitted form value; ``None`` means "clear the cell"."""
    values = [v.strip() for v in raw_values if v.strip()]
    if not values:
        return None
    return _SERIALIZERS[question.response_type](question, values)


def deserialize_answer(question: Question, value: str) -> str | list[str]:
    """Inverse of :func:`serialize_answer` for UI pre-fill (multi-select → list)."""
    if question.response_type != "multi-select":
        return value
    try:
        decoded = json.loads(value)
    except ValueError:
        return []
    return [str(v) for v in decoded] if isinstance(decoded, list) else []


# ---------------------------------------------------------------------------
# Strict decode (export-facing)
# ---------------------------------------------------------------------------


def decode_stored_answer(question: Question, stored: str) -> str:
    """Strictly decode one persisted cell against the current *question*.

    Returns the CSV cell: the canonical string for single-value types, the
    pipe-delimited option list (in questions.yaml order) for multi-select.

    Raises :class:`AnswerValidationError` describing the failure without
    revealing the stored content.
    """
    response_type = question.response_type
    if response_type == "categorical":
        assert isinstance(question, CategoricalQuestion)
        if stored not in question.options:
            raise AnswerValidationError("expected one of the configured options")
        return stored

    if response_type == "multi-select":
        assert isinstance(question, MultiSelectQuestion)
        try:
            decoded = json.loads(stored)
        except json.JSONDecodeError:
            raise AnswerValidationError("expected a JSON array of the configured options") from None
        if not isinstance(decoded, list):
            raise AnswerValidationError("expected a JSON array of the configured options")
        if not decoded:
            raise AnswerValidationError("expected at least one selected option")
        if any(not isinstance(opt, str) for opt in decoded):
            raise AnswerValidationError("expected every selected option to be a string")
        if any("|" in opt for opt in decoded):
            raise AnswerValidationError("a selected option contains the pipe delimiter")
        if any(opt not in question.options for opt in decoded):
            raise AnswerValidationError("a selected option is not in the configured option list")
        if len(set(decoded)) != len(decoded):
            raise AnswerValidationError("the selection contains duplicate options")
        if decoded != [opt for opt in question.options if opt in decoded]:
            raise AnswerValidationError("the selection is not in questions.yaml order")
        return "|".join(decoded)

    if response_type == "likert":
        assert isinstance(question, LikertQuestion)
        _strict_int(stored, question.scale_min, question.scale_max)
        return stored

    if response_type == "probability-0-100":
        _strict_int(stored, PROBABILITY_MIN, PROBABILITY_MAX)
        return stored

    if response_type == "free-text":
        if len(stored) > FREE_TEXT_MAX_CHARS:
            raise AnswerValidationError(f"stored text exceeds {FREE_TEXT_MAX_CHARS} characters")
        if stored != stored.strip():
            raise AnswerValidationError("stored text is not whitespace-canonical")
        return stored

    raise AnswerValidationError(f"unknown response type {response_type!r}")


def _strict_int(raw: str, lo: int, hi: int) -> None:
    """Reject anything that is not the bare decimal integer in range.

    ``"3"`` passes for a 1..5 scale; ``"03"``, ``"+3"``, ``"3.0"``,
    ``" 3"`` and non-decimal digit scripts are all rejected, since none
    of them is a form the serializer ever writes.
    """
    try:
        parsed = int(raw, 10)
    except ValueError:
        raise AnswerValidationError("stored value is not a canonical integer") from None
    if str(parsed) != raw:
        raise AnswerValidationError("stored value is not a canonical integer")
    if not lo <= parsed <= hi:
        raise AnswerValidationError(f"stored value is outside the configured range {lo}..{hi}")
