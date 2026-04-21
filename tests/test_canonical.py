"""Canonical schema validation tests."""

from __future__ import annotations

import pandas as pd
import pytest

from ehr_simulator.ingestion import (
    ADMISSION_SCHEMA,
    AI_OUTPUT_SCHEMA,
    IMAGING_SCHEMA,
    SCALAR_TS_SCHEMA,
    SCHEMAS,
    AdapterError,
    CanonicalShape,
    validate,
)


def _base_scalar_ts() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "patient_id": ["p1", "p1"],
            "t_minutes": [0.0, 60.0],
            "variable": ["hr", "hr"],
            "value": [72.0, 80.0],
            "unit": ["bpm", "bpm"],
            "source": ["EHR", "EHR"],
        }
    )


def test_scalar_ts_valid_frame() -> None:
    result = validate(_base_scalar_ts(), CanonicalShape.SCALAR_TS, strict=True)
    assert len(result) == 2
    assert list(result.columns) == [
        "patient_id",
        "t_minutes",
        "variable",
        "value",
        "unit",
        "source",
    ]


def test_scalar_ts_rejects_negative_t_minutes() -> None:
    frame = _base_scalar_ts()
    frame.loc[1, "t_minutes"] = -1.0
    with pytest.raises(AdapterError):
        validate(frame, CanonicalShape.SCALAR_TS, strict=True)


def test_scalar_ts_accepts_arbitrary_source_strings() -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["p1", "p1", "p1", "p1"],
            "t_minutes": [0.0, 60.0, 120.0, 180.0],
            "variable": ["hr", "hr", "hr", "hr"],
            "value": [72.0, 80.0, 75.0, 70.0],
            "unit": ["bpm", "bpm", "bpm", "bpm"],
            "source": ["EHR", "EHR_pop_imputed", "notes_locf_imputed", "stroke_registry"],
        }
    )
    result = validate(frame, CanonicalShape.SCALAR_TS, strict=True)
    assert len(result) == 4


def test_admission_unique_patient_field() -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["p1", "p1"],
            "field": ["age", "age"],
            "value": ["67", "68"],
        }
    )
    with pytest.raises(AdapterError):
        validate(frame, CanonicalShape.ADMISSION, strict=True)


def test_imaging_unique_patient_t_modality() -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["p1", "p1"],
            "t_minutes": [0.0, 0.0],
            "modality": ["CT", "CT"],
            "report_text": ["a", "b"],
            "image_refs": [None, None],
        }
    )
    with pytest.raises(AdapterError):
        validate(frame, CanonicalShape.IMAGING, strict=True)


def test_ai_output_requires_valid_json() -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["p1"],
            "t_minutes": [0.0],
            "model_id": ["demo_v0"],
            "output_json": ["{not json"],
        }
    )
    with pytest.raises(AdapterError):
        validate(frame, CanonicalShape.AI_OUTPUT, strict=True)


def test_strict_false_returns_partial_frame_with_issues() -> None:
    frame = pd.DataFrame(
        {
            "patient_id": ["p1", "p2", "p3"],
            "t_minutes": [0.0, -1.0, 60.0],
            "variable": ["hr", "hr", "hr"],
            "value": [72.0, 80.0, 75.0],
            "unit": ["bpm", "bpm", "bpm"],
            "source": ["EHR", "EHR", "EHR"],
        }
    )
    cleaned = validate(frame, CanonicalShape.SCALAR_TS, strict=False, dataset="test")
    assert len(cleaned) == 2
    assert sorted(cleaned["patient_id"].tolist()) == ["p1", "p3"]
    err = cleaned.attrs["adapter_error"]
    assert isinstance(err, AdapterError)
    assert len(err.issues) == 1
    assert err.issues[0].dataset == "test"
    assert err.issues[0].row_idx == 1


def test_validate_routes_by_shape_enum() -> None:
    assert SCHEMAS[CanonicalShape.SCALAR_TS] is SCALAR_TS_SCHEMA
    assert SCHEMAS[CanonicalShape.ADMISSION] is ADMISSION_SCHEMA
    assert SCHEMAS[CanonicalShape.IMAGING] is IMAGING_SCHEMA
    assert SCHEMAS[CanonicalShape.AI_OUTPUT] is AI_OUTPUT_SCHEMA
    admission_frame = pd.DataFrame({"patient_id": ["p1"], "field": ["age"], "value": ["67"]})
    result = validate(admission_frame, CanonicalShape.ADMISSION, strict=True)
    assert len(result) == 1
    bad_for_admission = _base_scalar_ts()
    with pytest.raises(AdapterError):
        validate(bad_for_admission, CanonicalShape.ADMISSION, strict=True)
