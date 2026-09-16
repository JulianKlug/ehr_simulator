"""Shared pytest fixtures for the EHR simulator test suite.

The ``dataset`` fixture caches one synthetic dataset for the whole session;
``load_synthetic`` is read-only so reuse is safe and saves test time.

``tmp_log_dir`` returns a per-test ``Path`` that callers pass into
``create_app(log_dir=...)`` per Decision D1. It also resets structlog's
contextvars between tests so a stale ``request_id`` cannot leak across.

``client`` (S6+) builds a fresh ``TestClient`` per test, pre-seeds a
clinician row, and threads the cookie + ``db_path=tmp_db_path`` +
``backup_dir=tmp_backup_dir`` through ``create_app`` so existing S2+ route
tests (which now hit auth-protected routes) stay green without rewrites.
The pre-seed step also primes the lifespan ``known_clinicians`` cache
(review-fix R11) so cache lookups succeed every request.

``anonymous_client`` is the rare bare client (no cookie, no seed) for
tests that need to exercise the unauthenticated path explicitly. Used by
``test_login.py``.

``study_client`` (S9a+) is ``client`` built through ``app_from_study_config``
on the synthetic study + questions fixtures, so the questions pane renders
and ``POST …/answer`` has a config to validate against.
``study_clinician_id`` is the seeded id for row assertions.
"""

from __future__ import annotations

import hashlib
import sqlite3
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
def study_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "study"


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "ehr_simulator.db"


@pytest.fixture
def tmp_backup_dir(tmp_path: Path) -> Path:
    return tmp_path / "backups"


@pytest.fixture
def db(tmp_db_path: Path) -> Iterator[sqlite3.Connection]:
    from ehr_simulator.db import apply_migrations, connect

    conn = connect(tmp_db_path)
    apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


def _seed_clinician(tmp_db_path: Path) -> str:
    """Insert ``Dr. Test`` into a fresh DB; return the canonical id."""
    from ehr_simulator.db import apply_migrations, connect

    name = "Dr. Test"
    name_normalized = " ".join(name.casefold().split())
    clinician_id = hashlib.sha256(name_normalized.encode("utf-8")).hexdigest()[:16]
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    seed_conn = connect(tmp_db_path)
    apply_migrations(seed_conn)
    seed_conn.execute(
        "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (clinician_id, name_normalized),
    )
    seed_conn.commit()
    seed_conn.close()
    return clinician_id


@pytest.fixture
def client(
    tmp_log_dir: Path,
    dataset: SyntheticDataset,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from ehr_simulator.web.app import create_app

    clinician_id = _seed_clinician(tmp_db_path)
    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=lambda: dataset,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as test_client:
        test_client.cookies.set("ehrsim_clinician_id", clinician_id)
        yield test_client


@pytest.fixture
def logged_in_client(client: object) -> object:
    """Alias for ``client``. Exists so test_login.py can spell out intent."""
    return client


@pytest.fixture
def anonymous_client(
    tmp_log_dir: Path,
    dataset: SyntheticDataset,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> Iterator[object]:
    """Fresh TestClient with no cookie and no seeded clinician.

    Used by ``test_login.py`` for the unauthenticated walk: GET /login
    renders, POST /login normalizes, protected routes 303 to /login.
    """
    from fastapi.testclient import TestClient

    from ehr_simulator.web.app import create_app

    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=lambda: dataset,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def study_clinician_id(tmp_db_path: Path) -> str:
    return _seed_clinician(tmp_db_path)


@pytest.fixture
def study_client(
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
    study_fixture_dir: Path,
    study_clinician_id: str,
) -> Iterator[object]:
    from fastapi.testclient import TestClient

    from ehr_simulator.web.app import app_from_study_config

    app = app_from_study_config(
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as test_client:
        test_client.cookies.set("ehrsim_clinician_id", study_clinician_id)
        yield test_client
