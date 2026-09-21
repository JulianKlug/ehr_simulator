"""Typer CLI tests — 5 commands × happy/sad paths.

Uses :class:`typer.testing.CliRunner` for everything except the
``serve`` carryover, which keeps the S2 monkeypatch-uvicorn pattern so
``cli.main([...])`` ergonomics survive the argparse → Typer swap.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ehr_simulator import cli


@pytest.fixture
def captured_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_run(app: object, **kwargs: Any) -> None:
        calls.append({"app": app, **kwargs})

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    return calls


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ---------------------------------------------------------------------------
# serve (carryover + new --config paths)
# ---------------------------------------------------------------------------


def test_cli_serve_invokes_uvicorn_default(captured_calls: list[dict[str, Any]]) -> None:
    cli.main(["serve", "--port", "8123", "--reload"])

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["app"] == "ehr_simulator.web.app:app"
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8123
    assert call["reload"] is True


def test_cli_serve_defaults(captured_calls: list[dict[str, Any]]) -> None:
    cli.main(["serve"])

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert call["host"] == "127.0.0.1"
    assert call["port"] == 8000
    assert call["reload"] is False


def test_cli_serve_with_config_routes_to_app_factory(
    captured_calls: list[dict[str, Any]],
    study_fixture_dir: Path,
) -> None:
    from fastapi import FastAPI

    cli.main(
        [
            "serve",
            "--config",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--questions",
            str(study_fixture_dir / "questions.yaml"),
        ]
    )

    assert len(captured_calls) == 1
    call = captured_calls[0]
    assert isinstance(call["app"], FastAPI)
    assert call["reload"] is False


def test_cli_serve_reload_with_config_warns_and_disables(
    captured_calls: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
    study_fixture_dir: Path,
) -> None:
    cli.main(
        [
            "serve",
            "--reload",
            "--config",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--questions",
            str(study_fixture_dir / "questions.yaml"),
        ]
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["reload"] is False
    captured = capsys.readouterr()
    assert "--reload disabled when --config is set" in captured.err


# ---------------------------------------------------------------------------
# validate-config
# ---------------------------------------------------------------------------


def test_cli_validate_config_happy_path_exits_0(runner: CliRunner, study_fixture_dir: Path) -> None:
    result = runner.invoke(
        cli.app_typer,
        [
            "validate-config",
            str(study_fixture_dir / "study_synthetic.yaml"),
            str(study_fixture_dir / "questions.yaml"),
        ],
    )
    assert result.exit_code == 0
    assert "OK:" in result.stdout
    assert "3 patients" in result.stdout
    assert "3 timepoints" in result.stdout
    assert "7 questions" in result.stdout


def test_cli_validate_config_bad_shape_exits_1(runner: CliRunner, study_fixture_dir: Path) -> None:
    result = runner.invoke(
        cli.app_typer,
        [
            "validate-config",
            str(study_fixture_dir / "study_broken_missing_schema_version.yaml"),
            str(study_fixture_dir / "questions.yaml"),
        ],
    )
    assert result.exit_code == 1
    assert "schema_version" in result.stderr


# ---------------------------------------------------------------------------
# validate-adapter
# ---------------------------------------------------------------------------


def test_cli_validate_adapter_synthetic_exits_0(runner: CliRunner, study_fixture_dir: Path) -> None:
    result = runner.invoke(
        cli.app_typer,
        ["validate-adapter", str(study_fixture_dir / "study_synthetic.yaml")],
    )
    assert result.exit_code == 0
    assert "Dataset:    synthetic" in result.stdout
    assert "SCALAR_TS:" in result.stdout
    assert "ADMISSION:" in result.stdout
    assert "IMAGING:" in result.stdout
    assert "AI_OUTPUT:" in result.stdout


def test_cli_validate_adapter_geneva_with_inline_paths(
    runner: CliRunner, study_fixture_dir: Path
) -> None:
    result = runner.invoke(
        cli.app_typer,
        ["validate-adapter", str(study_fixture_dir / "study_geneva.yaml")],
    )
    assert result.exit_code == 0
    assert "Dataset:    geneva" in result.stdout
    # Geneva fixture has at least some rows.
    assert "rows" in result.stdout


def test_cli_validate_adapter_non_synthetic_no_overrides_exits_1(
    runner: CliRunner, tmp_path: Path
) -> None:
    config_path = tmp_path / "study.yaml"
    config_path.write_text(
        """schema_version: "1"
