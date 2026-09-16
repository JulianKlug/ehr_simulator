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

``kind`` is closed over :data:`EventKind` (S9a). To add a producer, append
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
    "answer.upsert",
    "answer.clear",
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
) -> int:
    """Insert one ``events`` row; return its autoincrement ``event_id``."""
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
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise DbError(str(exc)) from exc
    event_id = cursor.lastrowid
    if app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    assert event_id is not None
    return event_id
