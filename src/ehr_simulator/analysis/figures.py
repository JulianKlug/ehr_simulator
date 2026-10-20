"""S12b supplementary descriptive figures over a validated bundle (pure).

Spec: ``specs/session-12-phase25-release.md`` §5. Builders read only the
S12a summaries and the bundle's pinned snapshots; they return an unsaved
``ggplot``. :func:`save_figure` is the only writer::

    build_figure(stem, bundle, summaries, version)
        └─ one version's slice ─► FIGURES[stem].build ─► ggplot ─► save_figure ─► .svg + .pdf

Supplementary (owner decision 2026-10-19): the main-message figures come
from the approved analysis plan. The stems are provisional (not a public
contract). Every figure:

* shows one ``config_version`` only (versions never share a figure) and
  splits by ITT ``arm``;
* states population, denominator and its descriptive nature in the caption;
* encodes the arm by colour **and** shape (greyscale safe: lines also
  differ by type per arm × answer source; rule answers have hollow markers);
* writes only constants, config versions, question ids, option labels and
  counts. Never a clinician / patient id, free text or event payload.

A version without activated cases renders a labelled ``No activated
cases`` placeholder.
"""

from __future__ import annotations

import dataclasses
import math
import textwrap
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
from mizani.breaks import breaks_extended
from plotnine import (
    aes,
    coord_flip,
    element_blank,
    element_text,
    facet_wrap,
    geom_blank,
    geom_col,
    geom_label,
    geom_line,
    geom_point,
    geom_segment,
    geom_text,
    geom_tile,
    ggplot,
    guide_legend,
    labs,
    position_stack,
    scale_color_manual,
    scale_fill_manual,
    scale_linetype_manual,
    scale_shape_manual,
    scale_x_continuous,
    scale_y_continuous,
    theme,
    theme_bw,
    theme_void,
)

from ehr_simulator.analysis.bundle import ConfigVersion, ValidatedStudyBundle
from ehr_simulator.analysis.errors import AnalysisError
from ehr_simulator.analysis.schema import (
    AnswerSource,
    Arm,
    FailureReason,
    MissingReason,
    ResponseType,
    TimepointTelemetry,
)
from ehr_simulator.analysis.summary import (
    ARMS,
    ATTENTION_MEASURES,
    MISSING_COLUMNS,
    NUMERIC_TYPES,
    OPTION_TYPES,
    POPULATION_APPLICABLE,
    POPULATION_CASES,
    POPULATION_REACHED,
    VALID_TELEMETRY,
    AnalysisSummaries,
    Cohort,
    attention_observations,
    cohort_status,
    numeric_observations,
)
from ehr_simulator.answer_codec import PROBABILITY_MAX, PROBABILITY_MIN

__all__ = [
    "FIGURES",
    "FIGURE_FORMATS",
    "FigureSpec",
    "build_figure",
    "case_divergence_figure",
    "save_figure",
]

# ---------------------------------------------------------------------------
# Style (one column, colour-blind safe Okabe-Ito, greyscale distinct shapes)
# ---------------------------------------------------------------------------

ONE_COLUMN_INCHES = 3.5
BASE_FONT_SIZE = 8
#: Smallest text drawn (pt): legible when printed at one column.
TEXT_SIZE = 6
PANEL_INCHES = 1.0
HEADER_INCHES = 1.6
#: A figure fits one page: panels shrink, but never below the minimum.
MAX_FIGURE_INCHES = 9.5
MIN_PANEL_INCHES = 0.75
#: Figure 1 / case figure rows: many short panels, one column.
ANSWER_PANEL_INCHES = 1.0
STRIP_WRAP = 44
FACET_WRAP = 34
NOTE_WRAP = 22
CAPTION_WRAP = 62
#: Caption line spacing: wrapped lines never touch.
CAPTION_LINESPACING = 1.25

ARM_LABELS = {Arm.AI.value: "AI", Arm.NO_AI.value: "No AI"}
ARM_COLORS = {Arm.AI.value: "#0072B2", Arm.NO_AI.value: "#D55E00"}
ARM_SHAPES = {Arm.AI.value: "o", Arm.NO_AI.value: "^"}
#: The arm's marker as text: compact, colour independent count row keys, e.g. "▲ n".
ARM_GLYPHS = {Arm.AI.value: "●", Arm.NO_AI.value: "▲"}
#: Arm offset along x, as a share of the timepoint span: equal values never hide a marker.
ARM_DODGE = {Arm.AI.value: -0.04, Arm.NO_AI.value: 0.04}

SOURCE_LABELS = {AnswerSource.CLINICIAN.value: "clinician", AnswerSource.RULE.value: "rule"}
#: Line type per arm × answer source: all four differ without colour.
ANSWER_LINETYPES = {
    f"{Arm.AI.value}:{AnswerSource.CLINICIAN.value}": "solid",
    f"{Arm.NO_AI.value}:{AnswerSource.CLINICIAN.value}": "dashed",
    f"{Arm.AI.value}:{AnswerSource.RULE.value}": "dotted",
    f"{Arm.NO_AI.value}:{AnswerSource.RULE.value}": "dashdot",
}
ANSWER_LINE_LABELS = {
    key: f"{ARM_LABELS[key.split(':')[0]]} {SOURCE_LABELS[key.split(':')[1]]}"
    for key in ANSWER_LINETYPES
}
#: Point ``fill`` key: the arm colour for clinician answers, white for rule answers.
HOLLOW = "hollow"
FILL_VALUES = {**ARM_COLORS, HOLLOW: "white"}

NOTE_COLOR = "#4d4d4d"
#: Bar segment outline: keeps light fills (e.g. white "not applicable") visible.
OUTLINE_COLOR = "#4d4d4d"
OUTLINE_WIDTH = 0.2
DESCRIPTIVE_NOTE = "Descriptive only; no inferential comparison."
SOURCE_NOTE = (
    "Lines: AI solid, No AI dashed; rule answers (auto_value): AI dotted, No AI"
    " dash-dot, hollow markers."
)
NO_CASES = "No activated cases"
NO_RESPONSES = "no responses"
SINGLE_ARM = "no between arm comparison available"
NO_DURATIONS = "no valid durations"
TIME_LABEL = "Minutes from first contact"
COUNT_LABEL = "Count"

#: Headroom above stacked bars for the ``n=`` / total labels.
COUNT_EXPAND = (0, 0, 0.35, 0)
FLOW_EXPAND = (0, 0, 0.6, 0)
#: Room right of the last timepoint for the ``case k`` labels.
CASE_X_EXPAND = (0.05, 0, 0.3, 0)
#: Room left / right of the first / last timepoint for its count labels.
LABEL_X_EXPAND = (0.18, 0)
#: Figure 1: room left for the count row keys, right for the option end labels.
DIVERGENCE_X_EXPAND = (0.5, 0, 1.3, 0)
CASE_LABEL_SHIFT = 0.18

FIGURE_FORMATS = ("svg", "pdf")
#: Fixed SVG id salt and no creation dates: reruns on one platform are byte stable.
_SVG_RC = {"svg.fonttype": "none", "svg.hashsalt": "ehr-simulator-analysis"}
_METADATA = {"svg": {"Date": None}, "pdf": {"CreationDate": None}}


@dataclass(frozen=True)
class FigureSpec:
    """``build`` takes one config version's slice (see :func:`build_figure`)."""

    stem: str
    title: str
    build: Callable[[ValidatedStudyBundle, AnalysisSummaries], ggplot]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _wrap(text: str, width: int = STRIP_WRAP) -> str:
    return textwrap.fill(text, width=width, break_on_hyphens=False)


def _caption(*sentences: str) -> str:
    return _wrap(" ".join(sentences), CAPTION_WRAP)


def _panel(question_id: str, option: str | None = None) -> str:
    """Facet row of one question (option questions: one row per option)."""
    return question_id if option is None else f"{question_id}: {option}"


