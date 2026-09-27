"""S11i: study behaviour policies — backward navigation, feedback, practice
cases and free text, all pinned per case.

Backward navigation runs Phase 1 study mode (``study_synthetic.yaml``);
practice runs Phase 2 (``study_randomised.yaml`` with synth_003 moved from
the measured pool to practice).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ehr_simulator.cli_support import walk_preflight
from ehr_simulator.config import (
    ConfigError,
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
    parse_study_snapshot,
    render_study_snapshot,
    validate_study_questions,
)
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db import connect, practice, progress
from ehr_simulator.db.exceptions import ConfigurationActivationError, ConfigurationProvenanceError
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.divergence import build_divergence_figure
from ehr_simulator.export import ExportOptions, build_export
from ehr_simulator.ingestion import load_synthetic
from ehr_simulator.web.app import app_from_study_config
from tests.conftest import _activate_configuration, _seed_clinician, answer_all_required

REPO = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "study"
FUC_QUESTIONS = REPO / "configs" / "example_phase2_questions.yaml"
V1_QUESTIONS = FIXTURES / "questions.yaml"
PID = "synth_001"
PRACTICE_PID = "synth_003"
HX = {"HX-Request": "true"}
HTTP_OK = 200
HTTP_SEE_OTHER = 303
HTTP_CONFLICT = 409
HTTP_PRECONDITION_FAILED = 412
LAST_T_INDEX = 2
BEHAVIOUR = {"backward_navigation": "allow_readonly"}
PRACTICE = {"enabled": True, "patient_ids": [PRACTICE_PID], "arm": "no_ai"}
FEEDBACK_MARKERS = ("correct", "score", "ground truth", "Ground truth")


def _yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((FIXTURES / name).read_text())


def _write(tmp_path: Path, name: str, data: dict[str, Any]) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data))
    return path


def _phase1(**behaviour: Any) -> dict[str, Any]:
    data = _yaml("study_synthetic.yaml")
    data["study_behaviour"] = {**BEHAVIOUR, **behaviour}
    return data


def _phase2(**behaviour: Any) -> dict[str, Any]:
    data = _yaml("study_randomised.yaml")
    data["patient_ids"] = ["synth_001", "synth_002"]
    data["study_behaviour"] = {**BEHAVIOUR, "practice": PRACTICE, **behaviour}
    return data


class Study:
    """One study DB + the configs activated on it."""

    def __init__(self, tmp_path: Path, study: dict[str, Any], questions: Path) -> None:
        self.tmp_path = tmp_path
        self.db_path = tmp_path / "study.db"
        self.study_yaml = _write(tmp_path, "study_v1.yaml", study)
        self.questions = questions
        self.clinician_id = _seed_clinician(self.db_path, study_id=study["study_id"])
        self.activate(self.study_yaml, "v1")

    def activate(self, study_yaml: Path, version: str) -> None:
        _activate_configuration(
            self.db_path, study_yaml, self.questions, version=version, description=version
        )

    @contextmanager
    def client(self, study_yaml: Path | None = None) -> Iterator[TestClient]:
        app = app_from_study_config(
            study_yaml or self.study_yaml,
            self.questions,
            log_dir=self.tmp_path / "logs",
            db_path=self.db_path,
            backup_dir=self.tmp_path / "backups",
        )
        with TestClient(app) as client:
            client.cookies.set("ehrsim_clinician_id", self.clinician_id)
            yield client

    def events(self, kind: str) -> list[tuple[str, dict]]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT patient_id, payload_json FROM events WHERE kind = ? ORDER BY event_id",
                (kind,),
            ).fetchall()
        finally:
            conn.close()
        return [(r[0], json.loads(r[1])) for r in rows]


def _page(client: TestClient, t_index: int, pid: str = PID, **kwargs: Any) -> Any:
    return client.get(f"/patient/{pid}/timepoint/{t_index}", follow_redirects=False, **kwargs)


def _advance(client: TestClient, t_index: int, pid: str = PID) -> None:
    answer_all_required(client, pid, t_index)
    response = client.post(f"/patient/{pid}/timepoint/{t_index}/advance", headers=HX)
    assert response.status_code == HTTP_OK, response.text


def _visit_kind(html: str) -> str:
    return BeautifulSoup(html, "html.parser").select_one("#patient-view")["data-visit-kind"]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_absent_block_keeps_historical_snapshot_bytes() -> None:
    study = StudyConfig.model_validate(_yaml("study_synthetic.yaml"))
    rendered = render_study_snapshot(study)
    assert "study_behaviour" not in rendered
    assert parse_study_snapshot(rendered) == study


def test_block_without_backward_navigation_rejected() -> None:
    data = _phase1()
    del data["study_behaviour"]["backward_navigation"]
    with pytest.raises(ValidationError, match="backward_navigation"):
        StudyConfig.model_validate(data)


@pytest.mark.parametrize(
    "flag", ["show_ground_truth", "show_correctness", "show_running_score", "show_ai_correctness"]
)
def test_feedback_flag_true_rejected(flag: str) -> None:
    with pytest.raises(ValidationError, match="not implemented"):
        StudyConfig.model_validate(_phase1(feedback={flag: True}))


def test_practice_measured_overlap_rejected() -> None:
    data = _phase2(practice={**PRACTICE, "patient_ids": ["synth_001"]})
    with pytest.raises(ValidationError, match="overlap"):
        StudyConfig.model_validate(data)


def test_practice_requires_phase2_and_ai_intervention() -> None:
    with pytest.raises(ValidationError, match="randomisation"):
        StudyConfig.model_validate(_phase1(practice=PRACTICE))

    data = _phase2(practice={**PRACTICE, "arm": "ai"})
    del data["ai_intervention"]
    with pytest.raises(ValidationError, match="ai_intervention"):
        StudyConfig.model_validate(data)


@pytest.mark.parametrize(
    "change",
    [
        {"backward_navigation": "prohibit"},
        {"free_text": {"enabled": True}},
        {"practice": {**PRACTICE, "arm": "ai"}},
    ],
    ids=["backward", "free_text", "practice"],
)
def test_policy_change_changes_config_hash(change: dict[str, Any]) -> None:
    questions = load_questions(FUC_QUESTIONS)
    base = StudyConfig.model_validate(_phase2())
    changed = StudyConfig.model_validate(_phase2(**change))
    assert compute_config_hash_from_models(base, questions) != compute_config_hash_from_models(
        changed, questions
    )


# ---------------------------------------------------------------------------
# Backward navigation
# ---------------------------------------------------------------------------


@pytest.fixture
def readonly(tmp_path: Path) -> Study:
    return Study(tmp_path, _phase1(), FUC_QUESTIONS)


@pytest.fixture
def prohibit(tmp_path: Path) -> Study:
    return Study(tmp_path, _phase1(backward_navigation="prohibit"), FUC_QUESTIONS)


def test_allow_readonly_renders_prior_timepoint_frozen(readonly: Study) -> None:
    with readonly.client() as client:
        _page(client, 0)
        _advance(client, 0)
        response = _page(client, 0)

    assert response.status_code == HTTP_OK
    assert _visit_kind(response.text) == "revisit"
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one("#questions-pane")["data-mode"] == "locked"
    assert all(f.has_attr("disabled") for f in soup.select("#questions-pane fieldset"))


def test_revisit_refuses_answer_and_advance(readonly: Study) -> None:
    with readonly.client() as client:
        _page(client, 0)
        _advance(client, 0)
        answer = client.post(
            f"/patient/{PID}/timepoint/0/answer",
            data={"question_id": "confidence", "value": "2"},
            headers=HX,
        )
        advance = client.post(f"/patient/{PID}/timepoint/0/advance", headers=HX)

    assert answer.status_code == HTTP_CONFLICT
    assert advance.status_code == HTTP_PRECONDITION_FAILED


def test_revisit_writes_marker_not_timing(readonly: Study) -> None:
    with readonly.client() as client:
        _page(client, 0)
        _advance(client, 0)
        enters, exits = (
            len(readonly.events("timepoint.enter")),
            len(readonly.events("timepoint.exit")),
        )
        _page(client, 0)

    assert readonly.events("timepoint.revisit") == [(PID, {"t_index": 0})]
    assert len(readonly.events("timepoint.enter")) == enters
    assert len(readonly.events("timepoint.exit")) == exits


def test_frontier_is_primary(readonly: Study) -> None:
    with readonly.client() as client:
        assert _visit_kind(_page(client, 0).text) == "primary"
    assert readonly.events("timepoint.revisit") == []


def test_prohibit_redirects_backward_get_before_slicing(
    prohibit: Study, monkeypatch: pytest.MonkeyPatch
) -> None:
    with prohibit.client() as client:
        _page(client, 0)
        _advance(client, 0)
        sessions_before = client.app.state.db.execute("SELECT COUNT(*) FROM sessions").fetchone()

        def no_slice(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("sliced a prohibited timepoint")

        monkeypatch.setattr("ehr_simulator.web.routes.slice_to_timepoint", no_slice)
        response = _page(client, 0)
        sessions_after = client.app.state.db.execute("SELECT COUNT(*) FROM sessions").fetchone()

    assert response.status_code == HTTP_SEE_OTHER
    assert response.headers["location"].startswith(f"/patient/{PID}/timepoint/1")
    assert prohibit.events("timepoint.revisit") == []
    assert sessions_before == sessions_after


def test_prohibit_renders_no_prev_button(prohibit: Study) -> None:
    with prohibit.client() as client:
        _page(client, 0)
        _advance(client, 0)
        html = _page(client, 1).text
    assert "tp-prev" not in html


def test_prohibit_keeps_completed_case_last_timepoint(prohibit: Study) -> None:
    with prohibit.client() as client:
        _page(client, 0)
        for t_index in range(LAST_T_INDEX + 1):
            _advance(client, t_index)
        last = _page(client, LAST_T_INDEX)
        earlier = _page(client, 0)

    assert last.status_code == HTTP_OK
    assert _visit_kind(last.text) == "revisit"
    assert earlier.status_code == HTTP_SEE_OTHER


def test_old_case_keeps_backward_policy(tmp_path: Path) -> None:
    study = Study(tmp_path, _phase1(backward_navigation="prohibit"), FUC_QUESTIONS)
    with study.client() as client:
        _page(client, 0)
        _advance(client, 0)  # synth_001 pinned to v1 (prohibit)

    v2 = _write(tmp_path, "study_v2.yaml", _phase1())
    study.activate(v2, "v2")
    with study.client(v2) as client:
        assert _page(client, 0).status_code == HTTP_SEE_OTHER
        _page(client, 0, pid="synth_002")
        _advance(client, 0, pid="synth_002")
        assert _page(client, 0, pid="synth_002").status_code == HTTP_OK


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------


def test_measured_pages_render_no_feedback(readonly: Study) -> None:
    with readonly.client() as client:
        _page(client, 0)
        _advance(client, 0)
        pages = [_page(client, 0).text, _page(client, 1).text, client.get("/").text]

    for html in pages:
        for marker in FEEDBACK_MARKERS:
            assert marker not in html, marker


# ---------------------------------------------------------------------------
# Practice
# ---------------------------------------------------------------------------


@pytest.fixture
def phase2(tmp_path: Path) -> Study:
    return Study(tmp_path, _phase2(), FUC_QUESTIONS)


def _start_practice(client: TestClient) -> Any:
    return client.post("/practice/start", follow_redirects=False)


def _count(client: TestClient, sql: str) -> int:
    return client.app.state.db.execute(sql).fetchone()[0]


def test_practice_disabled_shows_nothing_and_refuses(tmp_path: Path) -> None:
    data = _phase2(practice={"enabled": False})
    study = Study(tmp_path, data, FUC_QUESTIONS)
    with study.client() as client:
        assert "practice-action" not in client.get("/").text
        assert _start_practice(client).status_code == HTTP_CONFLICT


def test_practice_start_picks_configured_patient_and_resumes(phase2: Study) -> None:
    with phase2.client() as client:
        assert 'data-practice-action="start"' in client.get("/").text
        first = _start_practice(client)
        again = _start_practice(client)
        index = client.get("/").text

    assert first.status_code == HTTP_SEE_OTHER
    assert first.headers["location"].startswith(f"/patient/{PRACTICE_PID}/timepoint/0")
    assert again.headers["location"] == first.headers["location"]
    assert 'data-practice-action="resume"' in index
    assert 'data-observation-mode="practice"' in index


def test_practice_start_writes_no_allocation_rows(phase2: Study) -> None:
    with phase2.client() as client:
        _start_practice(client)
        for table in (
            "arm_assignments",
            "randomisation_schedules",
            "case_lifecycle",
            "case_replacements",
        ):
            assert _count(client, f"SELECT COUNT(*) FROM {table}") == 0, table
        assert _count(client, "SELECT COUNT(*) FROM practice_cases") == 1


def _walk_practice(client: TestClient) -> None:
    _start_practice(client)
    _page(client, 0, pid=PRACTICE_PID)
    for t_index in range(LAST_T_INDEX + 1):
        _advance(client, t_index, pid=PRACTICE_PID)


def test_practice_rows_are_marked_practice(phase2: Study) -> None:
    with phase2.client() as client:
        _walk_practice(client)
        modes = {
            table: {
                r[0] for r in client.app.state.db.execute(f"SELECT observation_mode FROM {table}")
            }
            for table in ("answers", "progress", "sessions")
        }

    assert modes == {table: {"practice"} for table in modes}


def test_practice_completion_leaves_measured_counts(phase2: Study) -> None:
    with phase2.client() as client:
        _walk_practice(client)
        index = client.get("/").text
        started = client.post("/case/start", follow_redirects=False)

    conn = connect(phase2.db_path)
    try:
        case = practice.fetch(conn, phase2.clinician_id, PRACTICE_PID)
        lifecycle = {r[0] for r in conn.execute("SELECT patient_id FROM case_lifecycle")}
    finally:
        conn.close()
    assert case is not None and case.completed_at is not None
    assert 'data-practice-action="exhausted"' in index
    assert [p for p, _ in phase2.events("practice.completed")] == [PRACTICE_PID]
    # The measured Start case still offers the first measured case.
    assert started.status_code == HTTP_SEE_OTHER
    assert PRACTICE_PID not in started.headers["location"]
    assert lifecycle == {started.headers["location"].split("/")[2]}


def test_measured_start_works_alongside_open_practice(phase2: Study) -> None:
    with phase2.client() as client:
        _start_practice(client)
        started = client.post("/case/start", follow_redirects=False)
        arms = client.app.state.db.execute(
            "SELECT patient_id FROM arm_assignments WHERE arm_source = 'phase2_randomized'"
        ).fetchall()

    assert started.status_code == HTTP_SEE_OTHER
    assert [r[0] for r in arms] != [PRACTICE_PID]
    assert PRACTICE_PID not in {r[0] for r in arms}


def test_mode_changing_write_refused(phase2: Study) -> None:
    with phase2.client() as client:
        _walk_practice(client)
        db = client.app.state.db
        row = progress.fetch(db, clinician_id=phase2.clinician_id, patient_id=PRACTICE_PID)
        with pytest.raises(ConfigurationProvenanceError, match="practice"):
            progress.mark_complete(
                db,
                clinician_id=phase2.clinician_id,
                patient_id=PRACTICE_PID,
                unlocked_t_index=LAST_T_INDEX,
                config_hash=row.config_hash,
                config_version=row.config_version,
                observation_mode=ObservationMode.MEASURED,
            )


def test_practice_case_is_immutable(phase2: Study) -> None:
    with phase2.client() as client:
        _start_practice(client)
        db = client.app.state.db
        with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
            db.execute("DELETE FROM practice_cases")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE practice_cases SET arm = 'ai'")
        db.rollback()


def test_activation_refuses_cross_version_overlap(phase2: Study, tmp_path: Path) -> None:
    data = _phase2(practice={"enabled": False})
    data["patient_ids"] = ["synth_001", "synth_002", PRACTICE_PID]
    v2 = _write(tmp_path, "study_v2.yaml", data)
    with pytest.raises(ConfigurationActivationError, match="practice patients"):
        phase2.activate(v2, "v2")


def test_export_and_divergence_exclude_practice(phase2: Study) -> None:
    with phase2.client() as client:
        _walk_practice(client)
        live_hash = client.app.state.config_hash

    conn = connect(phase2.db_path)
    try:
        study = load_study_config(phase2.study_yaml)
        questions = load_questions(FUC_QUESTIONS)
        bundle = build_export(
            conn,
            study=study,
            questions=questions,
            live_hash=live_hash,
            options=ExportOptions(),
        )
        assert bundle.frame.rows == ()
        # synth_001 has no measured answers; practice rows never feed it.
        build_divergence_figure(
            conn,
            study=study,
            questions=questions,
            live_hash=live_hash,
            patient_id=PID,
            dataset=load_synthetic(),
        )
    finally:
        conn.close()


def test_practice_case_renders_by_fixed_arm(phase2: Study) -> None:
    with phase2.client() as client:
        _start_practice(client)
        html = _page(client, 0, pid=PRACTICE_PID).text
    assert 'data-panel="ai"' not in html  # practice arm no_ai
    assert "Practice case" in html
    assert 'data-observation-mode="practice"' in html


def test_practice_ai_arm_renders_ai(tmp_path: Path) -> None:
    study = Study(tmp_path, _phase2(practice={**PRACTICE, "arm": "ai"}), FUC_QUESTIONS)
    with study.client() as client:
        _start_practice(client)
        html = _page(client, 0, pid=PRACTICE_PID).text
    assert 'data-intervention="ai"' in html


# ---------------------------------------------------------------------------
# Free text
# ---------------------------------------------------------------------------


def test_free_text_question_rejected_when_disabled(tmp_path: Path) -> None:
    study = StudyConfig.model_validate(_phase1())
    questions = load_questions(V1_QUESTIONS)  # has free_notes
    with pytest.raises(ConfigError, match="free_text.enabled is false"):
        validate_study_questions(study, questions)

    report = walk_preflight(study, questions, load_synthetic())
    assert any("free_text.enabled is false" in r.message for r in report.rows if r.status == "FAIL")

    path = _write(tmp_path, "study.yaml", _phase1())
    with pytest.raises(ConfigError, match="free_text.enabled is false"):
        app_from_study_config(path, V1_QUESTIONS, log_dir=tmp_path / "logs")


def test_activate_config_cli_refuses_disabled_free_text(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ehr_simulator.cli import app_typer

    path = _write(tmp_path, "study.yaml", _phase1())
    result = CliRunner().invoke(
        app_typer,
        [
            "activate-config",
            str(path),
            str(V1_QUESTIONS),
            "--version",
            "v1",
            "--description",
            "d",
            "--db-path",
            str(tmp_path / "db.sqlite"),
        ],
    )
    assert result.exit_code == 1
    assert not (tmp_path / "db.sqlite").exists()


@pytest.fixture
def free_text(tmp_path: Path) -> Study:
    return Study(tmp_path, _phase1(free_text={"enabled": True}), V1_QUESTIONS)


def test_free_text_enabled_saves_without_event_value(free_text: Study) -> None:
    secret = "patient looked drowsy"
    with free_text.client() as client:
        _page(client, 0)
        response = client.post(
            f"/patient/{PID}/timepoint/0/answer",
            data={"question_id": "free_notes", "value": secret},
            headers=HX,
        )
    assert response.status_code == HTTP_OK
    assert secret not in json.dumps(free_text.events("answer.upsert"))


@pytest.mark.parametrize(
    ("routine_export", "included"), [("exclude", False), ("include_explicit", True)]
)
def test_export_free_text_columns_follow_policy(
    tmp_path: Path, routine_export: str, included: bool
) -> None:
    data = _phase1(free_text={"enabled": True, "routine_export": routine_export})
    study_cfg = Study(tmp_path, data, V1_QUESTIONS)
    with study_cfg.client() as client:
        _page(client, 0)
        client.post(
            f"/patient/{PID}/timepoint/0/answer",
            data={"question_id": "free_notes", "value": "note"},
            headers=HX,
        )
        live_hash = client.app.state.config_hash

    conn = connect(study_cfg.db_path)
    try:
        bundle = build_export(
            conn,
            study=load_study_config(study_cfg.study_yaml),
            questions=load_questions(V1_QUESTIONS),
            live_hash=live_hash,
            options=ExportOptions(),
        )
        stored = conn.execute("SELECT COUNT(*) FROM answers WHERE question_id = 'free_notes'")
        assert stored.fetchone()[0] == 1
    finally:
        conn.close()
    assert ("free_notes" in bundle.frame.header) is included


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def test_example_config_keeps_practice_and_feedback_disabled() -> None:
    study = load_study_config(REPO / "configs" / "example_phase2_config.yaml")
    behaviour = study.study_behaviour
    assert behaviour is not None
    assert not behaviour.practice.enabled
    assert not any(value for _name, value in behaviour.feedback)
    assert not behaviour.free_text.enabled


def test_open_practice_case_resumes_after_practice_disabled(phase2: Study, tmp_path: Path) -> None:
    """An open practice case stays resumable when a newer version drops practice."""
    with phase2.client() as client:
        first = _start_practice(client)
    assert first.status_code == HTTP_SEE_OTHER

    v2 = _write(tmp_path, "study_v2.yaml", _phase2(practice={"enabled": False}))
    phase2.activate(v2, "v2")
    with phase2.client(v2) as client:
        index = client.get("/").text
        resumed = _start_practice(client)
        page = _page(client, 0, pid=PRACTICE_PID)

    assert 'data-practice-action="resume"' in index
    assert resumed.status_code == HTTP_SEE_OTHER
    assert resumed.headers["location"] == first.headers["location"]
    assert page.status_code == HTTP_OK


def test_practice_disabled_without_open_case_refuses(phase2: Study, tmp_path: Path) -> None:
    v2 = _write(tmp_path, "study_v2.yaml", _phase2(practice={"enabled": False}))
    phase2.activate(v2, "v2")
    with phase2.client(v2) as client:
        assert "practice-action" not in client.get("/").text
        assert _start_practice(client).status_code == HTTP_CONFLICT
