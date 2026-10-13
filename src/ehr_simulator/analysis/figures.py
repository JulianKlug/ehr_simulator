"""S12b provisional descriptive figures over a validated bundle (pure).

Spec: ``specs/session-12-phase25-release.md`` §5. Builders read only the
S12a summaries and the bundle's pinned snapshots; they return an unsaved
``ggplot``. :func:`save_figure` is the only writer::

    FIGURES[stem].build(bundle, summaries) ─► ggplot ─► save_figure ─► .svg + .pdf

The stems are provisional (not a public contract). Every figure:

* splits by ``config_version`` (never pooled) and ITT ``arm``;
* states population, denominator and its descriptive nature in the caption;
* encodes the arm by colour **and** shape, the answer source by line type
  and marker fill (rule answers: dotted line, hollow marker);
* writes only constants, config versions, question ids, option labels and
  counts. Never a clinician / patient id, free text or event payload.

An empty cohort renders a labelled ``No activated cases`` placeholder.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import product
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
    geom_text,
    ggplot,
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
    NUMERIC_TYPES,
    OPTION_TYPES,
    POPULATION_APPLICABLE,
    POPULATION_CASES,
    POPULATION_REACHED,
    SOURCES,
    VALID_TELEMETRY,
    AnalysisSummaries,
    Cohort,
    attention_observations,
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
#: Figure 1 / case figure rows: many short panels, versions side by side.
ANSWER_PANEL_INCHES = 1.0
STRIP_WRAP = 44
FACET_WRAP = 34
NOTE_WRAP = 22
CAPTION_WRAP = 62

ARM_LABELS = {Arm.AI.value: "AI", Arm.NO_AI.value: "No AI"}
ARM_COLORS = {Arm.AI.value: "#0072B2", Arm.NO_AI.value: "#D55E00"}
ARM_SHAPES = {Arm.AI.value: "o", Arm.NO_AI.value: "^"}
#: The arm's marker as text: compact, colour independent count labels, e.g. "▲ n=2".
ARM_GLYPHS = {Arm.AI.value: "●", Arm.NO_AI.value: "▲"}
#: Arm offset along x, as a share of the timepoint span, so arms never overlap.
ARM_DODGE = {Arm.AI.value: -0.02, Arm.NO_AI.value: 0.02}

SOURCE_LABELS = {AnswerSource.CLINICIAN.value: "clinician", AnswerSource.RULE.value: "rule"}
SOURCE_LINETYPES = {AnswerSource.CLINICIAN.value: "solid", AnswerSource.RULE.value: "dotted"}
#: Point ``fill`` key: the arm colour for clinician answers, white for rule answers.
HOLLOW = "hollow"
FILL_VALUES = {**ARM_COLORS, HOLLOW: "white"}

NOTE_COLOR = "#4d4d4d"
#: Bar segment outline: keeps light fills (e.g. white "not applicable") visible.
OUTLINE_COLOR = "#4d4d4d"
OUTLINE_WIDTH = 0.2
DESCRIPTIVE_NOTE = "Descriptive only; no inferential comparison."
SOURCE_NOTE = "Rule answers (auto_value): dotted lines, hollow markers."
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
CASE_LABEL_SHIFT = 0.18

FIGURE_FORMATS = ("svg", "pdf")
#: Fixed SVG id salt and no creation dates: reruns on one platform are byte stable.
_SVG_RC = {"svg.fonttype": "none", "svg.hashsalt": "ehr-simulator-analysis"}
_METADATA = {"svg": {"Date": None}, "pdf": {"CreationDate": None}}


@dataclass(frozen=True)
class FigureSpec:
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
    frame: pd.DataFrame, row: str, rows: list[str], col: str, cols: list[str]
) -> pd.DataFrame:
    """One single strip per ``col · row`` (e.g. ``v1 · confidence``), row-major
    so each column holds one ``col`` value (a config version or arm)."""
    labels = {(r, c): _wrap(f"{c} · {r}", FACET_WRAP) for r in rows for c in cols}
    out = frame.copy()
    keys = zip(out[row], out[col], strict=True)
    out["facet"] = pd.Categorical(
        [labels[k] for k in keys], categories=list(labels.values()), ordered=True
    )
    return out


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
        plot_caption=element_text(size=TEXT_SIZE, ha="left"),
    )


def _arm_scales() -> list:
    """Arm by colour and shape (one legend); fill = arm or hollow (rule answer)."""
    common = {"labels": ARM_LABELS, "name": "Arm", "limits": list(ARMS)}
    return [
        scale_color_manual(values=ARM_COLORS, **common),
        scale_shape_manual(values=ARM_SHAPES, **common),
        scale_fill_manual(values=FILL_VALUES, guide=None),
    ]


def _value_breaks(limits: tuple[float, float]) -> list[float]:
    """Answer axis ticks; the band below 0 holds the ``n=`` labels, not values."""
    return [b for b in breaks_extended(n=4)(limits) if b >= 0]


def _placeholder(title: str, caption: str, text: str = NO_CASES) -> ggplot:
    note = pd.DataFrame({"x": [0.0], "y": [0.0], "label": [text]})
    return (
        ggplot(note, aes("x", "y", label="label"))
        + geom_text(size=BASE_FONT_SIZE, color=NOTE_COLOR)
        + labs(title=title, caption=caption)
        + theme_void(base_size=BASE_FONT_SIZE)
        + theme(figure_size=(ONE_COLUMN_INCHES, 2.0), plot_caption=element_text(size=TEXT_SIZE))
    )


# ---------------------------------------------------------------------------
# Answer panels (figure 1 and the case-level figure)
# ---------------------------------------------------------------------------

#: ``points`` / ``lines``: version, panel, x, y, arm, source, fill, series.
#: ``texts``: version, panel, x, y, arm, label (arm coloured). ``notes``: no arm.
_POINT_COLUMNS = ["version", "panel", "x", "y", "arm", "source", "fill", "series"]
_TEXT_COLUMNS = ["version", "panel", "x", "y", "arm", "label"]
_NOTE_COLUMNS = ["version", "panel", "x", "y", "label"]
NOT_IN_VERSION = "not in this version"


@dataclass
class _AnswerLayers:
    """Rows of a ``panel ~ version`` grid; a question is never pooled across
    versions, it only shares a facet row with the same question id."""

    versions: list[str]
    panels: list[str] = field(default_factory=list)
    present: set[tuple[str, str]] = field(default_factory=set)
    y_range: dict[str, tuple[float, float]] = field(default_factory=dict)
    points: list[dict] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)
    texts: list[dict] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)

    def add_panel(self, version: ConfigVersion, panel: str, question) -> None:
        if panel not in self.panels:
            self.panels.append(panel)
        self.y_range.setdefault(panel, _y_range(question))
        self.present.add((version.version, panel))

    def y_mid(self, panel: str) -> float:
        low, high = self.y_range[panel]
        return (low + high) / 2

    def frames(self, x_mid: Mapping[str, float]) -> dict[str, pd.DataFrame]:
        cells = [(v, p) for p in self.panels for v in self.versions]
        absent = [
            {"version": v, "panel": p, "x": x_mid[v], "y": self.y_mid(p), "label": NOT_IN_VERSION}
            for v, p in cells
            if (v, p) not in self.present
        ]
        # Every cell spans its question's full scale, answered or not.
        blank = [
            {"version": v, "panel": p, "x": x_mid[v], "y": y}
            for v, p in cells
            for y in self.y_range[p]
        ]
        tables = {
            "points": (self.points, _POINT_COLUMNS),
            "lines": (self.lines, _POINT_COLUMNS),
            "texts": (self.texts, _TEXT_COLUMNS),
            "notes": (self.notes + absent, _NOTE_COLUMNS),
            "blank": (blank, ["version", "panel", "x", "y"]),
        }
        return {
            name: _with_facet(
                pd.DataFrame(rows, columns=columns), "panel", self.panels, "version", self.versions
            )
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
    }


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
    """``n=`` per timepoint and arm, in two rows below the value range."""
    low, high = _y_range(question)
    dodge = _dodge(version)
    for row, arm in enumerate(ARMS, start=1):
        for t_index in range(len(version.timepoints_minutes)):
            layers.texts.append(
                {
                    "version": version.version,
                    "panel": panel,
                    "x": _minutes(version, t_index) + dodge[arm],
                    "y": low - 0.2 * row * (high - low),
                    "arm": arm,
                    "label": f"{ARM_GLYPHS[arm]} n={counts.get((t_index, arm), 0)}",
                }
            )


def _divergence_layers(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> _AnswerLayers:
    layers = _AnswerLayers(versions=list(bundle.versions))
    observations = numeric_observations(bundle)
    numeric, options = s.numeric_responses, s.option_proportions

    for version in bundle.versions.values():
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
    layers.add_panel(version, panel, question)
    dodge = _dodge(version)

    for r in _rows(observations, version, question).itertuples(index=False):
        x = _minutes(version, r.t_index) + dodge[r.arm]
        series = f"{r.arm}:{r.answer_source}"
        value = float(r.value_number)
        layers.points.append(_point(version, panel, x, value, r.arm, r.answer_source, series))

    summary = _rows(numeric, version, question)
    for r in summary[summary["n"] > 0].itertuples(index=False):
        x = _minutes(version, r.t_index) + dodge[r.arm]
        series = f"{r.arm}:{r.answer_source}"
        median = float(r.median)
        layers.lines.append(_point(version, panel, x, median, r.arm, r.answer_source, series))

    counts = summary.groupby(["t_index", "arm"])["n"].sum()
    _count_texts(layers, panel, version, question, {k: int(v) for k, v in counts.items()})
    _arm_note(layers, panel, version, set(summary.loc[summary["n"] > 0, "arm"]))


def _option_divergence(layers, version, question, options) -> None:
    """One row per option, historical order: share of answers naming it."""
    rows = _rows(options, version, question)
    answers = rows[rows["option_order"] == 0].groupby(["t_index", "arm"])["answers"].sum()
    counts = {k: int(v) for k, v in answers.items()}
    arms = {arm for (_t, arm), n in counts.items() if n}
    dodge = _dodge(version)

    for order, option in enumerate(question.options):
        panel = _panel(question.question_id, option)
        layers.add_panel(version, panel, question)
        shares = rows[(rows["option_order"] == order) & rows["proportion"].notna()]
        for r in shares.itertuples(index=False):
            x = _minutes(version, r.t_index) + dodge[r.arm]
            series = f"{r.arm}:{r.answer_source}"
            share = float(r.proportion)
            layers.lines.append(_point(version, panel, x, share, r.arm, r.answer_source, series))
        _count_texts(layers, panel, version, question, counts)
        _arm_note(layers, panel, version, arms)


def _answer_plot(
    layers: _AnswerLayers, bundle: ValidatedStudyBundle, title: str, caption: str
) -> ggplot:
    x_mid = {v: _x_mid(bundle.versions[v]) for v in layers.versions}
    f = layers.frames(x_mid)
    p = (
        ggplot()
        + facet_wrap("facet", ncol=len(layers.versions), scales="free_y", drop=False)
        + geom_blank(data=f["blank"], mapping=aes("x", "y"))
        + _arm_scales()
        + scale_linetype_manual(
            values=SOURCE_LINETYPES, labels=SOURCE_LABELS, name="Answer", limits=list(SOURCES)
        )
        + labs(title=title, caption=caption, x=TIME_LABEL, y=ANSWER_LABEL)
        + scale_y_continuous(breaks=_value_breaks)
        # Answer panels carry two count rows: they never shrink (may exceed a page).
        + _theme(len(layers.panels), ANSWER_PANEL_INCHES, ANSWER_PANEL_INCHES)
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
        p += geom_line(
            data=f["lines"],
            mapping=aes("x", "y", color="arm", linetype="source", group="series"),
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
    caption = _caption(
        f"Population: {POPULATION_APPLICABLE}; columns: config version, colour /",
        "shape: ITT arm. Likert / probability: individual answers (small) and median",
        "(large, joined). Categorical / multi-select: share of answers naming each",
        "option; multi-select shares are independent. ● / ▲ n = AI / No AI answers",
        "with an exported value. Free text is not shown.",
        SOURCE_NOTE,
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(DIVERGENCE_TITLE, caption)
    plot = _answer_plot(_divergence_layers(bundle, s), bundle, DIVERGENCE_TITLE, caption)
    return plot + scale_x_continuous(expand=LABEL_X_EXPAND)


# ---------------------------------------------------------------------------
# Supplementary: one selected patient, one line per case
# ---------------------------------------------------------------------------

CASE_DIVERGENCE_TITLE = "Case-level answers"


def case_divergence_figure(bundle: ValidatedStudyBundle, patient_id: str) -> ggplot:
    """Every activated case of *patient_id*, one line per case (``case 1…``).

    Research inspection only: never batch generated, never named after the
    patient or a clinician. Option questions plot a 0 / 1 selection
    indicator per option (no invented ordinal scale).

    Raises:
        AnalysisError: the patient has no activated case in the bundle.
    """
    cases = bundle.cases[bundle.cases["patient_id"] == patient_id]
    if cases.empty:
        raise AnalysisError("the selected patient has no activated case in this bundle")

    version_order = list(bundle.versions)
    cases = cases.assign(_v=cases["config_version"].map(version_order.index)).sort_values(
        ["_v", "arm", "clinician_id"]
    )
    ordinal = {cid: f"case {i}" for i, cid in enumerate(cases["clinician_id"], start=1)}
    arm_of = dict(zip(cases["clinician_id"], cases["arm"], strict=True))
    answers = bundle.answers[
        (bundle.answers["patient_id"] == patient_id) & bundle.answers["value_exported"]
    ]

    case_versions = list(dict.fromkeys(cases["config_version"]))
    layers = _AnswerLayers(versions=case_versions)
    for version_id in case_versions:
        version = bundle.versions[version_id]
        mine = answers[answers["config_version"] == version_id]
        for question in version.questions.questions:
            if question.response_type in NUMERIC_TYPES:
                _case_panel(layers, version, question, None, mine, ordinal, arm_of)
            elif question.response_type in OPTION_TYPES:
                for option in question.options:
                    _case_panel(layers, version, question, option, mine, ordinal, arm_of)

    caption = _caption(
        f"{len(cases)} {POPULATION_CASES} of one selected patient; individual answers,",
        "not aggregated. Option rows: 1 = option selected, 0 = not selected. Lines join",
        "one case's answers; marker fill shows each answer's source.",
        SOURCE_NOTE,
        DESCRIPTIVE_NOTE,
    )
    plot = _answer_plot(layers, bundle, CASE_DIVERGENCE_TITLE, caption)
    return plot + scale_x_continuous(expand=CASE_X_EXPAND)


def _case_panel(layers, version, question, option, answers, ordinal, arm_of) -> None:
    panel = _panel(question.question_id, option)
    layers.add_panel(version, panel, question)
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
#: Six bars per version panel: taller than the default panel.
FLOW_PANEL_SCALE = 1.4
FLOW_COLORS = {
    "planned_items": "#BBBBBB",
    "completed": "#009E73",
    "incomplete": "#E69F00",
    "open": "#56B4E9",
    "state_unknown": "#999999",
}


def case_flow_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    caption = _caption(
        f"Population: {POPULATION_CASES} (realised ITT arm, activation version) beside",
        "planned schedule items (planned arm, generation version). Planned items are",
        "never counted as cases. Replacements are counted on the incomplete original;",
        "an activated replacement is also an activated case of its own.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(CASE_FLOW_TITLE, caption)

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
    versions = list(bundle.versions)
    seg = _ordered(_ordered(pd.DataFrame(segments), "bar", order), "version", versions)
    seg = _ordered(seg, "segment", list(FLOW_SEGMENTS))
    tot = _ordered(_ordered(pd.DataFrame(totals), "bar", order), "version", versions)
    note = _ordered(_ordered(pd.DataFrame(notes), "bar", order), "version", versions)

    return (
        ggplot(seg, aes("bar", "count", fill="segment"))
        + geom_col(
            position=position_stack(reverse=True),
            width=0.7,
            color=OUTLINE_COLOR,
            size=OUTLINE_WIDTH,
        )
        + geom_text(
            aes(label="label", group="segment"),
            position=position_stack(vjust=0.5, reverse=True),
            size=TEXT_SIZE,
        )
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
        + facet_wrap("version", ncol=1, drop=False)
        + scale_fill_manual(
            values=FLOW_COLORS,
            labels={k: v[1] for k, v in FLOW_SEGMENTS.items()},
            limits=list(FLOW_SEGMENTS),
            name="Cases",
        )
        + scale_y_continuous(expand=FLOW_EXPAND)
        + labs(title=CASE_FLOW_TITLE, caption=caption, x="", y=COUNT_LABEL)
        + _theme(len(versions) * FLOW_PANEL_SCALE)
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
    cols: list[str],
    t_labels: list[str],
    legend: str,
    title: str,
    caption: str,
) -> ggplot:
    """Bars per timepoint, stacked by category, one strip per ``row · col``."""
    data = _ordered(_with_facet(data, "row", rows, "col", cols), "t", t_labels)
    data = _ordered(data, "category", list(categories))
    totals = _ordered(_with_facet(totals, "row", rows, "col", cols), "t", t_labels)
    return (
        ggplot(data, aes("t", "count", fill="category"))
        + geom_col(
            position=position_stack(reverse=True),
            width=0.7,
            color=OUTLINE_COLOR,
            size=OUTLINE_WIDTH,
        )
        + geom_text(
            aes(label="label", group="category"),
            position=position_stack(vjust=0.5, reverse=True),
            size=TEXT_SIZE,
        )
        + geom_text(
            data=totals,
            mapping=aes("t", "count", label="label"),
            va="bottom",
            size=TEXT_SIZE,
            color=NOTE_COLOR,
            inherit_aes=False,
        )
        + facet_wrap("facet", ncol=len(cols), drop=False)
        + scale_y_continuous(expand=COUNT_EXPAND)
        + scale_fill_manual(
            values=dict(colors),
            labels={k: v[1] for k, v in categories.items()},
            limits=list(categories),
            name=legend,
        )
        + labs(title=title, caption=caption, x=TIME_LABEL, y=COUNT_LABEL)
        + _theme(len(rows))
        + theme(
            legend_position="bottom",
            legend_direction="vertical",
            panel_grid_major_x=element_blank(),
        )
    )


def _segment(row: str, col: str, t: str, category: str, count: int, code: str) -> dict:
    return {
        "row": row,
        "col": col,
        "t": t,
        "category": category,
        "count": count,
        "label": f"{code}{count}" if count else "",
    }


def _all_t_labels(bundle: ValidatedStudyBundle) -> list[str]:
    minutes = sorted({m for v in bundle.versions.values() for m in v.timepoints_minutes})
    return [_t_label(m) for m in minutes]


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
MISSING_COLORS = {
    MissingReason.TECHNICAL_FAILURE: "#CC79A7",
    MissingReason.CASE_ABANDONED: "#D55E00",
    MissingReason.REACHED_UNANSWERED: "#E69F00",
    MissingReason.PENDING: "#F0E442",
    MissingReason.TIMEPOINT_NEVER_REACHED: "#999999",
    MissingReason.UNKNOWN: "#009E73",  # light enough for the black "M?" label
}
PROVENANCE_COLORS = {
    "answered_clinician": "#0072B2",
    "answered_rule": "#56B4E9",
    "not_applicable": "#FFFFFF",  # outlined
    **{f"missing_{reason.value}": MISSING_COLORS[reason] for reason in MissingReason},
}


def response_provenance_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    caption = _caption(
        "Population: every configured timepoint of the activated measured cases.",
        "Bars: answered (clinician / rule), not applicable (hidden branch; never missing)",
        "and missing by reason; unreached timepoints are missing as pending, abandoned",
        "or never reached. n above each bar = case timepoints.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(PROVENANCE_TITLE, caption)

    data, totals, rows = [], [], []
    for r in s.response_provenance.to_dict("records"):
        version = bundle.versions[r["config_version"]]
        row = f"{r['config_version']} · {r['question_id']}"
        if row not in rows:
            rows.append(row)
        col, t = ARM_LABELS[r["arm"]], _t_label(_minutes(version, r["t_index"]))
        for category, (code, _label) in PROVENANCE_CATEGORIES.items():
            data.append(_segment(row, col, t, category, int(r[category]), code))
        totals.append(
            {
                "row": row,
                "col": col,
                "t": t,
                "count": int(r["observations"]),
                "label": f"n={r['observations']}",
            }
        )

    return _stacked_plot(
        pd.DataFrame(data),
        pd.DataFrame(totals),
        categories=PROVENANCE_CATEGORIES,
        colors=PROVENANCE_COLORS,
        rows=rows,
        cols=[ARM_LABELS[a] for a in ARMS],
        t_labels=_all_t_labels(bundle),
        legend="Response",
        title=PROVENANCE_TITLE,
        caption=caption,
    )


# ---------------------------------------------------------------------------
# Figure 4: attention
# ---------------------------------------------------------------------------

ATTENTION_TITLE = "Time per timepoint"
#: ``excl k`` labels above the points, one row per arm (share of the tallest point).
EXCLUDED_LABEL_HEIGHT = {Arm.AI.value: 1.15, Arm.NO_AI.value: 1.3}
MEASURE_LABELS = {
    "elapsed": "wall clock elapsed (s)",
    "foreground": "foreground (s)",
    "active": "active (s)",
}


def attention_figure(bundle: ValidatedStudyBundle, s: AnalysisSummaries) -> ggplot:
    excluded = _excluded_by_status(s.attention)
    caption = _caption(
        f"Population: {POPULATION_REACHED}. Points: individual durations; bars: median.",
        "Elapsed = S10 wall clock; foreground / active = S11j browser telemetry, only",
        f"where telemetry_status is {VALID_TELEMETRY.value}. An absent duration is never 0:",
        "'● / ▲ excl k' = AI / No AI reached observations without a valid duration.",
        f"Telemetry exclusions: {excluded}." if excluded else "No telemetry exclusions.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(ATTENTION_TITLE, caption)

    obs = attention_observations(bundle)
    measures = [MEASURE_LABELS[m] for m in ATTENTION_MEASURES]
    versions = list(bundle.versions)
    top = {m: _top_seconds(obs[obs["measure"] == m]["seconds"]) for m in ATTENTION_MEASURES}

    points, medians, texts, notes = [], [], [], []
    for r in obs.itertuples(index=False):
        version = bundle.versions[r.config_version]
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
        version = bundle.versions[r["config_version"]]
        x = _minutes(version, r["t_index"]) + _dodge(version)[r["arm"]]
        for measure in ATTENTION_MEASURES:
            base = {
                "version": r["config_version"],
                "measure": MEASURE_LABELS[measure],
                "x": x,
                "arm": r["arm"],
            }
            if r[f"{measure}_n"]:
                medians.append({**base, "y": float(r[f"{measure}_median_seconds"])})
            missing = r["reached"] - r[f"{measure}_n"]
            if missing:
                y = top[measure] * EXCLUDED_LABEL_HEIGHT[r["arm"]]
                label = f"{ARM_GLYPHS[r['arm']]} excl {missing}"
                texts.append({**base, "y": y, "label": label})

    for version, measure in product(versions, ATTENTION_MEASURES):
        if ((obs["config_version"] == version) & (obs["measure"] == measure)).any():
            continue
        notes.append(
            {
                "version": version,
                "measure": MEASURE_LABELS[measure],
                "x": _x_mid(bundle.versions[version]),
                "y": top[measure] / 2,
                "label": NO_DURATIONS,
            }
        )

    def frame(rows: list[dict], columns: list[str]) -> pd.DataFrame:
        return _with_facet(
            pd.DataFrame(rows, columns=columns), "measure", measures, "version", versions
        )

    base_cols = ["version", "measure", "x", "y", "arm"]
    p = (
        ggplot()
        + facet_wrap("facet", ncol=len(versions), scales="free_y", drop=False)
        + _arm_scales()
        + labs(title=ATTENTION_TITLE, caption=caption, x=TIME_LABEL, y="Seconds")
        + scale_x_continuous(expand=LABEL_X_EXPAND)
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
        p += geom_point(
            data=frame(medians, base_cols),
            mapping=aes("x", "y", color="arm"),
            shape="_",
            size=5,
            show_legend=False,
        )
    if texts:
        p += geom_text(
            data=frame(texts, [*base_cols, "label"]),
            mapping=aes("x", "y", label="label"),
            size=TEXT_SIZE,
            color=NOTE_COLOR,
        )
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
OUTCOME_COLORS = {
    Outcome.YES.value: "#0072B2",
    Outcome.NO.value: "#E69F00",
    Outcome.UNKNOWN.value: "#999999",
    Outcome.NOT_APPLICABLE.value: "#FFFFFF",  # outlined
}
INTERVENTION_MEASURES = {
    InterventionMeasure.DELIVERED: "AI delivered",
    InterventionMeasure.VIEWED: "AI viewed",
    InterventionMeasure.FAILURE: "intervention failure",
    InterventionMeasure.LEAKAGE: "no AI leakage",
    InterventionMeasure.PER_PROTOCOL: "per protocol",
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
        failed = int(r["intervention_failure"])
        return {Outcome.YES: failed, Outcome.NO: reached - failed}
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
    by_reason = {r: int(s.intervention[f"failure_{r.value}"].sum()) for r in FailureReason}
    failures = ", ".join(
        f"{reason.value.replace('_', ' ')} {count}" for reason, count in by_reason.items() if count
    )
    caption = _caption(
        f"Population: {POPULATION_REACHED}, grouped by ITT arm (never by exposure).",
        "Delivered / viewed apply to the AI arm; leakage to the no AI arm. Unknown and",
        "indeterminate are counted, never read as no. n above each bar = reached",
        "observations.",
        f"Failure reasons: {failures}." if failures else "No intervention failure.",
        DESCRIPTIVE_NOTE,
    )
    if s.cohort is Cohort.NO_ACTIVATED_CASES:
        return _placeholder(INTERVENTION_TITLE, caption)

    rows = [
        f"{label} ({ARM_LABELS[arm]} arm)"
        for label in INTERVENTION_MEASURES.values()
        for arm in ARMS
    ]
    data, totals = [], []
    for r in s.intervention.to_dict("records"):
        version = bundle.versions[r["config_version"]]
        t = _t_label(_minutes(version, r["t_index"]))
        for measure, label in INTERVENTION_MEASURES.items():
            row = f"{label} ({ARM_LABELS[r['arm']]} arm)"
            counts = _outcomes(measure, r)
            for outcome, (code, _legend) in OUTCOMES.items():
                data.append(
                    _segment(row, r["config_version"], t, outcome, counts.get(outcome, 0), code)
                )
            totals.append(
                {
                    "row": row,
                    "col": r["config_version"],
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
        cols=list(bundle.versions),
        t_labels=_all_t_labels(bundle),
        legend="Outcome",
        title=INTERVENTION_TITLE,
        caption=caption,
    )


# ---------------------------------------------------------------------------
# Registry and output
# ---------------------------------------------------------------------------

FIGURES: Mapping[str, FigureSpec] = MappingProxyType(
    {
        spec.stem: spec
        for spec in (
            FigureSpec("figure_01_divergence", DIVERGENCE_TITLE, divergence_figure),
            FigureSpec("figure_02_case_flow", CASE_FLOW_TITLE, case_flow_figure),
            FigureSpec(
                "figure_03_response_provenance", PROVENANCE_TITLE, response_provenance_figure
            ),
            FigureSpec("figure_04_attention", ATTENTION_TITLE, attention_figure),
            FigureSpec("figure_05_intervention", INTERVENTION_TITLE, intervention_figure),
        )
    }
)


def build_figure(stem: str, bundle: ValidatedStudyBundle, summaries: AnalysisSummaries) -> ggplot:
    return FIGURES[stem].build(bundle, summaries)


def save_figure(plot: ggplot, stem_path: Path) -> tuple[Path, ...]:
    """Write ``<stem_path>.svg`` and ``.pdf``; SVG text stays text (searchable).

    Drawn once, saved in every format (drawing dominates the cost).
    """
    written = []
    with matplotlib.rc_context(_SVG_RC):
        figure = deepcopy(plot).draw()  # draw() mutates the plot; keep the caller's
        try:
            for fmt in FIGURE_FORMATS:
                path = stem_path.with_suffix(f".{fmt}")
                figure.savefig(path, format=fmt, metadata=_METADATA[fmt])
                written.append(path)
        finally:
            plt.close(figure)
    return tuple(written)
