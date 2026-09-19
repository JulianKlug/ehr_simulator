"""Codec tests — the ``answers.value`` storage contract in both directions.

S9c spec §9 items 1-11: the strict decoder refuses every persisted shape
the lenient serializer never writes, and the lenient UI pre-fill path
(serialize/deserialize) keeps its forgiving behavior.
"""

from __future__ import annotations

import json

import pytest

from ehr_simulator.answer_codec import (
    FREE_TEXT_MAX_CHARS,
    AnswerValidationError,
    decode_stored_answer,
    deserialize_answer,
    serialize_answer,
)
from ehr_simulator.config.questions import (
    CategoricalQuestion,
    FreeTextQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    ProbabilityQuestion,
)


@pytest.fixture
def categorical() -> CategoricalQuestion:
    return CategoricalQuestion(
        question_id="q_cat",
        prompt="p",
        options=["Yes", "No", "Unknown"],
        response_type="categorical",
    )


@pytest.fixture
def multi_select() -> MultiSelectQuestion:
    return MultiSelectQuestion(
        question_id="q_ms",
        prompt="p",
        options=["Imaging", "Labs", "Neuro-consult", "Vitals"],
        response_type="multi-select",
    )


@pytest.fixture
def likert() -> LikertQuestion:
    return LikertQuestion(
        question_id="q_likert",
        prompt="p",
        scale_min=1,
        scale_max=5,
        response_type="likert",
    )


@pytest.fixture
def probability() -> ProbabilityQuestion:
    return ProbabilityQuestion(question_id="q_prob", prompt="p", response_type="probability-0-100")


@pytest.fixture
def free_text() -> FreeTextQuestion:
    return FreeTextQuestion(question_id="q_free", prompt="p", response_type="free-text")


# ---------------------------------------------------------------------------
# Strict decode: categorical
# ---------------------------------------------------------------------------


def test_decode_categorical_configured_option(categorical: CategoricalQuestion) -> None:
    assert decode_stored_answer(categorical, "Yes") == "Yes"


def test_decode_categorical_unconfigured_option_raises(categorical: CategoricalQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="configured options"):
        decode_stored_answer(categorical, "Maybe")


# ---------------------------------------------------------------------------
# Strict decode: multi-select
# ---------------------------------------------------------------------------


def test_decode_multi_select_valid_in_configured_order(multi_select: MultiSelectQuestion) -> None:
    stored = json.dumps(["Imaging", "Labs"])
    assert decode_stored_answer(multi_select, stored) == "Imaging|Labs"


def test_decode_multi_select_single_option(multi_select: MultiSelectQuestion) -> None:
    assert decode_stored_answer(multi_select, json.dumps(["Vitals"])) == "Vitals"


def test_decode_multi_select_malformed_json_raises(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="JSON array"):
        decode_stored_answer(multi_select, "Labs")


def test_decode_multi_select_non_list_raises(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="JSON array"):
        decode_stored_answer(multi_select, json.dumps("Labs"))


def test_decode_multi_select_unknown_member_raises(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="configured option list"):
        decode_stored_answer(multi_select, json.dumps(["Labs", "MRI"]))


def test_decode_multi_select_duplicate_members_raise(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="duplicate"):
        decode_stored_answer(multi_select, json.dumps(["Labs", "Labs"]))


def test_decode_multi_select_non_string_member_raises(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="string"):
        decode_stored_answer(multi_select, json.dumps(["Labs", 42]))


def test_decode_multi_select_non_canonical_order_raises(multi_select: MultiSelectQuestion) -> None:
    # Configured order is Imaging, Labs, ..., Vitals — Labs before
    # Imaging is a shape the serializer never writes.
    with pytest.raises(AnswerValidationError, match="questions.yaml order"):
        decode_stored_answer(multi_select, json.dumps(["Labs", "Imaging"]))


def test_decode_multi_select_empty_list_raises(multi_select: MultiSelectQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="at least one"):
        decode_stored_answer(multi_select, json.dumps([]))


