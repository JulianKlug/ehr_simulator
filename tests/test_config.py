"""Pydantic config schema + loader + canonical hash tests.

Mirrors S5 spec §9. Negative-fixture tests are parametrized per
/plan-eng-review tension D so failure output reads
``test_study_config_rejects[bad_time_unit]`` and case count stays grep-friendly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ehr_simulator.config import (
    CategoricalQuestion,
    ConfigError,
    FreeTextQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    ProbabilityQuestion,
    Questions,
    StudyConfig,
    compute_config_hash,
    load_questions,
    load_study_config,
)

# ---------------------------------------------------------------------------
# Schema-version + loader plumbing
# ---------------------------------------------------------------------------


def test_load_study_config_happy_path(study_fixture_dir: Path) -> None:
    study = load_study_config(study_fixture_dir / "study_synthetic.yaml")
    assert study.dataset == "synthetic"
    assert study.patient_ids == ["synth_001", "synth_002", "synth_003"]
    assert study.timepoints_minutes == [0.0, 60.0, 180.0]
    assert isinstance(study, StudyConfig)


def test_load_study_config_rejects_missing_schema_version(study_fixture_dir: Path) -> None:
    path = study_fixture_dir / "study_broken_missing_schema_version.yaml"
    with pytest.raises(ConfigError) as excinfo:
        load_study_config(path)
    msg = str(excinfo.value)
    assert "schema_version" in msg
    assert path.name in msg


def test_load_study_config_rejects_wrong_schema_version(study_fixture_dir: Path) -> None:
    path = study_fixture_dir / "study_broken_wrong_schema_version.yaml"
    with pytest.raises(ConfigError) as excinfo:
        load_study_config(path)
    msg = str(excinfo.value)
    assert "'1'" in msg
    assert "'2'" in msg


# ---------------------------------------------------------------------------
# StudyConfig field validation — parametrized
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("yaml_text", "expected_field"),
    [
        pytest.param(
            """
schema_version: "1"
dataset: omop
patient_ids: [pid_001]
time_unit: minutes
timepoints: [0, 60]
""",
            "dataset",
            id="unknown_dataset",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: days
timepoints: [0, 60]
""",
            "time_unit",
            id="bad_time_unit",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001, synth_002, synth_001]
time_unit: minutes
timepoints: [0, 60]
""",
            "patient_ids",
            id="duplicate_patient_ids",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: geneva
csv_path: /tmp/x.csv
patient_ids: [g_001]
time_unit: minutes
timepoints: [0, 60]
""",
            "csv_path",
            id="unpaired_path_overrides",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: synthetic