def _with_facet(
    frame: pd.DataFrame,
    row: str,
    rows: list[str],
    notes: Mapping[str, str] = MappingProxyType({}),
) -> pd.DataFrame:
    """One strip per ``row`` (e.g. ``confidence``), in *rows* order. A row
    note adds a strip line, e.g. ``excl: AI t60 1``."""
    labels = {r: _wrap(r, FACET_WRAP) for r in rows}
    for key, note in notes.items():
        labels[key] += "\n" + _wrap(note, FACET_WRAP)
    out = frame.copy()
    out["facet"] = pd.Categorical(
        [labels[k] for k in out[row]], categories=list(labels.values()), ordered=True
    )
    return out


#: Subtitle naming the figure's one config version.
VERSION_SUBTITLE = "Config version {version}"


def _labs(title: str, caption: str, version: str, **axes: str) -> labs:
    """Title, caption and the version subtitle: every figure names its version."""
    subtitle = _wrap(VERSION_SUBTITLE.format(version=version), CAPTION_WRAP)
    return labs(title=title, subtitle=subtitle, caption=caption, **axes)


def _only_version(bundle: ValidatedStudyBundle) -> ConfigVersion:
    """The one version of a :func:`build_figure` slice."""
    if len(bundle.versions) != 1:
        raise AnalysisError("a figure shows exactly one config version")
    return next(iter(bundle.versions.values()))


#: Summary tables sliced to the figure's version (all keyed by ``config_version``).
VERSION_TABLES = (
    "case_flow",
    "response_provenance",
    "option_proportions",
    "numeric_responses",
    "attention",
    "intervention",
)


def _version_slice(
    bundle: ValidatedStudyBundle, summaries: AnalysisSummaries, version: str
) -> tuple[ValidatedStudyBundle, AnalysisSummaries]:
    """The bundle and summaries of *version* only; its cohort is its own
    (a version without activated cases renders the placeholder)."""
    if version not in bundle.versions:
        raise AnalysisError(f"config version {version!r} is not in this bundle")

    def mine(frame: pd.DataFrame) -> pd.DataFrame:
        return frame[frame["config_version"] == version]

    view = dataclasses.replace(
        bundle,
        versions=MappingProxyType({version: bundle.versions[version]}),
        cases=mine(bundle.cases),
        timepoints=mine(bundle.timepoints),
        answers=mine(bundle.answers),
    )
    tables = {name: mine(getattr(summaries, name)) for name in VERSION_TABLES}
    return view, dataclasses.replace(summaries, cohort=cohort_status(view), **tables)


def _minutes(version: ConfigVersion, t_index: int) -> float:
    return version.timepoints_minutes[t_index]


def _span(version: ConfigVersion) -> float:
    return max(version.timepoints_minutes) - min(version.timepoints_minutes) or 1.0


def _dodge(version: ConfigVersion) -> dict[str, float]:
    return {arm: share * _span(version) for arm, share in ARM_DODGE.items()}


#: Significant digits of a timepoint label: distinct float minutes never collide.
T_LABEL_DIGITS = 15


def _t_label(minutes: float) -> str:
    """``60.0`` → ``"60"``; ``1440.125`` keeps every digit (``:g`` keeps six)."""
    return f"{minutes:.{T_LABEL_DIGITS}g}"


def _ordered(frame: pd.DataFrame, column: str, order: list[str]) -> pd.DataFrame:
    """Plotnine facets and stacks by category order: keep the snapshot order."""
    out = frame.copy()
    out[column] = pd.Categorical(out[column], categories=order, ordered=True)
    return out


#: Caption anchored at the figure's left edge (not the panels'), so it never
#: clips; the version subtitle sits between title and caption sizes.
_CAPTION_THEME = {
    "plot_caption": element_text(size=TEXT_SIZE, ha="left", linespacing=CAPTION_LINESPACING),
    "plot_caption_position": "plot",
    "plot_subtitle": element_text(size=7),
}


def _theme(
    panels: float,
    panel_inches: float = PANEL_INCHES,
    min_panel_inches: float = MIN_PANEL_INCHES,
) -> theme:
    panels = max(panels, 1)
    fitted = (MAX_FIGURE_INCHES - HEADER_INCHES) / panels
    panel_inches = max(min(panel_inches, fitted), min_panel_inches)
    return theme_bw(base_size=BASE_FONT_SIZE) + theme(
        figure_size=(ONE_COLUMN_INCHES, HEADER_INCHES + panel_inches * panels),
        legend_position="top",
        legend_title=element_text(size=6.5),
        legend_text=element_text(size=6),
        axis_text=element_text(size=6),
        strip_text=element_text(size=6),
        plot_title=element_text(size=9),
        **_CAPTION_THEME,
    )


def _arm_scales() -> list:
    """Arm by colour and shape (one legend); fill = arm or hollow (rule answer)."""
    common = {"labels": ARM_LABELS, "name": "Arm", "limits": list(ARMS)}
    return [
        scale_color_manual(values=ARM_COLORS, **common),
        scale_shape_manual(values=ARM_SHAPES, **common),
        scale_fill_manual(values=FILL_VALUES, guide=None),
    ]


def _placeholder(title: str, caption: str, version: str, text: str = NO_CASES) -> ggplot:
    note = pd.DataFrame({"x": [0.0], "y": [0.0], "label": [text]})
    return (
        ggplot(note, aes("x", "y", label="label"))
        + geom_text(size=BASE_FONT_SIZE, color=NOTE_COLOR)
        + _labs(title, caption, version)
        + theme_void(base_size=BASE_FONT_SIZE)
        + theme(figure_size=(ONE_COLUMN_INCHES, 2.0), **_CAPTION_THEME)
    )


# ---------------------------------------------------------------------------
# Answer panels (figure 1 and the case-level figure)
# ---------------------------------------------------------------------------

#: ``points`` / ``lines``: version, panel, x, y, arm, source, fill, series, style.
#: ``texts``: version, panel, x, y, arm, label. ``notes`` / ``ends``: no arm.
_POINT_COLUMNS = ["version", "panel", "x", "y", "arm", "source", "fill", "series", "style"]
_TEXT_COLUMNS = ["version", "panel", "x", "y", "arm", "label"]
_NOTE_COLUMNS = ["version", "panel", "x", "y", "label"]
_LEADER_COLUMNS = ["version", "panel", "x", "y", "xend", "yend"]
#: A two option question plots only its first option's share, named in the strip.
BINARY_OPTIONS = 2
SHARE_PANEL = "{question}: {option}"
#: Option end labels: wrap width, x offset (share of the timepoint span),
#: height of one line as a share of the value range. The offset clears the
#: last timepoint's count by ≥ 4 pt at one column width.
END_LABEL_WRAP = 13
END_LABEL_SHIFT = 0.25
END_LABEL_LINE = 0.21
#: Leaders run from each line end to just left of its label.
LEADER_END_SHIFT = 0.22
LEADER_WIDTH = 0.3
#: Labels may reach this share of the value range below the count band,
#: never above the top value (the panel border sits close above it).
END_LABEL_MARGIN = 0.05
#: Count rows: one per arm, this share of the value range apart, below it;
#: the ``● n`` / ``▲ n`` key sits this share of the timepoint span left of t0,
#: clear of the t0 count. Half a row more below keeps the last row off the border.
COUNT_ROW_SHARE = 0.2
COUNT_KEY_SHIFT = 0.27
COUNT_KEY = "{glyph} n"
COUNT_FLOOR_ROWS = len(ARMS) + 0.5
#: Arm × source line legend: two rows fit one column width.
LINE_LEGEND_ROWS = 2
#: Multi-line labels inside a panel (option ends, provenance tiles): compact.
TEXT_LINESPACING = 1.0
#: Float tolerance when matching a panel's limits to its value range.
RANGE_EPSILON = 1e-9
#: Likert scales with more points get every k-th integer tick.
MAX_INTEGER_TICKS = 6


