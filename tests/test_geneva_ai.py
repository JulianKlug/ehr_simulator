"""S7 Geneva AI predictions adapter: parser, integration, S11g and leakage regressions.

Runs against the fully synthetic fixture in ``tests/fixtures/geneva_ai`` (see
its builder). Tests that mutate artifacts work on a ``tmp_path`` copy.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import pickle
import shutil
import sys
from enum import Enum
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from ehr_simulator import cli
from ehr_simulator.cli_support import build_dataset_loader, walk_preflight
from ehr_simulator.config import (
    ConfigError,
    load_questions,
    load_study_config,
    render_study_snapshot,
)
from ehr_simulator.config.study import AIInterventionConfig
from ehr_simulator.ingestion import (
    AdapterError,
    CanonicalShape,
    GenevaAISource,
    load_geneva,
    load_geneva_ai_predictions,
    validate,
)
from ehr_simulator.ingestion import geneva_ai as geneva_ai_module
from ehr_simulator.ingestion.geneva_ai import _directory_digest, _dumps, _to_json_native
from ehr_simulator.ingestion.provenance import AIArtifactProvenance
from ehr_simulator.web.panels import provenance_mismatches

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "geneva_ai"
GENEVA_PARAMS_DIR = Path(__file__).parent / "fixtures" / "geneva"
QUESTIONS_PATH = Path(__file__).parent / "fixtures" / "study" / "questions.yaml"
SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "export_geneva_test_ids.py"

sys.path.insert(0, str(FIXTURE_DIR))
import build_geneva_ai_fixture as fx  # noqa: E402

TIMESTEPS = fx.TIMESTEPS
N_PATIENTS = len(fx.TEST_IDS)
SHAP_DIR = fx.EXPLANATIONS_DIR
SHAP_VALUES = f"{SHAP_DIR}/{fx.SHAP_VALUES_FILE}"
FEATURE_NAMES = f"{SHAP_DIR}/{fx.FEATURE_NAMES_FILE}"
SENTINEL_TEXT = "918273645"  # digits of fx.Y_TRUE_SENTINEL
UNRELATED_SHA256 = "0" * 64


class Explanations(Enum):
    WITH = "with"
    WITHOUT = "without"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_dir(tmp_path: Path) -> Path:
    """A mutable copy of the fixture artifact."""
    target = tmp_path / "geneva_ai"
    shutil.copytree(FIXTURE_DIR, target, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    return target


def _source(
    directory: Path = FIXTURE_DIR, explanations: Explanations = Explanations.WITH
) -> GenevaAISource:
    return GenevaAISource(
        predictions_path=directory / fx.PREDICTIONS_NAME,
        patient_ids_path=directory / fx.SIDECAR_NAME,
        model_path=directory / fx.MODEL_NAME,
        model_id=fx.MODEL_ID,
        explanations_dir=directory / SHAP_DIR if explanations is Explanations.WITH else None,
    )


def _load(directory: Path = FIXTURE_DIR, **kwargs: Any) -> pd.DataFrame:
    return load_geneva_ai_predictions(_source(directory), **kwargs).ai_output


def _payloads(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [json.loads(raw) for raw in frame["output_json"]]


def _read_pickle(path: Path) -> Any:
    return pickle.loads(path.read_bytes())


def _write_pickle(path: Path, obj: Any) -> None:
    path.write_bytes(pickle.dumps(obj, protocol=fx.PICKLE_PROTOCOL))


def _expected_frame() -> pd.DataFrame:
    return pd.read_csv(FIXTURE_DIR / fx.EXPECTED_NAME, dtype={"patient_id": str})


def _prediction_digest(sidecar: bytes, predictions: bytes) -> str:
    entries = [
        {"role": "patient_ids", "sha256": hashlib.sha256(sidecar).hexdigest()},
        {"role": "predictions", "sha256": hashlib.sha256(predictions).hexdigest()},
    ]
    return hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _intervention(provenance: AIArtifactProvenance, **overrides: Any) -> AIInterventionConfig:
    fields = {
        "model_id": fx.MODEL_ID,
        "model_system_version": provenance.model_system_version,
        "prediction_artifact_sha256": provenance.prediction_sha256,
        "explanation_artifact_sha256": provenance.explanation_sha256,
        "presentation_version": "ai_panel_v1",
        "intervention_build_id": "fixture_build",
    }
    fields.update(overrides)
    return AIInterventionConfig(**fields)


def _study_yaml(
    tmp_path: Path,
    *,
    geneva_ai: dict[str, Any] | None,
    dataset: str = "geneva",
    extra: dict[str, Any] | None = None,
) -> Path:
    """A Geneva study over the S7 clinical fixture; relative ``geneva_ai`` paths."""
    data: dict[str, Any] = {
        "schema_version": "2",
        "study_id": "fixture_geneva_ai",
        "dataset": dataset,
        "patient_ids": list(fx.TEST_IDS[:2]),
        "time_unit": "hours",
        "timepoints": [0, 1, 2],
    }
    if dataset != "synthetic":
        data["csv_path"] = str(FIXTURE_DIR / fx.CLINICAL_NAME)
        data["params_dir"] = str(GENEVA_PARAMS_DIR)
    if geneva_ai is not None:
        data["geneva_ai"] = geneva_ai
    data.update(extra or {})
    path = tmp_path / "study.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _geneva_ai_block(
    directory: Path, explanations: Explanations = Explanations.WITH
) -> dict[str, Any]:
    """Paths relative to ``directory.parent`` (the study YAML's directory)."""
    block = {
        "predictions_path": f"{directory.name}/{fx.PREDICTIONS_NAME}",
        "patient_ids_path": f"{directory.name}/{fx.SIDECAR_NAME}",
        "model_path": f"{directory.name}/{fx.MODEL_NAME}",
        "model_id": fx.MODEL_ID,
    }
    if explanations is Explanations.WITH:
        block["explanations_dir"] = f"{directory.name}/{SHAP_DIR}"
    return block


def _phase2_extra(provenance: AIArtifactProvenance) -> dict[str, Any]:
    """Phase 2 blocks so preflight also checks the per cell AI row."""
    intervention = _intervention(provenance).model_dump()
    return {
        "randomisation": {
            "master_seed": 123,
            "block_length": 1,
            "block_sequence": ["start", "other"],
        },
        "ai_intervention": intervention,
        "clinician_facing": {"prohibited_fields": []},
        "telemetry": {
            "inactivity_threshold_seconds": 30,
            "panel_viewport_threshold": 0.5,
            "panel_viewed_threshold_seconds": 0.5,
        },
    }


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("export_geneva_test_ids", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_calls: list[str] = []


def _record_call() -> None:
    _calls.append("built")


class _Smuggled:
    def __reduce__(self) -> tuple[Any, tuple[()]]:
        return (_record_call, ())


# ---------------------------------------------------------------------------
# Unit
# ---------------------------------------------------------------------------


def test_parser_returns_patient_major_rows() -> None:
    frame = _load()

    assert len(frame) == N_PATIENTS * TIMESTEPS
    expected_ids = [pid for pid in fx.TEST_IDS for _ in range(TIMESTEPS)]
    assert frame["patient_id"].tolist() == expected_ids
    assert (frame["model_id"] == fx.MODEL_ID).all()


def test_timestep_converts_to_exact_whole_hour_minutes() -> None:
    frame = _load()

    first = frame[frame["patient_id"] == fx.TEST_IDS[0]]
    assert first["t_minutes"].tolist() == [60.0 * i for i in range(TIMESTEPS)]


def test_float32_prediction_becomes_native_json_number() -> None:
    value = np.float32(0.0731)
    native = _to_json_native(value)

    assert type(native) is float
    assert native == float(value)

    y_prob = _read_pickle(FIXTURE_DIR / fx.PREDICTIONS_NAME)[1]
    assert y_prob.dtype == np.float32
    probability = _payloads(_load())[0]["probability"]
    assert type(probability) is float
    assert probability == float(y_prob[0])


def test_shap_contributions_become_feature_map_plus_base_value() -> None:
    shap = _read_pickle(FIXTURE_DIR / SHAP_VALUES)
    payload = _payloads(_load())[TIMESTEPS + 5]  # patient 1, t = 5

    explanation = payload["explanation"]
    assert explanation["base_value"] == float(shap[5][1, -1]) == fx.BIAS
    assert explanation["contributions"] == {
        name: float(shap[5][1, j]) for j, name in enumerate(fx.FEATURE_NAMES)
    }


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_nan_or_infinite_prediction_raises(ai_dir: Path, bad: float) -> None:
    y_true, y_prob = _read_pickle(ai_dir / fx.PREDICTIONS_NAME)
    y_prob[3] = bad
    _write_pickle(ai_dir / fx.PREDICTIONS_NAME, (y_true, y_prob))

    with pytest.raises(AdapterError, match="NaN or infinite"):
        _load(ai_dir)


@pytest.mark.parametrize("bad", [np.nan, np.inf])
def test_nan_or_infinite_explanation_raises(ai_dir: Path, bad: float) -> None:
    shap = _read_pickle(ai_dir / SHAP_VALUES)
    shap[4][0, 1] = bad
    _write_pickle(ai_dir / SHAP_VALUES, shap)

    with pytest.raises(AdapterError, match="NaN or infinite"):
        _load(ai_dir)


@pytest.mark.parametrize("bad", [float("nan"), np.float32("inf"), np.array([1.0, np.nan])])
def test_normaliser_rejects_non_finite(bad: Any) -> None:
    with pytest.raises(AdapterError, match="NaN or infinite"):
        _to_json_native(bad)


def test_normaliser_converts_numpy_and_keeps_json_natives() -> None:
    value = {
        "array": np.array([1, 2], dtype=np.int64),
        "flag": np.bool_(True),
        "native_flag": False,
        "nested": {"x": np.float32(0.5)},
        "none": None,
        "text": "kept",
        "tuple": (np.int32(3),),
    }

    assert _to_json_native(value) == {
        "array": [1, 2],
        "flag": True,
        "native_flag": False,
        "nested": {"x": 0.5},
        "none": None,
        "text": "kept",
        "tuple": [3],
    }
    assert type(_to_json_native(np.bool_(True))) is bool


def test_normaliser_rejects_unsupported_objects() -> None:
    with pytest.raises(AdapterError, match="non JSON value of type object"):
        _to_json_native(object())


def test_serialisation_is_byte_stable_and_independent_of_insertion_order() -> None:
    forward = {"probability": 0.25, "explanation": {"base_value": -1.0, "contributions": {}}}
    backward = {"explanation": {"contributions": {}, "base_value": -1.0}, "probability": 0.25}

    expected = '{"explanation":{"base_value":-1.0,"contributions":{}},"probability":0.25}'
    assert _dumps(forward) == _dumps(backward) == expected
    assert _load()["output_json"].tolist() == _load()["output_json"].tolist()


def test_prediction_digest_covers_sidecar_and_predictions(ai_dir: Path) -> None:
    sidecar = (ai_dir / fx.SIDECAR_NAME).read_bytes()
    predictions = (ai_dir / fx.PREDICTIONS_NAME).read_bytes()
    provenance = load_geneva_ai_predictions(_source(ai_dir)).provenance
    assert provenance.prediction_sha256 == _prediction_digest(sidecar, predictions)

    (ai_dir / fx.SIDECAR_NAME).write_bytes(fx.sidecar_bytes(tuple(reversed(fx.TEST_IDS))))
    reordered = load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT)).provenance
    assert reordered.prediction_sha256 != provenance.prediction_sha256

    (ai_dir / fx.SIDECAR_NAME).write_bytes(sidecar)
    y_true, y_prob = _read_pickle(ai_dir / fx.PREDICTIONS_NAME)
    y_prob[0] = np.float32(0.5)
    _write_pickle(ai_dir / fx.PREDICTIONS_NAME, (y_true, y_prob))
    changed = load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT)).provenance
    assert changed.prediction_sha256 != provenance.prediction_sha256


