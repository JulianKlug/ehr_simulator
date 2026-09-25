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

S9b gating: the GET route reads the walk frontier (``read_frontier``, a pure
read) and bounces any ``t_index`` past it **before** slicing or writing
anything; ``POST …/advance`` is the one forward path; ``POST …/answer``
refuses timepoints that are not the open one. HTMX partials carry
``HX-Push-Url`` so the address bar tracks the timepoint; a history-restore
request gets the full document back.

S11d Phase 2 study mode: ``POST /case/start`` is the only way a case begins.
A patient without an activated assignment is bounced to the index (GET) or
refused with 409 (answer/advance) before anything is resolved or written;
the index and jumper list only the clinician's cases.

S11e lifecycle: every case route runs ``case_contact.check`` right after the
case resolves — a timed-out case is made incomplete there (one commit) and
the request is refused; a paused case renders only the Resume interstitial
and refuses answer/advance. A successful contact touches ``last_seen_at``.
``POST /case/{pid}/heartbeat|pause|resume`` are the lifecycle endpoints.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Literal

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ehr_simulator.db import arm_assignments, clinicians, cookies, events
from ehr_simulator.db.exceptions import (
    CaseLifecycleError,
    ConfigurationProvenanceError,
    RandomisationIntegrityError,
    StaleConfigurationError,
)
from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.randomisation import ScheduleIncompatibleError
from ehr_simulator.web import case_contact
from ehr_simulator.web.answer_capture import (
    FREE_TEXT_AUTOSAVE_DELAY_MS,
    FREE_TEXT_MAX_CHARS,
    PROBABILITY_MAX,
    PROBABILITY_MIN,
    AnswerValidationError,
    record_answer,
    saved_answers,
)
from ehr_simulator.web.case_contact import CaseAccess, ContactResult
from ehr_simulator.web.case_start import (
    CaseStartRefusedError,
    case_patient_ids,
    case_states,
    index_state,
    start_next_case,
)
from ehr_simulator.web.gating import (
    AdvanceResult,
    PatientProgress,
    advance,
    completeness,
    is_viewable,
    pane_mode,
    progress_overview,
)
from ehr_simulator.web.panels import (
    PatientSlice,
    patient_timepoints,
    slice_to_timepoint,
)
from ehr_simulator.web.study_session import (
    CaseConfiguration,
    CaseNotActivatedError,
    Frontier,
    SessionContext,
    bootstrap_session,
    is_phase2_mode,
    read_frontier,
    resolve_case_configuration,
)
from ehr_simulator.web.timing_events import record_enter

router = APIRouter()

_NO_QUESTIONS_MSG = "No questions configured (start with --config/--questions)"
_MISSING_QUESTION_ID_MSG = "Missing question_id"
_TIMEPOINT_LOCKED_MSG = "Timepoint locked"
_NOT_ACTIVATED_MSG = "This patient is not an activated case; start a case from the study index"
_CASE_START_INTEGRITY_MSG = "Start case refused: stored allocation integrity check failed"
_CASE_PAUSED_MSG = "This case is paused; resume it from the study index"
_CASE_CLOSED_MSG = "This case is closed and accepts no further answers"
_CASE_NOT_ACTIVE_MSG = "This case is not active"
_CASE_NOT_PAUSED_MSG = "This case is not paused"
_PAUSE_DISABLED_MSG = "Pausing is not enabled for this study"
_HX_REQUEST_HEADER = "hx-request"
_HX_HISTORY_RESTORE_HEADER = "hx-history-restore-request"
_INDEX_URL = "/"

Chrome = Literal["dense", "epic"]


def _is_htmx(request: Request) -> bool:
    return request.headers.get(_HX_REQUEST_HEADER, "").lower() == "true"


def _is_history_restore(request: Request) -> bool:
    return request.headers.get(_HX_HISTORY_RESTORE_HEADER, "").lower() == "true"


