"""S11e: case lifecycle, reconnection, voluntary pause and clinician stopping.

Drives the real app on ``study_lifecycle.yaml`` (Phase 2: synth_001..003,
timepoints [0, 60, 180], reconnection grace 300 s, pause grace 600 s,
target 2 completed / max 3 activated) with an injected clock::

    POST /case/start ─► active ─► pause ─► paused ─► resume ─► active
                          │                  │
                          ├─ final advance ─► completed
                          └─ silent > grace ─► incomplete  ◄─ pause > grace

Test numbers refer to ``specs/session-11e-case-lifecycle.md``.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError
from typer.testing import CliRunner

from ehr_simulator import cli
from ehr_simulator.config import compute_config_hash_from_models, load_questions
from ehr_simulator.config.snapshot import render_study_snapshot
from ehr_simulator.config.study import (
    MIN_RECONNECTION_GRACE_SECONDS,
    CaseLifecycleConfig,
    StudyConfig,
)
from ehr_simulator.db import apply_migrations, connect
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import migrations as migrations_module
from ehr_simulator.db.case_lifecycle import CaseState, IncompleteReason
from ehr_simulator.db.exceptions import CaseLifecycleError
from ehr_simulator.web import case_start, routes
from ehr_simulator.web.case_contact import HEARTBEAT_INTERVAL_SECONDS, RECONNECT_GAP_SECONDS
from tests.conftest import _seed_clinician, answer_all_required, seed_progress
from tests.test_case_start import (
    ARM_MARKERS,
    HTTP_CONFLICT,
    HTTP_OK,
    HTTP_SEE_OTHER,
    INDEX_URL,
    LAST_T_INDEX,
    SECOND_CLINICIAN,
    Config,
    Harness,
    _start,
    _started_patient,
)

HTTP_NO_CONTENT = 204
HTTP_PRECONDITION_FAILED = 412
HX = {"HX-Request": "true"}
GRACE = 300
PAUSE_GRACE = 600
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

VALID_LIFECYCLE: dict[str, Any] = {
    "reconnection_grace_seconds": GRACE,
    "voluntary_pause_enabled": True,
    "voluntary_pause_grace_seconds": PAUSE_GRACE,
    "target_completed_cases_per_clinician": 2,
    "max_activated_cases_per_clinician": 3,
}


class FakeClock:
    """Injected ``app.state.clock``: time moves only when a test says so."""

    def __init__(self) -> None:
        self.moment = T0

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, seconds: float) -> None:
        self.moment += timedelta(seconds=seconds)


class LifecycleHarness(Harness):
    clock: FakeClock

    @contextmanager
    def client(
        self, config: Config | None = None, clinician_id: str | None = None
    ) -> Iterator[TestClient]:
        with self.boot(config, clinician_id) as client:
            client.app.state.clock = self.clock
            yield client

    def lifecycle(self, patient_id: str, clinician_id: str | None = None):
        with self.conn() as conn:
            return lifecycle_dao.fetch(conn, clinician_id or self.clinician_id, patient_id)

    def events(self, kind: str) -> list[dict[str, Any]]:
        with self.conn() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM events WHERE kind = ? ORDER BY event_id", (kind,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def open_sessions(self, patient_id: str) -> int:
        return self.count(
            "SELECT COUNT(*) FROM sessions WHERE clinician_id = ? AND patient_id = ? "
            "AND ended_at IS NULL",
            (self.clinician_id, patient_id),
        )


def _harness(tmp_path: Path, study_yaml: Path, questions_yaml: Path) -> LifecycleHarness:
    v1 = Config("v1", study_yaml, questions_yaml)
    db_path = tmp_path / "study.db"
    clinician_id = _seed_clinician(db_path, study_id=v1.study.study_id)
    h = LifecycleHarness(tmp_path, db_path, clinician_id, v1)
    h.clock = FakeClock()
    h.activate(v1)
    return h


@pytest.fixture
def lh(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    return _harness(
        tmp_path, study_fixture_dir / "study_lifecycle.yaml", study_fixture_dir / "questions.yaml"
    )


def _with_lifecycle(tmp_path: Path, study_fixture_dir: Path, **overrides: Any) -> LifecycleHarness:
    """A fresh harness whose v1 carries ``VALID_LIFECYCLE`` with ``overrides``."""
    data = yaml.safe_load((study_fixture_dir / "study_lifecycle.yaml").read_text())
    data["case_lifecycle"] = {**VALID_LIFECYCLE, **overrides}
    path = tmp_path / "study_variant.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, study_fixture_dir / "questions.yaml")


def _url(patient_id: str, t_index: int = 0) -> str:
    return f"/patient/{patient_id}/timepoint/{t_index}"


def _answer(client: TestClient, patient_id: str, t_index: int = 0, **headers: str):
    return client.post(
        f"{_url(patient_id, t_index)}/answer",
        data={"question_id": "deterioration_6h", "value": "Yes"},
        headers=headers,
    )


def _advance(client: TestClient, patient_id: str, t_index: int):
    return client.post(f"{_url(patient_id, t_index)}/advance", follow_redirects=False)


def _ready_final(client: TestClient, patient_id: str) -> None:
    """Frontier at the last timepoint with every required question answered."""
    seed_progress(client, patient_id, LAST_T_INDEX)
    answer_all_required(client, patient_id, LAST_T_INDEX)


def _finish(client: TestClient, patient_id: str) -> None:
    _ready_final(client, patient_id)
    assert _advance(client, patient_id, LAST_T_INDEX).status_code == HTTP_SEE_OTHER


def _abandon(lh: LifecycleHarness, patient_id: str) -> None:
    from ehr_simulator import case_lifecycle

    with lh.conn() as conn:
        case_lifecycle.abandon(
            conn, clinician_id=lh.clinician_id, patient_id=patient_id, now=lh.clock()
        )


def _post(client: TestClient, path: str, **headers: str):
    return client.post(path, headers=headers, follow_redirects=False)


# ---------------------------------------------------------------------------
# Configuration (#1-#7)
# ---------------------------------------------------------------------------


def _study(**lifecycle: Any) -> StudyConfig:
    return StudyConfig.model_validate(
        {
            "schema_version": "2",
            "study_id": "s",
            "dataset": "synthetic",
            "patient_ids": ["synth_001"],
            "time_unit": "minutes",
            "timepoints": [0],
            **({"case_lifecycle": lifecycle} if lifecycle else {}),
        }
    )


def test_v2_config_without_lifecycle_parses(study_fixture_dir: Path) -> None:
    config = Config("v", study_fixture_dir / "study_randomised.yaml", Path())
    assert config.study.case_lifecycle is None


def test_absent_section_is_absent_from_the_snapshot() -> None:
    assert "case_lifecycle" not in render_study_snapshot(_study())
    assert "case_lifecycle" in render_study_snapshot(_study(**VALID_LIFECYCLE))


def test_lifecycle_section_changes_config_hash(study_fixture_dir: Path) -> None:
    questions = load_questions(study_fixture_dir / "questions.yaml")
    without = compute_config_hash_from_models(_study(), questions)
    with_section = compute_config_hash_from_models(_study(**VALID_LIFECYCLE), questions)
    other_grace = compute_config_hash_from_models(
        _study(**{**VALID_LIFECYCLE, "reconnection_grace_seconds": GRACE + 1}), questions
    )
    assert len({without, with_section, other_grace}) == 3


@pytest.mark.parametrize(
    "overrides",
    [
        {"reconnection_grace_seconds": MIN_RECONNECTION_GRACE_SECONDS - 1},
        {"voluntary_pause_grace_seconds": 0},
        {"voluntary_pause_enabled": False},  # grace still set → ambiguous
        {"target_completed_cases_per_clinician": 0},
        {"max_activated_cases_per_clinician": 1},  # below target 2
        {"study_target_completed_cases": 0},
        {"unknown_key": 1},
        {"voluntary_pause_enabled": "yes"},
    ],
)
def test_invalid_lifecycle_settings_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CaseLifecycleConfig.model_validate({**VALID_LIFECYCLE, **overrides})


def test_pause_grace_falls_back_to_reconnection_grace_at_use() -> None:
    config = CaseLifecycleConfig.model_validate(
        {**VALID_LIFECYCLE, "voluntary_pause_grace_seconds": None}
    )
    assert config.effective_pause_grace_seconds == GRACE
    assert config.model_dump()["voluntary_pause_grace_seconds"] is None

    disabled = CaseLifecycleConfig.model_validate(
        {**VALID_LIFECYCLE, "voluntary_pause_enabled": False, "voluntary_pause_grace_seconds": None}
    )
    assert disabled.effective_pause_grace_seconds is None


def test_heartbeat_interval_stays_well_below_the_grace_floor() -> None:
    background_tab_wakeup_seconds = 60
    assert 3 * background_tab_wakeup_seconds <= MIN_RECONNECTION_GRACE_SECONDS
    assert HEARTBEAT_INTERVAL_SECONDS * 4 <= MIN_RECONNECTION_GRACE_SECONDS
    assert RECONNECT_GAP_SECONDS < MIN_RECONNECTION_GRACE_SECONDS


def test_validate_config_warns_on_randomisation_without_lifecycle(
    study_fixture_dir: Path,
) -> None:
    runner = CliRunner()
    questions = str(study_fixture_dir / "questions.yaml")
    bare = runner.invoke(
        cli.app_typer,
        ["validate-config", str(study_fixture_dir / "study_randomised.yaml"), questions],
    )
    full = runner.invoke(
        cli.app_typer,
        ["validate-config", str(study_fixture_dir / "study_lifecycle.yaml"), questions],
    )
    assert bare.exit_code == 0
    assert "Warning: randomisation without case_lifecycle" in bare.stderr
    assert full.exit_code == 0
    assert "Warning" not in full.stderr


# ---------------------------------------------------------------------------
# Migration (#8-#10)
# ---------------------------------------------------------------------------


def test_migration_8_backfills_realised_cases(tmp_db_path: Path, monkeypatch) -> None:
    all_migrations = migrations_module.MIGRATIONS
    monkeypatch.setattr(migrations_module, "MIGRATIONS", all_migrations[:7])
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    conn.execute("INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c1', 'c1')")
    for pid, position in (("done", 1), ("seen", 2), ("quiet", 3)):
        conn.execute(
            "INSERT INTO arm_assignments (clinician_id, patient_id, arm, arm_source, seed, "
            "config_hash, config_version, schedule_id, case_position, activated_at) "
            "VALUES ('c1', ?, 'ai', 'phase2_randomized', 1, 'h', 'v1', 's', ?, "
            "'2026-01-01 10:00:00')",
            (pid, position),
        )
    conn.execute(
        "INSERT INTO arm_assignments (clinician_id, patient_id, arm, arm_source, config_hash) "
        "VALUES ('c1', 'stub', 'no_ai', 'phase1_stub', 'h')"
    )
    conn.execute(
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, completed_at, "
        "config_hash) VALUES ('c1', 'done', 2, '2026-01-01 11:00:00', 'h')"
    )
    conn.execute(
        "INSERT INTO events (clinician_id, patient_id, kind, server_ts) "
        "VALUES ('c1', 'seen', 'answer.upsert', '2026-01-01 10:30:00')"
    )
    conn.commit()

    monkeypatch.setattr(migrations_module, "MIGRATIONS", all_migrations)
    assert apply_migrations(conn) == [8]
    rows = lifecycle_dao.list_for_clinician(conn, "c1")
    conn.close()

    at = lambda text: datetime.fromisoformat(text).replace(tzinfo=UTC)  # noqa: E731
    assert set(rows) == {"done", "seen", "quiet"}  # no phase1_stub row
    assert rows["done"].state is CaseState.COMPLETED
    assert rows["done"].completed_at == at("2026-01-01 11:00:00")
    assert rows["seen"].state is CaseState.ACTIVE
    assert rows["seen"].last_seen_at == at("2026-01-01 10:30:00")
    assert rows["quiet"].last_seen_at == at("2026-01-01 10:00:00")
    assert all(r.paused_at is None and r.incomplete_at is None for r in rows.values())


def test_schema_refuses_invalid_state_terminal_updates_and_deletes(
    lh: LifecycleHarness,
) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _finish(client, patient_id)

    with lh.conn() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE case_lifecycle SET state = 'active'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM case_lifecycle")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO case_lifecycle (clinician_id, patient_id, state, "
                "state_changed_at, last_seen_at) VALUES (?, ?, 'resumed', 'x', 'x')",
                (lh.clinician_id, patient_id),
            )
        conn.rollback()


# ---------------------------------------------------------------------------
# Lifecycle (#11-#16)
# ---------------------------------------------------------------------------


def test_start_case_creates_active_lifecycle(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))

    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.ACTIVE
    assert case.state_changed_at == case.last_seen_at == T0


def test_failed_activation_leaves_no_lifecycle_row(lh: LifecycleHarness, monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("session insert failed")

    with lh.client() as client:
        monkeypatch.setattr(case_start.sessions, "start_or_resume", boom)
        with pytest.raises(RuntimeError):
            _start(client)

    assert lh.count("SELECT COUNT(*) FROM case_lifecycle") == 0
    assert lh.count("SELECT COUNT(*) FROM arm_assignments") == 0


def test_pause_then_resume_is_audited(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        paused = _post(client, f"/case/{patient_id}/pause")
        assert paused.status_code == HTTP_SEE_OTHER
        assert lh.lifecycle(patient_id).state is CaseState.PAUSED
        assert lh.open_sessions(patient_id) == 0

        lh.clock.advance(PAUSE_GRACE - 1)
        resumed = _post(client, f"/case/{patient_id}/resume")

    assert resumed.status_code == HTTP_SEE_OTHER
    assert resumed.headers["location"] == f"{_url(patient_id)}?chrome=epic"
    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.ACTIVE and case.paused_at is None
    assert lh.open_sessions(patient_id) == 1
    assert lh.events("case.paused") == [{}]
    assert lh.events("case.resumed") == [{"paused_seconds": PAUSE_GRACE - 1}]


def test_invalid_transitions_are_refused_without_writes(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        before = lh.lifecycle(patient_id)
        refused = _post(client, f"/case/{patient_id}/resume")

    assert refused.status_code == HTTP_CONFLICT
    with lh.conn() as conn:
        with pytest.raises(CaseLifecycleError):
            lifecycle_dao.resume(conn, clinician_id=lh.clinician_id, patient_id=patient_id, now=T0)
        with pytest.raises(CaseLifecycleError):
            lifecycle_dao.mark_incomplete(
                conn,
                clinician_id=lh.clinician_id,
                patient_id=patient_id,
                reason=IncompleteReason.PAUSE_TIMEOUT,
                from_state=CaseState.PAUSED,
                now=T0,
            )
    assert lh.lifecycle(patient_id) == before


def test_completed_state_is_terminal(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _finish(client, patient_id)
        refused = _post(client, f"/case/{patient_id}/pause")

    assert refused.status_code == HTTP_CONFLICT
    with lh.conn() as conn:
        for write in (lifecycle_dao.pause, lifecycle_dao.complete, lifecycle_dao.touch):
            with pytest.raises(CaseLifecycleError):
                write(conn, clinician_id=lh.clinician_id, patient_id=patient_id, now=T0)
        with pytest.raises(CaseLifecycleError):
            lifecycle_dao.mark_incomplete(
                conn,
                clinician_id=lh.clinician_id,
                patient_id=patient_id,
                reason=IncompleteReason.OPERATOR_ABANDONED,
                from_state=CaseState.COMPLETED,
                now=T0,
            )
    assert lh.lifecycle(patient_id).state is CaseState.COMPLETED


def test_incomplete_state_is_terminal(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _abandon(lh, patient_id)
        refused = _post(client, f"/case/{patient_id}/resume")

    assert refused.status_code == HTTP_CONFLICT
    assert refused.headers["HX-Redirect"] == INDEX_URL
    with lh.conn() as conn:
        for write in (lifecycle_dao.resume, lifecycle_dao.complete, lifecycle_dao.pause):
            with pytest.raises(CaseLifecycleError):
                write(conn, clinician_id=lh.clinician_id, patient_id=patient_id, now=T0)
    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.INCOMPLETE
    assert case.incomplete_reason is IncompleteReason.OPERATOR_ABANDONED


def test_transitions_never_touch_assignment_provenance(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        before = lh.assignments()
        sessions_before = lh.count(
            "SELECT COUNT(DISTINCT config_hash || config_version || arm) FROM sessions"
        )
        _post(client, f"/case/{patient_id}/pause")
        _post(client, f"/case/{patient_id}/resume")
        lh.clock.advance(GRACE + 1)
        client.post(f"/case/{patient_id}/heartbeat")

    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE
    assert lh.assignments() == before
    assert (
        lh.count("SELECT COUNT(DISTINCT config_hash || config_version || arm) FROM sessions")
        == sessions_before
    )


# ---------------------------------------------------------------------------
# Reconnection and pause (#17-#26)
# ---------------------------------------------------------------------------


def test_heartbeat_moves_last_seen_and_records_no_event(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        events_before = lh.count("SELECT COUNT(*) FROM events")
        lh.clock.advance(HEARTBEAT_INTERVAL_SECONDS)
        beat = client.post(f"/case/{patient_id}/heartbeat")

    assert beat.status_code == HTTP_NO_CONTENT
    assert lh.lifecycle(patient_id).last_seen_at == lh.clock()
    assert lh.count("SELECT COUNT(*) FROM events") == events_before


def test_contact_inside_grace_continues_and_audits_the_gap(lh: LifecycleHarness) -> None:
    gap = RECONNECT_GAP_SECONDS + 10
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        session_before = lh.count("SELECT COUNT(*) FROM sessions")
        lh.clock.advance(gap)
        page = client.get(_url(patient_id))

    assert page.status_code == HTTP_OK
    assert 'id="case-heartbeat"' in page.text
    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE
    assert lh.lifecycle(patient_id).last_seen_at == lh.clock()
    assert lh.count("SELECT COUNT(*) FROM sessions") == session_before
    assert lh.events("case.reconnected") == [{"gap_seconds": gap}]


def test_short_gap_records_no_reconnection(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(RECONNECT_GAP_SECONDS - 1)
        client.get(_url(patient_id))

    assert lh.events("case.reconnected") == []


def test_contact_after_grace_times_the_case_out_atomically(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(GRACE + 1)
        refused = _answer(client, patient_id, **HX)

    assert refused.status_code == HTTP_CONFLICT
    assert refused.headers["HX-Redirect"] == INDEX_URL
    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.INCOMPLETE
    assert case.incomplete_reason is IncompleteReason.RECONNECTION_TIMEOUT
    assert case.incomplete_at == lh.clock()
    assert case.last_seen_at == T0  # kept as evidence
    assert lh.open_sessions(patient_id) == 0
    assert lh.count("SELECT COUNT(*) FROM answers") == 0
    assert lh.count("SELECT COUNT(*) FROM arm_assignments") == 1
    assert lh.events("case.incomplete") == [
        {
            "reason": "reconnection_timeout",
            "deadline": "2026-01-01 12:05:00",
            "grace_seconds": GRACE,
        }
    ]


def test_contact_exactly_at_the_deadline_continues(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(GRACE)
        assert client.post(f"/case/{patient_id}/heartbeat").status_code == HTTP_NO_CONTENT

    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE


def test_timed_out_get_redirects_before_any_slice(lh: LifecycleHarness, monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a timed-out case must not be sliced")

    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(GRACE + 1)
        monkeypatch.setattr(routes, "slice_to_timepoint", boom)
        page = client.get(_url(patient_id), follow_redirects=False)

    assert page.status_code == HTTP_SEE_OTHER
    assert page.headers["location"] == INDEX_URL
    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE


def test_pause_refused_when_disabled(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(
        tmp_path,
        study_fixture_dir,
        voluntary_pause_enabled=False,
        voluntary_pause_grace_seconds=None,
    )
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        page = client.get(_url(patient_id))
        refused = _post(client, f"/case/{patient_id}/pause")

    assert 'class="case-pause"' not in page.text
    assert refused.status_code == HTTP_CONFLICT
    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE
    assert lh.events("case.paused") == []


def test_pause_refused_without_lifecycle_section(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _harness(
        tmp_path, study_fixture_dir / "study_randomised.yaml", study_fixture_dir / "questions.yaml"
    )
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        refused = _post(client, f"/case/{patient_id}/pause")

    assert refused.status_code == HTTP_CONFLICT
    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE


def test_pause_button_renders_when_enabled(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        page = client.get(_url(patient_id))

    assert f'action="/case/{patient_id}/pause' in page.text


def test_resume_after_pause_grace_is_a_pause_timeout(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _post(client, f"/case/{patient_id}/pause")
        paused_at = lh.lifecycle(patient_id).paused_at
        lh.clock.advance(PAUSE_GRACE + 1)
        refused = _post(client, f"/case/{patient_id}/resume")

    assert refused.status_code == HTTP_CONFLICT
    assert refused.headers["HX-Redirect"] == INDEX_URL
    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.INCOMPLETE
    assert case.incomplete_reason is IncompleteReason.PAUSE_TIMEOUT
    assert case.paused_at == paused_at
    assert lh.events("case.resumed") == []
    assert lh.events("case.incomplete")[0]["reason"] == "pause_timeout"


def test_paused_case_shows_only_the_interstitial_and_refuses_writes(
    lh: LifecycleHarness, monkeypatch
) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a paused case must not be sliced")

    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _post(client, f"/case/{patient_id}/pause")
        monkeypatch.setattr(routes, "slice_to_timepoint", boom)

        page = client.get(_url(patient_id))
        htmx_page = client.get(_url(patient_id), headers=HX)
        answer = _answer(client, patient_id)
        advance = _advance(client, patient_id, 0)
        beat = client.post(f"/case/{patient_id}/heartbeat")

    assert page.status_code == HTTP_OK
    assert 'data-case-action="paused"' in page.text
    assert "questions-pane" not in page.text
    assert "case-heartbeat" not in page.text
    assert htmx_page.headers["HX-Redirect"] == _url(patient_id)
    assert answer.status_code == advance.status_code == beat.status_code == HTTP_CONFLICT
    assert lh.count("SELECT COUNT(*) FROM answers") == 0
    assert lh.lifecycle(patient_id).state is CaseState.PAUSED


def test_timed_out_case_refuses_advance(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        answer_all_required(client, patient_id, 0)
        lh.clock.advance(GRACE + 1)
        refused = client.post(f"{_url(patient_id)}/advance", headers=HX)

    assert refused.status_code == HTTP_CONFLICT
    assert refused.headers["HX-Redirect"] == INDEX_URL
    assert lh.count("SELECT COUNT(*) FROM events WHERE kind = 'advance.ok'") == 0


def test_case_without_lifecycle_section_never_times_out(
    tmp_path: Path, study_fixture_dir: Path
) -> None:
    lh = _harness(
        tmp_path, study_fixture_dir / "study_randomised.yaml", study_fixture_dir / "questions.yaml"
    )
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(timedelta(days=30).total_seconds())
        page = client.get(_url(patient_id))

    assert page.status_code == HTTP_OK
    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE


# ---------------------------------------------------------------------------
# Open case and Start case (#27-#29)
# ---------------------------------------------------------------------------


def test_incomplete_case_no_longer_blocks_start(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        first = _started_patient(_start(client))
        _abandon(lh, first)
        second = _started_patient(_start(client))

    assert second != first
    assert lh.lifecycle(first).state is CaseState.INCOMPLETE
    assert lh.lifecycle(second).state is CaseState.ACTIVE


def test_paused_case_blocks_start_without_resuming(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _post(client, f"/case/{patient_id}/pause")
        again = _start(client)
        index = client.get(INDEX_URL)

    assert _started_patient(again) == patient_id
    assert lh.lifecycle(patient_id).state is CaseState.PAUSED
    assert len(lh.assignments()) == 1
    assert 'data-case-action="resume_paused"' in index.text
    assert f'action="/case/{patient_id}/resume"' in index.text


def test_start_times_out_a_silent_open_case_then_activates_the_next(
    lh: LifecycleHarness,
) -> None:
    with lh.client() as client:
        first = _started_patient(_start(client))
        lh.clock.advance(GRACE + 1)
        second = _started_patient(_start(client))

    assert second != first
    assert lh.lifecycle(first).incomplete_reason is IncompleteReason.RECONNECTION_TIMEOUT
    assert lh.lifecycle(second).state is CaseState.ACTIVE
    assert [a.case_position for a in lh.assignments()] == [1, 2]


def test_repeated_start_keeps_one_lifecycle_row(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        first = _started_patient(_start(client))
        lh.clock.advance(10)
        again = _started_patient(_start(client))

    assert again == first
    assert lh.count("SELECT COUNT(*) FROM case_lifecycle") == 1
    assert lh.lifecycle(first).last_seen_at == lh.clock()


# ---------------------------------------------------------------------------
# Completion and stopping (#30-#39)
# ---------------------------------------------------------------------------


def test_final_advance_completes_the_case_atomically(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        lh.clock.advance(30)
        _finish(client, patient_id)

    case = lh.lifecycle(patient_id)
    assert case.state is CaseState.COMPLETED
    assert case.completed_at == lh.clock()
    assert len(lh.events("case.completed")) == 1


def test_failed_final_advance_leaves_the_case_active(lh: LifecycleHarness, monkeypatch) -> None:
    from ehr_simulator.web import gating

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("exit event lost")

    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _ready_final(client, patient_id)
        monkeypatch.setattr(gating.timing_events, "record_exit", boom)
        with pytest.raises(RuntimeError):
            _advance(client, patient_id, LAST_T_INDEX)

    assert lh.lifecycle(patient_id).state is CaseState.ACTIVE
    assert lh.events("case.completed") == []


def test_repeated_final_advance_does_not_duplicate_completion(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _finish(client, patient_id)
        again = _advance(client, patient_id, LAST_T_INDEX)

    assert again.status_code == HTTP_SEE_OTHER  # stale: the frontier's view
    assert len(lh.events("case.completed")) == 1


def test_final_advance_after_grace_is_incomplete_not_completed(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        _ready_final(client, patient_id)
        lh.clock.advance(GRACE + 1)
        refused = _advance(client, patient_id, LAST_T_INDEX)

    assert refused.status_code == HTTP_CONFLICT
    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE
    assert lh.events("case.completed") == []
    assert lh.count("SELECT COUNT(*) FROM progress WHERE completed_at IS NOT NULL") == 0


def test_incomplete_case_counts_as_activated(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        first = _started_patient(_start(client))
        _abandon(lh, first)
        _start(client)

    with lh.conn() as conn:
        counts = lifecycle_dao.counts_for_clinician(conn, lh.clinician_id)
    assert (counts.incomplete, counts.active, counts.activated) == (1, 1, 2)


def test_target_completed_count_blocks_start(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(tmp_path, study_fixture_dir, target_completed_cases_per_clinician=1)
    with lh.client() as client:
        _finish(client, _started_patient(_start(client)))
        before = lh.dump()
        refused = _start(client)
        index = client.get(INDEX_URL)

    assert refused.status_code == HTTP_CONFLICT
    assert lh.dump() == before
    assert 'data-case-action="limit_reached"' in index.text
    assert "No further cases can be started" in index.text
    assert "Start case" not in index.text


def test_maximum_activated_count_blocks_start(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=2,
        max_activated_cases_per_clinician=2,
    )
    with lh.client() as client:
        for _ in range(2):
            _abandon(lh, _started_patient(_start(client)))
        refused = _start(client)

    assert refused.status_code == HTTP_CONFLICT
    assert len(lh.assignments()) == 2


def test_open_case_stays_resumable_at_the_limit(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=1,
        max_activated_cases_per_clinician=1,
    )
    with lh.client() as client:
        first = _started_patient(_start(client))
        again = _started_patient(_start(client))

    assert again == first


def test_limit_is_rechecked_inside_the_activation_lock(
    tmp_path: Path, study_fixture_dir: Path
) -> None:
    lh = _with_lifecycle(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=1,
        max_activated_cases_per_clinician=1,
    )
    with lh.client() as client:
        _abandon(lh, _started_patient(_start(client)))
        schedule = lh.schedule()
        state = client.app.state
        # Phase B alone (a racing request that passed the fast path).
        with pytest.raises(case_start.ClinicianLimitReachedError):
            case_start._activate_next(
                state.db, state, clinician_id=lh.clinician_id, schedule=schedule
            )

    assert len(lh.assignments()) == 1


def test_index_markers_never_reveal_the_arm(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        first = _started_patient(_start(client))
        lh.clock.advance(GRACE + 1)
        second = _started_patient(_start(client))
        _post(client, f"/case/{second}/pause")
        index = client.get(INDEX_URL).text

    assert "incomplete" in index
    assert f'href="/patient/{first}/' not in index
    assert "paused" in index
    for marker in ARM_MARKERS:
        assert marker not in index


def test_limits_never_stop_the_whole_study(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=1,
        study_target_completed_cases=1,
    )
    second_id = lh.add_clinician(SECOND_CLINICIAN)
    with lh.client() as client:
        _finish(client, _started_patient(_start(client)))
        assert _start(client).status_code == HTTP_CONFLICT

    with lh.client(clinician_id=second_id) as client:
        started = _start(client)

    assert started.status_code == HTTP_SEE_OTHER


# ---------------------------------------------------------------------------
# CLI (#40-#43)
# ---------------------------------------------------------------------------


def _cli(lh: LifecycleHarness, *args: str):
    return CliRunner().invoke(cli.app_typer, [*args, "--db-path", str(lh.db_path)])


def test_abandon_case_marks_operator_abandoned(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))

    study = str(lh.v1.study_yaml)
    ok = _cli(lh, "abandon-case", study, "--clinician", "Dr. Test", "--patient", patient_id)
    again = _cli(lh, "abandon-case", study, "--clinician", "Dr. Test", "--patient", patient_id)
    unknown = _cli(lh, "abandon-case", study, "--clinician", "Nobody", "--patient", patient_id)

    assert ok.exit_code == 0, ok.output
    assert again.exit_code == unknown.exit_code == 1
    case = lh.lifecycle(patient_id)
    assert case.incomplete_reason is IncompleteReason.OPERATOR_ABANDONED
    assert lh.events("case.incomplete") == [
        {"reason": "operator_abandoned", "deadline": None, "grace_seconds": None}
    ]


def test_case_status_is_read_only_and_pseudonymous(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        _finish(client, _started_patient(_start(client)))
        _start(client)

    before = lh.dump()
    result = _cli(lh, "case-status", str(lh.v1.study_yaml))

    assert result.exit_code == 0, result.output
    assert lh.clinician_id in result.stdout
    assert "dr. test" not in result.stdout.lower()
    row = next(line for line in result.stdout.splitlines() if line.startswith(lh.clinician_id))
    # active paused completed incomplete activated completed_left activated_left
    assert row.split()[1:] == ["1", "0", "1", "0", "2", "1", "1"]
    assert lh.dump() == before


def test_reset_progress_respects_terminal_cases(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        done = _started_patient(_start(client))
        _finish(client, done)
        open_case = _started_patient(_start(client))
        seed_progress(client, open_case, 1)

    study = str(lh.v1.study_yaml)
    refused = _cli(lh, "reset-progress", study, "--clinician", "Dr. Test", "--patient", done)
    allowed = _cli(lh, "reset-progress", study, "--clinician", "Dr. Test", "--patient", open_case)

    assert refused.exit_code == 1
    assert "closed case cannot be reset" in refused.stderr
    assert allowed.exit_code == 0, allowed.output
    assert lh.lifecycle(done).state is CaseState.COMPLETED
    assert lh.lifecycle(open_case).state is CaseState.ACTIVE


def test_new_commands_reach_the_os_exit_status(lh: LifecycleHarness) -> None:
    import subprocess
    import sys

    exe = Path(sys.executable).parent / "ehr-simulator"
    study = str(lh.v1.study_yaml)
    status = subprocess.run(
        [str(exe), "case-status", study, "--db-path", str(lh.db_path)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    refused = subprocess.run(
        [
            str(exe),
            "abandon-case",
            study,
            "--clinician",
            "Dr. Test",
            "--patient",
            "synth_001",
            "--db-path",
            str(lh.db_path),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert status.returncode == 0, status.stderr
    assert refused.returncode == 1, refused.stderr
    assert "Traceback (most recent call last)" not in refused.stderr


# ---------------------------------------------------------------------------
# Export (#44)
# ---------------------------------------------------------------------------


def test_incomplete_cases_export_unless_only_complete(lh: LifecycleHarness) -> None:
    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        answer_all_required(client, patient_id, 0)
        lh.clock.advance(GRACE + 1)
        client.post(f"/case/{patient_id}/heartbeat")
    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE

    def export(*flags: str) -> list[dict[str, str]]:
        out = lh.tmp_path / f"export{len(flags)}.csv"
        result = _cli(
            lh,
            "export-answers",
            str(lh.v1.study_yaml),
            str(lh.v1.questions_yaml),
            "--out",
            str(out),
            *flags,
        )
        assert result.exit_code == 0, result.output
        with out.open(newline="") as fh:
            return list(csv.DictReader(fh))

    assert [r["patient_id"] for r in export() if r["deterioration_6h"]] == [patient_id]
    assert export("--only-complete") == []


# ---------------------------------------------------------------------------
# Regression (#45-#46)
# ---------------------------------------------------------------------------


def test_historical_case_keeps_its_pinned_lifecycle_policy(lh: LifecycleHarness) -> None:
    lenient = {**VALID_LIFECYCLE, "reconnection_grace_seconds": 10 * GRACE}
    v2 = lh.variant("v2", case_lifecycle=lenient)
    with lh.client() as client:
        patient_id = _started_patient(_start(client))

    lh.activate(v2)
    with lh.client(v2) as client:
        lh.clock.advance(GRACE + 1)
        page = client.get(_url(patient_id), follow_redirects=False)

    assert page.status_code == HTTP_SEE_OTHER
    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE


def test_lost_lifecycle_race_is_a_conflict_not_a_crash(lh: LifecycleHarness, monkeypatch) -> None:
    from ehr_simulator.web import case_contact

    def lost_race(*_args: Any, **_kwargs: Any) -> None:
        raise CaseLifecycleError("case expected state active, found paused")

    with lh.client() as client:
        patient_id = _started_patient(_start(client))
        monkeypatch.setattr(case_contact, "touch", lost_race)
        beat = client.post(f"/case/{patient_id}/heartbeat")

    assert beat.status_code == HTTP_CONFLICT
