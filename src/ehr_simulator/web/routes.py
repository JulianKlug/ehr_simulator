"""HTTP routes: index + ``/patient/{id}/timepoint/{t}`` + ``/login``/``/logout``.

The HX-Request header switches between the full ``<html>`` document and the
inner partial. Out-of-range / unknown patient renders an HTML error body
shaped for the swap target (Decisions **D6**, **D10**).

Per-panel renderer exceptions are contained inside the route handler
(Decision **D9**): a failed panel renders with the error visual treatment;
the per-request log line stays ``page.render``/``panel.swap``.

S6 protected-route preamble (``_require_clinician``): every clinician-
facing route resolves the cookie against ``app.state.known_clinicians``
(zero-DB-cost cache, review-fix R11) and HTMX-aware-redirects to
``/login`` on miss (review-fix R10).

S9a answer capture: ``POST /patient/{pid}/timepoint/{t}/answer`` shares
``_require_clinician`` + ``_resolve_timepoint`` with the GET route and
always answers with the ``_answer_status.html`` fragment. Both patient
routes are ``async def`` on purpose — the app owns one shared
``sqlite3.Connection`` and event-loop serialization is what keeps its
writes ordered. New code goes routes → ``answer_capture`` /
``study_session`` → DAOs; the S6 ``/login`` DAO calls are left as they are.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ehr_simulator.db import clinicians, cookies, events
from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.web.answer_capture import (
    FREE_TEXT_AUTOSAVE_DELAY_MS,
    FREE_TEXT_MAX_CHARS,
    PROBABILITY_MAX,
    PROBABILITY_MIN,
    AnswerValidationError,
    record_answer,
    saved_answers,
)
from ehr_simulator.web.panels import (
    PatientSlice,
    patient_timepoints,
    slice_to_timepoint,
)
from ehr_simulator.web.study_session import bootstrap_session, read_frontier

router = APIRouter()

_NO_QUESTIONS_MSG = "No questions configured (start with --config/--questions)"
_MISSING_QUESTION_ID_MSG = "Missing question_id"


def _require_clinician(request: Request) -> tuple[str | None, Response | None]:
    """Resolve the clinician cookie or build an HTMX-aware redirect.

    Returns ``(clinician_id, None)`` on success or ``(None, redirect)`` on
    failure. The redirect is HTMX-aware:

    - ``HX-Request: true`` → 200 + ``HX-Redirect: /login`` header (so HTMX
      swaps the full page rather than dropping a 303 into the swap target).
    - else → 303 → ``/login`` (browser follows).

    Cookie validation runs against ``app.state.known_clinicians`` —
    populated at lifespan boot, mutated by :func:`clinicians.lookup_or_create`.
    A tampered cookie (16-hex string not in the set) misses the cache and
    redirects to ``/login`` without touching the DB.
    """
    clinician_id = cookies.read_clinician_id(request)
    known = getattr(request.app.state, "known_clinicians", set())
    if clinician_id is None or clinician_id not in known:
        if request.headers.get("hx-request", "").lower() == "true":
            return None, Response(
                status_code=200,
                headers={"HX-Redirect": "/login"},
            )
        return None, RedirectResponse("/login", status_code=303)
    return clinician_id, None


def _logged_in_name(request: Request) -> str | None:
    """Best-effort display name for the logged-in clinician.

    The cookie carries the pseudonymized ``clinician_id``; the chrome
    stripe wants the case-folded display name. One row lookup per page
    render — pilot scale (≤1000 clinicians per DB) makes the cost
    negligible.
    """
    clinician_id = cookies.read_clinician_id(request)
    if clinician_id is None:
        return None
    db = getattr(request.app.state, "db", None)
    if db is None:
        return None
    row = db.execute(
        "SELECT name_normalized FROM clinicians WHERE clinician_id = ?",
        (clinician_id,),
    ).fetchone()
    if row is None:
        return None
    return row[0]


@dataclass(frozen=True)
class ResolvedTimepoint:
    timepoints: tuple[float, ...]
    t_minutes: float


def _resolve_timepoint(
    request: Request, patient_id: str, t_index: int
) -> tuple[ResolvedTimepoint | None, str | None]:
    """Study-membership → dataset-membership → ``t_index`` range.

    Returns ``(resolved, None)`` or ``(None, message)``. Callers render the
    message in their own shape: GET wraps it in the S2 ``error-flash`` div,
    POST routes it through ``_answer_status.html``.
    """
    dataset = request.app.state.dataset
    study_patient_ids = getattr(request.app.state, "study_patient_ids", None)
    if study_patient_ids is not None and patient_id not in study_patient_ids:
        return None, f"Patient '{patient_id}' is not part of this study"

    known_pids = set(dataset.admission["patient_id"].unique().tolist())
    if patient_id not in known_pids:
        return None, f"Patient '{patient_id}' not found"

    study_tps = getattr(request.app.state, "study_timepoints", None)
    timepoints = (
        tuple(float(t) for t in study_tps)
        if study_tps is not None
        else patient_timepoints(dataset, patient_id)
    )
    if t_index < 0 or t_index >= len(timepoints):
        return None, (f"Timepoint t_index={t_index} out of range (valid: 0…{len(timepoints) - 1})")

    return ResolvedTimepoint(timepoints=timepoints, t_minutes=timepoints[t_index]), None


def _error_flash(message: str) -> str:
    return f'<div class="error-flash" role="alert">{message}</div>'


def _answer_status(
    request: Request,
    *,
    state: str,
    question_id: str | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    """The one response shape of ``POST …/answer``: the badge fragment."""
    html = request.app.state.templates.get_template("_answer_status.html").render(
        request=request, state=state, question_id=question_id, error=error
    )
    return HTMLResponse(content=html, status_code=status_code)


def _render_questions_pane(
    request: Request, *, clinician_id: str, patient_id: str, t_index: int, t_minutes: float
) -> str:
    """Bootstrap the study session, bind ``arm``, render the pre-filled pane.

    Returns ``""`` outside study mode (bare ``serve``): no questions, no pane,
    no session row.
    """
    state = request.app.state
    if state.study is None:
        return ""

    frontier = read_frontier(state.db, state, clinician_id=clinician_id, patient_id=patient_id)
    ctx = bootstrap_session(
        state.db, state, clinician_id=clinician_id, patient_id=patient_id, frontier=frontier
    )
    update_request_context(arm=ctx.arm)
    prefill = saved_answers(
        state.db,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_minutes=t_minutes,
        questions=state.questions,
        config_hash=ctx.config_hash,
    )
    return state.templates.get_template("_questions_pane.html").render(
        request=request,
        patient_id=patient_id,
        t_index=t_index,
        t_minutes=t_minutes,
        questions=state.questions.questions,
        prefill=prefill,
        free_text_max_chars=FREE_TEXT_MAX_CHARS,
        free_text_autosave_delay_ms=FREE_TEXT_AUTOSAVE_DELAY_MS,
        probability_min=PROBABILITY_MIN,
        probability_max=PROBABILITY_MAX,
    )


@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@router.post("/login")
async def login_post(
    request: Request,
    clinician_name: str = Form(""),
) -> Response:
    raw_name = clinician_name.strip()
    if not raw_name:
        templates = request.app.state.templates
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Name required."},
            status_code=400,
        )
    clinician_id = clinicians.lookup_or_create(
        request.app.state.db,
        raw_name,
        known_clinicians=request.app.state.known_clinicians,
    )
    update_request_context(clinician_id=clinician_id)
    events.append(
        request.app.state.db,
        session_id=None,
        clinician_id=clinician_id,
        patient_id=None,
        timepoint=None,
        kind="clinician.login",
        payload={"name_normalized": " ".join(raw_name.casefold().split())},
        app_state=request.app.state,
    )
    response: Response = RedirectResponse("/", status_code=303)
    cookies.set_clinician_cookie(response, clinician_id)
    return response


@router.post("/logout")
async def logout_post(request: Request) -> Response:
    clinician_id = cookies.read_clinician_id(request)
    if clinician_id is not None and clinician_id in getattr(
        request.app.state, "known_clinicians", set()
    ):
        events.append(
            request.app.state.db,
            session_id=None,
            clinician_id=clinician_id,
            patient_id=None,
            timepoint=None,
            kind="clinician.logout",
            payload={},
            app_state=request.app.state,
        )
    response: Response = RedirectResponse("/login", status_code=303)
    cookies.clear_clinician_cookie(response)
    return response


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect  # type: ignore[return-value]
    update_request_context(clinician_id=clinician_id)
    dataset = request.app.state.dataset
    # Study config (when loaded via `serve --config`) is the authoritative
    # patient list — order is preserved, off-study patients are hidden.
    # Without a study config, fall back to the full dataset list (S2 behavior).
    study_patient_ids = getattr(request.app.state, "study_patient_ids", None)
    if study_patient_ids is not None:
        patient_ids = list(study_patient_ids)
    else:
        patient_ids = sorted(dataset.admission["patient_id"].unique().tolist())
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "patient_ids": patient_ids,
            "logged_in_name": _logged_in_name(request),
        },
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
) -> Response:
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)

    dataset = request.app.state.dataset
    templates = request.app.state.templates
    is_htmx = request.headers.get("hx-request", "").lower() == "true"

    resolved, message = _resolve_timepoint(request, patient_id, t_index)
    if resolved is None:
        return HTMLResponse(content=_error_flash(message or ""), status_code=404)

    t_minutes = resolved.t_minutes
    update_request_context(
        patient_id=patient_id,
        timepoint=float(t_minutes),
        timepoint_index=t_index,
        chrome=chrome,
    )

    patient_slice = slice_to_timepoint(dataset, patient_id, t_minutes, t_index)
    panels_html = _render_panels(patient_slice, request)
    summary_html = _render_summary(patient_slice, request, chrome=chrome)

    logged_in_name = _logged_in_name(request)
    template_name = "_chrome_dense.html" if chrome == "dense" else "_chrome_epic.html"
    chrome_html = templates.get_template(template_name).render(
        request=request,
        patient_slice=patient_slice,
        panels=panels_html,
        chrome=chrome,
        logged_in_name=logged_in_name,
    )

    questions_html = _render_questions_pane(
        request,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        t_index=t_index,
        t_minutes=float(t_minutes),
    )

    inner = templates.get_template("_patient_view.html").render(
        request=request,
        patient_slice=patient_slice,
        chrome=chrome,
        chrome_html=chrome_html,
        summary_html=summary_html,
        questions_html=questions_html,
        logged_in_name=logged_in_name,
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
            "logged_in_name": logged_in_name,
        },
    )


@router.post(
    "/patient/{patient_id}/timepoint/{t_index}/answer",
    response_class=HTMLResponse,
)
async def patient_answer(request: Request, patient_id: str, t_index: int) -> Response:
    """Auto-save one answer; always reply with the badge fragment."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)

    state = request.app.state
    if state.questions is None:
        return _answer_status(
            request,
            state="error",
            error=_NO_QUESTIONS_MSG,
            status_code=status.HTTP_409_CONFLICT,
        )

    resolved, message = _resolve_timepoint(request, patient_id, t_index)
    if resolved is None:
        return _answer_status(
            request, state="error", error=message, status_code=status.HTTP_404_NOT_FOUND
        )

    form = await request.form()
    # First wins if a malformed client sends question_id twice (FormData.get
    # would return the last one).
    question_ids = [v for v in form.getlist("question_id") if isinstance(v, str)]
    question_id = question_ids[0].strip() if question_ids else ""
    if not question_id:
        return _answer_status(
            request,
            state="error",
            error=_MISSING_QUESTION_ID_MSG,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )
    question = next((q for q in state.questions.questions if q.question_id == question_id), None)
    if question is None:
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=f"Unknown question '{question_id}'",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    update_request_context(
        patient_id=patient_id, timepoint=float(resolved.t_minutes), timepoint_index=t_index
    )
    frontier = read_frontier(
        state.db, state, clinician_id=clinician_id or "", patient_id=patient_id
    )
    ctx = bootstrap_session(
        state.db, state, clinician_id=clinician_id or "", patient_id=patient_id, frontier=frontier
    )
    update_request_context(arm=ctx.arm)

    raw_values = [v for v in form.getlist("value") if isinstance(v, str)]
    try:
        outcome = record_answer(
            state.db,
            state,
            ctx=ctx,
            clinician_id=clinician_id or "",
            patient_id=patient_id,
            t_minutes=float(resolved.t_minutes),
            question=question,
            raw_values=raw_values,
            client_ts=_form_str(form.get("client_ts")),
            client_seq=_form_str(form.get("client_seq")),
        )
    except AnswerValidationError as exc:
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=str(exc),
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    return _answer_status(request, state=outcome, question_id=question_id)


