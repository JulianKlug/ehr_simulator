"""Patient view composition: slice → panels → summary → chrome → pane, the
advance CTA, out-of-band question slots, and the ``/advance`` response map.

S11j: a render whose case pins ``telemetry`` carries a ``render_id``,
recorded as ``timepoint.render`` after the response is built (``_record_view``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from fastapi import Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from ehr_simulator.config.questions import Questions
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.logging import update_request_context
from ehr_simulator.question_branching import evaluate
from ehr_simulator.web import case_contact, tab_guard
from ehr_simulator.web.answer_capture import (
    FREE_TEXT_AUTOSAVE_DELAY_MS,
    FREE_TEXT_MAX_CHARS,
    PROBABILITY_MAX,
    PROBABILITY_MIN,
    saved_answers,
)
from ehr_simulator.web.case_contact import CaseAccess, ContactResult
from ehr_simulator.web.case_request import ResolvedTimepoint, _backward_policy, _resolve_timepoint
from ehr_simulator.web.case_start import case_patient_ids
from ehr_simulator.web.gating import AdvanceResult, completeness, pane_mode, progress_overview
from ehr_simulator.web.panel_render import _render_panels_locked, _render_summary
from ehr_simulator.web.panels import (
    LEGACY_INTERVENTION,
    ai_delivery,
    slice_to_timepoint,
)
from ehr_simulator.web.route_support import (
    _INDEX_URL,
    _TELEMETRY_URL,
    _error_flash,
    _htmx_aware_redirect,
    _is_htmx,
    _logged_in_name,
    _timepoint_url,
)
from ehr_simulator.web.study_session import (
    CaseConfiguration,
    Frontier,
    SessionContext,
    bootstrap_session,
    is_phase2_mode,
    pinned_timepoint_counts,
    resolve_intervention,
    transitional_patient_ids,
)
from ehr_simulator.web.timing_events import (
    VisitKind,
    new_render_id,
    record_enter,
    record_render,
    record_revisit,
)


class RenderTracking(StrEnum):
    """S11j: whether a rendered view may carry a telemetry ``render_id``."""

    TRACKED = "tracked"
    WRITE_FREE = "write_free"  # the S9b 412 stale view: nothing is written


@dataclass(frozen=True)
class RenderedView:
    """A rendered ``#patient-view``. ``render_id`` is set only when the case's
    pinned config enables telemetry (S11j); ``ai_delivery`` is the S11l
    evidence recorded with it."""

    html: str
    t_index: int
    t_minutes: float
    visit_kind: VisitKind
    render_id: str | None
    ai_delivery: dict[str, str]
    tab_guard: bool = False  # S11m: its tab must hold the case lease


def _case_patient_ids(request: Request, clinician_id: str) -> list[str]:
    """Index + jumper list: Phase 2 shows only the clinician's cases (S11d)."""
    state = request.app.state
    if is_phase2_mode(state):
        return case_patient_ids(state.db, clinician_id)

    return transitional_patient_ids(state.db, state, clinician_id=clinician_id)


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
        evaluated=evaluate(questions, prefill).questions,
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


async def _render_patient_view(
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
    render_tracking: RenderTracking = RenderTracking.TRACKED,
) -> RenderedView:
    """Slice → panels → summary → chrome → pane → ``_patient_view.html``.

    Shared by the GET route and ``/advance``. ``ctx is None`` outside study
    mode: no gate, no pane, S2 navigation. S11j: a case pinned to a
    ``telemetry`` block gets a ``render_id`` (the caller records it after the
    response is built); ``RenderTracking.WRITE_FREE`` keeps a view without one.
    """
    state = request.app.state
    templates = state.templates
    t_minutes = float(resolved.t_minutes)
    timepoint_count = len(resolved.timepoints)
    at_last = t_index == timepoint_count - 1

    patient_slice = slice_to_timepoint(state.dataset, patient_id, t_minutes, t_index)
    intervention = LEGACY_INTERVENTION
    if ctx is not None:
        intervention = resolve_intervention(
            state.db, state.dataset, case=case, clinician_id=clinician_id, patient_id=patient_id
        )

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
            timepoint_counts=pinned_timepoint_counts(
                state.db, clinician_id=clinician_id, patient_ids=case_list
            ),
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
    logged_in_name = _logged_in_name(request)

    # Every DB read is done: the plotnine charts render off the event loop.
    panels_html = await run_in_threadpool(
        _render_panels_locked, patient_slice, request, intervention
    )

    summary_html = _render_summary(
        patient_slice,
        request,
        chrome=chrome,
        timepoint_count=timepoint_count,
        show_next=show_next,
        resume_t_index=resume_t_index,
        patient_ids=jumper_patient_ids,
        intervention_mode=intervention.mode,
        backward=_backward_policy(case),
    )
    is_revisit = ctx is not None and pane_mode(ctx.frontier, t_index) != "open"
    visit_kind = VisitKind.REVISIT if is_revisit else VisitKind.PRIMARY
    telemetry = case.study.telemetry if case is not None and ctx is not None else None
    tracked = telemetry is not None and render_tracking is RenderTracking.TRACKED
    render_id = new_render_id() if tracked else None
    guarded = tracked and tab_guard.is_guarded(case, contact)
    template_name = "_chrome_dense.html" if chrome == "dense" else "_chrome_epic.html"
    chrome_html = templates.get_template(template_name).render(
        request=request,
        patient_slice=patient_slice,
        panels=panels_html,
        show_ai="ai" in panels_html,
        chrome=chrome,
        logged_in_name=logged_in_name,
    )
    html = templates.get_template("_patient_view.html").render(
        request=request,
        patient_slice=patient_slice,
        chrome=chrome,
        chrome_html=chrome_html,
        summary_html=summary_html,
        questions_html=questions_html,
        timepoint_count=timepoint_count,
        logged_in_name=logged_in_name,
        visit_kind=visit_kind,
        observation_mode=case.observation_mode if case is not None else ObservationMode.MEASURED,
        render_id=render_id,
        telemetry_url=_TELEMETRY_URL,
        viewport_threshold=telemetry.panel_viewport_threshold if telemetry else None,
        tab_guard=guarded,
    )
    return RenderedView(
        html=html,
        t_index=t_index,
        t_minutes=t_minutes,
        visit_kind=visit_kind,
        render_id=render_id,
        ai_delivery=ai_delivery(patient_slice, intervention),
        tab_guard=guarded,
    )


