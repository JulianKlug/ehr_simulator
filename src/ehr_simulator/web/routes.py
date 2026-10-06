"""HTTP routes: index + ``/patient/{id}/timepoint/{t}`` (GET, ``/answer``,
``/advance``); the other endpoints live in their own routers.

Module layout (``router`` here includes the two sub-routers; ``app.py``
includes ``router`` only)::

    app.py ──► routes.router ─┬─ GET  /                                  index
                              ├─ GET  /patient/{pid}/timepoint/{t}       view
                              ├─ POST /patient/{pid}/timepoint/{t}/answer
                              ├─ POST /patient/{pid}/timepoint/{t}/advance
                              ├─ auth_routes.router   /login /logout /profile
                              └─ case_routes.router   /case/start /practice/start
                                                      /case/{pid}/heartbeat|pause|resume
                                                      /case/{pid}/tab/claim|release
                                                      /telemetry/events
    helpers (no routes):
      case_request   the case preamble (cookie → unactivated → case → contact
                     → tab lease → timepoint), refusals per RefusalShape
      patient_view   view composition, questions pane, advance response
      panel_render   panels + summary card (templates + charts, no DB)
      route_support  HTMX/redirect/flash helpers, cookie check, display name
    services below: study_session, case_start, case_contact, tab_guard,
      gating, answer_capture, clinician_session, telemetry → db DAOs

The HX-Request header switches between the full ``<html>`` document and the
inner partial. Out-of-range / unknown patient renders an HTML error body
shaped for the swap target (Decisions **D6**, **D10**).

Both patient POST routes are ``async def`` on purpose — the app owns one
shared ``sqlite3.Connection`` and event-loop serialization is what keeps
its writes ordered; their only await (the form read) precedes the case
checks.

S9b gating: the GET route reads the walk frontier (``read_frontier``, a pure
read) and bounces any ``t_index`` past it **before** slicing or writing
anything; ``POST …/advance`` is the one forward path; ``POST …/answer``
refuses timepoints that are not the open one. HTMX partials carry
``HX-Push-Url`` so the address bar tracks the timepoint; a history-restore
request gets the full document back.

S11i backward navigation follows the case's pinned policy: ``prohibit``
bounces any non-frontier GET like the forward gate; ``allow_readonly``
renders it read-only, marked ``data-visit-kind="revisit"``, and records one
``timepoint.revisit``.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse

from ehr_simulator.db.exceptions import CaseLifecycleError, ConfigurationProvenanceError
from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.web import auth_routes, case_routes, tab_guard
from ehr_simulator.web.answer_capture import (
    AnswerValidationError,
    QuestionNotEditableError,
    record_answer,
    saved_answers,
)
from ehr_simulator.web.case_request import (
    RefusalShape,
    _backward_policy,
    _case_request,
    _touch_contact,
)
from ehr_simulator.web.case_start import case_states, index_state, replacement_markers
from ehr_simulator.web.gating import (
    PatientProgress,
    advance,
    completeness,
    is_viewable,
    pane_mode,
    progress_overview,
)
from ehr_simulator.web.patient_view import (
    _advance_response,
    _case_patient_ids,
    _full_document,
    _record_view,
    _render_advance_cta,
    _render_changed_slots,
    _render_patient_view,
    _study_bootstrap,
)
from ehr_simulator.web.practice_start import practice_index_state
from ehr_simulator.web.route_support import (
    Chrome,
    _answer_status,
    _error_flash,
    _form_str,
    _htmx_aware_redirect,
    _is_history_restore,
    _is_htmx,
    _logged_in_name,
    _require_clinician,
    _timepoint_url,
)
from ehr_simulator.web.study_session import (
    SessionContext,
    is_phase2_mode,
    pinned_timepoint_counts,
    read_frontier,
)

router = APIRouter()
router.include_router(auth_routes.router)
router.include_router(case_routes.router)

_MISSING_QUESTION_ID_MSG = "Missing question_id"
_TIMEPOINT_LOCKED_MSG = "Timepoint locked"


async def provenance_error_response(
    request: Request, exc: ConfigurationProvenanceError
) -> HTMLResponse:
    """App-wide handler: a case row pinned to another configuration is an
    integrity error — refuse with 500, never a crash or a silent fallback.
    """
    get_logger().error(
        "case provenance mismatch; request refused",
        event_kind="case.provenance.refused",
        path=request.url.path,
        error=str(exc),
    )
    return HTMLResponse(
        content=_error_flash(str(exc)), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
    )


async def lifecycle_error_response(request: Request, exc: CaseLifecycleError) -> HTMLResponse:
    """App-wide handler: a lifecycle transition that lost a race is a 409, never a 500."""
    get_logger().warning(
        "case lifecycle transition refused",
        event_kind="case.lifecycle.refused",
        path=request.url.path,
        error=str(exc),
    )
    return HTMLResponse(content=_error_flash(str(exc)), status_code=status.HTTP_409_CONFLICT)


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect  # type: ignore[return-value]
    update_request_context(clinician_id=clinician_id)
    state = request.app.state
    dataset = state.dataset
    # Study config (when loaded via `serve --config`) is the authoritative
    # patient list — order is preserved, off-study patients are hidden.
    # Without a study config, fall back to the full dataset list (S2 behavior).
    study_patient_ids = getattr(state, "study_patient_ids", None)
    patient_progress: dict[str, PatientProgress] | None = None
    case_state = index_state(state.db, state, clinician_id or "") if is_phase2_mode(state) else None
    practice_state = practice_index_state(state.db, state, clinician_id or "")
    lifecycle_states: dict[str, str] = {}
    replacements: dict[str, str] = {}
    if case_state is not None:
        lifecycle_states = case_states(state.db, clinician_id or "")
        replacements = replacement_markers(state.db, clinician_id or "")
    if study_patient_ids is not None:
        patient_ids = _case_patient_ids(request, clinician_id or "")
        patient_progress = progress_overview(
            state.db,
            clinician_id=clinician_id or "",
            patient_ids=patient_ids,
            timepoint_count=len(state.study_timepoints),
            timepoint_counts=pinned_timepoint_counts(
                state.db, clinician_id=clinician_id or "", patient_ids=patient_ids
            ),
        )
    else:
        patient_ids = sorted(dataset.admission["patient_id"].unique().tolist())
    return state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "patient_ids": patient_ids,
            "patient_progress": patient_progress,
            "case_state": case_state,
            "practice_state": practice_state,
            "lifecycle_states": lifecycle_states,
            "replacements": replacements,
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
    chrome: Chrome = "epic",
) -> Response:
    prepared = await _case_request(request, patient_id, t_index, RefusalShape.PAGE)
    if isinstance(prepared, Response):
        return prepared
    clinician_id, case, contact, resolved = (
        prepared.clinician_id,
        prepared.case,
        prepared.contact,
        prepared.resolved,
    )
    state = request.app.state

    # Bound before the gate so a redirected request stays attributable.
    update_request_context(
        patient_id=patient_id,
        timepoint=float(resolved.t_minutes),
        timepoint_index=t_index,
        chrome=chrome,
    )

    ctx: SessionContext | None = None
    if state.study is not None:
        # The gate decides on a pure read: nothing below may run for a
        # request we are about to bounce (no slice, no arm lock, no session).
        frontier = read_frontier(
            state.db,
            state,
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoints=case.timepoints if case is not None else None,
        )
        if not is_viewable(frontier, t_index, _backward_policy(case)):
            get_logger().warning(
                "timepoint beyond the frontier; redirecting",
                event_kind="gate.redirect",
                requested_t_index=t_index,
                unlocked_t_index=frontier.unlocked_t_index,
            )
            return _htmx_aware_redirect(
                request, _timepoint_url(patient_id, frontier.unlocked_t_index, chrome)
            )
        ctx = _study_bootstrap(
            request,
            clinician_id=clinician_id,
            patient_id=patient_id,
            frontier=frontier,
            case=case,
        )

    view = await _render_patient_view(
        request,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        resolved=resolved,
        ctx=ctx,
        case=case,
        contact=contact,
    )
    inner = view.html
    # Build/render the complete response before recording timepoint.enter.
    if _is_history_restore(request) or not _is_htmx(request):
        response = _full_document(
            request,
            inner=inner,
            patient_id=patient_id,
            t_index=t_index,
            chrome=chrome,
        )
    else:
        response = HTMLResponse(
            content=inner,
            status_code=status.HTTP_200_OK,
            headers={"HX-Push-Url": _timepoint_url(patient_id, t_index, chrome)},
        )

    # Only a successfully rendered editable frontier counts as an enter;
    # any other study render is a read-only revisit (S11i marker only).
    if state.study is not None and ctx is not None:
        _record_view(request, view=view, ctx=ctx, clinician_id=clinician_id, patient_id=patient_id)

    _touch_contact(request, contact)
    return response


@router.post(
    "/patient/{patient_id}/timepoint/{t_index}/answer",
    response_class=HTMLResponse,
)
async def patient_answer(
    request: Request, patient_id: str, t_index: int, chrome: Chrome = "epic"
) -> Response:
    """Auto-save one answer; always reply with the badge fragment."""
    prepared = await _case_request(request, patient_id, t_index, RefusalShape.ANSWER_BADGE)
    if isinstance(prepared, Response):
        return prepared
    clinician_id, case, contact, resolved, form = (
        prepared.clinician_id,
        prepared.case,
        prepared.contact,
        prepared.resolved,
        prepared.form,
    )
    state = request.app.state
    questions = case.questions if case is not None else state.questions

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
    question = next((q for q in questions.questions if q.question_id == question_id), None)
    if question is None:
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=f"Unknown question '{question_id}'",
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    t_minutes = float(resolved.t_minutes)
    update_request_context(patient_id=patient_id, timepoint=t_minutes, timepoint_index=t_index)
    frontier = read_frontier(
        state.db,
        state,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoints=case.timepoints if case is not None else None,
    )
    ctx = _study_bootstrap(
        request,
        clinician_id=clinician_id,
        patient_id=patient_id,
        frontier=frontier,
        case=case,
    )

    # Only the open timepoint takes answers; disabled fieldsets are a courtesy.
    if pane_mode(ctx.frontier, t_index) != "open":
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=_TIMEPOINT_LOCKED_MSG,
            status_code=status.HTTP_409_CONFLICT,
        )

    raw_values = [v for v in form.getlist("value") if isinstance(v, str)]
    try:
        recorded = record_answer(
            state.db,
            state,
            ctx=ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            t_minutes=t_minutes,
            questions=questions,
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
    except QuestionNotEditableError as exc:
        # S11h: hidden or derived under the current branch — nothing written.
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=str(exc),
            status_code=status.HTTP_409_CONFLICT,
        )
    except ConfigurationProvenanceError as exc:
        # The stored answer is pinned to another configuration: integrity error.
        return _answer_status(
            request,
            state="error",
            question_id=question_id,
            error=str(exc),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    # The CTA rides out-of-band so its remaining-count is always the server's.
    saved = saved_answers(
        state.db,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_minutes=t_minutes,
        questions=questions,
        config_hash=ctx.config_hash,
        config_version=ctx.config_version,
    )
    cta_html = _render_advance_cta(
        request,
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        remaining=completeness(questions, saved).remaining,
        is_last=t_index == len(resolved.timepoints) - 1,
        oob=True,
    )
    slots_html = _render_changed_slots(
        request,
        questions=questions,
        saved=saved,
        changed=recorded.changed_question_ids,
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
    )
    _touch_contact(request, contact)
    return _answer_status(
        request,
        state=recorded.outcome,
        question_id=question_id,
        trailing_html=cta_html + slots_html,
    )


@router.post(
    "/patient/{patient_id}/timepoint/{t_index}/advance",
    response_class=HTMLResponse,
)
async def patient_advance(
    request: Request, patient_id: str, t_index: int, chrome: Chrome = "epic"
) -> Response:
    """Leave ``t_index``: unlock the next timepoint, or explain why not.

    ``t_index`` is the client's optimistic "where I am"; a mismatch with the
    stored frontier is answered with the frontier's view (412), never with a
    write. Plain-browser (non-HTMX) submits get POST-redirect-GET 303s.
    """
    prepared = await _case_request(request, patient_id, t_index, RefusalShape.ADVANCE_FLASH)
    if isinstance(prepared, Response):
        return prepared
    clinician_id, case, contact, resolved, form = (
        prepared.clinician_id,
        prepared.case,
        prepared.contact,
        prepared.resolved,
        prepared.form,
    )
    state = request.app.state
    questions = case.questions if case is not None else state.questions
    update_request_context(
        patient_id=patient_id,
        timepoint=float(resolved.t_minutes),
        timepoint_index=t_index,
        chrome=chrome,
    )

    frontier = read_frontier(
        state.db,
        state,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoints=case.timepoints if case is not None else None,
    )
    ctx = _study_bootstrap(
        request,
        clinician_id=clinician_id,
        patient_id=patient_id,
        frontier=frontier,
        case=case,
    )

    result = advance(
        state.db,
        state,
        ctx=ctx,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=t_index,
        timepoints=resolved.timepoints,
        questions=questions,
        client_ts=_form_str(form.get("client_ts")),
        client_seq=_form_str(form.get("client_seq")),
    )
    # A finished walk completed the case; touch() then finds nothing active.
    _touch_contact(request, contact)
    return await _advance_response(
        request,
        result=result,
        ctx=ctx,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        timepoint_count=len(resolved.timepoints),
        case=case,
        contact=contact,
        tab_id=request.headers.get(tab_guard.TAB_ID_HEADER),
    )
