"""S10 rough divergence view: one descriptive SVG per patient.

Layering: this module is a *tool* layer — it reads the study DB through
the read DAOs, derives per-arm answer/timing series with the strict S9c
decode, summarizes newly visible data against the canonical frames, and
renders ONE figure per patient with ``plotnine`` (no second charting
stack). It never imports from ``web/``; the dataset arrives as a
structural object with the four canonical frames (``scalar_ts``,
``admission``, ``imaging``, ``ai_output``) — exactly the shape the
adapters in ``ingestion/`` produce.

Scope (spec §6-§7, descriptive only):

* no p-values, no CIs, no statistical tests — medians and proportions;
* ``free-text`` values NEVER enter the figure; only the count of
  non-empty responses by arm × timepoint;
* categorical options keep their questions.yaml order — no implied
  numeric ordering; multi-select proportions need not sum to 1;
* the timing panel carries the axis text "wall-clock elapsed seconds"
  (never "active time" / "dwell time");
* single-arm data still renders, annotated with :data:`SINGLE_ARM_NOTE`
  — no fabricated comparison (S11 will randomize arms);
* newly visible data is summarized by canonical category with counts
  only — raw clinical values never appear in annotations.

API:

- :class:`DivergenceError` — every refusal; the CLI maps it to exit 1.
- :func:`newly_visible_summary` — the per-timepoint annotation strings
  (pure over the dataset frames; independently testable).
- :func:`build_divergence_figure` — pure over (conn, configs, dataset);
  returns the unsaved plotnine figure (the CLI saves the SVG).
"""

from __future__ import annotations

from statistics import median as _median
from typing import TYPE_CHECKING, Any

import pandas as pd
from plotnine import (
    aes,
    element_rect,
    element_text,
    facet_wrap,
    geom_line,
    geom_point,
    geom_text,
    ggplot,
    labs,
    theme,
)

from ehr_simulator import timing
from ehr_simulator.answer_codec import AnswerValidationError, decode_stored_answer
from ehr_simulator.config import (
    CategoricalQuestion,
    FreeTextQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    ProbabilityQuestion,
    Question,
    Questions,
    StudyConfig,
)
from ehr_simulator.db import answers, arm_assignments
from ehr_simulator.ingestion.canonical import LAB_VAR_SET, VITAL_VAR_SET

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "SINGLE_ARM_NOTE",
    "WALL_CLOCK_LABEL",
    "DivergenceError",
    "build_divergence_figure",
    "newly_visible_summary",
]

#: Spec §6 — exact annotation text when only one arm is present.
SINGLE_ARM_NOTE = "Single-arm data at this patient/timepoint; no between-arm comparison available."

#: Spec §6 — the timing panel's axis label (never "active time"/dwell).
WALL_CLOCK_LABEL = "wall-clock elapsed seconds"

# Deterministic per-position encodings as **constants** (never aes
# mappings): the figure can hold up to two arms × ~6 options + medians,
# which exceeds plotnine's discrete-scale palette limits (linetype 4,
# colour 10). Constants bypass palette resolution entirely.
_ARM_COLORS = ("#1a1a1a", "#c0392b", "#1f77b4", "#2ca02c", "#9467bd", "#8c564b")
_ARM_COLOR_NAMES = ("black", "red", "blue", "green", "purple", "brown")
_OPT_STYLES = (
    "solid",
    "dashed",
    "dotted",
    (0, (3, 1, 1, 1)),
    (0, (1, 1)),
    (0, (4, 1)),
)
_STYLE_NAMES = ("solid", "dashed", "dotted", "dash-dot", "fine-dotted", "long-dash")

_PANEL_TIMING = "timing"
_PANEL_ANNOTATION = "newly_visible_data"

#: Uniform long-format columns every panel frame carries.
_FRAME_COLUMNS = ("x", "arm", "series", "value", "kind", "g", "label", "style")


class DivergenceError(ValueError):
    """The inputs cannot be rendered into a divergence figure.

    Every refusal path raises this (patient not in the study, unknown
    question id, configured-timepoint refutation, config-hash drift,
    invalid stored answer, conflicting arm, invalid timing history).
    The CLI maps it to a clean one-line error + exit code 1.
    """


# ---------------------------------------------------------------------------
# annotations (spec §7)
# ---------------------------------------------------------------------------