def test_directory_digest_is_order_independent_and_tracks_every_change() -> None:
    files = {"a.pkl": b"one", "b.pkl": b"two"}
    digest = _directory_digest(files)

    assert _directory_digest({"b.pkl": b"two", "a.pkl": b"one"}) == digest
    assert _directory_digest({"a.pkl": b"one", "b.pkl": b"TWO"}) != digest  # content
    assert _directory_digest({"a.pkl": b"one", "c.pkl": b"two"}) != digest  # rename
    assert _directory_digest({**files, "c.pkl": b""}) != digest  # add
    assert _directory_digest({"a.pkl": b"one"}) != digest  # remove


def test_explanation_digest_matches_spec_formula() -> None:
    provenance = load_geneva_ai_predictions(_source()).provenance
    entries = [
        {
            "path": name,
            "sha256": hashlib.sha256((FIXTURE_DIR / SHAP_DIR / name).read_bytes()).hexdigest(),
        }
        for name in sorted([fx.FEATURE_NAMES_FILE, fx.SHAP_VALUES_FILE])
    ]
    expected = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert provenance.explanation_sha256 == expected


def _drop_last_timestep(shap: list[np.ndarray]) -> list[np.ndarray]:
    return shap[:-1]


def _drop_last_patient(shap: list[np.ndarray]) -> list[np.ndarray]:
    return [a[:-1] for a in shap]