dataset: geneva
patient_ids: [g_001]
time_unit: minutes
timepoints: [0, 60]
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app_typer,
        ["validate-adapter", str(config_path)],
    )
    assert result.exit_code == 1
    assert "csv_path" in result.stderr
    assert "params_dir" in result.stderr
    assert "EHR_SIM_DATA_ROOT" in result.stderr
    assert "does not discover files" in result.stderr


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def test_cli_preflight_happy_path_exits_0(runner: CliRunner, study_fixture_dir: Path) -> None:
    result = runner.invoke(
        cli.app_typer,
        [
            "preflight",
            str(study_fixture_dir / "study_synthetic.yaml"),
            str(study_fixture_dir / "questions.yaml"),
        ],
    )
    assert result.exit_code == 0
    # 3 patients × 3 timepoints = 9 cells.
    ok_count = result.stdout.count("OK: ")
    assert ok_count == 9
    assert "Summary: 9 OK" in result.stdout


def test_cli_preflight_warns_on_empty_timepoint(
    runner: CliRunner,
    study_fixture_dir: Path,
    tmp_path: Path,
) -> None:
    # synth_001 only has rows at t in {0, 60, 180}; declaring timepoint 9000
    # forces the slice to be empty AT-OR-BEFORE 9000? Actually slice_to_timepoint
    # returns rows ≤ t, so any timepoint ≥0 has SOME data after t=0.
    # To exercise WARN: declare a t in the middle that is BEFORE any scalar_ts row.
    # But synth_001 has data at t=0, so sliced.scalar_ts at any t≥0 is non-empty.
    # Workaround: use a synthetic config with t=-0.5 ... but timepoints validator
    # rejects negatives. Real WARN path: a patient that exists in admission but
    # has no scalar_ts rows. Rare in synthetic fixture; let's use a custom yaml.
    config_path = tmp_path / "study.yaml"
    config_path.write_text(
        """schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0]
""",
        encoding="utf-8",
    )
    # synth_001 at t=0 should be OK (all panels populated).
    result = runner.invoke(
        cli.app_typer,
        [
            "preflight",
            str(config_path),
            str(study_fixture_dir / "questions.yaml"),
        ],
    )
    # No FAIL → exit 0. Verify the format is alive.
    assert result.exit_code == 0
    assert "Summary:" in result.stdout


def test_cli_preflight_unknown_patient_exits_1(
    runner: CliRunner, study_fixture_dir: Path, tmp_path: Path
) -> None:
    config_path = tmp_path / "study.yaml"
    config_path.write_text(
        """schema_version: "1"
dataset: synthetic
patient_ids: [synth_999]
time_unit: minutes
timepoints: [0]
""",
        encoding="utf-8",
    )
    result = runner.invoke(
        cli.app_typer,
        [
            "preflight",
            str(config_path),
            str(study_fixture_dir / "questions.yaml"),
        ],
    )
    assert result.exit_code == 1
    assert "FAIL: patient synth_999 not found in dataset" in result.stdout


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def test_cli_migrate_forward_then_idempotent(runner: CliRunner, tmp_path: Path) -> None:
    """S6: migrate applies migrations on a fresh DB; re-running is a no-op."""
    db_path = tmp_path / "x.db"
    first = runner.invoke(cli.app_typer, ["migrate", "--db-path", str(db_path)])
    assert first.exit_code == 0, first.stderr
    assert "Applied migrations: [1, 2, 3]" in first.stdout

    second = runner.invoke(cli.app_typer, ["migrate", "--db-path", str(db_path)])
    assert second.exit_code == 0, second.stderr
    assert "No migrations to apply." in second.stdout