def newly_visible_summary(
    dataset: Any, patient_id: str, timepoints_minutes: Sequence[float]
) -> list[str]:
    """One annotation string per study timepoint.

    Interval rule — identical to the UI's ``data <= t`` visibility rule:

    * ``i == 0``: data visible at or before ``t_0``;
    * ``i > 0``:  ``t_(i-1) < data_time <= t_i``.

    Counts only, canonical category, fixed order: ``admission`` (first
    timepoint only), ``vitals``, ``labs``, ``other scalar`` (any scalar
    variable outside the vitals/lab sets), ``AI``, ``imaging``.
    Categories with zero new rows are omitted; an all-empty interval
    renders ``"no new data"``. Otherwise the annotation is prefixed
    ``"new: "`` (spec §7 example): ``new: vitals 6 · labs 4 · AI 1``.
    Raw clinical values never appear.
    """
    if len(timepoints_minutes) == 0:
        return []
    scalar_pid = dataset.scalar_ts.loc[dataset.scalar_ts.patient_id == patient_id]
    admission_pid = dataset.admission.loc[dataset.admission.patient_id == patient_id]
    imaging_pid = dataset.imaging.loc[dataset.imaging.patient_id == patient_id]
    ai_pid = dataset.ai_output.loc[dataset.ai_output.patient_id == patient_id]

    vitals = scalar_pid.loc[scalar_pid.variable.isin(VITAL_VAR_SET)]
    labs = scalar_pid.loc[scalar_pid.variable.isin(LAB_VAR_SET)]
    other_scalar = scalar_pid.loc[
        ~scalar_pid.variable.isin(VITAL_VAR_SET) & ~scalar_pid.variable.isin(LAB_VAR_SET)
    ]

    out: list[str] = []
    for i, t_i in enumerate(timepoints_minutes):
        low = None if i == 0 else timepoints_minutes[i - 1]
        parts: list[str] = []
        if i == 0 and not admission_pid.empty:
            parts.append("admission")
        for label, frame in (
            ("vitals", vitals),
            ("labs", labs),
            ("other scalar", other_scalar),
            ("AI", ai_pid),
            ("imaging", imaging_pid),
        ):
            n = _count_visible(frame, low, t_i)
            if n:
                parts.append(f"{label} {n}")
        out.append(("new: " + " · ".join(parts)) if parts else "no new data")
    return out


def _count_visible(frame: Any, low: float | None, high: float) -> int:
    if frame.empty:
        return 0
    if low is None:
        return int((frame.t_minutes <= high).sum())
    return int(((frame.t_minutes > low) & (frame.t_minutes <= high)).sum())


# ---------------------------------------------------------------------------
# public build
# ---------------------------------------------------------------------------


