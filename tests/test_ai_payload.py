"""S8 AI payload families, bounded contributor selection and exposed ids."""

from __future__ import annotations

import math

import pytest

from ehr_simulator.web.ai_payload import (
    AIDetail,
    PayloadFamily,
    check_payload,
    exposed_ai_field_ids,
    plan_display,
    top_contributors,
)
from tests.support.geneva_ui import contributions


def _geneva(probability: object = 0.3, **explanation: object) -> dict:
    payload: dict = {"probability": probability}
    if explanation:
        payload["explanation"] = explanation
    return payload


def test_selects_deterministic_top_positive_and_negative() -> None:
    selected = top_contributors(contributions(0.0))
    assert [c.feature for c in selected.increasing] == [
        "lag2_glucose",
        "median_NIHSS",
        "age",
        "avg_median_heart_rate",
        "max_systolic_blood_pressure",
    ]
    assert [c.feature for c in selected.decreasing] == [
        "creatinine",
        "sex_male",
        "unmapped_feature_x",
        "cbf_lt_30",
        "rolling_mean_FIO2",
    ]
    assert selected == top_contributors(dict(reversed(list(contributions(0.0).items()))))


def test_zeros_excluded_no_padding_and_tie_break() -> None:
    selected = top_contributors({"b": 0.2, "a": 0.2, "c": -0.2, "z": 0.0, "timestep_idx": 0.2})
    assert [c.feature for c in selected.increasing] == ["a", "b", "timestep_idx"]
    assert [c.feature for c in selected.decreasing] == ["c"]
    assert selected.increasing[0].label is None  # unresolved → raw name
    assert selected.increasing[2].label == "Time since admission"


def test_probability_only_payload_is_valid() -> None:
    checked = check_payload(_geneva(0.42))
    assert (checked.family, checked.state) == (PayloadFamily.GENEVA, "loading")
    assert checked.probability == 0.42
    assert checked.contributors is None


def test_full_geneva_payload_is_valid() -> None:
    checked = check_payload(_geneva(0.42, base_value=-0.6, contributions={"age": 0.1}))
    assert checked.state == "loading"
    assert [c.feature for c in checked.contributors.all] == ["age"]


@pytest.mark.parametrize(
    "payload",
    [
        _geneva(1.2),
        _geneva(-0.1),
        _geneva(math.nan),
        _geneva("0.4"),
        _geneva(True),
        _geneva(0.4, base_value="x", contributions={}),
        _geneva(0.4, base_value=0.0, contributions=[1, 2]),
        _geneva(0.4, base_value=0.0, contributions={"a": math.inf}),
        {"probability": 0.4, "explanation": "nope"},
        {"prob_deterioration_6h": 0.1},
    ],
)
def test_malformed_payload_is_partial(payload: dict) -> None:
    assert check_payload(payload).state == "partial"


def test_partial_geneva_keeps_fields_that_passed() -> None:
    bad_probability = check_payload(_geneva(2.0, base_value=0.0, contributions={"age": 0.1}))
    assert bad_probability.probability is None
    assert bad_probability.contributors is not None
    bad_explanation = check_payload(_geneva(0.3, base_value=None, contributions={}))
    assert (bad_explanation.probability, bad_explanation.contributors) == (0.3, None)


def test_synthetic_family_unchanged() -> None:
    checked = check_payload({"prob_deterioration_6h": 0.1, "prob_mrs_0_2_90d": 0.2})
    assert (checked.family, checked.state) == (PayloadFamily.SYNTHETIC, "loading")


def test_exposed_ids_are_the_displayed_fields_only() -> None:
    payload = _geneva(0.3, base_value=-0.6, contributions=contributions(0.0))
    ids = exposed_ai_field_ids(payload)
    assert "ai:probability" in ids
    assert "ai:explanation.contributions.lag2_glucose" in ids
    assert "ai:explanation.contributions.timestep_idx" not in ids  # sixth positive
    assert "ai:explanation.contributions.std_glucose" not in ids  # zero
    assert "ai:explanation" not in ids and "ai:explanation.base_value" not in ids
    assert len(ids) == 11
    assert exposed_ai_field_ids(payload, AIDetail.PROBABILITY_ONLY) == {"ai:probability"}
    assert exposed_ai_field_ids({"prob_deterioration_6h": 0.1}) == {"ai:prob_deterioration_6h"}


def test_legacy_plan_shows_latest_geneva_row_in_full() -> None:
    synthetic = {"prob_deterioration_6h": 0.1, "prob_mrs_0_2_90d": 0.2}
    plan = plan_display([_geneva(0.1), synthetic, _geneva(0.2), _geneva(0.3)])
    assert plan == [
        AIDetail.PROBABILITY_ONLY,
        AIDetail.FULL,
        AIDetail.PROBABILITY_ONLY,
        AIDetail.FULL,
    ]