def _record_render(
    request: Request,
    *,
    view: RenderedView,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    commit: bool = True,
) -> None:
    """S11j: record a telemetry view after its response is built."""
    if view.render_id is None:
        return

    record_render(
        request.app.state.db,
        request.app.state,
        ctx=ctx,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=view.t_index,
        t_minutes=view.t_minutes,
        render_id=view.render_id,
        visit_kind=view.visit_kind,
        ai_delivery=view.ai_delivery,
        tab_guard=view.tab_guard,
        commit=commit,
    )


def _record_view(
    request: Request,
    *,
    view: RenderedView,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    lease_tab_id: str | None = None,
) -> None:
    """A built view's enter (or revisit), ``timepoint.render`` and, after a
    guarded advance, the owner's lease move: one transaction, so a failure
    never leaves an enter without its render (``commit=False`` convention).
    """
    state = request.app.state
    conn = state.db
    record = record_enter if view.visit_kind is VisitKind.PRIMARY else record_revisit
    try:
        record(
            conn,
            state,
            ctx=ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            t_index=view.t_index,
            t_minutes=view.t_minutes,
            commit=False,
        )
        _record_render(
            request,
            view=view,
            ctx=ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            commit=False,
        )
        if lease_tab_id and view.tab_guard and view.render_id is not None:
            tab_guard.move_to_render(
                conn,
                state,
                clinician_id=clinician_id,
                patient_id=patient_id,
                tab_id=lease_tab_id,
                render_id=view.render_id,
                commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    state.write_counter = getattr(state, "write_counter", 0) + 1


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


def _render_changed_slots(
    request: Request,
    *,
    questions: Questions,
    saved: dict[str, str | list[str]],
    changed: tuple[str, ...],
    patient_id: str,
    t_index: int,
    chrome: str,
) -> str:
    """S11h: out-of-band replacements of the slots whose branch state moved."""
    if not changed:
        return ""

    template = request.app.state.templates.get_template("_question.html")
    evaluated = evaluate(questions, saved)
    return "".join(
        template.render(
            request=request,
            item=evaluated.get(qid),
            prefill=saved,
            mode="open",
            oob=True,
            patient_id=patient_id,
            t_index=t_index,
            chrome=chrome,
            free_text_max_chars=FREE_TEXT_MAX_CHARS,
            free_text_autosave_delay_ms=FREE_TEXT_AUTOSAVE_DELAY_MS,
            probability_min=PROBABILITY_MIN,
            probability_max=PROBABILITY_MAX,
        )
        for qid in changed
    )


async def _advance_response(
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
    tab_id: str | None = None,
) -> Response:
    """Map an :class:`AdvanceResult` onto the HTMX / plain-browser contract (spec §5.1).

    S11m: the owner's lease follows the advanced-into render, so the swapped
    view writes without waiting for its own claim.
    """
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

    advanced = result.outcome == "advanced"
    if not advanced and tab_guard.is_guarded(case, contact):
        # S11m: the write-free stale view has no render id, so its tab could
        # never hold the lease again; the frontier GET serves a guarded one.
        return Response(
            status_code=status.HTTP_412_PRECONDITION_FAILED, headers={"HX-Redirect": target_url}
        )

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
    # S9b: the 412 stale view is write free, so it carries no render id.
    view = await _render_patient_view(
        request,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_index=target_t_index,
        chrome=chrome,
        resolved=target_resolved,
        ctx=target_ctx,
        case=case,
        contact=contact,
        render_tracking=RenderTracking.TRACKED if advanced else RenderTracking.WRITE_FREE,
    )
    status_code = status.HTTP_200_OK if advanced else status.HTTP_412_PRECONDITION_FAILED
    if advanced:
        # S10: the htmx swap shows the next frontier pane without a new GET,
        # so its enter rides on this response. The 412 "stale" reply keeps
        # the frontier the clinician is actually on — no enter there (and
        # the non-HTMX 303 path defers to the GET that follows).
        _record_view(
            request,
            view=view,
            ctx=target_ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            lease_tab_id=tab_id,
        )
    return HTMLResponse(
        content=view.html, status_code=status_code, headers={"HX-Push-Url": target_url}
    )
