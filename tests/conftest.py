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

import sqlite3
from collections.abc import Iterator
from enum import StrEnum
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


def drop_append_only_triggers(conn: sqlite3.Connection) -> None:
    """Let a test tamper with the append-only tables (migration 15) to
    simulate out-of-band corruption."""
    from ehr_simulator.db.migrations import _APPEND_ONLY_TABLES

    for table in _APPEND_ONLY_TABLES:
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_update")
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_no_delete")


def _seed_clinician(tmp_db_path: Path, *, study_id: str | None = None) -> str:
    """Insert ``Dr. Test`` into a fresh DB; return the canonical id.

    When ``study_id`` is given (study mode), the database is bound to that
    study BEFORE the clinician row is seeded — S11a refuses to claim a DB
    that already holds application data, so seeding must follow binding.
    """
    from ehr_simulator.db import apply_migrations, clinicians, connect, study_identity

    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    seed_conn = connect(tmp_db_path)
    apply_migrations(seed_conn)
    if study_id is not None:
        study_identity.bind(seed_conn, study_id)
    clinician_id = clinicians.lookup_or_create(seed_conn, "Dr. Test")
    seed_conn.close()
    return clinician_id


def _activate_configuration(
    tmp_db_path: Path,
    study_yaml: Path,
    questions_yaml: Path,
    *,
    version: str,
    description: str,
    reason: str | None = None,
) -> None:
    """Register + activate one configuration on a fixture database (S11b).

    Runs on a dedicated connection that is closed before the app's lifespan
    boots, matching the operator order: activate, then start the server.
    """
    from ehr_simulator.config import (
        compute_config_hash_from_models,
        load_questions,
        load_study_config,
    )
    from ehr_simulator.db import apply_migrations, config_history, connect

    study = load_study_config(study_yaml)
    questions = load_questions(questions_yaml)
    config_hash = compute_config_hash_from_models(study, questions)
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        config_history.activate(
            conn,
            study_id=study.study_id,
            config_version=version,
            config_hash=config_hash,
            description=description,
            reason=reason,
            study=study,
            questions=questions,
        )
    finally:
        conn.close()


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
    clinician_id = _seed_clinician(tmp_db_path, study_id=study.study_id)
    # S11b: study mode refuses to boot without an active configuration, so
    # the fixture database gets one registered before the app's lifespan runs.
    _activate_configuration(
        tmp_db_path,
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
        version="v1",
        description="test activation",
    )
    return clinician_id


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
    from ehr_simulator.question_branching import evaluate

    ensure_tab_owner(client, patient_id, t_index)
    questions = client.app.state.questions  # type: ignore[attr-defined]
    answered: list[str] = []
    values: dict[str, str] = {}
    for q in questions.questions:
        # S11h: follow the branch the answers so far select.
        item = evaluate(questions, values).get(q.question_id)
        if item is None or not item.required_now:
            continue
        value = _valid_value(q)
        r = client.post(  # type: ignore[attr-defined]
            f"/patient/{patient_id}/timepoint/{t_index}/answer",
            data={"question_id": q.question_id, "value": value},
        )
        assert r.status_code == 200, (q.question_id, r.status_code, r.text)
        answered.append(q.question_id)
        values[q.question_id] = value
    assert evaluate(questions, values).remaining == ()
    return answered


def seed_progress(
    client: object, patient_id: str, unlocked_t_index: int, *, completed: bool = False
) -> None:
    """Write a ``progress`` row for the cookie's clinician without going through ``/advance``."""
    from ehr_simulator.db import progress

    db = client.app.state.db  # type: ignore[attr-defined]
    clinician_id = client.cookies.get("ehrsim_clinician_id")  # type: ignore[attr-defined]
    config_hash = client.app.state.config_hash  # type: ignore[attr-defined]
    config_version = getattr(client.app.state, "config_version", None)  # type: ignore[attr-defined]
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
            config_version=config_version,
        )
        assert moved, "seed_progress: frontier already moved for this pair"
    if completed:
        progress.mark_complete(
            db,
            clinician_id=clinician_id,
            patient_id=patient_id,
            unlocked_t_index=unlocked_t_index,
            config_hash=config_hash,
            config_version=config_version,
        )
        # S11e: the final advance completes a tracked Phase 2 case too.
        from ehr_simulator.db import case_lifecycle
        from ehr_simulator.web.case_contact import now

        lifecycle = case_lifecycle.fetch(db, clinician_id, patient_id)
        if lifecycle is not None and lifecycle.state is case_lifecycle.CaseState.ACTIVE:
            case_lifecycle.complete(
                db,
                clinician_id=clinician_id,
                patient_id=patient_id,
                now=now(client.app.state),  # type: ignore[attr-defined]
            )


