"""S10 behavioral-event emission: ``timepoint.enter`` / ``timepoint.exit``
(specs/session10.md §3).

Every test asserts on the ``events`` table (kind, actor, patient, timepoint,
payload) — never on wall-clock values. The ``study_client`` fixture boots the
synthetic study (timepoints 0/60/180 min) with 6 required questions.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from tests.conftest import answer_all_required, seed_progress

PID = "synth_001"
HX = {"HX-Request": "true"}


def _view_url(t_index: int) -> str:
    return f"/patient/{PID}/timepoint/{t_index}?chrome=epic"


def _advance_url(t_index: int) -> str:
    return f"/patient/{PID}/timepoint/{t_index}/advance?chrome=epic"


def _events(client: TestClient, kind: str) -> list[dict]:
    db: sqlite3.Connection = client.app.state.db  # type: ignore[attr-defined]
    rows = db.execute(
        "SELECT clinician_id, patient_id, timepoint, payload_json FROM events "
        "WHERE kind = ? ORDER BY event_id",
        (kind,),
    )
    return [
        {"clinician_id": r[0], "patient_id": r[1], "timepoint": r[2], "payload": json.loads(r[3])}
        for r in rows
    ]


def _enter_pairs(client: TestClient) -> list[tuple[float, dict]]:
    return [(e["timepoint"], e["payload"]) for e in _events(client, "timepoint.enter")]


def _exit_pairs(client: TestClient) -> list[tuple[float, dict]]:
    return [(e["timepoint"], e["payload"]) for e in _events(client, "timepoint.exit")]


def test_open_frontier_view_records_one_enter_for_cookie_clinician(
    study_client: TestClient,
) -> None:
    r = study_client.get(_view_url(0), follow_redirects=False)
    assert r.status_code == 200
    ents = _events(study_client, "timepoint.enter")
    assert len(ents) == 1
    assert ents[0]["clinician_id"] == study_client.cookies.get("ehrsim_clinician_id")
    assert ents[0]["patient_id"] == PID
    assert ents[0]["timepoint"] == 0.0
    assert ents[0]["payload"] == {"t_index": 0}
    # No exit until the clinician advences.
    assert _exit_pairs(study_client) == []


def test_refresh_duplicate_enter_is_rejected_and_still_pairs(
    study_client: TestClient,
) -> None:
    """Spec §3: duplicate refresh enters never replace the first; §4 pairs the
    first enter with the first later exit."""
    assert study_client.get(_view_url(0)).status_code == 200
    assert study_client.get(_view_url(0)).status_code == 200
    assert len(_enter_pairs(study_client)) == 2  # tolerated append, not an error

    answer_all_required(study_client, PID, 0)
    r = study_client.post(_advance_url(0), headers=HX, follow_redirects=False)
    assert r.status_code == 200
    # One exit: first enter (t=0) paired with it — later duplicate is inert.
    assert _exit_pairs(study_client) == [(0.0, {"t_index": 0, "reason": "advance"})]


def test_beyond_frontier_navigation_records_nothing(study_client: TestClient) -> None:
    r = study_client.get(_view_url(2), follow_redirects=False)
    assert r.status_code == 303
    assert _enter_pairs(study_client) == []
    assert _exit_pairs(study_client) == []


def test_frozen_past_pane_records_no_enter(study_client: TestClient) -> None:
    # Answered and advanced past: t=0 is read-only; views are audit, not timing.
    answer_all_required(study_client, PID, 0)
    seed_progress(study_client, PID, 1)
    assert study_client.get(_view_url(0), follow_redirects=False).status_code == 200
    assert _enter_pairs(study_client) == []


def test_unanswered_frozen_pane_also_records_no_enter(study_client: TestClient) -> None:
    seed_progress(study_client, PID, 1)
    assert study_client.get(_view_url(0), follow_redirects=False).status_code == 200
    assert _enter_pairs(study_client) == []


def test_successful_advance_records_exit_then_next_enter(study_client: TestClient) -> None:
    answer_all_required(study_client, PID, 0)
    r = study_client.post(_advance_url(0), headers=HX, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers.get("HX-Push-Url") == _view_url(1)
    assert _exit_pairs(study_client) == [(0.0, {"t_index": 0, "reason": "advance"})]
    assert _enter_pairs(study_client) == [(60.0, {"t_index": 1})]


def test_blocked_advance_records_no_exit(study_client: TestClient) -> None:
    r = study_client.post(_advance_url(0), headers=HX, follow_redirects=False)
    assert r.status_code == 409
    assert _exit_pairs(study_client) == []
    assert _enter_pairs(study_client) == []


def test_full_walk_ends_with_exit_finish(study_client: TestClient) -> None:
    for t_index in range(3):
        answer_all_required(study_client, PID, t_index)
        r = study_client.post(
            _advance_url(t_index),
            headers=HX if t_index < 2 else {},
            follow_redirects=False,
        )
        assert r.status_code in (200, 303), (t_index, r.status_code)

    exits = _exit_pairs(study_client)
    assert exits == [
        (0.0, {"t_index": 0, "reason": "advance"}),
        (60.0, {"t_index": 1, "reason": "advance"}),
        (180.0, {"t_index": 2, "reason": "finish"}),
    ]
