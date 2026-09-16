"""DB layer tests: connection, migrations, per-table DAOs.

Numbering and section headers track ``specs/session-06-sqlite-persistence.md``
§9; tests in this file build up across commits 1 → 2. Commit 1 owns the
connection + migrations slice (tests #1-#7 + #4b); commit 2 layers the
per-table DAOs.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

from ehr_simulator.config.exceptions import ConfigError
from ehr_simulator.db import (
    MIGRATIONS,
    DbError,
    Migration,
    answers,
    apply_migrations,
    arm_assignments,
    clinicians,
    connect,
    events,
    ingestion_issues,
    resolve_db_path,
    sessions,
)
from ehr_simulator.ingestion.exceptions import IngestionIssue

# ---------------------------------------------------------------------------
# Connection + PRAGMAs (tests #1, #2, #3)
# ---------------------------------------------------------------------------


def test_connect_applies_pragmas(tmp_db_path: Path) -> None:
    conn = connect(tmp_db_path)
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        synchronous = conn.execute("PRAGMA synchronous").fetchone()[0]
        foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()
    assert journal_mode == "wal"
    assert synchronous == 1
    assert foreign_keys == 1


def test_connect_check_same_thread_false(tmp_db_path: Path) -> None:
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        results: list[object] = []

        def _query() -> None:
            try:
                results.append(conn.execute("SELECT 1").fetchone()[0])
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        t = threading.Thread(target=_query)
        t.start()
        t.join()
    finally:
        conn.close()
    assert results == [1]


@pytest.mark.parametrize(
    "case",
    ["cli_override", "env_var", "study_config", "default"],
)
def test_resolve_db_path_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    monkeypatch.delenv("EHR_SIM_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    class _Study:
        db_path: Path | None = None

    study: _Study | None = None
    cli_override: Path | None = None

    if case == "cli_override":
        cli_override = tmp_path / "cli.db"
    elif case == "env_var":
        env_dir = tmp_path / "env"
        env_dir.mkdir()
        monkeypatch.setenv("EHR_SIM_DB_PATH", "env/env.db")
    elif case == "study_config":
        study = _Study()
        study.db_path = tmp_path / "yaml.db"
    elif case == "default":
        pass

    resolved = resolve_db_path(study, cli_override=cli_override)

    if case == "cli_override":
        assert resolved == tmp_path / "cli.db"
    elif case == "env_var":
        assert resolved == (tmp_path / "env" / "env.db").resolve()
    elif case == "study_config":
        assert resolved == tmp_path / "yaml.db"
    elif case == "default":
        assert resolved == Path("data/ehr_simulator.db")


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("../escape.db", id="parent_traversal"),
        pytest.param("/tmp/elsewhere.db", id="absolute_outside_cwd"),
    ],
)
def test_resolve_db_path_traversal_guard_rejects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EHR_SIM_DB_PATH", bad)
    with pytest.raises(ConfigError):
        resolve_db_path(None)


# ---------------------------------------------------------------------------
# Migrations runner (tests #4, #4b, #5, #6, #7)
# ---------------------------------------------------------------------------


def test_apply_migrations_forward(tmp_db_path: Path) -> None:
    conn = connect(tmp_db_path)
    try:
        versions = apply_migrations(conn)
        rows = conn.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    finally:
        conn.close()
    assert versions == [1]
    assert [(r[0], r[1]) for r in rows] == [(1, "initial")]


def test_apply_migrations_recovers_from_partial_apply(tmp_db_path: Path) -> None:
    """Pre-create only the ``clinicians`` table (simulating a mid-DDL crash
    that didn't reach ``INSERT INTO schema_migrations``). The retry must
    succeed cleanly thanks to ``CREATE TABLE IF NOT EXISTS`` (review-fix R2).
    """
    pre = sqlite3.connect(tmp_db_path)
    pre.executescript(
        "CREATE TABLE clinicians ("
        " clinician_id TEXT PRIMARY KEY,"
        " name_normalized TEXT NOT NULL UNIQUE,"
        " first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);"
    )
    pre.commit()
    pre.close()

    conn = connect(tmp_db_path)
    try:
        versions = apply_migrations(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        migration_rows = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    assert versions == [1]
    expected = {
        "clinicians",
        "sessions",
        "arm_assignments",
        "answers",
        "events",
        "ingestion_issues",
        "schema_migrations",
    }
    assert expected.issubset(tables)
    assert migration_rows == 1


def test_apply_migrations_idempotent(tmp_db_path: Path) -> None:
    structlog.contextvars.clear_contextvars()
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        with capture_logs() as cap:
            versions = apply_migrations(conn)
        rows = conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    finally:
        conn.close()
    assert versions == []
    assert rows == 1
    assert any(entry.get("event_kind") == "db.migrate.noop" for entry in cap)


def test_post_migration_schema_matches_fixture(tmp_db_path: Path) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "db" / "migration_001_expected_schema.sql"
    conn = connect(tmp_db_path)
    try:
        apply_migrations(conn)
        rows = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
    finally:
        conn.close()
    live_ddl = ";\n\n".join(row[0] for row in rows) + ";\n"
    expected_text = fixture_path.read_text(encoding="utf-8")
    expected_ddl = "\n".join(
        line for line in expected_text.splitlines() if not line.startswith("--")
    ).strip()
    expected_ddl = expected_ddl + "\n"
    # Normalize blank-line runs so the fixture file's header comments don't drift.
    while "\n\n\n" in expected_ddl:
        expected_ddl = expected_ddl.replace("\n\n\n", "\n\n")
    assert live_ddl.strip() == expected_ddl.strip()


def test_migrations_constant_shape() -> None:
    assert isinstance(MIGRATIONS, tuple)
    assert len(MIGRATIONS) >= 1
    for i, m in enumerate(MIGRATIONS, start=1):
        assert isinstance(m, Migration)
        assert m.version == i
        assert m.name
        assert m.up_sql.strip()


# ---------------------------------------------------------------------------
# Per-table DAOs (tests #8, #8b, #9, #10, #11, #12, #13, #14, #14b, #15, #15b)
# ---------------------------------------------------------------------------


def test_clinicians_lookup_or_create_normalizes(db: sqlite3.Connection) -> None:
    id_a = clinicians.lookup_or_create(db, "Dr. Smith")
    id_b = clinicians.lookup_or_create(db, "  DR.   SMITH  ")
    id_c = clinicians.lookup_or_create(db, "Dr. Smyth")
    assert id_a == id_b
    assert id_a != id_c
    assert len(id_a) == 16
    int(id_a, 16)  # hex
    rows = db.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0]
    assert rows == 2


def test_clinicians_lookup_or_create_rejects_empty_name(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        clinicians.lookup_or_create(db, "")
    with pytest.raises(ValueError):
        clinicians.lookup_or_create(db, "   ")


def test_clinicians_lookup_or_create_updates_known_cache(
    db: sqlite3.Connection,
) -> None:
    cache: set[str] = set()
    cid = clinicians.lookup_or_create(db, "Dr. Cache", known_clinicians=cache)
    assert cid in cache


def test_sessions_start_or_resume_creates_then_resumes(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    s1 = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    s2 = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert s1 == s2

    db.execute(
        "UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?",
        (s1,),
    )
    db.commit()
    s3 = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert s3 != s1


def test_arm_assignments_phase1_stub_locks_no_ai(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    a1 = arm_assignments.assign_or_lookup(db, cid, "p1", config_hash="h")
    assert a1 == ("no_ai", "phase1_stub")
    a2 = arm_assignments.assign_or_lookup(db, cid, "p1", config_hash="h")
    assert a2 == ("no_ai", "phase1_stub")


def test_arm_assignments_existing_row_not_rewritten(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    arm_assignments.assign_or_lookup(db, cid, "p1", config_hash="h")
    # Simulate S11 swapping the stub to a randomized assigner.
    monkeypatch.setattr(
        arm_assignments,
        "_phase1_stub",
        lambda: ("ai", "phase2_randomized"),
    )
    a2 = arm_assignments.assign_or_lookup(db, cid, "p1", config_hash="h")
    assert a2 == ("no_ai", "phase1_stub")
    row = db.execute(
        "SELECT arm_source, seed FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
        (cid, "p1"),
    ).fetchone()
    assert row[0] == "phase1_stub"
    assert row[1] is None


def test_answers_upsert_idempotent_for_double_submit(db: sqlite3.Connection) -> None:
    """REGRESSION (non-negotiable): double-submit produces 1 row, latest wins."""
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=60.0,
        question_id="q1",
        value="Yes",
        arm="no_ai",
        config_hash="h",
    )
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=60.0,
        question_id="q1",
        value="No",
        arm="no_ai",
        config_hash="h",
    )
    rows = db.execute(
        "SELECT value FROM answers WHERE clinician_id = ? "
        "AND patient_id = ? AND timepoint = ? AND question_id = ?",
        (cid, "p1", 60.0, "q1"),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "No"


def test_answers_upsert_idempotent_for_float_timepoint(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    for value in ("A", "B"):
        answers.upsert(
            db,
            clinician_id=cid,
            patient_id="p1",
            timepoint=60.0,
            question_id="q1",
            value=value,
            arm="no_ai",
            config_hash="h",
        )
    count = db.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
    assert count == 1


def test_answers_upsert_increments_write_counter(db: sqlite3.Connection) -> None:
    class _State:
        write_counter = 0

    state = _State()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=0.0,
        question_id="q1",
        value="x",
        arm="no_ai",
        config_hash="h",
        app_state=state,
    )
    assert state.write_counter == 1


def test_events_append_returns_autoincrement_id(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    ids = [
        events.append(
            db,
            session_id=sid,
            clinician_id=cid,
            patient_id="p1",
            timepoint=0.0,
            kind="panel.swap",
            payload={"b": 2, "a": 1},
        )
        for _ in range(3)
    ]
    assert ids == [1, 2, 3]
    row = db.execute(
        "SELECT payload_json, server_ts, client_ts FROM events WHERE event_id = 1"
    ).fetchone()
    assert row[0] == '{"a":1,"b":2}'
    assert row[1] is not None
    assert row[2] is None


def test_events_append_with_null_session_id(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    event_id = events.append(
        db,
        session_id=None,
        clinician_id=cid,
        patient_id=None,
        timepoint=None,
        kind="clinician.login",
        payload={"name_normalized": "dr. smith"},
    )
    row = db.execute(
        "SELECT session_id, kind FROM events WHERE event_id = ?", (event_id,)
    ).fetchone()
    assert row[0] is None
    assert row[1] == "clinician.login"
    null_rows = db.execute("SELECT COUNT(*) FROM events WHERE session_id IS NULL").fetchone()[0]
    assert null_rows == 1


def test_events_append_increments_write_counter(db: sqlite3.Connection) -> None:
    class _State:
        write_counter = 0

    state = _State()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    events.append(
        db,
        session_id=None,
        clinician_id=cid,
        patient_id=None,
        timepoint=None,
        kind="clinician.login",
        app_state=state,
    )
    assert state.write_counter == 1


def test_ingestion_issues_record_batch_inserts_one_row_per_issue(
    db: sqlite3.Connection,
) -> None:
    issues = [
        IngestionIssue(dataset="synth", patient_id=f"p{i}", row_idx=i, reason=f"r{i}")
        for i in range(3)
    ]
    count = ingestion_issues.record_batch(db, "synth", "boot_a", issues)
    assert count == 3
    rows = db.execute(
        "SELECT dataset, boot_id, patient_id FROM ingestion_issues ORDER BY row_idx"
    ).fetchall()
    assert len(rows) == 3
    for row in rows:
        assert row[0] == "synth"
        assert row[1] == "boot_a"
        assert row[2] is not None


def test_ingestion_issues_boot_id_distinct_across_boots(
    db: sqlite3.Connection,
) -> None:
    issues_a = [IngestionIssue("synth", "p1", 0, "x"), IngestionIssue("synth", "p2", 1, "y")]
    issues_b = [IngestionIssue("synth", "p3", 2, "z"), IngestionIssue("synth", "p4", 3, "w")]
    ingestion_issues.record_batch(db, "synth", "boot_a", issues_a)
    ingestion_issues.record_batch(db, "synth", "boot_b", issues_b)
    total = db.execute("SELECT COUNT(*) FROM ingestion_issues").fetchone()[0]
    per_boot = db.execute(
        "SELECT boot_id, COUNT(*) FROM ingestion_issues GROUP BY boot_id ORDER BY boot_id"
    ).fetchall()
    distinct = db.execute("SELECT COUNT(DISTINCT boot_id) FROM ingestion_issues").fetchone()[0]
    assert total == 4
    assert [(r[0], r[1]) for r in per_boot] == [("boot_a", 2), ("boot_b", 2)]
    assert distinct == 2


def test_foreign_key_constraint_enforced_for_events_session_id(
    db: sqlite3.Connection,
) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    with pytest.raises(DbError):
        events.append(
            db,
            session_id="bogus_session_uuid",
            clinician_id=cid,
            patient_id="p1",
            timepoint=0.0,
            kind="panel.swap",
        )


def test_compute_config_hash_round_trips_through_db(
    db: sqlite3.Connection,
    study_fixture_dir: Path,
) -> None:
    """Insert + read-back a real compute_config_hash result through answers.

    The path-based ``compute_config_hash`` is the canonical source in S6;
    test #29 in test_app.py covers the ``_from_models`` sibling delegate.
    """
    from ehr_simulator.config import compute_config_hash

    h = compute_config_hash(
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
    )
    assert len(h) == 64

    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=0.0,
        question_id="q1",
        value="x",
        arm="no_ai",
        config_hash=h,
    )
    stored = db.execute("SELECT config_hash FROM answers").fetchone()[0]
    assert stored == h
