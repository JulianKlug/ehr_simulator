"""Per-(clinician, patient) session bootstrap — the S6 §7.3 deferral.

A study "session" is the window during which one clinician walks one
patient's timepoints. S9a opens it lazily on first patient contact (GET or
POST); S9b will close it (``ended_at``) on the final-timepoint ``/advance``.

::

    bootstrap_session(conn, app_state, clinician_id, patient_id)
        │
        ├─ arm_assignments.assign_or_lookup   (locks the arm; stub → no_ai)
        ├─ sessions.find_open                 (resume?)
        │     └─ None → sessions.start_or_resume + events "session.start"
        └─ SessionContext(session_id, arm, config_hash)

Idempotent per pair: the steady state is two SELECTs and no writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ehr_simulator.db import arm_assignments, events, sessions


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    arm: str
    config_hash: str


def bootstrap_session(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
) -> SessionContext:
    """Return the open session for the pair, creating it (once) if needed.

    Callers guarantee study mode: ``app_state.config_hash`` is a ``str``.
    """
    config_hash: str = app_state.config_hash
    arm, _source = arm_assignments.assign_or_lookup(
        conn, clinician_id, patient_id, config_hash=config_hash
    )

    session_id = sessions.find_open(conn, clinician_id, patient_id)
    if session_id is None:
        session_id = sessions.start_or_resume(
            conn, clinician_id, patient_id, arm=arm, config_hash=config_hash
        )
        events.append(
            conn,
            session_id=session_id,
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoint=None,
            kind="session.start",
            payload={"arm": arm},
            app_state=app_state,
        )

    return SessionContext(session_id=session_id, arm=arm, config_hash=config_hash)
