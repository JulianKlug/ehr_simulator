"""S11j/S11k browser telemetry intake: validate a batch, bind it to renders.

``POST /telemetry/events`` hands the raw body here. The browser names only
the ``render_id`` of the view it is reporting on; session, patient,
timepoint and visit kind come from the server's own ``timepoint.render``
row, so a page can never claim another patient, timepoint or arm::

    browser batch ──► parse_batch (closed kinds, per kind payload models)
                  ──► record_batch (renders exist and belong to the clinician)
                  ──► events.append_browser_batch (one transaction, idempotent)

Telemetry is not case contact: nothing here touches ``last_seen_at`` or
the S11e lifecycle, and late events of a finished case are still stored.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
)

from ehr_simulator.db import events, telemetry

__all__ = [
    "MAX_BATCH_BYTES",
    "MAX_BATCH_EVENTS",
    "TelemetryBatch",
    "TelemetryValidationError",
    "UnknownRenderError",
    "parse_batch",
    "record_batch",
]

#: Largest accepted request body (the client batches far below it).
MAX_BATCH_BYTES = 64 * 1024

#: Most events in one batch (mirrors ``MAX_BATCH`` in static/telemetry.js).
MAX_BATCH_EVENTS = 100

#: ISO-8601 browser timestamps are short; anything longer is not one.
_CLIENT_TS_MAX_CHARS = 40

#: ``crypto.randomUUID()`` shape (also produced by the client fallback).
_TAB_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

#: Server ``render_id``: ``uuid4().hex``.
_RENDER_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")

#: Mirrors ``panel_exposure.PANEL_IDS`` (a lockstep test pins the two).
PanelId = Literal["admission", "vitals", "labs", "imaging", "ai"]
PanelState = Literal[
    "loading", "empty-expected", "empty-unexpected", "partial", "error", "unavailable"
]


class TelemetryValidationError(ValueError):
    """The batch is malformed; nothing was written (HTTP 422)."""


class UnknownRenderError(LookupError):
    """A ``render_id`` is unknown or belongs to another clinician (HTTP 409)."""


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Visibility(_Payload):
    visible: StrictBool
    focused: StrictBool


class _State(_Visibility):
    reason: Literal["visibilitychange", "focus", "blur", "periodic"]


class _Activity(_Payload):
    activity_kind: Literal["click", "touch", "scroll", "keyboard", "answer_change"]


class _Exit(_Payload):
    reason: Literal["swap", "pagehide"]


class _Gap(_Payload):
    dropped: Annotated[StrictInt, Field(ge=1)]


class _PanelMount(_Payload):
    panel_id: PanelId
    expanded: StrictBool
    collapsible: StrictBool
    state: PanelState


class _PanelViewport(_Payload):
    panel_id: PanelId
    intersection_ratio: Annotated[StrictFloat | StrictInt, Field(ge=0, le=1)]


class _PanelToggle(_Payload):
    panel_id: PanelId


_PAYLOAD_MODELS: dict[str, type[_Payload]] = {
    "browser.timepoint_enter": _Visibility,
    "browser.state": _State,
    "browser.activity": _Activity,
    "browser.timepoint_exit": _Exit,
    "browser.gap": _Gap,
    "panel.mount": _PanelMount,
    "panel.viewport": _PanelViewport,
    "panel.open": _PanelToggle,
    "panel.close": _PanelToggle,
}

BrowserKind = Literal[
    "browser.timepoint_enter",
    "browser.state",
    "browser.activity",
    "browser.timepoint_exit",
    "browser.gap",
    "panel.mount",
    "panel.viewport",
    "panel.open",
    "panel.close",
]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: BrowserKind
    render_id: str
    client_seq: Annotated[StrictInt, Field(ge=1)]
    client_mono_ms: StrictFloat | StrictInt
    client_ts: str | None = None
    payload: dict[str, Any]

    @field_validator("render_id")
    @classmethod
    def _render_id_shape(cls, v: str) -> str:
        if not _RENDER_ID_PATTERN.fullmatch(v):
            raise ValueError("render_id must be 32 lowercase hex characters")
        return v

    @field_validator("client_mono_ms")
    @classmethod
    def _mono_finite(cls, v: float) -> float:
        if not math.isfinite(v) or v < 0:
            raise ValueError(f"client_mono_ms must be finite and >= 0; got {v}")
        return float(v)

    @field_validator("client_ts")
    @classmethod
    def _ts_short(cls, v: str | None) -> str | None:
        if v is not None and len(v) > _CLIENT_TS_MAX_CHARS:
            raise ValueError("client_ts too long")
        return v


class TelemetryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tab_id: str
    events: Annotated[list[_Event], Field(min_length=1, max_length=MAX_BATCH_EVENTS)]

    @field_validator("tab_id")
    @classmethod
    def _tab_id_shape(cls, v: str) -> str:
        if not _TAB_ID_PATTERN.fullmatch(v):
            raise ValueError("tab_id must be a lowercase UUID v4")
        return v


def parse_batch(raw: bytes) -> TelemetryBatch:
    """Decode and validate one request body; raise on anything off contract.

    Each payload is checked against its kind's closed model, so a page can
    only ever store the fields listed there (no values, text or coordinates).
    """
    if len(raw) > MAX_BATCH_BYTES:
        raise TelemetryValidationError(f"batch larger than {MAX_BATCH_BYTES} bytes")

    try:
        batch = TelemetryBatch.model_validate(json.loads(raw))
        for event in batch.events:
            _PAYLOAD_MODELS[event.kind].model_validate(event.payload)
    except (ValueError, ValidationError) as exc:
        raise TelemetryValidationError(str(exc)) from exc
    return batch


def record_batch(
    conn: sqlite3.Connection, app_state: Any, *, clinician_id: str, batch: TelemetryBatch
) -> int:
    """Store a validated batch; return the number of new rows.

    Every event must name a render of ``clinician_id``; one foreign or
    unknown render refuses the whole batch before anything is written.
    """
    renders = telemetry.fetch_renders(conn, (e.render_id for e in batch.events))
    for event in batch.events:
        render = renders.get(event.render_id)
        if render is None or render.clinician_id != clinician_id:
            raise UnknownRenderError(f"unknown render {event.render_id}")

    rows = [
        events.BrowserEvent(
            session_id=renders[e.render_id].session_id,
            clinician_id=clinician_id,
            patient_id=renders[e.render_id].patient_id,
            timepoint=renders[e.render_id].timepoint,
            kind=e.kind,
            payload=e.payload,
            client_ts=e.client_ts,
            client_seq=e.client_seq,
            tab_id=batch.tab_id,
            render_id=e.render_id,
            client_mono_ms=e.client_mono_ms,
        )
        for e in batch.events
    ]
    return events.append_browser_batch(conn, rows, app_state=app_state)
