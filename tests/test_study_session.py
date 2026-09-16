"""``web/study_session.bootstrap_session`` (specs/session-09a §9 #11, #12)."""

from __future__ import annotations

import sqlite3

from ehr_simulator.db import clinicians
from ehr_simulator.web.study_session import SessionContext, bootstrap_session


class _AppState:
    write_counter = 0
    config_hash = "cfg"


def _counts(db: sqlite3.Connection) -> tuple[int, int, int]:
    sessions_n = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    arms_n = db.execute("SELECT COUNT(*) FROM arm_assignments").fetchone()[0]
    starts_n = db.execute("SELECT COUNT(*) FROM events WHERE kind = 'session.start'").fetchone()[0]
    return sessions_n, arms_n, starts_n


def test_bootstrap_session_idempotent(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")

    first = bootstrap_session(db, state, clinician_id=cid, patient_id="p1")
    assert isinstance(first, SessionContext)
    assert first.arm == "no_ai"
    assert first.config_hash == "cfg"
    assert _counts(db) == (1, 1, 1)
    assert state.write_counter == 1

    second = bootstrap_session(db, state, clinician_id=cid, patient_id="p1")
    assert second == first
    assert _counts(db) == (1, 1, 1)
    assert state.write_counter == 1


def test_bootstrap_session_after_ended_creates_new(db: sqlite3.Connection) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    first = bootstrap_session(db, state, clinician_id=cid, patient_id="p1")

    db.execute(
        "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
        (first.session_id,),
    )
    db.commit()

    third = bootstrap_session(db, state, clinician_id=cid, patient_id="p1")
    assert third.session_id != first.session_id
    assert _counts(db) == (2, 1, 2)
