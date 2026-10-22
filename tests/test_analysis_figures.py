"""S12b supplementary figures over the committed S11n bundles.

Spec ``specs/session-12-phase25-release.md`` §5, §10 tests 9–13, 16, 17.
Walked bundle (``tests/fixtures/analysis_bundle/build_analysis_bundle.py``):
v1 X two completed cases, Z one incomplete AI case + its open AI
replacement; v2 Y an AI case without AI display (t1 without telemetry) and
a leaking no AI case. v2 adds a multi-select and a probability question.
Every figure shows one config version (owner decision 2026-10-19).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from enum import StrEnum
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.analysis import AnalysisError, ValidatedStudyBundle, load_phase2_bundle
from ehr_simulator.analysis.figures import (
    FIGURES,
    HOLLOW,
    NO_CASES,
    NO_DURATIONS,
    ONE_COLUMN_INCHES,
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
    "supplementary_01_divergence",
    "supplementary_02_case_flow",
    "supplementary_03_response_provenance",
    "supplementary_04_attention",
    "supplementary_05_intervention",
]
DIVERGENCE, CASE_FLOW, PROVENANCE, ATTENTION, INTERVENTION = STEMS
VERSIONS = ["v1", "v2"]
FIGURES_BY_VERSION = list(itertools.product(STEMS, VERSIONS))
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


def _name(stem: str, version: str) -> str:
    return f"{stem}_{version}"


@pytest.fixture(scope="module")
def rendered(walked, tmp_path_factory) -> Path:
    """Every registry figure + the case figure, once per version."""
    bundle, summaries = walked
    out = tmp_path_factory.mktemp("figures")
    for stem, version in FIGURES_BY_VERSION:
        save_figure(build_figure(stem, bundle, summaries, version), out / _name(stem, version))
    for version in VERSIONS:
        plot = case_divergence_figure(bundle, SELECTED_PATIENT, version)
        save_figure(plot, out / _name(CASE_STEM, version))
    return out


def _build(walked, stem: str, version: str):
    bundle, summaries = walked
    return build_figure(stem, bundle, summaries, version)


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


def test_registry_is_the_supplementary_set_in_order() -> None:
    assert list(FIGURES) == STEMS


def test_every_figure_writes_svg_and_pdf_with_marks(rendered: Path) -> None:
    names = [_name(stem, v) for stem, v in FIGURES_BY_VERSION]
    for name in [*names, *(_name(CASE_STEM, v) for v in VERSIONS)]:
        svg, pdf = rendered / f"{name}.svg", rendered / f"{name}.pdf"
        assert pdf.read_bytes().startswith(b"%PDF")
        content = svg.read_text()
        assert content.lstrip().startswith("<?xml")
        assert content.count("<path") > 20, name  # bars, lines, markers beyond the frame


@pytest.mark.parametrize(
    ("stem", "version", "legend"),
    [
        (DIVERGENCE, "v1", ["AI", "No AI", "clinician", "rule"]),
        (CASE_FLOW, "v1", ["C completed", "I incomplete", "P planned schedule items"]),
        (PROVENANCE, "v2", ["AC answered (clinician)", "AR answered (rule)"]),
        (ATTENTION, "v1", ["AI", "No AI", "Seconds"]),
        (INTERVENTION, "v2", ["Y yes / compliant", "? unknown / indeterminate"]),
    ],
)
def test_svg_keeps_legend_title_and_caption_as_text(
    rendered: Path, stem: str, version: str, legend: list[str]
) -> None:
    text = _svg_text(rendered / f"{_name(stem, version)}.svg")
    for item in [*legend, "Descriptive only"]:
        assert item in text, item


def test_saving_twice_is_byte_stable(walked, tmp_path: Path) -> None:
    plot = _build(walked, CASE_FLOW, "v1")
    first = save_figure(plot, tmp_path / "a")
    second = save_figure(plot, tmp_path / "b")
    for a, b in zip(first, second, strict=True):
        assert a.read_bytes() == b.read_bytes()


def test_save_keeps_a_dotted_version_in_the_file_name(walked, tmp_path: Path) -> None:
    """A config version may contain dots (``v1.2``): never read as a suffix."""
    written = save_figure(_build(walked, CASE_FLOW, "v1"), tmp_path / "supplementary_v1.2")
    assert [p.name for p in written] == ["supplementary_v1.2.svg", "supplementary_v1.2.pdf"]


# ---------------------------------------------------------------------------
# One config version per figure (owner decision 2026-10-19)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("stem", "version"), FIGURES_BY_VERSION)
def test_figure_names_its_version_and_never_another(
    rendered: Path, stem: str, version: str
) -> None:
    text = _svg_text(rendered / f"{_name(stem, version)}.svg")
    assert f"Config version {version}" in text
    for other in VERSIONS:
        if other != version:
            assert other not in text, other


@pytest.mark.parametrize(("stem", "version"), FIGURES_BY_VERSION)
def test_figure_data_and_strips_hold_one_version(walked, stem: str, version: str) -> None:
    plot = _build(walked, stem, version)
    for frame in _frames(plot):
        if "version" in frame.columns:
            assert set(frame["version"].astype(str)) <= {version}
        if "config_version" in frame.columns:
            assert set(frame["config_version"].astype(str)) <= {version}
    for strip in _facets(plot) if _has_facets(plot) else []:
        assert not any(v in strip.split(" ") for v in VERSIONS), strip


def test_unknown_version_is_refused(walked) -> None:
    with pytest.raises(AnalysisError, match="not in this bundle"):
        _build(walked, CASE_FLOW, "v9")


def test_case_figure_shows_only_its_version(walked, rendered: Path) -> None:
    bundle, _summaries = walked
    for version in VERSIONS:
        plot = case_divergence_figure(bundle, SELECTED_PATIENT, version)
        frames = [f for f in _frames(plot) if "version" in f.columns]
        assert {v for f in frames for v in f["version"].astype(str)} == {version}
        text = _svg_text(rendered / f"{_name(CASE_STEM, version)}.svg")
        assert f"Config version {version}" in text
        assert not any(v in text for v in VERSIONS if v != version)


# ---------------------------------------------------------------------------
# Versions and option order (tests 10, 11)
# ---------------------------------------------------------------------------


def _has_facets(plot) -> bool:
    return any("facet" in frame.columns for frame in _frames(plot))


def _facets(plot) -> list[str]:
    for frame in _frames(plot):
        if "facet" in frame.columns:
            return [str(c).replace("\n", " ") for c in frame["facet"].cat.categories]
    raise AssertionError("no facet column")


@pytest.mark.parametrize("version", VERSIONS)
def test_divergence_rows_follow_the_version_snapshot(walked, version: str) -> None:
    bundle, _summaries = walked
    facets = _facets(_build(walked, DIVERGENCE, version))

    expected = []
    for q in bundle.versions[version].questions.questions:
        if q.response_type in ("likert", "probability-0-100"):
            expected.append(q.question_id)
        elif q.response_type in ("categorical", "multi-select"):
            binary = len(q.options) == 2
            expected.append(f"{q.question_id}: {q.options[0]}" if binary else q.question_id)
    assert facets == expected  # historical question and option order, no version prefix


def test_v2_only_questions_are_never_pooled_into_v1(walked) -> None:
    v2_only = {"supporting_findings", "deterioration_probability"}
    v1 = {f.split(":")[0] for f in _facets(_build(walked, DIVERGENCE, "v1"))}
    v2 = {f.split(":")[0] for f in _facets(_build(walked, DIVERGENCE, "v2"))}
    assert not v1 & v2_only
    assert v2_only <= v2


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


@pytest.mark.parametrize("version", VERSIONS)
def test_one_arm_panels_say_no_between_arm_comparison(walked, version: str) -> None:
    bundle, _summaries = walked
    one_arm = _one_arm(bundle, Arm.AI.value)
    plot = build_figure(DIVERGENCE, one_arm, build_summaries(one_arm), version)

    notes = _labelled(plot, SINGLE_ARM)
    answered = pd.concat([f for f in _frames(plot) if "source" in f.columns])
    assert set(answered["arm"]) == {Arm.AI.value}
    # Every answered panel carries the note: never an implied difference.
    assert set(notes["panel"].astype(str)) >= set(answered["panel"].astype(str))


def test_rule_answers_are_plotted_and_distinguished(walked) -> None:
    plots = [_build(walked, DIVERGENCE, version) for version in VERSIONS]

    data = pd.concat([f for plot in plots for f in _frames(plot) if "source" in f.columns])
    rule = data[data["source"] == AnswerSource.RULE]
    clinician = data[data["source"] == AnswerSource.CLINICIAN]
    assert not rule.empty
    assert set(rule["panel"].astype(str).str.split(":").str[0]) == {"death_3mo"}
    assert set(rule["fill"]) == {HOLLOW}
    assert HOLLOW not in set(clinician["fill"])


def test_n_labels_count_answers_per_arm(walked) -> None:
    bundle, summaries = walked
    plot = _build(walked, DIVERGENCE, "v1")

    texts = _layer_frame(plot, Layer.COUNTS)
    confidence = texts[texts["panel"] == "confidence"]
    # v1 AI t0: X and Z both answered confidence → n=2 (S12a numeric_responses).
    numeric = summaries.numeric_responses
    n = numeric[
        (numeric["config_version"] == "v1")
        & (numeric["question_id"] == "confidence")
        & (numeric["arm"] == Arm.AI)
        & (numeric["t_index"] == 0)
    ]["n"].sum()
    labels = confidence[confidence["arm"] == Arm.AI]["label"].tolist()
    assert labels[0] == str(n)
    assert len(labels) == len(bundle.versions["v1"].timepoints_minutes)

    keys = _layer_frame(plot, Layer.KEYS)
    mine = keys[keys["panel"] == "confidence"]
    assert sorted(mine["label"]) == ["▲ n", "● n"]  # one count row per arm, keyed once


class Layer(StrEnum):
    """Figure 1 text / line layers, told apart by geom and alignment."""

    LINES = "lines"  # drawn segments
    MEDIANS = "medians"  # large markers: medians / shares, one per series point
    COUNTS = "counts"  # centred n per timepoint
    KEYS = "keys"  # right aligned arm glyphs
    ENDS = "ends"  # left aligned option labels
    LEADERS = "leaders"  # segments from line ends to their option labels


_TEXT_ALIGN = {Layer.COUNTS: "center", Layer.KEYS: "right", Layer.ENDS: "left"}


def _layer_frame(plot, kind: Layer) -> pd.DataFrame:
    after_line = False
    for layer in plot.layers:
        frame = layer.geom.data
        name = type(layer.geom).__name__
        if kind is Layer.MEDIANS and after_line:
            return frame
        after_line = name == "geom_line"
        if frame is None or frame.empty:
            continue
        if kind is Layer.LINES and name == "geom_line":
            return frame
        if kind is Layer.LEADERS and name == "geom_segment":
            return frame
        ha = layer.geom.aes_params.get("ha", "center")
        if name == "geom_text" and ha == _TEXT_ALIGN.get(kind):
            return frame
    raise AssertionError(f"no {kind} layer")


@pytest.mark.parametrize("stem", STEMS)
def test_empty_cohort_renders_a_placeholder_only(empty, stem: str, tmp_path: Path) -> None:
    """One placeholder per registered version (the empty bundle has v1 only)."""
    bundle, summaries = empty
    assert list(bundle.versions) == ["v1"]
    plot = build_figure(stem, bundle, summaries, "v1")

    frames = _frames(plot)
    assert [list(f["label"]) for f in frames] == [[NO_CASES]]
    assert "Descriptive only" in plot.labels.caption
    svg, _pdf = save_figure(plot, tmp_path / stem)
    assert NO_CASES in _svg_text(svg)
    assert "Config version v1" in _svg_text(svg)


@pytest.mark.parametrize("stem", STEMS)
def test_version_without_cases_renders_a_placeholder(walked, stem: str) -> None:
    """A version without activated cases is a placeholder, even when another has cases."""
    bundle, _summaries = walked
    v1_only = _only_cases_of(bundle, "v1")
    summaries = build_summaries(v1_only)

    plot = build_figure(stem, v1_only, summaries, "v2")
    assert [list(f["label"]) for f in _frames(plot)] == [[NO_CASES]]
    assert NO_CASES not in _plot_text(build_figure(stem, v1_only, summaries, "v1"))


def _only_cases_of(bundle: ValidatedStudyBundle, version: str) -> ValidatedStudyBundle:
    def only(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[frame["config_version"] == version]

    return dataclasses.replace(
        bundle,
        cases=only(bundle.cases),
        timepoints=only(bundle.timepoints),
        answers=only(bundle.answers),
    )


# ---------------------------------------------------------------------------
# Regression: ITT arm and unknown telemetry (tests 16, 17)
# ---------------------------------------------------------------------------


def _segments(plot, row: str) -> dict[str, int]:
    data = plot.data
    mine = data[data["row"] == row]
    return mine.groupby("category", observed=True)["count"].sum().to_dict()


def test_display_failure_and_leakage_keep_the_itt_arm(walked) -> None:
    bundle, _summaries = walked
    plot = _build(walked, INTERVENTION, "v2")

    leak = _segments(plot, "AI leakage (No AI arm)")
    assert leak["yes"] == 3  # the leaking no AI case, all three timepoints
    delivered = _segments(plot, "AI delivered (AI arm)")
    assert delivered["yes"] == 0 and delivered["no"] + delivered["unknown"] == 3

    # Each observation stays under its realised arm: exposure never moves it.
    reached = bundle.timepoints[bundle.timepoints["timepoint_reached"]]
    plots = {version: _build(walked, INTERVENTION, version) for version in VERSIONS}
    for (version, arm), group in reached.groupby(["config_version", "arm"]):
        label = "AI" if arm == Arm.AI else "No AI"
        row = f"per protocol ({label} arm)"
        assert sum(_segments(plots[version], row).values()) == len(group)


def test_unknown_telemetry_is_excluded_never_zero(walked) -> None:
    bundle, _summaries = walked
    plot = _build(walked, ATTENTION, "v2")

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

    strips = _facets(plot)
    assert "foreground (s) excl: AI t60 1" in strips
    assert "active (s) excl: AI t60 1" in strips
    assert "missing 1" in plot.labels.caption


@pytest.mark.parametrize("version", VERSIONS)
def test_no_browser_telemetry_shows_no_valid_durations(walked, version: str) -> None:
    """A study without the telemetry block: foreground / active are absent, not 0."""
    bundle, _summaries = walked
    tp = bundle.timepoints.assign(
        telemetry_status="not_configured",
        foreground_seconds=pd.NA,
        active_seconds=pd.NA,
    ).astype({"foreground_seconds": "Float64", "active_seconds": "Float64"})
    no_telemetry = dataclasses.replace(bundle, timepoints=tp)
    plot = build_figure(ATTENTION, no_telemetry, build_summaries(no_telemetry), version)

    notes = _labelled(plot, NO_DURATIONS)
    assert set(zip(notes["version"], notes["measure"], strict=True)) == {
        (version, m) for m in ("foreground (s)", "active (s)")
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
    for stem, version in FIGURES_BY_VERSION:
        texts += _plot_text(build_figure(stem, tainted, tainted_summaries, version))
        texts.append(_svg_text(rendered / f"{_name(stem, version)}.svg"))
    for version in VERSIONS:
        texts += _plot_text(case_divergence_figure(tainted, SELECTED_PATIENT, version))
        texts.append(_svg_text(rendered / f"{_name(CASE_STEM, version)}.svg"))

    for text in texts:
        for item in forbidden:
            assert item not in text, item


def test_figure_stems_are_fixed_names() -> None:
    for stem in FIGURES:
        assert re.fullmatch(r"supplementary_0[1-5]_[a-z_]+", stem)


@pytest.mark.parametrize("version", VERSIONS)
def test_case_figure_labels_cases_by_ordinal(walked, version: str) -> None:
    bundle, _summaries = walked
    plot = case_divergence_figure(bundle, SELECTED_PATIENT, version)

    assert SELECTED_PATIENT not in plot.labels.title
    labels = {v for f in _frames(plot) if "series" in f.columns for v in f["series"].astype(str)}
    cases = bundle.cases[
        (bundle.cases["patient_id"] == SELECTED_PATIENT)
        & (bundle.cases["config_version"] == version)
    ]
    assert labels == {f"case {i}" for i in range(1, len(cases) + 1)}


@pytest.mark.parametrize(("patient", "version"), [("synth_999", "v1"), ("synth_002", "v2")])
def test_case_figure_refuses_a_patient_without_a_case_of_the_version(
    walked, patient: str, version: str
) -> None:
    bundle, _summaries = walked
    with pytest.raises(AnalysisError, match="no activated case"):
        case_divergence_figure(bundle, patient, version)


def test_timepoint_labels_keep_distinct_minutes_apart() -> None:
    """``:g`` rounds to six digits: 1440.12 and 1440.125 must not share a bar."""
    from ehr_simulator.analysis.figures import _t_label

    assert _t_label(1440.12) != _t_label(1440.125)
    assert _t_label(60.0) == "60"


# ---------------------------------------------------------------------------
# Print legibility (review: one column, greyscale, small text)
# ---------------------------------------------------------------------------

MIN_TEXT_POINTS = 6
MAX_PAGE_INCHES = 9.5


@pytest.mark.parametrize(("stem", "version"), FIGURES_BY_VERSION)
def test_text_is_legible_in_print(walked, stem: str, version: str) -> None:
    """At least 6 pt, and arm text is told apart by its words, not its colour."""
    plot = _build(walked, stem, version)

    texts = [layer for layer in plot.layers if "label" in layer.mapping]
    for layer in texts:
        assert layer.geom.aes_params["size"] >= MIN_TEXT_POINTS
        assert "color" not in layer.mapping
    assert plot.theme.getp(("plot_caption", "size")) >= MIN_TEXT_POINTS
    assert plot.theme.getp(("strip_text", "size")) >= MIN_TEXT_POINTS  # e.g. "excl: AI t60 1"
    assert plot.theme.getp(("plot_subtitle", "size")) >= MIN_TEXT_POINTS


@pytest.mark.parametrize(("stem", "version"), FIGURES_BY_VERSION)
def test_figures_fit_a_page(walked, stem: str, version: str) -> None:
    """Every figure of the walked bundle fits one page at one column width."""
    width, height = _build(walked, stem, version).theme.getp("figure_size")
    assert width == ONE_COLUMN_INCHES
    assert height <= MAX_PAGE_INCHES


@pytest.mark.parametrize(("stem", "version"), FIGURES_BY_VERSION)
def test_captions_start_at_the_figure_edge(walked, stem: str, version: str) -> None:
    """Anchored at the plot, not the panels: a caption is never clipped right."""
    plot = _build(walked, stem, version)
    assert plot.theme.getp("plot_caption_position") == "plot"
    assert plot.theme.getp(("plot_caption", "linespacing")) >= MIN_CAPTION_LINESPACING


# ---------------------------------------------------------------------------
# Owner relayout decisions (S12c review)
# ---------------------------------------------------------------------------

MIN_CAPTION_LINESPACING = 1.2
BINARY_PANEL = "death_3mo: Yes"
MIN_LIGHTNESS_GAP = 15
#: WCAG AA for normal text.
MIN_TEXT_CONTRAST = 4.5
WHITE, BLACK = "#FFFFFF", "#000000"


def _caption(plot) -> str:
    return plot.labels.caption.replace("\n", " ")


def test_divergence_has_one_panel_per_question(walked) -> None:
    bundle, _summaries = walked
    plot = _build(walked, DIVERGENCE, "v2")

    mine = _layer_frame(plot, Layer.COUNTS)
    v2 = bundle.versions["v2"]
    assert mine["panel"].nunique() == len(v2.questions.questions)
    # The count strip appears once per panel: one count per timepoint and arm.
    per_panel = mine.groupby("panel", observed=True).size()
    assert (per_panel == len(v2.timepoints_minutes) * len(Arm)).all()


def test_binary_question_plots_only_its_first_option(walked) -> None:
    _bundle, summaries = walked
    plot = _build(walked, DIVERGENCE, "v2")

    lines = _layer_frame(plot, Layer.MEDIANS)
    shares = summaries.option_proportions
    expected = shares[
        (shares["config_version"] == "v2")
        & (shares["question_id"] == "death_3mo")
        & (shares["option_order"] == 0)
        & shares["proportion"].notna()
    ]
    death = lines[lines["panel"] == BINARY_PANEL]
    assert sorted(death["y"]) == sorted(float(v) for v in expected["proportion"])
    assert not lines["panel"].astype(str).str.endswith(": No").any()
    ends = _layer_frame(plot, Layer.ENDS)
    assert not ends["panel"].astype(str).str.startswith("death_3mo").any()


def test_multi_option_lines_are_labelled_at_their_end(walked) -> None:
    bundle, _summaries = walked
    plot = _build(walked, DIVERGENCE, "v2")

    ends = _layer_frame(plot, Layer.ENDS)
    findings = ends[ends["panel"] == "supporting_findings"]
    question = next(
        q
        for q in bundle.versions["v2"].questions.questions
        if q.question_id == "supporting_findings"
    )
    assert sorted(findings["label"].str.replace("\n", " ")) == sorted(question.options)
    assert findings["y"].round(6).is_unique  # spread apart, never stacked
    assert (findings["x"] > max(bundle.versions["v2"].timepoints_minutes)).all()


def test_lines_break_where_a_series_has_no_value(walked) -> None:
    """v2 death_3mo AI: rule answers at t0 and t2, a clinician answer at t1."""
    bundle, _summaries = walked
    plot = _build(walked, DIVERGENCE, "v2")

    def rule(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[
            (frame["panel"] == BINARY_PANEL)
            & (frame["style"] == f"{Arm.AI.value}:{AnswerSource.RULE.value}")
        ]

    markers = rule(_layer_frame(plot, Layer.MEDIANS))
    assert len(markers) == 2 and markers["series"].nunique() == 2  # two runs
    lines = _layer_frame(plot, Layer.LINES)
    assert rule(lines).empty  # one point runs: markers only, never joined across t1

    # Every drawn series covers consecutive timepoints only.
    minutes = list(bundle.versions["v2"].timepoints_minutes)
    for _key, group in lines.groupby(["version", "panel", "series"], observed=True):
        t = sorted(min(range(len(minutes)), key=lambda i: abs(minutes[i] - x)) for x in group["x"])
        assert t == list(range(t[0], t[0] + len(t)))


def test_arm_and_source_lines_differ_without_colour() -> None:
    from ehr_simulator.analysis.figures import ANSWER_LINETYPES, ARM_DODGE

    assert len(set(ANSWER_LINETYPES.values())) == len(ANSWER_LINETYPES) == 4
    assert ARM_DODGE[Arm.NO_AI.value] - ARM_DODGE[Arm.AI.value] >= 0.08


def test_likert_axis_has_integer_ticks_inside_its_scale(walked) -> None:
    plot = _build(walked, DIVERGENCE, "v2")
    breaks = next(sc for sc in plot.scales if "y" in sc.aesthetics).breaks

    assert breaks((-0.8, 5.2)) == [1.0, 2.0, 3.0, 4.0, 5.0]  # confidence 1-5: no 0 tick
    assert (min(breaks((-0.45, 1.05))), max(breaks((-0.45, 1.05)))) == (0, 1)
    assert max(breaks((-45.0, 105.0))) == 100


def test_intervention_drops_structurally_not_applicable_rows(walked) -> None:
    plot = _build(walked, INTERVENTION, "v1")

    assert _facets(plot) == [
        "AI delivered (AI arm)",
        "AI viewed (AI arm)",
        "intervention failure (AI arm)",
        "intervention failure (No AI arm)",
        "AI leakage (No AI arm)",
        "per protocol (AI arm)",
        "per protocol (No AI arm)",
    ]
    caption = _caption(plot)
    assert "leakage occurred" in caption and "never false" in caption


def test_unknown_delivery_plots_failure_unknown(walked) -> None:
    plot = _build(walked, INTERVENTION, "v2")

    failure = _segments(plot, "intervention failure (AI arm)")
    assert (failure["yes"], failure["no"], failure["unknown"]) == (2, 0, 1)


def _lightness(color: str) -> float:
    """CIE L* of ``#RRGGBB``, its greyscale value (independent of the module)."""
    linear = []
    for i in (1, 3, 5):
        c = int(color[i : i + 2], 16) / 255
        linear.append(c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
    y = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


def _contrast(a: str, b: str) -> float:
    """WCAG contrast ratio, from the relative luminance behind L*."""
    luminance = sorted(((_lightness(c) + 16) / 116) ** 3 for c in (a, b))
    return (luminance[1] + 0.05) / (luminance[0] + 0.05)


@pytest.mark.parametrize("palette", ["OUTCOME_COLORS", "TILE_COLORS"])
def test_category_fills_are_greyscale_distinct(palette: str) -> None:
    from ehr_simulator.analysis import figures

    lightness = sorted(_lightness(c) for c in getattr(figures, palette).values())
    assert min(b - a for a, b in itertools.pairwise(lightness)) >= MIN_LIGHTNESS_GAP


def test_in_bar_codes_read_on_their_fill(walked) -> None:
    from ehr_simulator.analysis.figures import OUTCOME_COLORS, TILE_COLORS

    plot = _build(walked, INTERVENTION, "v2")
    hexes = {"white": WHITE, "black": BLACK}
    layers = [
        layer for layer in plot.layers if layer.mapping.get("label") in ("on_dark", "on_light")
    ]
    assert len(layers) == 2
    for layer in layers:
        column, color = layer.mapping["label"], hexes[layer.geom.aes_params["color"]]
        labelled = plot.data[plot.data[column] != ""]
        for category in labelled["category"].astype(str).unique():
            assert _contrast(OUTCOME_COLORS[category], color) >= MIN_TEXT_CONTRAST, category

    # Figure 3 tiles carry black codes on every fill.
    assert min(_contrast(fill, BLACK) for fill in TILE_COLORS.values()) >= MIN_TEXT_CONTRAST


def test_provenance_is_one_tile_grid_per_version(walked) -> None:
    bundle, summaries = walked
    for version in bundle.versions.values():
        tiles = _build(walked, PROVENANCE, version.version).data
        questions = [str(r) for r in tiles["row"].unique()]
        assert sorted(questions) == sorted(q.question_id for q in version.questions.questions)
        assert tiles["column"].nunique() == len(Arm) * len(version.timepoints_minutes)

    plot = _build(walked, PROVENANCE, "v1")
    tiles = plot.data

    # Codes carry the meaning: every non-zero category of a cell is named.
    r = summaries.response_provenance
    cell = r[
        (r["config_version"] == "v1")
        & (r["question_id"] == "primary_cause")
        & (r["arm"] == Arm.AI)
        & (r["t_index"] == 0)
    ].iloc[0]
    tile = tiles[(tiles["row"] == "primary_cause") & (tiles["column"] == "AI\n0")]
    expected = f"NA{cell['not_applicable']}\nMU{cell['missing_reached_unanswered']}"
    assert tile["label"].item() == expected
    assert "n=3" in set(_labelled(plot, "n=3")["label"])


def test_provenance_caption_limits_hidden_to_reached_timepoints(walked) -> None:
    plot = _build(walked, PROVENANCE, "v1")
    assert "reached timepoints only" in " ".join(_caption(plot).split())


def test_attention_exclusions_of_every_timepoint_share_one_strip(walked) -> None:
    plot = _build(walked, ATTENTION, "v1")

    assert "wall clock elapsed (s) excl: AI t0 1, t60 1" in _facets(plot)
    assert "horizontal ticks: median" in _caption(plot)


# ---------------------------------------------------------------------------
# Print review fixes (greyscale renders of the walked bundle)
# ---------------------------------------------------------------------------

#: Horizontal clearance (pt) between texts sharing a row, e.g. "2" and "1".
MIN_ROW_GAP_POINTS = 4
#: Clearance (pt) of panel texts from the panel border.
MIN_BORDER_POINTS = 1
POINTS_PER_INCH = 72
GLYPHS = ("●", "▲")


@dataclasses.dataclass(frozen=True)
class DrawnText:
    text: str
    #: Extent in points: x0, y0, x1, y1 (y up).
    box: tuple[float, float, float, float]


def _drawn_panels(plot) -> list[tuple[tuple[float, float, float, float], list[DrawnText]]]:
    """Each panel's extent and its in-panel texts as drawn (points)."""
    import matplotlib.pyplot as plt

    figure = plot.draw()
    figure.canvas.draw()
    renderer = figure.canvas.get_renderer()
    scale = POINTS_PER_INCH / figure.dpi

    def points(extent) -> tuple[float, float, float, float]:
        return tuple(v * scale for v in extent.extents)

    panels = [
        (
            points(ax.get_window_extent(renderer)),
            [DrawnText(t.get_text(), points(t.get_window_extent(renderer))) for t in ax.texts],
        )
        for ax in figure.axes
    ]
    plt.close(figure)
    return panels


def _same_row(a: DrawnText, b: DrawnText) -> bool:
    """Vertical overlap over half the shorter text: one text row."""
    overlap = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1])
    return overlap > min(a.box[3] - a.box[1], b.box[3] - b.box[1]) / 2