csv_path: /tmp/x.csv
params_dir: /tmp
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0, 60]
""",
            "synthetic",
            id="synthetic_with_csv_path_forbidden",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [-1, 60]
""",
            "timepoints",
            id="negative_timepoint",
        ),
        pytest.param(
            """
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [60, 0]
""",
            "timepoints",
            id="unsorted_timepoints",
        ),
    ],
)
def test_study_config_rejects(
    tmp_path: Path,
    yaml_text: str,
    expected_field: str,
) -> None:
    path = tmp_path / "study.yaml"
    path.write_text(yaml_text.strip() + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_study_config(path)
    assert expected_field in str(excinfo.value)


def test_study_config_timepoints_minutes_property() -> None:
    study = StudyConfig(
        schema_version="1",
        dataset="synthetic",
        patient_ids=["synth_001"],
        time_unit="hours",
        timepoints=[0, 1, 24],
    )
    assert study.timepoints_minutes == [0.0, 60.0, 1440.0]


# ---------------------------------------------------------------------------
# extra="forbid" + relative-path resolution
# ---------------------------------------------------------------------------


def test_study_config_extra_forbid_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "study.yaml"
    path.write_text(
        """
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0]
unknown_field: 1
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_study_config(path)
    assert "unknown_field" in str(excinfo.value)


def test_load_study_config_resolves_relative_paths_against_yaml_dir(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "foo.csv").write_text("", encoding="utf-8")
    study_path = tmp_path / "study.yaml"
    study_path.write_text(
        """
schema_version: "1"
dataset: geneva
csv_path: data/foo.csv
params_dir: data
patient_ids: [g_001]
time_unit: minutes
timepoints: [0, 60]
""".strip()
        + "\n",
        encoding="utf-8",
    )
    study = load_study_config(study_path)
    assert study.csv_path == (tmp_path / "data" / "foo.csv").resolve()
    assert study.params_dir == (tmp_path / "data").resolve()


# ---------------------------------------------------------------------------
# Questions schema
# ---------------------------------------------------------------------------


def test_load_questions_happy_path_all_5_primitives(study_fixture_dir: Path) -> None:
    questions = load_questions(study_fixture_dir / "questions.yaml")
    assert isinstance(questions, Questions)
    assert len(questions.questions) == 7
    by_id = {q.question_id: q for q in questions.questions}
    assert isinstance(by_id["deterioration_6h"], CategoricalQuestion)
    assert isinstance(by_id["good_outcome_3mo"], ProbabilityQuestion)
    assert isinstance(by_id["confidence"], LikertQuestion)
    assert isinstance(by_id["contributing_factors"], MultiSelectQuestion)
    assert isinstance(by_id["free_notes"], FreeTextQuestion)


@pytest.mark.parametrize(
    ("fixture_or_yaml", "is_fixture", "expected_token"),
    [
        pytest.param(
            "questions_broken_categorical_no_options.yaml",
            True,
            "options",
            id="categorical_no_options",
        ),
        pytest.param(
            "questions_broken_likert_no_scale.yaml",
            True,
            "scale_min",
            id="likert_no_scale",
        ),
        pytest.param(
            "questions_broken_duplicate_id.yaml",
            True,
            "question_id",
            id="duplicate_question_id",
        ),
        pytest.param(
            "questions_broken_duplicate_options.yaml",
            True,
            "options",
            id="duplicate_options",
        ),
        pytest.param(
            """
schema_version: "1"
questions:
  - question_id: q1
    prompt: hi
    response_type: gut-feeling
""",
            False,
            "response_type",
            id="unknown_response_type",
        ),
        pytest.param(
            """
schema_version: "1"
questions:
  - question_id: BadID
    prompt: hi
    response_type: free-text
""",
            False,
            "question_id",
            id="question_id_with_uppercase",
        ),
        pytest.param(
            """
schema_version: "1"
questions:
  - question_id: bad id
    prompt: hi
    response_type: free-text
""",
            False,
            "question_id",
            id="question_id_with_space",
        ),
        pytest.param(
            """
schema_version: "1"
questions:
  - question_id: q1
    prompt: hi
    response_type: likert
    scale_min: 5
    scale_max: 5
""",
            False,
            "scale_min",
            id="likert_scale_min_eq_max",
        ),
    ],
)
def test_questions_rejects(
    tmp_path: Path,
    study_fixture_dir: Path,
    fixture_or_yaml: str,
    is_fixture: bool,
    expected_token: str,
) -> None:
    if is_fixture:
        path = study_fixture_dir / fixture_or_yaml
    else:
        path = tmp_path / "questions.yaml"
        path.write_text(fixture_or_yaml.strip() + "\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_questions(path)
    assert expected_token in str(excinfo.value)


def test_questions_extra_forbid_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "questions.yaml"
    path.write_text(
        """
schema_version: "1"
unknown_field: 1
questions:
  - question_id: q1
    prompt: hi
    response_type: free-text
""".strip()
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError) as excinfo:
        load_questions(path)
    assert "unknown_field" in str(excinfo.value)


# ---------------------------------------------------------------------------
# compute_config_hash — parametrized invariance + sensitivity
# ---------------------------------------------------------------------------


def _write_pair(tmp_path: Path, study_yaml: str, questions_yaml: str) -> tuple[Path, Path]:
    s = tmp_path / "study.yaml"
    q = tmp_path / "questions.yaml"
    s.write_text(study_yaml, encoding="utf-8")
    q.write_text(questions_yaml, encoding="utf-8")
    return s, q


_BASE_STUDY = """schema_version: "1"
dataset: synthetic
patient_ids: [synth_001, synth_002]
time_unit: minutes
timepoints: [0, 60]
"""

_BASE_QUESTIONS = """schema_version: "1"
questions:
  - question_id: q1
    prompt: "First question"
    response_type: free-text
  - question_id: q2
    prompt: "Second question"
    response_type: categorical
    options: [Yes, No]
"""


def test_compute_config_hash_stable_across_invocations(tmp_path: Path) -> None:
    s, q = _write_pair(tmp_path, _BASE_STUDY, _BASE_QUESTIONS)
    h1 = compute_config_hash(s, q)
    h2 = compute_config_hash(s, q)
    assert h1 == h2
    assert len(h1) == 64


@pytest.mark.parametrize(
    "mutation",
    ["whitespace_trailing_newline", "whitespace_lf_to_crlf", "yaml_key_reorder"],
)
def test_compute_config_hash_invariant_under(tmp_path: Path, mutation: str) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a_s, a_q = _write_pair(a_dir, _BASE_STUDY, _BASE_QUESTIONS)

    if mutation == "whitespace_trailing_newline":
        b_study = _BASE_STUDY + "\n\n"
        b_questions = _BASE_QUESTIONS + "\n"
    elif mutation == "whitespace_lf_to_crlf":
        b_study = _BASE_STUDY.replace("\n", "\r\n")
        b_questions = _BASE_QUESTIONS.replace("\n", "\r\n")
    elif mutation == "yaml_key_reorder":
        b_study = """timepoints: [0, 60]
time_unit: minutes
patient_ids: [synth_001, synth_002]
dataset: synthetic
schema_version: "1"
"""
        b_questions = _BASE_QUESTIONS
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    b_s, b_q = _write_pair(b_dir, b_study, b_questions)
    assert compute_config_hash(a_s, a_q) == compute_config_hash(b_s, b_q)


@pytest.mark.parametrize(
    "mutation",
    [
        "added_patient_id",
        "different_timepoint",
        "edited_question_prompt",
        "different_dataset",
    ],
)
def test_compute_config_hash_changes_on(tmp_path: Path, mutation: str) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    a_dir.mkdir()
    b_dir.mkdir()
    a_s, a_q = _write_pair(a_dir, _BASE_STUDY, _BASE_QUESTIONS)

    if mutation == "added_patient_id":
        b_study = _BASE_STUDY.replace("[synth_001, synth_002]", "[synth_001, synth_002, synth_003]")
        b_questions = _BASE_QUESTIONS
    elif mutation == "different_timepoint":
        b_study = _BASE_STUDY.replace("[0, 60]", "[0, 120]")
        b_questions = _BASE_QUESTIONS
    elif mutation == "edited_question_prompt":
        b_study = _BASE_STUDY
        b_questions = _BASE_QUESTIONS.replace('"First question"', '"First question (rev)"')
    elif mutation == "different_dataset":
        b_study = _BASE_STUDY.replace(
            "dataset: synthetic\n",
            "dataset: geneva\ncsv_path: ../geneva.csv\nparams_dir: ..\n",
        )
        b_questions = _BASE_QUESTIONS
    else:
        raise AssertionError(f"unknown mutation {mutation}")

    b_s, b_q = _write_pair(b_dir, b_study, b_questions)
    assert compute_config_hash(a_s, a_q) != compute_config_hash(b_s, b_q)