# ---------------------------------------------------------------------------
# S11m: guarded case views
# ---------------------------------------------------------------------------

#: The tab every test client claims as (a lowercase UUID v4).
TEST_TAB_ID = "0b6f7c1e-3f5a-4c2d-9e8b-7a6d5c4b3a21"


def _claim_view(client: object, response: object) -> None:
    """Response hook: claim a guarded ``#patient-view`` like case_tab_guard.js."""
    from bs4 import BeautifulSoup

    from ehr_simulator.web.tab_guard import RENDER_ID_HEADER, TAB_ID_HEADER

    if "text/html" not in response.headers.get("content-type", ""):  # type: ignore[attr-defined]
        return
    response.read()  # type: ignore[attr-defined]
    view = BeautifulSoup(response.text, "html.parser").select_one(  # type: ignore[attr-defined]
        "#patient-view[data-tab-claim-url]"
    )
    if view is None:
        return

    client.headers[TAB_ID_HEADER] = TEST_TAB_ID  # type: ignore[attr-defined]
    client.headers[RENDER_ID_HEADER] = view["data-render-id"]  # type: ignore[attr-defined]
    client.tab_views[view["data-patient-id"]] = view["data-render-id"]  # type: ignore[attr-defined]
    client.post(  # type: ignore[attr-defined]
        view["data-tab-claim-url"],
        json={"tab_id": TEST_TAB_ID, "render_id": view["data-render-id"]},
    )


def adopt_tab_views(client: object) -> None:
    """S11m: make ``client`` act as one browser tab that claims every guarded
    view it receives and sends the owner headers on every later request.

    Tab guard tests that need a second tab use a client without this hook.
    """
    client.tab_views = {}  # type: ignore[attr-defined]  # patient_id -> claimed render_id
    client.event_hooks["response"].append(  # type: ignore[attr-defined]
        lambda response: _claim_view(client, response)
    )


def ensure_tab_owner(client: object, patient_id: str, t_index: int) -> None:
    """A guarded write needs a claimed view: fetch one when none was claimed
    yet (tests that seed progress instead of browsing)."""
    from ehr_simulator.web.tab_guard import RENDER_ID_HEADER

    views = getattr(client, "tab_views", None)
    study = getattr(client.app.state, "study", None)  # type: ignore[attr-defined]
    if views is None or study is None or study.telemetry is None:
        return
    if patient_id in views:
        client.headers[RENDER_ID_HEADER] = views[patient_id]  # type: ignore[attr-defined]
        return
    client.get(f"/patient/{patient_id}/timepoint/{t_index}")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# S11p: clinician profiles
# ---------------------------------------------------------------------------


class ProfileSetup(StrEnum):
    AUTO = "auto"  # save a valid profile at boot when the study collects one
    NONE = "none"


def valid_profile_form(config: object) -> dict[str, str]:
    """A physician profile drawn from the configured vocabulary."""
    return {
        "professional_role": "physician",
        "years_of_practice": "7.5",
        "country_of_practice": config.countries[0],  # type: ignore[attr-defined]
        "primary_specialty": config.specialties[0],  # type: ignore[attr-defined]
    }


def ensure_profile(client: object) -> None:
    """Save a valid profile once, unless one exists or none is collected."""
    study = getattr(client.app.state, "study", None)  # type: ignore[attr-defined]
    config = getattr(study, "clinician_profile", None)
    if config is None:
        return
    from ehr_simulator.db import clinician_profiles

    clinician_id = client.cookies.get("ehrsim_clinician_id")  # type: ignore[attr-defined]
    if clinician_profiles.fetch(client.app.state.db, clinician_id) is not None:  # type: ignore[attr-defined]
        return
    response = client.post(  # type: ignore[attr-defined]
        "/profile", data=valid_profile_form(config), follow_redirects=False
    )
    assert response.status_code == 303, response.text
