"""Case lifecycle endpoints: Start case / practice (S11d, S11i), heartbeat,
pause, resume (S11e), the tab lease (S11m) and browser telemetry (S11j).

Every refusal is a flash with no arm-revealing value. A guarded case's
refusal is 409 + ``X-Ehrsim-Tab: conflict`` and writes nothing else.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import HTMLResponse

from ehr_simulator.db.exceptions import (
    CaseLifecycleError,
    RandomisationIntegrityError,
    StaleConfigurationError,
)
from ehr_simulator.logging import get_logger, update_request_context
from ehr_simulator.randomisation import ScheduleIncompatibleError
from ehr_simulator.web import case_contact, tab_guard
from ehr_simulator.web.case_contact import CaseAccess, ContactResult
from ehr_simulator.web.case_request import (
    _CASE_CLOSED_MSG,
    _NOT_ACTIVATED_MSG,
    _check_contact,
    _is_unactivated_phase2_patient,
    _tab_refusal,
    _touch_contact,
    _try_resolve_case,
)
from ehr_simulator.web.case_start import CaseStartRefusedError, start_next_case
from ehr_simulator.web.practice_start import PracticeRefusedError, start_practice_case
from ehr_simulator.web.route_support import (
    _HTTP_UNPROCESSABLE,
    _TELEMETRY_URL,
    Chrome,
    _back_to_index,
    _conflict,
    _error_flash,
    _form_refusal,
    _htmx_aware_redirect,
    _known_clinician,
    _mark_tab_conflict,
    _require_clinician,
    _timepoint_url,
)
from ehr_simulator.web.study_session import CaseConfiguration, is_phase2_mode, read_frontier
from ehr_simulator.web.tab_guard import ReleaseReason, TabGuardError
from ehr_simulator.web.telemetry import (
    TelemetryValidationError,
    UnclaimedRenderError,
    UnknownRenderError,
    parse_batch,
    parse_tab_request,
    record_batch,
)

router = APIRouter()

_CASE_START_INTEGRITY_MSG = "Start case refused: stored allocation integrity check failed"
_CASE_NOT_ACTIVE_MSG = "This case is not active"
_CASE_NOT_PAUSED_MSG = "This case is not paused"
_PAUSE_DISABLED_MSG = "Pausing is not enabled for this study"


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
        return _form_refusal(
            request,
            HTMLResponse(content=_error_flash(str(exc)), status_code=status.HTTP_409_CONFLICT),
        )
    except RandomisationIntegrityError as exc:
        get_logger().error(
            "start case integrity failure", event_kind="case.start.integrity", error=str(exc)
        )
        return _form_refusal(
            request,
            HTMLResponse(
                content=_error_flash(_CASE_START_INTEGRITY_MSG),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            ),
        )

    update_request_context(patient_id=started.patient_id)
    return _htmx_aware_redirect(
        request, _timepoint_url(started.patient_id, started.resume_t_index, chrome)
    )


@router.post("/practice/start")
async def practice_start(request: Request, chrome: Chrome = "epic") -> Response:
    """Resume or start a practice case (S11i); never a measured allocation."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)
    state = request.app.state

    try:
        started = start_practice_case(state.db, state, clinician_id=clinician_id or "")
    except (PracticeRefusedError, StaleConfigurationError) as exc:
        get_logger().warning(
            "practice start refused", event_kind="practice.start.refused", error=str(exc)
        )
        return _form_refusal(request, _conflict(str(exc)))

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

    contact, case, refusal = _lifecycle_request(request, clinician_id or "", patient_id)
    if refusal is not None:
        return refusal
    if contact.access is not CaseAccess.ACTIVE:  # type: ignore[union-attr]
        return _back_to_index(_conflict(_CASE_NOT_ACTIVE_MSG))

    tab_error = _tab_refusal(
        request, clinician_id=clinician_id or "", patient_id=patient_id, case=case, contact=contact
    )
    if tab_error is not None:
        return _mark_tab_conflict(_conflict(tab_error))

    _touch_contact(request, contact)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/case/{patient_id}/tab/claim")