def _timepoint_url(patient_id: str, t_index: int, chrome: str) -> str:
    return f"/patient/{patient_id}/timepoint/{t_index}?chrome={chrome}"


def _try_resolve_case(
    request: Request, clinician_id: str, patient_id: str
) -> tuple[CaseConfiguration | None, str | None, int]:
    """Resolve the pinned case, or the active snapshot for a new case (S11b).

    Non-study mode resolves to ``None``. Returns ``(case, message, status)``:
    a message means the route must refuse — ``StaleConfigurationError`` is a
    409 (restart required), a provenance mismatch is an integrity error (500).
    """
    state = request.app.state
    if state.study is None:
        return None, None, 200
    try:
        case = resolve_case_configuration(
            state.db, state, clinician_id=clinician_id, patient_id=patient_id
        )
    except (StaleConfigurationError, CaseNotActivatedError) as exc:
        return None, str(exc), status.HTTP_409_CONFLICT
    except ConfigurationProvenanceError as exc:
        return None, str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR
    return case, None, 200


def _is_unactivated_phase2_patient(request: Request, clinician_id: str, patient_id: str) -> bool:
    """Phase 2 pair with no assignment: not a case, nothing may be resolved or written."""
    state = request.app.state
    if not is_phase2_mode(state):
        return False

    return arm_assignments.fetch_for_pair(state.db, clinician_id, patient_id) is None


def _case_patient_ids(request: Request, clinician_id: str) -> list[str]:
    """Index + jumper list: Phase 2 shows only the clinician's cases (S11d)."""
    state = request.app.state
    if is_phase2_mode(state):
        return case_patient_ids(state.db, clinician_id)

    return _transitional_patient_ids(request, clinician_id)


def _transitional_patient_ids(request: Request, clinician_id: str) -> list[str]:
    """S11b transitional index: active-config patients in configured order, then

    this clinician's already-assigned patients that the active version no
    longer lists (existing cases stay reachable).
    """
    state = request.app.state
    active = list(getattr(state, "study_patient_ids", None) or [])
    seen = set(active)
    if clinician_id:
        for assignment in arm_assignments.fetch_all(state.db):
            if assignment.clinician_id == clinician_id and assignment.patient_id not in seen:
                seen.add(assignment.patient_id)
                active.append(assignment.patient_id)
    return active


def _htmx_aware_redirect(request: Request, url: str) -> Response:
    """303 for browsers; 200 + ``HX-Redirect`` so htmx swaps the whole page."""
    if _is_htmx(request):
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _back_to_index(response: Response) -> Response:
    """Refusal whose ``HX-Redirect`` sends htmx back to the index (S11e)."""
    response.headers["HX-Redirect"] = _INDEX_URL
    return response


def _conflict(message: str) -> HTMLResponse:
    return HTMLResponse(content=_error_flash(message), status_code=status.HTTP_409_CONFLICT)


def _check_contact(
    request: Request, clinician_id: str, patient_id: str, case: CaseConfiguration | None
) -> ContactResult:
    state = request.app.state
    return case_contact.check(
        state.db, state, clinician_id=clinician_id, patient_id=patient_id, case=case
    )


def _touch_contact(request: Request, contact: ContactResult | None) -> None:
    if contact is not None:
        case_contact.touch(request.app.state.db, request.app.state, contact)


