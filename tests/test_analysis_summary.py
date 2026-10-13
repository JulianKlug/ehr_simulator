"""S12a descriptive summaries over the committed S11n bundles.

Spec ``specs/session-12-phase25-release.md`` §3, §5, §10 tests 5–8, 13.
The walked bundle (``tests/fixtures/analysis_bundle/build_analysis_bundle.py``):
v1 X two completed cases, Z one incomplete AI case + its open AI
replacement; v2 Y an AI case without AI display (t1 without telemetry) and
a leaking no AI case.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ehr_simulator.analysis import load_phase2_bundle
from ehr_simulator.analysis.summary import (
    AnalysisSummaries,
    Cohort,
    build_summaries,
    numeric_observations,
)
from tests.support.analysis_bundle import EMPTY, FIXTURES, WALKED

RATE_COLUMNS = ("proportion", "median", "min", "max")


@pytest.fixture(scope="module")
def walked() -> AnalysisSummaries:
    return build_summaries(load_phase2_bundle(FIXTURES / WALKED))


def _one(frame: pd.DataFrame, **match: object) -> pd.Series:
    rows = frame
    for column, value in match.items():
        rows = rows[rows[column] == value]
    assert len(rows) == 1, match
    return rows.iloc[0]


# ---------------------------------------------------------------------------
# Test 5: planned items are never activated cases
# ---------------------------------------------------------------------------


def test_case_flow_keeps_planned_apart_from_activated(walked: AnalysisSummaries) -> None:
    flow = walked.case_flow
    bundle = load_phase2_bundle(FIXTURES / WALKED)

    assert flow["planned_items"].sum() == len(bundle.audit)
    assert flow["activated"].sum() == len(bundle.cases)
    assert flow["activated"].sum() < flow["planned_items"].sum()
    v2_no_ai = _one(flow, config_version="v2", arm="no_ai")
    assert (v2_no_ai["planned_items"], v2_no_ai["activated"]) == (2, 1)


def test_replacement_chain_counts_each_case_once(walked: AnalysisSummaries) -> None:
    v1_ai = _one(walked.case_flow, config_version="v1", arm="ai")

    # X's AI case, Z's lost case and Z's replacement: three activations.
    assert (v1_ai["activated"], v1_ai["completed"], v1_ai["incomplete"], v1_ai["open"]) == (
        3,
        1,
        1,
        1,
    )
    assert (v1_ai["replacement_planned"], v1_ai["replacement_activated"]) == (1, 1)
    assert v1_ai["replacement_pending"] == 0
    assert walked.case_flow["replacement_planned"].sum() == 1


# ---------------------------------------------------------------------------
# Test 6: hidden is not missing; rule answers keep their provenance
# ---------------------------------------------------------------------------


def test_every_observation_has_exactly_one_status(walked: AnalysisSummaries) -> None:
    p = walked.response_provenance
    missing = p.filter(like="missing_").sum(axis=1)

    assert (
        p["answered_clinician"] + p["answered_rule"] + p["not_applicable"] + missing
        == p["observations"]
    ).all()


def test_hidden_questions_are_not_missing(walked: AnalysisSummaries) -> None:
    cause = walked.response_provenance[walked.response_provenance["question_id"] == "primary_cause"]
    assert cause["not_applicable"].sum() > 0

    denominators = walked.denominators
    for row in cause.itertuples():
        d = _one(
            denominators,
            measure="answered",
            config_version=row.config_version,
            arm=row.arm,
            t_index=row.t_index,
            question_id="primary_cause",
        )
        assert d["denominator"] == row.reached_applicable <= row.reached
        assert d["missing_unknown"] <= d["denominator"]


def test_rule_answers_stay_distinguishable(walked: AnalysisSummaries) -> None:
    death = walked.response_provenance[walked.response_provenance["question_id"] == "death_3mo"]
    assert death["answered_rule"].sum() > 0 and death["answered_clinician"].sum() > 0

    shares = walked.option_proportions
    rule = shares[(shares["question_id"] == "death_3mo") & (shares["answer_source"] == "rule")]
    assert rule["count"].sum() == death["answered_rule"].sum()


# ---------------------------------------------------------------------------
# Test 7: denominators and option order per response type
# ---------------------------------------------------------------------------


def test_options_follow_the_historical_order(walked: AnalysisSummaries) -> None:
    bundle = load_phase2_bundle(FIXTURES / WALKED)
    shares = walked.option_proportions
    for version in bundle.versions.values():
        for question in version.questions.questions:
            if question.response_type not in ("categorical", "multi-select"):
                continue
            rows = shares[
                (shares["config_version"] == version.version)
                & (shares["question_id"] == question.question_id)
            ]
            for _key, group in rows.groupby(["t_index", "arm", "answer_source"]):
                assert list(group.sort_values("option_order")["option"]) == question.options


def test_multi_select_shares_are_independent(walked: AnalysisSummaries) -> None:
    rows = walked.option_proportions[
        (walked.option_proportions["question_id"] == "supporting_findings")
        & (walked.option_proportions["t_index"] == 0)
        & (walked.option_proportions["answer_source"] == "clinician")
        & (walked.option_proportions["arm"] == "ai")
    ]
    assert rows["answers"].unique().tolist() == [1]
    assert rows["proportion"].sum() == pytest.approx(2.0)  # two options of one answer


def test_categorical_shares_sum_to_one_where_answered(walked: AnalysisSummaries) -> None:
    cat = walked.option_proportions[walked.option_proportions["response_type"] == "categorical"]
    for _key, group in cat.groupby(["config_version", "question_id", "t_index", "arm"]):
        clinician = group[group["answer_source"] == "clinician"]
        if clinician["answers"].iloc[0] == 0:
            assert clinician["proportion"].isna().all()  # never 0 / 0
            continue
        assert clinician["proportion"].sum() == pytest.approx(1.0)


def test_numeric_summary_matches_observations(walked: AnalysisSummaries) -> None:
    bundle = load_phase2_bundle(FIXTURES / WALKED)
    points = numeric_observations(bundle)

    row = _one(
        walked.numeric_responses,
        config_version="v1",
        question_id="confidence",
        t_index=0,
        arm="ai",
        answer_source="clinician",
    )
    values = points[
        (points["config_version"] == "v1")
        & (points["question_id"] == "confidence")
        & (points["t_index"] == 0)
        & (points["arm"] == "ai")
    ]["value_number"]
    assert row["n"] == len(values) == 2  # Z's lost case left confidence blank
    assert row["median"] == values.median()


def test_every_cell_exists_for_both_arms(walked: AnalysisSummaries) -> None:
    """A one arm or unanswered cell is an explicit zero, never a missing row."""
    for frame in (walked.response_provenance, walked.numeric_responses, walked.attention):
        keys = [c for c in ("config_version", "question_id", "t_index") if c in frame]
        arms = frame.groupby(keys)["arm"].agg(frozenset)
        assert (arms == frozenset({"ai", "no_ai"})).all()

    empty = walked.numeric_responses[walked.numeric_responses["n"] == 0]
    assert not empty.empty and empty["median"].isna().all()


# ---------------------------------------------------------------------------
# Test 8: unknown telemetry / PP are never zero or false
# ---------------------------------------------------------------------------


def test_missing_telemetry_is_excluded_not_zero(walked: AnalysisSummaries) -> None:
    row = _one(walked.attention, config_version="v2", arm="ai", t_index=1)

    assert (row["reached"], row["telemetry_missing"]) == (1, 1)
    assert row["foreground_n"] == row["active_n"] == 0
    assert pd.isna(row["foreground_median_seconds"]) and pd.isna(row["active_median_seconds"])
    assert row["elapsed_n"] == 1  # S10 wall clock is independent of browser telemetry


def test_indeterminate_delivery_and_pp_are_kept(walked: AnalysisSummaries) -> None:
    silent = _one(walked.intervention, config_version="v2", arm="ai", t_index=1)
    assert (silent["delivered_unknown"], silent["viewed_unknown"]) == (1, 1)
    assert (silent["pp_indeterminate"], silent["pp_noncompliant"]) == (1, 0)

    failed = _one(walked.intervention, config_version="v2", arm="ai", t_index=0)
    assert (failed["delivered_false"], failed["failure_display_failure"]) == (1, 1)


def test_no_ai_viewing_is_not_applicable_and_leakage_stays_itt(
    walked: AnalysisSummaries,
) -> None:
    leaked = _one(walked.intervention, config_version="v2", arm="no_ai", t_index=0)

    assert leaked["viewed_not_applicable"] == leaked["reached"] == 1
    assert pd.isna(leaked["viewed_false"]) and pd.isna(leaked["delivered_true"])
    assert (leaked["leakage"], leaked["pp_noncompliant"]) == (1, 1)


def test_denominators_are_consistent(walked: AnalysisSummaries) -> None:
    d = walked.denominators
    assert (d["n"] >= 0).all() and (d["missing_unknown"] >= 0).all()
    assert (d["n"] + d["missing_unknown"] <= d["denominator"]).all()
    assert set(d["config_version"]) == {"v1", "v2"}


# ---------------------------------------------------------------------------
# Test 13: no activated cases
# ---------------------------------------------------------------------------


def test_empty_bundle_reports_no_activated_cases() -> None:
    summaries = build_summaries(load_phase2_bundle(FIXTURES / EMPTY))

    assert summaries.cohort is Cohort.NO_ACTIVATED_CASES
    assert summaries.case_flow["activated"].sum() == 0
    for frame in (
        summaries.option_proportions,
        summaries.numeric_responses,
        summaries.attention,
    ):
        for column in RATE_COLUMNS + ("elapsed_median_seconds",):
            if column in frame:
                assert frame[column].isna().all()
    assert (summaries.denominators["n"] == 0).all()