def _form_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    # Mirror the index-route filter: with a study config loaded, the
    # patient-jumper navigation only lists study patients (declared order
    # preserved). Without a study config, fall back to the full dataset list.
    study_patient_ids = getattr(request.app.state, "study_patient_ids", None)
    if study_patient_ids is not None:
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


# Round-03 layout: BP grouped on shared mmHg, then HR, RR; SpO2/Temp below.
# Order is fixed (clinician reading order); only present vitals get rendered,
# but the order they appear within each figure stays stable.
_VITALS_TABLE_ORDER: tuple[str, ...] = ("sbp", "dbp", "hr", "rr", "spo2", "temp")
_BP_PANEL_VARS: frozenset[str] = frozenset({"sbp", "dbp"})
_BP_GROUP_VARS_ORDERED: tuple[str, ...] = ("sbp", "dbp")
_UPPER_SINGLE_VARS: tuple[str, ...] = ("hr", "rr")
_LOWER_SINGLE_VARS: tuple[str, ...] = ("spo2", "temp")


def _render_vitals(patient_slice: PatientSlice, request: Request) -> str:
    """Vitals: two stacked figures.

    Round-03 layout (`specs/feedback/session-02-feedback.md` round-03):

    - **Upper figure** (hemodynamics): BP grouped on a shared mmHg y-scale,
      then HR, then RR — top to bottom.
    - **Lower figure** (oxygenation/metabolic): SpO₂, then Temp.
    - All panels in a figure share the same x-range; tick labels render on
      the bottom-most panel only.
    - Partial-within-BP: if SBP or DBP is missing from the slice, the BP
      panel renders a faint dashed expected band at the missing variable's
      reference range and the panel-level note "DBP missing at this
      timepoint." appears below the BP panel.

    Round-02 lineage (FINDING-007 / FINDING-008): per-variable rendering
    instead of ``facet_wrap`` because plotnine's facet strip text rendered
    as empty grey rectangles. Round-03 keeps that pattern for HR/RR/SpO₂/
    Temp and adds a single multi-line plotnine call for the BP panel.
    """
    from ehr_simulator.web.charts import render_grouped_bp_svg, render_timeline_svg
    from ehr_simulator.web.panels import _VITAL_VARS

    templates = request.app.state.templates
    state = patient_slice.panel_states["vitals"]
    rows = patient_slice.scalar_ts.loc[patient_slice.scalar_ts.variable.isin(_VITAL_VARS)]

    upper_panels: list[dict[str, object]] = []
    lower_panels: list[dict[str, object]] = []
    fallback_rows: list[dict[str, object]] = []
    variables_present: list[str] = []
    units: dict[str, str] = {}
    pivot_rows: list[dict[str, object]] = []
    bp_missing: list[str] = []
    bp_partial_note: str | None = None
    current_t = float(patient_slice.t_minutes)

    if state in {"loading", "partial"} and not rows.empty:
        present_set = set(rows["variable"].astype(str).unique().tolist())
        for r in rows.itertuples(index=False):
            units.setdefault(r.variable, str(r.unit))
        # Stable column order for the values table (matches the panel reading
        # order: BP → HR → RR → SpO₂ → Temp).
        variables_present = [v for v in _VITALS_TABLE_ORDER if v in present_set]

        all_t = rows["t_minutes"].astype(float)
        t_lo = float(all_t.min())
        t_hi = float(all_t.max())
        x_range = (t_lo - 1.0, t_hi + 1.0) if t_lo == t_hi else (t_lo, t_hi)
        sorted_rows = rows.sort_values(["variable", "t_minutes"])

        present_bp: frozenset[str] = frozenset(present_set & _BP_PANEL_VARS)
        bp_missing = [v for v in _BP_GROUP_VARS_ORDERED if v not in present_bp]

        # Compose upper figure (BP → HR → RR), then lower (SpO₂ → Temp).
        upper_specs: list[dict[str, object]] = []
        if present_bp:
            upper_specs.append(
                {
                    "group": "bp",
                    "label": "BP",
                    "unit": "mmHg",
                    "is_grouped": True,
                    "present_bp": present_bp,
                    "missing": list(bp_missing),
                }
            )
        for var in _UPPER_SINGLE_VARS:
            if var in present_set:
                upper_specs.append(
                    {
                        "group": var,
                        "label": var.upper(),
                        "unit": units.get(var, ""),
                        "is_grouped": False,
                        "variable": var,
                    }
                )
        lower_specs: list[dict[str, object]] = []
        for var in _LOWER_SINGLE_VARS:
            if var in present_set:
                lower_specs.append(
                    {
                        "group": var,
                        "label": "SpO₂" if var == "spo2" else var.upper(),
                        "unit": units.get(var, ""),
                        "is_grouped": False,
                        "variable": var,
                    }
                )

        def _render_specs(
            specs: list[dict[str, object]],
        ) -> list[dict[str, object]]:
            rendered: list[dict[str, object]] = []
            for idx, spec in enumerate(specs):
                is_bottom = idx == len(specs) - 1
                if spec["is_grouped"]:
                    svg = render_grouped_bp_svg(
                        sorted_rows,
                        present_vars=spec["present_bp"],  # type: ignore[arg-type]
                        x_range=x_range,
                        is_bottom=is_bottom,
                    )
                else:
                    svg = render_timeline_svg(
                        sorted_rows,
                        spec["variable"],  # type: ignore[arg-type]
                        x_range=x_range,
                        is_bottom=is_bottom,
                    )
                rendered.append(
                    {
                        "group": spec["group"],
                        "label": spec["label"],
                        "unit": spec["unit"],
                        "svg": svg,
                        "is_bottom": is_bottom,
                        "is_grouped": spec["is_grouped"],
                        "missing": spec.get("missing", []),
                    }
                )
            return rendered

        upper_panels = _render_specs(upper_specs)
        lower_panels = _render_specs(lower_specs)

        if bp_missing and present_bp:
            # Specific to round-03: the BP panel internally annotates which
            # of SBP/DBP is missing, layered on top of the panel-level
            # "Partial data at this timepoint." badge.
            missing_label = ", ".join(v.upper() for v in bp_missing)
            bp_partial_note = f"{missing_label} missing at this timepoint."

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
        upper_panels=upper_panels,
        lower_panels=lower_panels,
        variables=variables_present,
        units=units,
        pivot_rows=pivot_rows,
        fallback_rows=fallback_rows,
        bp_partial_note=bp_partial_note,
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
