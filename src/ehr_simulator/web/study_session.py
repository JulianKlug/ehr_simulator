"""Per-(clinician, patient) session bootstrap + walk frontier (S6 §7.3, S9a, S9b).

A study "session" is the window during which one clinician walks one
patient's timepoints. S9a opens it lazily on first patient contact; S9b's
final ``/advance`` closes it (``ended_at``).

Two halves, split on purpose (S9b review-fix R21)::

    read_frontier(conn, app_state, clinician_id, patient_id)   PURE READ
        └─ progress.fetch → Frontier(unlocked_t_index, completed)
           (clamped to the study's last index; drift → WARNING)

    bootstrap_session(conn, app_state, clinician_id, patient_id, frontier)   WRITES
        ├─ arm_assignments.assign_or_lookup   (locks the arm; stub → no_ai)
        ├─ sessions.find_open                 (resume?)
        │     ├─ None + completed → sessions.find_latest (re-point, no insert)
        │     └─ None → sessions.start_or_resume + events "session.start"
        └─ SessionContext(session_id, arm, config_hash, frontier)

The GET gate decides on ``read_frontier`` alone, so a request past the
frontier is bounced before anything is written — no arm lock, no session
row, no event. Steady state for a viewable request: three SELECTs, no writes.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from ehr_simulator.db import arm_assignments, events, progress, sessions
from ehr_simulator.logging import get_logger

NOT_STARTED_T_INDEX = 0


@dataclass(frozen=True)
class Frontier:
    unlocked_t_index: int
    completed: bool


@dataclass(frozen=True)
class SessionContext:
    session_id: str
    arm: str
    config_hash: str
    frontier: Frontier


def read_frontier(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
) -> Frontier:
    """The pair's walk frontier; a missing row is "not started".

    Two WARNING-only guards: a frontier past the study's last index (the
    timepoint list shrank under a live DB) is clamped, and a row recorded
    under a different ``config_hash`` is reported. Neither can 500.
    """
    row = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    if row is None:
        return Frontier(unlocked_t_index=NOT_STARTED_T_INDEX, completed=False)

    unlocked = row.unlocked_t_index
    # The test stub has no study_timepoints; skip the clamp rather than crash.
    timepoints = getattr(app_state, "study_timepoints", None)
    if timepoints:
        last = len(timepoints) - 1
        if unlocked > last:
            get_logger().warning(
                "progress beyond study timepoints; clamped",
                event_kind="progress.clamped",
                unlocked_t_index=unlocked,
                last_t_index=last,
            )
            unlocked = last

    live_hash: str | None = getattr(app_state, "config_hash", None)
    if live_hash is not None and row.config_hash != live_hash:
        get_logger().warning(
            "progress recorded under a different config",
            event_kind="progress.config_hash.drift",
            row_config_hash=row.config_hash,
            live_config_hash=live_hash,
        )

    return Frontier(unlocked_t_index=unlocked, completed=row.completed_at is not None)


def bootstrap_session(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    frontier: Frontier,
) -> SessionContext:
    """Return the pair's session, creating it (once) if needed.

    Callers guarantee study mode: ``app_state.config_hash`` is a ``str``.
    A completed walk re-points at its last (closed) session instead of
    opening one nothing could ever close (review-fix R12).
    """
    config_hash: str = app_state.config_hash
    arm, _source = arm_assignments.assign_or_lookup(
        conn, clinician_id, patient_id, config_hash=config_hash
    )

    session_id = sessions.find_open(conn, clinician_id, patient_id)
    if session_id is None and frontier.completed:
        session_id = sessions.find_latest(conn, clinician_id, patient_id)
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

    return SessionContext(
        session_id=session_id, arm=arm, config_hash=config_hash, frontier=frontier
    )
