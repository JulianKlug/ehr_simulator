"""The case-request preamble shared by the patient GET, ``/answer`` and
``/advance`` routes, plus the case helpers the lifecycle endpoints reuse.

One ordered gate, each step refusing in the calling route's own shape::

    cookie clinician ─► mode gate (answer: questions, advance: study)
        ─► unactivated Phase 2 pair (S11d)
        ─► [form read: POSTs only — the handler's only await, BEFORE the
            case checks, so no pause/timeout interleaves before the write]
        ─► pinned case (S11b) ─► lifecycle contact (S11e) ─► tab lease (S11m,
            POSTs only) ─► patient + t_index range
        ─► CaseRequest
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from fastapi import Request, Response, status
from fastapi.responses import HTMLResponse

from ehr_simulator.config.study import BackwardNavigation
from ehr_simulator.db.exceptions import ConfigurationProvenanceError, StaleConfigurationError
from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.web import case_contact, tab_guard
from ehr_simulator.web.case_contact import CaseAccess, ContactResult
from ehr_simulator.web.panels import patient_timepoints
from ehr_simulator.web.route_support import (
    _INDEX_URL,
    _answer_status,
    _back_to_index,
    _error_flash,
    _htmx_aware_redirect,
    _is_htmx,
    _logged_in_name,
    _mark_tab_conflict,
    _require_clinician,
)
from ehr_simulator.web.study_session import (
    CaseConfiguration,
    CaseNotActivatedError,
    is_unactivated_phase2_pair,
    resolve_case_configuration,
)
from ehr_simulator.web.tab_guard import TabGuardError

_NO_QUESTIONS_MSG = "No questions configured (start with --config/--questions)"
_NOT_ACTIVATED_MSG = "This patient is not an activated case; start a case from the study index"
_CASE_PAUSED_MSG = "This case is paused; resume it from the study index"
_CASE_CLOSED_MSG = "This case is closed and accepts no further answers"


class RefusalShape(StrEnum):
    """How a case route answers a refusal (each pinned by tests)."""

    PAGE = "page"  # GET: redirects, the paused interstitial, an error flash
    ANSWER_BADGE = "answer_badge"  # POST …/answer: the ``_answer_status`` fragment
    ADVANCE_FLASH = "advance_flash"  # POST …/advance: an error flash


@dataclass(frozen=True)
class ResolvedTimepoint:
    timepoints: tuple[float, ...]
    t_minutes: float


@dataclass(frozen=True)
class CaseRequest:
    """A case request that passed every preamble gate.

    ``case``/``contact`` are ``None`` outside study mode; ``form`` is ``None``
    on GET.
    """

    clinician_id: str
    case: CaseConfiguration | None
    contact: ContactResult | None
    resolved: ResolvedTimepoint
    form: Any


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
        return None, None, status.HTTP_200_OK
    try:
        case = resolve_case_configuration(
            state.db, state, clinician_id=clinician_id, patient_id=patient_id
        )
    except (StaleConfigurationError, CaseNotActivatedError) as exc:
        return None, str(exc), status.HTTP_409_CONFLICT
    except ConfigurationProvenanceError as exc:
        return None, str(exc), status.HTTP_500_INTERNAL_SERVER_ERROR
    return case, None, status.HTTP_200_OK


def _is_unactivated_phase2_patient(request: Request, clinician_id: str, patient_id: str) -> bool:
    """Phase 2 pair with no assignment or practice case: not a case, nothing
    may be resolved or written."""
    state = request.app.state
    return is_unactivated_phase2_pair(
        state.db, state, clinician_id=clinician_id, patient_id=patient_id
    )


def _tab_refusal(
    request: Request,
    *,
    clinician_id: str,
    patient_id: str,
    case: CaseConfiguration | None,
    contact: ContactResult | None,
    form: Any = None,
) -> str | None:
    """S11m: ``None`` when the request may write, else the refusal message.

    Unguarded cases pass untouched; a guarded one needs the lease holder's
    ``(tab_id, render_id)`` from the headers or, for plain forms, ``form``.
    """
    if not tab_guard.is_guarded(case, contact):
        return None

    tab_id, render_id = tab_guard.owner_identity(request.headers, form)
    try:
        tab_guard.require_owner(
            request.app.state.db,
            request.app.state,
            clinician_id=clinician_id,
            patient_id=patient_id,
            tab_id=tab_id,
            render_id=render_id,
        )
    except TabGuardError as exc:
        get_logger().warning("tab guard refused", event_kind="tab.refused", error=str(exc))
        return str(exc)
    return None


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
        # Leave the swap target: the interstitial is a whole page. Keep the
        # query (``?chrome=dense``) so the resumed view keeps its chrome.
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": target})
    return request.app.state.templates.TemplateResponse(
        request,
        "case_paused.html",
        {"patient_id": patient_id, "logged_in_name": _logged_in_name(request)},
    )


def _backward_policy(case: CaseConfiguration | None) -> BackwardNavigation:
    """S11i: the case's pinned backward navigation policy."""
    if case is None:
        return BackwardNavigation.ALLOW_READONLY
    return case.study.backward_navigation


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
                f"Timepoint t_index={t_index} out of range (valid: 0…{len(timepoints) - 1})"
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


