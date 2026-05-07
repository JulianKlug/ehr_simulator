"""Opt-in smoke test against the real MIMIC preprocessed-features CSV.

Skipped by default. Run via ``uv run pytest -m real_data``. Lives
outside the default ``-m 'not e2e'`` selector and is not parallelised so
the load gets a clean wall-clock budget.

Concrete patient counts (247 EHR / 247 notes) and wall-clock baseline
(~5 s) measured at S4 implementation time on the local dataset
``/mnt/data1/klug/datasets/opsum/.../mimic_prepro_16022026_095909/``.
The 90 s budget per OV.4 gives ample headroom for slower CI runners and
cold filesystem reads on ``/mnt``.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ehr_simulator.ingestion.mimic import load_mimic

REAL_CSV = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "mimic_prepro_16022026_095909/preprocessed_features_16022026_095909.csv"
)
REAL_PARAMS_DIR = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "mimic_prepro_16022026_095909/logs_16022026_095909"
)
EXPECTED_PATIENTS = 247


@pytest.mark.real_data
def test_load_mimic_real_csv_smoke() -> None:
    if not REAL_CSV.is_file() or not REAL_PARAMS_DIR.is_dir():
        pytest.skip(f"real MIMIC data not available at {REAL_CSV}")

    start = time.monotonic()
    dataset = load_mimic(REAL_CSV, REAL_PARAMS_DIR, strict=False)
    elapsed = time.monotonic() - start
    assert elapsed < 90.0, f"real-data load exceeded 90 s budget ({elapsed:.1f}s)"

    n_scalar_patients = dataset.scalar_ts["patient_id"].nunique()
    n_admission_patients = dataset.admission["patient_id"].nunique()
    # Every patient in the real CSV has both EHR rows and notes rows
    # (verified 2026-05-07). If a future upstream regenerates with a
    # cohort change, lower-bound the assertion rather than hardcoding a
    # new exact count.
    assert n_scalar_patients >= EXPECTED_PATIENTS, (
        f"expected ≥{EXPECTED_PATIENTS} scalar_ts patients, got {n_scalar_patients}; "
        f"issues: {dataset.issues[:5]}"
    )
    assert n_admission_patients == EXPECTED_PATIENTS, (
        f"expected exactly {EXPECTED_PATIENTS} admission patients, got {n_admission_patients}; "
        f"issues: {dataset.issues[:5]}"
    )
    print(
        f"real-data load: {elapsed:.1f}s, scalar patients={n_scalar_patients}, "
        f"admission patients={n_admission_patients}, issues={len(dataset.issues)}"
    )
