"""Panel + summary-card rendering for the patient view (templates + charts, no DB).

Per-panel renderer exceptions are contained here (Decision **D9**): a
failed panel renders with the error visual treatment; the exception detail
stays in the log.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

import pandas as pd
from fastapi import Request

from ehr_simulator.config.study import BackwardNavigation
from ehr_simulator.ingestion.canonical import (
    AGGREGATE_BOUNDS,
    IMAGING_DERIVED_VAR_SET,
    LAB_VAR_SET,
    NEURO_SUPPORT_VAR_SET,
    VITAL_VAR_SET,
)
from ehr_simulator.logging import get_logger
from ehr_simulator.web.clinical_fields import (
    DisplayGroup,
    SummaryFact,
    admission_sections,
    imaging_findings,
    ordered_ids,
    scalar_label,
    summary_fact,
)
from ehr_simulator.web.panels import (
    LEGACY_INTERVENTION,
    InterventionContext,
    InterventionMode,
    PatientSlice,
    measured_ai_state,
    select_measured_ai,
)

_PANEL_RENDER_FAILED_MSG = "this panel could not be displayed"


def _render_summary(
    patient_slice: PatientSlice,
    request: Request,
    *,
    chrome: str,
    timepoint_count: int,
    show_next: bool,
    resume_t_index: dict[str, int],
    patient_ids: list[str] | None = None,
    intervention_mode: InterventionMode = InterventionMode.LEGACY,
    backward: BackwardNavigation = BackwardNavigation.ALLOW_READONLY,
) -> str:
    """``patient_ids`` overrides the jumper list (S11d Phase 2: own cases only).

    Measured cases drop the AI row count (S11g): it must not exist in a no
    AI case, and the AI arm keeps identical chrome. ``prohibit`` drops the
    Prev button (S11i).
    """
    templates = request.app.state.templates
    dataset = request.app.state.dataset
    admission_facts = {
        row.field: row.value for row in patient_slice.admission.itertuples(index=False)
    }
    counts = {
        "scalar_ts": int(len(patient_slice.scalar_ts)),
        "imaging": int(len(patient_slice.imaging)),
        "ai": int(len(patient_slice.ai_output)),
        "admission": int(len(patient_slice.admission)),
    }
    # Mirror the index-route filter: with a study config loaded, the
    # patient-jumper navigation only lists study patients (declared order
    # preserved). Without a study config, fall back to the full dataset list.
    study_patient_ids = getattr(request.app.state, "study_patient_ids", None)
    if patient_ids is not None:
        all_patient_ids = patient_ids
    elif study_patient_ids is not None:
        all_patient_ids = list(study_patient_ids)
    else:
        all_patient_ids = sorted(dataset.admission["patient_id"].unique().tolist())
    return templates.get_template("_summary_card.html").render(
        request=request,
        patient_slice=patient_slice,
        admission_facts=admission_facts,
        counts=counts,
        chrome=chrome,
        all_patient_ids=all_patient_ids,
        timepoint_count=timepoint_count,
        show_next=show_next,
        resume_t_index=resume_t_index,
        summary_age=summary_fact(admission_facts, SummaryFact.AGE),
        summary_sex=summary_fact(admission_facts, SummaryFact.SEX),
        show_ai_count=intervention_mode is InterventionMode.LEGACY,
        show_prev=backward is BackwardNavigation.ALLOW_READONLY,
    )


# plotnine/matplotlib keep global figure state: one panel render at a time.
_PANEL_RENDER_LOCK = threading.Lock()


def _render_panels_locked(
    patient_slice: PatientSlice, request: Request, intervention: InterventionContext
) -> dict[str, str]:
    """Threadpool entry of :func:`_render_panels` (pure: templates + charts, no DB)."""
    with _PANEL_RENDER_LOCK:
        return _render_panels(patient_slice, request, intervention)


def _render_panels(
    patient_slice: PatientSlice,
    request: Request,
    intervention: InterventionContext = LEGACY_INTERVENTION,
) -> dict[str, str]:
    """Render each panel inside its own try/except so a failure in one panel
    cannot take down the whole page (Decision **D9**).

    S11g: a measured no AI case never calls the AI renderer, so the result
    has no ``"ai"`` key at all; a measured AI case renders the frozen row.
    """

    log = get_logger()
    out: dict[str, str] = {}
    renderers: list[tuple[str, Callable[[PatientSlice, Request], str]]] = [
        ("vitals", _render_vitals),
        ("neuro_support", _render_neuro_support),
        ("labs", _render_labs),
        ("admission", _render_admission),
        ("imaging", _render_imaging),
    ]
    if intervention.mode is InterventionMode.LEGACY:
        renderers.append(("ai", _render_ai))
    elif intervention.mode is InterventionMode.AI:
        renderers.append(("ai", partial(_render_measured_ai, intervention=intervention)))

    for panel_name, render_fn in renderers:
        try:
            out[panel_name] = render_fn(patient_slice, request)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "panel.render.failed",
                panel=panel_name,
                error=repr(exc),
            )
            patient_slice.panel_states[panel_name] = "error"
            patient_slice.panel_errors[panel_name] = repr(exc)
            templates = request.app.state.templates
            # The exception detail stays in the log, never on a clinician's screen.
            out[panel_name] = templates.get_template("_panel_error.html").render(
                request=request,
                panel=panel_name,
                error=_PANEL_RENDER_FAILED_MSG,
            )
    return out


# Round-03 layout: BP grouped on shared mmHg, then MAP (S8), HR, RR;
# SpO2/Temp below. Order is fixed (clinician reading order); only present
# vitals get rendered, but the order they appear within each figure stays
# stable. A family is present when any of its members (median or bound) is.
_VITALS_TABLE_ORDER: tuple[str, ...] = ("sbp", "dbp", "map", "hr", "rr", "spo2", "temp")
_BP_PANEL_VARS: frozenset[str] = frozenset({"sbp", "dbp"})
_BP_GROUP_VARS_ORDERED: tuple[str, ...] = ("sbp", "dbp")
_UPPER_SINGLE_VARS: tuple[str, ...] = ("map", "hr", "rr")
_LOWER_SINGLE_VARS: tuple[str, ...] = ("spo2", "temp")
_NEURO_ORDER: tuple[str, ...] = ("nihss", "gcs", "fio2")
_BP_GROUP = "bp"
_BP_LABEL = "BP"
_BP_UNIT = "mmHg"
_VITAL_LABELS: dict[str, str] = {"spo2": "SpO₂", "fio2": "FiO₂"}  # else the upper-cased variable


@dataclass(frozen=True)
class _VitalsPanelSpec:
    """One row of a vitals figure: the grouped BP panel or a single variable.

    ``variable is None`` marks the grouped BP panel, which plots
    ``present_bp`` and annotates ``missing``.
    """

    group: str
    label: str
    unit: str
    variable: str | None = None
    present_bp: frozenset[str] = frozenset()
    missing: tuple[str, ...] = ()

    @property
    def is_grouped(self) -> bool:
        return self.variable is None


@dataclass(frozen=True)
class _Cell:
    """One table cell: a value plus its aggregate bounds (``None`` = absent)."""

    value: float | None
    low: float | None = None
    high: float | None = None


def _single_vital_spec(variable: str, units: dict[str, str]) -> _VitalsPanelSpec:
    return _VitalsPanelSpec(
        group=variable,
        label=_VITAL_LABELS.get(variable, variable.upper()),
        unit=units.get(variable, ""),
        variable=variable,
    )


def _family_members(variable: str) -> tuple[str, ...]:
    """``hr`` → ``("hr", "hr_min", "hr_max")``; a plain variable → itself."""
    return (variable, *AGGREGATE_BOUNDS.get(variable, ()))


def _present_families(rows: pd.DataFrame, order: tuple[str, ...]) -> list[str]:
    present = set(rows["variable"].astype(str))
    return [v for v in order if present.intersection(_family_members(v))]


def _family_units(rows: pd.DataFrame, families: list[str]) -> dict[str, str]:
    """Unit per family, read from whichever member row comes first."""
    units: dict[str, str] = {}
    for family in families:
        members = rows.loc[rows["variable"].isin(_family_members(family)), "unit"]
        if not members.empty:
            units[family] = str(members.iloc[0])
    return units


def _has_bounds(rows: pd.DataFrame, variable: str) -> bool:
    return variable in AGGREGATE_BOUNDS and rows["variable"].isin(AGGREGATE_BOUNDS[variable]).any()


def _cells_by_time(rows: pd.DataFrame, family: str) -> dict[float, _Cell]:
    """Per observation time: the family's median and bounds (never interpolated)."""
    from ehr_simulator.web.charts import family_frame

    if family not in AGGREGATE_BOUNDS:
        values = rows.loc[rows["variable"] == family]
        return {float(r.t_minutes): _Cell(float(r.value)) for r in values.itertuples(index=False)}

    low, high = AGGREGATE_BOUNDS[family]
    frame = family_frame(rows, min_var=low, median_var=family, max_var=high)
    return {
        float(r.t_minutes): _Cell(_maybe(r.median), _maybe(r.low), _maybe(r.high))
        for r in frame.itertuples(index=False)
    }


