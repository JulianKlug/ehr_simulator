"""Structured logging for the EHR simulator.

One processor chain, one renderer (``JSONRenderer``), two stdlib handlers:

1. :class:`logging.handlers.TimedRotatingFileHandler` rolling at UTC midnight,
   producing ``<log_dir>/current.jsonl`` and ``current.jsonl.YYYY-MM-DD`` for
   rotated files. (Decision **D2**.)
2. ``StreamHandler(sys.stderr)`` for live debug.

Eight mandatory bound fields appear on every record (Decisions **D4**, **D13**):
``request_id``, ``clinician_id``, ``patient_id``, ``timepoint``,
``timepoint_index``, ``event_kind``, ``chrome``, ``arm``. Missing fields are
emitted as ``null``, never absent — keeps the JSONL schema stable for
downstream tooling.

``timepoint`` is bound to ``t_minutes`` (real clinical time, study-config-agnostic).
``timepoint_index`` is the URL ordinal, audit-only. Analysts join on
``timepoint``. (Decision **D13**.)

``setup_logging`` is idempotent: calling it twice removes prior handlers
before reattaching.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import uuid
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_clinician_id_var: ContextVar[str | None] = ContextVar("clinician_id", default=None)
_patient_id_var: ContextVar[str | None] = ContextVar("patient_id", default=None)
_timepoint_var: ContextVar[float | None] = ContextVar("timepoint", default=None)
_timepoint_index_var: ContextVar[int | None] = ContextVar("timepoint_index", default=None)
_event_kind_var: ContextVar[str | None] = ContextVar("event_kind", default=None)
_chrome_var: ContextVar[str | None] = ContextVar("chrome", default=None)
_arm_var: ContextVar[str | None] = ContextVar("arm", default=None)

_LOGGER_NAME = "ehr_simulator"
_LOG_FILENAME = "current.jsonl"


def _inject_context(
    _logger: object, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Inject the eight mandatory ContextVars into every event dict.

    Missing values are emitted as ``None`` (rendered as JSON ``null``) so the
    JSONL schema stays stable.
    """
    event_dict.setdefault("request_id", _request_id_var.get())
    event_dict.setdefault("clinician_id", _clinician_id_var.get())
    event_dict.setdefault("patient_id", _patient_id_var.get())
    event_dict.setdefault("timepoint", _timepoint_var.get())
    event_dict.setdefault("timepoint_index", _timepoint_index_var.get())
    event_dict.setdefault("event_kind", _event_kind_var.get())
    event_dict.setdefault("chrome", _chrome_var.get())
    event_dict.setdefault("arm", _arm_var.get())
    return event_dict


def setup_logging(log_dir: Path) -> structlog.stdlib.BoundLogger:
    """Boot structlog. Idempotent. Writes JSONL to ``<log_dir>/current.jsonl`` + stderr.

    ``log_dir`` is required (no default) so tests pass ``tmp_path`` and the
    production entry point passes ``Path("logs")``. (Decision **D1**.)
    """
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / _LOG_FILENAME),
        when="midnight",
        utc=True,
        backupCount=0,
        encoding="utf-8",
        delay=True,
    )
    file_handler.suffix = "%Y-%m-%d"
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))

    stdlib_logger = logging.getLogger(_LOGGER_NAME)
    for old in list(stdlib_logger.handlers):
        stdlib_logger.removeHandler(old)
        with contextlib.suppress(Exception):
            old.close()
    stdlib_logger.addHandler(file_handler)
    stdlib_logger.addHandler(stream_handler)
    stdlib_logger.setLevel(logging.INFO)
    stdlib_logger.propagate = False

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _inject_context,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    return structlog.get_logger(_LOGGER_NAME)


def get_logger() -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(_LOGGER_NAME)


def bind_request_context(
    *,
    request_id: str,
    patient_id: str | None = None,
    timepoint: float | None = None,
    timepoint_index: int | None = None,
    event_kind: str | None = None,
    chrome: str | None = None,
    arm: str | None = None,
    clinician_id: str | None = None,
) -> None:
    """Bind per-request fields. ``clinician_id`` stays None until S5; ``arm``
    stays None until S11. ``chrome`` is bound from the route's query param.
    (Decision **D4**.)"""
    _request_id_var.set(request_id)
    _clinician_id_var.set(clinician_id)
    _patient_id_var.set(patient_id)
    _timepoint_var.set(timepoint)
    _timepoint_index_var.set(timepoint_index)
    _event_kind_var.set(event_kind)
    _chrome_var.set(chrome)
    _arm_var.set(arm)


def set_event_kind(event_kind: str) -> None:
    _event_kind_var.set(event_kind)


_UNSET: Any = object()


def update_request_context(
    *,
    patient_id: str | None = _UNSET,
    timepoint: float | None = _UNSET,
    timepoint_index: int | None = _UNSET,
    event_kind: str | None = _UNSET,
    chrome: str | None = _UNSET,
    arm: str | None = _UNSET,
    clinician_id: str | None = _UNSET,
) -> None:
    """Patch a subset of ContextVars without disturbing the others.

    Used by route handlers to add ``patient_id``/``timepoint``/``timepoint_index``
    after the middleware has already bound ``request_id``/``event_kind``/``chrome``.
    """
    if patient_id is not _UNSET:
        _patient_id_var.set(patient_id)
    if timepoint is not _UNSET:
        _timepoint_var.set(timepoint)
    if timepoint_index is not _UNSET:
        _timepoint_index_var.set(timepoint_index)
    if event_kind is not _UNSET:
        _event_kind_var.set(event_kind)
    if chrome is not _UNSET:
        _chrome_var.set(chrome)
    if arm is not _UNSET:
        _arm_var.set(arm)
    if clinician_id is not _UNSET:
        _clinician_id_var.set(clinician_id)


def reset_request_context() -> None:
    _request_id_var.set(None)
    _clinician_id_var.set(None)
    _patient_id_var.set(None)
    _timepoint_var.set(None)
    _timepoint_index_var.set(None)
    _event_kind_var.set(None)
    _chrome_var.set(None)
    _arm_var.set(None)


def new_request_id() -> str:
    return uuid.uuid4().hex
