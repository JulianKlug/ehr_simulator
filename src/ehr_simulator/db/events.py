"""events DAO: append-only audit log of researcher-meaningful actions.

``session_id`` is nullable (review-fix R1): clinician-level events
(``clinician.login``, ``clinician.logout``) pass ``session_id=None``;
patient-scoped events from S9a/b will pass a real session id. The
``ix_events_session_id`` index makes session-scoped joins fast.

``payload_json`` is canonicalized through ``json.dumps(payload,
sort_keys=True, separators=(",", ":"))`` so events from different boots
join cleanly across pilots.

Increments ``app_state.write_counter`` after every successful append so the
shutdown-time backup gate trips (review-fix R8). FK violations on
``session_id`` are wrapped as :class:`DbError`; the ``session_id=None``
path bypasses the FK check entirely.

``kind`` is closed over :data:`EventKind` (S9a; S9b adds the ``advance.*``,
``session.end`` and ``progress.reset`` kinds). To add a producer, append
its kind to the ``Literal``; ``append`` raises :class:`ValueError` on
anything else *before* touching the DB, so a typo fails the producer's
first test instead of silently forking the taxonomy.

S11j: browser telemetry rows carry ``tab_id``, ``render_id`` and
``client_mono_ms`` and go through :func:`append_browser_batch`, which skips
a re-delivered ``(render_id, tab_id, client_seq)``. Server events that name
a rendered view (``timepoint.render``) pass ``render_id`` to :func:`append`.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, get_args

from ehr_simulator.db.exceptions import DbError

EventKind = Literal[
    "clinician.login",
    "clinician.logout",
    "session.start",
    "session.end",
    "answer.upsert",
    "answer.clear",
    "advance.ok",
    "advance.blocked",
    "progress.reset",
    # S10: behavioural timing. ``timepoint.enter`` fires whenever a clinician
    # is shown a timepoint pane (GET render or the 200 advance that moves to
    # it — refreshes duplicate the enter, the exporter prefers the first valid
    # pairing); ``timepoint.exit`` fires exactly once per timepoint, written
    # by the winning advance (or the completing final advance). ``server_ts``
    # is the source of truth for the export's timing columns and the
    # divergence figure.
    "timepoint.enter",
    "timepoint.exit",
    # S11i: a read-only render behind the frontier (payload: t_index only).
    "timepoint.revisit",
    # S11d: Start case realised one planned schedule item (payload:
    # schedule_id, case_position, arm — never the clinician name).
    "case.activated",
    # S11e: lifecycle transitions. ``case.reconnected`` records a contact
    # after a heartbeat gap still inside the grace period; ``case.incomplete``
    # carries the structured reason (never the clinician name).
    "case.paused",
    "case.resumed",
    "case.reconnected",
    "case.completed",
    "case.incomplete",
    # S11f: an incomplete case received its replacement plan (payload:
    # replacement_id, replacement_patient_id, replacement_case_position —
    # never the planned arm).
    "case.replacement_planned",
    # S11i: a practice case started / was completed (payload: arm on start —
    # a fixed training presentation, not a randomised assignment).
    "practice.started",
    "practice.completed",
    # S11j: one per telemetry-pinned study render (payload: t_index,
    # visit_kind, S11l ai delivery); names the view by ``render_id``.
    "timepoint.render",
    # S11j browser telemetry (render bound; durations on client_mono_ms).
    "browser.timepoint_enter",
    "browser.state",
    "browser.activity",
    "browser.timepoint_exit",
    "browser.gap",
    # S11k panel exposure primitives.
    "panel.mount",
    "panel.viewport",
    "panel.open",
    "panel.close",
    # S11m tab ownership audit (``tab_id`` = the tab concerned, ``render_id``
    # = its render; categorical ``reason`` payload only).
    "tab.claimed",
    "tab.released",
    "tab.conflict",
    "tab.lease_expired",
]
EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))

_INSERT_SQL = (
    "INSERT INTO events "
    "(session_id, clinician_id, patient_id, timepoint, kind, payload_json, "
    " client_ts, client_seq, tab_id, render_id, client_mono_ms) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
# The only unique index on events is S11j's browser delivery key.
_INSERT_SKIP_DUPLICATE_SQL = _INSERT_SQL + " ON CONFLICT DO NOTHING"


@dataclass(frozen=True)
class BrowserEvent:
    """One validated browser telemetry row; context copied from its render."""

    session_id: str | None
    clinician_id: str
    patient_id: str | None
    timepoint: float | None
    kind: EventKind
    payload: dict[str, Any]
    client_ts: str | None
    client_seq: int
    tab_id: str
    render_id: str
    client_mono_ms: float


def _canonical(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))


def _check_kind(kind: str) -> None:
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")


#: S11m: operational identity that must never enter an event payload.
PROHIBITED_PAYLOAD_KEY = "name_normalized"


def _check_payload(payload: Any) -> None:
    """Refuse the normalised clinician name at any depth (S11m).

    ``{"a": [{"name_normalized": "x"}]}`` raises; the row's ``clinician_id``
    is the only clinician identity an event may carry.
    """
    if isinstance(payload, dict):
        if PROHIBITED_PAYLOAD_KEY in payload:
            raise ValueError(f"event payload must not contain {PROHIBITED_PAYLOAD_KEY!r}")
        for value in payload.values():
            _check_payload(value)
        return

    if isinstance(payload, list | tuple):
        for value in payload:
            _check_payload(value)


def _check_mono(client_mono_ms: float | None) -> None:
    if client_mono_ms is None:
        return
    if not math.isfinite(client_mono_ms) or client_mono_ms < 0:
        raise ValueError(f"client_mono_ms must be finite and >= 0; got {client_mono_ms!r}")


def append(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    clinician_id: str,
    patient_id: str | None,
    timepoint: float | None,
    kind: EventKind,
    payload: dict[str, Any] | None = None,
    client_ts: str | None = None,
    client_seq: int | None = None,
    render_id: str | None = None,
    tab_id: str | None = None,
    app_state: Any = None,
    commit: bool = True,
) -> int:
    """Insert one ``events`` row; return its autoincrement ``event_id``.

    S10: ``commit=False`` leaves the row in the connection's open
    transaction (legacy ``isolation_level=""`` mode: the DML began it)
    and defers the write-counter bump, so the caller can batch the state
    writes and the behavioral events of one advance into a single atomic
    ``conn.commit()`` (see ``web/gating.py``); a failed batch is discarded
    whole by ``conn.rollback()``.

    S11m: ``tab_id`` names the tab of a ``tab.*`` audit row; a payload
    holding ``name_normalized`` anywhere raises before the insert.
    """
    _check_kind(kind)
    _check_payload(payload)

    try:
        cursor = conn.execute(
            _INSERT_SQL,
            (
                session_id,
                clinician_id,
                patient_id,
                timepoint,
                kind,
                _canonical(payload),
                client_ts,
                client_seq,
                tab_id,
                render_id,
                None,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise DbError(str(exc)) from exc
    if commit:
        conn.commit()
    event_id = cursor.lastrowid
    if commit and app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    assert event_id is not None
    return event_id


def append_browser_batch(
    conn: sqlite3.Connection,
    rows: Sequence[BrowserEvent],
    *,
    app_state: Any = None,
) -> int:
    """S11j: insert one browser batch in one transaction; return rows written.

    Every row is validated before the first insert. A row whose
    ``(render_id, tab_id, client_seq)`` is already stored is skipped (a
    retried delivery), so a batch is idempotent. Any other failure rolls
    the whole batch back.
    """
    for row in rows:
        _check_kind(row.kind)
        _check_payload(row.payload)
        _check_mono(row.client_mono_ms)

    written = 0
    try:
        for row in rows:
            cursor = conn.execute(
                _INSERT_SKIP_DUPLICATE_SQL,
                (
                    row.session_id,
                    row.clinician_id,
                    row.patient_id,
                    row.timepoint,
                    row.kind,
                    _canonical(row.payload),
                    row.client_ts,
                    row.client_seq,
                    row.tab_id,
                    row.render_id,
                    row.client_mono_ms,
                ),
            )
            written += cursor.rowcount
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise DbError(str(exc)) from exc

    if written and app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return written

