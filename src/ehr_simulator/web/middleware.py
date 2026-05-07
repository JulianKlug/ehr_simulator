"""Content-Security-Policy ASGI middleware.

Mirrors the bare-ASGI shape of :class:`RequestContextMiddleware` so contextvars
remain visible — see ``web/app.py`` module docstring for the rationale.

Locks the CSP for v1.0 open-source release surface (per TODOS.md plan-eng-review
on session-02-thin-ui-synthetic.md). Per /plan-eng-review issue 2.1,
``script-src`` is ``'self'``-only: zero inline ``<script>`` blocks and zero
``on*=`` event handlers across ``web/templates/`` were verified, and htmx's
``hx-*`` attributes are HTML data-attributes processed by the loaded htmx
library (external) — they don't require ``'unsafe-inline'``.

Only ``style-src 'unsafe-inline'`` remains permissive: plotnine emits inline
``<style>`` blocks inside its SVG output. This is the smallest viable CSP
today; the v1.0 hardening path is to switch plotnine SVG output to a hashed
external stylesheet (TODOS.md S2 carryover, revisited at S8 alongside the
inline-SVG payload measurement).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'"
)

_CSP_HEADER_BYTES = _CSP_HEADER_VALUE.encode("latin-1")


class CSPMiddleware:
    """Inject the Content-Security-Policy response header on every HTTP response."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"content-security-policy", _CSP_HEADER_BYTES))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