def build_divergence_figure(
    conn: Any,
    *,
    study: StudyConfig,
    questions: Questions,
    live_hash: str,
    patient_id: str,
    dataset: Any,
) -> Any:
    """Render the unsaved plotnine figure for one patient (spec §6-§7).

    Raises :class:`DivergenceError` on any integrity problem (same
    posture as the S9c exporter). The caller saves the SVG.
    """
    if patient_id not in study.patient_ids:
        raise DivergenceError(f"patient {patient_id!r} is not in the study config")
    tps: tuple[float, ...] = tuple(study.timepoints_minutes)
    tps_set = set(tps)
    by_id: Mapping[str, Question] = {q.question_id: q for q in questions.questions}

    all_answers = tuple(a for a in answers.fetch_all(conn) if a.patient_id == patient_id)
    # S10 fix: config-hash drift is checked on the assignment rows too, not
    # only the answer rows — with zero answers the drift would otherwise be
    # invisible (spec §5: the patient's answers AND their locked arm were
    # both written under the live config).
    all_assignments = tuple(
        a for a in arm_assignments.fetch_all(conn) if a.patient_id == patient_id
    )
    for a in all_assignments:
        if a.config_hash != live_hash:
            raise DivergenceError(
                "arm assignment for patient "
                f"{patient_id!r} was recorded under a different study/question "
                "configuration (config-hash drift)"
            )
    assignments = {a.clinician_id: a.arm for a in all_assignments}

    arm_of = _attribute_arms(patient_id, all_answers, assignments, by_id, live_hash, tps_set)

    decoded: list[tuple[str, float, str, str]] = []  # (clinician, t, qid, decoded)
    for a in all_answers:
        q = by_id[a.question_id]
        try:
            val = decode_stored_answer(q, a.value)
        except AnswerValidationError as exc:
            raise DivergenceError(f"stored answer does not decode: {exc}") from exc
        decoded.append((a.clinician_id, a.timepoint, a.question_id, val))

    # -- Panels: one per configured question, in config order -----------
    panels: list[tuple[str, pd.DataFrame]] = []
    fallback_arm = next(iter(arm_of.values()), "not set")
    for q in questions.questions:
        mine = [(cid, t, val) for cid, t, qid, val in decoded if qid == q.question_id]
        df = _question_panel(q, mine, arm_of, fallback_arm)
        if df.empty:
            # S10 fix: an all-blank question must still get a visible
            # placeholder strip — "no responses", never a blank facet.
            df = _placeholder_panel(tps, fallback_arm, "no responses")
        panels.append((q.question_id, df))

    # -- Timing (spec §4/§6) --------------------------------------------
    events = tuple(e for e in timing.fetch_timing_events(conn) if e.patient_id == patient_id)
    # S9c guarantee retained by S10: a timing event's recording generation is
    # pinned on its sessions row — refuse a present, differing hash.
    for ev in events:
        if ev.config_hash not in (None, live_hash):
            raise DivergenceError(
                "timepoint enter/exit event for clinician "
                f"{ev.clinician_id!r}, patient {patient_id!r} was recorded in a session "
                "opened under a different study/question configuration (config-hash drift)"
            )
    t_points: list[tuple[str, float, float]] = []  # (arm, t, elapsed)
    for cid in sorted(arm_of):
        try:
            per_tp = timing.derive_timepoint_timings(
                events, clinician_id=cid, patient_id=patient_id
            )
        except timing.TimingError as exc:
            raise DivergenceError(
                f"cannot derive timepoint timing for clinician {cid}: {exc}"
            ) from exc
        for t, tt in per_tp.items():
            if tt.elapsed_seconds is None:
                continue
            t_points.append((arm_of[cid], t, float(tt.elapsed_seconds)))

    # -- Annotation material: per-timepoint arms for the single-arm note -
    arms_at_tp: dict[float, set[str]] = {t: set() for t in tps}
    for cid, t, _qid, _val in decoded:
        arms_at_tp[t].add(arm_of[cid])
    for arm, t, _elapsed in t_points:
        arms_at_tp[t].add(arm)

    ann_points: list[tuple[float, str]] = []
    ann_strings = newly_visible_summary(dataset, patient_id, list(tps))
    for idx, t in enumerate(tps):
        ann_points.append((t, ann_strings[idx]))
        if len(arms_at_tp[t]) == 1:
            ann_points.append((t, SINGLE_ARM_NOTE))

    # S10 fix: facet order is all questions (config order), then the
    # newly-visible-data annotations, then the timing panel last — a
    # consistent reading top-to-bottom, with timing the explicit tail.
    panels.append((_PANEL_ANNOTATION, _annotation_panel(ann_points, fallback_arm)))
    timing_df = _timing_panel(t_points, fallback_arm)
    if timing_df.empty:
        timing_df = _placeholder_panel(tps, fallback_arm, "no completed timing intervals")
    panels.append((_PANEL_TIMING, timing_df))

    # S10 fix: deduplicate — multiple clinicians can share one arm, and the
    # subtitle must list each arm once (rendering uses one colour per unique
    # arm), not once per clinician.
    arms_present = sorted(set(arm_of.values())) or [fallback_arm]
    arm_note = " · ".join(
        f"{a} = {_ARM_COLOR_NAMES[i % len(_ARM_COLOR_NAMES)]}" for i, a in enumerate(arms_present)
    )
    subtitle = (
        "descriptive only (medians/proportions; no statistical tests) · "
        f"arm colours — {arm_note} · free-text values are never plotted"
    )
    return _render(
        panels,
        title=f"Rough divergence view — patient {patient_id}",
        subtitle=subtitle,
    )