def _drop_last_column(shap: list[np.ndarray]) -> list[np.ndarray]:
    return [a[:, :-1] for a in shap]


def _as_float64(shap: list[np.ndarray]) -> list[np.ndarray]:
    return [a.astype(np.float64) for a in shap]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop_last_timestep, "list of 72 arrays"),
        (_drop_last_patient, "shape"),
        (_drop_last_column, "shape"),
        (_as_float64, "float32"),
    ],
)
def test_wrong_shap_values_shape_raises(ai_dir: Path, mutate: Any, message: str) -> None:
    _write_pickle(ai_dir / SHAP_VALUES, mutate(_read_pickle(ai_dir / SHAP_VALUES)))

    with pytest.raises(AdapterError, match=message):
        _load(ai_dir)


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (list(fx.FEATURE_NAMES[:-1]), "shape"),
        ([fx.FEATURE_NAMES[0]] * len(fx.FEATURE_NAMES), "unique"),
        ([1, 2, 3, 4], "list of strings"),
    ],
)
def test_wrong_feature_names_raise(ai_dir: Path, names: list[Any], message: str) -> None:
    _write_pickle(ai_dir / FEATURE_NAMES, names)

    with pytest.raises(AdapterError, match=message):
        _load(ai_dir)


def test_duplicated_sidecar_id_raises(ai_dir: Path) -> None:
    duplicated = (fx.TEST_IDS[0], fx.TEST_IDS[0], fx.TEST_IDS[2])
    (ai_dir / fx.SIDECAR_NAME).write_bytes(fx.sidecar_bytes(duplicated))

    with pytest.raises(AdapterError, match="duplicate"):
        _load(ai_dir)


