"""S10 divergence figure tests (specs/session10.md §5-§7):
annotation summaries (counts only), SVG rendering (no free-text contents),
arm attribution/conflict refusals, and config drift refusal.
"""

from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator import divergence
from ehr_simulator.config import (
    compute_config_hash,
    load_questions,
    load_study_config,
)

STUDY_DIR = Path(__file__).parent / "fixtures" / "study"
PID = "synth_001"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _fake_dataset(
    *,
    admitted: bool = True,
    vitals: list[float] | None = None,
    labs: list[float] | None = None,
    ai: list[float] | None = None,
    imaging: list[float] | None = None,
) -> types.SimpleNamespace:
    """The four canonical frames ``newly_visible_summary`` reads."""
    vitals_rows = [
        {"patient_id": PID, "variable": "hr" if i % 2 == 0 else "sbp", "t_minutes": t}
        for i, t in enumerate(vitals or [])
    ]
    lab_rows = [
        {"patient_id": PID, "variable": "glucose" if i % 2 == 0 else "cr", "t_minutes": t}
        for i, t in enumerate(labs or [])
    ]
    rows = vitals_rows + lab_rows
    scalar = pd.DataFrame(rows, columns=["patient_id", "variable", "t_minutes"])
    return types.SimpleNamespace(
        scalar_ts=scalar,
        admission=(
            pd.DataFrame([{"patient_id": PID, "t_minutes": 0.0, "note": "n/a"}])
            if admitted
            else pd.DataFrame(columns=["patient_id", "t_minutes"])
        ),
        imaging=pd.DataFrame(
            {"patient_id": [PID] * len(imaging or []), "t_minutes": imaging or []}
        ),
        ai_output=pd.DataFrame({"patient_id": [PID] * len(ai or []), "t_minutes": ai or []}),
    )


@pytest.fixture
def live_hash() -> str:
    return compute_config_hash(STUDY_DIR / "study_synthetic.yaml", STUDY_DIR / "questions.yaml")


@pytest.fixture
def study():
    return load_study_config(STUDY_DIR / "study_synthetic.yaml")


@pytest.fixture
def questions():
    return load_questions(STUDY_DIR / "questions.yaml")


def _seed_clinician(db: sqlite3.Connection, name: str) -> str:
    from ehr_simulator.db import clinicians

    return clinicians.lookup_or_create(db, name)


def _arm(db: sqlite3.Connection, cid: str, arm: str, *, live_hash: str) -> None:
    db.execute(
        "INSERT INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash) "
        "VALUES (?, ?, ?, 'test', NULL, ?)",
        (cid, PID, arm, live_hash),
    )
    db.commit()


def _answer(
    db: sqlite3.Connection,
    cid: str,
    *,
    t: float,
    qid: str,
    value: str,
    arm: str,
    live_hash: str,
) -> None:
    from ehr_simulator.db import answers

    answers.upsert(
        db,
        clinician_id=cid,
        patient_id=PID,
        timepoint=t,
        question_id=qid,
        value=value,
        arm=arm,
        config_hash=live_hash,
    )


def _timing_pair(db: sqlite3.Connection, cid: str, *, enter_ts: str, exit_ts: str) -> None:
    """Fixed server_ts pair → a known integer elapsed_seconds."""
    db.execute(
        "INSERT INTO events "
        "(session_id, clinician_id, patient_id, timepoint, kind, payload_json, server_ts) "
        "VALUES (NULL, ?, ?, 0.0, 'timepoint.enter', '{}', ?)",
        (cid, PID, enter_ts),
    )
    db.execute(
        "INSERT INTO events "
        "(session_id, clinician_id, patient_id, timepoint, kind, payload_json, server_ts) "
        "VALUES (NULL, ?, ?, 0.0, 'timepoint.exit', '{}', ?)",
        (cid, PID, exit_ts),
    )
    db.commit()


def _synthetic_dataset():
    from ehr_simulator.cli_support import build_dataset_loader

    return build_dataset_loader(load_study_config(STUDY_DIR / "study_synthetic.yaml"))()


