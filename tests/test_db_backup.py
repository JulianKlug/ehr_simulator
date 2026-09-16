"""Backup module tests (specs/session-06-sqlite-persistence.md §9 #19-#21).

Covers file creation, post-write integrity, dest-dir auto-create, and the
write-counter shutdown gate (review-fix R8): no backup when no writes; one
backup when the counter is non-zero.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from ehr_simulator.db import answers, apply_migrations, clinicians, connect
from ehr_simulator.db.backup import create_backup
from ehr_simulator.ingestion.synthetic import SyntheticDataset
from ehr_simulator.web.app import create_app


def test_backup_creates_file_in_dest_dir(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    cid = clinicians.lookup_or_create(conn, "Dr. Smith")
    answers.upsert(
        conn,
        clinician_id=cid,
        patient_id="p1",
        timepoint=0.0,
        question_id="q1",
        value="x",
        arm="no_ai",
        config_hash="h",
    )
    conn.close()

    dest = create_backup(tmp_db_path, tmp_backup_dir)
    assert dest.exists()
    backup_files = list(tmp_backup_dir.glob("ehr_simulator_*.db"))
    assert len(backup_files) == 1
    assert backup_files[0] == dest


def test_backup_dest_is_readable_with_data_intact(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    clinicians.lookup_or_create(conn, "Dr. Smith")
    clinicians.lookup_or_create(conn, "Dr. Other")
    conn.close()

    dest = create_backup(tmp_db_path, tmp_backup_dir)
    copy = sqlite3.connect(dest)
    try:
        rows = copy.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0]
    finally:
        copy.close()
    assert rows == 2


def test_backup_creates_dest_dir_if_missing(tmp_path: Path, tmp_db_path: Path) -> None:
    conn = connect(tmp_db_path)
    apply_migrations(conn)
    conn.close()
    missing = tmp_path / "deeply" / "nested" / "backups"
    assert not missing.exists()
    dest = create_backup(tmp_db_path, missing)
    assert missing.exists()
    assert dest.exists()


def test_backup_skipped_when_write_counter_zero(
    tmp_log_dir: Path,
    dataset: SyntheticDataset,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """review-fix R8: lifespan shutdown skips the backup when no writes."""
    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=lambda: dataset,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app):
        pass
    assert not tmp_backup_dir.exists() or not list(tmp_backup_dir.glob("ehr_simulator_*.db"))

    import logging as _stdlogging

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    records = [
        json.loads(line)
        for line in (tmp_log_dir / "current.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("event_kind") == "db.backup.skipped" for r in records)


def test_backup_runs_when_write_counter_positive(
    tmp_log_dir: Path,
    dataset: SyntheticDataset,
    tmp_db_path: Path,
    tmp_backup_dir: Path,
) -> None:
    """review-fix R8: a successful /login POST trips the gate; shutdown writes a backup."""
    app = create_app(
        log_dir=tmp_log_dir,
        dataset_loader=lambda: dataset,
        db_path=tmp_db_path,
        backup_dir=tmp_backup_dir,
    )
    with TestClient(app) as client:
        client.post("/login", data={"clinician_name": "Dr. Smith"}, follow_redirects=False)

    backups = list(tmp_backup_dir.glob("ehr_simulator_*.db"))
    assert len(backups) == 1

    import logging as _stdlogging

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    records = [
        json.loads(line)
        for line in (tmp_log_dir / "current.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(r.get("event_kind") == "db.backup.ok" for r in records)
