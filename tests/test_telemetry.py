"""S11j: telemetry configuration, render identity and the telemetry endpoint.

Route tests drive the real Phase 2 app on ``study_randomised.yaml`` plus the
first use case ``telemetry`` block (``Harness.variant``)::

    GET / advance ──► timepoint.render {render_id, t_index, visit_kind, ai}
    page          ──► POST /telemetry/events {tab_id, events[render_id …]}
    server        ──► events rows with the render's session/patient/timepoint
"""

from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ehr_simulator.cli_support import walk_preflight
from ehr_simulator.config import compute_config_hash_from_models, load_questions
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db import connect, events
from ehr_simulator.db.migrations import apply_migrations
from ehr_simulator.ingestion import load_synthetic
from ehr_simulator.web.telemetry import MAX_BATCH_BYTES, MAX_BATCH_EVENTS
from tests.conftest import answer_all_required
from tests.test_case_start import Harness, _start, _started_patient, harness  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures" / "study"
TELEMETRY = {
    "inactivity_threshold_seconds": 60,
    "panel_viewport_threshold": 0.05,
    "panel_viewed_threshold_seconds": 2.0,
}
TELEMETRY_URL = "/telemetry/events"
HX = {"HX-Request": "true"}
HTTP_OK = 200
HTTP_NO_CONTENT = 204
HTTP_CONFLICT = 409
HTTP_PRECONDITION_FAILED = 412
HTTP_UNPROCESSABLE = 422
HTTP_UNAUTHORIZED = 401
RENDER_KIND = "timepoint.render"
OTHER_CLINICIAN = "Dr. Other"


def _study_dict(**changes: Any) -> dict[str, Any]:
    data = yaml.safe_load((FIXTURES / "study_randomised.yaml").read_text())
    data.update(changes)
    return data


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_first_use_case_block_loads() -> None:
    study = StudyConfig.model_validate(_study_dict(telemetry=TELEMETRY))

    assert study.telemetry is not None
    assert study.telemetry.inactivity_threshold_seconds == 60
    assert study.telemetry.panel_viewport_threshold == 0.05
    assert study.telemetry.panel_viewed_threshold_seconds == 2.0


def test_absent_block_is_omitted_from_serialization() -> None:
    study = StudyConfig.model_validate(_study_dict())

    assert "telemetry" not in study.model_dump()


@pytest.mark.parametrize("value", [0, -1, math.inf, math.nan, True])
def test_bad_inactivity_threshold_rejected(value: Any) -> None:
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(
            _study_dict(telemetry={**TELEMETRY, "inactivity_threshold_seconds": value})
        )


@pytest.mark.parametrize("value", [0, -0.1, 1.01, math.nan])
def test_bad_viewport_threshold_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(
            _study_dict(telemetry={**TELEMETRY, "panel_viewport_threshold": value})
        )


def test_full_viewport_threshold_accepted() -> None:
    study = StudyConfig.model_validate(
        _study_dict(telemetry={**TELEMETRY, "panel_viewport_threshold": 1.0})
    )
    assert study.telemetry is not None and study.telemetry.panel_viewport_threshold == 1.0


@pytest.mark.parametrize("value", [0, -2.0])
def test_bad_viewed_threshold_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(
            _study_dict(telemetry={**TELEMETRY, "panel_viewed_threshold_seconds": value})
        )


def test_block_changes_config_hash() -> None:
    questions = load_questions(FIXTURES / "questions.yaml")
    without = compute_config_hash_from_models(StudyConfig.model_validate(_study_dict()), questions)
    with_block = compute_config_hash_from_models(
        StudyConfig.model_validate(_study_dict(telemetry=TELEMETRY)), questions
    )
    assert without != with_block


def test_preflight_fails_phase2_without_telemetry() -> None:
    questions = load_questions(FIXTURES / "questions.yaml")
    without = walk_preflight(StudyConfig.model_validate(_study_dict()), questions, load_synthetic())
    with_block = walk_preflight(
        StudyConfig.model_validate(_study_dict(telemetry=TELEMETRY)), questions, load_synthetic()
    )

    assert any("declares no telemetry" in r.message for r in without.rows if r.status == "FAIL")
    assert not with_block.has_fail


def test_unknown_telemetry_key_rejected() -> None:
    with pytest.raises(ValidationError):
        StudyConfig.model_validate(_study_dict(telemetry={**TELEMETRY, "sample_rate": 1}))


# ---------------------------------------------------------------------------
# Render identity
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry_harness(harness: Harness) -> tuple[Harness, Any]:  # noqa: F811
    config = harness.variant("v2", telemetry=TELEMETRY)
    harness.activate(config)
    return harness, config


