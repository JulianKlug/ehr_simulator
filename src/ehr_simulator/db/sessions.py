"""sessions DAO: per-(clinician, patient) session lifecycle.

A "session" is the bounded interaction window during which a single
clinician walks a single patient's timepoints. S6 ships ``start_or_resume``
(the open path); S9a adds ``find_open`` so the service layer can tell
"resumed" from "created"; S9b will own the close path by setting
``ended_at`` on the final-timepoint ``/advance``.

Migration 2 (``ux_sessions_open``) makes "at most one open session per
(clinician, patient)" a schema invariant rather than a check-then-insert
discipline.
"""

from __future__ import annotations

import sqlite3
from uuid import uuid4


def find_open(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> str | None:
    """Return the open ``session_id`` for the pair, or ``None``."""
    row = conn.execute(
        "SELECT session_id FROM sessions "
        "WHERE clinician_id = ? AND patient_id = ? AND ended_at IS NULL "
        "ORDER BY started_at DESC LIMIT 1",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else row[0]


def start_or_resume(
    conn: sqlite3.Connection,
    clinician_id: str,
    patient_id: str,
    *,
    arm: str,
    config_hash: str,
) -> str:
    """Return the open ``session_id`` for ``(clinician_id, patient_id)``,
    creating a new row if no open session exists."""
    existing = find_open(conn, clinician_id, patient_id)
    if existing is not None:
        return existing

    session_id = uuid4().hex
    conn.execute(
        "INSERT INTO sessions (session_id, clinician_id, patient_id, arm, config_hash) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, clinician_id, patient_id, arm, config_hash),
    )
    conn.commit()
    return session_id
