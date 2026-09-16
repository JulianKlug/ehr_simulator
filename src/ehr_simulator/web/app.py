"""FastAPI app factory.

Parameterized factory (Decision **D1**): tests build their own app via
``create_app(log_dir=tmp_path, dataset_loader=fake_loader)``. Routes never
import ``app`` at module scope — they read ``request.app.state.dataset``
instead. Every test gets an isolated app, isolated ``logs/`` directory under
``tmp_path``, and a controllable dataset.

Lifespan (Decision **D17** + S6 growth):

1. ``setup_logging(log_dir)``; init ``app.state.boot_id`` (uuid4 hex) and
   ``app.state.write_counter = 0`` (the backup-gate counter).
2. try ``app.state.dataset = dataset_loader()`` (validate-once, cache parsed
   frames);
3. on :class:`AdapterError`: log ``app.boot.failed`` with the issues list,
   print remediation hint to stderr, raise :class:`SystemExit(1)`;
4. on any other :class:`Exception` (review-fix R9): log ``app.boot.failed``,
   raise :class:`SystemExit(1)`. No DB file is created.
5. on success: ``connect(db_path)`` → ``apply_migrations`` →
   ``known_clinicians`` cache from ``SELECT clinician_id FROM clinicians`` →
   ``ingestion_issues.record_batch`` (if dataset exposes ``.issues``) →
   emit ``app.boot``.
6. on shutdown: if ``app.state.write_counter > 0`` (review-fix R8), call
   :func:`create_backup`; otherwise emit ``db.backup.skipped``. Close the
   connection.

Middleware stack (outermost first) unchanged.
"""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import matplotlib  # noqa: F401  (eager import so plotnine's first render is fast)
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ehr_simulator.db.backup import create_backup
from ehr_simulator.db.connection import connect, resolve_db_path
from ehr_simulator.db.ingestion_issues import record_batch as record_ingestion_issues
from ehr_simulator.db.migrations import apply_migrations
from ehr_simulator.ingestion.exceptions import AdapterError
from ehr_simulator.ingestion.synthetic import load_synthetic
from ehr_simulator.logging import (
    bind_request_context,
    get_logger,
    new_request_id,
    reset_request_context,
    setup_logging,
)
from ehr_simulator.web.middleware import CSPMiddleware
from ehr_simulator.web.panels import DatasetLike

_THIS_DIR = Path(__file__).resolve().parent
_TEMPLATES_DIR = _THIS_DIR / "templates"
_STATIC_DIR = _THIS_DIR / "static"


def create_app(
    *,
    log_dir: Path = Path("logs"),
    dataset_loader: Callable[[], DatasetLike] = load_synthetic,
    db_path: Path | None = None,
    backup_dir: Path | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(log_dir)
        log = get_logger()
        app.state.boot_id = uuid4().hex
        app.state.write_counter = 0
        app.state.known_clinicians = set()

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
        except Exception as exc:
            log.error("boot failed", event_kind="app.boot.failed", error=repr(exc))
            raise SystemExit(1) from exc

        db_path_resolved = db_path if db_path is not None else resolve_db_path(None)
        db_path_resolved.parent.mkdir(parents=True, exist_ok=True)
        app.state.db_path = db_path_resolved
        app.state.backup_dir = (
            backup_dir if backup_dir is not None else db_path_resolved.parent / "backups"
        )
        app.state.db = connect(db_path_resolved)
        versions = apply_migrations(app.state.db)
        log.info(
            "db ready",
            event_kind="db.ready",
            db_path=str(db_path_resolved),
            migrations=versions,
        )

        app.state.known_clinicians = {
            row[0] for row in app.state.db.execute("SELECT clinician_id FROM clinicians")
        }

        issues = getattr(app.state.dataset, "issues", None) or []
        if issues:
            dataset_name = issues[0].dataset
            n = record_ingestion_issues(
                app.state.db,
                dataset_name,
                app.state.boot_id,
                issues,
            )
            log.info(
                "ingestion issues recorded",
                event_kind="db.ingestion_issues.recorded",
                count=n,
            )

        log.info("boot ok", event_kind="app.boot")
        try:
            yield
        finally:
            try:
                if app.state.write_counter > 0:
                    dest = create_backup(db_path_resolved, app.state.backup_dir)
                    log.info(
                        "backup ok",
                        event_kind="db.backup.ok",
                        dest=str(dest),
                    )
                else:
                    log.info(
                        "backup skipped (no writes)",
                        event_kind="db.backup.skipped",
                    )
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "backup failed",
                    event_kind="db.backup.failed",
                    error=repr(exc),
                )
            with contextlib.suppress(Exception):
                app.state.db.close()
            log.info("shutdown", event_kind="app.shutdown")

    app = FastAPI(lifespan=lifespan)
    app.state.templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    from ehr_simulator.web.routes import router

    app.include_router(router)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CSPMiddleware)
    return app


def app_from_study_config(
    study_path: Path,
    questions_path: Path,
    *,
    log_dir: Path = Path("logs"),
    db_path: Path | None = None,
    backup_dir: Path | None = None,
) -> FastAPI:
    """Build a FastAPI app whose dataset_loader and timepoints come from the study config.

    Sets ``app.state.study_timepoints`` to ``study.timepoints_minutes`` so
    ``routes.patient_timepoint`` resolves the URL ordinal ``t_index`` against
    the **study-defined** timepoints, not against dataset-derived ones (per
    /plan-eng-review issue 1.2 — closes the silent study-validity bug where
    Geneva's 24+ distinct timepoints would otherwise shadow a 3-timepoint
    pilot study).

    The synthetic-only ``serve`` path (no ``--config``) does NOT call this
    function and therefore leaves ``app.state.study_timepoints`` unset; the
    routes fall back to ``patient_timepoints(dataset, pid)`` in that case.
    """
    from ehr_simulator.cli_support import build_dataset_loader
    from ehr_simulator.config import load_questions, load_study_config

    study = load_study_config(study_path)
    load_questions(questions_path)  # validate shape; the parsed model isn't wired up until S9
    loader = build_dataset_loader(study)
    resolved_db_path = db_path if db_path is not None else resolve_db_path(study)
    app = create_app(
        log_dir=log_dir,
        dataset_loader=loader,
        db_path=resolved_db_path,
        backup_dir=backup_dir,
    )
    app.state.study_timepoints = list(study.timepoints_minutes)
    app.state.study_patient_ids = list(study.patient_ids)
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