def _renders(client: TestClient) -> list[tuple[str, str, float, dict]]:
    rows = client.app.state.db.execute(
        "SELECT render_id, session_id, timepoint, payload_json FROM events "
        "WHERE kind = ? ORDER BY event_id",
        (RENDER_KIND,),
    ).fetchall()
    return [(r[0], r[1], r[2], json.loads(r[3])) for r in rows]


def _view(html: str) -> Any:
    return BeautifulSoup(html, "html.parser").select_one("#patient-view")


def _open_case(client: TestClient) -> tuple[str, str]:
    """Start a case and GET its first timepoint; return (patient, render_id)."""
    patient_id = _started_patient(_start(client))
    page = client.get(f"/patient/{patient_id}/timepoint/0")
    assert page.status_code == HTTP_OK
    return patient_id, _view(page.text)["data-render-id"]


def test_get_writes_one_render_matching_the_page(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _patient, render_id = _open_case(client)
        renders = _renders(client)

    assert len(renders) == 1
    stored_id, session_id, timepoint, payload = renders[0]
    assert stored_id == render_id
    assert session_id is not None and timepoint == 0.0
    assert payload["t_index"] == 0 and payload["visit_kind"] == "primary"
    assert payload["ai"] in ("shown", "none")


def test_page_carries_telemetry_attributes(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id = _started_patient(_start(client))
        view = _view(client.get(f"/patient/{patient_id}/timepoint/0").text)

    assert view["data-telemetry-url"] == TELEMETRY_URL
    assert float(view["data-viewport-threshold"]) == 0.05


def test_config_without_telemetry_renders_no_identity(harness: Harness) -> None:  # noqa: F811
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))
        view = _view(client.get(f"/patient/{patient_id}/timepoint/0").text)
        renders = _renders(client)

    assert not view.has_attr("data-render-id")
    assert not view.has_attr("data-telemetry-url")
    assert renders == []


