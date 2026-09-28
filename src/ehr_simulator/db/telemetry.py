"""S11j telemetry reads: rendered views and their browser events.

A ``timepoint.render`` row names one rendered ``#patient-view`` by
``render_id``; browser rows point at it. Writes go through
``events.append`` (renders) and ``events.append_browser_batch`` (browser
batches); this module only reads.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RENDER_KIND",
    "RenderRow",
    "TelemetryRow",
    "enter_t_indices",
    "fetch_renders",
    "load_render_rows",
    "load_telemetry_rows",
    "session_config_hashes",
]

RENDER_KIND = "timepoint.render"
_ENTER_KIND = "timepoint.enter"

_RENDER_COLUMNS = (
    "event_id, render_id, session_id, clinician_id, patient_id, timepoint, payload_json"
)
# ``client_ts`` is declared TIMESTAMP but holds the browser's ISO string
# (``…T…Z``); the CAST keeps sqlite3's decltype converter from parsing it.
_TELEMETRY_COLUMNS = (
    "event_id, render_id, tab_id, kind, client_seq, client_mono_ms, "
    "CAST(client_ts AS TEXT), payload_json"
)


@dataclass(frozen=True)
class RenderRow:
    """One server-rendered view. ``payload``: ``t_index``, ``visit_kind``
    and the S11l ``ai`` delivery value."""

    event_id: int
    render_id: str
    session_id: str | None
    clinician_id: str
    patient_id: str | None
    timepoint: float | None
    payload: dict[str, Any]

    @property
    def t_index(self) -> int:
        return int(self.payload["t_index"])

    @property
    def visit_kind(self) -> str:
        return str(self.payload["visit_kind"])


@dataclass(frozen=True)
class TelemetryRow:
    """One browser event bound to a render."""

    event_id: int
    render_id: str
    tab_id: str
    kind: str
    client_seq: int
    client_mono_ms: float
    client_ts: str | None
    payload: dict[str, Any]


def _render(row: tuple[Any, ...]) -> RenderRow:
    event_id, render_id, session_id, clinician_id, patient_id, timepoint, payload_json = row
    return RenderRow(
        event_id=event_id,
        render_id=render_id,
        session_id=session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=timepoint,
        payload=json.loads(payload_json),
    )


def fetch_renders(conn: sqlite3.Connection, render_ids: Iterable[str]) -> dict[str, RenderRow]:
    """The render rows for ``render_ids`` (missing ids are absent)."""
    wanted = sorted(set(render_ids))
    if not wanted:
        return {}

    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT {_RENDER_COLUMNS} FROM events WHERE kind = ? AND render_id IN ({placeholders})",
        (RENDER_KIND, *wanted),
    ).fetchall()
    return {r.render_id: r for r in map(_render, rows)}


def load_render_rows(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> list[RenderRow]:
    """Every render of one clinician × patient, in render order."""
    rows = conn.execute(
        f"SELECT {_RENDER_COLUMNS} FROM events "
        "WHERE kind = ? AND clinician_id = ? AND patient_id = ? ORDER BY event_id",
        (RENDER_KIND, clinician_id, patient_id),
    ).fetchall()
    return [_render(r) for r in rows]


def load_telemetry_rows(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> list[TelemetryRow]:
    """Every browser event of one clinician × patient, in arrival order."""
    rows = conn.execute(
        f"SELECT {_TELEMETRY_COLUMNS} FROM events "
        "WHERE tab_id IS NOT NULL AND clinician_id = ? AND patient_id = ? ORDER BY event_id",
        (clinician_id, patient_id),
    ).fetchall()
    return [
        TelemetryRow(
            event_id=event_id,
            render_id=render_id,
            tab_id=tab_id,
            kind=kind,
            client_seq=client_seq,
            client_mono_ms=client_mono_ms,
            client_ts=client_ts,
            payload=json.loads(payload_json),
        )
        for (
            event_id,
            render_id,
            tab_id,
            kind,
            client_seq,
            client_mono_ms,
            client_ts,
            payload_json,
        ) in rows
    ]


def enter_t_indices(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> frozenset[int]:
    """S11l: timepoints with S10 ``timepoint.enter`` presentation evidence."""
    rows = conn.execute(
        "SELECT payload_json FROM events WHERE kind = ? AND clinician_id = ? AND patient_id = ?",
        (_ENTER_KIND, clinician_id, patient_id),
    ).fetchall()
    return frozenset(int(json.loads(r[0])["t_index"]) for r in rows)


def session_config_hashes(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> dict[str, str]:
    """S11l: ``{session_id: config_hash}`` of one clinician × patient."""
    rows = conn.execute(
        "SELECT session_id, config_hash FROM sessions WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchall()
    return {r[0]: r[1] for r in rows}
