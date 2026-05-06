"""HTTP routes: index + ``/patient/{id}/timepoint/{t}``.

The HX-Request header switches between the full ``<html>`` document and the
inner partial. Out-of-range / unknown patient renders an HTML error body
shaped for the swap target (Decisions **D6**, **D10**).

Per-panel renderer exceptions are contained inside the route handler
(Decision **D9**): a failed panel renders with the error visual treatment;
the per-request log line stays ``page.render``/``panel.swap``.
"""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.web.panels import (
    PatientSlice,
    patient_timepoints,
    slice_to_timepoint,
)

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    dataset = request.app.state.dataset
    patient_ids = sorted(dataset.admission["patient_id"].unique().tolist())
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        {"patient_ids": patient_ids},
    )


@router.get(
    "/patient/{patient_id}/timepoint/{t_index}",
    response_class=HTMLResponse,
)
async def patient_timepoint(
    request: Request,
    patient_id: str,
    t_index: int,
    chrome: Literal["dense", "epic"] = "epic",
) -> HTMLResponse:
    dataset = request.app.state.dataset
    templates = request.app.state.templates
    is_htmx = request.headers.get("hx-request", "").lower() == "true"

    known_pids = set(dataset.admission["patient_id"].unique().tolist())
    if patient_id not in known_pids:
        body = f'<div class="error-flash" role="alert">Patient \'{patient_id}\' not found</div>'
        return HTMLResponse(content=body, status_code=404)

    timepoints = patient_timepoints(dataset, patient_id)
    if t_index < 0 or t_index >= len(timepoints):
        body = (
            f'<div class="error-flash" role="alert">'
            f"Timepoint t_index={t_index} out of range "
            f"(valid: 0…{len(timepoints) - 1})"
            f"</div>"
        )
        return HTMLResponse(content=body, status_code=404)

    t_minutes = timepoints[t_index]
    update_request_context(
        patient_id=patient_id,
        timepoint=float(t_minutes),
        timepoint_index=t_index,
        chrome=chrome,
    )

    patient_slice = slice_to_timepoint(dataset, patient_id, t_minutes, t_index)
    panels_html = _render_panels(patient_slice, request)
    summary_html = _render_summary(patient_slice, request, chrome=chrome)

    template_name = "_chrome_dense.html" if chrome == "dense" else "_chrome_epic.html"
    chrome_html = templates.get_template(template_name).render(
        request=request,
        patient_slice=patient_slice,
        panels=panels_html,
        chrome=chrome,
    )

    inner = templates.get_template("_patient_view.html").render(
        request=request,
        patient_slice=patient_slice,
        chrome=chrome,
        chrome_html=chrome_html,
        summary_html=summary_html,
    )
    if is_htmx:
        return HTMLResponse(content=inner, status_code=200)
    return templates.TemplateResponse(
        request,
        "base.html",
        {
            "patient_slice": patient_slice,
            "chrome": chrome,
            "inner": inner,
            "patient_id": patient_id,
            "t_index": t_index,
        },
    )


def _render_summary(patient_slice: PatientSlice, request: Request, *, chrome: str) -> str:
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
    all_patient_ids = sorted(dataset.admission["patient_id"].unique().tolist())
    return templates.get_template("_summary_card.html").render(
        request=request,
        patient_slice=patient_slice,
        admission_facts=admission_facts,
        counts=counts,
        chrome=chrome,
        all_patient_ids=all_patient_ids,
    )


def _render_panels(patient_slice: PatientSlice, request: Request) -> dict[str, str]:
    """Render each panel inside its own try/except so a failure in one panel
    cannot take down the whole page (Decision **D9**)."""

    log = get_logger()
    out: dict[str, str] = {}
    for panel_name, render_fn in (
        ("vitals", _render_vitals),
        ("labs", _render_labs),
        ("admission", _render_admission),
        ("imaging", _render_imaging),
        ("ai", _render_ai),
    ):
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
            out[panel_name] = templates.get_template("_panel_error.html").render(
                request=request,
                panel=panel_name,
                error=repr(exc),
            )
    return out