def _attribute_arms(
    patient_id: str,
    all_answers: tuple[Any, ...],
    assignments: Mapping[str, str],
    by_id: Mapping[str, Question],
    live_hash: str,
    tps_set: set[float],
) -> dict[str, str]:
    """Arm per clinician: primary ``arm_assignments``, fallback answer
    rows; any conflict → :class:`DivergenceError` (spec §6). Also the
    per-row config-drift checks (spec §5/§6)."""
    for a in all_answers:
        if a.question_id not in by_id:
            raise DivergenceError(
                f"answers row for patient {patient_id!r} references unknown question "
                f"{a.question_id!r}; the DB was written under a different questions.yaml"
            )
        if a.config_hash != live_hash:
            raise DivergenceError(
                "answers for patient "
                f"{patient_id!r} were recorded under a different study/question "
                "configuration (config-hash drift)"
            )
        if a.timepoint not in tps_set:
            raise DivergenceError(
                f"answers row for patient {patient_id!r} references timepoint "
                f"{a.timepoint} the study config does not know"
            )

    # S10 fix: arm_of is initialised from the *assignment map*, not just the
    # clinicians appearing in all_answers — an arm assignment with no answers
    # yet still pins a known arm for that clinician (the old code ignored that
    # and could label the figure "not set"). Then every answer row is
    # validated against its clinician's assigned arm (or, where there is no
    # assignment, against the row's own column).
    arm_of: dict[str, str] = {cid: assignments[cid] for cid in sorted(assignments)}
    for cid in sorted({a.clinician_id for a in all_answers}):
        assigned = assignments.get(cid)
        row_arms = sorted({a.arm for a in all_answers if a.clinician_id == cid})
        if len(row_arms) > 1:
            raise DivergenceError(
                f"conflicting arm values ({', '.join(row_arms)}) in answer rows "
                f"for clinician {cid}, patient {patient_id!r}"
            )
        if assigned is not None:
            if row_arms and row_arms[0] != assigned:
                raise DivergenceError(
                    f"answers for clinician {cid} (patient {patient_id!r}) hold arm "
                    f"{row_arms[0]!r} but the locked assignment is {assigned!r}"
                )
            # arm_of[cid] already set from the assignment.
        elif len(row_arms) == 1:
            arm_of[cid] = row_arms[0]
        else:
            # Defensive: a clinician appearing in all_answers always has ≥1
            # answer row carrying an arm, so this is unreachable; the guard
            # keeps the failure loud (per-cid message) rather than a KeyError
            # downstream.
            raise DivergenceError(f"no resolvable arm for clinician {cid}, patient {patient_id!r}")
    return arm_of


# ---------------------------------------------------------------------------
# panel builders (uniform long-format frames)
# ---------------------------------------------------------------------------


def _question_panel(
    q: Question,
    mine: list[tuple[str, float, str]],  # (clinician, t, decoded)
    arm_of: Mapping[str, str],
    fallback_arm: str,
) -> pd.DataFrame:
    per: dict[tuple[str, float], list[str]] = {}
    for cid, t, val in mine:
        per.setdefault((arm_of[cid], t), []).append(val)

    rows: list[dict[str, Any]] = []
    if isinstance(q, (LikertQuestion, ProbabilityQuestion)):
        xs: list[float] = []
        hi_vals: list[float] = []
        for (arm, t), vals in sorted(per.items()):
            nums = [float(_as_number(v, "numeric answer")) for v in vals]
            xs.append(t)
            hi_vals.extend(nums)
            for v in nums:
                rows.append({"x": t, "arm": arm, "series": "·", "value": v, "kind": "obs"})
            rows.append(
                {
                    "x": t,
                    "arm": arm,
                    "series": "median",
                    "value": float(_median(nums)),
                    "kind": "median",
                }
            )
        if xs:
            rows.append(
                {
                    "x": min(xs),
                    "arm": fallback_arm,
                    "series": "note",
                    "value": float(max(hi_vals) + 1),
                    "kind": "text",
                    "label": "median = ◆ · individual observations = ○",
                }
            )
        return _frame(rows, fallback_arm)

    if isinstance(q, CategoricalQuestion):
        return _option_panel(q, per, q.options, fallback_arm)

    if isinstance(q, MultiSelectQuestion):
        return _option_panel(q, per, q.options, fallback_arm)

    if isinstance(q, FreeTextQuestion):
        max_n = 0
        xs: list[float] = []
        for (arm, t), vals in sorted(per.items()):
            n = sum(1 for v in vals if (v or "").strip())
            max_n = max(max_n, n)
            xs.append(t)
            rows.append(
                {
                    "x": t,
                    "arm": arm,
                    "series": "non-empty responses",
                    "value": float(n),
                    "kind": "obs",
                }
            )
        if xs:
            rows.append(
                {
                    "x": min(xs),
                    "arm": fallback_arm,
                    "series": "note",
                    "value": float(max_n + 1),
                    "kind": "text",
                    "label": "count of non-empty responses — never the free-text values",
                }
            )
        return _frame(rows, fallback_arm)

    raise DivergenceError(f"unsupported response type {q.response_type!r} (defensive)")


def _as_number(v: str, ctx: str) -> int:
    try:
        return int(v)
    except ValueError as exc:
        raise DivergenceError(f"{ctx} does not decode: {v!r}") from exc


