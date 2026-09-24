"""End-to-end tests for ``app_from_study_config`` + the ``study_timepoints``
regression locked by /plan-eng-review issue 1.2, plus S6 lifespan tests.

These tests close the silent study-validity bug: without
``app.state.study_timepoints``, a Geneva pilot whose study config declares
``timepoints: [0, 60, 180]`` would resolve URL ``t_index=1`` to the
**dataset's** second distinct ``t_minutes`` (often 60 minutes — coincidentally
correct for synthetic but off-by-many-minutes on Geneva real data with 24+
distinct timepoints).

S6 adds lifespan-wiring tests: ``app.state.db`` exists, migrations applied,
``known_clinicians`` populated, ingestion_issues recorded, non-AdapterError
exceptions surface as ``app.boot.failed``.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ehr_simulator.db import MIGRATIONS
from ehr_simulator.web.app import app_from_study_config, create_app


def _seed_and_cookie(tmp_db_path: Path, client: TestClient) -> str:
    from ehr_simulator.db import apply_migrations, connect

    name = "Dr. Test"
    name_normalized = " ".join(name.casefold().split())
    clinician_id = hashlib.sha256(name_normalized.encode("utf-8")).hexdigest()[:16]
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    conn.execute(
        "INSERT OR IGNORE INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (clinician_id, name_normalized),
    )
    conn.commit()
    conn.close()
    client.cookies.set("ehrsim_clinician_id", clinician_id)
    return clinician_id


def _pre_seed(tmp_db_path: Path, study_path: Path, questions_path: Path) -> str:
    """Seed the test DB *before* the app boots so the lifespan picks the
    clinician up into ``known_clinicians`` and the protected-route cache
    lookup succeeds. S11a: the study's identity is bound BEFORE the
    clinician row is seeded — bind refuses to claim a non-empty unbound
    DB, so seeding must follow binding. S11b: activation rides on top of
    the bound identity (its foreign key requires it); boot now refuses
    without an active configuration.
    """
    from ehr_simulator.config import load_study_config
    from ehr_simulator.db import apply_migrations, connect, study_identity
    from tests.conftest import _activate_configuration

    study = load_study_config(study_path)
    name = "Dr. Test"
    name_normalized = " ".join(name.casefold().split())
    clinician_id = hashlib.sha256(name_normalized.encode("utf-8")).hexdigest()[:16]
    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    study_identity.bind(conn, study.study_id)
    conn.execute(
        "INSERT OR IGNORE INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (clinician_id, name_normalized),
    )
    conn.commit()
    conn.close()
    _activate_configuration(
        tmp_db_path, study_path, questions_path, version="v1", description="test activation"
    )
    return clinician_id


def test_app_from_study_config_synthetic_renders_synth_001(
    study_fixture_dir: Path,
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    clinician_id = _pre_seed(
        tmp_db_path,
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
    )
    app = app_from_study_config(
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        response = client.get("/patient/synth_001/timepoint/0")
        assert response.status_code == 200
        # Patient summary card includes the patient_id.
        assert "synth_001" in response.text


def test_app_from_study_config_t_index_resolves_to_study_timepoints(
    study_fixture_dir: Path,
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """REGRESSION (per /plan-eng-review issue 1.2)."""
    custom_dir = tmp_log_dir.parent / "study"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "2"
study_id: app_test
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0, 180]
""",
        encoding="utf-8",
    )
    clinician_id = _pre_seed(tmp_db_path, study_path, study_fixture_dir / "questions.yaml")
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )

    # app.state binding (the fix)
    assert app.state.study_timepoints == [0.0, 180.0]

    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        # S9b gate: t=1 is behind the frontier of a fresh DB. follow_redirects
        # is off so the 303 cannot be silently followed to t=0 (review-fix R2).
        gated = client.get("/patient/synth_001/timepoint/1", follow_redirects=False)
        assert gated.status_code == 303
        assert gated.headers["location"] == "/patient/synth_001/timepoint/0?chrome=epic"
        response_t0 = client.get("/patient/synth_001/timepoint/0")
        assert response_t0.status_code == 200

        from ehr_simulator.db import progress

        progress.unlock(
            app.state.db,
            clinician_id=clinician_id,
            patient_id="synth_001",
            from_t_index=0,
            to_t_index=1,
            config_hash=app.state.config_hash,
            config_version=app.state.config_version,
        )
        response = client.get("/patient/synth_001/timepoint/1")
        assert response.status_code == 200
        assert "synth_001" in response.text
        assert 'data-t-index="1"' in response.text
        assert 'data-t-minutes="180.0"' in response.text


