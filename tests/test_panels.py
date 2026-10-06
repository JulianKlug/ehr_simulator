"""Slicing + panel-state detection tests (Decisions D5).

The slice function is the only authorized reader of unsliced data, so these
tests pin both the row-filtering AND the per-panel state derivation.
"""

from __future__ import annotations

import pytest

from ehr_simulator.ingestion.synthetic import load_synthetic
from ehr_simulator.web.panels import DatasetLike, patient_timepoints, slice_to_timepoint


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


@pytest.mark.parametrize(
    "patient_id, t_minutes, timepoint_index, panel, expected",
    [
        # synth_001 happy path: vitals at t=0 are loading (all variables present at t=0).
        ("synth_001", 0.0, 0, "vitals", "loading"),
        ("synth_001", 0.0, 0, "labs", "loading"),
        ("synth_001", 0.0, 0, "imaging", "loading"),
        ("synth_001", 0.0, 0, "ai", "loading"),
        ("synth_001", 0.0, 0, "admission", "loading"),
        # synth_002 at t=60: labs were skipped at t=60 → sparse, not partial (S8 §10.1).
        ("synth_002", 60.0, 1, "labs", "loading"),
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


def test_dataset_like_protocol_accepts_all_three_dataset_classes(
    synthetic, geneva_fixture_dir, mimic_fixture_dir
) -> None:
    """DatasetLike is structurally satisfied by all three adapter classes
    (per /plan-eng-review tension B). Locks the typing generalization that
    lets ``walk_preflight`` accept any adapter dataset."""
    from ehr_simulator.ingestion.geneva import load_geneva
    from ehr_simulator.ingestion.mimic import load_mimic

    geneva = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    mimic = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )

    assert isinstance(synthetic, DatasetLike)
    assert isinstance(geneva, DatasetLike)
    assert isinstance(mimic, DatasetLike)


# ---------------------------------------------------------------------------
# S8 §10.1: partial reads rows at or before t only
# ---------------------------------------------------------------------------


def _scalar_dataset(rows: list[tuple[float, str, float]]):
    import pandas as pd

    from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame
    from ehr_simulator.ingestion.geneva import GenevaDataset

    scalar = pd.DataFrame(
        [
            {
                "patient_id": "p",
                "t_minutes": t,
                "variable": v,
                "value": x,
                "unit": "",
                "source": "EHR",
            }
            for t, v, x in rows
        ]
    )
    admission = pd.DataFrame([{"patient_id": "p", "field": "age", "value": "70"}])
    return GenevaDataset(
        scalar_ts=scalar,
        admission=admission,
        imaging=empty_frame(CanonicalShape.IMAGING),
        ai_output=empty_frame(CanonicalShape.AI_OUTPUT),
    )


@pytest.mark.parametrize(
    ("rows", "panel", "expected"),
    [
        ([(0.0, "hr_max", 90.0)], "vitals", "partial"),  # bound without median
        ([(0.0, "hr_min", 80.0), (0.0, "hr", 70.0)], "vitals", "partial"),  # min > median
        ([(0.0, "nihss", 4.0), (0.0, "nihss_max", 3.0)], "neuro_support", "partial"),
        ([(0.0, "hr", 70.0), (60.0, "sbp", 120.0)], "vitals", "loading"),  # sbp first after t
        ([(0.0, "hr", 70.0), (0.0, "hr_max", 90.0)], "vitals", "loading"),  # one bound is fine
        ([(0.0, "gcs", 14.0)], "neuro_support", "loading"),
        ([(0.0, "glucose", 5.0), (60.0, "crp", 3.0)], "labs", "loading"),
        ([(60.0, "glucose", 5.0)], "labs", "empty-unexpected"),
        ([(0.0, "tmax_gt_6", 12.0)], "imaging", "loading"),
        ([(60.0, "tmax_gt_6", 12.0)], "imaging", "empty-unexpected"),
        ([(0.0, "hr", 70.0)], "neuro_support", "empty-expected"),
    ],
)
def test_partial_rules_read_rows_at_or_before_t(rows, panel: str, expected: str) -> None:
    sliced = slice_to_timepoint(_scalar_dataset(rows), "p", 0.0, 0)
    assert sliced.panel_states[panel] == expected


def test_admission_imaging_findings_make_imaging_present() -> None:
    import pandas as pd

    dataset = _scalar_dataset([(0.0, "hr", 70.0)])
    dataset.admission = pd.DataFrame(
        [{"patient_id": "p", "field": "vascular_occlusion", "value": "True"}]
    )
    sliced = slice_to_timepoint(dataset, "p", 0.0, 0)
    assert sliced.panel_states["imaging"] == "loading"