def _case_paused_page(request: Request, patient_id: str) -> Response:
    """Resume interstitial: no panels, no questions, nothing sliced."""
    if _is_htmx(request):
        # Leave the swap target: the interstitial is a whole page.
        return Response(
            status_code=status.HTTP_200_OK, headers={"HX-Redirect": str(request.url.path)}
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "case_paused.html",
        {"patient_id": patient_id, "logged_in_name": _logged_in_name(request)},
    )


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
        return None, _htmx_aware_redirect(request, "/login")
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
    request: Request,
    patient_id: str,
    t_index: int,
    *,
    patient_ids: list[str] | None = None,
    timepoints: tuple[float, ...] | None = None,
) -> tuple[ResolvedTimepoint | None, str | None]:
    """Study-membership → dataset-membership → ``t_index`` range.

    ``patient_ids``/``timepoints`` override the app-state study models with
    the case's historical snapshot (S11b: a pinned case uses its own patient
    list and timepoints).

    Returns ``(resolved, None)`` or ``(None, message)``. Callers render the
    message in their own shape: GET wraps it in the S2 ``error-flash`` div,
    POST routes it through ``_answer_status.html``.
    """
    dataset = request.app.state.dataset
    study_patient_ids = patient_ids
    if study_patient_ids is None:
        study_patient_ids = getattr(request.app.state, "study_patient_ids", None)
    if study_patient_ids is not None and patient_id not in study_patient_ids:
        return None, f"Patient '{patient_id}' is not part of this study"

    known_pids = set(dataset.admission["patient_id"].unique().tolist())
    if patient_id not in known_pids:
        return None, f"Patient '{patient_id}' not found"

    if timepoints is not None:
        if t_index < 0 or t_index >= len(timepoints):
            return None, (
                f"Timepoint t_index={t_index} out of range (valid: 0\u2026{len(timepoints) - 1})"
            )
        return ResolvedTimepoint(timepoints=timepoints, t_minutes=timepoints[t_index]), None
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


def _answer_status(
    request: Request,
    *,
    state: str,
    question_id: str | None = None,
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
    trailing_html: str = "",
) -> HTMLResponse:
    """The one response shape of ``POST …/answer``: the badge fragment.

    ``trailing_html`` rides behind the badge — the out-of-band advance CTA
    on a 200, nothing on an error.
    """
    html = request.app.state.templates.get_template("_answer_status.html").render(
        request=request, state=state, question_id=question_id, error=error
    )
    return HTMLResponse(content=html + trailing_html, status_code=status_code)


def _render_advance_cta(
    request: Request,
    *,
    patient_id: str,
    t_index: int,
    chrome: str,
    remaining: tuple[str, ...],
    is_last: bool,
    oob: bool,
) -> str:
    return request.app.state.templates.get_template("_advance_cta.html").render(
        request=request,
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        remaining=remaining,
        is_last=is_last,
        oob=oob,
    )


def _study_bootstrap(
    request: Request,
    *,
    clinician_id: str,
    patient_id: str,
    frontier: Frontier,
    case: CaseConfiguration | None = None,
) -> SessionContext:
    state = request.app.state
    ctx = bootstrap_session(
        state.db,
        state,
        clinician_id=clinician_id,
        patient_id=patient_id,
        frontier=frontier,
        case=case,
    )
    update_request_context(arm=ctx.arm)
    return ctx


def _render_questions_pane(
    request: Request,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    t_minutes: float,
    chrome: str,
    timepoint_count: int,
    case: CaseConfiguration | None = None,
    contact: ContactResult | None = None,
) -> str:
    """Render the pre-filled pane in ``open`` or ``locked`` mode (study mode only).

    S11e: an active tracked case also carries the heartbeat anchor and, when
    its pinned policy allows it, the Pause button.
    """
    state = request.app.state
    questions = case.questions if case is not None else state.questions
    prefill = saved_answers(
        state.db,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_minutes=t_minutes,
        questions=questions,
        config_hash=ctx.config_hash,
        config_version=ctx.config_version,
    )
    mode = pane_mode(ctx.frontier, t_index)
    is_last = t_index == timepoint_count - 1
    case_active = contact is not None and contact.access is CaseAccess.ACTIVE
    cta_html = ""
    if mode == "open":
        comp = completeness(questions, prefill)
        cta_html = _render_advance_cta(
            request,
            patient_id=patient_id,
            t_index=t_index,
            chrome=chrome,
            remaining=comp.remaining,
            is_last=is_last,
            oob=False,
        )
    return state.templates.get_template("_questions_pane.html").render(
        request=request,
        patient_id=patient_id,
        t_index=t_index,
        t_minutes=t_minutes,
        chrome=chrome,
        questions=questions.questions,
        prefill=prefill,
        mode=mode,
        completed=ctx.frontier.completed,
        unlocked_t_index=ctx.frontier.unlocked_t_index,
        timepoint_count=timepoint_count,
        cta_html=cta_html,
        free_text_max_chars=FREE_TEXT_MAX_CHARS,
        free_text_autosave_delay_ms=FREE_TEXT_AUTOSAVE_DELAY_MS,
        probability_min=PROBABILITY_MIN,
        probability_max=PROBABILITY_MAX,
        heartbeat_enabled=case_active,
        heartbeat_interval_ms=case_contact.HEARTBEAT_INTERVAL_SECONDS * 1000,
        pause_enabled=case_active and case_contact.policy_for(case).pause_enabled,
    )


