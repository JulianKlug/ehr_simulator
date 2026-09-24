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

from ehr_simulator.db import arm_assignments
from ehr_simulator.db.backup import create_backup
from ehr_simulator.db.connection import AccessMode, connect, resolve_db_path
from ehr_simulator.db.exceptions import ConfigurationProvenanceError, StudyIdentityError
from ehr_simulator.db.ingestion_issues import record_batch as record_ingestion_issues
from ehr_simulator.db.migrations import apply_migrations
from ehr_simulator.db.study_identity import bind as bind_study_identity
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
        # S11a: study mode binds the database to exactly one study. A
        # mismatching or nonempty unbound legacy database fails startup
        # here — before the clinician cache, ingestion issues, or any
        # other application write (spec §Application boot).
        study_id = getattr(app.state, "study_id", None)
        if study_id is not None:
            try:
                bind_study_identity(app.state.db, study_id)
            except StudyIdentityError as exc:
                log.error(
                    "study identity refused; refusing to boot",
                    event_kind="app.boot.failed",
                    study_id=study_id,
                    error=repr(exc),
                )
                print(f"Refusing to boot: {exc}", file=sys.stderr)
                with contextlib.suppress(Exception):
                    app.state.db.close()
                raise SystemExit(1) from exc
        log.info(
            "db ready",
            event_kind="db.ready",
            db_path=str(db_path_resolved),
            migrations=versions,
        )

        # S11b: study mode must boot against the active configuration. Bare
        # non-study mode (app.state.study is None) is unchanged and skips this.
        if getattr(app.state, "study", None) is not None:
            try:
                _verify_active_configuration(app)
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "failed to validate active configuration",
                    event_kind="app.boot.failed",
                    error=repr(exc),
                )
                print(
                    f"Refusing to boot: {exc}",
                    file=sys.stderr,
                )
                with contextlib.suppress(Exception):
                    app.state.db.close()
                raise SystemExit(1) from exc

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
    # Study mode is opt-in via app_from_study_config; bare create_app() has
    # no questions to ask, so the pane stays hidden and /answer returns 409.
    app.state.study = None
    app.state.study_id = None
    app.state.questions = None
    app.state.config_hash = None
    app.state.config_version = None
    app.state.active_configuration = None
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    from ehr_simulator.web.routes import provenance_error_response, router

    app.include_router(router)
    app.add_exception_handler(ConfigurationProvenanceError, provenance_error_response)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(CSPMiddleware)
    return app


def _verify_active_configuration(app: FastAPI) -> None:
    """S11b boot gate: study mode boots against the active configuration.

    Refuses (raises) when:

    * no active configuration exists;
    * the supplied YAML's computed hash differs from the active hash;
    * the active history row belongs to another ``study_id``;
    * a persisted snapshot does not revalidate (integrity error);
    * the snapshot models' recomputed hash disagrees with the stored hash.

    On success sets ``app.state.config_version``, ``app.state.config_hash``
    (unchanged, but re-asserted equal) and ``app.state.active_configuration``.
    """
    from ehr_simulator.config import compute_config_hash_from_models
    from ehr_simulator.config.snapshot import (
        parse_questions_snapshot,
        parse_study_snapshot,
    )
    from ehr_simulator.db import config_history

    conn = app.state.db
    active = config_history.fetch_active(conn)
    if active is None:
        raise ValueError(
            "no active configuration: run "
            "`uv run ehr-simulator activate-config STUDY_CONFIG QUESTIONS "
            "--version ... --description ...` before starting the application"
        )
    supplied = app.state.config_hash
    if active.config_hash != supplied:
        raise ValueError(
            f"supplied config hash ({supplied}) differs from the active "
            f"configuration hash ({active.config_hash}); start the app on "
            "the configuration files matching "
            f"{active.config_version!r} or activate a new version"
        )
    if active.study_id != app.state.study_id:
        raise ValueError(
            f"active configuration belongs to study {active.study_id!r}, not {app.state.study_id!r}"
        )
    # Invalid stored snapshots are database integrity errors — never a fallback.
    study_snap = parse_study_snapshot(active.study_json)
    questions_snap = parse_questions_snapshot(active.questions_json)
    recomputed = compute_config_hash_from_models(study_snap, questions_snap)
    if recomputed != active.config_hash:
        raise ValueError(
            "stored configuration snapshots do not hash to the recorded "
            "config_hash (database integrity error)"
        )
    if recomputed != supplied:
        raise ValueError("supplied YAML no longer hashes to the active configuration")
    app.state.config_version = active.config_version
    app.state.config_hash = active.config_hash
    app.state.active_configuration = active


def _assigned_patient_ids(db_path: Path) -> tuple[str, ...]:
    """Patients of existing cases, read before the lifespan opens the DB.

    Read-only and never creates the file: a fresh study has no cases yet.
    """
    if not db_path.exists():
        return ()

    conn = connect(db_path, access=AccessMode.READ_ONLY)
    try:
        return arm_assignments.assigned_patient_ids(conn)
    finally:
        conn.close()


def app_from_study_config(
    study_path: Path,
    questions_path: Path,
    *,
    log_dir: Path = Path("logs"),
    db_path: Path | None = None,
    backup_dir: Path | None = None,
) -> FastAPI:
    """Build a FastAPI app whose dataset_loader and timepoints come from the study config.

    Also binds ``app.state.study`` / ``questions`` / ``config_hash`` (S9a):
    the questions pane renders from the parsed model and every ``answers``
    row carries the hash computed once here.

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
    from ehr_simulator.config.loader import compute_config_hash_from_models

    study = load_study_config(study_path)
    questions = load_questions(questions_path)
    resolved_db_path = db_path if db_path is not None else resolve_db_path(study)
    loader = build_dataset_loader(
        study, extra_patient_ids=lambda: _assigned_patient_ids(resolved_db_path)
    )
    app = create_app(
        log_dir=log_dir,
        dataset_loader=loader,
        db_path=resolved_db_path,
        backup_dir=backup_dir,
    )
    app.state.study_timepoints = list(study.timepoints_minutes)
    app.state.study_patient_ids = list(study.patient_ids)
    app.state.study = study
    app.state.study_id = study.study_id
    app.state.questions = questions
    app.state.config_hash = compute_config_hash_from_models(study, questions)
    # S9b: with no required question the advance gate is vacuous — say so.
    if not any(q.required for q in questions.questions):
        get_logger().warning(
            "no question is required; the advance gate never blocks",
            event_kind="questions.none_required",
            questions_path=str(questions_path),
        )
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