@dataclass
class _AnswerLayers:
    """Rows of one config version's panels, one column."""

    version: str
    panels: list[str] = field(default_factory=list)
    y_range: dict[str, tuple[float, float]] = field(default_factory=dict)
    points: list[dict] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    #: Option labels at the right end of their lines (multi option panels).
    ends: list[dict] = field(default_factory=list)
    #: ``● n`` / ``▲ n`` count row keys, right aligned left of the first timepoint.
    keys: list[dict] = field(default_factory=list)
    #: Segments from each option series end to its end label.
    leaders: list[dict] = field(default_factory=list)
    #: Extra blank rows: the panel extends below its count rows.
    floors: list[dict] = field(default_factory=list)
    #: Likert value ranges: integer ticks only.
    integer_ranges: set[tuple[float, float]] = field(default_factory=set)

    def add_panel(self, panel: str, question) -> None:
        if panel not in self.panels:
            self.panels.append(panel)
        self.y_range.setdefault(panel, _y_range(question))
        if question.response_type == ResponseType.LIKERT:
            self.integer_ranges.add(_y_range(question))

    def breaks(self, limits: tuple[float, float]) -> list[float]:
        """Ticks of the panel's value range only (the band below holds ``n=``).

        ``free_y`` hands each panel only its limits: the value range is the
        widest-reaching one inside them, e.g. limits (-1.8, 5.2) → likert (1, 5).
        """
        fitting = [r for r in set(self.y_range.values()) if r[1] <= limits[1] + RANGE_EPSILON]
        if not fitting:
            return []
        low, high = max(fitting, key=lambda r: (r[1], r[0]))
        if (low, high) in self.integer_ranges:
            step = max(1, math.ceil((high - low) / MAX_INTEGER_TICKS))
            return [float(v) for v in range(int(low), int(high) + 1, step)]
        return [b for b in breaks_extended(n=4)((low, high)) if low <= b <= high]

    def y_mid(self, panel: str) -> float:
        low, high = self.y_range[panel]
        return (low + high) / 2

    def frames(self, x_mid: float) -> dict[str, pd.DataFrame]:
        # Every panel spans its question's full scale, answered or not.
        blank = [
            {"version": self.version, "panel": p, "x": x_mid, "y": y}
            for p in self.panels
            for y in self.y_range[p]
        ]
        tables = {
            "leaders": (self.leaders, _LEADER_COLUMNS),
            "points": (self.points, _POINT_COLUMNS),
            "lines": (self.lines, _POINT_COLUMNS),
            "texts": (self.texts, _TEXT_COLUMNS),
            "keys": (self.keys, _TEXT_COLUMNS),
            "notes": (self.notes, _NOTE_COLUMNS),
            "ends": (self.ends, _NOTE_COLUMNS),
            "blank": (blank + self.floors, ["version", "panel", "x", "y"]),
        }
        return {
            name: _with_facet(pd.DataFrame(rows, columns=columns), "panel", self.panels)
            for name, (rows, columns) in tables.items()
        }


def _y_range(question) -> tuple[float, float]:
    if question.response_type == ResponseType.LIKERT:
        return float(question.scale_min), float(question.scale_max)
    if question.response_type == ResponseType.PROBABILITY:
        return float(PROBABILITY_MIN), float(PROBABILITY_MAX)
    return 0.0, 1.0  # proportion / selection indicator


def _x_mid(version: ConfigVersion) -> float:
    minutes = version.timepoints_minutes
    return (min(minutes) + max(minutes)) / 2


def _point(version: ConfigVersion, panel, x, y, arm, source, series) -> dict:
    fill = arm if source == AnswerSource.CLINICIAN else HOLLOW
    return {
        "version": version.version,
        "panel": panel,
        "x": x,
        "y": y,
        "arm": arm,
        "source": source,
        "fill": fill,
        "series": series,
        "style": f"{arm}:{source}",
    }


def _runs(t_indices) -> list[int]:
    """Run id per timepoint: a series line never crosses a timepoint without
    a value, e.g. t 0, 1, 3 → runs 0, 0, 1."""
    runs, run, previous = [], 0, None
    for t in t_indices:
        if previous is not None and t != previous + 1:
            run += 1
        runs.append(run)
        previous = t
    return runs


def _arm_note(layers: _AnswerLayers, panel: str, version: ConfigVersion, arms: set) -> None:
    """Empty panel → ``no responses``; one arm → ``no between arm comparison``."""
    if len(arms) == len(ARMS):
        return
    layers.notes.append(
        {
            "version": version.version,
            "panel": panel,
            "x": _x_mid(version),
            "y": layers.y_mid(panel),
            "label": _wrap(NO_RESPONSES if not arms else SINGLE_ARM, NOTE_WRAP),
        }
    )


def _count_texts(layers, panel, version, question, counts: Mapping[tuple[int, str], int]):
    """One count row per arm below the value range: the ``● n`` key left of
    the first timepoint, then the bare count under each timepoint, e.g.
    ``● n   2   1   1``. Bare numbers keep counts of close timepoints apart
    (``n=3 2`` read as ``n=32``)."""
    low, high = _y_range(question)
    first = min(version.timepoints_minutes)
    for row, arm in enumerate(ARMS, start=1):
        base = {"version": version.version, "panel": panel, "arm": arm}
        y = low - COUNT_ROW_SHARE * row * (high - low)
        key_x = first - COUNT_KEY_SHIFT * _span(version)
        key = COUNT_KEY.format(glyph=ARM_GLYPHS[arm])
        layers.keys.append({**base, "x": key_x, "y": y, "label": key})
        for t_index in range(len(version.timepoints_minutes)):
            n = counts.get((t_index, arm), 0)
            layers.texts.append({**base, "x": _minutes(version, t_index), "y": y, "label": str(n)})
    floor = low - COUNT_ROW_SHARE * COUNT_FLOOR_ROWS * (high - low)
    layers.floors.append({"version": version.version, "panel": panel, "x": first, "y": floor})


