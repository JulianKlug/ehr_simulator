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
    admission_facts = {
        row.field: row.value for row in patient_slice.admission.itertuples(index=False)
    }
    counts = {
        "scalar_ts": int(len(patient_slice.scalar_ts)),
        "imaging": int(len(patient_slice.imaging)),
        "ai": int(len(patient_slice.ai_output)),
        "admission": int(len(patient_slice.admission)),
    }
    return templates.get_template("_summary_card.html").render(
        request=request,
        patient_slice=patient_slice,
        admission_facts=admission_facts,
        counts=counts,
        chrome=chrome,
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
    """Vitals: one shared-x facet plot of all vital variables (FINDING-003)."""
    from ehr_simulator.web.charts import render_facet_timeline_svg
    from ehr_simulator.web.panels import _VITAL_VARS

    templates = request.app.state.templates
    state = patient_slice.panel_states["vitals"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(_VITAL_VARS)]

    chart_svg: str | None = None
    table_rows: list[dict[str, object]] = []
    variables_present: list[str] = []
    if state in {"loading", "partial"} and not rows.empty:
        variables_present = sorted(rows["variable"].unique().tolist())
        chart_svg = render_facet_timeline_svg(
            rows.sort_values(["variable", "t_minutes"]),
            variables_present,
        )
        table_rows = [
            {
                "t": float(r.t_minutes),
                "variable": r.variable,
                "value": float(r.value),
                "unit": r.unit,
            }
            for r in rows.sort_values(["t_minutes", "variable"]).itertuples(index=False)
        ]

    return templates.get_template("_panel_vitals.html").render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get("vitals"),
        chart_svg=chart_svg,
        variables=variables_present,
        table_rows=table_rows,
    )


def _render_labs(patient_slice: PatientSlice, request: Request) -> str:
    return _render_scalar_panel(patient_slice, request, panel="labs", template="_panel_labs.html")


def _render_scalar_panel(
    patient_slice: PatientSlice,
    request: Request,
    *,
    panel: str,
    template: str,
) -> str:
    from ehr_simulator.web.charts import render_timeline_svg
    from ehr_simulator.web.panels import _LAB_VARS, _VITAL_VARS

    templates = request.app.state.templates
    state = patient_slice.panel_states[panel]
    var_set = _VITAL_VARS if panel == "vitals" else _LAB_VARS
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(var_set)]

    charts: list[dict[str, object]] = []
    if state in {"loading", "partial"} and not rows.empty:
        for variable in sorted(rows["variable"].unique()):
            sub = rows.loc[rows.variable == variable].sort_values("t_minutes")
            charts.append(
                {
                    "variable": variable,
                    "svg": render_timeline_svg(sub, variable),
                    "table_rows": [
                        {"t": float(r.t_minutes), "value": float(r.value), "unit": r.unit}
                        for r in sub.itertuples(index=False)
                    ],
                }
            )

    return templates.get_template(template).render(
        request=request,
        patient_slice=patient_slice,
        state=state,
        error=patient_slice.panel_errors.get(panel),
        charts=charts,
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
