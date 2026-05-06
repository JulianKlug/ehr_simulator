"""FastAPI app factory.

Parameterized factory (Decision **D1**): tests build their own app via
``create_app(log_dir=tmp_path, dataset_loader=fake_loader)``. Routes never
import ``app`` at module scope — they read ``request.app.state.dataset``
instead. Every test gets an isolated app, isolated ``logs/`` directory under
``tmp_path``, and a controllable dataset.

Lifespan (Decision **D17**):

1. ``setup_logging(log_dir)``;
2. try ``app.state.dataset = dataset_loader()`` (validate-once, cache parsed
   frames);
3. on :class:`AdapterError`: log ``app.boot.failed`` with the issues list,
   print remediation hint to stderr, raise :class:`SystemExit(1)`;
4. on success, emit ``app.boot``.

Middleware stack (outermost first):

1. Request-ID middleware: generate ``request_id``, bind to context vars,
   attach ``X-Request-ID`` response header.
2. Logging middleware: emit one log record per request after handler returns
   per Decision **D3** (HX-Request → ``panel.swap``; else ``page.render``;
   exception → ``page.error``).
"""

from __future__ import annotations

import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import matplotlib  # noqa: F401  (eager import so plotnine's first render is fast)
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ehr_simulator.ingestion.exceptions import AdapterError
from ehr_simulator.ingestion.synthetic import SyntheticDataset, load_synthetic
from ehr_simulator.logging import (
    bind_request_context,
    get_logger,
    new_request_id,
    reset_request_context,
    setup_logging,
)

_THIS_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"


def create_app(
    *,
    log_dir: Path = Path("logs"),
    dataset_loader: Callable[[], SyntheticDataset] = load_synthetic,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(log_dir)
        log = get_logger()
        try:
            app.state.dataset = dataset_loader()
        except AdapterError as exc:
            issues = [
                {
                    "dataset": i.dataset,
                    "patient_id": i.patient_id,
                    "row_idx": i.row_idx,
                    "reason": i.reason,
                }
                for i in exc.issues
            ]
            log.error("boot failed", event_kind="app.boot.failed", issues=issues, error=str(exc))
            print(
                "Synthetic dataset failed validation. "
                "Run `uv run pytest tests/test_synthetic.py` to see what's broken.",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc

        log.info("boot ok", event_kind="app.boot")
        try:
            yield
        finally:
            log.info("shutdown", event_kind="app.shutdown")

    app = FastAPI(lifespan=lifespan)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    from ehr_simulator.web.routes import router

    app.include_router(router)
    app.add_middleware(RequestContextMiddleware)
    return app


class RequestContextMiddleware:
    """Pure ASGI middleware so contextvars set by the route handler are visible
    when the middleware emits its per-request log line.

    Starlette's :class:`BaseHTTPMiddleware` runs the handler in a child task,
    which copies contextvars at task-creation time and isolates child writes
    from the parent — that's why this is implemented at the raw ASGI level.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        reset_request_context()
        request_id = new_request_id()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        is_htmx = headers.get("hx-request", "").lower() == "true"
        query_string = scope.get("query_string", b"").decode("latin-1")
        chrome = _parse_chrome(query_string)
        bind_request_context(
            request_id=request_id,
            event_kind="panel.swap" if is_htmx else "page.render",
            chrome=chrome,
        )

        status_code_holder: dict[str, int] = {"status": 0}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                status_code_holder["status"] = message["status"]
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-request-id", request_id.encode("latin-1")))
                message = {**message, "headers": response_headers}
            await send(message)

        log = get_logger()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            log.exception(
                "page.error",
                event_kind="page.error",
                error=repr(exc),
                path=scope.get("path"),
            )
            raise
        log.info(
            "request",
            path=scope.get("path"),
            status_code=status_code_holder["status"],
        )


def _parse_chrome(query_string: str) -> str:
    """Extract ``chrome=`` from a raw query string; default to ``dense``."""
    for part in query_string.split("&"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if key == "chrome":
            return value or "dense"
    return "dense"


app = create_app()