def _render(
    db: sqlite3.Connection, study, questions, *, live_hash: str, out_dir: Path, patient: str = PID
) -> Path:
    out = out_dir / ("divergence_" + patient + ".svg")
    fig = divergence.build_divergence_figure(
        db,
        study=study,
        questions=questions,
        live_hash=live_hash,
        patient_id=patient,
        dataset=_synthetic_dataset(),
    )
    fig.save(str(out))
    return out


# ---------------------------------------------------------------------------
# annotations (spec §7)
# ---------------------------------------------------------------------------


def test_summary_counts_only_and_orders_categories() -> None:
    ds = _fake_dataset(admitted=True, vitals=[0.0, 30.0, 60.0], labs=[45.0])
    got = divergence.newly_visible_summary(ds, PID, [0.0, 60.0, 180.0])
    assert got == [
        "new: admission · vitals 1",
        "new: vitals 2 · labs 1",
        "no new data",
    ]


def test_summary_includes_ai_and_imaging() -> None:
    ds = _fake_dataset(admitted=False, ai=[10.0], imaging=[170.0])
    got = divergence.newly_visible_summary(ds, PID, [0.0, 60.0, 180.0])
    assert got[0] == "no new data"
    assert got[1] == "new: AI 1"
    assert got[2] == "new: imaging 1"


def test_rows_beyond_last_timepoint_are_excluded() -> None:
    ds = _fake_dataset(admitted=False, vitals=[500.0], ai=[1000.0])
    got = divergence.newly_visible_summary(ds, PID, [0.0, 60.0, 180.0])
    assert got == ["no new data", "no new data", "no new data"]


def test_other_patient_rows_are_ignored() -> None:
    ds = _fake_dataset(admitted=False, vitals=[5.0])
    ds.scalar_ts.loc[:, "patient_id"] = "other_pid"
    got = divergence.newly_visible_summary(ds, PID, [0.0, 60.0])
    assert got == ["no new data", "no new data"]


def test_raw_values_never_appear_in_annotations() -> None:
    ds = _fake_dataset(admitted=False, vitals=[5.0])
    got = divergence.newly_visible_summary(ds, PID, [0.0, 60.0, 180.0])
    assert all(len(s) < 60 for s in got)  # counts only, no clinical text
    assert "42.5" not in " ".join(got)


# ---------------------------------------------------------------------------
# SVG build (spec §5-§6)
# ---------------------------------------------------------------------------


def test_build_renders_svg_without_freetext_values(
    db: sqlite3.Connection, study, questions, live_hash: str, tmp_path: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)

    a = _seed_clinician(db, "Dr. Arm A")
    b = _seed_clinician(db, "Dr. Arm B")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _arm(db, b, "ai", live_hash=live_hash)

    secret = "SECRET_NOTE_do_not_render"
    for cid, arm in ((a, "no_ai"), (b, "ai")):
        _answer(db, cid, t=60.0, qid="deterioration_6h", value="Yes", arm=arm, live_hash=live_hash)
        _answer(db, cid, t=60.0, qid="survives_hospital", value="No", arm=arm, live_hash=live_hash)
        _answer(db, cid, t=60.0, qid="good_outcome_3mo", value="55", arm=arm, live_hash=live_hash)
        _answer(db, cid, t=60.0, qid="confidence", value="4", arm=arm, live_hash=live_hash)
        _answer(
            db,
            cid,
            t=60.0,
            qid="contributing_factors",
            value=json.dumps(["Imaging", "Vitals"]),
            arm=arm,
            live_hash=live_hash,
        )
    _answer(db, a, t=60.0, qid="free_notes", value=secret, arm="no_ai", live_hash=live_hash)

    # Deterministic timing for the timing panel.
    _timing_pair(db, a, enter_ts="2026-09-18 14:00:00", exit_ts="2026-09-18 14:02:05")
    _timing_pair(db, b, enter_ts="2026-09-18 14:00:00", exit_ts="2026-09-18 15:00:00")

    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)

    assert svg.startswith("<?xml") or "<svg" in svg
    assert PID in svg
    assert divergence.WALL_CLOCK_LABEL in svg
    assert "Secret" not in svg and secret not in svg  # free-text never rendered