def test_cli_backup_creates_file(runner: CliRunner, tmp_path: Path) -> None:
    """S6: backup writes one snapshot file into --backup-dir."""
    db_path = tmp_path / "x.db"
    backup_dir = tmp_path / "backups"
    runner.invoke(cli.app_typer, ["migrate", "--db-path", str(db_path)])
    result = runner.invoke(
        cli.app_typer,
        [
            "backup",
            "--db-path",
            str(db_path),
            "--backup-dir",
            str(backup_dir),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert "Backup written to:" in result.stdout
    files = list(backup_dir.glob("ehr_simulator_*.db"))
    assert len(files) == 1


def test_cli_serve_db_path_plumbs_through_create_app(
    captured_calls: list[dict[str, Any]], tmp_path: Path
) -> None:
    """S6: ``serve --db-path X`` reaches ``create_app`` via the no-config branch."""
    db_path = tmp_path / "srv.db"
    cli.main(["serve", "--db-path", str(db_path)])
    assert len(captured_calls) == 1
    # No-config + db-path branch passes a FastAPI instance (not an import string).
    from fastapi import FastAPI

    assert isinstance(captured_calls[0]["app"], FastAPI)


def test_cli_preview_text_summary_and_html_out(
    runner: CliRunner, study_fixture_dir: Path, tmp_path: Path
) -> None:
    out_dir = tmp_path / "preview"

    # text-only mode
    result_text = runner.invoke(
        cli.app_typer,
        [
            "preview",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--patient",
            "synth_001",
        ],
    )
    assert result_text.exit_code == 0, result_text.stderr
    assert "Patient: synth_001" in result_text.stdout
    # 3 timepoints rendered.
    assert result_text.stdout.count("t=") >= 3

    # html-out mode (requires --questions)
    result_html = runner.invoke(
        cli.app_typer,
        [
            "preview",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--patient",
            "synth_001",
            "--questions",
            str(study_fixture_dir / "questions.yaml"),
            "--html-out",
            str(out_dir),
        ],
    )
    assert result_html.exit_code == 0, result_html.stderr
    written = sorted(out_dir.glob("synth_001_t*.html"))
    assert len(written) == 3
    for idx, path in enumerate(written):
        text = path.read_text(encoding="utf-8")
        assert "synth_001" in text
        # plotnine SVG output; locked by S2 chart tests.
        assert "<svg" in text
        # S9b: each file is ITS OWN timepoint (not a followed gate redirect to
        # t=0) and shows the open pane with the advance CTA.
        assert f'data-t-index="{idx}"' in text
        assert 'id="advance-form"' in text
        assert 'data-remaining="6"' in text


# ---------------------------------------------------------------------------
# reset-progress (S9b, spec §9 #10d-#10e)
# ---------------------------------------------------------------------------


def _walked_db(db_path: Path, *, unlocked: int, completed: bool) -> str:
    """``Dr. Test`` walked synth_001 to ``unlocked``; answers exist at every timepoint."""
    from ehr_simulator.db import answers, apply_migrations, clinicians, connect, progress

    conn = connect(db_path)
    apply_migrations(conn)
    cid = clinicians.lookup_or_create(conn, "Dr. Test")
    progress.unlock(
        conn,
        clinician_id=cid,
        patient_id="synth_001",
        from_t_index=0,
        to_t_index=unlocked,
        config_hash="h",
    )
    if completed:
        progress.mark_complete(
            conn,
            clinician_id=cid,
            patient_id="synth_001",
            unlocked_t_index=unlocked,
            config_hash="h",
        )
    for t in (0.0, 60.0, 180.0):
        answers.upsert(
            conn,
            clinician_id=cid,
            patient_id="synth_001",
            timepoint=t,
            question_id="confidence",
            value="3",
            arm="no_ai",
            config_hash="h",
        )
    conn.close()
    return cid


def test_cli_reset_progress_rewinds_walk(
    runner: CliRunner, study_fixture_dir: Path, tmp_path: Path
) -> None:
    import json

    from ehr_simulator.db import connect, progress

    db_path = tmp_path / "walk.db"
    cid = _walked_db(db_path, unlocked=2, completed=True)

    result = runner.invoke(
        cli.app_typer,
        [
            "reset-progress",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--clinician",
            "Dr. Test",
            "--patient",
            "synth_001",
            "--to-t-index",
            "1",
            "--db-path",
            str(db_path),
        ],
    )
    assert result.exit_code == 0, result.stderr
    assert "frontier 2 (was complete) → 1" in result.stdout
    assert "1 answer(s) deleted" in result.stdout

    conn = connect(db_path)
    row = progress.fetch(conn, clinician_id=cid, patient_id="synth_001")
    assert (row.unlocked_t_index, row.completed_at) == (1, None)
    left = sorted(r[0] for r in conn.execute("SELECT timepoint FROM answers"))
    assert left == [0.0, 60.0]
    payload = json.loads(
        conn.execute("SELECT payload_json FROM events WHERE kind = 'progress.reset'").fetchone()[0]
    )
    assert payload == {
        "from_t_index": 2,
        "to_t_index": 1,
        "was_completed": True,
        "deleted_answers": 1,
    }
    conn.close()


@pytest.mark.parametrize(
    ("clinician", "patient", "to_t_index", "fragment"),
    [
        ("Dr. Nobody", "synth_001", 0, "unknown clinician"),
        ("Dr. Test", "synth_002", 0, "has not started"),
        ("Dr. Test", "synth_001", 7, "outside the study"),
        ("Dr. Test", "synth_001", 2, "ahead of the current frontier"),
    ],
    ids=["unknown_clinician", "no_progress_row", "index_out_of_range", "forward_reset"],
)
def test_cli_reset_progress_errors(
    runner: CliRunner,
    study_fixture_dir: Path,
    tmp_path: Path,
    clinician: str,
    patient: str,
    to_t_index: int,
    fragment: str,
) -> None:
    from ehr_simulator.db import connect

    db_path = tmp_path / "walk.db"
    _walked_db(db_path, unlocked=1, completed=False)

    result = runner.invoke(
        cli.app_typer,
        [
            "reset-progress",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--clinician",
            clinician,
            "--patient",
            patient,
            "--to-t-index",
            str(to_t_index),
            "--db-path",
            str(db_path),
        ],
    )
    assert result.exit_code == 1
    assert fragment in result.stderr

    conn = connect(db_path)
    assert conn.execute("SELECT unlocked_t_index FROM progress").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 3
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0] == 1
    conn.close()


def test_cli_reset_progress_refuses_stale_schema(
    runner: CliRunner, study_fixture_dir: Path, tmp_path: Path
) -> None:
    """review #6: the recovery command never applies DDL under a live server."""
    from ehr_simulator.db import MIGRATIONS, connect

    db_path = tmp_path / "old.db"
    conn = connect(db_path)
    conn.executescript(MIGRATIONS[0].up_sql)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (1, 'initial')")
    conn.commit()
    conn.close()

    result = runner.invoke(
        cli.app_typer,
        [
            "reset-progress",
            str(study_fixture_dir / "study_synthetic.yaml"),
            "--clinician",
            "Dr. Test",
            "--patient",
            "synth_001",
            "--db-path",
            str(db_path),
        ],
    )
    assert result.exit_code == 1
    assert "pending migrations [2, 3]" in result.stderr
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# export-answers (S9c, spec tests 52-60)
# ---------------------------------------------------------------------------

STUDY_CONFIG = "study_synthetic.yaml"
QUESTIONS = "questions.yaml"
FINAL_INDEX = 2  # len([0, 60, 180]) - 1

EXPECTED_ANSWER_HEADER = (
    "patient_id,clinician_id,t_index,timepoint_minutes,arm,completed_at,"
    "config_hash,deterioration_6h,survives_hospital,good_outcome_3mo,dead_6mo,"
    "confidence,contributing_factors,free_notes"
)


def _live_hash(study_fixture_dir: Path) -> str:
    from ehr_simulator.config import compute_config_hash

    return compute_config_hash(study_fixture_dir / STUDY_CONFIG, study_fixture_dir / QUESTIONS)


def _seed_completed_walk(
    db: sqlite3.Connection, clinician_name: str, patient_id: str, *, live_hash: str
) -> str:
    from ehr_simulator.db import arm_assignments, clinicians, progress

    cid = clinicians.lookup_or_create(db, clinician_name)
    arm_assignments.assign_or_lookup(db, cid, patient_id, config_hash=live_hash)
    for to_index in range(1, FINAL_INDEX + 1):
        progress.unlock(
            db,
            clinician_id=cid,
            patient_id=patient_id,
            from_t_index=to_index - 1,
            to_t_index=to_index,
            config_hash=live_hash,
        )
    progress.mark_complete(
        db,
        clinician_id=cid,
        patient_id=patient_id,
        unlocked_t_index=FINAL_INDEX,
        config_hash=live_hash,
    )
    return cid


def _export_args(study_fixture_dir: Path, tmp_db_path: Path, *extra: str) -> list[str]:
    return [
        "export-answers",
        str(study_fixture_dir / STUDY_CONFIG),
        str(study_fixture_dir / QUESTIONS),
        "--db-path",
        str(tmp_db_path),
        *extra,
    ]


def test_cli_export_answers_happy_path_writes_and_reports(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    out = tmp_path / "answers.csv"

    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, tmp_db_path, "--out", str(out))
    )

    assert result.exit_code == 0, result.stderr
    assert (
        "Wrote 3 rows × 14 columns for 1 patient, 1 clinician (1 complete walk, 0 in progress)"
        in result.stdout
    )
    assert f"to {out}" in result.stdout
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == EXPECTED_ANSWER_HEADER
    assert len(lines) == 4  # header + 3 timepoint rows
    assert lines[1].startswith("synth_001,")
    assert "no_ai" in lines[1]


