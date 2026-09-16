"""Clinician-id cookie helpers.

The cookie is unsigned: S6's local-only threat model doesn't justify
HMAC/signing. Tampering means a clinician could write under another's
identity, which is not a threat in the controlled-pilot setting; the
``app.state.known_clinicians`` cache lookup in ``_require_clinician``
catches tampered IDs that don't exist in ``clinicians`` and redirects to
``/login`` (review-fix R11).

- Name: ``ehrsim_clinician_id``
- Value: the 16-hex-char ``clinician_id`` (the SHA256-truncated pseudonym).
- Flags: ``HttpOnly; SameSite=Strict; Path=/``. No ``Secure`` (localhost
  deployments only per design doc).
- Max-Age: 30 days. A pilot session may span days; the cookie outlasts the
  browser process.
"""

from __future__ import annotations

from fastapi import Request, Response

_COOKIE_NAME = "ehrsim_clinician_id"
_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def read_clinician_id(request: Request) -> str | None:
    return request.cookies.get(_COOKIE_NAME)


def set_clinician_cookie(response: Response, clinician_id: str) -> None:
    response.set_cookie(
        key=_COOKIE_NAME,
        value=clinician_id,
        max_age=_MAX_AGE,
        httponly=True,
        samesite="strict",
        path="/",
    )


def clear_clinician_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, path="/")