def test_pickle_global_outside_allowlist_raises_before_building(ai_dir: Path) -> None:
    _calls.clear()
    (ai_dir / fx.PREDICTIONS_NAME).write_bytes(pickle.dumps(_Smuggled()))

    with pytest.raises(AdapterError, match="pickle global .*_record_call is not allowed"):
        _load(ai_dir)
    assert _calls == []


def test_numpy_1_reconstruct_global_is_accepted(ai_dir: Path) -> None:
    # Protocol 3 spells globals as text, so the NumPy 1 module name can be swapped in.
    obj = _read_pickle(ai_dir / fx.PREDICTIONS_NAME)
    data = pickle.dumps(obj, protocol=3).replace(
        b"numpy._core.multiarray", b"numpy.core.multiarray"
    )
    assert b"numpy.core.multiarray" in data
    (ai_dir / fx.PREDICTIONS_NAME).write_bytes(data)

    assert len(_load(ai_dir)) == N_PATIENTS * TIMESTEPS


@pytest.mark.parametrize(
    ("obj", "message"),
    [
        (["not", "a tuple"], "2-tuple"),
        ((np.zeros(72), np.zeros(72), np.zeros(72)), "2-tuple"),
        ((np.zeros(72), np.zeros((72, 1))), "1-D"),
        ((np.zeros(72), np.zeros(144, dtype=np.float32)), "differ in length"),
        ((np.zeros(72), np.zeros(72, dtype=np.int64)), "float array"),
        ((np.zeros(72), np.full(72, 1.5, dtype=np.float32)), r"outside \[0, 1\]"),
        ((np.zeros(73), np.zeros(73, dtype=np.float32)), "multiple of 72"),
    ],
)
def test_malformed_predictions_raise(ai_dir: Path, obj: Any, message: str) -> None:
    _write_pickle(ai_dir / fx.PREDICTIONS_NAME, obj)

    with pytest.raises(AdapterError, match=message):
        load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT))


def test_empty_model_id_raises() -> None:
    source = GenevaAISource(**{**_source().__dict__, "model_id": " "})

    with pytest.raises(AdapterError, match="model_id"):
        load_geneva_ai_predictions(source)


@pytest.mark.parametrize(
    "missing",
    [fx.PREDICTIONS_NAME, fx.SIDECAR_NAME, fx.MODEL_NAME, SHAP_DIR],
)
def test_missing_artifact_raises(ai_dir: Path, missing: str) -> None:
    target = ai_dir / missing
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()

    with pytest.raises(AdapterError, match="not found"):
        _load(ai_dir)


