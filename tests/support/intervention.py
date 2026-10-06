"""AI intervention delivery (S11g) helpers: arm-split cases, rendered pages."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from ehr_simulator.config.study import StudyConfig
from tests.conftest import seed_progress
from tests.support.cases import Harness, _start, _started_patient
from tests.support.telemetry import TELEMETRY

T_MINUTES = (0.0, 60.0, 180.0)
FIRST_T = 0


def _study_dict(study_fixture_dir: Path) -> dict[str, Any]:
    data = yaml.safe_load((study_fixture_dir / "study_randomised.yaml").read_text())
    # S11j: preflight FAILs a Phase 2 study without telemetry.
    data["telemetry"] = TELEMETRY
    return data


def _study(study_fixture_dir: Path, **changes: Any) -> StudyConfig:
    data = _study_dict(study_fixture_dir)
    data.update(changes)
    return StudyConfig.model_validate(data)


def _cases_by_arm(harness: Harness, client: TestClient) -> dict[str, str]:
    """Start two cases (opposite arms under block length 1); ``{arm: patient}``."""
    first = _started_patient(_start(client))
    seed_progress(client, first, len(T_MINUTES) - 1, completed=True)
    second = _started_patient(_start(client))
    arms = {a.patient_id: a.arm for a in harness.assignments()}
    assert {arms[first], arms[second]} == {"ai", "no_ai"}
    return {arms[first]: first, arms[second]: second}


def _page(client: TestClient, patient_id: str, t_index: int = FIRST_T, chrome: str = "epic") -> str:
    response = client.get(f"/patient/{patient_id}/timepoint/{t_index}?chrome={chrome}")
    assert response.status_code == 200, response.text
    return response.text