def _refuse(request: Request, shape: RefusalShape, message: str, status_code: int) -> Response:
    """A preamble refusal in the route's own body shape."""
    if shape is RefusalShape.ANSWER_BADGE:
        return _answer_status(request, state="error", error=message, status_code=status_code)
    return HTMLResponse(content=_error_flash(message), status_code=status_code)


def _mode_refusal(request: Request, shape: RefusalShape) -> Response | None:
    """``/answer`` needs questions, ``/advance`` a study; GET runs in any mode."""
    state = request.app.state
    if shape is RefusalShape.ANSWER_BADGE and state.questions is None:
        return _refuse(request, shape, _NO_QUESTIONS_MSG, status.HTTP_409_CONFLICT)
    if shape is RefusalShape.ADVANCE_FLASH and state.study is None:
        return _refuse(request, shape, _NO_QUESTIONS_MSG, status.HTTP_409_CONFLICT)
    return None


def _contact_refusal(
    request: Request, shape: RefusalShape, patient_id: str, contact: ContactResult
) -> Response | None:
    """S11e: a paused or incomplete case refuses before any slice or write."""
    if contact.access is CaseAccess.PAUSED:
        if shape is RefusalShape.PAGE:
            return _case_paused_page(request, patient_id)
        return _refuse(request, shape, _CASE_PAUSED_MSG, status.HTTP_409_CONFLICT)

    if contact.access is CaseAccess.INCOMPLETE:
        if shape is RefusalShape.PAGE:
            return _htmx_aware_redirect(request, _INDEX_URL)
        return _back_to_index(_refuse(request, shape, _CASE_CLOSED_MSG, status.HTTP_409_CONFLICT))
    return None


async def _case_request(
    request: Request, patient_id: str, t_index: int, shape: RefusalShape
) -> CaseRequest | Response:
    """Run the preamble (module docstring); a ``Response`` is the refusal."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    clinician_id = clinician_id or ""
    update_request_context(clinician_id=clinician_id)

    mode_refusal = _mode_refusal(request, shape)
    if mode_refusal is not None:
        return mode_refusal

    if _is_unactivated_phase2_patient(request, clinician_id, patient_id):
        if shape is RefusalShape.PAGE:
            return _htmx_aware_redirect(request, _INDEX_URL)
        return _refuse(request, shape, _NOT_ACTIVATED_MSG, status.HTTP_409_CONFLICT)

    # The handler's only await: before the case checks, so a pause or a
    # timeout cannot interleave between the lifecycle check and the write.
    form = None if shape is RefusalShape.PAGE else await request.form()

    state = request.app.state
    case: CaseConfiguration | None = None
    contact: ContactResult | None = None
    if state.study is not None:
        case, case_error, case_status = _try_resolve_case(request, clinician_id, patient_id)
        if case_error is not None:
            return _refuse(request, shape, case_error, case_status)

        # S11e: lifecycle gate before the frontier gate and before any slice.
        contact = _check_contact(request, clinician_id, patient_id, case)
        contact_refusal = _contact_refusal(request, shape, patient_id, contact)
        if contact_refusal is not None:
            return contact_refusal

    # S11m: only writes need the tab lease; a GET view claims it afterwards.
    if shape is not RefusalShape.PAGE:
        tab_error = _tab_refusal(
            request, clinician_id=clinician_id, patient_id=patient_id, case=case, contact=contact
        )
        if tab_error is not None:
            return _mark_tab_conflict(_refuse(request, shape, tab_error, status.HTTP_409_CONFLICT))

    resolved, message = _resolve_timepoint(
        request,
        patient_id,
        t_index,
        patient_ids=list(case.patient_ids) if case is not None else None,
        timepoints=case.timepoints if case is not None else None,
    )
    if resolved is None:
        return _refuse(request, shape, message or "", status.HTTP_404_NOT_FOUND)

    return CaseRequest(
        clinician_id=clinician_id, case=case, contact=contact, resolved=resolved, form=form
    )
