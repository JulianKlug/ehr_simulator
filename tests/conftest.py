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


def _seed_clinician(tmp_db_path: Path, *, study_id: str | None = None) -> str:
    """Insert ``Dr. Test`` into a fresh DB; return the canonical id.

    When ``study_id`` is given (study mode), the database is bound to that
    study BEFORE the clinician row is seeded — S11a refuses to claim a DB
    that already holds application data, so seeding must follow binding.
    """
    from ehr_simulator.db import apply_migrations, connect, study_identity

    name = "Dr. Test"
    name_normalized = " ".join(name.casefold().split())
    clinician_id = hashlib.sha256(name_normalized.encode("utf-8")).hexdigest()[:16]
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    seed_conn = connect(tmp_db_path)
    apply_migrations(seed_conn)
    if study_id is not None:
        study_identity.bind(seed_conn, study_id)
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
def study_clinician_id(tmp_db_path: Path, study_fixture_dir: Path) -> str:
    from ehr_simulator.config import load_study_config

    study = load_study_config(study_fixture_dir / "study_synthetic.yaml")
    return _seed_clinician(tmp_db_path, study_id=study.study_id)


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


# ---------------------------------------------------------------------------
# S9b helpers (spec §2 #24): reach a complete cell / a seeded frontier
# ---------------------------------------------------------------------------


def _valid_value(question: object) -> str:
    """One accepted value per response type, read off the question model."""
    response_type = question.response_type  # type: ignore[attr-defined]
    if response_type in {"categorical", "multi-select"}:
        return question.options[0]  # type: ignore[attr-defined]
    if response_type == "likert":
        return str(question.scale_min)  # type: ignore[attr-defined]
    if response_type == "probability-0-100":
        return "50"
    return "x"


def answer_all_required(client: object, patient_id: str, t_index: int) -> list[str]:
    """POST one valid answer per ``required`` question of the running config.

    Derived from ``app.state.questions`` so a fixture change breaks loudly
    (review-fix R15). Returns the question ids answered.
    """
    questions = client.app.state.questions  # type: ignore[attr-defined]
    answered: list[str] = []
    for q in questions.questions:
        if not q.required:
            continue
        r = client.post(  # type: ignore[attr-defined]
            f"/patient/{patient_id}/timepoint/{t_index}/answer",
            data={"question_id": q.question_id, "value": _valid_value(q)},
        )
        assert r.status_code == 200, (q.question_id, r.status_code, r.text)
        answered.append(q.question_id)
    required = [q.question_id for q in questions.questions if q.required]
    assert answered == required
    return answered


def seed_progress(
    client: object, patient_id: str, unlocked_t_index: int, *, completed: bool = False
) -> None:
    """Write a ``progress`` row for the cookie's clinician without going through ``/advance``."""
    from ehr_simulator.db import progress

    db = client.app.state.db  # type: ignore[attr-defined]
    clinician_id = client.cookies.get("ehrsim_clinician_id")  # type: ignore[attr-defined]
    config_hash = client.app.state.config_hash  # type: ignore[attr-defined]
    if unlocked_t_index > 0:
        # A second seed for the same pair would miss the compare-and-set and
        # silently write nothing; fail loudly instead.
        moved = progress.unlock(
            db,
            clinician_id=clinician_id,
            patient_id=patient_id,
            from_t_index=0,
            to_t_index=unlocked_t_index,
            config_hash=config_hash,
        )
        assert moved, "seed_progress: frontier already moved for this pair"
    if completed:
        progress.mark_complete(
            db,
            clinician_id=clinician_id,
            patient_id=patient_id,
            unlocked_t_index=unlocked_t_index,
            config_hash=config_hash,
        )
