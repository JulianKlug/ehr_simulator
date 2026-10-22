"""S12a bundle reader: the committed S11n bundles load; every tampering refuses.

Spec ``specs/session-12-phase25-release.md`` §4, §10 tests 1–4, 13, 15.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr_simulator.analysis import AnalysisError, load_phase2_bundle
from tests.support.analysis_bundle import (
    EMPTY,
    FIXTURES,
    WALKED,
    Digest,
    Row,
    copy_fixture,
    edit_csv,
    edit_manifest,
    read_rows,
    snapshot,
    write_rows,
)

TIMEPOINTS = "timepoints.csv"
ANSWERS = "answers.csv"
PANELS = "panel_summaries.csv"
EVENTS = "behavioral_events.csv"
AUDIT = "randomisation_audit.csv"
HISTORY = "configuration_history.csv"
COUNTS = "configuration_counts.csv"
PRACTICE = ("practice_timepoints.csv", "practice_answers.csv")
CASES = 6


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path)


def _refuses(bundle: Path, match: str) -> None:
    with pytest.raises(AnalysisError, match=match):
        load_phase2_bundle(bundle)


def _first(rows: list[Row], **match: str) -> Row:
    return next(r for r in rows if all(r[k] == v for k, v in match.items()))


# ---------------------------------------------------------------------------
# Valid bundles
# ---------------------------------------------------------------------------


def test_committed_bundle_loads_mixed_versions() -> None:
    loaded = load_phase2_bundle(FIXTURES / WALKED)

    assert list(loaded.versions) == ["v1", "v2"]
    assert len(loaded.cases) == CASES
    assert set(loaded.cases["config_version"]) == {"v1", "v2"}
    assert set(loaded.cases["arm"]) == {"ai", "no_ai"}
    for case in loaded.cases.itertuples():
        pinned = loaded.versions[case.config_version]
        assert case.config_hash == pinned.config_hash


def test_answers_decode_against_their_historical_question() -> None:
    answers = load_phase2_bundle(FIXTURES / WALKED).answers

    multi = answers[answers["question_id"] == "supporting_findings"]
    guarded = multi[multi["response_value"].str.startswith("'")]
    assert not guarded.empty
    assert all(o == ("+ new imaging finding", "Laboratory trend") for o in guarded["value_options"])
    assert set(answers.loc[answers["question_id"] == "supporting_findings", "config_version"]) == {
        "v2"
    }

    likert = answers[(answers["question_id"] == "confidence") & answers["value_exported"]]
    assert likert["value_number"].notna().all()
    hidden = answers[answers["branch_state"] == "hidden"]
    assert hidden["value_options"].isna().all() and hidden["value_number"].isna().all()


def test_empty_bundle_loads_without_cases() -> None:
    loaded = load_phase2_bundle(FIXTURES / EMPTY)

    assert not loaded.has_cases
    assert loaded.answers.empty and loaded.timepoints.empty
    assert list(loaded.versions) == ["v1"]


def test_declared_practice_files_are_checked_then_ignored(bundle: Path) -> None:
    baseline = load_phase2_bundle(bundle)
    for name in PRACTICE:
        header, _rows = read_rows(FIXTURES / WALKED, ANSWERS)
        practice_header = (
            header + ["observation_mode"] if name == "practice_answers.csv" else _practice_header()
        )
        write_rows(bundle, name, practice_header, [])
    edit_manifest(bundle, lambda m: m.__setitem__("practice_included", True))

    loaded = load_phase2_bundle(bundle)

    assert loaded.practice_declared
    assert loaded.cases.equals(baseline.cases)
    assert loaded.answers.equals(baseline.answers)


def _practice_header() -> list[str]:
    from ehr_simulator.analysis.schema import OPTIONAL_PRACTICE_FILES, header

    return list(header(OPTIONAL_PRACTICE_FILES["practice_timepoints.csv"]))


def test_reader_never_writes_the_input(bundle: Path) -> None:
    before = snapshot(bundle)
    load_phase2_bundle(bundle)
    edit_csv(bundle, TIMEPOINTS, lambda rows: rows[:-1])
    tampered = snapshot(bundle)

    with pytest.raises(AnalysisError):
        load_phase2_bundle(bundle)

    assert snapshot(bundle) == tampered
    assert before.keys() == tampered.keys()


# ---------------------------------------------------------------------------
# Manifest and files (tests 1–3)
# ---------------------------------------------------------------------------


def test_refuses_another_export_schema(bundle: Path) -> None:
    edit_manifest(bundle, lambda m: m.__setitem__("export_schema_version", "phase2_export_v2"))
    _refuses(bundle, "export schema")


def test_refuses_a_missing_manifest_key(bundle: Path) -> None:
    edit_manifest(bundle, lambda m: m.pop("practice_included"))
    _refuses(bundle, "keys")


def test_refuses_a_missing_file(bundle: Path) -> None:
    (bundle / PANELS).unlink()
    _refuses(bundle, "missing")


def test_refuses_a_digest_mismatch(bundle: Path) -> None:
    edit_csv(bundle, ANSWERS, lambda rows: None, digest=Digest.KEEP)  # same rows, same bytes
    load_phase2_bundle(bundle)

    def blank_ts(rows: list[Row]) -> None:
        rows[0]["ts_recorded"] = ""

    edit_csv(bundle, ANSWERS, blank_ts, digest=Digest.KEEP)
    _refuses(bundle, "SHA256")


def test_refuses_an_undeclared_file(bundle: Path) -> None:
    (bundle / "notes.txt").write_text("x")
    _refuses(bundle, "undeclared")


def test_refuses_a_declared_file_outside_the_contract(bundle: Path) -> None:
    edit_manifest(bundle, lambda m: m["files"].__setitem__("extra.csv", m["files"][COUNTS]))
    _refuses(bundle, "outside the contract")


def test_refuses_practice_included_without_its_files(bundle: Path) -> None:
    edit_manifest(bundle, lambda m: m.__setitem__("practice_included", True))
    _refuses(bundle, "does not declare required files")


def test_refuses_a_row_count_mismatch(bundle: Path) -> None:
    edit_manifest(bundle, lambda m: m["files"][TIMEPOINTS].__setitem__("rows", 1))
    _refuses(bundle, "data rows")


def test_refuses_a_header_change(bundle: Path) -> None:
    header, rows = read_rows(bundle, TIMEPOINTS)
    renamed = ["arm_itt" if c == "arm" else c for c in header]
    write_rows(bundle, TIMEPOINTS, renamed, [{**r, "arm_itt": r["arm"]} for r in rows])
    _refuses(bundle, "header")


@pytest.mark.parametrize("name", [TIMEPOINTS, ANSWERS, PANELS, AUDIT])
def test_refuses_duplicate_key_rows(bundle: Path, name: str) -> None:
    edit_csv(bundle, name, lambda rows: [*rows, rows[0]])
    _refuses(bundle, "duplicate key|activated one patient twice")


# ---------------------------------------------------------------------------
# Cells: unknown is never false or zero
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("timepoint_reached", ""),  # a required boolean is never blank
        ("ai_delivered", "unknown"),  # unknown is "", never a word
        ("elapsed_seconds", "nan"),
        ("foreground_seconds", "-1.0"),
        ("arm", "AI"),
        ("t_index", "-1"),
    ],
)
def test_refuses_invalid_cells(bundle: Path, column: str, value: str) -> None:
    def tamper(rows: list[Row]) -> None:
        rows[0][column] = value

    edit_csv(bundle, TIMEPOINTS, tamper)
    _refuses(bundle, f"column '{column}'")


# ---------------------------------------------------------------------------
# Provenance and joins (test 4)
# ---------------------------------------------------------------------------


def test_refuses_an_unknown_config_version(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        for r in rows:
            r["config_version"] = "v9" if r["config_version"] == "v2" else r["config_version"]

    edit_csv(bundle, PANELS, tamper)
    _refuses(bundle, "not registered")


def test_refuses_config_hash_drift_on_one_row(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        rows[0]["config_hash"] = "0" * 64

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "not registered or its config_hash drifted")


def test_refuses_a_history_hash_that_does_not_match_its_snapshots(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        row = _first(rows, config_version="v2")
        row["questions_json"] = row["questions_json"].replace(
            "Which findings support", "Which findings back"
        )

    edit_csv(bundle, HISTORY, tamper)
    _refuses(bundle, "config_hash does not match its snapshots")


def test_refuses_a_case_with_two_versions(bundle: Path) -> None:
    _, history = read_rows(bundle, HISTORY)
    v2_hash = _first(history, config_version="v2")["config_hash"]

    def tamper(rows: list[Row]) -> None:
        row = _first(rows, config_version="v1", t_index="2")
        row["config_version"], row["config_hash"] = "v2", v2_hash

    edit_csv(bundle, TIMEPOINTS, tamper)
    _refuses(bundle, "more than one config_version")


def test_refuses_an_answer_whose_timepoint_is_missing(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        rows[0]["patient_id"] = "synth_999"

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "absent from timepoints.csv")


def test_refuses_a_timepoint_without_all_its_questions(bundle: Path) -> None:
    edit_csv(bundle, ANSWERS, lambda rows: rows[1:])
    _refuses(bundle, "exactly its pinned questions")


def test_refuses_a_missing_timepoint(bundle: Path) -> None:
    edit_csv(bundle, TIMEPOINTS, lambda rows: [r for r in rows if r is not rows[1]])
    _refuses(bundle, "pinned timepoints")


def test_refuses_an_audit_arm_that_differs_from_the_case(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        row = _first(rows, activated="true")
        flipped = "ai" if row["realised_arm"] == "no_ai" else "no_ai"
        row["realised_arm"] = row["planned_arm"] = flipped

    edit_csv(bundle, AUDIT, tamper)
    _refuses(bundle, "differs from the case's arm")


def test_refuses_a_broken_replacement_link(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        _first(rows, lifecycle_state="incomplete")["replaced_by_patient_id"] = ""

    edit_csv(bundle, AUDIT, tamper)
    _refuses(bundle, "not linked")


def test_refuses_an_event_of_no_case(bundle: Path) -> None:
    """Every exported event belongs to an activated case (S11n events table)."""

    def tamper(rows: list[Row]) -> None:
        rows[0]["patient_id"] = "synth_999"

    edit_csv(bundle, EVENTS, tamper)
    _refuses(bundle, "event of no case")


def test_refuses_a_determinate_reached_observation_without_pp(bundle: Path) -> None:
    """Reached ∧ pp_determinate ⇒ pp_compliant known; else PP counts drop it."""

    def tamper(rows: list[Row]) -> None:
        row = _first(rows, timepoint_reached="true", pp_determinate="true")
        row["pp_compliant"] = ""

    edit_csv(bundle, TIMEPOINTS, tamper)
    _refuses(bundle, "pp_compliant")


def test_refuses_counts_that_differ_from_the_rows(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        rows[0]["completed_cases"] = str(int(rows[0]["completed_cases"]) + 1)

    edit_csv(bundle, COUNTS, tamper)
    _refuses(bundle, "completed_cases")


# ---------------------------------------------------------------------------
# Answer status and values
# ---------------------------------------------------------------------------


def test_refuses_a_hidden_question_marked_answered(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        _first(rows, branch_state="hidden")["response_status"] = "answered"

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "inconsistent answer status")


def test_refuses_an_unknown_categorical_option(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        _first(rows, question_id="deterioration_6h", value_exported="true")["response_value"] = (
            "Maybe"
        )

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "not one option")


def test_never_strips_a_leading_quote_blindly(bundle: Path) -> None:
    """``'Yes`` is not the guarded form of ``Yes`` (no formula trigger)."""

    def tamper(rows: list[Row]) -> None:
        row = _first(rows, question_id="deterioration_6h", response_value="Yes")
        row["response_value"] = "'Yes"

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "not one option")


@pytest.mark.parametrize(
    "value",
    [
        "Laboratory trend|'+ new imaging finding",  # out of historical order
        "+ new imaging finding|Laboratory trend",  # guard missing
        "Clinical worsening|Clinical worsening",  # duplicate
        "Clinical worsening|Imaging",  # not an option of v2
    ],
)
def test_refuses_invalid_multi_select_cells(bundle: Path, value: str) -> None:
    def tamper(rows: list[Row]) -> None:
        _first(rows, question_id="supporting_findings", value_exported="true")["response_value"] = (
            value
        )

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, "multi-select value")


def test_refuses_a_likert_value_outside_its_scale(bundle: Path) -> None:
    def tamper(rows: list[Row]) -> None:
        _first(rows, question_id="confidence", value_exported="true")["response_value"] = "6"

    edit_csv(bundle, ANSWERS, tamper)
    _refuses(bundle, r"integer in \[1, 5\]")
