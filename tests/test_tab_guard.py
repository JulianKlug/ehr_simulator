"""S11m: clinician identity privacy, one tab per case, backup identity.

Drives the real Phase 2 app on ``study_lifecycle.yaml`` plus the first use
case ``telemetry`` block, so every active case is guarded::

    tab A  GET ─► render R1 ─► claim ─► lease (A, R1) ─► writes carry (A, R1)
    tab B  GET ─► render R2 ─► claim ─► 409 + tab.conflict, no writes

Clients here are raw tabs (no auto-claim hook); each request names its tab
explicitly. Test numbers refer to ``specs/session-11m-privacy-multitab-backups.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from freezegun import freeze_time

from ehr_simulator.behavioral_timing import TelemetryStatus, derive_observation_timings
from ehr_simulator.db import apply_migrations, connect, events, telemetry
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db.backup import create_backup, read_identity
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.exceptions import BackupIdentityError
from ehr_simulator.db.migrations import MIGRATIONS
from ehr_simulator.db.study_identity import bind
from ehr_simulator.study_variables_reader import load_case_inputs, load_case_variables
from ehr_simulator.web.tab_guard import (
    CONFLICT_HEADER,
    RENDER_ID_FIELD,
    TAB_ID_FIELD,
    TAB_LEASE_TTL_SECONDS,
)
from ehr_simulator.web.telemetry import BrowserKind
from tests.conftest import _valid_value
from tests.support.cases import (
    Harness,
    _start,
    _started_patient,
    harness,  # noqa: F401
)
from tests.support.lifecycle import LifecycleHarness, _harness
from tests.support.tab_guard import TAB_A, TAB_B, _answer_all, _claim, _owner, _page, _post, _tab
from tests.support.telemetry import TELEMETRY, _event

HX = {"HX-Request": "true"}
HTTP_OK = 200
HTTP_SEE_OTHER = 303
HTTP_NO_CONTENT = 204
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_UNAUTHORIZED = 401
UNKNOWN_RENDER = "f" * 32
COOKIE = "ehrsim_clinician_id"
LAST_T_INDEX = 2


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def th(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    data = yaml.safe_load((study_fixture_dir / "study_lifecycle.yaml").read_text())
    data["telemetry"] = TELEMETRY
    path = tmp_path / "study_guarded.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, study_fixture_dir / "questions.yaml")


def _release(client: TestClient, patient_id: str, tab_id: str, render_id: str) -> Any:
    return client.post(
        f"/case/{patient_id}/tab/release",
        json={"tab_id": tab_id, "render_id": render_id, "reason": "pagehide"},
    )


def _open(client: TestClient) -> tuple[str, str]:
    """Start a case, render t=0 and claim it as tab A."""
    patient_id = _started_patient(_start(client))
    render_id = _page(client, patient_id)
    assert _claim(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT
    return patient_id, render_id


def _answer(client: TestClient, patient_id: str, t_index: int, headers: dict[str, str]) -> Any:
    question = client.app.state.questions.questions[0]
    return client.post(
        f"/patient/{patient_id}/timepoint/{t_index}/answer",
        data={"question_id": question.question_id, "value": _valid_value(question)},
        headers=headers,
    )


def _tab_rows(th: LifecycleHarness, kind: str) -> list[tuple[str, str, dict]]:
    with th.conn() as conn:
        rows = conn.execute(
            "SELECT tab_id, render_id, payload_json FROM events WHERE kind = ? ORDER BY event_id",
            (kind,),
        ).fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


def _lease(th: LifecycleHarness, patient_id: str) -> tuple | None:
    with th.conn() as conn:
        return conn.execute(
            "SELECT tab_id, render_id, last_seen_at FROM case_tab_leases "
            "WHERE clinician_id = ? AND patient_id = ?",
            (th.clinician_id, patient_id),
        ).fetchone()


def _answer_rows(th: LifecycleHarness) -> int:
    return th.count("SELECT COUNT(*) FROM answers")


# ---------------------------------------------------------------------------
# Clinician identity privacy (#1-#7)
# ---------------------------------------------------------------------------


def test_login_event_carries_id_not_name(th: LifecycleHarness) -> None:  # 1, 6
    with _tab(th) as client:
        first = client.post("/login", data={"clinician_name": "Dr. Tab"}, follow_redirects=False)
        second = client.post("/login", data={"clinician_name": "dr.  TAB"}, follow_redirects=False)

    assert first.cookies.get(COOKIE) == second.cookies.get(COOKIE)
    with th.conn() as conn:
        rows = conn.execute(
            "SELECT clinician_id, payload_json FROM events WHERE kind = 'clinician.login'"
        ).fetchall()
    assert len(rows) == 2
    assert all(r[0] == first.cookies.get(COOKIE) for r in rows)
    assert all(json.loads(r[1]) == {} for r in rows)


@pytest.mark.parametrize(
    "payload",
    [
        {"name_normalized": "dr. x"},
        {"nested": [{"deeper": {"name_normalized": "dr. x"}}]},
    ],
    ids=["top level", "nested"],
)
def test_append_refuses_name_normalized(tmp_db_path: Path, payload: dict) -> None:  # 2, 3
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        conn.execute("INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c', 'n')")
        conn.commit()
        with pytest.raises(ValueError, match="name_normalized"):
            events.append(
                conn,
                session_id=None,
                clinician_id="c",
                patient_id=None,
                timepoint=None,
                kind="clinician.login",
                payload=payload,
            )
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        conn.close()


def test_browser_batch_refuses_name_normalized(tmp_db_path: Path) -> None:  # 4
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        conn.execute("INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c', 'n')")
        conn.commit()
        row = events.BrowserEvent(
            session_id=None,
            clinician_id="c",
            patient_id="p",
            timepoint=0.0,
            kind="browser.state",
            payload={"name_normalized": "n"},
            client_ts=None,
            client_seq=1,
            tab_id=TAB_A,
            render_id=UNKNOWN_RENDER,
            client_mono_ms=1.0,
        )
        with pytest.raises(ValueError, match="name_normalized"):
            events.append_browser_batch(conn, [row])
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        conn.close()


def test_historical_name_payload_survives_migration(tmp_db_path: Path) -> None:  # 5
    """A pre-S11m login row keeps its payload through migration 13."""
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        conn.execute("DROP TABLE case_tab_leases")
        conn.execute("DELETE FROM schema_migrations WHERE version = 13")
        conn.execute("INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c', 'n')")
        conn.execute(
            "INSERT INTO events (clinician_id, kind, payload_json) "
            "VALUES ('c', 'clinician.login', '{\"name_normalized\":\"n\"}')"
        )
        conn.commit()
        assert apply_migrations(conn) == [13]
        payload = conn.execute("SELECT payload_json FROM events").fetchone()[0]
    finally:
        conn.close()
    assert json.loads(payload) == {"name_normalized": "n"}


# ---------------------------------------------------------------------------
# Lease persistence and concurrency (#8-#20)
# ---------------------------------------------------------------------------


def test_first_claim_creates_one_lease(th: LifecycleHarness) -> None:  # 8
    with _tab(th) as client:
        patient_id, render_id = _open(client)

    assert _lease(th, patient_id)[:2] == (TAB_A, render_id)
    assert _tab_rows(th, "tab.claimed") == [(TAB_A, render_id, {"reason": "initial"})]


def test_same_claim_is_idempotent_and_refreshes(th: LifecycleHarness) -> None:  # 9
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        before = _lease(th, patient_id)[2]
        th.clock.advance(10)
        assert _claim(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT

    assert _lease(th, patient_id)[2] > before
    assert len(_tab_rows(th, "tab.claimed")) == 1


def test_same_tab_new_render_moves_the_lease(th: LifecycleHarness) -> None:  # 10
    with _tab(th) as client:
        patient_id, _ = _open(client)
        newer = _page(client, patient_id)
        assert _claim(client, patient_id, TAB_A, newer).status_code == HTTP_NO_CONTENT

    assert _lease(th, patient_id)[:2] == (TAB_A, newer)
    assert _tab_rows(th, "tab.claimed")[-1] == (TAB_A, newer, {"reason": "navigate"})


def test_second_live_tab_is_refused_and_audited(th: LifecycleHarness) -> None:  # 11, 12
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        render_b = _page(client, patient_id)
        refused = _claim(client, patient_id, TAB_B, render_b)

    assert refused.status_code == HTTP_CONFLICT
    assert refused.headers[CONFLICT_HEADER] == "conflict"
    for marker in ("ai", "no_ai", "arm", TAB_A):
        assert marker not in refused.text.split()
    assert _lease(th, patient_id)[:2] == (TAB_A, render_a)
    assert _tab_rows(th, "tab.conflict") == [(TAB_B, render_b, {"reason": "live_other_tab"})]


def test_stale_lease_is_expired_and_reclaimed(th: LifecycleHarness) -> None:  # 13
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        render_b = _page(client, patient_id)
        th.clock.advance(TAB_LEASE_TTL_SECONDS + 1)
        granted = _claim(client, patient_id, TAB_B, render_b)

    assert granted.status_code == HTTP_NO_CONTENT
    assert _tab_rows(th, "tab.lease_expired") == [(TAB_A, render_a, {"reason": "ttl"})]
    assert _tab_rows(th, "tab.claimed")[-1] == (TAB_B, render_b, {"reason": "reclaim"})
    assert _lease(th, patient_id)[:2] == (TAB_B, render_b)


def test_lease_of_a_closed_session_is_void(th: LifecycleHarness) -> None:  # 14
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        paused = client.post(
            f"/case/{patient_id}/pause",
            data={TAB_ID_FIELD: TAB_A, RENDER_ID_FIELD: render_a},
            follow_redirects=False,
        )
        assert paused.status_code == HTTP_SEE_OTHER
        client.post(f"/case/{patient_id}/resume", follow_redirects=False)
        render_b = _page(client, patient_id)
        granted = _claim(client, patient_id, TAB_B, render_b)

    assert granted.status_code == HTTP_NO_CONTENT
    assert _tab_rows(th, "tab.conflict") == []


def test_release_by_holder_is_idempotent(th: LifecycleHarness) -> None:  # 15
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        assert _release(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT
        assert _release(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT

    assert _lease(th, patient_id) is None
    assert _tab_rows(th, "tab.released") == [(TAB_A, render_id, {"reason": "pagehide"})]


def test_release_by_other_tab_or_old_render_keeps_lease(th: LifecycleHarness) -> None:  # 16
    with _tab(th) as client:
        patient_id, old = _open(client)
        newer = _page(client, patient_id)
        _claim(client, patient_id, TAB_A, newer)
        _release(client, patient_id, TAB_B, newer)
        _release(client, patient_id, TAB_A, old)  # the old page's late beacon

    assert _lease(th, patient_id)[:2] == (TAB_A, newer)
    assert _tab_rows(th, "tab.released") == []


def test_logout_releases_every_lease(th: LifecycleHarness) -> None:  # 17
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        client.post("/logout", follow_redirects=False)

    assert _lease(th, patient_id) is None
    assert _tab_rows(th, "tab.released") == [(TAB_A, render_id, {"reason": "logout"})]


def test_startup_clears_leases_but_not_events(th: LifecycleHarness) -> None:  # 18
    with _tab(th) as client:
        patient_id, _ = _open(client)
    with _tab(th):
        pass

    assert _lease(th, patient_id) is None
    assert len(_tab_rows(th, "tab.claimed")) == 1


def test_paused_case_cannot_claim(th: LifecycleHarness) -> None:  # 19
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        client.post(
            f"/case/{patient_id}/pause",
            data={TAB_ID_FIELD: TAB_A, RENDER_ID_FIELD: render_id},
            follow_redirects=False,
        )
        refused = _claim(client, patient_id, TAB_A, render_id)

    assert refused.status_code == HTTP_CONFLICT


def test_completed_case_cannot_claim(th: LifecycleHarness) -> None:  # 19
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        for t_index in range(LAST_T_INDEX + 1):
            headers = {**HX, **_owner(TAB_A, render_id)}
            _answer_all(client, patient_id, t_index, headers)
            response = client.post(
                f"/patient/{patient_id}/timepoint/{t_index}/advance", headers=headers
            )
            if t_index < LAST_T_INDEX:
                render_id = BeautifulSoup(response.text, "html.parser").select_one("#patient-view")[
                    "data-render-id"
                ]
        refused = _claim(client, patient_id, TAB_A, render_id)

    assert th.lifecycle(patient_id).state is CaseState.COMPLETED
    assert refused.status_code == HTTP_CONFLICT


def test_unknown_render_cannot_claim(th: LifecycleHarness) -> None:  # 20
    with _tab(th) as client:
        patient_id = _started_patient(_start(client))
        refused = _claim(client, patient_id, TAB_A, UNKNOWN_RENDER)

    assert refused.status_code == HTTP_CONFLICT
    assert _lease(th, patient_id) is None


def test_unguarded_case_needs_no_identity(harness: Harness) -> None:  # noqa: F811  # 20
    """No ``telemetry`` block: no render id, no claim, writes as before."""
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))
        page = client.get(f"/patient/{patient_id}/timepoint/0")
        answered = _answer(client, patient_id, 0, {})

    assert "data-tab-claim-url" not in page.text
    assert answered.status_code == HTTP_OK


@pytest.mark.parametrize(
    "body",
    [{}, {"tab_id": "nope", "render_id": UNKNOWN_RENDER}, {"tab_id": TAB_A, "render_id": "x"}],
)
def test_malformed_claim_is_422(th: LifecycleHarness, body: dict) -> None:
    with _tab(th) as client:
        patient_id = _started_patient(_start(client))
        response = client.post(f"/case/{patient_id}/tab/claim", json=body)

    assert response.status_code == HTTP_UNPROCESSABLE


def test_unknown_clinician_claim_is_401(th: LifecycleHarness) -> None:
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        client.cookies.set(COOKIE, "0" * 16)
        response = _claim(client, patient_id, TAB_A, render_id)

    assert response.status_code == HTTP_UNAUTHORIZED


# ---------------------------------------------------------------------------
# Protected writes (#21-#29)
# ---------------------------------------------------------------------------


def test_owner_answers_other_tab_and_anonymous_are_refused(th: LifecycleHarness) -> None:  # 21-23
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        render_b = _page(client, patient_id)
        other = _answer(client, patient_id, 0, _owner(TAB_B, render_b))
        missing = _answer(client, patient_id, 0, {})
        rows_after_refusals = _answer_rows(th)
        owner = _answer(client, patient_id, 0, _owner(TAB_A, render_a))

    assert other.status_code == missing.status_code == HTTP_CONFLICT
    assert other.headers[CONFLICT_HEADER] == missing.headers[CONFLICT_HEADER] == "conflict"
    assert rows_after_refusals == 0
    assert owner.status_code == HTTP_OK
    assert _answer_rows(th) == 1
    assert len(th.events("answer.upsert")) == 1


def test_owner_advance_moves_lease_other_tab_refused(th: LifecycleHarness) -> None:  # 24, 25
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        render_b = _page(client, patient_id)
        owner_headers = {**HX, **_owner(TAB_A, render_a)}
        _answer_all(client, patient_id, 0, owner_headers)
        refused = client.post(
            f"/patient/{patient_id}/timepoint/0/advance", headers={**HX, **_owner(TAB_B, render_b)}
        )
        frontier_after_refusal = th.count("SELECT COALESCE(MAX(unlocked_t_index), 0) FROM progress")
        advanced = client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=owner_headers)
        next_render = BeautifulSoup(advanced.text, "html.parser").select_one("#patient-view")[
            "data-render-id"
        ]
        next_answer = _answer(client, patient_id, 1, _owner(TAB_A, next_render))

    assert refused.status_code == HTTP_CONFLICT
    assert frontier_after_refusal == 0
    assert advanced.status_code == HTTP_OK
    assert _lease(th, patient_id)[:2] == (TAB_A, next_render)
    assert next_answer.status_code == HTTP_OK


def test_non_owner_heartbeat_does_not_touch(th: LifecycleHarness) -> None:  # 26
    with _tab(th) as client:
        patient_id, _ = _open(client)
        render_b = _page(client, patient_id)
        before = th.lifecycle(patient_id).last_seen_at
        th.clock.advance(20)
        refused = client.post(f"/case/{patient_id}/heartbeat", headers=_owner(TAB_B, render_b))

    assert refused.status_code == HTTP_CONFLICT
    assert th.lifecycle(patient_id).last_seen_at == before


def test_non_owner_pause_is_refused(th: LifecycleHarness) -> None:  # 27
    with _tab(th) as client:
        patient_id, _ = _open(client)
        render_b = _page(client, patient_id)
        refused = client.post(
            f"/case/{patient_id}/pause",
            data={TAB_ID_FIELD: TAB_B, RENDER_ID_FIELD: render_b},
            follow_redirects=False,
        )

    assert refused.status_code == HTTP_CONFLICT
    assert th.lifecycle(patient_id).state is CaseState.ACTIVE


def test_heartbeat_reacquires_after_restart(th: LifecycleHarness) -> None:  # 28
    with _tab(th) as client:
        patient_id, render_id = _open(client)
    with _tab(th) as client:
        beat = client.post(f"/case/{patient_id}/heartbeat", headers=_owner(TAB_A, render_id))

    assert beat.status_code == HTTP_NO_CONTENT
    assert _lease(th, patient_id)[:2] == (TAB_A, render_id)


def test_start_and_resume_need_no_lease(th: LifecycleHarness) -> None:  # 29
    with _tab(th) as client:
        started = _start(client)
        patient_id = _started_patient(started)
        render_id = _page(client, patient_id)
        before_claim = _answer(client, patient_id, 0, _owner(TAB_A, render_id))

    assert started.status_code == HTTP_SEE_OTHER
    # No lease yet: the first owner write acquires it (restart recovery path).
    assert before_claim.status_code == HTTP_OK
    assert _lease(th, patient_id)[:2] == (TAB_A, render_id)


# ---------------------------------------------------------------------------
# Telemetry (#30-#34)
# ---------------------------------------------------------------------------


def _complete_render(render_id: str) -> list[dict]:
    return [
        _event(render_id, 1, "browser.timepoint_enter"),
        _event(render_id, 2, "browser.timepoint_exit"),
    ]


def test_claimed_render_telemetry_accepted_unclaimed_refused(
    th: LifecycleHarness,
) -> None:  # 30, 31
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        render_b = _page(client, patient_id)
        accepted = _post(client, TAB_A, _complete_render(render_a))
        refused = _post(client, TAB_B, _complete_render(render_b))
        wrong_tab = _post(client, TAB_B, [_event(render_a, 9)])

    assert accepted.status_code == HTTP_NO_CONTENT
    assert refused.status_code == wrong_tab.status_code == HTTP_CONFLICT
    with th.conn() as conn:
        rows = telemetry.load_telemetry_rows(conn, th.clinician_id, patient_id)
    assert {(r.tab_id, r.render_id) for r in rows} == {(TAB_A, render_a)}


def test_granted_render_reports_after_release_and_completion(th: LifecycleHarness) -> None:  # 32
    with _tab(th) as client:
        patient_id, render_id = _open(client)
        _post(client, TAB_A, [_event(render_id, 1, "browser.timepoint_enter")])
        _release(client, patient_id, TAB_A, render_id)
        late = _post(client, TAB_A, [_event(render_id, 2, "browser.timepoint_exit")])

    assert late.status_code == HTTP_NO_CONTENT


def test_tab_rows_are_not_timeline_input(th: LifecycleHarness) -> None:  # 33, 34
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        _post(client, TAB_A, _complete_render(render_a))
        _claim(client, patient_id, TAB_B, _page(client, patient_id))

    with th.conn() as conn:
        rows = telemetry.load_telemetry_rows(conn, th.clinician_id, patient_id)
        inputs = load_case_inputs(conn, th.clinician_id, patient_id)
    assert all(not r.kind.startswith("tab.") for r in rows)
    assert inputs is not None
    timings = derive_observation_timings(
        inputs.renders, inputs.telemetry_rows, inactivity_threshold_seconds=60
    )
    assert timings[(0, "primary")].status is TelemetryStatus.COMPLETE


def test_browser_kinds_lockstep() -> None:
    from typing import get_args

    assert set(telemetry.BROWSER_KINDS) == set(get_args(BrowserKind))


# ---------------------------------------------------------------------------
# Multi tab derivation (#35-#37)
# ---------------------------------------------------------------------------


def test_refused_second_tab_flags_conflict_only(th: LifecycleHarness) -> None:  # 35, 36
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        _post(client, TAB_A, _complete_render(render_a))
        _claim(client, patient_id, TAB_B, _page(client, patient_id))
        _claim(client, patient_id, TAB_B, _page(client, patient_id, 0))

    with th.conn() as conn:
        variables = load_case_variables(conn, th.clinician_id, patient_id)
        inputs = load_case_inputs(conn, th.clinician_id, patient_id)
    assert variables is not None and inputs is not None
    first = variables.observations[0]
    assert first.tab_conflict_detected is True
    assert variables.observations[1].tab_conflict_detected is False
    assert [r.render_id for r in inputs.renders] == [render_a]


def _primary_timing(th: LifecycleHarness, patient_id: str) -> Any:
    with th.conn() as conn:
        inputs = load_case_inputs(conn, th.clinician_id, patient_id)
    assert inputs is not None
    return derive_observation_timings(
        inputs.renders,
        inputs.telemetry_rows,
        inactivity_threshold_seconds=60,
        tab_audit=inputs.tab_audit,
    )[(0, "primary")]


def test_release_then_claim_hands_over_cleanly(th: LifecycleHarness) -> None:  # 37b
    """Close the owner, Retry in the other tab: the tabs took turns, so sum."""
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        _post(client, TAB_A, _complete_render(render_a))
        render_b = _page(client, patient_id)
        assert _claim(client, patient_id, TAB_B, render_b).status_code == HTTP_CONFLICT
        _release(client, patient_id, TAB_A, render_a)
        assert _claim(client, patient_id, TAB_B, render_b).status_code == HTTP_NO_CONTENT
        _post(client, TAB_B, _complete_render(render_b))

    timing = _primary_timing(th, patient_id)
    assert timing.status is TelemetryStatus.COMPLETE
    assert timing.foreground_seconds == pytest.approx(0.2)  # 0.1 s per tab
    assert timing.tab_ids == frozenset({TAB_A, TAB_B})


def test_stale_handover_without_late_reports_takes_turns(th: LifecycleHarness) -> None:  # 37
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        _post(client, TAB_A, [_event(render_a, 1, "browser.timepoint_enter")])
        th.clock.advance(TAB_LEASE_TTL_SECONDS + 1)
        render_b = _page(client, patient_id)
        assert _claim(client, patient_id, TAB_B, render_b).status_code == HTTP_NO_CONTENT
        _post(client, TAB_B, _complete_render(render_b))

    # Tab A never exited: its seconds are a lower bound, so the sum is too.
    assert _primary_timing(th, patient_id).status is TelemetryStatus.INCOMPLETE


def test_owner_reporting_after_losing_the_lease_stays_multi_tab(th: LifecycleHarness) -> None:
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        _post(client, TAB_A, [_event(render_a, 1, "browser.timepoint_enter")])
        th.clock.advance(TAB_LEASE_TTL_SECONDS + 1)
        render_b = _page(client, patient_id)
        _claim(client, patient_id, TAB_B, render_b)
        _post(client, TAB_B, _complete_render(render_b))
        _post(client, TAB_A, [_event(render_a, 2, "browser.timepoint_exit")])  # A was alive

    timing = _primary_timing(th, patient_id)
    assert timing.status is TelemetryStatus.MULTI_TAB
    assert timing.foreground_seconds is None


# ---------------------------------------------------------------------------
# Backup identity (#38-#45)
# ---------------------------------------------------------------------------

FROZEN = "2026-09-27T14:00:00Z"
STAMP = "20260927T140000Z"


def _bound_db(path: Path, study_id: str) -> Path:
    conn = connect(path)
    try:
        apply_migrations(conn)
        bind(conn, study_id)
    finally:
        conn.close()
    return path


@freeze_time(FROZEN)
def test_study_backup_path_and_identity(tmp_path: Path) -> None:  # 38, 39
    db = _bound_db(tmp_path / "study.db", "icu_ai_phase2")
    dest = create_backup(db, tmp_path / "backups")

    version = len(MIGRATIONS)
    expected = tmp_path / "backups" / "icu_ai_phase2"
    assert dest == expected / f"study_icu_ai_phase2_schema_{version}_{STAMP}.db"
    copy = connect(dest)
    try:
        identity = read_identity(copy)
    finally:
        copy.close()
    assert (identity.study_id, identity.schema_version) == ("icu_ai_phase2", version)


def test_mismatching_expected_study_refuses(tmp_path: Path) -> None:  # 40
    db = _bound_db(tmp_path / "study.db", "study_a")
    with pytest.raises(BackupIdentityError, match="study_a"):
        create_backup(db, tmp_path / "backups", expected_study_id="study_b")
    assert not (tmp_path / "backups").exists() or not any((tmp_path / "backups").rglob("*.db"))


@freeze_time(FROZEN)
def test_two_studies_get_separate_directories(tmp_path: Path) -> None:  # 41
    a = create_backup(_bound_db(tmp_path / "a.db", "study_a"), tmp_path / "backups")
    b = create_backup(_bound_db(tmp_path / "b.db", "study_b"), tmp_path / "backups")

    assert a.parent.name == "study_a" and b.parent.name == "study_b"
    assert a.parent != b.parent


@freeze_time(FROZEN)
def test_existing_destination_is_never_overwritten(tmp_path: Path) -> None:  # 42
    db = _bound_db(tmp_path / "study.db", "study_a")
    first = create_backup(db, tmp_path / "backups")
    first_bytes = first.read_bytes()
    second = create_backup(db, tmp_path / "backups")

    assert second != first
    assert second.name == f"{first.stem}_2.db"
    assert first.read_bytes() == first_bytes


def test_backup_writes_only_the_copy(tmp_path: Path) -> None:  # 44
    db = _bound_db(tmp_path / "study.db", "study_a")
    create_backup(db, tmp_path / "backups")

    written = [p for p in (tmp_path / "backups").rglob("*") if p.is_file()]
    assert len(written) == 1 and written[0].suffix == ".db"


def test_shutdown_backup_uses_study_path(th: LifecycleHarness) -> None:  # 45
    with _tab(th) as client:
        _open(client)

    study_dir = th.tmp_path / "backups" / "fixture_lifecycle"
    backups = list(study_dir.glob("study_fixture_lifecycle_schema_*_*.db"))
    assert len(backups) == 1


def test_cli_backup_reports_study(th: LifecycleHarness) -> None:
    from typer.testing import CliRunner

    from ehr_simulator import cli

    result = CliRunner().invoke(
        cli.app_typer,
        ["backup", "--db-path", str(th.db_path), "--backup-dir", str(th.tmp_path / "cli")],
    )
    assert result.exit_code == 0, result.output
    assert "Study: fixture_lifecycle, schema version" in result.output


def test_lifecycle_row_untouched_by_guard(th: LifecycleHarness) -> None:
    """Refusals never move the arm or the lifecycle (invariant 8)."""
    with _tab(th) as client:
        patient_id, _ = _open(client)
        before = th.dump()
        render_b = _page(client, patient_id)
        _claim(client, patient_id, TAB_B, render_b)
        _answer(client, patient_id, 0, _owner(TAB_B, render_b))

    after = th.dump()
    assert after["arm_assignments"] == before["arm_assignments"]
    with th.conn() as conn:
        assert lifecycle_dao.fetch(conn, th.clinician_id, patient_id).state is CaseState.ACTIVE


def test_revisit_conflict_does_not_flag_the_primary_row(th: LifecycleHarness) -> None:
    """Review fix: the flag belongs to the conflict's own (t_index, visit_kind)."""
    with _tab(th) as client:
        patient_id, render_a = _open(client)
        headers = {**HX, **_owner(TAB_A, render_a)}
        _answer_all(client, patient_id, 0, headers)
        client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=headers)
        revisit = _page(client, patient_id, 0)
        assert _claim(client, patient_id, TAB_B, revisit).status_code == HTTP_CONFLICT

    with th.conn() as conn:
        variables = load_case_variables(conn, th.clinician_id, patient_id)
    assert variables is not None
    assert [o.tab_conflict_detected for o in variables.observations] == [False, False, False]


def test_unmigrated_db_backup_refuses(tmp_path: Path) -> None:
    """Review fix: no schema version → no backup, bound or not."""
    db = tmp_path / "plain.db"
    conn = connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()

    with pytest.raises(BackupIdentityError, match="schema version"):
        create_backup(db, tmp_path / "backups")
    assert not (tmp_path / "backups").exists() or not any((tmp_path / "backups").iterdir())
