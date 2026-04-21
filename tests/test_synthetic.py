"""Synthetic adapter contract tests."""

from __future__ import annotations

import pandas as pd

from ehr_simulator.ingestion import CanonicalShape, load_synthetic, validate


def test_load_synthetic_returns_all_four_shapes() -> None:
    d = load_synthetic()
    assert not d.scalar_ts.empty
    assert not d.admission.empty
    assert not d.imaging.empty
    assert not d.ai_output.empty


def test_load_synthetic_frames_validate_strict() -> None:
    d = load_synthetic()
    validate(d.scalar_ts, CanonicalShape.SCALAR_TS, strict=True, dataset="synthetic")
    validate(d.admission, CanonicalShape.ADMISSION, strict=True, dataset="synthetic")
    validate(d.imaging, CanonicalShape.IMAGING, strict=True, dataset="synthetic")
    validate(d.ai_output, CanonicalShape.AI_OUTPUT, strict=True, dataset="synthetic")


def test_load_synthetic_drops_imputed_rows() -> None:
    d = load_synthetic()
    assert not d.scalar_ts["source"].str.contains("imputed", na=False).any()


def test_load_synthetic_has_three_patients() -> None:
    d = load_synthetic()
    assert set(d.scalar_ts["patient_id"].unique()) == {"synth_001", "synth_002", "synth_003"}
    assert set(d.admission["patient_id"].unique()) == {"synth_001", "synth_002", "synth_003"}
    assert set(d.ai_output["patient_id"].unique()) == {"synth_001", "synth_002", "synth_003"}


def test_load_synthetic_timepoints_non_negative() -> None:
    d = load_synthetic()
    assert (d.scalar_ts["t_minutes"] >= 0).all()
    assert (d.imaging["t_minutes"] >= 0).all()
    assert (d.ai_output["t_minutes"] >= 0).all()


def test_load_synthetic_synth_002_missing_labs_at_60min() -> None:
    d = load_synthetic()
    labs = {"hgb", "na", "cr", "glucose"}
    s02_t60 = d.scalar_ts[
        (d.scalar_ts["patient_id"] == "synth_002") & (d.scalar_ts["t_minutes"] == 60.0)
    ]
    assert labs.isdisjoint(set(s02_t60["variable"].unique()))


def test_load_synthetic_deterministic() -> None:
    a = load_synthetic(seed=42)
    b = load_synthetic(seed=42)
    pd.testing.assert_frame_equal(a.scalar_ts, b.scalar_ts)
    pd.testing.assert_frame_equal(a.admission, b.admission)
    pd.testing.assert_frame_equal(a.imaging, b.imaging)
    pd.testing.assert_frame_equal(a.ai_output, b.ai_output)