def _x_gap(a: DrawnText, b: DrawnText) -> float:
    return max(a.box[0], b.box[0]) - min(a.box[2], b.box[2])


@pytest.mark.parametrize("version", VERSIONS)
def test_divergence_texts_never_touch_and_stay_inside_their_panel(walked, version: str) -> None:
    """Count rows: glyph key clear of the counts, counts ≥ 4 pt apart (no
    "n=3 2" read as "n=32"), end labels clear of the t180 count, nothing on
    the panel border."""
    plot = _build(walked, DIVERGENCE, version)

    problems = []
    for panel, texts in _drawn_panels(plot):
        for t in texts:
            inside = (
                t.box[0] >= panel[0] + MIN_BORDER_POINTS
                and t.box[1] >= panel[1] + MIN_BORDER_POINTS
                and t.box[2] <= panel[2] - MIN_BORDER_POINTS
                and t.box[3] <= panel[3] - MIN_BORDER_POINTS
            )
            if not inside:
                problems.append(f"{t.text!r} on the border")
        for a, b in itertools.combinations(texts, 2):
            if _same_row(a, b) and _x_gap(a, b) < MIN_ROW_GAP_POINTS:
                problems.append(f"{a.text!r} {b.text!r} {_x_gap(a, b):.1f} pt apart")
    assert not problems


