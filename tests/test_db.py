"""DB layer tests: connection, migrations, per-table DAOs.

Numbering and section headers track ``specs/session-06-sqlite-persistence.md``
§9; tests in this file build up across commits 1 → 2. Commit 1 owns the
connection + migrations slice (tests #1-#7 + #4b); commit 2 layers the
per-table DAOs.

S9a additions (``specs/session-09a-answer-capture.md`` §9 #13-#16c) sit at
the bottom: the ``EventKind`` guard, ``answers.fetch_for_cell`` /
``delete_one``, ``sessions.find_open`` and migration 2's open-session
unique index. S9b (``specs/session-09b-question-gating.md`` §9 #4-#10c)
follows: migration 3 + the ``progress`` DAO, ``sessions.close`` /
``find_latest``, ``answers.delete_after``, the new event kinds.
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
    progress,
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
    assert versions == [1, 2, 3, 4, 5]
    assert [(r[0], r[1]) for r in rows] == [
        (1, "initial"),
        (2, "sessions_open_unique"),
        (3, "progress"),
        (4, "study_identity"),
        (5, "s11b_config_version_history"),
    ]


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
    assert versions == [1, 2, 3, 4, 5]
    expected = {
        "clinicians",
        "sessions",
        "arm_assignments",
        "answers",
        "events",
        "ingestion_issues",
        "progress",
        "schema_migrations",
    }
    assert expected.issubset(tables)
    assert migration_rows == len(MIGRATIONS)


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
    assert rows == len(MIGRATIONS)
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
            kind="session.start",
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
            kind="session.start",
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


# ---------------------------------------------------------------------------
# S9a additions (spec §9 tests #13, #14, #15, #16, #16b, #16c)
# ---------------------------------------------------------------------------


def _cell(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "patient_id": "p1",
        "timepoint": 60.0,
        "question_id": "q1",
    }
    base.update(overrides)
    return base


def test_events_append_rejects_unknown_kind(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    with pytest.raises(ValueError, match="unknown event kind"):
        events.append(
            db,
            session_id=None,
            clinician_id=cid,
            patient_id=None,
            timepoint=None,
            kind="panel.swap",  # type: ignore[arg-type]  (a structlog event_kind, not an events row)
        )
    assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert "answer.upsert" in events.EVENT_KINDS


def test_answers_fetch_for_cell_returns_mapping(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    other = clinicians.lookup_or_create(db, "Dr. Other")
    common = {"arm": "no_ai", "config_hash": "h"}
    answers.upsert(db, clinician_id=cid, value="Yes", **_cell(), **common)
    answers.upsert(db, clinician_id=cid, value="42", **_cell(question_id="q2"), **common)
    # Excluded: other timepoint, other clinician.
    answers.upsert(db, clinician_id=cid, value="No", **_cell(timepoint=180.0), **common)
    answers.upsert(db, clinician_id=other, value="No", **_cell(), **common)

    got = answers.fetch_for_cell(db, clinician_id=cid, patient_id="p1", timepoint=60.0)
    assert {qid: value for qid, (value, _h, _v) in got.items()} == {"q1": "Yes", "q2": "42"}


def test_answers_fetch_for_cell_returns_config_hash(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(db, clinician_id=cid, value="Yes", arm="no_ai", config_hash="old", **_cell())
    got = answers.fetch_for_cell(db, clinician_id=cid, patient_id="p1", timepoint=60.0)
    assert got == {"q1": ("Yes", "old", None)}


def test_answers_delete_one_rowcount_and_write_counter(db: sqlite3.Connection) -> None:
    class _State:
        write_counter = 0

    state = _State()
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(db, clinician_id=cid, value="Yes", arm="no_ai", config_hash="h", **_cell())

    first = answers.delete_one(db, clinician_id=cid, app_state=state, **_cell())
    second = answers.delete_one(db, clinician_id=cid, app_state=state, **_cell())
    assert (first, second) == (1, 0)
    assert state.write_counter == 1
    assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


def test_sessions_find_open(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    assert sessions.find_open(db, cid, "p1") is None

    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert sessions.find_open(db, cid, "p1") == sid

    db.execute("UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ?", (sid,))
    db.commit()
    assert sessions.find_open(db, cid, "p1") is None


def test_migration_2_rejects_second_open_session(tmp_db_path: Path) -> None:
    """Migration 2's partial unique index makes "one open session per pair"
    structural. Also covers the v1 → v2 forward path + idempotency."""
    v1 = connect(tmp_db_path)
    v1.executescript(MIGRATIONS[0].up_sql)
    v1.execute(
        "CREATE TABLE schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    v1.execute("INSERT INTO schema_migrations (version, name) VALUES (1, 'initial')")
    v1.commit()
    assert apply_migrations(v1) == [2, 3, 4, 5]
    assert apply_migrations(v1) == []

    cid = clinicians.lookup_or_create(v1, "Dr. Smith")
    insert = (
        "INSERT INTO sessions (session_id, clinician_id, patient_id, arm, config_hash) "
        "VALUES (?, ?, 'p1', 'no_ai', 'h')"
    )
    v1.execute(insert, ("s1", cid))
    with pytest.raises(sqlite3.IntegrityError):
        v1.execute(insert, ("s2", cid))

    v1.execute("UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = 's1'")
    v1.execute(insert, ("s3", cid))
    v1.commit()
    open_rows = v1.execute("SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL").fetchone()[0]
    v1.close()
    assert open_rows == 1


# ---------------------------------------------------------------------------
# S9b: migration 3 + progress DAO + sessions.close/find_latest (#4-#10c)
# ---------------------------------------------------------------------------


class _State:
    """Minimal ``app_state`` stand-in: the DAOs only touch ``write_counter``."""

    write_counter = 0


def _count_events(db: sqlite3.Connection, kind: str) -> int:
    return db.execute("SELECT COUNT(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0]


def _v2_db(tmp_db_path: Path) -> sqlite3.Connection:
    """A DB at schema version 2 (S9a state) with the runner table in place."""
    conn = connect(tmp_db_path)
    conn.executescript(MIGRATIONS[0].up_sql)
    conn.executescript(MIGRATIONS[1].up_sql)
    conn.execute(
        "CREATE TABLE schema_migrations ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (1, 'initial')")
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (2, 'sessions_open_unique')")
    conn.commit()
    return conn


def test_migration_3_creates_progress_and_is_idempotent(tmp_db_path: Path) -> None:
    v2 = _v2_db(tmp_db_path)
    assert apply_migrations(v2) == [3, 4, 5]
    assert apply_migrations(v2) == []
    tables = {r[0] for r in v2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "progress" in tables

    cid = clinicians.lookup_or_create(v2, "Dr. Smith")
    insert = (
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
        "VALUES (?, 'p1', 0, 'h')"
    )
    v2.execute(insert, (cid,))
    with pytest.raises(sqlite3.IntegrityError):
        v2.execute(insert, (cid,))
    v2.close()


def test_progress_fetch_none_when_absent(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    assert progress.fetch(db, clinician_id=cid, patient_id="p1") is None
    assert progress.list_for_clinician(db, cid) == {}


def test_progress_unlock_upserts_and_is_monotonic(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    state = _State()

    assert progress.unlock(
        db,
        clinician_id=cid,
        patient_id="p1",
        from_t_index=0,
        to_t_index=1,
        config_hash="h1",
        app_state=state,
    )
    row = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert row is not None
    assert (row.unlocked_t_index, row.completed_at, row.config_hash) == (1, None, "h1")
    assert state.write_counter == 1

    assert progress.unlock(
        db,
        clinician_id=cid,
        patient_id="p1",
        from_t_index=1,
        to_t_index=2,
        config_hash="h1",
        app_state=state,
    )
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 2
    assert state.write_counter == 2


def test_progress_unlock_preserves_original_config_hash(db: sqlite3.Connection) -> None:
    """review-fix R11: the row records the hash the walk started under."""
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="h1"
    )
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=1, to_t_index=2, config_hash="h2"
    )
    row = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert (row.unlocked_t_index, row.config_hash) == (2, "h1")


def test_progress_unlock_compare_and_set_rejects_stale_from_index(db: sqlite3.Connection) -> None:
    """review-fix R29: the frontier guard lives in SQL."""
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    state = _State()
    kwargs = {"clinician_id": cid, "patient_id": "p1", "config_hash": "h", "app_state": state}

    assert progress.unlock(db, from_t_index=0, to_t_index=1, **kwargs) is True
    assert progress.unlock(db, from_t_index=0, to_t_index=1, **kwargs) is False  # replay
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    assert progress.unlock(db, from_t_index=1, to_t_index=2, **kwargs) is True
    assert progress.unlock(db, from_t_index=0, to_t_index=1, **kwargs) is False  # stale
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 2
    # no row yet + from != 0 → nothing to move
    assert (
        progress.unlock(
            db,
            from_t_index=1,
            to_t_index=2,
            clinician_id=cid,
            patient_id="p2",
            config_hash="h",
            app_state=state,
        )
        is False
    )
    assert state.write_counter == 2


def test_progress_mark_complete_sets_completed_at_once(db: sqlite3.Connection) -> None:
    from datetime import datetime

    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=2, config_hash="h"
    )
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=2, config_hash="h"
    )
    first = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert isinstance(first.completed_at, datetime)
    assert first.unlocked_t_index == 2

    db.execute("UPDATE progress SET completed_at = '2020-01-01 00:00:00' WHERE patient_id = 'p1'")
    db.commit()
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=2, config_hash="h"
    )
    second = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert second.completed_at == datetime(2020, 1, 1)

    # mark_complete on a pair with no row still inserts one (defensive).
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p9", unlocked_t_index=0, config_hash="h"
    )
    assert progress.fetch(db, clinician_id=cid, patient_id="p9").completed_at is not None


def test_progress_reset_rewinds_and_clears_completed(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    progress.unlock(
        db, clinician_id=cid, patient_id="p1", from_t_index=0, to_t_index=2, config_hash="h"
    )
    progress.mark_complete(
        db, clinician_id=cid, patient_id="p1", unlocked_t_index=2, config_hash="h"
    )

    assert progress.reset(db, clinician_id=cid, patient_id="p1", to_t_index=0) == 1
    row = progress.fetch(db, clinician_id=cid, patient_id="p1")
    assert (row.unlocked_t_index, row.completed_at) == (0, None)
    assert progress.reset(db, clinician_id=cid, patient_id="nope", to_t_index=0) == 0


def test_progress_list_for_clinician(db: sqlite3.Connection) -> None:
    a = clinicians.lookup_or_create(db, "Dr. A")
    b = clinicians.lookup_or_create(db, "Dr. B")
    progress.unlock(
        db, clinician_id=a, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="h"
    )
    progress.unlock(
        db, clinician_id=a, patient_id="p2", from_t_index=0, to_t_index=2, config_hash="h"
    )
    progress.unlock(
        db, clinician_id=b, patient_id="p1", from_t_index=0, to_t_index=1, config_hash="h"
    )

    listed_a = progress.list_for_clinician(db, a)
    assert set(listed_a) == {"p1", "p2"}
    assert listed_a["p2"].unlocked_t_index == 2
    assert set(progress.list_for_clinician(db, b)) == {"p1"}


def test_answers_delete_after_keeps_boundary(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    state = _State()
    for t in (0.0, 60.0, 180.0):
        answers.upsert(
            db,
            clinician_id=cid,
            patient_id="p1",
            timepoint=t,
            question_id="q",
            value="v",
            arm="no_ai",
            config_hash="h",
        )
    deleted = answers.delete_after(
        db, clinician_id=cid, patient_id="p1", min_timepoint_exclusive=60.0, app_state=state
    )
    assert deleted == 1
    left = sorted(r[0] for r in db.execute("SELECT timepoint FROM answers"))
    assert left == [0.0, 60.0]
    assert state.write_counter == 1
    assert (
        answers.delete_after(
            db, clinician_id=cid, patient_id="p1", min_timepoint_exclusive=60.0, app_state=state
        )
        == 0
    )
    assert state.write_counter == 1


def test_sessions_close_sets_ended_at_and_frees_pair(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")

    assert sessions.close(db, sid) == 1
    assert sessions.find_open(db, cid, "p1") is None
    ended = db.execute("SELECT ended_at FROM sessions WHERE session_id = ?", (sid,)).fetchone()[0]
    assert ended is not None

    new_sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert new_sid != sid
    assert sessions.close(db, sid) == 0


def test_sessions_find_latest_returns_most_recent_open_or_closed(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    other = clinicians.lookup_or_create(db, "Dr. Other")
    assert sessions.find_latest(db, cid, "p1") is None

    first = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert sessions.find_latest(db, cid, "p1") == first
    sessions.close(db, first)
    assert sessions.find_latest(db, cid, "p1") == first

    sessions.start_or_resume(db, other, "p1", arm="no_ai", config_hash="h")
    second = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert sessions.find_latest(db, cid, "p1") == second


def test_events_kind_taxonomy_includes_s9b_kinds(db: sqlite3.Connection) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    new_kinds = ("advance.ok", "advance.blocked", "session.end", "progress.reset")
    assert set(new_kinds) <= events.EVENT_KINDS
    for kind in new_kinds:
        events.append(
            db,
            session_id=None,
            clinician_id=cid,
            patient_id="p1",
            timepoint=None,
            kind=kind,
            payload={},
        )  # type: ignore[arg-type]
    n = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    assert n == len(new_kinds)


# ---------------------------------------------------------------------------
# S9c (spec tests 63-68): export read paths — DAO fetch_all/fetch_by_ids and
# read-only connections.
# ---------------------------------------------------------------------------

from ehr_simulator.db import AccessMode  # noqa: E402

S9C_HASH = "c" * 64


def test_answers_fetch_all_deterministic_typed_rows(db) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p2",
        timepoint=60.0,
        question_id="zeta",
        value="v",
        arm="no_ai",
        config_hash=S9C_HASH,
    )
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=60.0,
        question_id="alpha",
        value="v",
        arm="no_ai",
        config_hash=S9C_HASH,
    )
    answers.upsert(
        db,
        clinician_id=cid,
        patient_id="p1",
        timepoint=0.0,
        question_id="zeta",
        value="v",
        arm="no_ai",
        config_hash=S9C_HASH,
    )
    rows = answers.fetch_all(db)
    assert isinstance(rows, tuple)
    assert all(isinstance(r, answers.AnswerRow) for r in rows)
    assert [(r.patient_id, r.timepoint, r.question_id) for r in rows] == [
        ("p1", 0.0, "zeta"),
        ("p1", 60.0, "alpha"),
        ("p2", 60.0, "zeta"),
    ]
    assert all(isinstance(r.timepoint, float) for r in rows)


def test_progress_fetch_all_keyed_by_pair(db) -> None:
    a = clinicians.lookup_or_create(db, "Dr. Alpha")
    b = clinicians.lookup_or_create(db, "Dr. Beta")
    progress.unlock(
        db, clinician_id=a, patient_id="p2", from_t_index=0, to_t_index=1, config_hash=S9C_HASH
    )
    progress.unlock(
        db, clinician_id=b, patient_id="p1", from_t_index=0, to_t_index=1, config_hash=S9C_HASH
    )
    progress.mark_complete(
        db,
        clinician_id=b,
        patient_id="p1",
        unlocked_t_index=1,
        config_hash=S9C_HASH,
    )
    rows = progress.fetch_all(db)
    assert list(rows) == [(a, "p2"), (b, "p1")]
    assert all(isinstance(row.unlocked_t_index, int) for row in rows.values())
    assert rows[(a, "p2")].completed_at is None
    assert rows[(b, "p1")].completed_at is not None


def test_arm_assignments_fetch_all_returns_arm_source_and_hash(db) -> None:
    cid = clinicians.lookup_or_create(db, "Dr. Smith")
    arm_assignments.assign_or_lookup(db, cid, "p1", config_hash=S9C_HASH)
    rows = arm_assignments.fetch_all(db)
    assert len(rows) == 1
    row = rows[0]
    assert (row.clinician_id, row.patient_id) == (cid, "p1")
    assert row.arm == "no_ai"
    assert row.arm_source == "phase1_stub"
    assert row.config_hash == S9C_HASH


def test_clinicians_fetch_by_ids_only_requested_in_deterministic_order(db) -> None:
    a = clinicians.lookup_or_create(db, "Dr. Alpha")
    b = clinicians.lookup_or_create(db, "Dr. Beta")
    clinicians.lookup_or_create(db, "Dr. Not requested")
    result = clinicians.fetch_by_ids(db, [b, a, a, "f" * 16])
    assert [row[0] for row in result] == sorted({a, b})
    assert all(row[1] for row in result)


def test_read_only_connection_rejects_insert(db, tmp_db_path: Path) -> None:
    ro = connect(tmp_db_path, access=AccessMode.READ_ONLY)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro.execute(
                "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
                ("f" * 16, "forbidden"),
            )
    finally:
        ro.close()
    n = db.execute(
        "SELECT COUNT(*) FROM clinicians WHERE clinician_id = ?", ("f" * 16,)
    ).fetchone()[0]
    assert n == 0


def test_read_only_connect_refuses_missing_db(tmp_path: Path) -> None:
    missing = tmp_path / "sub" / "nope.db"
    with pytest.raises(FileNotFoundError):
        connect(missing, access=AccessMode.READ_ONLY)
    assert not missing.exists()


# ---------------------------------------------------------------------------
# S10 atomicity (specs/session10.md §2/§3; s10_fixes.md fix 1): the DAOs gain
# ``commit=False`` so web/gating batches the state write and the behavioral
# events of one advance into a single atomic transaction — either all commit
# or all roll back together, and neither bumps ``write_counter`` on its own.
# ---------------------------------------------------------------------------


def test_unlock_commit_false_rolls_back_atomically(db: sqlite3.Connection) -> None:
    """The advance path: ``unlock`` + the exit event ride one transaction.

    A rollback retracts the frontier move together with the ``timepoint.exit``
    (the core S10 guarantee — no advanced frontier without its exit), and the
    deferred writes never bump ``write_counter``.
    """
    cid = clinicians.lookup_or_create(db, "Dr. Atomic")
    state = _State()
    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")

    assert (
        progress.unlock(
            db,
            clinician_id=cid,
            patient_id="p1",
            from_t_index=0,
            to_t_index=1,
            config_hash="h",
            app_state=state,
            commit=False,
        )
        is True
    )
    # Same transaction is visible to this connection, but nothing is durable,
    # and the DAO deferred its commit/bump to the caller.
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    assert state.write_counter == 0

    events.append(
        db,
        session_id=sid,
        clinician_id=cid,
        patient_id="p1",
        timepoint=60.0,
        kind="timepoint.exit",
        payload={"t_index": 0, "reason": "advance"},
        app_state=state,
        commit=False,
    )
    assert state.write_counter == 0
    exits = _count_events(db, "timepoint.exit")
    assert exits == 1

    db.rollback()
    # Both the frontier move and the exit event rolled back together.
    assert progress.fetch(db, clinician_id=cid, patient_id="p1") is None
    assert _count_events(db, "timepoint.exit") == 0
    assert state.write_counter == 0


def test_finish_path_commit_false_rolls_back_state_and_events_together(
    db: sqlite3.Connection,
) -> None:
    """The terminal path: mark_complete + close + the final events are atomic.

    If the final ``timepoint.exit`` (or any event) fails, the completion,
    the session close and the emitted events all retract at once — the
    session is re-opened, ``completed_at`` is cleared, no exit lingers.
    """
    cid = clinicians.lookup_or_create(db, "Dr. Finish")
    state = _State()
    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    # Advance to the final timepoint (committed defaults).
    progress.unlock(
        db,
        clinician_id=cid,
        patient_id="p1",
        from_t_index=0,
        to_t_index=1,
        config_hash="h",
    )
    progress.unlock(
        db,
        clinician_id=cid,
        patient_id="p1",
        from_t_index=1,
        to_t_index=2,
        config_hash="h",
    )

    # Terminal advance: everything deferred.
    progress.mark_complete(
        db,
        clinician_id=cid,
        patient_id="p1",
        unlocked_t_index=2,
        config_hash="h",
        app_state=state,
        commit=False,
    )
    sessions.close(db, sid, commit=False)
    events.append(
        db,
        session_id=sid,
        clinician_id=cid,
        patient_id="p1",
        timepoint=180.0,
        kind="advance.ok",
        payload={"final": True},
        app_state=state,
        commit=False,
    )
    events.append(
        db,
        session_id=sid,
        clinician_id=cid,
        patient_id="p1",
        timepoint=180.0,
        kind="timepoint.exit",
        payload={"t_index": 2, "reason": "finish"},
        app_state=state,
        commit=False,
    )
    events.append(
        db,
        session_id=sid,
        clinician_id=cid,
        patient_id="p1",
        timepoint=None,
        kind="session.end",
        payload={"reason": "patient_complete"},
        app_state=state,
        commit=False,
    )
    assert state.write_counter == 0  # deferred — the caller owns the single bump

    # Within one transaction the clinician sees it all…
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").completed_at is not None
    assert sessions.find_open(db, cid, "p1") is None
    assert _count_events(db, "timepoint.exit") == 1

    # …but a failed event retracts the whole block.
    db.rollback()
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").completed_at is None
    assert sessions.find_open(db, cid, "p1") == sid  # session re-opened
    assert _count_events(db, "timepoint.exit") == 0
    assert _count_events(db, "advance.ok") == 0
    assert state.write_counter == 0


def test_atomic_block_persists_and_bumps_exactly_once_when_caller_commits(
    db: sqlite3.Connection,
) -> None:
    """Happy path: the caller's single commit makes the batch durable, and the
    caller (not the deferred DAOs) owns the one ``write_counter`` bump."""
    cid = clinicians.lookup_or_create(db, "Dr. Commit")
    state = _State()
    sid = sessions.start_or_resume(db, cid, "p1", arm="no_ai", config_hash="h")
    assert (
        progress.unlock(
            db,
            clinician_id=cid,
            patient_id="p1",
            from_t_index=0,
            to_t_index=1,
            config_hash="h",
            app_state=state,
            commit=False,
        )
        is True
    )
    events.append(
        db,
        session_id=sid,
        clinician_id=cid,
        patient_id="p1",
        timepoint=60.0,
        kind="timepoint.exit",
        payload={"t_index": 0, "reason": "advance"},
        app_state=state,
        commit=False,
    )
    db.commit()
    state.write_counter += 1
    assert state.write_counter == 1
    assert progress.fetch(db, clinician_id=cid, patient_id="p1").unlocked_t_index == 1
    assert _count_events(db, "timepoint.exit") == 1


# ---------------------------------------------------------------------------
# S11a: study identity (spec tests 8-17)
# ---------------------------------------------------------------------------


def test_resolve_db_path_study_default_is_study_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EHR_SIM_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    class _Study:
        db_path = None
        study_id = "my_study"

    assert resolve_db_path(_Study()) == Path("data/study_my_study.db")


def test_migration_4_creates_exactly_study_identity_table(tmp_db_path: Path) -> None:
    """Spec test 11: migration 4 adds only the singleton identity table,
    never populates it, and is safe to re-apply."""
    conn = connect(tmp_db_path)
    try:
        assert apply_migrations(conn) == [1, 2, 3, 4, 5]
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if not r[0].startswith("sqlite_")
        }
        assert "study_identity" in tables
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='study_identity'"
        ).fetchone()[0]
        assert "singleton" in ddl and "CHECK(singleton=1)" in ddl.replace(" ", "")
        assert "study_id" in ddl and "created_at" in ddl.lower()
        # Never guessed or populated a study_id:
        assert conn.execute("SELECT COUNT(*) FROM study_identity").fetchone()[0] == 0
        # Idempotent re-apply:
        assert apply_migrations(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM study_identity").fetchone()[0] == 0
    finally:
        conn.close()


def test_study_id_patterns_in_lockstep() -> None:
    """The config layer and the DB layer duplicate STUDY_ID_PATTERN on
    purpose (db must not import config) — pin them in lockstep."""
    from ehr_simulator.config.study import STUDY_ID_PATTERN as CONFIG_PATTERN
    from ehr_simulator.db import study_identity

    assert study_identity.STUDY_ID_PATTERN.pattern == CONFIG_PATTERN.pattern


class TestStudyIdentityDao:
    """Spec tests 12-16: bind / require / fetch / has_persistent_data."""

    def _migrated(self, tmp_db_path: Path) -> sqlite3.Connection:
        conn = connect(tmp_db_path)
        apply_migrations(conn)
        return conn

    def test_fetch_returns_none_when_unbound(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            assert si.fetch(conn) is None
            assert si.has_persistent_data(conn) is False
        finally:
            conn.close()

    def test_bind_writes_identity_to_empty_migrated_db(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            si.bind(conn, "alpha")
            assert si.fetch(conn) == "alpha"
            # Singleton: exactly one row, keyed 1.
            rows = conn.execute("SELECT singleton, study_id FROM study_identity").fetchall()
            assert [(r["singleton"], r["study_id"]) for r in rows] == [(1, "alpha")]
        finally:
            conn.close()

    def test_rebind_same_study_is_noop(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            si.bind(conn, "alpha")
            row = conn.execute("SELECT created_at FROM study_identity").fetchone()
            si.bind(conn, "alpha")
            assert conn.execute("SELECT COUNT(*) FROM study_identity").fetchone()[0] == 1
            assert conn.execute("SELECT created_at FROM study_identity").fetchone() == row
            assert si.fetch(conn) == "alpha"
        finally:
            conn.close()

    def test_bind_different_study_refused_and_unchanged(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import exceptions
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            si.bind(conn, "alpha")
            with pytest.raises(exceptions.StudyIdentityError) as exc:
                si.bind(conn, "beta")
            # Both identities named; stored identity unchanged.
            msg = str(exc.value)
            assert "alpha" in msg and "beta" in msg
            assert si.fetch(conn) == "alpha"
        finally:
            conn.close()

    def test_bind_nonempty_unbound_db_refused(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import exceptions
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            # One application row is enough to count as nonempty.
            conn.execute(
                "INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c1', 'dr')"
            )
            conn.commit()
            assert si.has_persistent_data(conn) is True
            with pytest.raises(exceptions.StudyIdentityError):
                si.bind(conn, "alpha")
            assert si.fetch(conn) is None
            assert si.has_persistent_data(conn) is True
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "table,sql",
        [
            (
                "clinicians",
                "INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c1', 'dr')",
            ),
            (
                "sessions",
                "INSERT INTO sessions (session_id, clinician_id, patient_id, arm, config_hash) "
                "VALUES ('s1', 'c1', 'p1', 'no_ai', 'h')",
            ),
            (
                "answers",
                "INSERT INTO answers (clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "  # noqa: E501
                "VALUES ('c1', 'p1', 0.0, 'q1', 'Yes', 'no_ai', 'h')",
            ),
        ],
    )
    def test_nonempty_detection_covers_application_tables(
        self, tmp_db_path: Path, table: str, sql: str
    ) -> None:
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            # Satisfy the FK from the parameterized table to clinicians.
            if table != "clinicians":
                conn.execute(
                    "INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c1', 'dr')"
                )
            conn.execute(sql)
            conn.commit()
            # schema_migrations rows alone do NOT count...
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 5
            # ...but a row in any application table does:
            assert si.has_persistent_data(conn) is True
        finally:
            conn.close()

    def test_require_matches_succeeds_without_writing(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            si.bind(conn, "alpha")
            # require is pure: no row changes, no new rows.
            before = conn.execute("SELECT COUNT(*) FROM study_identity").fetchone()[0]
            si.require(conn, "alpha")
            assert conn.execute("SELECT COUNT(*) FROM study_identity").fetchone()[0] == before
        finally:
            conn.close()

    def test_require_refuses_unbound_database(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import exceptions
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            with pytest.raises(exceptions.StudyIdentityError):
                si.require(conn, "alpha")
            assert si.fetch(conn) is None  # no write performed
        finally:
            conn.close()

    def test_require_refuses_mismatch(self, tmp_db_path: Path) -> None:
        from ehr_simulator.db import exceptions
        from ehr_simulator.db import study_identity as si

        conn = self._migrated(tmp_db_path)
        try:
            si.bind(conn, "alpha")
            with pytest.raises(exceptions.StudyIdentityError) as exc:
                si.require(conn, "beta")
            assert "alpha" in str(exc.value) and "beta" in str(exc.value)
        finally:
            conn.close()

    @pytest.mark.parametrize(
        "good",
        ["a", "z9", "my-study_2", "a" * 64, "abc_def-ghi"],
    )
    def test_validate_study_id_accepts(self, good: str) -> None:
        from ehr_simulator.db import study_identity as si

        assert si.validate_study_id(good) == good

    @pytest.mark.parametrize(
        "bad",
        ["UPPER", "has space", "seg/a", "a" * 65, "-lead", "_lead", "dot.id", "", 123, None],
    )
    def test_validate_study_id_rejects(self, bad: object, tmp_db_path: Path) -> None:
        """Invalid ids are refused by the validator AND by bind (before it
        touches the connection)."""
        from ehr_simulator.db import exceptions
        from ehr_simulator.db import study_identity as si

        with pytest.raises(exceptions.StudyIdentityError):
            si.validate_study_id(bad)
        conn = self._migrated(tmp_db_path)
        try:
            with pytest.raises(exceptions.StudyIdentityError):
                si.bind(conn, bad)
            assert si.fetch(conn) is None
        finally:
            conn.close()
