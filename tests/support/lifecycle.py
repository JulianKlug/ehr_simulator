"""Case lifecycle harness (S11e): the case harness plus an injected clock."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ehr_simulator import cli
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from tests.conftest import _seed_clinician, answer_all_required, seed_progress
from tests.support.cases import HTTP_SEE_OTHER, LAST_T_INDEX, Config, Harness

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


def _with_lifecycle(tmp_path: Path, study_fixture_dir: Path, **overrides: Any) -> LifecycleHarness:
    """A fresh harness whose v1 carries ``VALID_LIFECYCLE`` with ``overrides``."""
    data = yaml.safe_load((study_fixture_dir / "study_lifecycle.yaml").read_text())
    data["case_lifecycle"] = {**VALID_LIFECYCLE, **overrides}
    path = tmp_path / "study_variant.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, study_fixture_dir / "questions.yaml")


def _url(patient_id: str, t_index: int = 0) -> str:
    return f"/patient/{patient_id}/timepoint/{t_index}"


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


def _cli(lh: LifecycleHarness, *args: str):
    return CliRunner().invoke(cli.app_typer, [*args, "--db-path", str(lh.db_path)])


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
