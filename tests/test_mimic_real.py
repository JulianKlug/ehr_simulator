"""Opt-in smoke test against the real MIMIC preprocessed-features CSV.

Skipped by default. Run via ``uv run pytest -m real_data``. Lives
outside the default ``-m 'not e2e'`` selector and is not parallelised so
the load gets a clean wall-clock budget.

Wall-clock baseline (~5 s) measured at S4 implementation time on the
local dataset
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


@pytest.mark.real_data
def test_load_mimic_real_csv_smoke() -> None:
    if not REAL_CSV.is_file() or not REAL_PARAMS_DIR.is_dir():
        pytest.skip(f"real MIMIC data not available at {REAL_CSV}")

    start = time.monotonic()
    dataset = load_mimic(REAL_CSV, REAL_PARAMS_DIR, strict=False)
    elapsed = time.monotonic() - start
    assert elapsed < 90.0, f"real-data load exceeded 90 s budget ({elapsed:.1f}s)"

    scalar_patients = set(dataset.scalar_ts["patient_id"])
    admission_patients = set(dataset.admission["patient_id"])
    n_scalar_patients = len(scalar_patients)
    n_admission_patients = len(admission_patients)
    # Every patient in the real CSV has both EHR rows and notes rows
    # (verified 2026-05-07). No cohort size is pinned: it is not published.
    assert admission_patients, f"no admission patients; issues: {dataset.issues[:5]}"
    assert scalar_patients == admission_patients, (
        f"scalar_ts and admission patients differ; issues: {dataset.issues[:5]}"
    )
    print(
        f"real-data load: {elapsed:.1f}s, scalar patients={n_scalar_patients}, "
        f"admission patients={n_admission_patients}, issues={len(dataset.issues)}"
    )
