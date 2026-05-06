"""Slicing + panel-state detection tests (Decisions D5).

The slice function is the only authorized reader of unsliced data, so these
tests pin both the row-filtering AND the per-panel state derivation.
"""

from __future__ import annotations

import pytest

from ehr_simulator.ingestion.synthetic import load_synthetic
from ehr_simulator.web.panels import patient_timepoints, slice_to_timepoint


@pytest.fixture(scope="module")
def synthetic():
    return load_synthetic()


def test_slice_to_timepoint_filters_data_above_t(synthetic) -> None:
    sliced = slice_to_timepoint(synthetic, "synth_001", t_minutes=60.0, timepoint_index=1)

    assert sliced.scalar_ts["t_minutes"].max() <= 60.0
    assert sliced.imaging["t_minutes"].max() <= 60.0
    assert sliced.ai_output["t_minutes"].max() <= 60.0
    # ADMISSION has no t_minutes column → filtered by patient only.
    assert (sliced.admission["patient_id"] == "synth_001").all()
    assert not sliced.admission.empty


def test_slice_to_timepoint_excludes_t_above_60(synthetic) -> None:
    sliced = slice_to_timepoint(synthetic, "synth_001", t_minutes=60.0, timepoint_index=1)
    for frame in (sliced.scalar_ts, sliced.imaging, sliced.ai_output):
        if "t_minutes" in frame.columns and not frame.empty:
            assert (frame["t_minutes"] <= 60.0).all()
            assert not (frame["t_minutes"] == 180.0).any()


@pytest.mark.parametrize(
    "patient_id, t_minutes, timepoint_index, panel, expected",
    [
        # synth_001 happy path: vitals at t=0 are loading (all variables present at t=0).
        ("synth_001", 0.0, 0, "vitals", "loading"),
        ("synth_001", 0.0, 0, "labs", "loading"),
        ("synth_001", 0.0, 0, "imaging", "loading"),
        ("synth_001", 0.0, 0, "ai", "loading"),
        ("synth_001", 0.0, 0, "admission", "loading"),
        # synth_002 at t=60: labs were skipped at t=60 → partial.
        ("synth_002", 60.0, 1, "labs", "partial"),
        # synth_002 at t=60: vitals are present at t=60 → loading.
        ("synth_002", 60.0, 1, "vitals", "loading"),
        # synth_003 has no imaging at all → empty-expected.
        ("synth_003", 0.0, 0, "imaging", "empty-expected"),
        ("synth_003", 60.0, 1, "imaging", "empty-expected"),
        ("synth_003", 180.0, 2, "imaging", "empty-expected"),
        # synth_002 imaging exists only at t=0 — at t=60 still in slice (loading).
        ("synth_002", 60.0, 1, "imaging", "loading"),
    ],
)
def test_panel_state_detection_table(
    synthetic, patient_id, t_minutes, timepoint_index, panel, expected
) -> None:
    sliced = slice_to_timepoint(synthetic, patient_id, t_minutes, timepoint_index)
    assert sliced.panel_states[panel] == expected, (
        f"{patient_id} t={t_minutes} panel={panel}: expected {expected}, "
        f"got {sliced.panel_states[panel]}"
    )


def test_patient_timepoints_returns_sorted_unique(synthetic) -> None:
    tps = patient_timepoints(synthetic, "synth_001")
    assert tps == (0.0, 60.0, 180.0)
