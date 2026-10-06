"""HTTP helpers shared by every router in ``web/``: HTMX detection,
redirects, refusal flashes, the clinician cookie check and the display name.

No route lives here; ``routes``, ``auth_routes``, ``case_routes``,
``case_request`` and ``patient_view`` import these helpers.
"""

from __future__ import annotations

from html import escape
from typing import Literal

from fastapi import Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from ehr_simulator.db import cookies
from ehr_simulator.web import clinician_session, tab_guard

_HX_REQUEST_HEADER = "hx-request"
_HX_HISTORY_RESTORE_HEADER = "hx-history-restore-request"
_INDEX_URL = "/"
_LOGIN_URL = "/login"
_HTTP_UNPROCESSABLE = status.HTTP_422_UNPROCESSABLE_CONTENT
_TELEMETRY_URL = "/telemetry/events"

Chrome = Literal["dense", "epic"]


def _is_htmx(request: Request) -> bool:
    return request.headers.get(_HX_REQUEST_HEADER, "").lower() == "true"


def _is_history_restore(request: Request) -> bool:
    return request.headers.get(_HX_HISTORY_RESTORE_HEADER, "").lower() == "true"


def _timepoint_url(patient_id: str, t_index: int, chrome: str) -> str:
    return f"/patient/{patient_id}/timepoint/{t_index}?chrome={chrome}"


def _htmx_aware_redirect(request: Request, url: str) -> Response:
    """303 for browsers; 200 + ``HX-Redirect`` so htmx swaps the whole page."""
    if _is_htmx(request):
        return Response(status_code=status.HTTP_200_OK, headers={"HX-Redirect": url})
    return RedirectResponse(url, status_code=status.HTTP_303_SEE_OTHER)


def _back_to_index(response: Response) -> Response:
    """Refusal whose ``HX-Redirect`` sends htmx back to the index (S11e)."""
    response.headers["HX-Redirect"] = _INDEX_URL
    return response


def _error_flash(message: str) -> str:
    # Messages echo URL input (patient ids): escape before embedding.
    return f'<div class="error-flash" role="alert">{escape(message, quote=False)}</div>'


def _conflict(message: str) -> HTMLResponse:
    return HTMLResponse(content=_error_flash(message), status_code=status.HTTP_409_CONFLICT)


def _mark_tab_conflict(response: Response) -> Response:
    response.headers[tab_guard.CONFLICT_HEADER] = tab_guard.CONFLICT_VALUE
    return response


def _form_refusal(request: Request, response: Response) -> Response:
    """Start / Pause / Resume are plain forms: a refusal must not strand the
    browser on a bare flash, so non-htmx callers get a page linking back."""
    if _is_htmx(request):
        return response

    page = request.app.state.templates.TemplateResponse(
        request,
        "case_refused.html",
        {"flash_html": bytes(response.body).decode(), "logged_in_name": _logged_in_name(request)},
        status_code=response.status_code,
    )

    # Keep the refusal's HX-Redirect so the contract stays the same for any caller.
    redirect = response.headers.get("HX-Redirect")
    if redirect is not None:
        page.headers["HX-Redirect"] = redirect
    return page


def _known_clinician(request: Request) -> str | None:
    """The cookie's clinician when ``app.state.known_clinicians`` holds it.

    Zero DB cost (review-fix R11): a tampered cookie (16-hex string not in
    the set) misses the cache without touching the DB. Fetch/beacon
    endpoints answer ``None`` with 401, never the login redirect a
    ``fetch()`` would follow.
    """
    clinician_id = cookies.read_clinician_id(request)
    known = getattr(request.app.state, "known_clinicians", set())
    if clinician_id is None or clinician_id not in known:
        return None
    return clinician_id


def _require_clinician(request: Request) -> tuple[str | None, Response | None]:
    """Resolve the clinician cookie or build an HTMX-aware redirect.

    Returns ``(clinician_id, None)`` on success or ``(None, redirect)`` on
    failure. The redirect is HTMX-aware:

    - ``HX-Request: true`` → 200 + ``HX-Redirect: /login`` header (so HTMX
      swaps the full page rather than dropping a 303 into the swap target).
    - else → 303 → ``/login`` (browser follows).
    """
    clinician_id = _known_clinician(request)
    if clinician_id is None:
        return None, _htmx_aware_redirect(request, _LOGIN_URL)
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
    return clinician_session.display_name(db, clinician_id)


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


def _form_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
