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
from typing import Any

from ehr_simulator.domain_types import (
    CLAIMED_KIND,
    CONFLICT_KIND,
    LEASE_EXPIRED_KIND,
    RELEASED_KIND,
    RenderRow,
    TabAuditRow,
    TelemetryRow,
)

__all__ = [
    "BROWSER_KINDS",
    "CLAIMED_KIND",
    "CONFLICT_KIND",
    "LEASE_EXPIRED_KIND",
    "RELEASED_KIND",
    "RENDER_KIND",
    "RenderRow",
    "TabAuditRow",
    "TelemetryRow",
    "claimed_tabs",
    "enter_t_indices",
    "fetch_renders",
    "load_render_rows",
    "load_tab_audit",
    "load_tab_render_ids",
    "load_telemetry_rows",
    "session_config_hashes",
]

RENDER_KIND = "timepoint.render"
_ENTER_KIND = "timepoint.enter"

#: The browser-reported kinds (``web/telemetry.py`` payload models; a
#: lockstep test pins the two). S11m ``tab.*`` rows also carry ``tab_id``
#: but are server audit, never timeline input.
BROWSER_KINDS = (
    "browser.timepoint_enter",
    "browser.state",
    "browser.activity",
    "browser.timepoint_exit",
    "browser.gap",
    "panel.mount",
    "panel.viewport",
    "panel.open",
    "panel.close",
)
_BROWSER_KIND_PLACEHOLDERS = ", ".join("?" for _ in BROWSER_KINDS)

_RENDER_COLUMNS = (
    "event_id, render_id, session_id, clinician_id, patient_id, timepoint, payload_json"
)
# ``client_ts`` is declared TIMESTAMP but holds the browser's ISO string
# (``…T…Z``); the CAST keeps sqlite3's decltype converter from parsing it.
_TELEMETRY_COLUMNS = (
    "event_id, render_id, tab_id, kind, client_seq, client_mono_ms, "
    "CAST(client_ts AS TEXT), payload_json"
)


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
        f"WHERE kind IN ({_BROWSER_KIND_PLACEHOLDERS}) AND clinician_id = ? AND patient_id = ? "
        "ORDER BY event_id",
        (*BROWSER_KINDS, clinician_id, patient_id),
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


def claimed_tabs(conn: sqlite3.Connection, render_ids: Iterable[str]) -> dict[str, frozenset[str]]:
    """S11m: ``{render_id: tabs ever granted its lease}`` (missing = none)."""
    wanted = sorted(set(render_ids))
    if not wanted:
        return {}

    placeholders = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT render_id, tab_id FROM events WHERE kind = ? AND render_id IN ({placeholders})",
        (CLAIMED_KIND, *wanted),
    ).fetchall()
    out: dict[str, set[str]] = {}
    for render_id, tab_id in rows:
        out.setdefault(render_id, set()).add(tab_id)
    return {k: frozenset(v) for k, v in out.items()}


def load_tab_audit(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> list[TabAuditRow]:
    """Every ``tab.*`` row of one clinician × patient, in event order."""
    rows = conn.execute(
        "SELECT event_id, kind, tab_id, render_id FROM events "
        "WHERE kind LIKE 'tab.%' AND clinician_id = ? AND patient_id = ? ORDER BY event_id",
        (clinician_id, patient_id),
    ).fetchall()
    return [TabAuditRow(*row) for row in rows]


def load_tab_render_ids(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str, kind: str
) -> frozenset[str]:
    """S11m: renders of one clinician × patient named by a ``tab.*`` ``kind``."""
    rows = conn.execute(
        "SELECT DISTINCT render_id FROM events "
        "WHERE kind = ? AND clinician_id = ? AND patient_id = ? AND render_id IS NOT NULL",
        (kind, clinician_id, patient_id),
    ).fetchall()
    return frozenset(r[0] for r in rows)


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
