"""Phase 2 case harness (S11d): a study DB plus a booted app per clinician."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ehr_simulator.config import compute_config_hash_from_models, load_questions, load_study_config
from ehr_simulator.db import arm_assignments, clinicians, connect
from ehr_simulator.db import randomisation as schedules_dao
from ehr_simulator.web.app import app_from_study_config
from tests.conftest import (
    ProfileSetup,
    _activate_configuration,
    _seed_clinician,
    adopt_tab_views,
    ensure_profile,
)

HTTP_OK = 200
HTTP_SEE_OTHER = 303
HTTP_CONFLICT = 409
HX = {"HX-Request": "true"}
COOKIE = "ehrsim_clinician_id"
INDEX_URL = "/"
START_URL = "/case/start"
LAST_T_INDEX = 2
SECOND_CLINICIAN = "Dr. Two"
CASE_KINDS = ("case.activated", "session.start")
ARM_MARKERS = ("no_ai", "phase2_randomized", "data-arm")


@dataclass(frozen=True)
class Config:
    version: str
    study_yaml: Path
    questions_yaml: Path

    @property
    def study(self):
        return load_study_config(self.study_yaml)

    @property
    def questions(self):
        return load_questions(self.questions_yaml)

    @property
    def config_hash(self) -> str:
        return compute_config_hash_from_models(self.study, self.questions)


@dataclass
class Harness:
    tmp_path: Path
    db_path: Path
    clinician_id: str
    v1: Config
    #: S11p: give every booted clinician a valid profile when the study asks
    #: for one (the gate's own tests switch this off).
    profile_setup: ProfileSetup = field(default_factory=lambda: ProfileSetup.AUTO)

    def activate(self, config: Config) -> None:
        _activate_configuration(
            self.db_path,
            config.study_yaml,
            config.questions_yaml,
            version=config.version,
            description=f"activate {config.version}",
        )

    def variant(self, version: str, **changes: Any) -> Config:
        """A copy of v1's study YAML with top-level keys replaced."""
        data = yaml.safe_load(self.v1.study_yaml.read_text())
        data.update(changes)
        path = self.tmp_path / f"study_{version}.yaml"
        path.write_text(yaml.safe_dump(data))
        return Config(version, path, self.v1.questions_yaml)

    @contextmanager
    def boot(self, config: Config | None = None, clinician_id: str | None = None):
        config = config or self.v1
        app = app_from_study_config(
            config.study_yaml,
            config.questions_yaml,
            log_dir=self.tmp_path / "logs",
            db_path=self.db_path,
            backup_dir=self.tmp_path / "backups",
        )
        with TestClient(app) as client:
            client.cookies.set(COOKIE, clinician_id or self.clinician_id)
            if self.profile_setup is ProfileSetup.AUTO:
                ensure_profile(client)
            adopt_tab_views(client)
            yield client

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def count(self, sql: str, params: tuple = ()) -> int:
        with self.conn() as conn:
            return conn.execute(sql, params).fetchone()[0]

    def dump(self) -> dict[str, list[tuple]]:
        tables = (
            "arm_assignments",
            "sessions",
            "progress",
            "randomisation_schedules",
            "randomisation_schedule_items",
        )
        with self.conn() as conn:
            dumped = {
                t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in tables
            }
            dumped["case_events"] = [
                tuple(r)
                for r in conn.execute(
                    "SELECT kind, patient_id FROM events WHERE kind IN (?, ?) ORDER BY event_id",
                    CASE_KINDS,
                )
            ]
        return dumped

    def assignments(self, clinician_id: str | None = None):
        with self.conn() as conn:
            return arm_assignments.list_for_clinician(conn, clinician_id or self.clinician_id)

    def schedule(self, clinician_id: str | None = None):
        with self.conn() as conn:
            stored = schedules_dao.fetch_for_clinician(
                conn, self.v1.study.study_id, clinician_id or self.clinician_id
            )
        assert stored is not None
        return stored.schedule

    def add_clinician(self, name: str) -> str:
        with self.conn() as conn:
            return clinicians.lookup_or_create(conn, name)


def new_harness(tmp_path: Path, study_fixture_dir: Path) -> Harness:
    """``study_randomised.yaml`` activated as v1, ``Dr. Test`` seeded."""
    v1 = Config(
        "v1", study_fixture_dir / "study_randomised.yaml", study_fixture_dir / "questions.yaml"
    )
    db_path = tmp_path / "study.db"
    clinician_id = _seed_clinician(db_path, study_id=v1.study.study_id)
    h = Harness(tmp_path, db_path, clinician_id, v1)
    h.activate(v1)
    return h


@pytest.fixture
def harness(tmp_path: Path, study_fixture_dir: Path) -> Harness:
    return new_harness(tmp_path, study_fixture_dir)


def _start(client: TestClient, *, htmx: bool = False):
    return client.post(START_URL, headers=HX if htmx else {}, follow_redirects=False)


def _started_patient(response) -> str:
    assert response.status_code == HTTP_SEE_OTHER, response.text
    return response.headers["location"].split("/")[2]
