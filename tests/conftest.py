"""Shared pytest fixtures for the EHR simulator test suite.

The ``dataset`` fixture caches one synthetic dataset for the whole session;
``load_synthetic`` is read-only so reuse is safe and saves test time.

``tmp_log_dir`` returns a per-test ``Path`` that callers pass into
``create_app(log_dir=...)`` per Decision D1. It also resets structlog's
contextvars between tests so a stale ``request_id`` cannot leak across.

``client`` builds a fresh ``TestClient`` per test against an isolated
``create_app`` instance with the synthetic dataset loader.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ehr_simulator.ingestion.synthetic import SyntheticDataset, load_synthetic
from ehr_simulator.logging import reset_request_context


@pytest.fixture(scope="session")
def dataset() -> SyntheticDataset:
    return load_synthetic()


@pytest.fixture
def tmp_log_dir(tmp_path: Path) -> Iterator[Path]:
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    reset_request_context()
    yield log_dir
    reset_request_context()


@pytest.fixture
def geneva_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "geneva"


@pytest.fixture
def mimic_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "mimic"


@pytest.fixture
def client(tmp_log_dir: Path, dataset: SyntheticDataset) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from ehr_simulator.web.app import create_app

    app = create_app(log_dir=tmp_log_dir, dataset_loader=lambda: dataset)
    with TestClient(app) as test_client:
        yield test_client