def test_explanations_directory_drift_raises(ai_dir: Path) -> None:
    (ai_dir / SHAP_DIR / "notes.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(AdapterError, match="drift"):
        _load(ai_dir)


def test_explanations_directory_missing_file_raises(ai_dir: Path) -> None:
    (ai_dir / FEATURE_NAMES).unlink()

    with pytest.raises(AdapterError, match="drift"):
        _load(ai_dir)


def test_explanations_directory_symlink_or_subdirectory_raises(ai_dir: Path) -> None:
    (ai_dir / SHAP_DIR / "nested").mkdir()
    with pytest.raises(AdapterError, match="non regular entry"):
        _load(ai_dir)

    (ai_dir / SHAP_DIR / "nested").rmdir()
    values = ai_dir / SHAP_VALUES
    moved = ai_dir / "moved.pkl"
    values.rename(moved)
    values.symlink_to(moved)
    with pytest.raises(AdapterError, match="non regular entry"):
        _load(ai_dir)


# ---------------------------------------------------------------------------
# Patient subset and identifier regressions
# ---------------------------------------------------------------------------


def test_requested_patient_outside_test_subset_gets_no_rows() -> None:
    frame = _load(patient_ids=(fx.NON_TEST_ID,))

    assert frame.empty
    validate(frame, CanonicalShape.AI_OUTPUT, strict=True, dataset="geneva")


def test_mixed_request_keeps_only_test_subset_patients() -> None:
    frame = _load(patient_ids=(fx.NON_TEST_ID, fx.TEST_IDS[2], fx.TEST_IDS[0]))

    assert frame["patient_id"].unique().tolist() == [fx.TEST_IDS[0], fx.TEST_IDS[2]]
    assert len(frame) == 2 * TIMESTEPS


def test_regression_sidecar_id_format_loss_raises_instead_of_empty_output(
    ai_dir: Path,
) -> None:
    lost_suffix = tuple(pid.split("_")[0] for pid in fx.TEST_IDS)
    (ai_dir / fx.SIDECAR_NAME).write_bytes(fx.sidecar_bytes(lost_suffix))

    with pytest.raises(AdapterError, match="do not match") as excinfo:
        _load(ai_dir, patient_ids=fx.TEST_IDS)
    assert all(pid not in str(excinfo.value) for pid in lost_suffix)


def test_sidecar_row_count_mismatch_raises(ai_dir: Path) -> None:
    (ai_dir / fx.SIDECAR_NAME).write_bytes(fx.sidecar_bytes(fx.TEST_IDS[:2]))

    with pytest.raises(AdapterError, match="sidecar holds 2 ids but predictions cover 3"):
        _load(ai_dir)


@pytest.mark.parametrize(
    "content",
    [b"patient_id\n900001_0001\n", b"case_admission_id,x\n900001_0001,1\n", b"\xff\xfe"],
)
def test_malformed_sidecar_raises(ai_dir: Path, content: bytes) -> None:
    (ai_dir / fx.SIDECAR_NAME).write_bytes(content)

    with pytest.raises(AdapterError, match="sidecar"):
        _load(ai_dir)


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


def test_fixture_output_is_strict_canonical_and_matches_expected_csv() -> None:
    frame = _load()

    validate(frame, CanonicalShape.AI_OUTPUT, strict=True, dataset="geneva")
    expected = _expected_frame()
    assert frame["patient_id"].tolist() == expected["patient_id"].tolist()
    assert frame["t_minutes"].tolist() == expected["t_minutes"].tolist()
    assert frame["model_id"].tolist() == expected["model_id"].tolist()
    assert frame["output_json"].tolist() == expected["output_json"].tolist()


def test_load_geneva_with_ai_source_keeps_clinical_frames_and_adds_ai() -> None:
    csv_path = FIXTURE_DIR / fx.CLINICAL_NAME
    plain = load_geneva(csv_path, GENEVA_PARAMS_DIR)
    with_ai = load_geneva(csv_path, GENEVA_PARAMS_DIR, ai_source=_source())

    pd.testing.assert_frame_equal(with_ai.scalar_ts, plain.scalar_ts)
    pd.testing.assert_frame_equal(with_ai.admission, plain.admission)
    pd.testing.assert_frame_equal(with_ai.imaging, plain.imaging)
    assert with_ai.issues == plain.issues
    assert with_ai.ai_output["output_json"].tolist() == _expected_frame()["output_json"].tolist()
    assert with_ai.ai_provenance == load_geneva_ai_predictions(_source()).provenance


def test_load_geneva_filters_ai_rows_with_patient_ids() -> None:
    dataset = load_geneva(
        FIXTURE_DIR / fx.CLINICAL_NAME,
        GENEVA_PARAMS_DIR,
        patient_ids=(fx.TEST_IDS[1], fx.NON_TEST_ID),
        ai_source=_source(),
    )

    assert dataset.ai_output["patient_id"].unique().tolist() == [fx.TEST_IDS[1]]
    assert fx.NON_TEST_ID in set(dataset.admission["patient_id"])


def test_load_geneva_without_ai_source_is_unchanged() -> None:
    dataset = load_geneva(FIXTURE_DIR / fx.CLINICAL_NAME, GENEVA_PARAMS_DIR)

    assert dataset.ai_output.empty
    assert list(dataset.ai_output.columns) == ["patient_id", "t_minutes", "model_id", "output_json"]
    assert dataset.ai_provenance is None


def test_build_dataset_loader_loads_geneva_ai_when_configured(tmp_path: Path, ai_dir: Path) -> None:
    study = load_study_config(_study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir)))
    dataset = build_dataset_loader(study)()

    direct = load_geneva_ai_predictions(_source(ai_dir), patient_ids=study.patient_ids)
    assert study.geneva_ai is not None
    assert study.geneva_ai.predictions_path == ai_dir / fx.PREDICTIONS_NAME  # resolved
    pd.testing.assert_frame_equal(dataset.ai_output, direct.ai_output)
    assert dataset.ai_provenance == direct.provenance


def test_build_dataset_loader_without_geneva_ai_has_no_ai(tmp_path: Path) -> None:
    study = load_study_config(_study_yaml(tmp_path, geneva_ai=None))
    dataset = build_dataset_loader(study)()

    assert dataset.ai_output.empty
    assert dataset.ai_provenance is None
    assert "geneva_ai" not in json.loads(render_study_snapshot(study))


@pytest.mark.parametrize("dataset", ["synthetic", "mimic"])
def test_geneva_ai_with_other_dataset_is_config_error(
    tmp_path: Path, ai_dir: Path, dataset: str
) -> None:
    path = _study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir), dataset=dataset)

    with pytest.raises(ConfigError, match="geneva_ai requires dataset 'geneva'"):
        load_study_config(path)


def test_geneva_ai_block_rejects_unknown_keys_and_blank_model_id(
    tmp_path: Path, ai_dir: Path
) -> None:
    with pytest.raises(ConfigError, match="model_system_version"):
        load_study_config(
            _study_yaml(
                tmp_path,
                geneva_ai={**_geneva_ai_block(ai_dir), "model_system_version": "x"},
            )
        )
    with pytest.raises(ConfigError, match="non-blank"):
        load_study_config(
            _study_yaml(tmp_path, geneva_ai={**_geneva_ai_block(ai_dir), "model_id": " "})
        )