def _render_patient_view(
    request: Request,
    *,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    chrome: str,
    resolved: ResolvedTimepoint,
    ctx: SessionContext | None,
    case: CaseConfiguration | None = None,
    contact: ContactResult | None = None,
) -> str:
    """Slice → panels → summary → chrome → pane → ``_patient_view.html``.

    Shared by the GET route and ``/advance``. ``ctx is None`` outside study
    mode: no gate, no pane, S2 navigation.
    """
    state = request.app.state
    templates = state.templates
    t_minutes = float(resolved.t_minutes)
    timepoint_count = len(resolved.timepoints)
    at_last = t_index == timepoint_count - 1

    patient_slice = slice_to_timepoint(state.dataset, patient_id, t_minutes, t_index)
    panels_html = _render_panels(patient_slice, request)

    # Forward navigation by plain hx-get is allowed only into already
    # unlocked timepoints; at the frontier the pane CTA is the one path.
    show_next = not at_last
    resume_t_index: dict[str, int] = {}
    jumper_patient_ids: list[str] | None = None
    questions_html = ""
    if ctx is not None:
        show_next = not at_last and t_index + 1 <= ctx.frontier.unlocked_t_index
        case_list = _case_patient_ids(request, clinician_id)
        if is_phase2_mode(state):
            jumper_patient_ids = case_list
        overview = progress_overview(
            state.db,
            clinician_id=clinician_id,
            patient_ids=case_list,
            timepoint_count=timepoint_count,
        )
        resume_t_index = {pid: p.unlocked_t_index for pid, p in overview.items()}
        questions_html = _render_questions_pane(
            request,
            ctx=ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            t_index=t_index,
            t_minutes=t_minutes,
            chrome=chrome,
            timepoint_count=timepoint_count,
            case=case,
            contact=contact,
        )

    summary_html = _render_summary(
        patient_slice,
        request,
        chrome=chrome,
        timepoint_count=timepoint_count,
        show_next=show_next,
        resume_t_index=resume_t_index,
        patient_ids=jumper_patient_ids,
    )
    logged_in_name = _logged_in_name(request)
    template_name = "_chrome_dense.html" if chrome == "dense" else "_chrome_epic.html"
    chrome_html = templates.get_template(template_name).render(
        request=request,
        patient_slice=patient_slice,
        panels=panels_html,
        chrome=chrome,
        logged_in_name=logged_in_name,
    )
    return templates.get_template("_patient_view.html").render(
        request=request,
        patient_slice=patient_slice,
        chrome=chrome,
        chrome_html=chrome_html,
        summary_html=summary_html,
        questions_html=questions_html,
        timepoint_count=timepoint_count,
        logged_in_name=logged_in_name,
    )


