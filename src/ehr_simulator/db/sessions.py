"""sessions DAO: per-(clinician, patient) session lifecycle.

A "session" is the bounded interaction window during which a single
clinician walks a single patient's timepoints. S6 ships ``start_or_resume``
(the open path); S9a adds ``find_open`` so the service layer can tell
"resumed" from "created"; S9b adds ``close`` (the final-timepoint
``/advance`` sets ``ended_at``) and ``find_latest`` (a completed patient's
read-only revisit re-points at the closed session instead of opening one
nothing could ever close).

Migration 2 (``ux_sessions_open``) makes "at most one open session per
(clinician, patient)" a schema invariant rather than a check-then-insert
discipline.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from ehr_simulator.db.answers import _require_version_provenance


@dataclass(frozen=True)
class SessionRow:
    """One materialized ``sessions`` row (S11b provenance reads)."""

    session_id: str
    clinician_id: str
    patient_id: str
    arm: str
    config_hash: str
    config_version: str | None = None
    ended_at: object | None = None


_FETCH_PAIR_SQL = (
    "SELECT session_id, clinician_id, patient_id, arm, config_hash, config_version, ended_at "
    "FROM sessions WHERE clinician_id = ? AND patient_id = ? "
    "ORDER BY started_at DESC, rowid DESC LIMIT 1"
)


def fetch_for_pair(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str
) -> SessionRow | None:
    """The pair's most recent session (open or closed), with provenance."""
    row = conn.execute(_FETCH_PAIR_SQL, (clinician_id, patient_id)).fetchone()
    if row is None:
        return None
    return SessionRow(*row)


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
    config_version: str | None = None,
    commit: bool = True,
) -> str:
    """Return the open ``session_id`` for ``(clinician_id, patient_id)``,
    creating a new row if no open session exists.

    S11b: the new row is stamped with the case's provenance pair
    (``config_version``, ``config_hash``); an existing open session is
    returned as-is (its provenance was fixed when it opened).

    S11d: ``commit=False`` leaves the INSERT in the caller's transaction
    (Start case commits it together with the activation).
    """
    existing = find_open(conn, clinician_id, patient_id)
    if existing is not None:
        return existing

    _require_version_provenance(conn, config_version)
    session_id = uuid4().hex
    conn.execute(
        "INSERT INTO sessions (session_id, clinician_id, patient_id, arm, "
        "config_hash, config_version) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (session_id, clinician_id, patient_id, arm, config_hash, config_version),
    )
    if commit:
        conn.commit()
    return session_id


def find_latest(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> str | None:
    """Return the most recent ``session_id`` for the pair, open **or** closed."""
    row = conn.execute(
        "SELECT session_id FROM sessions "
        "WHERE clinician_id = ? AND patient_id = ? "
        "ORDER BY started_at DESC, rowid DESC LIMIT 1",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else row[0]


def close(conn: sqlite3.Connection, session_id: str, *, commit: bool = True) -> int:
    """Set ``ended_at`` on an open session; return the rowcount (0 if already closed).

    Frees the pair under ``ux_sessions_open``.

    S10: ``commit=False`` leaves the UPDATE in the connection's open
    transaction so the final advance can commit it together with
    ``advance.ok`` / ``timepoint.exit`` / ``session.end`` (see
    ``web/gating.py``); a failed event write rolls the close back.
    """
    cursor = conn.execute(
        "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP "
        "WHERE session_id = ? AND ended_at IS NULL",
        (session_id,),
    )
    if commit:
        conn.commit()
    return cursor.rowcount