def test_serve_no_config_path_does_not_set_study_timepoints(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """The synthetic-only ``serve`` path (no --config) keeps the S2 behavior:
    routes fall back to ``patient_timepoints(dataset, pid)``. Locks the
    "no-config path is unchanged" acceptance criterion in spec §12.
    """
    app = create_app(log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)
    assert not hasattr(app.state, "study_timepoints") or app.state.study_timepoints is None


def test_app_from_study_config_index_lists_only_study_patients(
    study_fixture_dir: Path,
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """The synthetic dataset has 3 patients (synth_001/002/003). When the
    study config declares only one of them, the index page must render
    only that one — not the full dataset list. Closes the post-S5 user
    report: "all patients are loaded instead of loading only patients in
    patient_ids" against the Geneva real-data CSV (~3K patients).
    """
    custom_dir = tmp_log_dir.parent / "study_subset"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "2"
study_id: app_test
dataset: synthetic
patient_ids: [synth_002]
time_unit: minutes
timepoints: [0, 60]
""",
        encoding="utf-8",
    )

    clinician_id = _pre_seed(tmp_db_path, study_path, study_fixture_dir / "questions.yaml")
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    assert app.state.study_patient_ids == ["synth_002"]

    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        index = client.get("/")
        assert index.status_code == 200
        assert "synth_002" in index.text
        assert "synth_001" not in index.text
        assert "synth_003" not in index.text

        # Off-study patient URL 404s with the "not part of this study" message.
        off_study = client.get("/patient/synth_001/timepoint/0")
        assert off_study.status_code == 404
        assert "not part of this study" in off_study.text

        # In-study patient still works.
        in_study = client.get("/patient/synth_002/timepoint/0")
        assert in_study.status_code == 200

        # The summary card's patient-jumper navigation must ALSO honor
        # study_patient_ids — not just the index page.
        assert "/patient/synth_001/timepoint" not in in_study.text
        assert "/patient/synth_003/timepoint" not in in_study.text


def test_app_from_study_config_preserves_patient_id_order(
    study_fixture_dir: Path,
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """Spec §2: declared order is meaningful."""
    custom_dir = tmp_log_dir.parent / "study_order"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "2"
study_id: app_test
dataset: synthetic
patient_ids: [synth_003, synth_001, synth_002]
time_unit: minutes
timepoints: [0, 60]
""",
        encoding="utf-8",
    )

    clinician_id = _pre_seed(tmp_db_path, study_path, study_fixture_dir / "questions.yaml")
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        index = client.get("/")
        idx_003 = index.text.find("synth_003")
        idx_001 = index.text.find("synth_001")
        idx_002 = index.text.find("synth_002")
        assert idx_003 < idx_001 < idx_002, (
            f"expected declared order [003, 001, 002] but got "
            f"positions: 003={idx_003}, 001={idx_001}, 002={idx_002}"
        )


def test_serve_no_config_path_does_not_set_study_patient_ids(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    app = create_app(log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)
    assert not hasattr(app.state, "study_patient_ids") or app.state.study_patient_ids is None


# ---------------------------------------------------------------------------
# S6: lifespan-wiring carryover tests (#28, #29, #29b, #29c)
# ---------------------------------------------------------------------------


def test_lifespan_wires_db_and_runs_migrations(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    app = create_app(log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)
    with TestClient(app):
        assert isinstance(app.state.db, sqlite3.Connection)
        rows = app.state.db.execute("SELECT version, name FROM schema_migrations").fetchall()
        assert len(rows) == len(MIGRATIONS)
        assert rows[0][0] == 1


def test_lifespan_records_ingestion_issues_when_dataset_carries_them(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """REVISED (review-fix R6): lifespan reads ``app.state.dataset.issues``."""
    from ehr_simulator.ingestion.exceptions import IngestionIssue
    from ehr_simulator.ingestion.synthetic import load_synthetic

    class _FakeDataset:
        def __init__(self, real: object) -> None:
            self._real = real
            self.issues = [IngestionIssue("synth", "p1", 7, "bad row")]

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    real = load_synthetic()
    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=lambda: _FakeDataset(real),
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app):
        rows = app.state.db.execute(
            "SELECT dataset, patient_id, row_idx, reason, boot_id FROM ingestion_issues"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "synth"
        assert rows[0][1] == "p1"
        assert rows[0][2] == 7
        assert rows[0][3] == "bad row"
        assert rows[0][4] == app.state.boot_id


def test_lifespan_populates_known_clinicians_cache(
    tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """review-fix R11: cache primed from ``SELECT clinician_id FROM clinicians``."""
    from ehr_simulator.db import apply_migrations, connect

    tmp_db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    conn.executemany(
        "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        [("a" * 16, "dr. a"), ("b" * 16, "dr. b")],
    )
    conn.commit()
    conn.close()

    app = create_app(log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)
    with TestClient(app):
        assert app.state.known_clinicians == {"a" * 16, "b" * 16}


def test_lifespan_handles_non_adapter_exceptions(
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """review-fix R9: non-AdapterError exceptions log ``app.boot.failed`` and exit 1.
    No DB file is created.

    TestClient routes the lifespan SystemExit through anyio's portal as an
    unhandled task exception rather than re-raising it on context entry,
    so the observable contract is: (a) ``app.boot.failed`` is logged with
    the wrapped error, (b) no DB file appears at ``db_path``.
    """
    import json
    import logging as _stdlogging
    import warnings

    def _broken_loader() -> Iterator[object]:
        raise FileNotFoundError("missing CSV")

    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=_broken_loader,  # type: ignore[arg-type]
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with TestClient(app):
                pass
        except BaseException:  # noqa: BLE001
            pass

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    log_file = tmp_log_dir / "current.jsonl"
    records = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    boot_failed = [r for r in records if r.get("event_kind") == "app.boot.failed"]
    assert boot_failed, "expected app.boot.failed event in log"
    assert "FileNotFoundError" in boot_failed[0]["error"]
    assert not tmp_db_path.exists()


def test_app_from_study_config_sets_questions_and_config_hash(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    from ehr_simulator.config import compute_config_hash
    from ehr_simulator.config.questions import Questions

    study_path = study_fixture_dir / "study_synthetic.yaml"
    questions_path = study_fixture_dir / "questions.yaml"
    app = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    assert isinstance(app.state.questions, Questions)
    assert len(app.state.questions.questions) == 7
    assert app.state.study is not None
    assert app.state.config_hash == compute_config_hash(study_path, questions_path)

    bare = create_app(log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)
    assert (bare.state.study, bare.state.questions, bare.state.config_hash) == (None, None, None)


def test_app_from_study_config_warns_when_no_question_required(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    from structlog.testing import capture_logs

    optional = tmp_log_dir.parent / "questions_optional.yaml"
    optional.write_text(
        """schema_version: "1"
questions:
  - question_id: q1
    prompt: "Optional only"
    response_type: free-text
    required: false
""",
        encoding="utf-8",
    )
    with capture_logs() as logs:
        app_from_study_config(
            study_fixture_dir / "study_synthetic.yaml",
            optional,
            log_dir=tmp_log_dir,
            db_path=tmp_db_path,
            backup_dir=tmp_backup_dir,
        )
    assert any(log.get("event_kind") == "questions.none_required" for log in logs)

    with capture_logs() as quiet:
        app_from_study_config(
            study_fixture_dir / "study_synthetic.yaml",
            study_fixture_dir / "questions.yaml",
            log_dir=tmp_log_dir,
            db_path=tmp_db_path,
            backup_dir=tmp_backup_dir,
        )
    assert not any(log.get("event_kind") == "questions.none_required" for log in quiet)


# ---------------------------------------------------------------------------
# S11a: study-identity lifespan integration
#
# The four boot outcomes a study-mode app can hit when it meets an existing
# database: bound-to-me (fresh bind), bound-to-me (restart), bound-to-
# another study (refused), and unbound with preexisting data (refused).
# Refusals are asserted through their observable effects — the ``R9``
# pattern: TestClient routes the lifespan ``SystemExit(1)`` through
# anyio's portal as an unhandled task exception rather than re-raising it
# on context entry.
# ---------------------------------------------------------------------------


def _study_paths(study_fixture_dir: Path) -> tuple[Path, Path]:
    return (
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
    )


def _other_study_path(tmp_log_dir: Path) -> Path:
    """Inline study config on the same synthetic dataset, different identity."""
    p = tmp_log_dir.parent / "study_other_fixture.yaml"
    p.write_text(
        """schema_version: "2"
study_id: other_fixture
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0, 60, 180]
""",
        encoding="utf-8",
    )
    return p


def _seed_clinician_row(db_path: Path, *, bound_study_id: str | None = None) -> str:
    import hashlib

    from ehr_simulator.db import apply_migrations, connect, study_identity

    name = "Dr. Preexisting"
    nn = " ".join(name.casefold().split())
    cid = hashlib.sha256(nn.encode("utf-8")).hexdigest()[:16]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    apply_migrations(conn)
    if bound_study_id is not None:
        # Bind FIRST: bind refuses to claim a non-empty unbound DB, so the
        # preexisting clinician row only exists after the identity is set.
        study_identity.bind(conn, bound_study_id)
    conn.execute(
        "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (cid, nn),
    )
    conn.commit()
    conn.close()
    return cid


def _boot_expect_refused(app: object, log_dir: Path) -> list[dict]:
    """Enter/exit the app lifespan expecting a boot refusal; return the
    ``app.boot.failed`` log records so the caller can assert the reason."""
    import json
    import logging as _stdlogging
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with TestClient(app):  # type: ignore[arg-type]
                pass
        except BaseException:  # noqa: BLE001
            pass

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    records: list[dict] = []
    log_file = log_dir / "current.jsonl"
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("event_kind") == "app.boot.failed":
                    records.append(r)
    assert records, "expected an app.boot.failed log record on refused boot"
    assert any("study identity refused" in rec.get("event", "") for rec in records)
    assert any("StudyIdentityError" in rec.get("error", "") for rec in records)
    return records


def test_lifespan_binds_fresh_study_db_on_first_boot(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """S11b: a fresh, empty database is STILL BOUND to the study on first boot
    (S11a), and the boot then refuses for the S11b configuration gate —
    activation is never implicit. The bound identity survives the refused
    boot so the first activation can claim it.
    """
    import json
    import warnings

    from ehr_simulator.db import connect, study_identity

    study_path, questions_path = _study_paths(study_fixture_dir)
    app = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        refused = False
        try:
            with TestClient(app):
                pass
        except BaseException:  # noqa: BLE001 — lifespan SystemExit(1) routed via the portal
            refused = True
    assert refused

    # The config gate fired (not the identity gate) and after binding.
    log_file = tmp_log_dir / "current.jsonl"
    failed = []
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("event_kind") == "app.boot.failed":
                    failed.append(r)
    assert any("failed to validate active configuration" in rec.get("event", "") for rec in failed)

    conn = connect(tmp_db_path)
    assert study_identity.fetch(conn) == "fixture_synthetic"  # binding survived
    conn.close()

    # With the first activation registered, the same YAML now boots cleanly.
    from tests.conftest import _activate_configuration

    _activate_configuration(
        tmp_db_path, study_path, questions_path, version="v1", description="first activation"
    )
    with TestClient(app) as client:
        assert client.get("/login").status_code == 200


def test_lifespan_same_study_restarts_successfully(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """S11a: booting the same study again against its own database must
    succeed — the identity is stable across restarts. S11b: the activation
    (one registration) also survives — the configured hash has not moved.
    """
    from ehr_simulator.db import connect, study_identity

    study_path, questions_path = _study_paths(study_fixture_dir)
    # Operator order: bind → activate → boot (the DB is otherwise unclaimable).
    _pre_seed(tmp_db_path, study_path, questions_path)

    first = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(first) as client:
        assert client.get("/login").status_code == 200

    conn = connect(tmp_db_path)
    assert study_identity.fetch(conn) == "fixture_synthetic"
    conn.close()

    second = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(second) as client:
        assert client.get("/login").status_code == 200
        assert second.state.config_version == "v1"

    conn = connect(tmp_db_path)
    assert study_identity.fetch(conn) == "fixture_synthetic"
    conn.close()


def test_lifespan_different_study_against_same_db_refused(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """S11a: a database already bound to one study must refuse to boot for a
    different study — before the clinician cache is populated."""
    from ehr_simulator.db import connect, study_identity

    cid = _seed_clinician_row(tmp_db_path, bound_study_id="fixture_synthetic")

    other_study = _other_study_path(tmp_log_dir)
    app = app_from_study_config(
        other_study,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    _boot_expect_refused(app, tmp_log_dir)

    # Refused BEFORE the clinician state load: the preexisting row must not
    # have been picked up into the protected-route cache.
    assert app.state.known_clinicians == set()
    assert cid not in app.state.known_clinicians

    conn = connect(tmp_db_path)
    assert study_identity.fetch(conn) == "fixture_synthetic"  # identity untouched
    assert conn.execute("SELECT COUNT(*) FROM ingestion_issues").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0] == 1  # unmodified
    conn.close()


def test_lifespan_nonempty_unbound_legacy_db_refused_before_app_reads_writes(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """S11a: a nonempty pre-S11a (unbound) database is refused before ANY
    application read or write — the app must not silently claim it or
    invent an identity."""
    from ehr_simulator.db import connect, study_identity

    cid = _seed_clinician_row(tmp_db_path)  # no identify bound at all

    study_path, questions_path = _study_paths(study_fixture_dir)
    app = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    _boot_expect_refused(app, tmp_log_dir)

    # The clinician cache load happens after the bind and did not run.
    assert app.state.known_clinicians == set()
    assert cid not in app.state.known_clinicians

    conn = connect(tmp_db_path)
    assert study_identity.fetch(conn) is None  # identity NOT invented
    assert conn.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM ingestion_issues").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()


def test_lifespan_refuses_hash_mismatch_against_active_configuration(
    study_fixture_dir: Path, tmp_log_dir: Path, tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    """S11b: booting with a YAML whose computed hash differs from the active
    configuration's stored hash is an integrity refusal, not a fallback."""
    import yaml

    from ehr_simulator.config import load_questions

    study_path, questions_path = _study_paths(study_fixture_dir)
    _pre_seed(tmp_db_path, study_path, questions_path)

    # A questions file identical except for one prompt -> different hash.
    model = load_questions(questions_path)
    first = model.questions[0]
    first.prompt = first.prompt + " (revised)"
    variant_path = tmp_db_path.parent / "questions_variant.yaml"
    variant_path.write_text(yaml.safe_dump(model.model_dump(mode="json")))

    app = app_from_study_config(
        study_path,
        variant_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )

    import json
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        refused = False
        try:
            with TestClient(app):
                pass
        except BaseException:  # noqa: BLE001 — lifespan SystemExit(1) routed via the portal
            refused = True
    assert refused

    failed = []
    log_file = tmp_log_dir / "current.jsonl"
    if log_file.exists():
        for line in log_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("event_kind") == "app.boot.failed":
                    failed.append(r)
    assert any("failed to validate active configuration" in rec.get("event", "") for rec in failed)
    assert any(
        "differs from the active configuration hash" in rec.get("error", "") for rec in failed
    )
    assert getattr(app.state, "config_version", None) is None


def _tamper_prompt(questions_json: str) -> str:
    """Valid JSON that no longer hashes to the stored config_hash."""
    import json

    doc = json.loads(questions_json)
    doc["questions"][0]["prompt"] += " (tampered)"
    return json.dumps(doc)


@pytest.mark.parametrize(
    ("column", "corrupt"),
    [
        ("study_json", lambda _: "{not json"),
        ("questions_json", lambda _: "{not json"),
        ("questions_json", _tamper_prompt),
    ],
    ids=["study-unparseable", "questions-unparseable", "questions-rehash-mismatch"],
)
def test_lifespan_refuses_corrupted_active_snapshot(
    study_fixture_dir: Path,
    tmp_log_dir: Path,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
    column: str,
    corrupt: object,
) -> None:
    """S11b boot test #11: a corrupted stored snapshot refuses boot."""
    import json
    import warnings

    from ehr_simulator.db import connect

    study_path, questions_path = _study_paths(study_fixture_dir)
    _pre_seed(tmp_db_path, study_path, questions_path)
    conn = connect(tmp_db_path)
    stored = conn.execute(f"SELECT {column} FROM configuration_history").fetchone()[0]
    conn.execute(f"UPDATE configuration_history SET {column} = ?", (corrupt(stored),))  # type: ignore[operator]
    conn.commit()
    conn.close()

    app = app_from_study_config(
        study_path,
        questions_path,
        log_dir=tmp_log_dir,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    refused = False
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with TestClient(app):
                pass
        except BaseException:  # noqa: BLE001 — lifespan SystemExit(1) routed via the portal
            refused = True

    assert refused
    failed = [
        json.loads(line)
        for line in (tmp_log_dir / "current.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("event_kind") == "app.boot.failed"
    ]
    assert any("failed to validate active configuration" in r.get("event", "") for r in failed)
    assert getattr(app.state, "config_version", None) is None