def _option_panel(
    q: Question,
    per: dict[tuple[str, float], list[str]],
    options: tuple[str, ...],
    fallback_arm: str,
) -> pd.DataFrame:
    """Categorical / multi-select: proportion per option, one line per
    (arm, option), arm by colour, option by line style (spec §5)."""
    rows: list[dict[str, Any]] = []
    xs: list[float] = []
    for (arm, t), vals in sorted(per.items()):
        answered = len(vals)
        xs.append(t)
        for oi, opt in enumerate(options):  # questions.yaml order — no numeric order implied
            if isinstance(q, CategoricalQuestion):
                n = sum(1 for v in vals if v == opt)
            else:  # MultiSelectQuestion: decoded value is pipe-joined
                n = sum(1 for v in vals if opt in v.split("|"))
            rows.append(
                {
                    "x": t,
                    "arm": arm,
                    "series": opt,
                    "value": n / answered if answered else 0.0,
                    "kind": "line",
                    "style": _OPT_STYLES[oi % len(_OPT_STYLES)],
                }
            )
    if xs:
        rows.append(
            {
                "x": min(xs),
                "arm": fallback_arm,
                "series": "note",
                "value": 0.5,
                "kind": "text",
                "label": " · ".join(
                    f"{opt} = {_STYLE_NAMES[oi % len(_STYLE_NAMES)]}"
                    for oi, opt in enumerate(options)
                ),
            }
        )
    return _frame(rows, fallback_arm)


def _timing_panel(points: list[tuple[str, float, float]], fallback_arm: str) -> pd.DataFrame:
    if not points:
        return _frame([], fallback_arm)
    per: dict[tuple[str, float], list[float]] = {}
    for arm, t, elapsed in points:
        per.setdefault((arm, t), []).append(elapsed)
    rows: list[dict[str, Any]] = []
    for (arm, t), vals in sorted(per.items()):
        for v in vals:
            rows.append({"x": t, "arm": arm, "series": "·", "value": v, "kind": "obs"})
        m = float(_median(vals))
        rows.append({"x": t, "arm": arm, "series": "median", "value": m, "kind": "median"})
        rows.append({"x": t, "arm": arm, "series": "median", "value": m, "kind": "line"})
    # Spec §6: the axis label "wall-clock elapsed seconds" rides on the
    # panel as a text note (plotnine facets share one global y-label).
    lo_x = min(r["x"] for r in rows)
    lo_y = min(r["value"] for r in rows)
    rows.append(
        {
            "x": lo_x,
            "arm": fallback_arm,
            "series": WALL_CLOCK_LABEL,
            "value": max(0.0, lo_y) - 2.0,
            "kind": "text",
            "label": f"{WALL_CLOCK_LABEL} (median ◇, individual ● — not active/dwell time)",
        }
    )
    return _frame(rows, fallback_arm)


def _annotation_panel(points: list[tuple[float, str]], fallback_arm: str) -> pd.DataFrame:
    if not points:
        return _frame([], fallback_arm)
    # two note rows (data + single-arm) must not stack on one x: offset y
    seen: dict[float, int] = {}
    rows: list[dict[str, Any]] = []
    for t, s in points:
        seen[t] = seen.get(t, 0) + 1
        rows.append(
            {
                "x": t,
                "arm": fallback_arm,
                "series": s,
                "value": float(seen[t]),
                "kind": "text",
                "label": s,
            }
        )
    return _frame(rows, fallback_arm)


def _placeholder_panel(x_range: Sequence[float], fallback_arm: str, label: str) -> pd.DataFrame:
    """Honest blank: a timepoint range plus a single text row (``label``)."""
    if not x_range:
        return _frame([], fallback_arm)
    return _frame(
        [
            {
                "x": float(min(x_range)),
                "arm": fallback_arm,
                "series": "note",
                "value": 0.5,
                "kind": "text",
                "label": label,
            }
        ],
        fallback_arm,
    )