def test_geneva_ai_snapshot_hashes_paths_and_omits_absent_explanations(
    tmp_path: Path, ai_dir: Path
) -> None:
    with_shap = load_study_config(_study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir)))
    without_shap = load_study_config(
        _study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir, Explanations.WITHOUT))
    )

    snapshot = json.loads(render_study_snapshot(with_shap))
    assert snapshot["geneva_ai"]["predictions_path"] == str(ai_dir / fx.PREDICTIONS_NAME)
    assert "explanations_dir" not in json.loads(render_study_snapshot(without_shap))["geneva_ai"]


@pytest.mark.parametrize(
    "outside",
    [fx.PREDICTIONS_NAME, fx.SIDECAR_NAME, fx.MODEL_NAME, SHAP_DIR],
)
def test_every_geneva_ai_path_outside_data_root_is_refused(
    tmp_path: Path, ai_dir: Path, monkeypatch: pytest.MonkeyPatch, outside: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shutil.move(ai_dir / outside, elsewhere / outside)
    source = _source(ai_dir)
    field = {
        fx.PREDICTIONS_NAME: "predictions_path",
        fx.SIDECAR_NAME: "patient_ids_path",
        fx.MODEL_NAME: "model_path",
        SHAP_DIR: "explanations_dir",
    }[outside]
    source = GenevaAISource(**{**source.__dict__, field: elsewhere / outside})
    monkeypatch.setenv("EHR_SIM_DATA_ROOT", str(ai_dir))

    with pytest.raises(AdapterError, match="path traversal"):
        load_geneva_ai_predictions(source)


def test_paths_inside_data_root_load(ai_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EHR_SIM_DATA_ROOT", str(ai_dir))

    assert len(_load(ai_dir)) == N_PATIENTS * TIMESTEPS


# ---------------------------------------------------------------------------
# S11g compatibility regressions
# ---------------------------------------------------------------------------


def test_matching_provenance_passes_s11g_gate_and_preflight(tmp_path: Path, ai_dir: Path) -> None:
    provenance = load_geneva_ai_predictions(_source(ai_dir)).provenance
    path = _study_yaml(
        tmp_path, geneva_ai=_geneva_ai_block(ai_dir), extra=_phase2_extra(provenance)
    )
    study = load_study_config(path)
    dataset = build_dataset_loader(study)()

    assert provenance_mismatches(study.ai_intervention, dataset.ai_provenance) == []
    report = walk_preflight(study, load_questions(QUESTIONS_PATH), dataset)
    assert not report.has_fail, [r.message for r in report.rows if r.status == "FAIL"]


def test_phase2_patient_outside_test_subset_fails_preflight(tmp_path: Path, ai_dir: Path) -> None:
    provenance = load_geneva_ai_predictions(_source(ai_dir)).provenance
    extra = {**_phase2_extra(provenance), "patient_ids": [fx.TEST_IDS[0], fx.NON_TEST_ID]}
    study = load_study_config(
        _study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir), extra=extra)
    )
    report = walk_preflight(study, load_questions(QUESTIONS_PATH), build_dataset_loader(study)())

    failures = [r for r in report.rows if r.status == "FAIL"]
    assert failures and {r.patient_id for r in failures} == {fx.NON_TEST_ID}
    assert all("no AI row" in r.message for r in failures)


@pytest.mark.parametrize("artifact", [fx.PREDICTIONS_NAME, fx.SIDECAR_NAME])
def test_modified_predictions_or_sidecar_fail_s11g_gate(ai_dir: Path, artifact: str) -> None:
    frozen = _intervention(load_geneva_ai_predictions(_source(ai_dir)).provenance)
    if artifact == fx.SIDECAR_NAME:
        swapped = (fx.TEST_IDS[1], fx.TEST_IDS[0], fx.TEST_IDS[2])
        (ai_dir / artifact).write_bytes(fx.sidecar_bytes(swapped))
    else:
        y_true, y_prob = _read_pickle(ai_dir / artifact)
        y_true[0] = 0.0  # the probabilities stay aligned; only the bytes move
        _write_pickle(ai_dir / artifact, (y_true, y_prob))

    loaded = load_geneva_ai_predictions(_source(ai_dir)).provenance
    assert loaded.prediction_sha256 != frozen.prediction_artifact_sha256
    assert any("prediction SHA256" in m for m in provenance_mismatches(frozen, loaded))


def test_modified_shap_file_fails_s11g_gate(ai_dir: Path) -> None:
    frozen = _intervention(load_geneva_ai_predictions(_source(ai_dir)).provenance)
    _write_pickle(ai_dir / FEATURE_NAMES, ["renamed", *fx.FEATURE_NAMES[1:]])

    loaded = load_geneva_ai_predictions(_source(ai_dir)).provenance
    assert loaded.explanation_sha256 != frozen.explanation_artifact_sha256
    assert any("explanation SHA256" in m for m in provenance_mismatches(frozen, loaded))


