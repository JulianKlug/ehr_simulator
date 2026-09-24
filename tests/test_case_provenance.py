"""S11b: multi-version case provenance, end to end.

Each test drives the real app across configuration activations on one
database file:

    activate v1 ─► boot v1 ─► open cases ─► stop
    activate v2 ─► boot v2 ─► old cases stay on v1, new cases pin v2

v1: patients synth_001..003, timepoints [0, 60, 180], all fixture questions.
v2: patient synth_001 only, timepoints [0, 120], ``confidence`` dropped.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from ehr_simulator.config import compute_config_hash_from_models, load_questions, load_study_config
from ehr_simulator.db import ConfigurationProvenanceError, config_history, connect, progress
from ehr_simulator.web.app import app_from_study_config
from tests.conftest import _activate_configuration, _seed_clinician

KEPT_PID = "synth_001"
REMOVED_PID = "synth_002"
UNOPENED_PID = "synth_003"
DROPPED_QUESTION = "confidence"
V2_PATIENT_IDS = [KEPT_PID]
V2_TIMEPOINTS = [0, 120]
HTTP_OK = 200
HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_INTEGRITY_ERROR = 500


@dataclass(frozen=True)
class Config:
    version: str
    study_yaml: Path
    questions_yaml: Path

    @property
    def config_hash(self) -> str:
        return compute_config_hash_from_models(
            load_study_config(self.study_yaml), load_questions(self.questions_yaml)
        )


@dataclass(frozen=True)
class Harness:
    tmp_path: Path
    db_path: Path
    clinician_id: str
    v1: Config
    v2: Config

    def activate(self, config: Config) -> None:
        _activate_configuration(
            self.db_path,
            config.study_yaml,
            config.questions_yaml,
            version=config.version,
            description=f"activate {config.version}",
        )

    @contextmanager
    def boot(self, config: Config) -> Iterator[TestClient]:
        app = app_from_study_config(
            config.study_yaml,
            config.questions_yaml,
            log_dir=self.tmp_path / "logs",
            db_path=self.db_path,
            backup_dir=self.tmp_path / "backups",
        )
        with TestClient(app) as client:
            client.cookies.set("ehrsim_clinician_id", self.clinician_id)
            yield client

    def provenance(self, table: str, patient_id: str) -> tuple[str, str]:
        conn = connect(self.db_path)
        try:
            row = conn.execute(
                f"SELECT config_version, config_hash FROM {table} "
                "WHERE clinician_id = ? AND patient_id = ?",
                (self.clinician_id, patient_id),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, (table, patient_id)
        return (row[0], row[1])

    def execute(self, sql: str, params: tuple = ()) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def count(self, table: str, patient_id: str) -> int:
        conn = connect(self.db_path)
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE patient_id = ?", (patient_id,)
            ).fetchone()[0]
        finally:
            conn.close()


def _write_v2(tmp_path: Path, study_fixture_dir: Path) -> tuple[Path, Path]:
    study = yaml.safe_load((study_fixture_dir / "study_synthetic.yaml").read_text())
    study["patient_ids"] = V2_PATIENT_IDS
    study["timepoints"] = V2_TIMEPOINTS
    study_path = tmp_path / "study_v2.yaml"
    study_path.write_text(yaml.safe_dump(study))

    questions = yaml.safe_load((study_fixture_dir / "questions.yaml").read_text())
    questions["questions"] = [
        q for q in questions["questions"] if q["question_id"] != DROPPED_QUESTION
    ]
    questions_path = tmp_path / "questions_v2.yaml"
    questions_path.write_text(yaml.safe_dump(questions))
    return study_path, questions_path


@pytest.fixture
def harness(tmp_path: Path, study_fixture_dir: Path) -> Harness:
    v1 = Config(
        "v1", study_fixture_dir / "study_synthetic.yaml", study_fixture_dir / "questions.yaml"
    )
    v2 = Config("v2", *_write_v2(tmp_path, study_fixture_dir))
    db_path = tmp_path / "study.db"
    clinician_id = _seed_clinician(db_path, study_id=load_study_config(v1.study_yaml).study_id)
    h = Harness(tmp_path, db_path, clinician_id, v1, v2)
    h.activate(v1)
    return h


@pytest.fixture
def upgraded(harness: Harness) -> Harness:
    """v1 case opened on REMOVED_PID, then v2 activated (server stopped)."""
    with harness.boot(harness.v1) as client:
        assert client.get(f"/patient/{REMOVED_PID}/timepoint/0").status_code == HTTP_OK
    harness.activate(harness.v2)
    return harness


def _answer(client: TestClient, patient_id: str, question_id: str, value: str):
    return client.post(
        f"/patient/{patient_id}/timepoint/0/answer",
        data={"question_id": question_id, "value": value},
    )


# ---------------------------------------------------------------------------
# #13-#15 new vs existing cases across an activation
# ---------------------------------------------------------------------------


def test_new_case_receives_active_version(harness: Harness) -> None:
    with harness.boot(harness.v1) as client:
        assert client.get(f"/patient/{KEPT_PID}/timepoint/0").status_code == HTTP_OK

    expected = ("v1", harness.v1.config_hash)
    for table in ("arm_assignments", "sessions"):
        assert harness.provenance(table, KEPT_PID) == expected


def test_existing_case_keeps_version_after_later_activation(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        assert client.get(f"/patient/{REMOVED_PID}/timepoint/0").status_code == HTTP_OK

    expected = ("v1", upgraded.v1.config_hash)
    for table in ("arm_assignments", "sessions"):
        assert upgraded.provenance(table, REMOVED_PID) == expected


def test_new_case_after_activation_receives_new_version(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        assert client.get(f"/patient/{KEPT_PID}/timepoint/0").status_code == HTTP_OK

    assert upgraded.provenance("arm_assignments", KEPT_PID) == ("v2", upgraded.v2.config_hash)


# ---------------------------------------------------------------------------
# #16 historical questions and timepoints
# ---------------------------------------------------------------------------


def test_historical_case_uses_its_questions(upgraded: Harness) -> None:
    question_marker = f'data-question-id="{DROPPED_QUESTION}"'
    with upgraded.boot(upgraded.v2) as client:
        old_case = client.get(f"/patient/{REMOVED_PID}/timepoint/0")
        new_case = client.get(f"/patient/{KEPT_PID}/timepoint/0")
        old_answer = _answer(client, REMOVED_PID, DROPPED_QUESTION, "3")
        new_answer = _answer(client, KEPT_PID, DROPPED_QUESTION, "3")

    assert question_marker in old_case.text
    assert question_marker not in new_case.text
    assert old_answer.status_code == HTTP_OK
    assert new_answer.status_code == HTTP_UNPROCESSABLE
    assert upgraded.provenance("answers", REMOVED_PID) == ("v1", upgraded.v1.config_hash)


def test_historical_case_uses_its_timepoints(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        old_case = client.get(f"/patient/{REMOVED_PID}/timepoint/99")
        new_case = client.get(f"/patient/{KEPT_PID}/timepoint/99")

    assert old_case.status_code == new_case.status_code == HTTP_NOT_FOUND
    assert "valid: 0…2" in old_case.text  # v1: three timepoints
    assert "valid: 0…1" in new_case.text  # v2: two timepoints


# ---------------------------------------------------------------------------
# #17-#20 provenance integrity on sessions, progress and answers
# ---------------------------------------------------------------------------


def test_session_provenance_mismatch_is_refused(upgraded: Harness) -> None:
    upgraded.execute(
        "UPDATE sessions SET config_version = 'v2' WHERE patient_id = ?", (REMOVED_PID,)
    )

    with upgraded.boot(upgraded.v2) as client:
        r = client.get(f"/patient/{REMOVED_PID}/timepoint/0")

    assert r.status_code == HTTP_INTEGRITY_ERROR
    assert upgraded.count("sessions", REMOVED_PID) == 1


def test_progress_provenance_mismatch_is_refused(upgraded: Harness) -> None:
    upgraded.execute(
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash, "
        "config_version) VALUES (?, ?, 0, ?, 'v2')",
        (upgraded.clinician_id, REMOVED_PID, upgraded.v2.config_hash),
    )

    with upgraded.boot(upgraded.v2) as client:
        r = client.get(f"/patient/{REMOVED_PID}/timepoint/0")

    assert r.status_code == HTTP_INTEGRITY_ERROR


def test_progress_writes_cannot_change_provenance(upgraded: Harness) -> None:
    v1_hash = upgraded.v1.config_hash
    conn = connect(upgraded.db_path)
    try:
        pair = {"clinician_id": upgraded.clinician_id, "patient_id": REMOVED_PID}
        assert progress.unlock(
            conn, **pair, from_t_index=0, to_t_index=1, config_hash=v1_hash, config_version="v1"
        )
        foreign = {"config_hash": upgraded.v2.config_hash, "config_version": "v2"}

        with pytest.raises(ConfigurationProvenanceError, match="provenance"):
            progress.unlock(conn, **pair, from_t_index=1, to_t_index=2, **foreign)
        with pytest.raises(ConfigurationProvenanceError, match="provenance"):
            progress.mark_complete(conn, **pair, unlocked_t_index=1, **foreign)

        row = progress.fetch(conn, **pair)
    finally:
        conn.close()

    assert row is not None
    assert (row.unlocked_t_index, row.completed_at) == (1, None)
    assert (row.config_version, row.config_hash) == ("v1", v1_hash)


def test_answer_upsert_on_historical_case_keeps_provenance(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        assert _answer(client, REMOVED_PID, "deterioration_6h", "Yes").status_code == HTTP_OK
        assert _answer(client, REMOVED_PID, "deterioration_6h", "No").status_code == HTTP_OK

    assert upgraded.provenance("answers", REMOVED_PID) == ("v1", upgraded.v1.config_hash)


def test_answer_provenance_mismatch_refuses_write(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        assert _answer(client, REMOVED_PID, "deterioration_6h", "Yes").status_code == HTTP_OK
    upgraded.execute(
        "UPDATE answers SET config_version = 'v2' WHERE patient_id = ?", (REMOVED_PID,)
    )

    with upgraded.boot(upgraded.v2) as client:
        update = _answer(client, REMOVED_PID, "deterioration_6h", "No")
        clear = _answer(client, REMOVED_PID, "deterioration_6h", "")

    assert update.status_code == clear.status_code == HTTP_INTEGRITY_ERROR
    conn = connect(upgraded.db_path)
    try:
        value = conn.execute(
            "SELECT value FROM answers WHERE patient_id = ?", (REMOVED_PID,)
        ).fetchone()
    finally:
        conn.close()
    assert value is not None and value[0] == "Yes"


# ---------------------------------------------------------------------------
# #21 removed but assigned patient stays reachable
# ---------------------------------------------------------------------------


def test_removed_assigned_patient_remains_accessible(upgraded: Harness) -> None:
    with upgraded.boot(upgraded.v2) as client:
        index = client.get("/")
        case = client.get(f"/patient/{REMOVED_PID}/timepoint/0")
        unassigned = client.get(f"/patient/{UNOPENED_PID}/timepoint/0")

    assert REMOVED_PID in index.text
    assert UNOPENED_PID not in index.text
    assert case.status_code == HTTP_OK
    assert unassigned.status_code == HTTP_NOT_FOUND


# ---------------------------------------------------------------------------
# #26 stale server
# ---------------------------------------------------------------------------


def test_stale_server_refuses_new_case_after_external_activation(harness: Harness) -> None:
    with harness.boot(harness.v1) as client:
        assert client.get(f"/patient/{REMOVED_PID}/timepoint/0").status_code == HTTP_OK

        # Out-of-band activation while the v1 server keeps running.
        harness.activate(harness.v2)

        new_case = client.get(f"/patient/{UNOPENED_PID}/timepoint/0")
        existing_case = client.get(f"/patient/{REMOVED_PID}/timepoint/0")

    assert new_case.status_code == HTTP_CONFLICT
    assert "restart" in new_case.text
    assert harness.count("arm_assignments", UNOPENED_PID) == 0
    assert existing_case.status_code == HTTP_OK

    conn = connect(harness.db_path)
    try:
        assert config_history.fetch_active(conn).config_version == "v2"  # type: ignore[union-attr]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# #21 on a filtered real-data adapter: the loader must include assigned patients
# ---------------------------------------------------------------------------


def _geneva_study(
    tmp_path: Path, study_fixture_dir: Path, name: str, patient_ids: list[str]
) -> Path:
    study = yaml.safe_load((study_fixture_dir / "study_geneva.yaml").read_text())
    study["patient_ids"] = patient_ids
    study["csv_path"] = str((study_fixture_dir / study["csv_path"]).resolve())
    study["params_dir"] = str((study_fixture_dir / study["params_dir"]).resolve())
    path = tmp_path / name
    path.write_text(yaml.safe_dump(study))
    return path


def test_removed_assigned_patient_loaded_for_filtered_dataset(
    tmp_path: Path, study_fixture_dir: Path
) -> None:
    kept, removed = "geneva_fixture_001", "geneva_fixture_002"
    questions = study_fixture_dir / "questions.yaml"
    v1 = Config(
        "v1", _geneva_study(tmp_path, study_fixture_dir, "g1.yaml", [kept, removed]), questions
    )
    v2 = Config("v2", _geneva_study(tmp_path, study_fixture_dir, "g2.yaml", [kept]), questions)
    db_path = tmp_path / "geneva.db"
    clinician_id = _seed_clinician(db_path, study_id=load_study_config(v1.study_yaml).study_id)
    h = Harness(tmp_path, db_path, clinician_id, v1, v2)

    h.activate(v1)
    with h.boot(v1) as client:
        assert client.get(f"/patient/{removed}/timepoint/0").status_code == HTTP_OK
    h.activate(v2)

    with h.boot(v2) as client:
        loaded = set(client.app.state.dataset.admission["patient_id"])
        case = client.get(f"/patient/{removed}/timepoint/0")

    assert loaded == {kept, removed}
    assert case.status_code == HTTP_OK, case.text


def test_dataset_loader_adds_extra_patient_ids(tmp_path: Path, study_fixture_dir: Path) -> None:
    """Extras are read at load time and merged after the active list."""
    from ehr_simulator.cli_support import build_dataset_loader

    study_yaml = _geneva_study(tmp_path, study_fixture_dir, "g.yaml", ["geneva_fixture_001"])
    study = load_study_config(study_yaml)

    only_active = build_dataset_loader(study)()
    with_extra = build_dataset_loader(study, extra_patient_ids=lambda: ["geneva_fixture_002"])()

    assert set(only_active.admission["patient_id"]) == {"geneva_fixture_001"}
    assert set(with_extra.admission["patient_id"]) == {"geneva_fixture_001", "geneva_fixture_002"}
