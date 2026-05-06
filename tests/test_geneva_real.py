"""Opt-in smoke test against the real 1.5 GB Geneva CSV.

Skipped by default. Run via ``uv run pytest -m real_data``. Lives outside
the default ``-m 'not e2e'`` selector and is not parallelised so the load
gets a clean wall-clock budget.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ehr_simulator.ingestion import load_geneva

REAL_CSV = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "gsu_Extraction_20220815_prepro_30012026_154047/"
    "preprocessed_features_30012026_154047.csv"
)
REAL_PARAMS_DIR = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "gsu_Extraction_20220815_prepro_30012026_154047/logs_30012026_154047"
)


@pytest.mark.real_data
def test_load_geneva_real_csv_smoke() -> None:
    if not REAL_CSV.is_file() or not REAL_PARAMS_DIR.is_dir():
        pytest.skip(f"real Geneva data not available at {REAL_CSV}")

    start = time.monotonic()
    dataset = load_geneva(REAL_CSV, REAL_PARAMS_DIR, strict=False)
    elapsed = time.monotonic() - start
    assert elapsed < 120.0, f"real-data load exceeded 120 s budget ({elapsed:.1f}s)"

    n_scalar_patients = dataset.scalar_ts["patient_id"].nunique()
    n_admission_patients = dataset.admission["patient_id"].nunique()
    assert n_scalar_patients > 1000, n_scalar_patients
    assert n_admission_patients > 1000, n_admission_patients
    print(
        f"real-data load: {elapsed:.1f}s, scalar patients={n_scalar_patients}, "
        f"admission patients={n_admission_patients}, issues={len(dataset.issues)}"
    )