def test_model_system_version_is_model_file_sha256(ai_dir: Path) -> None:
    provenance = load_geneva_ai_predictions(_source(ai_dir)).provenance

    expected = hashlib.sha256(fx.MODEL_BYTES).hexdigest()
    assert provenance.model_system_version == expected
    unrelated = _intervention(provenance, model_system_version="unrelated_v1")
    assert any("model/system version" in m for m in provenance_mismatches(unrelated, provenance))

    (ai_dir / fx.MODEL_NAME).write_bytes(b"retrained")
    moved = load_geneva_ai_predictions(_source(ai_dir)).provenance
    assert moved.model_system_version != expected


# ---------------------------------------------------------------------------
# Leakage and integrity regressions
# ---------------------------------------------------------------------------


def test_regression_y_true_never_reaches_payload_frame_or_errors(ai_dir: Path) -> None:
    frame = _load(ai_dir)
    assert SENTINEL_TEXT not in frame.to_csv()

    shap = _read_pickle(ai_dir / SHAP_VALUES)
    shap[0][0, 0] += np.float32(1.0)  # break alignment to provoke an error
    _write_pickle(ai_dir / SHAP_VALUES, shap)
    with pytest.raises(AdapterError) as excinfo:
        _load(ai_dir)
    error_text = str(excinfo.value) + "".join(i.reason for i in excinfo.value.issues)
    assert SENTINEL_TEXT not in error_text


def test_regression_row_t_built_from_shap_t_only(ai_dir: Path) -> None:
    target_t = 7
    before = _load(ai_dir)

    # Move contribution between two features at every other timestep: row
    # sums (and so alignment) hold, every other row's payload changes.
    shap = _read_pickle(ai_dir / SHAP_VALUES)
    for t, array in enumerate(shap):
        if t != target_t:
            array[:, 0] += np.float32(0.25)
            array[:, 1] -= np.float32(0.25)
    _write_pickle(ai_dir / SHAP_VALUES, shap)
    after = _load(ai_dir)

    at_t = before["t_minutes"] == target_t * 60.0
    assert after.loc[at_t, "output_json"].tolist() == before.loc[at_t, "output_json"].tolist()
    assert (after.loc[~at_t, "output_json"] != before.loc[~at_t, "output_json"]).all()


@pytest.mark.parametrize(
    ("row", "patient_id", "t_minutes", "probability"),
    [
        (0, "900001_0001", 0.0, 0.2972087860107422),
        (1, "900001_0001", 60.0, 0.31851059198379517),
        (71, "900001_0001", 4260.0, 0.3634028732776642),
    ],
)
def test_regression_timestep_index_i_maps_to_60_i(
    row: int, patient_id: str, t_minutes: float, probability: float
) -> None:
    frame = _load()

    assert frame.iloc[row]["patient_id"] == patient_id
    assert frame.iloc[row]["t_minutes"] == t_minutes
    assert json.loads(frame.iloc[row]["output_json"])["probability"] == probability


def test_regression_swapped_shap_patients_fail_alignment(ai_dir: Path) -> None:
    shap = _read_pickle(ai_dir / SHAP_VALUES)
    _write_pickle(ai_dir / SHAP_VALUES, [a[[1, 0, 2]] for a in shap])

    with pytest.raises(AdapterError, match="alignment failed"):
        _load(ai_dir)


def test_regression_swapped_prediction_patients_fail_alignment(ai_dir: Path) -> None:
    y_true, y_prob = _read_pickle(ai_dir / fx.PREDICTIONS_NAME)
    swapped = y_prob.reshape(N_PATIENTS, TIMESTEPS)[[1, 0, 2]].reshape(-1)
    _write_pickle(ai_dir / fx.PREDICTIONS_NAME, (y_true, swapped))

    with pytest.raises(AdapterError, match="alignment failed"):
        _load(ai_dir)


def test_alignment_checks_retained_rows_only(ai_dir: Path) -> None:
    shap = _read_pickle(ai_dir / SHAP_VALUES)
    shap[3][2, 0] += np.float32(1.0)  # misaligned patient 2 only
    _write_pickle(ai_dir / SHAP_VALUES, shap)

    assert len(_load(ai_dir, patient_ids=fx.TEST_IDS[:2])) == 2 * TIMESTEPS
    with pytest.raises(AdapterError, match="alignment failed for 1 rows"):
        _load(ai_dir)


def test_hash_and_parse_come_from_the_same_bytes(
    ai_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = ai_dir / fx.PREDICTIONS_NAME
    original = path.read_bytes()
    y_true, y_prob = _read_pickle(path)
    replacement = pickle.dumps((y_true, np.full_like(y_prob, 0.5)), protocol=fx.PICKLE_PROTOCOL)
    real_read = geneva_ai_module._read_bytes

    def read_then_replace(target: Path) -> bytes:
        data = real_read(target)
        if target == path:
            path.write_bytes(replacement)  # swapped on disk after the single read
        return data

    monkeypatch.setattr(geneva_ai_module, "_read_bytes", read_then_replace)
    output = load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT))

    sidecar = (ai_dir / fx.SIDECAR_NAME).read_bytes()
    assert output.provenance.prediction_sha256 == _prediction_digest(sidecar, original)
    assert _payloads(output.ai_output)[0]["probability"] == float(y_prob[0])


