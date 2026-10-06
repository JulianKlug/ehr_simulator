"""``/login``, ``/logout`` and the S11p ``/profile`` form.

The typed name becomes a pseudonymous ``clinician_id`` carried in an
unsigned cookie (``web/clinician_session.py`` → ``db/clinicians.py``).
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ehr_simulator.clinician_profile import FIELDS as PROFILE_FIELDS
from ehr_simulator.clinician_profile import MAX_YEARS_OF_PRACTICE, ProfileValidationError
from ehr_simulator.db import cookies
from ehr_simulator.db.clinician_profiles import ProfileLockedError
from ehr_simulator.logging import update_request_context
from ehr_simulator.web import clinician_session
from ehr_simulator.web.case_start import profile_missing
from ehr_simulator.web.clinician_profile_page import (
    ProfileContext,
    profile_config,
    profile_context,
    save_profile,
)
from ehr_simulator.web.route_support import (
    _HTTP_UNPROCESSABLE,
    _INDEX_URL,
    _LOGIN_URL,
    _error_flash,
    _known_clinician,
    _require_clinician,
)

router = APIRouter()

_PROFILE_URL = "/profile"
_NO_PROFILE_MSG = "This study does not collect clinician profiles"


@router.get(_LOGIN_URL, response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@router.post(_LOGIN_URL)
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
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    state = request.app.state
    clinician_id = clinician_session.log_in(state.db, state, raw_name)
    update_request_context(clinician_id=clinician_id)

    # S11p: a clinician without the required profile fills it in first.
    target = _PROFILE_URL if profile_missing(state.db, state, clinician_id) else _INDEX_URL
    response: Response = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    cookies.set_clinician_cookie(response, clinician_id)
    return response


def _profile_page(
    request: Request,
    ctx: ProfileContext,
    *,
    form: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = status.HTTP_200_OK,
) -> Response:
    """The form prefilled from the submission, else the stored profile."""
    stored = ctx.stored
    values = dict.fromkeys(PROFILE_FIELDS, "")
    if stored is not None:
        values.update(
            professional_role=stored.professional_role,
            years_of_practice=f"{stored.years_of_practice:g}",
            country_of_practice=stored.country_of_practice,
            primary_specialty=stored.primary_specialty or "",
        )
    values.update(form or {})
    return request.app.state.templates.TemplateResponse(
        request,
        "profile.html",
        {
            "ctx": ctx,
            "values": values,
            "errors": errors or {},
            "max_years": f"{MAX_YEARS_OF_PRACTICE:g}",
        },
        status_code=status_code,
    )


@router.get(_PROFILE_URL, response_class=HTMLResponse)
async def profile_get(request: Request) -> Response:
    """S11p: the clinician characteristics form (404 when not collected)."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    config = profile_config(request.app.state)
    if config is None:
        return HTMLResponse(
            content=_error_flash(_NO_PROFILE_MSG), status_code=status.HTTP_404_NOT_FOUND
        )
    return _profile_page(request, profile_context(request.app.state.db, config, clinician_id or ""))


@router.post(_PROFILE_URL)
async def profile_post(request: Request) -> Response:
    """S11p: store the characteristics; 422 invalid, 409 locked, 303 saved."""
    clinician_id, redirect = _require_clinician(request)
    if redirect is not None:
        return redirect
    update_request_context(clinician_id=clinician_id)
    state = request.app.state
    config = profile_config(state)
    if config is None:
        return HTMLResponse(
            content=_error_flash(_NO_PROFILE_MSG), status_code=status.HTTP_404_NOT_FOUND
        )

    raw = await request.form()
    form = {name: str(raw.get(name) or "") for name in PROFILE_FIELDS}
    try:
        save_profile(state.db, state, clinician_id=clinician_id or "", config=config, form=form)
    except ProfileValidationError as exc:
        ctx = profile_context(state.db, config, clinician_id or "")
        return _profile_page(
            request, ctx, form=form, errors=exc.errors, status_code=_HTTP_UNPROCESSABLE
        )
    except ProfileLockedError:
        ctx = profile_context(state.db, config, clinician_id or "")
        return _profile_page(request, ctx, status_code=status.HTTP_409_CONFLICT)
    return RedirectResponse(_INDEX_URL, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout_post(request: Request) -> Response:
    clinician_id = _known_clinician(request)
    if clinician_id is not None:
        clinician_session.log_out(request.app.state.db, request.app.state, clinician_id)
    response: Response = RedirectResponse(_LOGIN_URL, status_code=status.HTTP_303_SEE_OTHER)
    cookies.clear_clinician_cookie(response)
    return response