async def case_tab_claim(request: Request, patient_id: str) -> Response:
    """S11m: grant the case lease to the posting tab's render (204) or 409."""
    clinician_id = _known_clinician(request)
    if clinician_id is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    update_request_context(clinician_id=clinician_id, patient_id=patient_id)

    try:
        body = parse_tab_request(await request.body())
    except TelemetryValidationError:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    contact, _case, refusal = _lifecycle_request(request, clinician_id, patient_id)
    if refusal is not None:
        return refusal
    if contact.access is not CaseAccess.ACTIVE:  # type: ignore[union-attr]
        return _conflict(_CASE_NOT_ACTIVE_MSG)

    try:
        tab_guard.claim(
            request.app.state.db,
            request.app.state,
            clinician_id=clinician_id,
            patient_id=patient_id,
            tab_id=body.tab_id,
            render_id=body.render_id,
        )
    except TabGuardError as exc:
        get_logger().warning("tab claim refused", event_kind="tab.refused", error=str(exc))
        return _mark_tab_conflict(_conflict(str(exc)))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/case/{patient_id}/tab/release")
async def case_tab_release(request: Request, patient_id: str) -> Response:
    """S11m: drop the lease if the posting render holds it (sendBeacon, 204)."""
    clinician_id = _known_clinician(request)
    if clinician_id is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        body = parse_tab_request(await request.body())
    except TelemetryValidationError:
        return Response(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)

    tab_guard.release(
        request.app.state.db,
        request.app.state,
        clinician_id=clinician_id,
        patient_id=patient_id,
        tab_id=body.tab_id,
        render_id=body.render_id,
        reason=ReleaseReason.PAGEHIDE,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(_TELEMETRY_URL)
async def telemetry_events(request: Request) -> Response:
    """S11j/S11k: store one browser telemetry batch (204).

    401 unknown clinician, 422 malformed, 409 unknown or foreign render;
    nothing written on any. 401 instead of the login redirect: ``fetch()``
    follows redirects and would read the login page as a successful upload.
    Not case contact: no lifecycle check, no ``last_seen_at`` touch.
    """
    clinician_id = _known_clinician(request)
    if clinician_id is None:
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)

    try:
        batch = parse_batch(await request.body())
        record_batch(
            request.app.state.db, request.app.state, clinician_id=clinician_id, batch=batch
        )
    except TelemetryValidationError as exc:
        get_logger().warning(
            "telemetry batch refused", event_kind="telemetry.refused", error=str(exc)
        )
        return Response(status_code=_HTTP_UNPROCESSABLE)
    except (UnknownRenderError, UnclaimedRenderError) as exc:
        get_logger().warning(
            "telemetry batch refused", event_kind="telemetry.unknown_render", error=str(exc)
        )
        return Response(status_code=status.HTTP_409_CONFLICT)
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
        return _form_refusal(request, refusal)
    if not case_contact.policy_for(case).pause_enabled:
        return _form_refusal(request, _conflict(_PAUSE_DISABLED_MSG))
    if contact.access is not CaseAccess.ACTIVE:  # type: ignore[union-attr]
        return _form_refusal(request, _conflict(_CASE_NOT_ACTIVE_MSG))

    tab_error = _tab_refusal(
        request,
        clinician_id=clinician_id or "",
        patient_id=patient_id,
        case=case,
        contact=contact,
        form=await request.form(),
    )
    if tab_error is not None:
        return _mark_tab_conflict(_form_refusal(request, _conflict(tab_error)))

    try:
        case_contact.pause_case(request.app.state.db, request.app.state, contact)  # type: ignore[arg-type]
    except CaseLifecycleError as exc:
        return _form_refusal(request, _conflict(str(exc)))

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
        return _form_refusal(request, refusal)
    if contact.access is not CaseAccess.PAUSED:  # type: ignore[union-attr]
        return _form_refusal(request, _conflict(_CASE_NOT_PAUSED_MSG))

    try:
        case_contact.resume_case(request.app.state.db, request.app.state, contact, case)  # type: ignore[arg-type]
    except CaseLifecycleError as exc:
        return _form_refusal(request, _conflict(str(exc)))

    return _htmx_aware_redirect(
        request, _frontier_url(request, clinician_id or "", patient_id, case, chrome)
    )