# ---------------------------------------------------------------------------
# Strict decode: likert / probability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stored", ["0", "6", "-1"])
def test_decode_likert_out_of_range_raises(likert: LikertQuestion, stored: str) -> None:
    with pytest.raises(AnswerValidationError, match="range"):
        decode_stored_answer(likert, stored)


@pytest.mark.parametrize("stored", ["+3", "03", "3.0", " 3"])
def test_decode_likert_non_canonical_form_raises(likert: LikertQuestion, stored: str) -> None:
    with pytest.raises(AnswerValidationError, match="canonical integer"):
        decode_stored_answer(likert, stored)


def test_decode_likert_valid(likert: LikertQuestion) -> None:
    assert decode_stored_answer(likert, "3") == "3"


@pytest.mark.parametrize("stored", ["101"])
def test_decode_probability_out_of_range_raises(
    probability: ProbabilityQuestion, stored: str
) -> None:
    with pytest.raises(AnswerValidationError, match="range"):
        decode_stored_answer(probability, stored)


def test_decode_probability_bounds(probability: ProbabilityQuestion) -> None:
    assert decode_stored_answer(probability, "0") == "0"
    assert decode_stored_answer(probability, "100") == "100"


def test_decode_probability_non_canonical_form_raises(probability: ProbabilityQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="canonical integer"):
        decode_stored_answer(probability, "050")


# ---------------------------------------------------------------------------
# Strict decode: free-text
# ---------------------------------------------------------------------------


def test_decode_free_text_valid(free_text: FreeTextQuestion) -> None:
    text = "Patient stable, no new deficits."
    assert decode_stored_answer(free_text, text) == text


def test_decode_free_text_over_length_raises(free_text: FreeTextQuestion) -> None:
    with pytest.raises(AnswerValidationError, match=str(FREE_TEXT_MAX_CHARS)):
        decode_stored_answer(free_text, "x" * (FREE_TEXT_MAX_CHARS + 1))


def test_decode_free_text_bound_ok(free_text: FreeTextQuestion) -> None:
    assert decode_stored_answer(free_text, "x" * FREE_TEXT_MAX_CHARS) == "x" * FREE_TEXT_MAX_CHARS


def test_decode_free_text_unstripped_raises(free_text: FreeTextQuestion) -> None:
    with pytest.raises(AnswerValidationError, match="whitespace-canonical"):
        decode_stored_answer(free_text, " trailing")


# ---------------------------------------------------------------------------
# Lenient UI paths (unchanged, S9a contract)
# ---------------------------------------------------------------------------


def test_serialize_empty_submission_clears_cell(categorical: CategoricalQuestion) -> None:
    assert serialize_answer(categorical, ["  "]) is None


def test_serialize_multi_select_canonicalizes_order(
    multi_select: MultiSelectQuestion,
) -> None:
    stored = serialize_answer(multi_select, ["Vitals", "Labs"])
    assert json.loads(stored) == ["Labs", "Vitals"]


def test_serialize_then_decode_round_trip(categorical: CategoricalQuestion) -> None:
    stored = serialize_answer(categorical, ["No"])
    assert stored is not None
    assert decode_stored_answer(categorical, stored) == "No"


def test_deserialize_answer_ui_prefill_tolerant(categorical: CategoricalQuestion) -> None:
    # A corrupted persisted value must not break pre-fill — the UI simply
    # shows nothing selected.
    assert deserialize_answer(categorical, "Whatever") == "Whatever"


def test_deserialize_multi_select_prefill(multi_select: MultiSelectQuestion) -> None:
    assert deserialize_answer(multi_select, json.dumps(["Labs", "Vitals"])) == ["Labs", "Vitals"]
    assert deserialize_answer(multi_select, "not json") == []


def test_decode_error_never_names_content(probability: ProbabilityQuestion) -> None:
    with pytest.raises(AnswerValidationError) as exc:
        decode_stored_answer(probability, "101")
    # No clinician identifier, no coordinates: the caller adds those.
    assert "patient" not in str(exc.value).lower()
