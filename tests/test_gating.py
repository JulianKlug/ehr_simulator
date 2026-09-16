"""``web/gating.py`` (specs/session-09b-question-gating.md §9 #11-#18b)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from ehr_simulator.config import Questions, load_questions
from ehr_simulator.db import answers, clinicians, progress
from ehr_simulator.web.gating import (
    advance,
    completeness,
    is_viewable,
    pane_mode,
    progress_overview,
)
from ehr_simulator.web.study_session import Frontier, bootstrap_session, read_frontier

TIMEPOINTS = (0.0, 60.0, 180.0)
REQUIRED_IDS = (
    "deterioration_6h",
    "survives_hospital",
    "good_outcome_3mo",
    "dead_6mo",
    "confidence",
    "contributing_factors",
)


class _AppState:
    write_counter = 0
    config_hash = "cfg"
    study_timepoints = list(TIMEPOINTS)


@pytest.fixture
def questions(study_fixture_dir: Path) -> Questions:
    return load_questions(study_fixture_dir / "questions.yaml")


def _save(db: sqlite3.Connection, cid: str, t: float, *question_ids: str) -> None:
    for qid in question_ids:
        answers.upsert(
            db,
            clinician_id=cid,
            patient_id="p1",
            timepoint=t,
            question_id=qid,
            value="1",
            arm="no_ai",
            config_hash="cfg",
        )


def _ctx(db: sqlite3.Connection, state: _AppState, cid: str):
    frontier = read_frontier(db, state, clinician_id=cid, patient_id="p1")
    return bootstrap_session(db, state, clinician_id=cid, patient_id="p1", frontier=frontier)


def _events(db: sqlite3.Connection, kind: str) -> list[dict]:
    rows = db.execute(
        "SELECT payload_json FROM events WHERE kind = ? ORDER BY event_id", (kind,)
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _advance(db, state, ctx, questions, t_index):
    return advance(
        db,
        state,
        ctx=ctx,
        clinician_id=ctx_cid(ctx, db),
        patient_id="p1",
        t_index=t_index,
        timepoints=TIMEPOINTS,
        questions=questions,
    )


def ctx_cid(ctx, db: sqlite3.Connection) -> str:
    row = db.execute(
        "SELECT clinician_id FROM sessions WHERE session_id = ?", (ctx.session_id,)
    ).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Pure predicates (#11-#13)
# ---------------------------------------------------------------------------


def test_completeness_ignores_optional_questions(questions: Questions) -> None:
    saved = dict.fromkeys(REQUIRED_IDS, "x")
    assert completeness(questions, saved).complete
    assert completeness(questions, {**saved, "free_notes": "note"}).complete


def test_completeness_remaining_in_yaml_order(questions: Questions) -> None:
    comp = completeness(questions, {"confidence": "3", "deterioration_6h": "No"})
    assert comp.remaining == (
        "survives_hospital",
        "good_outcome_3mo",
        "dead_6mo",
        "contributing_factors",
    )
    assert not comp.complete


@pytest.mark.parametrize(
    ("frontier", "t_index", "viewable", "mode"),
    [
        pytest.param(Frontier(1, False), 1, True, "open", id="open"),
        pytest.param(Frontier(1, False), 0, True, "locked", id="past"),
        pytest.param(Frontier(1, False), 2, False, "locked", id="future"),
        pytest.param(Frontier(2, True), 2, True, "locked", id="completed"),
    ],
)
def test_pane_mode_and_viewable(
    frontier: Frontier, t_index: int, viewable: bool, mode: str
) -> None:
    assert is_viewable(frontier, t_index) is viewable
    assert pane_mode(frontier, t_index) == mode


# ---------------------------------------------------------------------------
# advance (#14-#18)
# ---------------------------------------------------------------------------


def test_advance_blocked_emits_event_and_keeps_progress(
    db: sqlite3.Connection, questions: Questions
) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    ctx = _ctx(db, state, cid)

    result = _advance(db, state, ctx, questions, 0)

    assert result.outcome == "blocked"
    assert result.remaining == REQUIRED_IDS
    assert result.unlocked_t_index == 0
    blocked = _events(db, "advance.blocked")
    assert len(blocked) == 1
    assert blocked[0]["remaining"] == list(REQUIRED_IDS)
    assert blocked[0]["answered_required"] == 0
    assert blocked[0]["required"] == 6
    assert progress.fetch(db, clinician_id=cid, patient_id="p1") is None


def test_advance_ok_unlocks_next_and_emits_event(
    db: sqlite3.Connection, questions: Questions
) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    ctx = _ctx(db, state, cid)
    _save(db, cid, 0.0, *REQUIRED_IDS)

    result = _advance(db, state, ctx, questions, 0)

    assert result == result.__class__("advanced", 1, ())
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    ok = _events(db, "advance.ok")
    assert ok == [
        {
            "t_index": 0,
            "to_t_index": 1,
            "final": False,
            "answered_required": 6,
            "answered_total": 6,
            "required": 6,
        }
    ]
    open_sessions = db.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()
    assert open_sessions[0] == 1


def test_advance_counts_optional_answers_separately(
    db: sqlite3.Connection, questions: Questions
) -> None:
    """review-fix R9: answered_required never counts free_notes."""
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    ctx = _ctx(db, state, cid)
    _save(db, cid, 0.0, *REQUIRED_IDS, "free_notes")

    _advance(db, state, ctx, questions, 0)
    payload = _events(db, "advance.ok")[0]
    assert (payload["answered_required"], payload["answered_total"]) == (6, 7)


def test_advance_final_completes_and_closes_session(
    db: sqlite3.Connection, questions: Questions
) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=2, config_hash="cfg"
    )
    ctx = _ctx(db, state, cid)
    assert ctx.frontier == Frontier(2, False)
    _save(db, cid, 180.0, *REQUIRED_IDS)

    result = _advance(db, state, ctx, questions, 2)

    assert result.outcome == "finished"
    assert result.unlocked_t_index == 2
    row = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert row.completed_at is not None
    ended = db.execute(
        "SELECT ended_at FROM sessions WHERE session_id = ?", (ctx.session_id,)
    ).fetchone()[0]
    assert ended is not None
    kinds = [
        r[0]
        for r in db.execute(
            "SELECT kind FROM events WHERE kind IN ('advance.ok', 'session.end') ORDER BY event_id"
        )
    ]
    assert kinds == ["advance.ok", "session.end"]
    assert _events(db, "advance.ok")[0]["final"] is True
    assert _events(db, "session.end") == [{"reason": "patient_complete"}]


def test_advance_stale_when_t_index_mismatch(db: sqlite3.Connection, questions: Questions) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="cfg"
    )
    ctx = _ctx(db, state, cid)
    _save(db, cid, 0.0, *REQUIRED_IDS)

    with capture_logs() as logs:
        result = _advance(db, state, ctx, questions, 0)

    assert result.outcome == "stale"
    assert result.unlocked_t_index == 1
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    assert _events(db, "advance.ok") == [] and _events(db, "advance.blocked") == []
    assert any(log.get("event_kind") == "advance.stale" for log in logs)


def test_advance_after_complete_is_stale(db: sqlite3.Connection, questions: Questions) -> None:
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=2, config_hash="cfg"
    )
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=2, config_hash="cfg"
    )
    ctx = _ctx(db, state, cid)
    _save(db, cid, 180.0, *REQUIRED_IDS)

    result = _advance(db, state, ctx, questions, 2)
    assert result.outcome == "stale"
    assert _events(db, "advance.ok") == []


def test_advance_lost_cas_race_is_stale(db: sqlite3.Connection, questions: Questions) -> None:
    """review-fix R29: the frontier moved between bootstrap and the write."""
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    ctx = _ctx(db, state, cid)  # frontier 0
    _save(db, cid, 0.0, *REQUIRED_IDS)
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="cfg"
    )

    result = _advance(db, state, ctx, questions, 0)
    assert result.outcome == "stale"
    assert result.unlocked_t_index == 1
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    assert _events(db, "advance.ok") == []


def test_advance_passes_client_clock_fields(db: sqlite3.Connection, questions: Questions) -> None:
    """review-fix R17: advance events carry client_ts / client_seq."""
    state = _AppState()
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    ctx = _ctx(db, state, cid)

    advance(
        db,
        state,
        ctx=ctx,
        clinician_id=cid,
        patient_id="p1",
        t_index=0,
        timepoints=TIMEPOINTS,
        questions=questions,
        client_ts="2026-09-16T12:34:56.789Z",
        client_seq="7",
    )
    row = db.execute(
        "SELECT client_ts, client_seq FROM events WHERE kind = 'advance.blocked'"
    ).fetchone()
    assert row[0] is not None
    assert row[1] == 7


# ---------------------------------------------------------------------------
# progress_overview (#18b)
# ---------------------------------------------------------------------------


def test_progress_overview_states_and_resume_indices(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Gate")
    progress.unlock(
        db, clinician_id=cid, patient_id="p2", from_t_index=0, to_t_index=1, config_hash="cfg"
    )
    progress.unlock(
        db, clinician_id=cid, patient_id="p3", from_t_index=0, to_t_index=2, config_hash="cfg"
    )
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p3", unlocked_t_index=2, config_hash="cfg"
    )
    progress.unlock(
        db, clinician_id=cid, patient_id="p4", from_t_index=0, to_t_index=9, config_hash="cfg"
    )

    overview = progress_overview(
        db, clinician_id=cid, patient_ids=["p1", "p2", "p3", "p4"], timepoint_count=3
    )

    assert set(overview) == {"p1", "p2", "p3", "p4"}  # total over the request
    assert (overview["p1"].state, overview["p1"].unlocked_t_index) == ("not_started", 0)
    assert (overview["p2"].state, overview["p2"].unlocked_t_index) == ("in_progress", 1)
    assert (overview["p3"].state, overview["p3"].unlocked_t_index) == ("complete", 2)
    assert overview["p4"].unlocked_t_index == 2  # clamped
    assert overview["p1"].timepoint_count == 3