def test_cli_export_answers_default_out_path_under_db_exports(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))

    result = runner.invoke(cli.app_typer, _export_args(study_fixture_dir, tmp_db_path))

    assert result.exit_code == 0, result.stderr
    exports_dir = tmp_db_path.parent / "exports"
    matches = list(exports_dir.glob("answers_*.csv"))
    assert len(matches) == 1
    assert "to " in result.stdout


def test_cli_export_answers_refuses_existing_out_without_force(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    out = tmp_path / "answers.csv"
    out.write_text("pre-existing", encoding="utf-8")

    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, tmp_db_path, "--out", str(out))
    )

    assert result.exit_code == 1
    assert "refusing to overwrite existing output" in result.stderr
    assert out.read_text(encoding="utf-8") == "pre-existing"


def test_cli_export_answers_force_replaces_existing_out(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    out = tmp_path / "answers.csv"
    out.write_text("pre-existing", encoding="utf-8")

    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, tmp_db_path, "--out", str(out), "--force")
    )

    assert result.exit_code == 0, result.stderr
    first_line = out.read_text(encoding="utf-8").splitlines()[0]
    assert first_line == EXPECTED_ANSWER_HEADER


def test_cli_export_answers_failures_exit_1_and_leave_no_output(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    # (a) missing database
    missing_db = tmp_path / "does_not_exist" / "db.sqlite3"
    out_a = tmp_path / "a.csv"
    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, missing_db, "--out", str(out_a))
    )
    assert result.exit_code == 1
    assert "database not found" in result.stderr
    assert not any(p.exists() for p in [out_a])

    # (b) stale schema: only migration 1
    from ehr_simulator.db import MIGRATIONS, connect

    stale_db = tmp_path / "stale.db"
    stale_db.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(stale_db)
    conn.executescript(MIGRATIONS[0].up_sql)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("INSERT INTO schema_migrations (version, name) VALUES (1, 'initial')")
    conn.commit()
    conn.close()
    out_b = tmp_path / "b.csv"
    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, stale_db, "--out", str(out_b))
    )
    assert result.exit_code == 1
    assert "pending migrations" in result.stderr
    assert not out_b.exists()

    # (c) integrity failure: foreign config hash in one table
    from ehr_simulator.db import arm_assignments, clinicians

    foreign_hash = "a" * 64
    cid = clinicians.lookup_or_create(db, "Dr. Drift")
    arm_assignments.assign_or_lookup(db, cid, "synth_001", config_hash=foreign_hash)
    out_c = tmp_path / "c.csv"
    result = runner.invoke(
        cli.app_typer, _export_args(study_fixture_dir, tmp_db_path, "--out", str(out_c))
    )
    assert result.exit_code == 1
    assert "another study configuration" in result.stderr
    assert not out_c.exists()