def test_divergence_count_rows_key_n_once_with_bare_counts(walked) -> None:
    plot = _build(walked, DIVERGENCE, "v1")

    counts = _layer_frame(plot, Layer.COUNTS)
    assert counts["label"].str.fullmatch(r"\d+").all()
    keys = _layer_frame(plot, Layer.KEYS)
    assert set(keys["label"]) == {f"{glyph} n" for glyph in GLYPHS}


def test_multi_option_end_labels_have_leaders_to_their_lines(walked) -> None:
    plot = _build(walked, DIVERGENCE, "v2")

    def mine(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[frame["panel"] == "supporting_findings"]

    ends = mine(_layer_frame(plot, Layer.ENDS))
    leaders = mine(_layer_frame(plot, Layer.LEADERS))
    lines = mine(_layer_frame(plot, Layer.MEDIANS))

    # One leader per option series end, from its last point to its label.
    series_ends = lines.groupby(lines["series"].str.rsplit(":", n=1).str[0])[["x", "y"]].last()
    assert len(leaders) == len(series_ends)
    starts = set(zip(leaders["x"].round(6), leaders["y"].round(6), strict=True))
    assert starts == set(zip(series_ends["x"].round(6), series_ends["y"].round(6), strict=True))
    label_ys = set(ends["y"].round(6))
    assert set(leaders["yend"].round(6)) == label_ys
    assert (leaders["xend"] < ends["x"].min()).all()


def test_attention_exclusions_are_words_outside_the_data(walked) -> None:
    plot = _build(walked, ATTENTION, "v2")

    for text in _plot_text(plot):
        assert not any(glyph in text for glyph in GLYPHS), text
    strips = _facets(plot)
    assert "foreground (s) excl: AI t60 1" in strips
    assert not any("excl" in s for s in strips if s.startswith("wall clock"))


def test_attention_median_ticks_differ_by_arm_without_colour(walked) -> None:
    from ehr_simulator.analysis.figures import MEDIAN_LINETYPES

    plot = _build(walked, ATTENTION, "v1")

    ticks = next(layer for layer in plot.layers if type(layer.geom).__name__ == "geom_segment")
    assert ticks.mapping["linetype"] == "arm"
    assert any("linetype" in sc.aesthetics for sc in plot.scales)
    assert len({str(style) for style in MEDIAN_LINETYPES.values()}) == len(Arm)
    # Each tick spans its arm's dodged position only: the two never meet.
    frame = ticks.geom.data
    for _key, group in frame.groupby(["version", "measure", "x_mid"], observed=True):
        ai, no_ai = (group[group["arm"] == arm] for arm in (Arm.AI.value, Arm.NO_AI.value))
        if len(ai) and len(no_ai):
            assert ai["xend"].max() < no_ai["x"].min()


@pytest.mark.parametrize("version", VERSIONS)
def test_intervention_legend_lists_only_drawn_outcomes(walked, version: str) -> None:
    plot = _build(walked, INTERVENTION, version)

    scale = next(sc for sc in plot.scales if "fill" in sc.aesthetics)
    drawn = set(plot.data.loc[plot.data["count"] > 0, "category"].astype(str))
    assert set(scale.limits) == drawn
    assert "not_applicable" not in scale.limits


@pytest.mark.parametrize("version", VERSIONS)
def test_case_flow_legend_lists_only_drawn_segments(walked, version: str) -> None:
    """v2 has no incomplete or open case: its legend must not list them."""
    plot = _build(walked, "supplementary_02_case_flow", version)

    scale = next(sc for sc in plot.scales if "fill" in sc.aesthetics)
    drawn = set(plot.data.loc[plot.data["count"] > 0, "segment"].astype(str))
    assert set(scale.limits) == drawn


@pytest.mark.parametrize("palette", ["OUTCOME_COLORS", "FLOW_COLORS"])
def test_category_fills_keep_one_dark_fill_apart(palette: str) -> None:
    """Two dark fills read alike in greyscale (yes vs unknown, completed vs
    unknown): every drawn category pair is ≥ 15 L* apart, at most one dark
    fill among outcomes."""
    from ehr_simulator.analysis import figures

    colors = getattr(figures, palette)
    lightness = sorted(_lightness(c) for c in colors.values())
    assert min(b - a for a, b in itertools.pairwise(lightness)) >= MIN_LIGHTNESS_GAP
    if palette == "OUTCOME_COLORS":
        drawn = [c for k, c in colors.items() if k != "not_applicable"]
        assert sum(_lightness(c) < figures.DARK_FILL_LIGHTNESS for c in drawn) <= 1


def test_case_flow_codes_read_on_their_fill(walked) -> None:
    from ehr_simulator.analysis.figures import FLOW_COLORS

    plot = _build(walked, CASE_FLOW, "v1")
    hexes = {"white": WHITE, "black": BLACK}
    layers = [
        layer for layer in plot.layers if layer.mapping.get("label") in ("on_dark", "on_light")
    ]
    assert len(layers) == 2
    for layer in layers:
        column, color = layer.mapping["label"], hexes[layer.geom.aes_params["color"]]
        labelled = plot.data[plot.data[column] != ""]
        for segment in labelled["segment"].astype(str).unique():
            assert _contrast(FLOW_COLORS[segment], color) >= MIN_TEXT_CONTRAST, segment


@pytest.mark.parametrize("version", VERSIONS)
def test_case_flow_replacement_note_clears_the_panel_border(walked, version: str) -> None:
    plot = _build(walked, CASE_FLOW, version)

    for panel, texts in _drawn_panels(plot):
        for t in texts:
            if t.text.startswith("replacements:"):
                assert t.box[0] >= panel[0] + 2 * MIN_BORDER_POINTS, t.text