def _frame(rows: list[dict[str, Any]], fallback_arm: str) -> pd.DataFrame:
    """Uniform long-format frame (all columns always present)."""
    if not rows:
        cols: dict[str, pd.Series] = {c: pd.Series(dtype=object) for c in _FRAME_COLUMNS}
        cols["value"] = pd.Series(dtype="float64")
        cols["x"] = pd.Series(dtype="float64")
        return pd.DataFrame(cols)
    df = pd.DataFrame(rows)
    for col in ("arm", "series", "label", "style"):
        if col not in df.columns:
            df[col] = None
    df["arm"] = df["arm"].fillna(fallback_arm)
    df["series"] = df["series"].fillna("")
    df["style"] = df["style"].fillna("solid")
    df["g"] = df["arm"].astype(str) + "|" + df["series"].astype(str) + "|" + df["kind"].astype(str)
    out = pd.DataFrame()
    out["x"] = df["x"].astype("float64")
    out["arm"] = df["arm"].astype(str)
    out["series"] = df["series"].astype(str)
    out["value"] = df["value"].astype("float64")
    out["kind"] = df["kind"].astype(str)
    out["g"] = df["g"].astype(str)
    out["label"] = df["label"].fillna("").astype(str)
    out["style"] = df["style"].astype(object)
    return out


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _render(panels: list[tuple[str, pd.DataFrame]], *, title: str, subtitle: str) -> Any:
    """One ``ggplot`` + one facet row per panel.

    Arms and series are encoded with **constant** colours/line styles
    (never aes mappings): the figure can hold two arms x ~6 options per
    panel, which exceeds plotnine's discrete-scale palette limits
    (linetype 4, colour 10). Constants bypass palette resolution
    entirely (see :data:`_ARM_COLORS`).

    Plotnine 0.15.x has no ``combined_plot``/``subplot``; the only
    verified multi-panel recipe is a single base dataset (concatenated
    panel frames) + ``facet_wrap(..., ncol=1, scales="free_y",
    as_table=True)`` with each geom restricted to its panel subset via
    ``data=``.
    """
    parts = []
    panel_order = [name for name, _df in panels]
    for name, df in panels:
        part = df.copy()
        # Ordered Categorical: plotnine facets by category levels, which
        # preserves the requested panel order (the alphabetical fallback
        # would scramble question ids and the timing/annotation tails).
        part["panel"] = pd.Categorical([name] * len(part), categories=panel_order, ordered=True)
        parts.append(part)
    base = pd.concat(parts, ignore_index=True)
    for col in ("arm", "series", "label", "style"):
        if col not in base.columns:
            base[col] = None

    arms = sorted(base["arm"].dropna().astype(str).unique().tolist())
    arm_color = {a: _ARM_COLORS[i % len(_ARM_COLORS)] for i, a in enumerate(arms)}

    n = len(panels)
    height = max(6.5, 0.9 * n + 3.0)

    p = (
        ggplot(base, aes("panel"))
        + facet_wrap("panel", nrow=n, ncol=1, scales="free_y", as_table=True)
        + theme(
            figure_size=(10.0, height),
            plot_background=element_rect(fill="white", color=None),
            panel_background=element_rect(fill="white", color=None),
            strip_background=element_rect(fill="#eaeaea", color=None),
            strip_text=element_text(size=11, hjust=0),
        )
    )

    for part in parts:  # each panel frame, already tagged with ``panel``
        if part.empty:
            continue
        names = set(part["kind"].unique())
        if "text" in names:  # panel notes: option styles, wall-clock, annotations
            text_df = part.loc[part["kind"] == "text"]
            p = p + geom_text(
                data=text_df,
                mapping=aes(x="x", y="value", label="label"),
                size=10.5,
                color="#3a3a3a",
            )
        if "line" in names:  # one line per (arm, series); per-geom constants
            line = part.loc[part["kind"] == "line"]
            for (arm, _), sub in line.groupby(["arm", "series"], sort=True):
                style = sub["style"].iloc[0]
                color = arm_color.get(arm, _ARM_COLORS[0])
                p = p + geom_line(
                    data=sub,
                    mapping=aes(x="x", y="value", group="g"),
                    color=color,
                    size=1.0,
                    alpha=0.9,
                    linetype=style,
                )
                p = p + geom_point(
                    data=sub,
                    mapping=aes(x="x", y="value"),
                    color=color,
                    size=2.1,
                    alpha=0.9,
                )
        for kind, shape, size, alpha in (("obs", "o", 2.5, 0.7), ("median", "D", 4.2, 1.0)):
            pts = part[part["kind"] == kind]
            if pts.empty:
                continue
            for arm, sub in pts.groupby("arm", sort=True):
                p = p + geom_point(
                    data=sub,
                    mapping=aes(x="x", y="value"),
                    color=arm_color.get(arm, _ARM_COLORS[0]),
                    shape=shape,
                    size=size,
                    alpha=alpha,
                )

    return p + labs(
        title=title,
        subtitle=subtitle,
        x="time (minutes since first contact)",
    )
