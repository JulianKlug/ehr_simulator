"""S12b provisional figures over the committed S11n bundles.

Spec ``specs/session-12-phase25-release.md`` §5, §10 tests 9–13, 16, 17.
Walked bundle (``tests/fixtures/analysis_bundle/build_analysis_bundle.py``):
v1 X two completed cases, Z one incomplete AI case + its open AI
replacement; v2 Y an AI case without AI display (t1 without telemetry) and
a leaking no AI case. v2 adds a multi-select and a probability question.
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.analysis import AnalysisError, ValidatedStudyBundle, load_phase2_bundle
from ehr_simulator.analysis.figures import (
    FIGURES,
    HOLLOW,
    NO_CASES,
    NO_DURATIONS,
    NOT_IN_VERSION,
    SINGLE_ARM,
    build_figure,
    case_divergence_figure,
    save_figure,
)
from ehr_simulator.analysis.schema import AnswerSource, Arm
from ehr_simulator.analysis.summary import AnalysisSummaries, build_summaries
from tests.support.analysis_bundle import EMPTY, FIXTURES, WALKED

pytestmark = pytest.mark.xdist_group("analysis_figures")

STEMS = [
    "figure_01_divergence",
    "figure_02_case_flow",
    "figure_03_response_provenance",
    "figure_04_attention",
    "figure_05_intervention",
]
CASE_STEM = "case_inspection"
SELECTED_PATIENT = "synth_003"
FREE_TEXT_SENTINEL = "FREE-TEXT-SENTINEL never plotted"
_SVG_TEXT = re.compile(r"<text[^>]*>(.*?)</text>", re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


@pytest.fixture(scope="module")
def walked() -> tuple[ValidatedStudyBundle, AnalysisSummaries]:
    bundle = load_phase2_bundle(FIXTURES / WALKED)
    return bundle, build_summaries(bundle)


@pytest.fixture(scope="module")
def empty() -> tuple[ValidatedStudyBundle, AnalysisSummaries]:
    bundle = load_phase2_bundle(FIXTURES / EMPTY)
    return bundle, build_summaries(bundle)


@pytest.fixture(scope="module")
def rendered(walked, tmp_path_factory) -> Path:
    """Every registry figure + the case figure, saved once."""
    bundle, summaries = walked
    out = tmp_path_factory.mktemp("figures")
    for stem in FIGURES:
        save_figure(build_figure(stem, bundle, summaries), out / stem)
    save_figure(case_divergence_figure(bundle, SELECTED_PATIENT), out / CASE_STEM)
    return out


def _frames(plot) -> list[pd.DataFrame]:
    """The plot's own data and every layer's data (public plotnine attributes)."""
    frames = [plot.data] if isinstance(plot.data, pd.DataFrame) else []
    return frames + [layer.geom.data for layer in plot.layers if layer.geom.data is not None]


def _labelled(plot, label: str) -> pd.DataFrame:
    """Rows of the layer frames whose ``label`` (newlines folded) equals *label*."""
    rows = [
        f[f["label"].astype(str).str.replace("\n", " ") == label]
        for f in _frames(plot)
        if "label" in f.columns
    ]
    return pd.concat(rows, ignore_index=True)


def _plot_text(plot) -> list[str]:
    """Title, caption, axis labels and every string cell the plot can draw."""
    labels = plot.labels
    texts = [str(t) for t in (labels.title, labels.caption, labels.x, labels.y) if t]
    for frame in _frames(plot):
        for column in frame.columns:
            texts += [v for v in frame[column].astype(object) if isinstance(v, str)]
    return texts


def _svg_text(path: Path) -> str:
    return " ".join(_TAG.sub("", m) for m in _SVG_TEXT.findall(path.read_text()))


# ---------------------------------------------------------------------------
# Registry and output (tests 10, 12)
# ---------------------------------------------------------------------------


def test_registry_is_the_provisional_set_in_order() -> None:
    assert list(FIGURES) == STEMS


def test_every_figure_writes_svg_and_pdf_with_marks(rendered: Path) -> None:
    for stem in [*STEMS, CASE_STEM]:
        svg, pdf = rendered / f"{stem}.svg", rendered / f"{stem}.pdf"
        assert pdf.read_bytes().startswith(b"%PDF")
        content = svg.read_text()
        assert content.lstrip().startswith("<?xml")
        assert content.count("<path") > 20, stem  # bars, lines, markers beyond the frame


@pytest.mark.parametrize(
    ("stem", "legend"),
    [
        ("figure_01_divergence", ["AI", "No AI", "clinician", "rule"]),
        ("figure_02_case_flow", ["C completed", "I incomplete", "P planned schedule items"]),
        ("figure_03_response_provenance", ["AC answered (clinician)", "AR answered (rule)"]),
        ("figure_04_attention", ["AI", "No AI", "Seconds"]),
        ("figure_05_intervention", ["Y yes / compliant", "? unknown / indeterminate"]),
    ],
)
def test_svg_keeps_legend_title_and_caption_as_text(
    rendered: Path, stem: str, legend: list[str]
) -> None:
    text = _svg_text(rendered / f"{stem}.svg")
    for item in [*legend, "Descriptive only"]:
        assert item in text, item


def test_saving_twice_is_byte_stable(walked, tmp_path: Path) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_02_case_flow", bundle, summaries)
    first = save_figure(plot, tmp_path / "a")
    second = save_figure(plot, tmp_path / "b")
    for a, b in zip(first, second, strict=True):
        assert a.read_bytes() == b.read_bytes()


# ---------------------------------------------------------------------------
# Versions and option order (tests 10, 11)
# ---------------------------------------------------------------------------


def _facets(plot) -> list[str]:
    for frame in _frames(plot):
        if "facet" in frame.columns:
            return [str(c).replace("\n", " ") for c in frame["facet"].cat.categories]
    raise AssertionError("no facet column")


def test_divergence_rows_follow_each_version_snapshot(walked) -> None:
    bundle, summaries = walked
    facets = _facets(build_figure("figure_01_divergence", bundle, summaries))

    for version in bundle.versions.values():
        expected = []
        for q in version.questions.questions:
            if q.response_type in ("likert", "probability-0-100"):
                expected.append(q.question_id)
            elif q.response_type in ("categorical", "multi-select"):
                expected += [f"{q.question_id}: {option}" for option in q.options]
        mine = [f.removeprefix(f"{version.version} · ") for f in facets]
        mine = [f for f, full in zip(mine, facets, strict=True) if full.startswith(version.version)]
        present = [m for m in mine if m in expected]
        assert present == expected  # historical question and option order


def test_v2_only_questions_are_never_pooled_into_v1(walked) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_01_divergence", bundle, summaries)

    absent = _labelled(plot, NOT_IN_VERSION)
    assert set(absent["version"]) == {"v1"}
    assert set(absent["panel"].astype(str).str.split(":").str[0]) == {
        "supporting_findings",
        "deterioration_probability",
    }
    data = pd.concat([f for f in _frames(plot) if "source" in f.columns])
    v1_panels = set(data.loc[data["version"] == "v1", "panel"].astype(str))
    assert not any(p.startswith("supporting_findings") for p in v1_panels)


def test_multi_select_shares_are_independent(walked) -> None:
    bundle, summaries = walked
    shares = summaries.option_proportions
    rows = shares[
        (shares["question_id"] == "supporting_findings")
        & (shares["answer_source"] == AnswerSource.CLINICIAN)
        & shares["proportion"].notna()
    ]
    totals = rows.groupby(["config_version", "t_index", "arm"])["proportion"].sum()
    assert (totals > 1).any()  # multi-select shares need not sum to one


# ---------------------------------------------------------------------------
# Notes, rule answers, empty cohort (tests 7, 13)
# ---------------------------------------------------------------------------


def _one_arm(bundle: ValidatedStudyBundle, arm: str) -> ValidatedStudyBundle:
    keep = bundle.cases[bundle.cases["arm"] == arm][["clinician_id", "patient_id"]]

    def only(frame: pd.DataFrame) -> pd.DataFrame:
        return frame.merge(keep, on=["clinician_id", "patient_id"])

    return dataclasses.replace(
        bundle,
        cases=only(bundle.cases),
        timepoints=only(bundle.timepoints),
        answers=only(bundle.answers),
    )


def test_one_arm_panels_say_no_between_arm_comparison(walked) -> None:
    bundle, _summaries = walked
    one_arm = _one_arm(bundle, Arm.AI.value)
    plot = build_figure("figure_01_divergence", one_arm, build_summaries(one_arm))

    notes = _labelled(plot, SINGLE_ARM)
    answered = pd.concat([f for f in _frames(plot) if "source" in f.columns])
    assert set(answered["arm"]) == {Arm.AI.value}
    # Every answered panel carries the note: never an implied difference.
    assert set(zip(notes["version"], notes["panel"].astype(str), strict=True)) >= set(
        zip(answered["version"], answered["panel"].astype(str), strict=True)
    )


def test_rule_answers_are_plotted_and_distinguished(walked) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_01_divergence", bundle, summaries)

    data = pd.concat([f for f in _frames(plot) if "source" in f.columns])
    rule = data[data["source"] == AnswerSource.RULE]
    clinician = data[data["source"] == AnswerSource.CLINICIAN]
    assert not rule.empty
    assert set(rule["panel"].astype(str).str.split(":").str[0]) == {"death_3mo"}
    assert set(rule["fill"]) == {HOLLOW}
    assert HOLLOW not in set(clinician["fill"])


def test_n_labels_count_answers_per_arm(walked) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_01_divergence", bundle, summaries)

    texts = pd.concat([f for f in _frames(plot) if "label" in f.columns and "arm" in f.columns])
    confidence = texts[(texts["panel"] == "confidence") & (texts["version"] == "v1")]
    # v1 AI t0: X and Z both answered confidence → n=2 (S12a numeric_responses).
    numeric = summaries.numeric_responses
    n = numeric[
        (numeric["config_version"] == "v1")
        & (numeric["question_id"] == "confidence")
        & (numeric["arm"] == Arm.AI)
        & (numeric["t_index"] == 0)
    ]["n"].sum()
    labels = confidence[confidence["arm"] == Arm.AI]["label"].tolist()
    assert labels[0] == f"n={n}"
    assert len(labels) == len(bundle.versions["v1"].timepoints_minutes)


@pytest.mark.parametrize("stem", STEMS)
def test_empty_cohort_renders_a_placeholder_only(empty, stem: str, tmp_path: Path) -> None:
    bundle, summaries = empty
    plot = build_figure(stem, bundle, summaries)

    frames = _frames(plot)
    assert [list(f["label"]) for f in frames] == [[NO_CASES]]
    assert "Descriptive only" in plot.labels.caption
    svg, _pdf = save_figure(plot, tmp_path / stem)
    assert NO_CASES in _svg_text(svg)


# ---------------------------------------------------------------------------
# Regression: ITT arm and unknown telemetry (tests 16, 17)
# ---------------------------------------------------------------------------


def _segments(plot, row: str, version: str) -> dict[str, int]:
    data = plot.data
    mine = data[(data["row"] == row) & (data["col"] == version)]
    return mine.groupby("category", observed=True)["count"].sum().to_dict()


def test_display_failure_and_leakage_keep_the_itt_arm(walked) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_05_intervention", bundle, summaries)

    leak = _segments(plot, "no AI leakage (No AI arm)", "v2")
    assert leak["yes"] == 3  # the leaking no AI case, all three timepoints
    assert _segments(plot, "no AI leakage (AI arm)", "v2")["not_applicable"] == 3
    delivered = _segments(plot, "AI delivered (AI arm)", "v2")
    assert delivered["yes"] == 0 and delivered["no"] + delivered["unknown"] == 3
    assert _segments(plot, "AI viewed (No AI arm)", "v2")["not_applicable"] == 3

    # Each observation stays under its realised arm: exposure never moves it.
    reached = bundle.timepoints[bundle.timepoints["timepoint_reached"]]
    for (version, arm), group in reached.groupby(["config_version", "arm"]):
        label = "AI" if arm == Arm.AI else "No AI"
        assert sum(_segments(plot, f"per protocol ({label} arm)", version).values()) == len(group)


def test_unknown_telemetry_is_excluded_never_zero(walked) -> None:
    bundle, summaries = walked
    plot = build_figure("figure_04_attention", bundle, summaries)

    frames = _frames(plot)
    points = pd.concat([f for f in frames if "label" not in f.columns and "arm" in f.columns])
    assert (points["y"] > 0).all()

    tp = bundle.timepoints
    gap = tp[(tp["config_version"] == "v2") & (tp["arm"] == Arm.AI) & (tp["t_index"] == 1)]
    assert gap["telemetry_status"].item() == "missing"
    x = bundle.versions["v2"].timepoints_minutes[1]
    foreground = points[
        (points["version"] == "v2")
        & (points["measure"] == "foreground (s)")
        & (points["arm"] == Arm.AI)
    ]
    assert not ((foreground["x"] - x).abs() < 10).any()  # no point for the gap

    excluded = _labelled(plot, "excl 1")
    keys = set(zip(excluded["version"], excluded["measure"], excluded["arm"], strict=True))
    assert ("v2", "foreground (s)", Arm.AI.value) in keys
    assert ("v2", "active (s)", Arm.AI.value) in keys
    assert "missing 1" in plot.labels.caption


def test_no_browser_telemetry_shows_no_valid_durations(walked) -> None:
    """A study without the telemetry block: foreground / active are absent, not 0."""
    bundle, _summaries = walked
    tp = bundle.timepoints.assign(
        telemetry_status="not_configured",
        foreground_seconds=pd.NA,
        active_seconds=pd.NA,
    ).astype({"foreground_seconds": "Float64", "active_seconds": "Float64"})
    no_telemetry = dataclasses.replace(bundle, timepoints=tp)
    plot = build_figure("figure_04_attention", no_telemetry, build_summaries(no_telemetry))

    notes = _labelled(plot, NO_DURATIONS)
    assert set(zip(notes["version"], notes["measure"], strict=True)) == {
        (v, m) for v in bundle.versions for m in ("foreground (s)", "active (s)")
    }
    points = pd.concat(
        [f for f in _frames(plot) if "label" not in f.columns and "arm" in f.columns]
    )
    assert set(points["measure"]) == {"wall clock elapsed (s)"}
    assert "not configured" in plot.labels.caption


# ---------------------------------------------------------------------------
# Privacy (test 9)
# ---------------------------------------------------------------------------


def _identifiers(bundle: ValidatedStudyBundle) -> set[str]:
    ids = set(bundle.cases["clinician_id"]) | set(bundle.cases["patient_id"])
    ids |= set(bundle.cases["schedule_id"]) | set(bundle.audit["patient_id"])
    return {i for i in ids if i}


def test_no_identifier_or_free_text_reaches_a_figure(walked, rendered: Path) -> None:
    bundle, _summaries = walked
    # Every answer cell replaced by a sentinel: builders read decoded values only.
    tainted = dataclasses.replace(
        bundle,
        answers=bundle.answers.assign(response_value=FREE_TEXT_SENTINEL),
    )
    tainted_summaries = build_summaries(tainted)

    forbidden = _identifiers(bundle) | {FREE_TEXT_SENTINEL}
    texts = []
    for stem in FIGURES:
        texts += _plot_text(build_figure(stem, tainted, tainted_summaries))
        texts.append(_svg_text(rendered / f"{stem}.svg"))
    texts += _plot_text(case_divergence_figure(tainted, SELECTED_PATIENT))
    texts.append(_svg_text(rendered / f"{CASE_STEM}.svg"))

    for text in texts:
        for item in forbidden:
            assert item not in text, item


def test_figure_stems_are_fixed_names() -> None:
    for stem in FIGURES:
        assert re.fullmatch(r"figure_0[1-5]_[a-z_]+", stem)


def test_case_figure_labels_cases_by_ordinal(walked) -> None:
    bundle, _summaries = walked
    plot = case_divergence_figure(bundle, SELECTED_PATIENT)

    assert SELECTED_PATIENT not in plot.labels.title
    labels = {v for f in _frames(plot) if "series" in f.columns for v in f["series"].astype(str)}
    cases = bundle.cases[bundle.cases["patient_id"] == SELECTED_PATIENT]
    assert labels == {f"case {i}" for i in range(1, len(cases) + 1)}


def test_case_figure_refuses_an_unknown_patient(walked) -> None:
    bundle, _summaries = walked
    with pytest.raises(AnalysisError, match="no activated case"):
        case_divergence_figure(bundle, "synth_999")