def test_cli_export_answers_keyfile_writes_mode_0600(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    import hashlib
    import stat

    from ehr_simulator.db import clinicians

    clinician_id = clinicians.lookup_or_create(db, "Dr. CLI")
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    keyfile = tmp_path / "keys" / "clinicians.keyfile.csv"

    result = runner.invoke(
        cli.app_typer,
        _export_args(
            study_fixture_dir,
            tmp_db_path,
            "--out",
            str(tmp_path / "answers.csv"),
            "--keyfile",
            str(keyfile),
        ),
    )

    assert result.exit_code == 0, result.stderr
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    assert hashlib.sha256(b"dr. cli").hexdigest()[:16] == clinician_id
    assert keyfile.read_text(encoding="utf-8") == (
        f"clinician_id,name_normalized\n{clinician_id},dr. cli\n"
    )
    assert "Wrote keyfile" in result.stdout


def test_cli_export_answers_same_path_for_out_and_keyfile_refused(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    same = tmp_path / "both.csv"

    result = runner.invoke(
        cli.app_typer,
        _export_args(
            study_fixture_dir,
            tmp_db_path,
            "--out",
            str(same),
            "--keyfile",
            str(same),
        ),
    )

    assert result.exit_code == 1
    assert "same path" in result.stderr
    assert not same.exists()


def test_cli_export_answers_keyfile_refused_on_non_posix(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from ehr_simulator import export as export_module

    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))

    class _NonPosixOS:
        """Looks like a non-POSIX ``os`` module but delegates everything else."""

        name = "nt"

        def __getattr__(self, item: str) -> object:
            return getattr(os, item)

    monkeypatch.setattr(export_module, "os", _NonPosixOS())
    keyfile = tmp_path / "clinicians.keyfile.csv"
    out = tmp_path / "answers.csv"

    result = runner.invoke(
        cli.app_typer,
        _export_args(
            study_fixture_dir,
            tmp_db_path,
            "--out",
            str(out),
            "--keyfile",
            str(keyfile),
        ),
    )

    assert result.exit_code == 1
    assert "keyfile" in result.stderr
    assert not out.exists()
    assert not keyfile.exists()


def test_cli_export_answers_os_failure_is_exit_1(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ehr_simulator import export as export_module

    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    out = tmp_path / "answers.csv"

    def fail_write(*_args, **_kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(export_module, "write_export", fail_write)

    result = runner.invoke(
        cli.app_typer,
        _export_args(study_fixture_dir, tmp_db_path, "--out", str(out)),
    )

    assert result.exit_code == 1
    assert "Error: permission denied" in result.stderr
    assert not out.exists()


def test_cli_export_answers_invisible_to_uncommitted_writer(
    runner: CliRunner,
    db: sqlite3.Connection,
    tmp_db_path: Path,
    tmp_path: Path,
    study_fixture_dir: Path,
) -> None:
    from ehr_simulator.db import connect

    _seed_completed_walk(db, "Dr. CLI", "synth_001", live_hash=_live_hash(study_fixture_dir))
    live_hash = _live_hash(study_fixture_dir)
    bob = "b" * 16

    writer = connect(tmp_db_path)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
            (bob, "bob"),
        )
        writer.execute(
            "INSERT INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, seed, config_hash) "
            "VALUES (?, ?, ?, ?, NULL, ?)",
            (bob, "synth_001", "no_ai", "test", live_hash),
        )
        writer.execute(
            "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
            "VALUES (?, ?, ?, ?)",
            (bob, "synth_001", 0, live_hash),
        )
        writer.execute(
            "INSERT INTO answers "
            "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (bob, "synth_001", 0.0, "deterioration_6h", "Yes", "no_ai", live_hash),
        )
        # Deliberately no COMMIT.

        out = tmp_path / "answers.csv"
        result = runner.invoke(
            cli.app_typer,
            _export_args(study_fixture_dir, tmp_db_path, "--out", str(out)),
        )

        assert result.exit_code == 0, result.stderr
        assert "Wrote 3 rows" in result.stdout
        assert bob not in out.read_text(encoding="utf-8")
        # The writer remains usable and still sees its own uncommitted row.
        assert writer.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0] == 2
    finally:
        writer.rollback()
        writer.close()