def _full_document(
    request: Request, *, inner: str, patient_id: str, t_index: int, chrome: str
) -> Response:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "base.html",
        {
            "chrome": chrome,
            "inner": inner,
            "patient_id": patient_id,
            "t_index": t_index,
            "logged_in_name": _logged_in_name(request),
        },
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
    state = request.app.state
    dataset = state.dataset
    # Study config (when loaded via `serve --config`) is the authoritative
    # patient list — order is preserved, off-study patients are hidden.
    # Without a study config, fall back to the full dataset list (S2 behavior).
    study_patient_ids = getattr(state, "study_patient_ids", None)
    patient_progress: dict[str, PatientProgress] | None = None
    case_state = index_state(state.db, state, clinician_id or "") if is_phase2_mode(state) else None
    lifecycle_states: dict[str, str] = {}
    if case_state is not None:
        lifecycle_states = case_states(state.db, clinician_id or "")
    if study_patient_ids is not None:
        patient_ids = _case_patient_ids(request, clinician_id or "")
        patient_progress = progress_overview(
            state.db,
            clinician_id=clinician_id or "",
            patient_ids=patient_ids,
            timepoint_count=len(state.study_timepoints),
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
            "lifecycle_states": lifecycle_states,
            "logged_in_name": _logged_in_name(request),
        },
    )