def test_advance_writes_render_for_next_pane(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, _ = _open_case(client)
        answer_all_required(client, patient_id, 0)
        response = client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        renders = _renders(client)

    assert response.status_code == HTTP_OK
    assert [r[3]["t_index"] for r in renders] == [0, 1]
    assert _view(response.text)["data-render-id"] == renders[1][0]


def test_blocked_and_stale_advance_write_no_render(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, _ = _open_case(client)
        blocked = client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        answer_all_required(client, patient_id, 0)
        client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        before = len(_renders(client))
        stale = client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        after = len(_renders(client))

    assert blocked.status_code == HTTP_CONFLICT
    assert stale.status_code == HTTP_PRECONDITION_FAILED
    assert not _view(stale.text).has_attr("data-render-id")
    assert before == after == 2


def test_revisit_render_is_labelled_revisit(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, _ = _open_case(client)
        answer_all_required(client, patient_id, 0)
        client.post(f"/patient/{patient_id}/timepoint/0/advance", headers=HX)
        client.get(f"/patient/{patient_id}/timepoint/0")
        renders = _renders(client)

    assert [(r[3]["t_index"], r[3]["visit_kind"]) for r in renders] == [
        (0, "primary"),
        (1, "primary"),
        (0, "revisit"),
    ]


def test_render_ids_are_unique_per_render(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, first = _open_case(client)
        second = _view(client.get(f"/patient/{patient_id}/timepoint/0").text)["data-render-id"]

    assert first != second


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

TAB_ID = "0b6f7c1e-3f5a-4c2d-9e8b-7a6d5c4b3a21"
OTHER_TAB_ID = "5d1c9a0e-8b7f-4e6d-a5c4-3b2a1f0e9d8c"


def _event(render_id: str, seq: int, kind: str = "browser.state", **overrides: Any) -> dict:
    payloads = {
        "browser.timepoint_enter": {"visible": True, "focused": True},
        "browser.state": {"visible": True, "focused": False, "reason": "blur"},
        "browser.activity": {"activity_kind": "click"},
        "browser.timepoint_exit": {"reason": "swap"},
        "browser.gap": {"dropped": 3},
        "panel.mount": {
            "panel_id": "vitals",
            "expanded": True,
            "collapsible": True,
            "state": "loading",
        },
        "panel.viewport": {"panel_id": "vitals", "intersection_ratio": 0.049},
        "panel.open": {"panel_id": "labs"},
        "panel.close": {"panel_id": "admission"},
    }
    event = {
        "kind": kind,
        "render_id": render_id,
        "client_seq": seq,
        "client_mono_ms": 100.0 * seq,
        "client_ts": "2026-09-27T10:00:00.000Z",
        "payload": payloads[kind],
    }
    event.update(overrides)
    return event


def _post(client: TestClient, events_: list[dict], tab_id: str = TAB_ID) -> Any:
    return client.post(TELEMETRY_URL, json={"tab_id": tab_id, "events": events_})


def _browser_rows(client: TestClient) -> list[tuple]:
    return client.app.state.db.execute(
        "SELECT kind, session_id, patient_id, timepoint, tab_id, render_id, client_seq, "
        "client_mono_ms, payload_json FROM events WHERE tab_id IS NOT NULL ORDER BY event_id"
    ).fetchall()


ALL_KINDS = (
    "browser.timepoint_enter",
    "browser.state",
    "browser.activity",
    "browser.timepoint_exit",
    "browser.gap",
    "panel.mount",
    "panel.viewport",
    "panel.open",
    "panel.close",
)


def test_batch_inherits_render_context(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, render_id = _open_case(client)
        batch = [_event(render_id, i + 1, kind) for i, kind in enumerate(ALL_KINDS)]
        response = _post(client, batch)
        rows = _browser_rows(client)
        render = _renders(client)[0]

    assert response.status_code == HTTP_NO_CONTENT
    assert [r[0] for r in rows] == list(ALL_KINDS)
    for kind, session_id, pid, timepoint, tab_id, rid, _seq, mono, _payload in rows:
        assert (session_id, pid, timepoint) == (render[1], patient_id, render[2]), kind
        assert (tab_id, rid) == (TAB_ID, render_id)
        assert mono > 0


def test_foreign_render_refused(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
    other = study.add_clinician(OTHER_CLINICIAN)
    with study.boot(config, clinician_id=other) as client:
        response = _post(client, [_event(render_id, 1)])
        rows = _browser_rows(client)

    assert response.status_code == HTTP_CONFLICT
    assert rows == []


def test_unknown_render_refuses_whole_batch(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        response = _post(client, [_event(render_id, 1), _event(uuid.uuid4().hex, 2)])
        rows = _browser_rows(client)

    assert response.status_code == HTTP_CONFLICT
    assert rows == []


BAD_EVENTS = {
    "unknown kind": {"kind": "browser.keystroke"},
    "payload override": {
        "payload": {"visible": True, "focused": True, "reason": "blur", "patient_id": "x"}
    },
    "answer value": {
        "kind": "browser.activity",
        "payload": {"activity_kind": "keyboard", "key": "a"},
    },
    "bad reason": {"payload": {"visible": True, "focused": True, "reason": "advance"}},
    "negative mono": {"client_mono_ms": -1},
    "string mono": {"client_mono_ms": "12"},
    "zero seq": {"client_seq": 0},
    "extra field": {"arm": "ai"},
    "bad render id": {"render_id": "not-a-render"},
    "ratio above one": {
        "kind": "panel.viewport",
        "payload": {"panel_id": "ai", "intersection_ratio": 1.5},
    },
    "unknown panel": {"kind": "panel.open", "payload": {"panel_id": "questions"}},
    "unparseable client_ts": {"client_ts": "yesterday"},
    "naive client_ts": {"client_ts": "2026-09-27T10:00:00"},
    "string bool": {"payload": {"visible": "true", "focused": True, "reason": "blur"}},
}


@pytest.mark.parametrize("override", BAD_EVENTS.values(), ids=BAD_EVENTS.keys())
def test_malformed_event_refused(telemetry_harness: Any, override: dict) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        good = _event(render_id, 1)
        bad = {**_event(render_id, 2), **override}
        response = _post(client, [good, bad])
        rows = _browser_rows(client)

    assert response.status_code == HTTP_UNPROCESSABLE
    assert rows == []


def test_non_finite_mono_refused(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        body = json.dumps({"tab_id": TAB_ID, "events": [_event(render_id, 1)]})
        body = body.replace('"client_mono_ms": 100.0', '"client_mono_ms": NaN')
        response = client.post(
            TELEMETRY_URL, content=body, headers={"Content-Type": "application/json"}
        )

    assert response.status_code == HTTP_UNPROCESSABLE


@pytest.mark.parametrize("tab_id", ["", "tab-1", TAB_ID.upper()])
def test_bad_tab_id_refused(telemetry_harness: Any, tab_id: str) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        response = _post(client, [_event(render_id, 1)], tab_id=tab_id)

    assert response.status_code == HTTP_UNPROCESSABLE


def test_empty_oversized_and_too_long_batches_refused(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        empty = _post(client, [])
        too_many = _post(client, [_event(render_id, i + 1) for i in range(MAX_BATCH_EVENTS + 1)])
        huge = client.post(
            TELEMETRY_URL,
            content=b"x" * (MAX_BATCH_BYTES + 1),
            headers={"Content-Type": "application/json"},
        )
        garbage = client.post(
            TELEMETRY_URL, content=b"{not json", headers={"Content-Type": "application/json"}
        )

    assert {r.status_code for r in (empty, too_many, huge, garbage)} == {HTTP_UNPROCESSABLE}


def test_redelivery_is_idempotent(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        batch = [_event(render_id, 1), _event(render_id, 2, "browser.activity")]
        first = _post(client, batch)
        second = _post(client, batch)
        rows = _browser_rows(client)

    assert first.status_code == second.status_code == HTTP_NO_CONTENT
    assert len(rows) == 2


def test_same_seq_from_another_tab_is_kept(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        _post(client, [_event(render_id, 1)])
        _post(client, [_event(render_id, 1)], tab_id=OTHER_TAB_ID)
        rows = _browser_rows(client)

    assert len(rows) == 2


def test_telemetry_is_not_case_contact(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, render_id = _open_case(client)
        db = client.app.state.db
        before = db.execute(
            "SELECT last_seen_at FROM case_lifecycle WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        _post(client, [_event(render_id, 1)])
        after = db.execute(
            "SELECT last_seen_at FROM case_lifecycle WHERE patient_id = ?", (patient_id,)
        ).fetchone()

    assert before == after


def test_late_events_accepted_after_completion(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        patient_id, _ = _open_case(client)
        for t_index in range(3):
            answer_all_required(client, patient_id, t_index)
            client.post(f"/patient/{patient_id}/timepoint/{t_index}/advance", headers=HX)
        last_render = _renders(client)[-1][0]
        response = _post(client, [_event(last_render, 50, "browser.timepoint_exit")])

    assert response.status_code == HTTP_NO_CONTENT


def test_logged_out_post_is_401_not_a_login_redirect(telemetry_harness: Any) -> None:
    # fetch() follows redirects: a 303 to /login would read as a 200 success.
    study, config = telemetry_harness
    with study.boot(config) as client:
        client.cookies.clear()
        response = client.post(
            TELEMETRY_URL, json={"tab_id": TAB_ID, "events": []}, follow_redirects=False
        )

    assert response.status_code == HTTP_UNAUTHORIZED
    assert "location" not in response.headers
    assert "HX-Redirect" not in response.headers


def test_unactivated_patient_has_no_render(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        response = client.get("/patient/synth_003/timepoint/0", follow_redirects=False)
        renders = _renders(client)

    assert response.headers["location"] == "/"
    assert renders == []


def test_stored_payloads_hold_no_identity_or_values(telemetry_harness: Any) -> None:
    study, config = telemetry_harness
    with study.boot(config) as client:
        _, render_id = _open_case(client)
        _post(client, [_event(render_id, i + 1, kind) for i, kind in enumerate(ALL_KINDS)])
        payloads = [json.loads(r[8]) for r in _browser_rows(client)]
        payloads += [r[3] for r in _renders(client)]

    forbidden = {"name_normalized", "clinician", "value", "answer", "text", "key", "x", "y"}
    assert all(not (forbidden & set(p)) for p in payloads)


# ---------------------------------------------------------------------------
# DAO and migration
# ---------------------------------------------------------------------------


def test_migration_adds_nullable_columns(tmp_path: Path) -> None:
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn)
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(events)")}
    finally:
        conn.close()

    for name in ("tab_id", "render_id", "client_mono_ms"):
        assert name in columns and columns[name][3] == 0  # notnull flag off


@pytest.mark.parametrize("mono", [-0.5, math.inf, math.nan])
def test_dao_refuses_bad_mono(tmp_path: Path, mono: float) -> None:
    row = events.BrowserEvent(
        session_id=None,
        clinician_id="c",
        patient_id="p",
        timepoint=0.0,
        kind="browser.state",
        payload={},
        client_ts=None,
        client_seq=1,
        tab_id=TAB_ID,
        render_id="r",
        client_mono_ms=mono,
    )
    conn = connect(tmp_path / "db.sqlite")
    try:
        apply_migrations(conn)
        with pytest.raises(ValueError, match="client_mono_ms"):
            events.append_browser_batch(conn, [row])
    finally:
        conn.close()
