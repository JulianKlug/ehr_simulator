"""Export service tests (``ehr_simulator.export``) — S9c spec §9 items 12-51.

Covers build (shape, ordering, decoding, every integrity refusal), the
formula-injection guard, keyfile scoping, the WAL snapshot invariant,
and the staged-write / no-clobber install contract.
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import stat
from pathlib import Path

import pytest

from ehr_simulator.config import (
    MultiSelectQuestion,
    compute_config_hash,
    load_questions,
    load_study_config,
)
from ehr_simulator.db import answers, arm_assignments, clinicians, connect, progress
from ehr_simulator.db.connection import AccessMode
from ehr_simulator.export import (
    METADATA_COLUMNS,
    ExportError,
    ExportOptions,
    build_export,
    guard_cell,
    write_csv,
    write_export,
    write_keyfile,
)

DEFAULT_OPTIONS = ExportOptions()

TIMEPOINTS = [0, 60, 180]
FINAL_INDEX = len(TIMEPOINTS) - 1

# One configured value per question id (fixture questions.yaml order).
VALID_CELLS = {
    "deterioration_6h": "No",
    "survives_hospital": "Yes",
    "good_outcome_3mo": "75",
    "dead_6mo": "No",
    "confidence": "4",
    "contributing_factors": json.dumps(["Imaging", "Vitals"]),
    "free_notes": "Stable.",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def study(study_fixture_dir: Path):
    return load_study_config(study_fixture_dir / "study_synthetic.yaml")


@pytest.fixture
def questions(study_fixture_dir: Path):
    return load_questions(study_fixture_dir / "questions.yaml")


@pytest.fixture
def live_hash(study_fixture_dir: Path) -> str:
    return compute_config_hash(
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
    )


@pytest.fixture
def ro_db(db, tmp_db_path: Path):
    """A read-only connection for the exporter over the (committed) test DB."""
    conn = connect(tmp_db_path, access=AccessMode.READ_ONLY)
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Seeding helpers (write through a RW connection; export reads committed state)
# ---------------------------------------------------------------------------


def seed_clinician(db: sqlite3.Connection, name: str) -> str:
    return clinicians.lookup_or_create(db, name)


def seed_arm(
    db: sqlite3.Connection,
    clinician_id: str,
    patient_id: str,
    *,
    config_hash: str,
    arm: str | None = None,
) -> None:
    if arm is None:
        arm_assignments.assign_or_lookup(db, clinician_id, patient_id, config_hash=config_hash)
        return
    db.execute(
        "INSERT OR REPLACE INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash) "
        "VALUES (?, ?, ?, 'test', NULL, ?)",
        (clinician_id, patient_id, arm, config_hash),
    )
    db.commit()


def seed_frontier(
    db: sqlite3.Connection, clinician_id: str, patient_id: str, unlocked: int, *, live_hash: str
) -> None:
    for to in range(1, unlocked + 1):
        progress.unlock(
            db,
            clinician_id=clinician_id,
            patient_id=patient_id,
            from_t_index=to - 1,
            to_t_index=to,
            config_hash=live_hash,
        )


def seed_complete(
    db: sqlite3.Connection, clinician_id: str, patient_id: str, unlocked: int, *, live_hash: str
) -> None:
    progress.mark_complete(
        db,
        clinician_id=clinician_id,
        patient_id=patient_id,
        unlocked_t_index=unlocked,
        config_hash=live_hash,
    )


def seed_answer(
    db: sqlite3.Connection,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    question_id: str,
    value: str,
    *,
    arm: str = "no_ai",
    config_hash: str = "LIVE-HASH",
) -> None:
    answers.upsert(
        db,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=TIMEPOINTS[t_index],
        question_id=question_id,
        value=value,
        arm=arm,
        config_hash=config_hash,
    )


def build(study, questions, ro_db, *, live_hash, options=DEFAULT_OPTIONS, include_keyfile=False):
    return build_export(
        ro_db,
        study=study,
        questions=questions,
        live_hash=live_hash,
        options=options,
        include_keyfile=include_keyfile,
    )


# ---------------------------------------------------------------------------
# Frame shape / ordering (items 12-21)
# ---------------------------------------------------------------------------


def test_header_is_metadata_then_questions_yaml_order(
    study, questions, db, ro_db, live_hash
) -> None:
    bundle = build(study, questions, ro_db, live_hash=live_hash)
    expected = METADATA_COLUMNS + tuple(q.question_id for q in questions.questions)
    assert bundle.frame.header == expected
    assert bundle.frame.header == (
        "patient_id",
        "clinician_id",
        "t_index",
        "timepoint_minutes",
        "arm",
        "completed_at",
        "config_hash",
        "deterioration_6h",
        "survives_hospital",
        "good_outcome_3mo",
        "dead_6mo",
        "confidence",
        "contributing_factors",
        "free_notes",
    )


def test_one_row_per_unlocked_timepoint_for_progress_pairs(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_frontier(db, alice, "synth_001", 1, live_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 1, "deterioration_6h", "Yes", config_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert len(frame.rows) == 2
    rows = {(r[2], r[3]) for r in frame.rows}
    assert rows == {("0", "0.0"), ("1", "60.0")}


def test_answer_only_pair_frontier_is_highest_answered(
    study, questions, db, ro_db, live_hash
) -> None:
    # No progress row at all: the pair still exports (frontier = max answer).
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 1, "deterioration_6h", "No", config_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert [r[2] for r in frame.rows] == ["0", "1"]
    assert [r[6] for r in frame.rows] == [live_hash] * 2
    # Answer-only pairs can never count as complete.
    assert bundle_complete_counts(frame) == (0, 1)


def bundle_complete_counts(frame) -> tuple[int, int]:
    return frame.report.complete_walks, frame.report.in_progress_walks


def test_progress_only_pair_yields_blank_answer_cells(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    db.execute(
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
        "VALUES (?, 'synth_001', 0, ?)",
        (alice, live_hash),
    )
    db.commit()

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert len(frame.rows) == 1
    row = frame.rows[0]
    # Metadata cells are all populated...
    assert row[:7] == ("synth_001", alice, "0", "0.0", "no_ai", "", live_hash)
    # ...and every answer cell is the empty string.
    assert row[7:] == ("",) * 7


def test_only_complete_drops_incomplete_pairs_silently(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    bob = seed_clinician(db, "Bob")
    for cid in (alice, bob):
        seed_arm(db, cid, "synth_001", config_hash=live_hash)
        seed_answer(db, cid, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)
    # Alice completes; Bob stays in progress (answer-only pair).
    seed_frontier(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    for qid in VALID_CELLS:
        for t in range(FINAL_INDEX + 1):
            seed_answer(db, alice, "synth_001", t, qid, VALID_CELLS[qid], config_hash=live_hash)
    seed_complete(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)

    all_pairs = build(study, questions, ro_db, live_hash=live_hash)
    assert all_pairs.frame.report.clinicians == 2
    complete = build(
        study,
        questions,
        ro_db,
        live_hash=live_hash,
        options=ExportOptions(only_complete=True),
    )
    assert complete.frame.report.clinicians == 1
    assert complete.frame.report.complete_walks == 1
    assert complete.frame.report.in_progress_walks == 0
    assert {r[1] for r in complete.frame.rows} == {alice}


def test_optional_unanswered_question_is_empty_string(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(
        db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash
    )  # only one answered

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    row = frame.rows[0]
    free_idx = METADATA_COLUMNS + tuple(frame.header[len(METADATA_COLUMNS) :])
    assert row[free_idx.index("free_notes")] == ""  # required: false, unanswered
    assert row[free_idx.index("deterioration_6h")] == "No"


def test_completed_at_repeats_on_every_row_of_the_pair(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_frontier(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    for t in range(FINAL_INDEX + 1):
        for qid in VALID_CELLS:
            seed_answer(db, alice, "synth_001", t, qid, VALID_CELLS[qid], config_hash=live_hash)
    seed_complete(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    stamp = [r[5] for r in frame.rows]
    assert len(stamp) == FINAL_INDEX + 1
    assert len(set(stamp)) == 1  # same wall-clock on every row of the walk
    assert stamp[0]


def test_completed_at_cell_matches_db_value(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_frontier(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    seed_complete(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    stored = db.execute(
        "SELECT completed_at FROM progress WHERE clinician_id = ?", (alice,)
    ).fetchone()[0]

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert frame.rows[0][5] == stored.strftime("%Y-%m-%d %H:%M:%S")


def test_completed_at_cell_uses_exact_format(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_frontier(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    seed_complete(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", frame.rows[0][5])


def test_row_order_is_patient_then_clinician_then_t_index(
    study, questions, db, ro_db, live_hash
) -> None:
    # synth_001 sorts last in the study, Bob < Alice alphabetically.
    patients = ["synth_003", "synth_001"]
    names = ["Zed", "Alice"]
    for i, pid in enumerate(patients):
        for name in names:
            cid = seed_clinician(db, name)
            seed_arm(db, cid, pid, config_hash=live_hash)
            seed_answer(db, cid, pid, i, "deterioration_6h", "No", config_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    # synth_001's pairs were seeded at t_index 1 → frontier = 1 → rows for
    # t_index 0 and 1. synth_003's pairs were seeded at t_index 0 → one row each.
    ranked = {name: clinicians.lookup(db, name) for name in ("Alice", "Zed")}
    rows_per_patient = {"synth_001": ["0", "1"], "synth_003": ["0"]}
    expected = [
        (pid, cid, tp)
        for pid in ("synth_001", "synth_003")
        for cid in sorted((ranked["Alice"], ranked["Zed"]))
        for tp in rows_per_patient[pid]
    ]
    assert [(r[0], r[1], r[2]) for r in frame.rows] == expected


def test_timepoint_minutes_cell_uses_float_repr(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_frontier(db, alice, "synth_001", FINAL_INDEX, live_hash=live_hash)
    for t in range(FINAL_INDEX + 1):
        seed_answer(db, alice, "synth_001", t, "deterioration_6h", "No", config_hash=live_hash)

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    assert [r[3] for r in frame.rows] == ["0.0", "60.0", "180.0"]


def test_export_is_byte_identical_across_runs(
    study, questions, db, ro_db, tmp_path, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)

    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    write_csv(build(study, questions, ro_db, live_hash=live_hash).frame, a)
    write_csv(build(study, questions, ro_db, live_hash=live_hash).frame, b)
    assert a.read_bytes() == b.read_bytes()


def test_multi_select_pipe_encoding_preserves_option_order(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    # The UI serializer stores the canonical JSON array in questions.yaml
    # option order; the CSV cell is the pipe encoding of that list.
    seed_answer(
        db,
        alice,
        "synth_001",
        0,
        "contributing_factors",
        json.dumps(["Imaging", "Vitals"]),
        config_hash=live_hash,
    )

    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    idx = list(frame.header).index("contributing_factors")
    assert frame.rows[0][idx] == "Imaging|Vitals"


# ---------------------------------------------------------------------------
# Refusals: config / integrity (items 24-37)
# ---------------------------------------------------------------------------


def test_question_id_collision_with_metadata_column(study, questions, ro_db, live_hash) -> None:
    broken = questions.model_copy(deep=True)
    broken.questions = [
        q.model_copy(update={"question_id": "arm"}) if q.question_id == "confidence" else q
        for q in broken.questions
    ]
    with pytest.raises(ExportError, match="metadata column"):
        build(study, broken, ro_db, live_hash=live_hash)


def test_pipe_carrying_multi_select_member_never_survives_decode(
    study, db, ro_db, live_hash
) -> None:
    # Even if a pipe-carrying option somehow reached the exporter (e.g. a
    # question constructed past the config validator), the strict decoder
    # refuses it: pipe members would corrupt the CSV encoding.
    hostile = MultiSelectQuestion.model_construct(
        question_id="q_pipe",
        prompt="p",
        options=["A|B", "C"],
        response_type="multi-select",
    )
    from ehr_simulator.answer_codec import AnswerValidationError, decode_stored_answer

    with pytest.raises(AnswerValidationError, match="pipe"):
        decode_stored_answer(hostile, '["A|B"]')


def test_unknown_question_id_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "ghost_question", "x", config_hash=live_hash)

    with pytest.raises(ExportError, match=r"unknown question 'ghost_question'"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_unknown_timepoint_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    # timepoint 30 is not in the study config
    db.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "
        "VALUES (?, 'synth_001', 30.0, 'deterioration_6h', 'No', 'no_ai', ?)",
        (alice, live_hash),
    )
    db.commit()
    with pytest.raises(ExportError, match="timepoint 30\\.0 the study config does not know"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_unknown_patient_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    db.execute(
        "INSERT INTO answers "
        "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "
        "VALUES (?, 'synth_999', 0.0, 'deterioration_6h', 'No', 'no_ai', ?)",
        (alice, live_hash),
    )
    db.commit()
    with pytest.raises(ExportError, match=r"patient 'synth_999'"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_progress_frontier_out_of_range_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    db.execute(
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
        "VALUES (?, 'synth_001', 9, ?)",
        (alice, live_hash),
    )
    db.commit()
    with pytest.raises(ExportError, match="out of study range"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_answer_beyond_frontier_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    db.execute(
        "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
        "VALUES (?, 'synth_001', 0, ?)",
        (alice, live_hash),
    )
    db.commit()
    seed_answer(db, alice, "synth_001", 2, "deterioration_6h", "No", config_hash=live_hash)

    with pytest.raises(ExportError, match="beyond the progress frontier"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_completed_before_final_index_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    db.execute(
        "INSERT INTO progress "
        "(clinician_id, patient_id, unlocked_t_index, completed_at, config_hash) "
        "VALUES (?, 'synth_001', 0, CURRENT_TIMESTAMP, ?)",
        (alice, live_hash),
    )
    db.commit()
    with pytest.raises(ExportError, match="marked completed"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_missing_arm_assignment_is_rejected(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)

    with pytest.raises(ExportError, match="no arm assignment"):
        build(study, questions, ro_db, live_hash=live_hash)


def test_arm_mismatch_between_assignment_and_answer_is_rejected(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash, arm="ai")
    seed_answer(
        db, alice, "synth_001", 0, "deterioration_6h", "No", arm="no_ai", config_hash=live_hash
    )

    with pytest.raises(ExportError, match=r"arm mismatch.*'ai' but an answer row carries 'no_ai'"):
        build(study, questions, ro_db, live_hash=live_hash)


@pytest.mark.parametrize("table", ["answers", "progress", "arm_assignments"])
def test_foreign_config_hash_is_rejected_per_table(
    study, questions, db, ro_db, live_hash, table
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash="OTHER-GEN")
    db.execute("DELETE FROM arm_assignments")
    if table == "answers":
        seed_arm(db, alice, "synth_001", config_hash=live_hash)
        seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash="OTHER-GEN")
    elif table == "progress":
        seed_arm(db, alice, "synth_001", config_hash=live_hash)
        db.execute(
            "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
            "VALUES (?, 'synth_001', 0, 'OTHER-GEN')",
            (alice,),
        )
        db.commit()
    else:
        seed_arm(db, alice, "synth_001", config_hash="OTHER-GEN")

    with pytest.raises(ExportError, match="another study configuration") as excinfo:
        build(study, questions, ro_db, live_hash=live_hash)
    message = str(excinfo.value)
    assert table + ":" in message
    assert "OTHE" in message and "\u2026" in message  # truncated with ellipsis
    assert "OTHER-GEN" not in message  # full hash never reaches diagnostics


def test_two_foreign_hashes_are_both_listed(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash="AAAA-ONE-X")
    seed_answer(db, alice, "synth_001", 1, "deterioration_6h", "No", config_hash="BBBB-TWO-Y")

    with pytest.raises(ExportError) as excinfo:
        build(study, questions, ro_db, live_hash=live_hash)
    message = str(excinfo.value)
    assert "AAAA\u2026" in message and "BBBB\u2026" in message  # one line per foreign hash
    assert "2 rows" not in message  # one row per hash, not a merged count


def test_corrupt_free_text_names_cell_and_hides_content(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    secret_text = "top-secret-needle"
    seed_answer(db, alice, "synth_001", 1, "free_notes", secret_text + " ", config_hash=live_hash)

    with pytest.raises(ExportError) as excinfo:
        build(study, questions, ro_db, live_hash=live_hash)
    message = str(excinfo.value)
    assert "synth_001" in message
    assert alice in message
    assert "free_notes" in message
    assert "60.0" in message
    assert secret_text not in message


def test_corrupt_likert_names_cell(study, questions, db, ro_db, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "confidence", "07", config_hash=live_hash)

    with pytest.raises(ExportError) as excinfo:
        build(study, questions, ro_db, live_hash=live_hash)
    message = str(excinfo.value)
    assert "confidence" in message and "0.0" in message and "07" not in message.replace("0.0", "")


# ---------------------------------------------------------------------------
# Formula-injection guard (items 39-46)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "trigger", ["=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20"]
)
def test_guard_prefixes_every_trigger(trigger: str) -> None:
    assert guard_cell(trigger + "payload") == "'" + trigger + "payload"


def test_guard_leaves_empty_string_unchanged() -> None:
    assert guard_cell("") == ""


def test_guard_leaves_leading_apostrophe_formulas_untouched() -> None:
    assert guard_cell("'=1+1") == "'=1+1"
    assert guard_cell("'@x") == "'@x"


def test_guard_ignores_triggers_past_the_first_column() -> None:
    assert guard_cell("a=1+1") == "a=1+1"


def test_guard_applies_to_metadata_and_question_cells(
    study, questions, db, ro_db, tmp_path, live_hash
) -> None:
    # A hostile patient id (operator's own study config) and a hostile
    # free-note: the guard — not the capture path — must defuse them.
    evil_pid = "=HYPERLINK(1)"
    hostile_study = study.model_copy(deep=True)
    hostile_study.patient_ids = [evil_pid]

    alice = seed_clinician(db, "Alice")
    seed_answer(db, alice, evil_pid, 0, "free_notes", "@SUM(A1:A5)", config_hash=live_hash)
    seed_arm(db, alice, evil_pid, config_hash=live_hash)

    frame = build(hostile_study, questions, ro_db, live_hash=live_hash).frame
    # The bundle is UNGUARDED...
    row = frame.rows[0]
    assert row[0] == "=HYPERLINK(1)"
    assert row[list(frame.header).index("free_notes")] == "@SUM(A1:A5)"
    # ...and only write_csv installs the guard.
    out = tmp_path / "out.csv"
    write_csv(frame, out)
    parsed = list(csv.reader(out.open(newline="")))
    assert parsed[1][0] == "'=HYPERLINK(1)"
    assert parsed[1][list(parsed[0]).index("free_notes")] == "'@SUM(A1:A5)"


def test_guard_applies_to_headers(tmp_path) -> None:
    from ehr_simulator.export import ExportFrame, ExportReport

    frame = ExportFrame(
        header=("@attn", "note"),
        rows=(("+x", "-y"),),
        report=ExportReport(
            rows=1,
            columns=2,
            patients=0,
            clinicians=0,
            complete_walks=0,
            in_progress_walks=0,
            config_hash="h",
        ),
    )
    out = tmp_path / "hdr.csv"
    write_csv(frame, out)
    parsed = list(csv.reader(out.open(newline="")))
    assert parsed[0] == ["'@attn", "note"]
    assert parsed[1] == ["'+x", "'-y"]


def test_csv_round_trips_utf8_commas_quotes_and_newlines(
    study, questions, db, ro_db, tmp_path, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(
        db,
        alice,
        "synth_001",
        0,
        "free_notes",
        'Stable — café, "quoted" notes\nover two lines.',
        config_hash=live_hash,
    )

    out = tmp_path / "out.csv"
    write_csv(build(study, questions, ro_db, live_hash=live_hash).frame, out)
    parsed = list(csv.reader(out.open(newline="")))
    idx = parsed[0].index("free_notes")
    assert parsed[1][idx] == 'Stable — café, "quoted" notes\nover two lines.'
    assert len(parsed) == 2
    assert len(parsed[0]) == 14


def test_csv_reads_back_as_pandas_frame(study, questions, db, ro_db, tmp_path, live_hash) -> None:
    import pandas as pd

    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "free_notes", "=SUM(x)", config_hash=live_hash)

    out = tmp_path / "out.csv"
    frame = build(study, questions, ro_db, live_hash=live_hash).frame
    write_csv(frame, out)

    df = pd.read_csv(out, dtype=str, keep_default_na=False)
    assert list(df.columns) == list(frame.header)
    assert df.loc[0, "deterioration_6h"] == "No"
    assert df.loc[0, "free_notes"] == "'=SUM(x)"  # guarded on disk
    assert df.loc[0, "survives_hospital"] == ""


def test_csv_has_no_bom(study, questions, db, ro_db, tmp_path, live_hash) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    out = tmp_path / "out.csv"
    write_csv(build(study, questions, ro_db, live_hash=live_hash).frame, out)
    assert not out.read_bytes().startswith(b"\xef\xbb\xbf")


# ---------------------------------------------------------------------------
# Empty-DB header-only export (items 47)
# ---------------------------------------------------------------------------


def test_empty_database_yields_header_only_frame_and_keyfile(
    study, questions, ro_db, live_hash, tmp_path
) -> None:
    bundle = build(study, questions, ro_db, live_hash=live_hash, include_keyfile=True)
    assert bundle.frame.rows == ()
    assert bundle.frame.report.rows == 0
    assert bundle.keyfile_rows == ()

    out = tmp_path / "answers.csv"
    key = tmp_path / "keyfile.csv"
    write_export(bundle, out=out, keyfile=key)
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        ",".join(METADATA_COLUMNS)
        + ",deterioration_6h,survives_hospital,good_outcome_3mo,dead_6mo"
        + ",confidence,contributing_factors,free_notes"
    )
    assert key.read_text().splitlines() == ["clinician_id,name_normalized"]


# ---------------------------------------------------------------------------
# Keyfile (items 48-51)
# ---------------------------------------------------------------------------


def test_clinician_lookup_skipped_when_keyfile_not_requested(
    study, questions, db, ro_db, live_hash, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)

    def fail(*_a, **_k):
        raise AssertionError("clinicians DAO must not be called without --keyfile")

    monkeypatch.setattr(clinicians, "fetch_by_ids", fail)
    bundle = build(study, questions, ro_db, live_hash=live_hash)
    assert bundle.keyfile_rows is None


def test_keyfile_contains_exactly_the_exported_clinicians(
    study, questions, db, ro_db, live_hash
) -> None:
    alice = seed_clinician(db, "Alice")
    seed_clinician(db, "Bob")  # exists in the DB but is not in this export
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)

    bundle = build(study, questions, ro_db, live_hash=live_hash, include_keyfile=True)
    assert bundle.keyfile_rows == ((alice, "alice"),)


def test_write_keyfile_installs_mode_0600(study, tmp_path) -> None:
    keyfile = tmp_path / "keys" / "clinicians.keyfile.csv"
    write_keyfile((("id16", "Alice"),), keyfile)
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    assert keyfile.read_text() == "clinician_id,name_normalized\nid16,Alice\n"


def test_write_keyfile_refuses_duplicate_ids(tmp_path) -> None:
    with pytest.raises(ExportError, match="duplicate clinician_id"):
        write_keyfile((("dup", "A"), ("dup", "B")), tmp_path / "kf.csv")


def test_write_keyfile_refuses_non_posix(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import os as _os

    monkeypatch.setattr(_os, "name", "nt")
    with pytest.raises(ExportError, match="keyfile"):
        write_keyfile((("id16", "Alice"),), tmp_path / "kf.csv")


# ---------------------------------------------------------------------------
# Snapshot semantics + import hygiene (items 50-51)
# ---------------------------------------------------------------------------


def test_wal_snapshot_excludes_concurrent_commits(
    study, questions, db, tmp_db_path, ro_db, live_hash, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit landing between two exporter fetches must not be visible."""
    alice = seed_clinician(db, "Alice")
    seed_arm(db, alice, "synth_001", config_hash=live_hash)
    seed_answer(db, alice, "synth_001", 0, "deterioration_6h", "No", config_hash=live_hash)

    bob = seed_clinician(db, "Bob")
    writer = connect(tmp_db_path)
    real_progress_fetch = progress.fetch_all

    def rogue_progress_fetch(conn):
        writer.execute(
            "INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash) "
            "VALUES (?, 'synth_001', 0, ?)",
            (bob, live_hash),
        )
        writer.execute(
            "INSERT INTO answers "
            "(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash) "
            "VALUES (?, 'synth_001', 0.0, 'deterioration_6h', 'Yes', 'no_ai', ?)",
            (bob, live_hash),
        )
        writer.execute(
            "INSERT INTO arm_assignments (clinician_id, patient_id, arm, arm_source, config_hash) "
            "VALUES (?, 'synth_001', 'no_ai', 'test', ?)",
            (bob, live_hash),
        )
        writer.commit()  # lands mid-export, between the first two fetches
        return real_progress_fetch(conn)

    monkeypatch.setattr(progress, "fetch_all", rogue_progress_fetch)
    try:
        frame = build(study, questions, ro_db, live_hash=live_hash).frame
    finally:
        writer.close()

    # Bob is a fully-valid pair; if the exporter saw his mid-flight commit the
    # frame would carry a second clinician. The snapshot must shield it.
    assert {r[1] for r in frame.rows} == {alice}
    assert frame.report.clinicians == 1


def test_export_module_never_imports_web() -> None:
    """AST-level check: the exporter must not import the UI layer."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("ehr_simulator.export")
    assert spec is not None and spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    for name in imported:
        parts = name.split(".")
        assert "web" not in parts, name
