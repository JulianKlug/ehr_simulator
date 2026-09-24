"""S11b: configuration version history, snapshots, and provenance guards.

Covers the ``configuration_history`` / ``active_configuration`` DAO
(:mod:`ehr_simulator.db.config_history`), the immutable study/questions
snapshots (:mod:`ehr_simulator.config.snapshot`), the S11a upgrade
backfill, and the write-side provenance guards on the four case tables.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pydantic
import pytest

from ehr_simulator.config import (
    ConfigValidationError,
    Questions,
    StudyConfig,
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
    parse_questions_snapshot,
    parse_study_snapshot,
    render_questions_snapshot,
    render_study_snapshot,
)
from ehr_simulator.db import (
    ConfigurationActivationError,
    ConfigurationProvenanceError,
    answers,
    arm_assignments,
    config_history,
    progress,
    sessions,
    study_identity,
)


@pytest.fixture
def study(study_fixture_dir) -> StudyConfig:
    return load_study_config(study_fixture_dir / "study_synthetic.yaml")


@pytest.fixture
def questions(study_fixture_dir) -> Questions:
    return load_questions(study_fixture_dir / "questions.yaml")


@pytest.fixture
def config_hash(study: StudyConfig, questions: Questions) -> str:
    return compute_config_hash_from_models(study, questions)


def _activate(
    conn: sqlite3.Connection,
    *,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
    version: str = "v1",
    description: str = "initial activation",
    reason: str | None = None,
) -> config_history.ConfigHistoryRow:
    study_identity.bind(conn, study.study_id)
    return config_history.activate(
        conn,
        study_id=study.study_id,
        config_version=version,
        config_hash=config_hash,
        description=description,
        reason=reason,
        study=study,
        questions=questions,
    )


# ---------------------------------------------------------------------------
# Metadata validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_version",
    [
        "bad label!",  # space + punctuation
        "",  # empty
        "a" * 65,  # too long
        "-leading-dash",  # must start alphanumeric
        ".v1",
        "v1\u00e9-acute",  # non-ascii (e-acute)
    ],
)
def test_activate_rejects_invalid_version_labels(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
    bad_version: str,
) -> None:
    with pytest.raises(ConfigurationActivationError, match="config_version"):
        _activate(
            db,
            study=study,
            questions=questions,
            config_hash=config_hash,
            version=bad_version,
        )
    assert not config_history.has_any(db)


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        ("   ", None),  # blank description
        ("x" * 501, None),  # description too long
        ("ok", "   "),  # provided-but-blank reason
        ("ok", "y" * 1001),  # reason too long
    ],
)
def test_activate_rejects_invalid_metadata(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
    description: str,
    reason: str | None,
) -> None:
    with pytest.raises(ConfigurationActivationError):
        _activate(
            db,
            study=study,
            questions=questions,
            config_hash=config_hash,
            description=description,
            reason=reason,
        )
    assert not config_history.has_any(db)


def test_activate_rejects_study_id_mismatch(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    study_identity.bind(db, study.study_id)
    with pytest.raises(ConfigurationActivationError, match="does not match"):
        config_history.activate(
            db,
            study_id="other_study",
            config_version="v1",
            config_hash=config_hash,
            description="x",
            reason=None,
            study=study,
            questions=questions,
        )
    assert not config_history.has_any(db)


def test_activate_rejects_empty_hash(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
) -> None:
    study_identity.bind(db, study.study_id)
    with pytest.raises(ConfigurationActivationError, match="config_hash"):
        config_history.activate(
            db,
            study_id=study.study_id,
            config_version="v1",
            config_hash="",
            description="x",
            reason=None,
            study=study,
            questions=questions,
        )
    assert not config_history.has_any(db)


# ---------------------------------------------------------------------------
# Decision table: new / no-op / collision / rollback
# ---------------------------------------------------------------------------


def test_activate_registers_and_activates(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    row = _activate(
        db,
        study=study,
        questions=questions,
        config_hash=config_hash,
        description="establish baseline",
        reason="first real run",
    )
    assert row.config_version == "v1"
    assert row.study_id == study.study_id
    assert row.config_hash == config_hash
    assert row.change_description == "establish baseline"
    assert row.change_reason == "first real run"
    assert config_history.has_any(db)
    assert config_history.fetch_active(db) is not None
    assert config_history.fetch_active(db).config_version == "v1"  # type: ignore[union-attr]
    assert config_history.fetch_version(db, "v1") == row
    assert [r.config_version for r in config_history.list_all(db)] == ["v1"]


def test_activate_rerun_exact_match_is_noop(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    first = _activate(db, study=study, questions=questions, config_hash=config_hash)
    second = _activate(db, study=study, questions=questions, config_hash=config_hash)
    assert second == first
    assert len(config_history.list_all(db)) == 1
    assert config_history.fetch_active(db).config_version == "v1"  # type: ignore[union-attr]


def test_activate_exact_replay_of_older_version_keeps_active(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    """Replaying v1 verbatim after v2 is a no-op — never a rollback to v1."""
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    _activate(
        db,
        study=study,
        questions=questions,
        config_hash=config_hash,
        version="v2",
        description="second activation",
    )

    replay = _activate(db, study=study, questions=questions, config_hash=config_hash)

    assert replay.config_version == "v1"
    assert config_history.fetch_active(db).config_version == "v2"  # type: ignore[union-attr]
    assert [r.config_version for r in config_history.list_all(db)] == ["v1", "v2"]


def test_activate_same_version_different_hash_refused(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    with pytest.raises(ConfigurationActivationError, match="different config_hash"):
        _activate(
            db,
            study=study,
            questions=questions,
            config_hash="0" * 64,
        )
    # Active pointer and metadata untouched.
    active = config_history.fetch_active(db)
    assert active is not None and active.config_hash == config_hash
    assert active.change_description == "initial activation"  # type: ignore[union-attr]


def test_activate_same_version_different_metadata_refused(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    with pytest.raises(ConfigurationActivationError, match="different"):
        _activate(
            db,
            study=study,
            questions=questions,
            config_hash=config_hash,
            description="pretending it was different",
        )
    assert len(config_history.list_all(db)) == 1


def test_activate_new_version_same_hash_allowed_rollback(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    """A rollback must use a NEW version label (spec decision table)."""
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    v2 = _activate(
        db,
        study=study,
        questions=questions,
        config_hash=config_hash,
        version="v2",
        description="roll back timepoints to 90",
        reason="180-minute horizon showed leakage",
    )
    assert v2.config_version == "v2"
    assert v2.config_hash == config_hash
    versions = [r.config_version for r in config_history.list_all(db)]
    assert versions == ["v1", "v2"]
    assert config_history.fetch_active(db).config_version == "v2"  # type: ignore[union-attr]
    # v1 survives for historical cases.
    assert config_history.fetch_version(db, "v1") is not None


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------


def test_stored_study_snapshot_excludes_paths_and_round_trips(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    row = _activate(db, study=study, questions=questions, config_hash=config_hash)
    payload = row.study_json
    for excluded in ("csv_path", "params_dir", "db_path"):
        assert f'"{excluded}"' not in payload
    parsed = parse_study_snapshot(payload)
    assert parsed == study

    q_payload = row.questions_json
    parsed_q = parse_questions_snapshot(q_payload)
    assert parsed_q == questions
    assert render_questions_snapshot(parsed_q) == q_payload


def test_parse_study_snapshot_rejects_tampering(study_fixture_dir: Path) -> None:
    config = load_study_config(study_fixture_dir / "study_synthetic.yaml")
    payload = render_study_snapshot(config)
    tampered = json.loads(payload)
    tampered["timepoints"] = [0, 30]
    with pytest.raises((ConfigValidationError, pydantic.ValidationError)):
        parse_study_snapshot(json.dumps(tampered))


def test_parse_study_snapshot_rejects_invalid_json() -> None:
    with pytest.raises((ConfigValidationError, pydantic.ValidationError)):
        parse_study_snapshot("definitely-not-json")


def test_activate_refuses_on_top_of_unparseable_snapshot(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    # Corrupt the stored snapshot so it no longer re-renders identically.
    db.execute("UPDATE configuration_history SET study_json = '{\"broken\": true}'")
    db.commit()
    with pytest.raises(ConfigurationActivationError, match="no longer parses"):
        _activate(
            db,
            study=study,
            questions=questions,
            config_hash=config_hash,
            version="v2",
            description="cannot ride on corrupted history",
        )


def test_dataset_cannot_change_within_one_study(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    other_dataset = study.model_copy(update={"dataset": "mimic"})
    other_hash = compute_config_hash_from_models(other_dataset, questions)
    with pytest.raises(ConfigurationActivationError, match="dataset"):
        _activate(
            db,
            study=other_dataset,
            questions=questions,
            config_hash=other_hash,
            version="v2",
            description="switch datasets",
        )


def test_history_belongs_to_one_study(
    db: sqlite3.Connection,
    study_fixture_dir,
) -> None:
    study = load_study_config(study_fixture_dir / "study_synthetic.yaml")
    questions = load_questions(study_fixture_dir / "questions.yaml")
    _activate(
        db,
        study=study,
        questions=questions,
        config_hash=compute_config_hash_from_models(study, questions),
    )
    other = load_study_config(study_fixture_dir / "study_mimic.yaml")
    other_hash = compute_config_hash_from_models(other, questions)
    with pytest.raises(ConfigurationActivationError, match="belongs to study"):
        config_history.activate(
            db,
            study_id=other.study_id,
            config_version="v9",
            config_hash=other_hash,
            description="foreign study",
            reason=None,
            study=other,
            questions=questions,
        )


# ---------------------------------------------------------------------------
# require_known / read API
# ---------------------------------------------------------------------------


def test_require_known_round_trip(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    row = _activate(db, study=study, questions=questions, config_hash=config_hash)
    assert config_history.require_known(db, "v1", config_hash) == row
    with pytest.raises(ConfigurationProvenanceError, match="unknown config_version"):
        config_history.require_known(db, "v9", config_hash)
    with pytest.raises(ConfigurationProvenanceError, match="refusing to serve the case"):
        config_history.require_known(db, "v1", "f" * 64)


def test_fetch_active_none_when_unactivated(
    db: sqlite3.Connection,
) -> None:
    assert config_history.fetch_active(db) is None
    assert config_history.fetch_version(db, "v1") is None
    assert config_history.list_all(db) == ()
    assert not config_history.has_any(db)


# ---------------------------------------------------------------------------
# S11a upgrade backfill
# ---------------------------------------------------------------------------


def _seed_clinician(db: sqlite3.Connection, clinician_id: str) -> None:
    db.execute(
        "INSERT OR IGNORE INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (clinician_id, clinician_id),
    )
    db.commit()


def _seed_legacy_row(db: sqlite3.Connection, study: StudyConfig, hash_value: str) -> None:
    """S11a state: identity bound, case rows present but without config_version."""
    study_identity.bind(db, study.study_id)
    _seed_clinician(db, "cl1")
    db.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash, "
        "config_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        ("cl1", "p1", 0.0, "q1", "Yes", "no_ai", hash_value),
    )
    db.commit()


def test_backfill_applies_when_single_matching_hash(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _seed_legacy_row(db, study, config_hash)
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    got = db.execute("SELECT config_version FROM answers WHERE clinician_id = 'cl1'").fetchone()
    assert got[0] == "v1"


def test_backfill_refused_for_mismatching_hash(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _seed_legacy_row(db, study, "a" * 64)
    with pytest.raises(ConfigurationActivationError, match="does not match"):
        _activate(db, study=study, questions=questions, config_hash=config_hash)
    assert not config_history.has_any(db)
    assert db.execute("SELECT config_version FROM answers").fetchone()[0] is None


def test_backfill_refused_for_ambiguous_hashes(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    _seed_legacy_row(db, study, config_hash)
    _seed_clinician(db, "cl2")
    db.execute(
        "INSERT INTO sessions "
        "(session_id, clinician_id, patient_id, arm, config_hash, config_version) "
        "VALUES ('s1', 'cl1', 'p1', 'no_ai', ?, NULL)",
        (config_hash,),
    )
    db.execute(
        "INSERT INTO sessions "
        "(session_id, clinician_id, patient_id, arm, config_hash, config_version) "
        "VALUES ('s2', 'cl2', 'p2', 'no_ai', 'b' * 64, NULL)"
    )
    db.commit()
    with pytest.raises(ConfigurationActivationError, match="ambiguous"):
        _activate(db, study=study, questions=questions, config_hash=config_hash)
    assert not config_history.has_any(db)


def test_backfill_skipped_when_history_exists(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> None:
    """Once history exists, the S11a backfill must not run again."""
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    # A stray legacy-looking row (e.g. a test import) appears AFTER v1.
    _seed_clinician(db, "cl9")
    db.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash, "
        "config_version) "
        "VALUES ('cl9', 'p9', 0.0, 'q1', 'No', 'no_ai', 'c' * 64, NULL)"
    )
    db.commit()
    # The activation above is a no-op (exact metadata match) — the stray row is untouched.
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    assert (
        db.execute("SELECT config_version FROM answers WHERE clinician_id='cl9'").fetchone()[0]
        is None
    )


# ---------------------------------------------------------------------------
# Write-side provenance guards on the four case tables
# ---------------------------------------------------------------------------


@pytest.fixture
def activated(
    db: sqlite3.Connection,
    study: StudyConfig,
    questions: Questions,
    config_hash: str,
) -> str:
    _activate(db, study=study, questions=questions, config_hash=config_hash)
    return config_hash


def test_answers_upsert_refuses_null_version_when_history_exists(
    db: sqlite3.Connection, activated: str
) -> None:
    _seed_clinician(db, "c1")
    with pytest.raises(ConfigurationProvenanceError, match="config_version"):
        answers.upsert(
            db,
            clinician_id="c1",
            patient_id="p1",
            timepoint=0.0,
            question_id="q1",
            value="Yes",
            arm="no_ai",
            config_hash=activated,
        )
    assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 0


def test_arm_assignments_refuse_null_version(db: sqlite3.Connection, activated: str) -> None:
    _seed_clinician(db, "c1")
    with pytest.raises(ConfigurationProvenanceError, match="config_version"):
        arm_assignments.assign_or_lookup(db, "c1", "p1", config_hash=activated)
    assert db.execute("SELECT COUNT(*) FROM arm_assignments").fetchone()[0] == 0


def test_sessions_refuse_null_version(db: sqlite3.Connection, activated: str) -> None:
    _seed_clinician(db, "c1")
    with pytest.raises(ConfigurationProvenanceError, match="config_version"):
        sessions.start_or_resume(db, "c1", "p1", arm="no_ai", config_hash=activated)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_progress_refuses_null_version(db: sqlite3.Connection, activated: str) -> None:
    _seed_clinician(db, "c1")
    with pytest.raises(ConfigurationProvenanceError, match="config_version"):
        progress.unlock(
            db,
            clinician_id="c1",
            patient_id="p1",
            from_t_index=0,
            to_t_index=1,
            config_hash=activated,
        )
    assert db.execute("SELECT COUNT(*) FROM progress").fetchone()[0] == 0


def test_null_version_still_allowed_while_history_empty(db: sqlite3.Connection) -> None:
    """A fresh database (S11a state) keeps NULL-version rows legal."""
    assert not config_history.has_any(db)
    _seed_clinician(db, "c1")
    answers.upsert(
        db,
        clinician_id="c1",
        patient_id="p1",
        timepoint=0.0,
        question_id="q1",
        value="Yes",
        arm="no_ai",
        config_hash="d" * 64,
    )
    assert db.execute("SELECT COUNT(*) FROM answers").fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Answer provenance: a mismatched write is refused before any mutation
# ---------------------------------------------------------------------------


_CELL = {"clinician_id": "c1", "patient_id": "p1", "timepoint": 0.0, "question_id": "q1"}


def _seed_v1_answer(db: sqlite3.Connection, config_hash: str) -> None:
    _seed_clinician(db, "c1")
    answers.upsert(
        db, **_CELL, value="No", arm="no_ai", config_hash=config_hash, config_version="v1"
    )


def _stored_answer(db: sqlite3.Connection) -> tuple[str, str, str]:
    row = db.execute("SELECT value, config_hash, config_version FROM answers").fetchone()
    return (row[0], row[1], row[2])


@pytest.mark.parametrize(
    ("version", "hash_value"),
    [("v2", "e" * 64), ("v2", None), ("v1", "e" * 64)],
    ids=["version-and-hash", "version-only", "hash-only"],
)
def test_answer_upsert_refuses_mismatched_provenance_without_mutation(
    db: sqlite3.Connection,
    activated: str,
    version: str,
    hash_value: str | None,
) -> None:
    _seed_v1_answer(db, activated)

    with pytest.raises(ConfigurationProvenanceError, match="provenance"):
        answers.upsert(
            db,
            **_CELL,
            value="Yes",
            arm="no_ai",
            config_hash=hash_value or activated,
            config_version=version,
        )

    assert _stored_answer(db) == ("No", activated, "v1")


def test_answer_delete_refuses_mismatched_provenance_without_mutation(
    db: sqlite3.Connection, activated: str
) -> None:
    _seed_v1_answer(db, activated)

    with pytest.raises(ConfigurationProvenanceError, match="provenance"):
        answers.delete_one(db, **_CELL, config_hash="e" * 64, config_version="v2")

    assert _stored_answer(db) == ("No", activated, "v1")


def test_answer_upsert_with_matching_provenance_updates_value(
    db: sqlite3.Connection, activated: str
) -> None:
    _seed_v1_answer(db, activated)

    answers.upsert(
        db, **_CELL, value="Yes", arm="no_ai", config_hash=activated, config_version="v1"
    )

    assert _stored_answer(db) == ("Yes", activated, "v1")