def _divergence_layers(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> _AnswerLayers:
    version = _only_version(bundle)
    layers = _AnswerLayers(version=version.version)
    observations = numeric_observations(bundle)
    numeric, options = s.numeric_responses, s.option_proportions

    for question in version.questions.questions:
        if question.response_type in NUMERIC_TYPES:
            _numeric_divergence(layers, version, question, numeric, observations)
        elif question.response_type in OPTION_TYPES:
            _option_divergence(layers, version, question, options)
    return layers


def _rows(frame: pd.DataFrame, version: ConfigVersion, question) -> pd.DataFrame:
    return frame[
        (frame["config_version"] == version.version)
        & (frame["question_id"] == question.question_id)
    ]


def _numeric_divergence(layers, version, question, numeric, observations) -> None:
    """Individual values (small) and the median per arm × source (joined)."""
    panel = _panel(question.question_id)
    layers.add_panel(panel, question)
    dodge = _dodge(version)

    for r in _rows(observations, version, question).itertuples(index=False):
        x = _minutes(version, r.t_index) + dodge[r.arm]
        series = f"{r.arm}:{r.answer_source}"
        value = float(r.value_number)
        layers.points.append(_point(version, panel, x, value, r.arm, r.answer_source, series))

    summary = _rows(numeric, version, question)
    valued = summary[summary["n"] > 0].sort_values("t_index")
    for (arm, source), g in valued.groupby(["arm", "answer_source"], sort=False):
        for r, run in zip(g.itertuples(index=False), _runs(g["t_index"]), strict=True):
            x = _minutes(version, r.t_index) + dodge[arm]
            point = _point(version, panel, x, float(r.median), arm, source, f"{arm}:{source}:{run}")
            layers.lines.append(point)

    counts = summary.groupby(["t_index", "arm"])["n"].sum()
    _count_texts(layers, panel, version, question, {k: int(v) for k, v in counts.items()})
    _arm_note(layers, panel, version, set(summary.loc[summary["n"] > 0, "arm"]))


def _option_divergence(layers, version, question, options) -> None:
    """One panel per question: share of answers naming each option.

    Two options: only the first option's share (the second is its
    complement). More: one line per option, labelled right of the last
    timepoint near the mean of its series' last values, with a leader from
    each series end to the label.
    """
    rows = _rows(options, version, question)
    answers = rows[rows["option_order"] == 0].groupby(["t_index", "arm"])["answers"].sum()
    counts = {k: int(v) for k, v in answers.items()}
    arms = {arm for (_t, arm), n in counts.items() if n}
    dodge = _dodge(version)

    binary = len(question.options) == BINARY_OPTIONS
    shown = question.options[:1] if binary else question.options
    panel = _panel(question.question_id)
    if binary:
        panel = SHARE_PANEL.format(question=question.question_id, option=shown[0])
    layers.add_panel(panel, question)

    ends = []
    for order, option in enumerate(shown):
        shares = rows[(rows["option_order"] == order) & rows["proportion"].notna()]
        last_points = []
        for (arm, source), g in shares.sort_values("t_index").groupby(
            ["arm", "answer_source"], sort=False
        ):
            for r, run in zip(g.itertuples(index=False), _runs(g["t_index"]), strict=True):
                x = _minutes(version, r.t_index) + dodge[arm]
                series = f"{arm}:{source}:{order}:{run}"
                layers.lines.append(
                    _point(version, panel, x, float(r.proportion), arm, source, series)
                )
            last_points.append(layers.lines[-1])
        if not binary and last_points:
            mean = sum(point["y"] for point in last_points) / len(last_points)
            ends.append({"label": _wrap(option, END_LABEL_WRAP), "y": mean, "from": last_points})
    _end_labels(layers, panel, version, question, ends)
    _count_texts(layers, panel, version, question, counts)
    _arm_note(layers, panel, version, arms)


def _end_labels(layers, panel, version, question, ends: list[dict]) -> None:
    """Option labels right of the last timepoint, spread so none overlap,
    each tied to its series ends by thin leaders (a spread label may sit
    away from its line's end value).

    Two passes over the labels sorted by value: push up off the one below,
    then push down off the one above (the panel top), so labels stay inside
    the panel, the count band included (labels start clear of the last
    timepoint's count).
    """
    low, high = _y_range(question)
    scale = high - low
    x = max(version.timepoints_minutes) + END_LABEL_SHIFT * _span(version)
    placed = sorted(ends, key=lambda e: e["y"])
    halves = [END_LABEL_LINE * scale * (e["label"].count("\n") + 1) / 2 for e in placed]
    ys = [e["y"] for e in placed]

    floor = low - (len(ARMS) * COUNT_ROW_SHARE + END_LABEL_MARGIN) * scale
    for i, half in enumerate(halves):
        ys[i] = max(ys[i], floor + half)
        floor = ys[i] + half
    ceiling = high
    for i in reversed(range(len(halves))):
        ys[i] = min(ys[i], ceiling - halves[i])
        ceiling = ys[i] - halves[i]

    leader_x = max(version.timepoints_minutes) + LEADER_END_SHIFT * _span(version)
    for end, y in zip(placed, ys, strict=True):
        base = {"version": version.version, "panel": panel}
        layers.ends.append({**base, "x": x, "y": y, "label": end["label"]})
        for point in end["from"]:
            leader = {"x": point["x"], "y": point["y"], "xend": leader_x, "yend": y}
            layers.leaders.append({**base, **leader})


def _answer_plot(
    layers: _AnswerLayers, bundle: ValidatedStudyBundle, title: str, caption: str
) -> ggplot:
    f = layers.frames(_x_mid(bundle.versions[layers.version]))
    p = (
        ggplot()
        + facet_wrap("facet", ncol=1, scales="free_y", drop=False)
        + geom_blank(data=f["blank"], mapping=aes("x", "y"))
        + _arm_scales()
        + scale_linetype_manual(
            values=ANSWER_LINETYPES,
            labels=ANSWER_LINE_LABELS,
            name="Line",
            limits=list(ANSWER_LINETYPES),
            guide=guide_legend(nrow=LINE_LEGEND_ROWS),
        )
        + _labs(title, caption, layers.version, x=TIME_LABEL, y=ANSWER_LABEL)
        + scale_y_continuous(breaks=layers.breaks)
        # Answer panels carry two count rows: they never shrink (may exceed a page).
        + _theme(len(layers.panels), ANSWER_PANEL_INCHES, ANSWER_PANEL_INCHES)
    )
    if not f["leaders"].empty:
        p += geom_segment(
            data=f["leaders"],
            mapping=aes("x", "y", xend="xend", yend="yend"),
            color=NOTE_COLOR,
            size=LEADER_WIDTH,
        )
    if not f["points"].empty:
        p += geom_point(
            data=f["points"],
            mapping=aes("x", "y", color="arm", shape="arm", fill="fill"),
            size=1.2,
            alpha=0.6,
            stroke=0.5,
        )
    if not f["lines"].empty:
        # A one point run (a series value between two gaps) is a marker, no line.
        lines = f["lines"]
        run_size = lines.groupby(["version", "panel", "series"], observed=True)["x"]
        p += geom_line(
            data=lines[run_size.transform("size") > 1],
            mapping=aes("x", "y", color="arm", linetype="style", group="series"),
            size=0.6,
        )
        p += geom_point(
            data=f["lines"],
            mapping=aes("x", "y", color="arm", shape="arm", fill="fill"),
            size=2.0,
            stroke=0.6,
        )
    if not f["texts"].empty:
        p += geom_text(
            data=f["texts"],
            mapping=aes("x", "y", label="label"),
            size=TEXT_SIZE,
            color=NOTE_COLOR,
        )
    if not f["keys"].empty:
        p += geom_text(
            data=f["keys"],
            mapping=aes("x", "y", label="label"),
            ha="right",
            size=TEXT_SIZE,
            color=NOTE_COLOR,
        )
    if not f["ends"].empty:
        p += geom_text(
            data=f["ends"],
            mapping=aes("x", "y", label="label"),
            ha="left",
            size=TEXT_SIZE,
            color=NOTE_COLOR,
            lineheight=TEXT_LINESPACING,
        )
    if not f["notes"].empty:
        # Translucent white box: readable over the answers, which stay visible.
        p += geom_label(
            data=f["notes"],
            mapping=aes("x", "y", label="label"),
            size=TEXT_SIZE,
            color=NOTE_COLOR,
            fill="white",
            alpha=0.75,
            label_size=0,
        )
    return p


# ---------------------------------------------------------------------------
# Figure 1: divergence
# ---------------------------------------------------------------------------

DIVERGENCE_TITLE = "Answers by arm and timepoint"
ANSWER_LABEL = "Answer (scale value or share of answers)"


def divergence_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    version = _only_version(bundle).version
    caption = _caption(
        f"Population: {POPULATION_APPLICABLE}; colour / shape: ITT arm.",
        "One panel per question of this config version. Likert / probability:",
        "answers (small), median (large).",
        "Options: share of answers naming the option after the colon in the strip",
        "(two option questions) or at the line's right end (independent for",
        "multi-select), joined to it by thin grey leaders. Lines break where a series",
        "has no value. ● n / ▲ n rows = AI / No AI answers per timepoint. No free text.",
        SOURCE_NOTE,
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(DIVERGENCE_TITLE, caption, version)
    plot = _answer_plot(_divergence_layers(bundle, s), bundle, DIVERGENCE_TITLE, caption)
    # Ticks at the timepoints only: the right margin holds labels, not time.
    return plot + scale_x_continuous(breaks=_all_minutes(bundle), expand=DIVERGENCE_X_EXPAND)


# ---------------------------------------------------------------------------
# Supplementary: one selected patient, one line per case
# ---------------------------------------------------------------------------

CASE_DIVERGENCE_TITLE = "Case-level answers"


def case_divergence_figure(bundle: ValidatedStudyBundle, patient_id: str, version: str) -> ggplot:
    """Every activated case of *patient_id* pinned to *version*, one line per
    case (``case 1…``). Versions never share a figure: a patient seen under
    two versions gets one figure per version.

    Research inspection only: never batch generated, never named after the
    patient or a clinician. Option questions plot a 0 / 1 selection
    indicator per option (no invented ordinal scale).

    Raises:
        AnalysisError: the patient has no activated case of *version*.
    """
    cases = bundle.cases[
        (bundle.cases["patient_id"] == patient_id) & (bundle.cases["config_version"] == version)
    ]
    if cases.empty:
        raise AnalysisError("the selected patient has no activated case in this config version")

    cases = cases.sort_values(["arm", "clinician_id"])
    ordinal = {cid: f"case {i}" for i, cid in enumerate(cases["clinician_id"], start=1)}
    arm_of = dict(zip(cases["clinician_id"], cases["arm"], strict=True))
    answers = bundle.answers[
        (bundle.answers["patient_id"] == patient_id)
        & (bundle.answers["config_version"] == version)
        & bundle.answers["value_exported"]
    ]

    pinned = bundle.versions[version]
    layers = _AnswerLayers(version=version)
    for question in pinned.questions.questions:
        if question.response_type in NUMERIC_TYPES:
            _case_panel(layers, pinned, question, None, answers, ordinal, arm_of)
        elif question.response_type in OPTION_TYPES:
            for option in question.options:
                _case_panel(layers, pinned, question, option, answers, ordinal, arm_of)

    caption = _caption(
        f"{len(cases)} {POPULATION_CASES} of one selected patient; individual answers,",
        "not aggregated. Option rows: 1 = option selected, 0 = not selected. Lines join",
        "one case's answers (AI solid, No AI dashed); hollow markers: rule answers",
        "(auto_value).",
        DESCRIPTIVE_NOTE,
    )
    plot = _answer_plot(layers, bundle, CASE_DIVERGENCE_TITLE, caption)
    return plot + scale_x_continuous(expand=CASE_X_EXPAND)


def _case_panel(layers, version, question, option, answers, ordinal, arm_of) -> None:
    panel = _panel(question.question_id, option)
    layers.add_panel(panel, question)
    rows = answers[answers["question_id"] == question.question_id].sort_values("t_index")

    answered_arms: set[str] = set()
    for clinician_id, case_rows in rows.groupby("clinician_id", sort=False):
        label, arm = ordinal[clinician_id], arm_of[clinician_id]
        last: dict = {}
        for r in case_rows.itertuples(index=False):
            y = float(r.value_number) if option is None else float(option in r.value_options)
            x = _minutes(version, r.t_index)
            last = _point(version, panel, x, y, arm, r.answer_source, label)
            layers.points.append(last)
            layers.lines.append({**last, "source": AnswerSource.CLINICIAN.value})
            answered_arms.add(arm)
        # Label right of the case's last answer (ha is centred, so shift by a step).
        shifted = {**last, "x": last["x"] + CASE_LABEL_SHIFT * _span(version)}
        layers.texts.append({**{k: shifted[k] for k in _TEXT_COLUMNS[:-1]}, "label": label})

    _arm_note(layers, panel, version, answered_arms)


# ---------------------------------------------------------------------------
# Figure 2: case flow
# ---------------------------------------------------------------------------

CASE_FLOW_TITLE = "Case flow"
#: segment → (label code, legend label). Codes keep segments colour independent.
FLOW_SEGMENTS = {
    "planned_items": ("P", "P planned schedule items"),
    "completed": ("C", "C completed"),
    "incomplete": ("I", "I incomplete"),
    "open": ("O", "O open (active / paused)"),
    "state_unknown": ("?", "? state unknown"),
}
#: Bars per arm: planned schedule items, then activated cases by lifecycle state.
FLOW_BARS = {
    "planned": ["planned_items"],
    "activated": ["completed", "incomplete", "open", "state_unknown"],
}
#: Six bars in the one panel: taller than the default panel.
FLOW_PANEL_SCALE = 1.4
#: L* 88 / 30 / 71 / 46 / 9: every pair ≥ 15 apart, distinct in greyscale.
FLOW_COLORS = {
    "planned_items": "#DDDDDD",
    "completed": "#00503A",
    "incomplete": "#E69F00",
    "open": "#0072B2",
    "state_unknown": "#1A1A1A",
}
#: Replacement notes start this share of the longest bar right of the panel edge.
FLOW_NOTE_OFFSET = 0.03


def case_flow_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    version = _only_version(bundle).version
    caption = _caption(
        f"Population: {POPULATION_CASES} (realised ITT arm, activation version) beside",
        "planned schedule items (planned arm, generation version). Planned items are",
        "never counted as cases. Replacements are counted on the incomplete original;",
        "an activated replacement is also an activated case of its own.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(CASE_FLOW_TITLE, caption, version)

    bars: list[str] = []
    segments, totals, notes = [], [], []
    for r in s.case_flow.itertuples(index=False):
        arm_label = ARM_LABELS[r.arm]
        for bar, names in FLOW_BARS.items():
            bar_segments, total = _flow_bar(r, f"{arm_label} · {bar}", names)
            segments += bar_segments
            totals.append(total)
        notes.append(_replacement_note(r, f"{arm_label} · replacements"))
    for arm in ARMS:
        bars += [f"{ARM_LABELS[arm]} · {bar}" for bar in (*FLOW_BARS, "replacements")]

    order = list(reversed(bars))  # coord_flip draws the first category lowest
    seg = _ordered(_ordered(pd.DataFrame(segments), "bar", order), "segment", list(FLOW_SEGMENTS))
    tot = _ordered(pd.DataFrame(totals), "bar", order)
    note = _ordered(pd.DataFrame(notes), "bar", order)
    note = note.assign(count=FLOW_NOTE_OFFSET * max(tot["count"].max(), 1))
    # Code text by fill lightness: white on dark, black on light (no colour-mapped text).
    dark = seg["segment"].astype(str).map(lambda name: _is_dark(FLOW_COLORS[name]))
    seg = seg.assign(on_dark=seg["label"].where(dark, ""), on_light=seg["label"].where(~dark, ""))

    p = ggplot(seg, aes("bar", "count", fill="segment")) + geom_col(
        position=position_stack(reverse=True),
        width=0.7,
        color=OUTLINE_COLOR,
        size=OUTLINE_WIDTH,
    )
    for column, color in (("on_dark", "white"), ("on_light", "black")):
        p += geom_text(
            aes(label=column, group="segment"),
            position=position_stack(vjust=0.5, reverse=True),
            size=TEXT_SIZE,
            color=color,
        )
    return (
        p
        + geom_text(
            data=tot,
            mapping=aes("bar", "count", label="label"),
            ha="left",
            size=TEXT_SIZE,
            inherit_aes=False,
        )
        + geom_text(
            data=note,
            mapping=aes("bar", "count", label="label"),
            ha="left",
            size=TEXT_SIZE,
            color=NOTE_COLOR,
            inherit_aes=False,
        )
        + coord_flip()
        + scale_fill_manual(
            values=FLOW_COLORS,
            labels={k: v[1] for k, v in FLOW_SEGMENTS.items()},
            # The legend lists only the segments this version draws.
            limits=[k for k in FLOW_SEGMENTS if (seg.loc[seg["segment"] == k, "count"] > 0).any()],
            name="Cases",
        )
        + scale_y_continuous(expand=FLOW_EXPAND)
        + _labs(CASE_FLOW_TITLE, caption, version, x="", y=COUNT_LABEL)
        + _theme(FLOW_PANEL_SCALE)
        + theme(legend_position="bottom", legend_direction="vertical")
    )


def _flow_bar(r, bar: str, names: list[str]) -> tuple[list[dict], dict]:
    """Segments of one stacked bar (``"C 2"`` labels) and its ``n=`` total."""
    counts = {name: int(getattr(r, name)) for name in names}
    segments = [
        {
            "version": r.config_version,
            "bar": bar,
            "segment": name,
            "count": count,
            "label": f"{FLOW_SEGMENTS[name][0]} {count}" if count else "",
        }
        for name, count in counts.items()
    ]
    total = sum(counts.values())
    return segments, {
        "version": r.config_version,
        "bar": bar,
        "count": total,
        "label": f" n={total}",
    }


def _replacement_note(r, bar: str) -> dict:
    """Replacements as text beside the bars (plotnine has no flow diagram)."""
    return {
        "version": r.config_version,
        "bar": bar,
        "count": 0,
        "label": (
            f"replacements: planned {r.replacement_planned},"
            f" activated {r.replacement_activated},"
            f" pending {r.replacement_pending}"
        ),
    }


# ---------------------------------------------------------------------------
# Stacked count panels (figures 3 and 5)
# ---------------------------------------------------------------------------


def _stacked_plot(
    data: pd.DataFrame,
    totals: pd.DataFrame,
    *,
    categories: Mapping[str, tuple[str, str]],
    colors: Mapping[str, str],
    rows: list[str],
    t_labels: list[str],
    legend: str,
    title: str,
    caption: str,
    version: str,
) -> ggplot:
    """Bars per timepoint, stacked by category, one strip per ``row``."""
    # Only drawn categories: the legend never lists one that is absent.
    present = [c for c in categories if (data.loc[data["category"] == c, "count"] > 0).any()]
    data = data[data["category"].isin(present)]
    data = _ordered(_with_facet(data, "row", rows), "t", t_labels)
    data = _ordered(data, "category", present)
    totals = _ordered(_with_facet(totals, "row", rows), "t", t_labels)
    # Two text layers over every segment (stacking stays aligned), each
    # labelling only the segments its colour reads on.
    dark = data["category"].astype(str).map(lambda c: _is_dark(colors[c]))
    data = data.assign(
        on_dark=data["label"].where(dark, ""), on_light=data["label"].where(~dark, "")
    )
    p = ggplot(data, aes("t", "count", fill="category")) + geom_col(
        position=position_stack(reverse=True),
        width=0.7,
        color=OUTLINE_COLOR,
        size=OUTLINE_WIDTH,
    )
    for column, color in (("on_dark", "white"), ("on_light", "black")):
        p += geom_text(
            aes(label=column, group="category"),
            position=position_stack(vjust=0.5, reverse=True),
            size=TEXT_SIZE,
            color=color,
        )
    return (
        p
        + geom_text(
            data=totals,
            mapping=aes("t", "count", label="label"),
            va="bottom",
            size=TEXT_SIZE,
            color=NOTE_COLOR,
            inherit_aes=False,
        )
        + facet_wrap("facet", ncol=1, drop=False)
        + scale_y_continuous(expand=COUNT_EXPAND)
        + scale_fill_manual(
            values=dict(colors),
            labels={k: v[1] for k, v in categories.items()},
            limits=present,
            name=legend,
        )
        + _labs(title, caption, version, x=TIME_LABEL, y=COUNT_LABEL)
        + _theme(len(rows))
        + theme(
            legend_position="bottom",
            legend_direction="vertical",
            panel_grid_major_x=element_blank(),
        )
    )


#: Fills darker than this CIE L* carry white text, lighter ones black.
DARK_FILL_LIGHTNESS = 55.0


def _lightness(color: str) -> float:
    """CIE L* (0 black … 100 white) of a ``#RRGGBB`` colour: its greyscale value."""
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return 116 * y ** (1 / 3) - 16 if y > (6 / 29) ** 3 else y * (29 / 3) ** 3


def _is_dark(color: str) -> bool:
    return _lightness(color) < DARK_FILL_LIGHTNESS


def _segment(row: str, t: str, category: str, count: int, code: str) -> dict:
    return {
        "row": row,
        "t": t,
        "category": category,
        "count": count,
        "label": f"{code}{count}" if count else "",
    }


def _all_minutes(bundle: ValidatedStudyBundle) -> list[float]:
    return sorted({m for v in bundle.versions.values() for m in v.timepoints_minutes})


def _all_t_labels(bundle: ValidatedStudyBundle) -> list[str]:
    return [_t_label(m) for m in _all_minutes(bundle)]


# ---------------------------------------------------------------------------
# Figure 3: response provenance
# ---------------------------------------------------------------------------

PROVENANCE_TITLE = "Response provenance"
MISSING_CODES = {
    MissingReason.TECHNICAL_FAILURE: "MT",
    MissingReason.CASE_ABANDONED: "MA",
    MissingReason.REACHED_UNANSWERED: "MU",
    MissingReason.PENDING: "MP",
    MissingReason.TIMEPOINT_NEVER_REACHED: "MN",
    MissingReason.UNKNOWN: "M?",
}
PROVENANCE_CATEGORIES = {
    "answered_clinician": ("AC", "AC answered (clinician)"),
    "answered_rule": ("AR", "AR answered (rule)"),
    "not_applicable": ("NA", "NA not applicable (hidden branch)"),
    **{
        f"missing_{reason.value}": (code, f"{code} missing: {reason.value.replace('_', ' ')}")
        for reason, code in MISSING_CODES.items()
    },
}


class TileState(StrEnum):
    """Missing share of a provenance tile: its fill, readable without colour."""

    NO_MISSING = "no_missing"
    SOME_MISSING = "some_missing"
    ALL_MISSING = "all_missing"


TILE_STATES = {
    TileState.NO_MISSING.value: "no missing",
    TileState.SOME_MISSING.value: "some missing",
    TileState.ALL_MISSING.value: "all missing",
}
#: Greys ≥ 15 L* apart (100, 82, 63): distinct in greyscale, black text readable.
TILE_COLORS = {
    TileState.NO_MISSING.value: "#FFFFFF",
    TileState.SOME_MISSING.value: "#CCCCCC",
    TileState.ALL_MISSING.value: "#999999",
}
OBSERVATIONS_ROW = "n (case timepoints)"
#: Tile row height: one 6 pt line per category code plus padding.
TILE_LINE_INCHES = 0.085
TILE_PAD_INCHES = 0.03
TILE_HEADER_INCHES = 1.8


def response_provenance_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    version = _only_version(bundle)
    data = s.response_provenance
    present = [category for category in PROVENANCE_CATEGORIES if int(data[category].sum())]
    key = "; ".join(PROVENANCE_CATEGORIES[c][1] for c in present)
    caption = _caption(
        "Population: every configured timepoint of the activated measured cases;",
        "n = case timepoints per column (ITT arm × minutes from first contact).",
        f"Tiles list non-zero categories as code + count{': ' + key if key else ''}.",
        "NA (hidden) applies to reached timepoints only; unreached ones are",
        "missing (pending, abandoned, never reached). Fill: share missing.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(PROVENANCE_TITLE, caption, version.version)

    columns = [f"{ARM_LABELS[arm]}\n{t}" for arm in ARMS for t in _all_t_labels(bundle)]
    tiles, counts, rows = [], [], [OBSERVATIONS_ROW]
    for r in data.to_dict("records"):
        t = _t_label(_minutes(version, r["t_index"]))
        column = f"{ARM_LABELS[r['arm']]}\n{t}"
        row = r["question_id"]
        if row not in rows:
            rows.append(row)

        codes = [
            f"{code}{int(r[category])}"
            for category, (code, _label) in PROVENANCE_CATEGORIES.items()
            if int(r[category])
        ]
        tiles.append(
            {
                "row": row,
                "column": column,
                "state": _tile_state(sum(int(r[c]) for c in MISSING_COLUMNS), r["observations"]),
                "label": "\n".join(codes),
            }
        )
        counts.append(
            {"row": OBSERVATIONS_ROW, "column": column, "label": f"n={r['observations']}"}
        )

    order = list(reversed(rows))  # discrete y draws the first category lowest
    tile_frame = _ordered(_ordered(pd.DataFrame(tiles), "row", order), "column", columns)
    count_frame = _ordered(pd.DataFrame(counts), "row", order)
    count_frame = _ordered(count_frame.drop_duplicates(), "column", columns)

    lines = max(1, int(tile_frame["label"].str.count("\n").max()) + 1)
    row_inches = TILE_LINE_INCHES * lines + TILE_PAD_INCHES
    height = TILE_HEADER_INCHES + row_inches * len(rows)

    return (
        ggplot(tile_frame, aes("column", "row"))
        + geom_tile(aes(fill="state"), color=OUTLINE_COLOR, size=OUTLINE_WIDTH)
        + geom_text(aes(label="label"), size=TEXT_SIZE, lineheight=TEXT_LINESPACING)
        + geom_text(
            data=count_frame,
            mapping=aes("column", "row", label="label"),
            size=TEXT_SIZE,
            color=NOTE_COLOR,
        )
        + scale_fill_manual(
            values=TILE_COLORS, labels=TILE_STATES, limits=list(TILE_STATES), name="Tile"
        )
        + _labs(
            PROVENANCE_TITLE,
            caption,
            version.version,
            x="ITT arm · minutes from first contact",
            y="",
        )
        + _theme(1)
        + theme(figure_size=(ONE_COLUMN_INCHES, height), panel_grid=element_blank())
    )


def _tile_state(missing: int, observations: int) -> str:
    if not missing:
        return TileState.NO_MISSING.value
    if missing < observations:
        return TileState.SOME_MISSING.value
    return TileState.ALL_MISSING.value


# ---------------------------------------------------------------------------
# Figure 4: attention
# ---------------------------------------------------------------------------

ATTENTION_TITLE = "Time per timepoint"
#: Reached observations without a valid duration, as a strip line in words
#: (never a marker glyph read as data), e.g. ``excl: AI t0 1, t60 1; No AI t0 1``.
EXCLUDED_NOTE = "excl: {arms}"
#: Median tick per arm on its own side of the timepoint, from this inner to
#: outer share of the timepoint span: it reaches past its arm's dodged points
#: on both sides and never meets the other arm's tick. Line type: AI solid,
#: No AI dashed (as in figure 1).
MEDIAN_INNER = 0.005
MEDIAN_OUTER = 0.15
MEDIAN_WIDTH = 0.8
MEDIAN_LINETYPES = {Arm.AI.value: "solid", Arm.NO_AI.value: (0, (2, 1.2))}
#: Room above / below the extreme durations: a marker never sits on the border.
ATTENTION_Y_EXPAND = (0.12, 0)
MEASURE_LABELS = {
    "elapsed": "wall clock elapsed (s)",
    "foreground": "foreground (s)",
    "active": "active (s)",
}


def attention_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    version = _only_version(bundle)
    excluded = _excluded_by_status(s.attention)
    caption = _caption(
        f"Population: {POPULATION_REACHED}. Points: individual durations; horizontal",
        "ticks: median (AI solid, No AI dashed).",
        "Elapsed = S10 wall clock; foreground / active = S11j browser telemetry, only",
        f"where telemetry_status is {VALID_TELEMETRY.value}. An absent duration is never 0:",
        "strip 'excl: AI t60 1' = one AI reached observation at 60 min without a valid",
        "duration.",
        f"Telemetry exclusions: {excluded}." if excluded else "No telemetry exclusions.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(ATTENTION_TITLE, caption, version.version)

    obs = attention_observations(bundle)
    measures = [MEASURE_LABELS[m] for m in ATTENTION_MEASURES]
    top = {m: _top_seconds(obs[obs["measure"] == m]["seconds"]) for m in ATTENTION_MEASURES}

    points, medians, notes = [], [], []
    excluded_by_panel: dict[str, dict[str, list[str]]] = {}
    for r in obs.itertuples(index=False):
        x = _minutes(version, r.t_index) + _dodge(version)[r.arm]
        points.append(
            {
                "version": r.config_version,
                "measure": MEASURE_LABELS[r.measure],
                "x": x,
                "y": float(r.seconds),
                "arm": r.arm,
            }
        )
    for r in s.attention.to_dict("records"):
        t = _minutes(version, r["t_index"])
        side = math.copysign(_span(version), ARM_DODGE[r["arm"]])
        tick = sorted((t + MEDIAN_INNER * side, t + MEDIAN_OUTER * side))
        for measure in ATTENTION_MEASURES:
            base = {
                "version": r["config_version"],
                "measure": MEASURE_LABELS[measure],
                "arm": r["arm"],
            }
            if r[f"{measure}_n"]:
                y = float(r[f"{measure}_median_seconds"])
                medians.append({**base, "x": tick[0], "xend": tick[1], "y": y, "x_mid": t})
            missing = r["reached"] - r[f"{measure}_n"]
            if missing:
                panel = excluded_by_panel.setdefault(MEASURE_LABELS[measure], {})
                panel.setdefault(r["arm"], []).append(f"t{_t_label(t)} {missing}")

    for measure in ATTENTION_MEASURES:
        if (obs["measure"] == measure).any():
            continue
        notes.append(
            {
                "version": version.version,
                "measure": MEASURE_LABELS[measure],
                "x": _x_mid(version),
                "y": top[measure] / 2,
                "label": NO_DURATIONS,
            }
        )

    # Arms in a fixed order: "excl: AI t0 1, t60 1; No AI t0 1".
    strip_notes = {
        key: EXCLUDED_NOTE.format(
            arms="; ".join(
                f"{ARM_LABELS[arm]} {', '.join(by_arm[arm])}" for arm in ARMS if arm in by_arm
            )
        )
        for key, by_arm in excluded_by_panel.items()
    }

    def frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
        return _with_facet(pd.DataFrame(rows, columns=columns), "measure", measures, strip_notes)

    base_cols = ["version", "measure", "x", "y", "arm"]
    p = (
        ggplot()
        + facet_wrap("facet", ncol=1, scales="free_y", drop=False)
        + _arm_scales()
        + _labs(ATTENTION_TITLE, caption, version.version, x=TIME_LABEL, y="Seconds")
        + scale_x_continuous(breaks=_all_minutes(bundle), expand=LABEL_X_EXPAND)
        + scale_y_continuous(expand=ATTENTION_Y_EXPAND)
        + _theme(len(measures))
    )
    if points:
        p += geom_point(
            data=frame(points, base_cols),
            mapping=aes("x", "y", color="arm", shape="arm"),
            size=1.2,
            alpha=0.7,
        )
    if medians:
        p += geom_segment(
            data=frame(medians, [*base_cols, "xend", "x_mid"]),
            mapping=aes("x", "y", xend="xend", yend="y", color="arm", linetype="arm"),
            size=MEDIAN_WIDTH,
            show_legend=False,
        )
        p += scale_linetype_manual(values=MEDIAN_LINETYPES, guide=None)
    if notes:
        p += geom_text(
            data=frame(notes, ["version", "measure", "x", "y", "label"]),
            mapping=aes("x", "y", label="label"),
            size=TEXT_SIZE,
            color=NOTE_COLOR,
        )
    return p


def _top_seconds(seconds: pd.Series) -> float:
    """Panel height reference; 1 s when a measure has no valid duration."""
    return float(seconds.max()) if len(seconds) else 1.0


def _excluded_by_status(attention: pd.DataFrame) -> str:
    """``"missing 1, gapped 2"``: reached observations by non-valid telemetry status."""
    parts = []
    for status in TimepointTelemetry:
        if status == VALID_TELEMETRY:
            continue
        count = int(attention[f"telemetry_{status.value}"].sum())
        if count:
            parts.append(f"{status.value.replace('_', ' ')} {count}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Figure 5: intervention
# ---------------------------------------------------------------------------

INTERVENTION_TITLE = "AI intervention integrity"


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class InterventionMeasure(StrEnum):
    DELIVERED = "delivered"
    VIEWED = "viewed"
    FAILURE = "failure"
    LEAKAGE = "leakage"
    PER_PROTOCOL = "pp"


OUTCOMES = {
    Outcome.YES.value: ("Y", "Y yes / compliant"),
    Outcome.NO.value: ("N", "N no / noncompliant"),
    Outcome.UNKNOWN.value: ("?", "? unknown / indeterminate"),
    Outcome.NOT_APPLICABLE.value: ("NA", "NA not applicable to this arm"),
}
#: L* 46 / 71 / 100 / 21: every pair ≥ 25 apart, distinct in greyscale; one
#: dark fill only among drawn outcomes (yes), so unknown never reads as yes.
#: Not applicable rows are dropped (MEASURE_ARMS): its dark fill is never drawn.
OUTCOME_COLORS = {
    Outcome.YES.value: "#0072B2",
    Outcome.NO.value: "#E69F00",
    Outcome.UNKNOWN.value: "#FFFFFF",  # outlined
    Outcome.NOT_APPLICABLE.value: "#333333",
}
INTERVENTION_MEASURES = {
    InterventionMeasure.DELIVERED: "AI delivered",
    InterventionMeasure.VIEWED: "AI viewed",
    InterventionMeasure.FAILURE: "intervention failure",
    InterventionMeasure.LEAKAGE: "AI leakage",
    InterventionMeasure.PER_PROTOCOL: "per protocol",
}
#: The arms a measure is plotted for: rows that would be all ``not applicable``
#: (delivered / viewed on the no AI arm, leakage on the AI arm) are dropped.
MEASURE_ARMS = {
    InterventionMeasure.DELIVERED: (Arm.AI.value,),
    InterventionMeasure.VIEWED: (Arm.AI.value,),
    InterventionMeasure.FAILURE: ARMS,
    InterventionMeasure.LEAKAGE: (Arm.NO_AI.value,),
    InterventionMeasure.PER_PROTOCOL: ARMS,
}


def _outcomes(measure: InterventionMeasure, r: Mapping) -> dict[Outcome, int]:
    """Counts per outcome of one S12a ``intervention`` row.

    Delivered / viewed exist on the AI arm only, leakage on the no AI arm
    only; the other arm is ``not_applicable``, never false.
    """
    reached = int(r["reached"])
    is_ai = r["arm"] == Arm.AI
    if measure in (InterventionMeasure.DELIVERED, InterventionMeasure.VIEWED):
        if not is_ai:
            return {Outcome.NOT_APPLICABLE: reached}
        return {
            Outcome.YES: int(r[f"{measure}_true"]),
            Outcome.NO: int(r[f"{measure}_false"]),
            Outcome.UNKNOWN: int(r[f"{measure}_unknown"]),
        }
    if measure == InterventionMeasure.FAILURE:
        failed, unknown = int(r["intervention_failure"]), int(r["intervention_failure_unknown"])
        return {
            Outcome.YES: failed,
            Outcome.NO: reached - failed - unknown,
            Outcome.UNKNOWN: unknown,
        }
    if measure == InterventionMeasure.LEAKAGE:
        if is_ai:
            return {Outcome.NOT_APPLICABLE: reached}
        return {Outcome.YES: int(r["leakage"]), Outcome.NO: reached - int(r["leakage"])}
    return {
        Outcome.YES: int(r["pp_compliant"]),
        Outcome.NO: int(r["pp_noncompliant"]),
        Outcome.UNKNOWN: int(r["pp_indeterminate"]),
    }


def intervention_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    version = _only_version(bundle)
    by_reason = {r: int(s.intervention[f"failure_{r.value}"].sum()) for r in FailureReason}
    failures = ", ".join(
        f"{reason.value.replace('_', ' ')} {count}" for reason, count in by_reason.items() if count
    )
    caption = _caption(
        f"Population: {POPULATION_REACHED}, grouped by ITT arm (never by exposure).",
        "Delivered / viewed apply to the AI arm, leakage to the no AI arm (yes =",
        "leakage occurred); not applicable is never false. Unknown delivery makes failure",
        "unknown. Unknown and indeterminate are counted, never read as no. n above",
        "each bar = reached observations.",
        f"Failure reasons: {failures}." if failures else "No intervention failure.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(INTERVENTION_TITLE, caption, version.version)

    rows = [
        f"{label} ({ARM_LABELS[arm]} arm)"
        for measure, label in INTERVENTION_MEASURES.items()
        for arm in MEASURE_ARMS[measure]
    ]
    data, totals = [], []
    for r in s.intervention.to_dict("records"):
        t = _t_label(_minutes(version, r["t_index"]))
        for measure, label in INTERVENTION_MEASURES.items():
            if r["arm"] not in MEASURE_ARMS[measure]:
                continue
            row = f"{label} ({ARM_LABELS[r['arm']]} arm)"
            counts = _outcomes(measure, r)
            for outcome, (code, _legend) in OUTCOMES.items():
                data.append(_segment(row, t, outcome, counts.get(outcome, 0), code))
            totals.append(
                {
                    "row": row,
                    "t": t,
                    "count": int(r["reached"]),
                    "label": f"n={r['reached']}",
                }
            )

    return _stacked_plot(
        pd.DataFrame(data),
        pd.DataFrame(totals),
        categories=OUTCOMES,
        colors=OUTCOME_COLORS,
        rows=rows,
        t_labels=_all_t_labels(bundle),
        legend="Outcome",
        title=INTERVENTION_TITLE,
        caption=caption,
        version=version.version,
    )


# ---------------------------------------------------------------------------
# Registry and output
# ---------------------------------------------------------------------------

FIGURES: Mapping[str, FigureSpec] = MappingProxyType(
    {
        spec.stem: spec
        for spec in (
            FigureSpec("supplementary_01_divergence", DIVERGENCE_TITLE, divergence_figure),
            FigureSpec("supplementary_02_case_flow", CASE_FLOW_TITLE, case_flow_figure),
            FigureSpec(
                "supplementary_03_response_provenance",
                PROVENANCE_TITLE,
                response_provenance_figure,
            ),
            FigureSpec("supplementary_04_attention", ATTENTION_TITLE, attention_figure),
            FigureSpec("supplementary_05_intervention", INTERVENTION_TITLE, intervention_figure),
        )
    }
)


def build_figure(
    stem: str, bundle: ValidatedStudyBundle, summaries: AnalysisSummaries, version: str
) -> ggplot:
    """Figure *stem* of config *version* alone: versions never share a figure.

    Raises:
        AnalysisError: *version* is not in the bundle.
    """
    return FIGURES[stem].build(*_version_slice(bundle, summaries, version))


def save_figure(plot: ggplot, stem_path: Path) -> tuple[Path, ...]:
    """Write ``<stem_path>.svg`` and ``.pdf``; SVG text stays text (searchable).

    Drawn once, saved in every format (drawing dominates the cost).
    """
    written = []
    with matplotlib.rc_context(_SVG_RC):
        figure = deepcopy(plot).draw()  # draw() mutates the plot; keep the caller's
        try:
            for fmt in FIGURE_FORMATS:
                # Appended, not with_suffix: a dotted version ("_v1.2") is no suffix.
                path = stem_path.with_name(f"{stem_path.name}.{fmt}")
                figure.savefig(path, format=fmt, metadata=_METADATA[fmt])
                written.append(path)
        finally:
            plt.close(figure)
    return tuple(written)