def _render_vitals(patient_slice: PatientSlice, request: Request) -> str:
    """Vitals: one labeled mini-chart per variable on a shared x-axis +
    a wide-format values table.

    Round-02 FINDING-007 (unreadable plot) and FINDING-008 (single-timestep
    empty plot): per-variable rendering replaces ``facet_wrap``. plotnine's
    facet strip text was rendering as empty grey rectangles in the SVG
    output, which left clinicians staring at five unlabeled rows. Stacking
    individually-labeled mini-charts lets us caption each row in HTML and
    pin the same x-range across them so the time axis still lines up.
    """
    from ehr_simulator.web.charts import render_timeline_svg
    from ehr_simulator.web.panels import _VITAL_VARS

    templates = request.app.state.templates
    state = patient_slice.panel_states["vitals"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(_VITAL_VARS)]

    chart_panels: list[dict[str, object]] = []
    fallback_rows: list[dict[str, object]] = []
    variables_present: list[str] = []
    units: dict[str, str] = {}
    pivot_rows: list[dict[str, object]] = []
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        variables_present = sorted(rows["variable"].unique().tolist())
        for r in rows.itertuples(index=False):
            units.setdefault(r.variable, str(r.unit))
        all_t = rows["t_minutes"].astype(float)
        t_lo = float(all_t.min())
        t_hi = float(all_t.max())
        # Single-timepoint frames: pad the x-range so plotnine doesn't
        # collapse to a degenerate axis.
        x_range = (t_lo - 1.0, t_hi + 1.0) if t_lo == t_hi else (t_lo, t_hi)
        sorted_rows = rows.sort_values(["variable", "t_minutes"])
        for var in variables_present:
            chart_panels.append(
                {
                    "variable": var,
                    "unit": units.get(var, ""),
                    "svg": render_timeline_svg(sorted_rows, var, x_range=x_range),
                }
            )
        fallback_rows = [
            {
                "t": float(r.t_minutes),
                "variable": r.variable,
                "value": float(r.value),
                "unit": r.unit,
            }
            for r in rows.sort_values(["t_minutes", "variable"]).itertuples(index=False)
        ]
        pivot: dict[float, dict[str, float]] = {}
        for r in rows.itertuples(index=False):
            pivot.setdefault(float(r.t_minutes), {})[r.variable] = float(r.value)
        for t in sorted(pivot.keys()):
            pivot_rows.append(
                {
                    "t": t,
                    "is_current": t == current_t,
                    "cells": [pivot[t].get(var) for var in variables_present],
                }
            )

    return templates.get_template("_panel_vitals.html").render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get("vitals"),
        chart_panels=chart_panels,
        variables=variables_present,
        units=units,
        pivot_rows=pivot_rows,
        fallback_rows=fallback_rows,
    )


def _render_labs(patient_slice: PatientSlice, request: Request) -> str:
    """Labs: variable-by-timepoint table (FINDING-005). Tabular form is the
    clinical standard for labs; charts add visual noise without aiding the
    point-in-time read."""
    from ehr_simulator.web.panels import _LAB_VARS

    templates = request.app.state.templates
    state = patient_slice.panel_states["labs"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(_LAB_VARS)]

    timepoints: list[float] = []
    variables_present: list[str] = []
    units: dict[str, str] = {}
    table_rows: list[dict[str, object]] = []
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        timepoints = sorted({float(t) for t in rows["t_minutes"].tolist()})
        variables_present = sorted(rows["variable"].unique().tolist())
        for r in rows.itertuples(index=False):
            units.setdefault(r.variable, str(r.unit))
        pivot: dict[str, dict[float, float]] = {}
        for r in rows.itertuples(index=False):
            pivot.setdefault(r.variable, {})[float(r.t_minutes)] = float(r.value)
        for variable in variables_present:
            table_rows.append(
                {
                    "variable": variable,
                    "unit": units.get(variable, ""),
                    "cells": [pivot[variable].get(t) for t in timepoints],
                }
            )

    return templates.get_template("_panel_labs.html").render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get("labs"),
        timepoints=timepoints,
        current_t=current_t,
        table_rows=table_rows,
    )


def _render_admission(patient_slice: PatientSlice, request: Request) -> str:
    templates = request.app.state.templates
    state = patient_slice.panel_states["admission"]
    facts = [
        {"field": row.field, "value": row.value}
        for row in patient_slice.admission.itertuples(index=False)
    ]
    return templates.get_template("_panel_admission.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("admission"),
        facts=facts,
    )


def _render_imaging(patient_slice: PatientSlice, request: Request) -> str:
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
    return templates.get_template("_panel_imaging.html").render(
        request=request,
        state=state,
        error=patient_slice.panel_errors.get("imaging"),
        rows=rows,
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
