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
"""

from __future__ import annotations

import json
import sqlite3
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
]
EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))


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
    """
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind {kind!r}")

    payload_json = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))
    try:
        cursor = conn.execute(
            "INSERT INTO events "
            "(session_id, clinician_id, patient_id, timepoint, kind, "
            " payload_json, client_ts, client_seq) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                clinician_id,
                patient_id,
                timepoint,
                kind,
                payload_json,
                client_ts,
                client_seq,
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