def test_single_arm_data_renders_the_note(
    db: sqlite3.Connection, study, questions, live_hash: str, tmp_path: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    a = _seed_clinician(db, "Dr. Solo")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _answer(db, a, t=0.0, qid="deterioration_6h", value="Unknown", arm="no_ai", live_hash=live_hash)

    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert "no between-arm comparison available" in svg


def test_free_text_panel_shows_nonempty_count_not_values(
    db: sqlite3.Connection, study, questions, live_hash: str, tmp_path: Path
) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    a = _seed_clinician(db, "Dr. FT A")
    b = _seed_clinician(db, "Dr. FT B")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _arm(db, b, "ai", live_hash=live_hash)
    _answer(
        db, a, t=0.0, qid="free_notes", value="unique_token_A99", arm="no_ai", live_hash=live_hash
    )
    # B leaves the free-text blank.
    _answer(db, b, t=0.0, qid="deterioration_6h", value="No", arm="ai", live_hash=live_hash)

    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert "unique_token_A99" not in svg
    assert "non-empty responses" in svg  # count series label is present


# ---------------------------------------------------------------------------
# refusal paths (spec §5-§6)
# ---------------------------------------------------------------------------


def test_unknown_patient_refuses(db, study, questions, live_hash, tmp_path: Path) -> None:
    with pytest.raises(divergence.DivergenceError, match="not in the study"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path, patient="ghost_999")


def test_config_hash_drift_refuses(db, study, questions, tmp_path: Path) -> None:
    a = _seed_clinician(db, "Dr. Drift")
    _arm(db, a, "no_ai", live_hash="deadbeefdeadbeef")
    _answer(
        db, a, t=0.0, qid="deterioration_6h", value="Yes", arm="no_ai", live_hash="deadbeefdeadbeef"
    )
    with pytest.raises(divergence.DivergenceError, match="config-hash drift"):
        _render(db, study, questions, live_hash="0000000000000000", out_dir=tmp_path)


def test_conflicting_arm_refuses(db, study, questions, live_hash, tmp_path: Path) -> None:
    a = _seed_clinician(db, "Dr. Conflict")
    _arm(db, a, "ai", live_hash=live_hash)
    _answer(db, a, t=0.0, qid="deterioration_6h", value="Yes", arm="no_ai", live_hash=live_hash)
    with pytest.raises(divergence.DivergenceError, match="arm"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)


def test_unknown_question_id_refuses(db, study, questions, live_hash, tmp_path: Path) -> None:
    a = _seed_clinician(db, "Dr. StaleQ")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _answer(db, a, t=0.0, qid="old_question_id", value="x", arm="no_ai", live_hash=live_hash)
    with pytest.raises(divergence.DivergenceError, match="unknown question"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)


def test_invalid_stored_value_refuses(db, study, questions, live_hash, tmp_path: Path) -> None:
    a = _seed_clinician(db, "Dr. BadVal")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _answer(db, a, t=0.0, qid="confidence", value="9", arm="no_ai", live_hash=live_hash)
    with pytest.raises(divergence.DivergenceError, match="decode"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)


def test_answer_at_unconfigured_timepoint_refuses(
    db, study, questions, live_hash, tmp_path: Path
) -> None:
    a = _seed_clinician(db, "Dr. BadTP")
    _arm(db, a, "no_ai", live_hash=live_hash)
    _answer(db, a, t=55.0, qid="deterioration_6h", value="Yes", arm="no_ai", live_hash=live_hash)
    with pytest.raises(divergence.DivergenceError, match="timepoint"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)


def test_no_answers_at_all_still_renders(db, study, questions, live_hash, tmp_path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg", force=True)
    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert "<svg" in svg or svg.startswith("<?xml")


# ---------------------------------------------------------------------------
# S10-fix regressions (data/s10_fixes.md §8)
# ---------------------------------------------------------------------------


def test_assignment_only_drift_refuses(db, study, questions, live_hash, tmp_path: Path) -> None:
    """Zero answers + an arm assignment recorded under a stale config must
    still refuse — the drift check cannot key on answers alone."""
    a = _seed_clinician(db, "Dr. StaleArm")
    db.execute(
        "INSERT INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash) "
        "VALUES (?, ?, 'no_ai', 'test', NULL, 'stale-hash-000')",
        (a, PID),
    )
    db.commit()
    with pytest.raises(divergence.DivergenceError, match="config-hash drift"):
        _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)


def test_empty_panels_render_visible_placeholders(
    db, study, questions, live_hash, tmp_path: Path
) -> None:
    """One clinician, zero answers: every question panel and the timing
    panel must show a visible placeholder, never a blank facet."""
    a = _seed_clinician(db, "Dr. Blank")
    _arm(db, a, "no_ai", live_hash=live_hash)
    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert svg.count("no responses") >= len(questions.questions)
    assert "no completed timing intervals" in svg


def test_facet_order_is_questions_then_annotation_then_timing(
    db, study, questions, live_hash, tmp_path: Path
) -> None:
    """Panels render top-to-bottom: every question (config order), then the
    newly-visible-data annotation strip, then the timing panel last."""
    a = _seed_clinician(db, "Dr. Order")
    _arm(db, a, "no_ai", live_hash=live_hash)
    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    labels = [q.question_id for q in questions.questions] + [
        "newly_visible_data",
        "timing",
    ]
    pos = -1
    for label in labels:
        found = svg.find(label, pos + 1)
        assert found > pos, f"panel {label!r} out of order (previous pos {pos})"
        pos = found


def test_other_scalar_variables_get_their_own_category() -> None:
    """A scalar variable outside the vitals/lab sets is summarised as
    ``other scalar`` — counted, and the variable name never appears."""
    ds = _fake_dataset(vitals=[0.0], labs=[0.0])
    ds.scalar_ts = pd.concat(
        [
            ds.scalar_ts,
            pd.DataFrame(
                {"patient_id": [PID], "variable": ["nihs_stroke_scale"], "t_minutes": [0.0]}
            ),
        ],
        ignore_index=True,
    )
    summary = divergence.newly_visible_summary(ds, PID, [0.0, 60.0])
    assert "other scalar 1" in summary[0]
    assert "nihs_stroke_scale" not in summary[0]


def test_subtitle_deduplicates_shared_arms(
    db: sqlite3.Connection, study, questions, live_hash: str, tmp_path: Path
) -> None:
    """Three clinicians in the same arm must list the arm ONCE in the
    subtitle (matching one colour per unique arm in the rendering) — the
    subtitle must not claim a second colour for the same arm."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    for name in ("Dr. One", "Dr. Two", "Dr. Three"):
        cid = _seed_clinician(db, name)
        _arm(db, cid, "no_ai", live_hash=live_hash)
        _answer(
            db, cid, t=0.0, qid="deterioration_6h", value="Yes", arm="no_ai", live_hash=live_hash
        )

    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert svg.count("no_ai = black") == 1
    assert "no_ai = red" not in svg
    assert "no_ai = blue" not in svg


def test_assignment_without_answers_pins_known_arm(
    db: sqlite3.Connection, study, questions, live_hash: str, tmp_path: Path
) -> None:
    """A clinician with an arm assignment but no answers yet must still count
    as that arm — the figure may not fall back to 'not set'."""
    import matplotlib

    matplotlib.use("Agg", force=True)
    a = _seed_clinician(db, "Dr. Assigned")
    _arm(db, a, "no_ai", live_hash=live_hash)  # no answers for this clinician

    out = _render(db, study, questions, live_hash=live_hash, out_dir=tmp_path)
    try:
        svg = out.read_text(encoding="utf-8")
    finally:
        out.unlink(missing_ok=True)
    assert "not set" not in svg
    assert "no_ai = black" in svg
