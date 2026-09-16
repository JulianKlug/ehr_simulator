"""``web/study_session`` (specs/session-09a §9 #11, #12; session-09b §9 #19-#20b)."""

from __future__ import annotations

import sqlite3

from structlog.testing import capture_logs

from ehr_simulator.db import clinicians, progress, sessions
from ehr_simulator.web.study_session import (
    Frontier,
    SessionContext,
    bootstrap_session,
    read_frontier,
)


class _AppState:
    write_counter = 0
    config_hash = "cfg"
    study_timepoints = [0.0, 60.0, 180.0]


def _counts(db: sqlite3.Connection) -> tuple[int, int, int]:
    sessions_n = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    arms_n = db.execute("SELECT COUNT(*) FROM arm_assignments").fetchone()[0]
    starts_n = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'session.start'").fetchone()[0]
    return sessions_n, arms_n, starts_n


def _bootstrap(db: sqlite3.Connection, state: _AppState, cid: str) -> SessionContext:
    frontier = read_frontier(db, state, clinician_id=cid, patient_id="p1")
    return bootstrap_session(db, state, clinician_id=cid, patient_id="p1", frontier=frontier)


def test_bootstrap_session_idempotent(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")

    first = _bootstrap(db, state, cid)
    assert isinstance(first, SessionContext)
    assert first.arm == "no_ai"
    assert first.config_hash == "cfg"
    assert first.frontier == Frontier(0, False)
    assert _counts(db) == (1, 1, 1)
    assert state.write_counter == 1

    second = _bootstrap(db, state, cid)
    assert second == first
    assert _counts(db) == (1, 1, 1)
    assert state.write_counter == 1


def test_bootstrap_session_after_ended_creates_new(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    first = _bootstrap(db, state, cid)

    db.execute(
        "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
        (first.session_id,),
    )
    db.commit()

    third = _bootstrap(db, state, cid)
    assert third.session_id != first.session_id
    assert _counts(db) == (2, 1, 2)


# ---------------------------------------------------------------------------
# S9b: read_frontier + completed-walk session reuse (#19, #20, #20b)
# ---------------------------------------------------------------------------


def test_read_frontier_reads_progress(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")

    assert read_frontier(db, state, clinician_id=cid, patient_id="p1") == Frontier(0, False)
    # A pure read: nothing bootstrapped.
    assert _counts(db) == (0, 0, 0)

    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="cfg"
    )
    assert read_frontier(db, state, clinician_id=cid, patient_id="p1") == Frontier(1, False)

    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=1, config_hash="cfg"
    )
    assert read_frontier(db, state, clinician_id=cid, patient_id="p1") == Frontier(1, True)
    assert _counts(db) == (0, 0, 0)


def test_read_frontier_clamps_and_warns_on_drift(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=7, config_hash="old"
    )

    with capture_logs() as logs:
        frontier = read_frontier(db, state, clinician_id=cid, patient_id="p1")
    kinds = [log.get("event_kind") for log in logs]
    assert frontier.unlocked_t_index == 2
    assert "progress.clamped" in kinds
    assert "progress.config_hash.drift" in kinds

    # Live hash + in-range → neither warning.
    progress.unlock(
        db, clinician_id=cid, patient_id="p2", from_t_index=0, to_t_index=1, config_hash="cfg"
    )
    with capture_logs() as quiet:
        read_frontier(db, state, clinician_id=cid, patient_id="p2")
    assert [log for log in quiet if log.get("log_level") == "warning"] == []

    # review-fix R19: an app_state without study_timepoints must not crash.
    class _Bare:
        config_hash = "old"

    assert read_frontier(db, _Bare(), clinician_id=cid, patient_id="p1").unlocked_t_index == 7


def test_bootstrap_session_reuses_latest_session_when_completed(db: sqlite3.Connection) -> None:
    """review-fix R12: a completed walk never opens a session nothing can close."""
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    first = _bootstrap(db, state, cid)

    sessions.close(db, first.session_id)
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=2, config_hash="cfg"
    )

    again = _bootstrap(db, state, cid)
    assert again.session_id == first.session_id
    assert again.frontier.completed is True
    assert _counts(db) == (1, 1, 1)