def _maybe(value: float) -> float | None:
    return None if pd.isna(value) else float(value)


def _scalar_tables(
    rows: pd.DataFrame, families: list[str], units: dict[str, str], current_t: float
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Values pivot (one row per time, one cell per family) + a11y fallback
    rows (time, variable, min, value, max, unit)."""
    by_family = {family: _cells_by_time(rows, family) for family in families}
    times = sorted({t for cells in by_family.values() for t in cells})
    empty = _Cell(None)

    pivot_rows = [
        {
            "t": t,
            "is_current": t == current_t,
            "cells": [by_family[family].get(t, empty) for family in families],
        }
        for t in times
    ]
    fallback_rows = [
        {
            "t": t,
            "variable": scalar_label(family),
            "cell": by_family[family][t],
            "unit": units.get(family, ""),
        }
        for t in times
        for family in families
        if t in by_family[family]
    ]
    return pivot_rows, fallback_rows


def _render_scalar_figure(
    specs: list[_VitalsPanelSpec],
    sorted_rows: pd.DataFrame,
    x_range: tuple[float, float],
) -> list[dict[str, object]]:
    """Render one stacked figure; only its bottom panel carries tick labels.

    An aggregate family with bounds (or bounds only) gets range bars (S8).
    """
    from ehr_simulator.web.charts import (
        render_grouped_bp_svg,
        render_range_timeline_svg,
        render_timeline_svg,
    )

    medians = set(sorted_rows["variable"].astype(str))
    rendered: list[dict[str, object]] = []
    for idx, spec in enumerate(specs):
        is_bottom = idx == len(specs) - 1
        if spec.variable is None:
            svg = render_grouped_bp_svg(
                sorted_rows, present_vars=spec.present_bp, x_range=x_range, is_bottom=is_bottom
            )
        elif _has_bounds(sorted_rows, spec.variable) or spec.variable not in medians:
            low, high = AGGREGATE_BOUNDS[spec.variable]
            svg = render_range_timeline_svg(
                sorted_rows,
                min_var=low,
                median_var=spec.variable,
                max_var=high,
                x_range=x_range,
                is_bottom=is_bottom,
            )
        else:
            svg = render_timeline_svg(
                sorted_rows, spec.variable, x_range=x_range, is_bottom=is_bottom
            )

        rendered.append(
            {
                "group": spec.group,
                "label": spec.label,
                "unit": spec.unit,
                "svg": svg,
                "is_bottom": is_bottom,
                "is_grouped": spec.is_grouped,
                "missing": list(spec.missing),
            }
        )
    return rendered


def _x_range(rows: pd.DataFrame) -> tuple[float, float]:
    """Shared time window of the sliced rows (never beyond t)."""
    all_t = rows["t_minutes"].astype(float)
    t_lo = float(all_t.min())
    t_hi = float(all_t.max())
    return (t_lo - 1.0, t_hi + 1.0) if t_lo == t_hi else (t_lo, t_hi)


def _render_vitals(patient_slice: PatientSlice, request: Request) -> str:
    """Vitals: two stacked figures.

    Round-03 layout (`specs/feedback/session-02-feedback.md` round-03):

    - **Upper figure** (hemodynamics): BP grouped on a shared mmHg y-scale,
      then MAP (S8), HR, then RR — top to bottom.
    - **Lower figure** (oxygenation/metabolic): SpO₂, then Temp.
    - All panels in a figure share the same x-range; tick labels render on
      the bottom-most panel only.
    - Partial-within-BP: if SBP or DBP is missing from the slice, the BP
      panel renders a faint dashed expected band at the missing variable's
      reference range and the panel-level note "DBP missing at this
      timepoint." appears below the BP panel.
    - S8: Geneva min/max bounds render as per-bucket range bars.

    Round-02 lineage (FINDING-007 / FINDING-008): per-variable rendering
    instead of ``facet_wrap`` because plotnine's facet strip text rendered
    as empty grey rectangles. Round-03 keeps that pattern for HR/RR/SpO₂/
    Temp and adds a single multi-line plotnine call for the BP panel.
    """
    templates = request.app.state.templates
    state = patient_slice.panel_states["vitals"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(VITAL_VAR_SET)]

    upper_panels: list[dict[str, object]] = []
    lower_panels: list[dict[str, object]] = []
    fallback_rows: list[dict[str, object]] = []
    variables_present: list[str] = []
    units: dict[str, str] = {}
    pivot_rows: list[dict[str, object]] = []
    bp_partial_note: str | None = None
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        variables_present = _present_families(rows, _VITALS_TABLE_ORDER)
        present_set = set(variables_present)
        units = _family_units(rows, variables_present)

        x_range = _x_range(rows)
        sorted_rows = rows.sort_values(["variable", "t_minutes"])

        medians = set(rows["variable"].astype(str))
        present_bp: frozenset[str] = frozenset(medians & _BP_PANEL_VARS)
        bp_missing = [v for v in _BP_GROUP_VARS_ORDERED if v not in present_bp]

        # Compose upper figure (BP → MAP → HR → RR), then lower (SpO₂ → Temp).
        upper_specs: list[_VitalsPanelSpec] = []
        if present_set & _BP_PANEL_VARS:
            upper_specs.append(
                _VitalsPanelSpec(
                    group=_BP_GROUP,
                    label=_BP_LABEL,
                    unit=_BP_UNIT,
                    present_bp=present_bp,
                    missing=tuple(bp_missing),
                )
            )
        upper_specs += [
            _single_vital_spec(v, units) for v in _UPPER_SINGLE_VARS if v in present_set
        ]
        lower_specs = [_single_vital_spec(v, units) for v in _LOWER_SINGLE_VARS if v in present_set]

        upper_panels = _render_scalar_figure(upper_specs, sorted_rows, x_range)
        lower_panels = _render_scalar_figure(lower_specs, sorted_rows, x_range)

        if bp_missing and present_bp:
            # Specific to round-03: the BP panel internally annotates which
            # of SBP/DBP is missing, layered on top of the panel-level
            # "Partial data at this timepoint." badge.
            missing_label = ", ".join(v.upper() for v in bp_missing)
            bp_partial_note = f"{missing_label} missing at this timepoint."

        pivot_rows, fallback_rows = _scalar_tables(rows, variables_present, units, current_t)

    return templates.get_template("_panel_vitals.html").render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get("vitals"),
        upper_panels=upper_panels,
        lower_panels=lower_panels,
        variables=variables_present,
        units=units,
        pivot_rows=pivot_rows,
        fallback_rows=fallback_rows,
        bp_partial_note=bp_partial_note,
    )


def _render_neuro_support(patient_slice: PatientSlice, request: Request) -> str:
    """Neurological / support (S8): NIHSS range bars, GCS and FiO₂ timelines."""
    templates = request.app.state.templates
    state = patient_slice.panel_states["neuro_support"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(NEURO_SUPPORT_VAR_SET)]

    panels: list[dict[str, object]] = []
    variables_present: list[str] = []
    units: dict[str, str] = {}
    pivot_rows: list[dict[str, object]] = []
    fallback_rows: list[dict[str, object]] = []
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        variables_present = _present_families(rows, _NEURO_ORDER)
        units = _family_units(rows, variables_present)
        specs = [_single_vital_spec(v, units) for v in variables_present]
        panels = _render_scalar_figure(
            specs, rows.sort_values(["variable", "t_minutes"]), _x_range(rows)
        )
        pivot_rows, fallback_rows = _scalar_tables(rows, variables_present, units, current_t)

    return templates.get_template("_panel_neuro_support.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("neuro_support"),
        panels=panels,
        variables=variables_present,
        labels=[_VITAL_LABELS.get(v, v.upper()) for v in variables_present],
        units=units,
        pivot_rows=pivot_rows,
        fallback_rows=fallback_rows,
    )


def _observation_table(
    rows: pd.DataFrame, order: tuple[str, ...]
) -> tuple[list[float], list[dict[str, object]]]:
    """Variable × observation time table over sliced rows, registry order.

    Columns are the distinct ``t_minutes`` of these rows (never the study
    timepoints: binning would change values, D21); a variable gets a row
    only when measured at or before ``t``.
    """
    timepoints = sorted({float(t) for t in rows["t_minutes"].tolist()})
    present = set(rows["variable"].astype(str))
    pivot: dict[str, dict[float, float]] = {}
    units: dict[str, str] = {}
    for r in rows.itertuples(index=False):
        units.setdefault(r.variable, str(r.unit))
        pivot.setdefault(r.variable, {})[float(r.t_minutes)] = float(r.value)
    table_rows = [
        {
            "variable": variable,
            "label": scalar_label(variable),
            "unit": units.get(variable, ""),
            "cells": [pivot[variable].get(t) for t in timepoints],
        }
        for variable in order
        if variable in present
    ]
    return timepoints, table_rows


def _render_labs(patient_slice: PatientSlice, request: Request) -> str:
    """Labs: variable-by-timepoint table (FINDING-005). Tabular form is the
    clinical standard for labs; charts add visual noise without aiding the
    point-in-time read. S8: registry order + labels, scrolling columns."""
    templates = request.app.state.templates
    state = patient_slice.panel_states["labs"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(LAB_VAR_SET)]

    timepoints: list[float] = []
    table_rows: list[dict[str, object]] = []
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        timepoints, table_rows = _observation_table(rows, ordered_ids(DisplayGroup.LABS))

    return templates.get_template("_panel_labs.html").render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get("labs"),
        timepoints=timepoints,
        current_t=current_t,
        table_rows=table_rows,
    )


def _admission_facts(patient_slice: PatientSlice) -> dict[str, object]:
    return {row.field: row.value for row in patient_slice.admission.itertuples(index=False)}


def _render_admission(patient_slice: PatientSlice, request: Request) -> str:
    """S8: registry sections (baseline, treatment), unregistered fields under Other."""
    templates = request.app.state.templates
    state = patient_slice.panel_states["admission"]
    return templates.get_template("_panel_admission.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("admission"),
        sections=admission_sections(_admission_facts(patient_slice)),
    )


def _render_imaging(patient_slice: PatientSlice, request: Request) -> str:
    """S8: perfusion volume table, binary findings, then canonical reports."""
    templates = request.app.state.templates
    state = patient_slice.panel_states["imaging"]
    rows = [
        {
            "t_minutes": float(r.t_minutes),
            "modality": r.modality,
            "report_text": r.report_text,
        }
        for r in patient_slice.imaging.itertuples(index=False)
    ]
    perfusion = patient_slice.scalar_ts.loc[
        patient_slice.scalar_ts.variable.isin(IMAGING_DERIVED_VAR_SET)
    ]
    perfusion_times: list[float] = []
    perfusion_rows: list[dict[str, object]] = []
    if not perfusion.empty:
        perfusion_times, perfusion_rows = _observation_table(
            perfusion, ordered_ids(DisplayGroup.IMAGING)
        )
    return templates.get_template("_panel_imaging.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("imaging"),
        rows=rows,
        perfusion_times=perfusion_times,
        perfusion_rows=perfusion_rows,
        findings=imaging_findings(_admission_facts(patient_slice)),
        current_t=float(patient_slice.t_minutes),
    )


def _render_ai(patient_slice: PatientSlice, request: Request) -> str:
    templates = request.app.state.templates
    state = patient_slice.panel_states["ai"]
    rows: list[dict[str, object]] = []
    for r in patient_slice.ai_output.itertuples(index=False):
        try:
            payload = json.loads(r.output_json)
        except (TypeError, ValueError):
            payload = {}
        rows.append(
            {
                "t_minutes": float(r.t_minutes),
                "model_id": r.model_id,
                "payload": payload,
            }
        )
    return templates.get_template("_panel_ai.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("ai"),
        rows=rows,
    )


def _render_measured_ai(
    patient_slice: PatientSlice, request: Request, *, intervention: InterventionContext
) -> str:
    """S11g AI arm: the pinned model's row at exactly t, or ``unavailable``."""
    measured = select_measured_ai(patient_slice, intervention)
    state, payload = measured_ai_state(measured)
    if measured.unavailable is not None:
        get_logger().warning(
            "measured AI output unavailable",
            event_kind="intervention.ai.unavailable",
            reason=str(measured.unavailable),
        )

    rows = []
    if measured.unavailable is None:
        rows.append(
            {"t_minutes": measured.t_minutes, "model_id": measured.model_id, "payload": payload}
        )
    return request.app.state.templates.get_template("_panel_ai.html").render(
        request=request,
        state=state,
        error=None,
        rows=rows,
        measured=True,
    )