@router.post("/case/start")
async def case_start(request: Request, chrome: Chrome = "epic") -> Response:
    """Resume the open case or activate the next planned one (S11d).

    Success redirects to the case's frontier; every refusal is a flash with
    no arm-revealing value and leaves no assignment, session or event.
    """
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)
    state = request.app.state

    try:
        started = start_next_case(state.db, state, clinician_id=clinician_id or "")
    except (CaseStartRefusedError, StaleConfigurationError, ScheduleIncompatibleError) as exc:
        get_logger().warning("start case refused", event_kind="case.start.refused", error=str(exc))
        return HTMLResponse(content=_error_flash(str(exc)), status_code=status.HTTP_409_CONFLICT)
    except RandomisationIntegrityError as exc:
        get_logger().error(
            "start case integrity failure", event_kind="case.start.integrity", error=str(exc)
        )
        return HTMLResponse(
            content=_error_flash(_CASE_START_INTEGRITY_MSG),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    update_request_context(patient_id=started.patient_id)
    return _htmx_aware_redirect(
        request, _timepoint_url(started.patient_id, started.resume_t_index, chrome)
    )


def _lifecycle_request(
    request: Request, clinician_id: str, patient_id: str
) -> tuple[ContactResult | None, CaseConfiguration | None, Response | None]:
    """Shared preamble of the S11e case endpoints: a tracked Phase 2 case or a refusal."""
    state = request.app.state
    if not is_phase2_mode(state) or _is_unactivated_phase2_patient(
        request, clinician_id, patient_id
    ):
        return None, None, _conflict(_NOT_ACTIVATED_MSG)

    case, case_error, case_status = _try_resolve_case(request, clinician_id, patient_id)
    if case_error is not None:
        return None, None, HTMLResponse(content=_error_flash(case_error), status_code=case_status)

    contact = _check_contact(request, clinician_id, patient_id, case)
    if contact.access is CaseAccess.UNTRACKED:
        return None, None, _conflict(_NOT_ACTIVATED_MSG)
    if contact.access in (CaseAccess.INCOMPLETE, CaseAccess.COMPLETED):
        return None, None, _back_to_index(_conflict(_CASE_CLOSED_MSG))
    return contact, case, None


def _frontier_url(
    request: Request,
    clinician_id: str,
    patient_id: str,
    case: CaseConfiguration | None,
    chrome: str,
) -> str:
    frontier = read_frontier(
        request.app.state.db,
        request.app.state,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoints=case.timepoints if case is not None else None,
    )
    return _timepoint_url(patient_id, frontier.unlocked_t_index, chrome)


@router.post("/case/{patient_id}/heartbeat")
async def case_heartbeat(request: Request, patient_id: str) -> Response:
    """Keep an open case page alive; records nothing but ``last_seen_at``."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect

    contact, _case, refusal = _lifecycle_request(request, clinician_id or "", patient_id)
    if refusal is not None:
        return refusal
    if contact.access is not CaseAccess.ACTIVE:  # type: ignore[union-attr]
        return _back_to_index(_conflict(_CASE_NOT_ACTIVE_MSG))

    _touch_contact(request, contact)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/case/{patient_id}/pause")
async def case_pause(request: Request, patient_id: str, chrome: Chrome = "epic") -> Response:
    """Voluntary pause, when the case's pinned policy allows it."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id, patient_id=patient_id)

    contact, case, refusal = _lifecycle_request(request, clinician_id or "", patient_id)
    if refusal is not None:
        return refusal
    if not case_contact.policy_for(case).pause_enabled:
        return _conflict(_PAUSE_DISABLED_MSG)
    if contact.access is not CaseAccess.ACTIVE:  # type: ignore[union-attr]
        return _conflict(_CASE_NOT_ACTIVE_MSG)

    try:
        case_contact.pause_case(request.app.state.db, request.app.state, contact)  # type: ignore[arg-type]
    except CaseLifecycleError as exc:
        return _conflict(str(exc))

    return _htmx_aware_redirect(
        request, _frontier_url(request, clinician_id or "", patient_id, case, chrome)
    )


@router.post("/case/{patient_id}/resume")
async def case_resume(request: Request, patient_id: str, chrome: Chrome = "epic") -> Response:
    """Resume a paused case inside its pause grace; beyond it the case is incomplete."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id, patient_id=patient_id)

    contact, case, refusal = _lifecycle_request(request, clinician_id or "", patient_id)
    if refusal is not None:
        return refusal
    if contact.access is not CaseAccess.PAUSED:  # type: ignore[union-attr]
        return _conflict(_CASE_NOT_PAUSED_MSG)

    try:
        case_contact.resume_case(request.app.state.db, request.app.state, contact, case)  # type: ignore[arg-type]
    except CaseLifecycleError as exc:
        return _conflict(str(exc))

    return _htmx_aware_redirect(
        request, _frontier_url(request, clinician_id or "", patient_id, case, chrome)
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
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)
    state = request.app.state
    if _is_unactivated_phase2_patient(request, clinician_id or "", patient_id):
        return _htmx_aware_redirect(request, _INDEX_URL)

    case: CaseConfiguration | None = None
    if state.study is not None:
        case, case_error, case_status = _try_resolve_case(request, clinician_id or "", patient_id)
        if case_error is not None:
            return HTMLResponse(content=_error_flash(case_error), status_code=case_status)

    # S11e: lifecycle gate before the frontier gate and before any slice.
    contact: ContactResult | None = None
    if state.study is not None:
        contact = _check_contact(request, clinician_id or "", patient_id, case)
        if contact.access is CaseAccess.INCOMPLETE:
            return _htmx_aware_redirect(request, _INDEX_URL)
        if contact.access is CaseAccess.PAUSED:
            return _case_paused_page(request, patient_id)

    resolved, message = _resolve_timepoint(
        request,
        patient_id,
        t_index,
        patient_ids=list(case.patient_ids) if case is not None else None,
        timepoints=case.timepoints if case is not None else None,
    )
    if resolved is None:
        return HTMLResponse(content=_error_flash(message or ""), status_code=404)

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
            clinician_id=clinician_id or "",
            patient_id=patient_id,
            timepoints=case.timepoints if case is not None else None,
        )
        if not is_viewable(frontier, t_index):
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
            clinician_id=clinician_id or "",
            patient_id=patient_id,
            frontier=frontier,
            case=case,
        )

    inner = _render_patient_view(
        request,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        resolved=resolved,
        ctx=ctx,
        case=case,
        contact=contact,
    )
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
            status_code=200,
            headers={"HX-Push-Url": _timepoint_url(patient_id, t_index, chrome)},
        )

    # Only a successfully rendered editable frontier counts as an enter.
    if state.study is not None and ctx is not None and pane_mode(ctx.frontier, t_index) == "open":
        record_enter(
            state.db,
            state,
            ctx=ctx,
            clinician_id=clinician_id or "",
            patient_id=patient_id,
            t_index=t_index,
            t_minutes=float(resolved.t_minutes),
        )

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
    if _is_unactivated_phase2_patient(request, clinician_id or "", patient_id):
        return _answer_status(
            request, state="error", error=_NOT_ACTIVATED_MSG, status_code=status.HTTP_409_CONFLICT
        )

    case = None
    if state.study is not None:
        case, case_error, case_status = _try_resolve_case(request, clinician_id or "", patient_id)
        if case_error is not None:
            return _answer_status(request, state="error", error=case_error, status_code=case_status)

    contact: ContactResult | None = None
    if state.study is not None:
        contact = _check_contact(request, clinician_id or "", patient_id, case)
        if contact.access is CaseAccess.PAUSED:
            return _answer_status(
                request, state="error", error=_CASE_PAUSED_MSG, status_code=status.HTTP_409_CONFLICT
            )
        if contact.access is CaseAccess.INCOMPLETE:
            return _back_to_index(
                _answer_status(
                    request,
                    state="error",
                    error=_CASE_CLOSED_MSG,
                    status_code=status.HTTP_409_CONFLICT,
                )
            )

    questions = case.questions if case is not None else state.questions
    resolved, message = _resolve_timepoint(
        request,
        patient_id,
        t_index,
        patient_ids=list(case.patient_ids) if case is not None else None,
        timepoints=case.timepoints if case is not None else None,
    )
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
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        timepoints=case.timepoints if case is not None else None,
    )
    ctx = _study_bootstrap(
        request,
        clinician_id=clinician_id or "",
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
        outcome = record_answer(
            state.db,
            state,
            ctx=ctx,
            clinician_id=clinician_id or "",
            patient_id=patient_id,
            t_minutes=t_minutes,
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
        clinician_id=clinician_id or "",
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
    _touch_contact(request, contact)
    return _answer_status(request, state=outcome, question_id=question_id, trailing_html=cta_html)


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
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)

    state = request.app.state
    if state.study is None:
        return HTMLResponse(
            content=_error_flash(_NO_QUESTIONS_MSG), status_code=status.HTTP_409_CONFLICT
        )
    if _is_unactivated_phase2_patient(request, clinician_id or "", patient_id):
        return HTMLResponse(
            content=_error_flash(_NOT_ACTIVATED_MSG), status_code=status.HTTP_409_CONFLICT
        )

    case, case_error, case_status = _try_resolve_case(request, clinician_id or "", patient_id)
    if case_error is not None:
        return HTMLResponse(content=_error_flash(case_error), status_code=case_status)

    contact = _check_contact(request, clinician_id or "", patient_id, case)
    if contact.access is CaseAccess.PAUSED:
        return HTMLResponse(
            content=_error_flash(_CASE_PAUSED_MSG), status_code=status.HTTP_409_CONFLICT
        )
    if contact.access is CaseAccess.INCOMPLETE:
        return _back_to_index(_conflict(_CASE_CLOSED_MSG))

    questions = case.questions if case is not None else state.questions
    resolved, message = _resolve_timepoint(
        request,
        patient_id,
        t_index,
        patient_ids=list(case.patient_ids) if case is not None else None,
        timepoints=case.timepoints if case is not None else None,
    )
    if resolved is None:
        return HTMLResponse(content=_error_flash(message or ""), status_code=404)
    update_request_context(
        patient_id=patient_id,
        timepoint=float(resolved.t_minutes),
        timepoint_index=t_index,
        chrome=chrome,
    )

    # The only await in this handler sits BEFORE the frontier read, so the
    # read → compare-and-set below runs without yielding to the loop.
    form = await request.form()
    frontier = read_frontier(
        state.db,
        state,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        timepoints=case.timepoints if case is not None else None,
    )
    ctx = _study_bootstrap(
        request,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        frontier=frontier,
        case=case,
    )

    result = advance(
        state.db,
        state,
        ctx=ctx,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        t_index=t_index,
        timepoints=resolved.timepoints,
        questions=questions,
        client_ts=_form_str(form.get("client_ts")),
        client_seq=_form_str(form.get("client_seq")),
    )
    # A finished walk completed the case; touch() then finds nothing active.
    _touch_contact(request, contact)
    return _advance_response(
        request,
        result=result,
        ctx=ctx,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        t_index=t_index,
        chrome=chrome,
        timepoint_count=len(resolved.timepoints),
        case=case,
        contact=contact,
    )


def _advance_response(
    request: Request,
    *,
    result: AdvanceResult,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    chrome: str,
    timepoint_count: int,
    case: CaseConfiguration | None = None,
    contact: ContactResult | None = None,
) -> Response:
    """Map an :class:`AdvanceResult` onto the HTMX / plain-browser contract (spec §5.1)."""
    if result.outcome == "finished":
        return _htmx_aware_redirect(request, _INDEX_URL)

    if result.outcome == "blocked":
        if not _is_htmx(request):
            return RedirectResponse(
                _timepoint_url(patient_id, t_index, chrome), status_code=status.HTTP_303_SEE_OTHER
            )
        html = _render_advance_cta(
            request,
            patient_id=patient_id,
            t_index=t_index,
            chrome=chrome,
            remaining=result.remaining,
            is_last=t_index == timepoint_count - 1,
            oob=False,
        )
        return HTMLResponse(
            content=html,
            status_code=status.HTTP_409_CONFLICT,
            headers={"HX-Retarget": "#advance-form", "HX-Reswap": "outerHTML"},
        )

    # "advanced" and "stale" both answer with the frontier's view. The ctx
    # from bootstrap predates the write, so re-point it at the new frontier.
    target_t_index = result.unlocked_t_index
    target_url = _timepoint_url(patient_id, target_t_index, chrome)
    if not _is_htmx(request):
        return RedirectResponse(target_url, status_code=status.HTTP_303_SEE_OTHER)

    target_ctx = replace(ctx, frontier=Frontier(target_t_index, ctx.frontier.completed))
    target_resolved, message = _resolve_timepoint(
        request,
        patient_id,
        target_t_index,
        patient_ids=list(case.patient_ids) if case is not None else None,
        timepoints=case.timepoints if case is not None else None,
    )
    if target_resolved is None:  # unreachable: read_frontier clamps to the study range
        return HTMLResponse(
            content=_error_flash(message or ""),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    inner = _render_patient_view(
        request,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=target_t_index,
        chrome=chrome,
        resolved=target_resolved,
        ctx=target_ctx,
        case=case,
        contact=contact,
    )
    status_code = (
        status.HTTP_200_OK if result.outcome == "advanced" else status.HTTP_412_PRECONDITION_FAILED
    )
    if result.outcome == "advanced":
        # S10: the htmx swap shows the next frontier pane without a new GET,
        # so its enter rides on this response. The 412 "stale" reply keeps
        # the frontier the clinician is actually on — no enter there (and
        # the non-HTMX 303 path defers to the GET that follows).
        record_enter(
            request.app.state.db,
            request.app.state,
            ctx=target_ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            t_index=target_t_index,
            t_minutes=float(target_resolved.t_minutes),
        )
    return HTMLResponse(content=inner, status_code=status_code, headers={"HX-Push-Url": target_url})


def _form_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _render_summary(
    patient_slice: PatientSlice,
    request: Request,
    *,
    chrome: str,
    timepoint_count: int,
    show_next: bool,
    resume_t_index: dict[str, int],
    patient_ids: list[str] | None = None,
) -> str:
    """``patient_ids`` overrides the jumper list (S11d Phase 2: own cases only)."""
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
