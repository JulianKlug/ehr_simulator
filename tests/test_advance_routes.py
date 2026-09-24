"""``POST /patient/{pid}/timepoint/{t}/advance`` + the GET gate
(specs/session-09b-question-gating.md §9 #26-#40b).

``study_client`` boots the synthetic study (timepoints 0/60/180 min) with
the 7-question fixture (6 required). Every 3xx assertion passes
``follow_redirects=False`` — ``TestClient`` follows redirects by default.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from ehr_simulator.db import progress
from ehr_simulator.web import routes
from tests.conftest import answer_all_required, seed_progress

PID = "synth_001"
HX = {"HX-Request": "true"}
REQUIRED_COUNT = 6


def _url(t_index: int, pid: str = PID) -> str:
    return f"/patient/{pid}/timepoint/{t_index}/advance?chrome=epic"


def _view_url(t_index: int, pid: str = PID) -> str:
    return f"/patient/{pid}/timepoint/{t_index}?chrome=epic"


def _db(client: TestClient) -> sqlite3.Connection:
    return client.app.state.db  # type: ignore[attr-defined]


def _cid(client: TestClient) -> str:
    return client.cookies.get("ehrsim_clinician_id")


def _count(client: TestClient, table: str, where: str = "1=1") -> int:
    return _db(client).execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def _progress(client: TestClient):
    return progress.fetch(_db(client), clinician_id=_cid(client), patient_id=PID)


def _payloads(client: TestClient, kind: str) -> list[dict]:
    rows = _db(client).execute(
        "SELECT payload_json FROM events WHERE kind = ? ORDER BY event_id", (kind,)
    )
    return [json.loads(r[0]) for r in rows]


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _advance(client: TestClient, t_index: int, *, htmx: bool = True, **data: str):
    return client.post(
        _url(t_index), data=data or None, headers=HX if htmx else {}, follow_redirects=False
    )


# ---------------------------------------------------------------------------
# GET gate (#26, #26b, #26d, #27, #40b)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_get_beyond_frontier_redirects(study_client: TestClient, htmx: bool) -> None:
    """REGRESSION: data ≤ frontier."""
    with capture_logs() as logs:
        r = study_client.get(_view_url(2), headers=HX if htmx else {}, follow_redirects=False)
    if htmx:
        assert r.status_code == 200
        assert r.headers["HX-Redirect"] == _view_url(0)
    else:
        assert r.status_code == 303
        assert r.headers["location"] == _view_url(0)
    assert "<svg" not in r.text and "patient-view" not in r.text
    assert any(log.get("event_kind") == "gate.redirect" for log in logs)


def test_gate_beyond_frontier_writes_nothing(
    study_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review-fix R20 + R21: no slice, no arm lock, no session, no event."""
    calls: list[tuple] = []
    real = routes.slice_to_timepoint

    def _counting(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(routes, "slice_to_timepoint", _counting)
    r = study_client.get(_view_url(2), follow_redirects=False)
    assert r.status_code == 303
    assert calls == []
    for table in ("progress", "sessions", "arm_assignments"):
        assert _count(study_client, table) == 0, table
    assert _count(study_client, "events", "kind = 'session.start'") == 0


def test_gate_redirect_log_line_is_attributable(
    study_client: TestClient, tmp_log_dir: Path
) -> None:
    """review-fix R10: the request context is bound before the gate.

    Read from the JSONL sink — ``capture_logs`` bypasses the contextvars merge.
    """
    study_client.get(_view_url(2), follow_redirects=False)
    lines = [
        json.loads(line)
        for line in (tmp_log_dir / "current.jsonl").read_text().splitlines()
        if line.strip()
    ]
    warning = next(entry for entry in lines if entry.get("event_kind") == "gate.redirect")
    request_line = next(
        entry for entry in lines if entry.get("path") == f"/patient/{PID}/timepoint/2"
    )
    for entry in (warning, request_line):
        assert entry["patient_id"] == PID
        assert entry["timepoint_index"] == 2
        assert entry["chrome"] == "epic"


def test_get_frontier_and_past_are_200(study_client: TestClient) -> None:
    seed_progress(study_client, PID, 1)
    assert (
        _soup(study_client.get(_view_url(0)).text).select_one("#questions-pane")["data-mode"]
        == "locked"
    )
    assert (
        _soup(study_client.get(_view_url(1)).text).select_one("#questions-pane")["data-mode"]
        == "open"
    )
    assert study_client.get(_view_url(2), follow_redirects=False).status_code == 303


def test_history_restore_request_serves_full_document(study_client: TestClient) -> None:
    """review-fix R6: htmx re-GETs on a cache miss and swaps the whole body."""
    restore = study_client.get(_view_url(0), headers={**HX, "HX-History-Restore-Request": "true"})
    assert restore.status_code == 200
    assert restore.text.lstrip().lower().startswith("<!doctype html>")
    assert 'id="shortcut-overlay"' in restore.text
    assert 'id="patient-view"' in restore.text
    assert "HX-Push-Url" not in restore.headers

    partial = study_client.get(_view_url(0), headers=HX)
    assert "<html" not in partial.text
    assert partial.headers["HX-Push-Url"] == _view_url(0)

    gated = study_client.get(
        _view_url(2), headers={**HX, "HX-History-Restore-Request": "true"}, follow_redirects=False
    )
    assert gated.headers["HX-Redirect"] == _view_url(0)


# ---------------------------------------------------------------------------
# /advance outcomes (#28-#33)
# ---------------------------------------------------------------------------


def test_advance_blocked_409_fragment_and_event(study_client: TestClient) -> None:
    r = _advance(study_client, 0)
    assert r.status_code == 409
    assert r.headers["HX-Retarget"] == "#advance-form"
    assert r.headers["HX-Reswap"] == "outerHTML"
    cta = _soup(r.text).select_one("#advance-form")
    assert cta["data-remaining"] == str(REQUIRED_COUNT)
    assert cta["data-first-unanswered"] == "deterioration_6h"
    assert not cta.has_attr("hx-swap-oob")
    blocked = _payloads(study_client, "advance.blocked")
    assert len(blocked) == 1
    assert blocked[0]["remaining"][0] == "deterioration_6h"
    assert _progress(study_client) is None


def test_advance_cta_has_no_js_form_action(study_client: TestClient) -> None:
    """review-fix R24: a submit without htmx must hit the real route."""
    cta = _soup(study_client.get(_view_url(0)).text).select_one("#advance-form")
    assert cta["method"] == "post"
    assert cta["action"].endswith("/timepoint/0/advance?chrome=epic")


def test_advance_ok_renders_next_view_and_pushes_url(study_client: TestClient) -> None:
    answer_all_required(study_client, PID, 0)
    r = _advance(study_client, 0)
    assert r.status_code == 200
    assert r.headers["HX-Push-Url"] == _view_url(1)
    soup = _soup(r.text)
    assert soup.select_one("#patient-view")["data-t-index"] == "1"
    # review-fix R8: rendered with the post-write context → open pane.
    assert soup.select_one("#questions-pane")["data-mode"] == "open"
    assert soup.select_one("#advance-form")["data-remaining"] == str(REQUIRED_COUNT)
    assert _progress(study_client).unlocked_t_index == 1
    ok = _payloads(study_client, "advance.ok")
    assert ok == [
        {
            "t_index": 0,
            "to_t_index": 1,
            "final": False,
            "answered_required": REQUIRED_COUNT,
            "answered_total": REQUIRED_COUNT,
            "required": REQUIRED_COUNT,
        }
    ]


def test_advance_events_carry_client_ts_and_client_seq(study_client: TestClient) -> None:
    """review-fix R17."""
    _advance(study_client, 0, client_ts="2026-09-16T12:34:56.789Z", client_seq="7")
    row = (
        _db(study_client)
        .execute("SELECT client_ts, client_seq FROM events WHERE kind = 'advance.blocked'")
        .fetchone()
    )
    assert str(row[0]) == "2026-09-16 12:34:56.789000"
    assert row[1] == 7

    answer_all_required(study_client, PID, 0)
    with capture_logs() as logs:
        r = _advance(study_client, 0, client_ts="garbage")
    assert r.status_code == 200
    ok_row = (
        _db(study_client)
        .execute("SELECT client_ts FROM events WHERE kind = 'advance.ok'")
        .fetchone()
    )
    assert ok_row[0] is None
    assert any(log.get("event_kind") == "answer.client_ts.invalid" for log in logs)


def test_advance_stale_412_renders_frontier_view(study_client: TestClient) -> None:
    answer_all_required(study_client, PID, 0)
    assert _advance(study_client, 0).status_code == 200

    r = _advance(study_client, 0)
    assert r.status_code == 412
    assert r.headers["HX-Push-Url"] == _view_url(1)
    assert _soup(r.text).select_one("#patient-view")["data-t-index"] == "1"
    assert _progress(study_client).unlocked_t_index == 1
    assert len(_payloads(study_client, "advance.ok")) == 1


def test_advance_concurrent_frontier_move_is_stale(study_client: TestClient) -> None:
    """The frontier moved between the clinician's render and their click.

    Through the route this takes the plain stale path (the handler re-reads
    the frontier before advancing); the SQL compare-and-set itself is locked
    at service level by ``test_gating.py::test_advance_lost_cas_race_is_stale``.
    """
    answer_all_required(study_client, PID, 0)
    # Someone moves the frontier out from under the next request.
    seed_progress(study_client, PID, 1)
    r = _advance(study_client, 0)
    assert r.status_code == 412
    assert _progress(study_client).unlocked_t_index == 1
    assert _payloads(study_client, "advance.ok") == []


def test_advance_double_submit_second_is_stale(study_client: TestClient) -> None:
    """REGRESSION: two clicks, one unlock."""
    answer_all_required(study_client, PID, 0)
    first = _advance(study_client, 0)
    second = _advance(study_client, 0)
    assert (first.status_code, second.status_code) == (200, 412)
    assert _progress(study_client).unlocked_t_index == 1
    assert len(_payloads(study_client, "advance.ok")) == 1


def _walk_to_last(client: TestClient) -> None:
    for t in (0, 1):
        answer_all_required(client, PID, t)
        assert _advance(client, t).status_code == 200
    answer_all_required(client, PID, 2)


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_advance_final_redirects_and_closes_session(study_client: TestClient, htmx: bool) -> None:
    _walk_to_last(study_client)
    sessions_before = _count(study_client, "sessions")
    starts_before = _count(study_client, "events", "kind = 'session.start'")

    r = _advance(study_client, 2, htmx=htmx)
    if htmx:
        assert r.status_code == 200
        assert r.headers["HX-Redirect"] == "/"
    else:
        assert r.status_code == 303
        assert r.headers["location"] == "/"

    row = _progress(study_client)
    assert row.completed_at is not None and row.unlocked_t_index == 2
    assert _count(study_client, "sessions", "ended_at IS NOT NULL") == 1
    assert _payloads(study_client, "session.end") == [{"reason": "patient_complete"}]
    assert _payloads(study_client, "advance.ok")[-1]["final"] is True

    # review-fix R12: revisits are read-only and open no new session.
    last = _soup(study_client.get(_view_url(2)).text)
    assert last.select_one("#questions-pane")["data-mode"] == "locked"
    assert "Patient complete" in last.select_one(".pane-lock-note").get_text()
    assert last.select_one("#advance-form") is None
    first = _soup(study_client.get(_view_url(0)).text)
    assert first.select_one("#questions-pane")["data-mode"] == "locked"
    assert _count(study_client, "sessions") == sessions_before
    assert _count(study_client, "events", "kind = 'session.start'") == starts_before


def test_advance_free_notes_optional_does_not_block(study_client: TestClient) -> None:
    """S9a review-fix R22 acceptance."""
    answered = answer_all_required(study_client, PID, 0)
    assert "free_notes" not in answered
    assert _advance(study_client, 0).status_code == 200


@pytest.mark.parametrize("outcome", ["blocked", "advanced", "stale", "finished"], ids=lambda o: o)
def test_advance_plain_browser_outcomes_are_303(study_client: TestClient, outcome: str) -> None:
    """review-fix R24: POST-redirect-GET without htmx."""
    if outcome == "blocked":
        r = _advance(study_client, 0, htmx=False)
        assert (r.status_code, r.headers["location"]) == (303, _view_url(0))
        assert len(_payloads(study_client, "advance.blocked")) == 1
        return
    if outcome == "advanced":
        answer_all_required(study_client, PID, 0)
        r = _advance(study_client, 0, htmx=False)
        assert (r.status_code, r.headers["location"]) == (303, _view_url(1))
        return
    if outcome == "stale":
        answer_all_required(study_client, PID, 0)
        _advance(study_client, 0)
        r = _advance(study_client, 0, htmx=False)
        assert (r.status_code, r.headers["location"]) == (303, _view_url(1))
        return
    _walk_to_last(study_client)
    r = _advance(study_client, 2, htmx=False)
    assert (r.status_code, r.headers["location"]) == (303, "/")


# ---------------------------------------------------------------------------
# Preamble failures (#34-#36)
# ---------------------------------------------------------------------------


def test_advance_no_study_409(client: TestClient) -> None:
    r = client.post(_url(0), headers=HX)
    assert r.status_code == 409
    assert 'class="error-flash"' in r.text
    assert "No questions configured" in r.text


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_advance_no_cookie_redirects(anonymous_client: TestClient, htmx: bool) -> None:
    r = anonymous_client.post(_url(0), headers=HX if htmx else {}, follow_redirects=False)
    if htmx:
        assert (r.status_code, r.headers["HX-Redirect"]) == (200, "/login")
    else:
        assert (r.status_code, r.headers["location"]) == (303, "/login")


@pytest.mark.parametrize(
    ("pid", "t_index", "fragment"),
    [
        ("synth_999", 0, "not part of this study"),
        (PID, 99, "out of range"),
    ],
    ids=["not_in_study", "out_of_range"],
)
def test_advance_bad_target_404(
    study_client: TestClient, pid: str, t_index: int, fragment: str
) -> None:
    r = study_client.post(_url(t_index, pid), headers=HX)
    assert r.status_code == 404
    assert 'class="error-flash"' in r.text
    assert fragment in r.text
    assert _count(study_client, "sessions") == 0
    assert _count(study_client, "events") == 0


# ---------------------------------------------------------------------------
# Navigation reflections (#37-#40)
# ---------------------------------------------------------------------------


def test_summary_card_next_hidden_at_frontier(study_client: TestClient) -> None:
    assert _soup(study_client.get(_view_url(0)).text).select_one(".tp-next") is None

    seed_progress(study_client, PID, 1)
    past = _soup(study_client.get(_view_url(0)).text)
    nxt = past.select_one(".tp-next")
    assert nxt is not None and nxt["hx-get"].endswith("/timepoint/1?chrome=epic")
    assert past.select_one(".tp-prev") is not None
    assert _soup(study_client.get(_view_url(1)).text).select_one(".tp-next") is None


def test_patient_jumper_and_index_resume_at_frontier(study_client: TestClient) -> None:
    seed_progress(study_client, PID, 1)
    soup = _soup(study_client.get(_view_url(0, "synth_002")).text)
    hrefs = {a.get_text(strip=True): a["href"] for a in soup.select(".patient-link")}
    assert hrefs["synth_001"].endswith("/timepoint/1?chrome=epic")
    assert hrefs["synth_002"].endswith("/timepoint/0?chrome=epic")

    index = _soup(study_client.get("/").text)
    rows = {
        li.select_one("strong").get_text(strip=True): li for li in index.select(".patient-list li")
    }
    assert rows["synth_001"]["data-progress-state"] == "in_progress"
    assert "t 2/3" in rows["synth_001"].select_one(".progress-marker").get_text()
    assert rows["synth_001"].select_one("a")["href"] == "/patient/synth_001/timepoint/1?chrome=epic"
    assert rows["synth_002"]["data-progress-state"] == "not_started"

    seed_progress(study_client, "synth_003", 2, completed=True)
    index = _soup(study_client.get("/").text)
    row = next(li for li in index.select(".patient-list li") if "synth_003" in li.get_text())
    assert row["data-progress-state"] == "complete"
    assert row.select_one("a")["href"] == "/patient/synth_003/timepoint/2?chrome=epic"


def test_index_without_study_has_no_progress_markers(client: TestClient) -> None:
    index = _soup(client.get("/").text)
    assert index.select(".progress-marker") == []
    assert all(
        a["href"].endswith("/timepoint/0?chrome=epic")
        or a["href"].endswith("/timepoint/0?chrome=dense")
        for a in index.select(".patient-list a")
    )
    # review-fix R14: non-study jumper hrefs byte-identical to S2.
    soup = _soup(client.get(_view_url(1)).text)
    assert {a["href"] for a in soup.select(".patient-link")} == {
        f"/patient/{pid}/timepoint/0?chrome=epic" for pid in ("synth_001", "synth_002", "synth_003")
    }
    assert soup.select_one(".tp-next") is not None


def test_timepoint_count_uses_study_timepoints(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """REGRESSION: the summary total and data-t-count follow the study, not the dataset."""
    from ehr_simulator.web.app import app_from_study_config
    from tests.conftest import _activate_configuration, _seed_clinician

    study_path = tmp_log_dir.parent / "study_two.yaml"
    study_path.write_text(
        'schema_version: "2"\nstudy_id: advance_two\ndataset: synthetic\npatient_ids: [synth_001]\n'
        "time_unit: minutes\ntimepoints: [0, 180]\n",
        encoding="utf-8",
    )
    cid = _seed_clinician(tmp_db_path, study_id="advance_two")
    _activate_configuration(
        tmp_db_path, study_path, study_fixture_dir / "questions.yaml", version="v1", description="t"
    )
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as c:
        c.cookies.set("ehrsim_clinician_id", cid)
        soup = _soup(c.get(_view_url(0)).text)
    assert soup.select_one("#patient-view")["data-t-count"] == "2"
    assert "(1/2)" in soup.select_one(".tp-position").get_text()
    assert "of 2" in soup.select_one(".summary-time").get_text()