def test_lenient_load_geneva_still_rejects_malformed_ai(ai_dir: Path) -> None:
    shap = _read_pickle(ai_dir / SHAP_VALUES)
    _write_pickle(ai_dir / SHAP_VALUES, [a[[1, 0, 2]] for a in shap])

    with pytest.raises(AdapterError, match="alignment failed"):
        load_geneva(
            ai_dir / fx.CLINICAL_NAME, GENEVA_PARAMS_DIR, strict=False, ai_source=_source(ai_dir)
        )


@pytest.mark.parametrize("strict", [False, True])
def test_validate_adapter_prints_provenance_and_no_payload(
    tmp_path: Path, ai_dir: Path, strict: bool
) -> None:
    path = _study_yaml(tmp_path, geneva_ai=_geneva_ai_block(ai_dir))
    provenance = load_geneva_ai_predictions(_source(ai_dir)).provenance
    args = ["validate-adapter", str(path), *(["--strict"] if strict else [])]

    result = CliRunner().invoke(cli.app_typer, args)

    assert result.exit_code == 0, result.output
    assert provenance.prediction_sha256 in result.stdout
    assert provenance.explanation_sha256 in result.stdout
    assert provenance.model_system_version in result.stdout
    assert "AI patients:" in result.stdout
    for leaked in ("probability", "contributions", "base_value", fx.FEATURE_NAMES[0]):
        assert leaked not in result.stdout


# ---------------------------------------------------------------------------
# Optional explanations
# ---------------------------------------------------------------------------


def test_geneva_ai_without_explanations_loads_and_passes_preflight(
    tmp_path: Path, ai_dir: Path
) -> None:
    output = load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT))
    assert output.provenance.explanation_sha256 is None
    assert all(set(p) == {"probability"} for p in _payloads(output.ai_output))

    block = _geneva_ai_block(ai_dir, Explanations.WITHOUT)
    extra = _phase2_extra(output.provenance)
    assert "explanation_artifact_sha256" not in extra["ai_intervention"]
    study = load_study_config(_study_yaml(tmp_path, geneva_ai=block, extra=extra))
    report = walk_preflight(study, load_questions(QUESTIONS_PATH), build_dataset_loader(study)())
    assert not report.has_fail, [r.message for r in report.rows if r.status == "FAIL"]


def test_explanation_digest_presence_mismatch_fails_s11g_gate(ai_dir: Path) -> None:
    with_shap = load_geneva_ai_predictions(_source(ai_dir)).provenance
    without_shap = load_geneva_ai_predictions(_source(ai_dir, Explanations.WITHOUT)).provenance

    expects_shap = _intervention(with_shap)
    expects_none = _intervention(without_shap)
    assert any("explanation SHA256" in m for m in provenance_mismatches(expects_shap, without_shap))
    assert any("explanation SHA256" in m for m in provenance_mismatches(expects_none, with_shap))
    assert provenance_mismatches(expects_none, without_shap) == []


# ---------------------------------------------------------------------------
# Export script
# ---------------------------------------------------------------------------


def test_export_script_writes_expected_sidecar(tmp_path: Path) -> None:
    script = _load_script()
    out = tmp_path / "ids" / "test_patient_ids.csv"

    status = script.main(
        [
            "export",
            str(FIXTURE_DIR / fx.SPLIT_NAME),
            str(FIXTURE_DIR / fx.PREDICTIONS_NAME),
            str(out),
        ]
    )

    assert status == 0
    assert out.read_bytes() == (FIXTURE_DIR / fx.SIDECAR_NAME).read_bytes()
    assert [p.name for p in out.parent.iterdir()] == [out.name]  # no temp file left


def test_export_script_refuses_failed_order_cross_check(
    tmp_path: Path, ai_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = _load_script()
    y_true, y_prob = _read_pickle(ai_dir / fx.PREDICTIONS_NAME)
    shifted = np.roll(y_true, TIMESTEPS)  # positives now on another patient
    _write_pickle(ai_dir / fx.PREDICTIONS_NAME, (shifted, y_prob))
    out = tmp_path / "test_patient_ids.csv"

    status = script.main(
        ["export", str(ai_dir / fx.SPLIT_NAME), str(ai_dir / fx.PREDICTIONS_NAME), str(out)]
    )

    assert status == 1
    assert not out.exists()
    captured = capsys.readouterr()
    assert "cross check failed" in captured.err
    assert all(pid not in captured.err + captured.out for pid in fx.TEST_IDS)


def test_export_script_unpickler_shims_int64index_and_refuses_other_globals() -> None:
    script = _load_script()
    unpickler = script._SplitUnpickler(__import__("io").BytesIO(b""))

    assert unpickler.find_class("pandas.core.indexes.numeric", "Int64Index") is pd.Index
    with pytest.raises(script.ExportError, match="not allowed"):
        unpickler.find_class("os", "system")
