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


# ---------------------------------------------------------------------------
# S8 §1.3: every inventory label arrives in its expected frame
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_geneva():
    if not REAL_CSV.is_file() or not REAL_PARAMS_DIR.is_dir():
        pytest.skip(f"real Geneva data not available at {REAL_CSV}")
    return load_geneva(REAL_CSV, REAL_PARAMS_DIR, strict=False)


@pytest.mark.real_data
def test_inventory_labels_arrive_in_expected_frames(real_geneva) -> None:
    from ehr_simulator.ingestion.canonical import IMAGING_FINDING_FIELDS
    from ehr_simulator.ingestion.geneva import _VARIABLE_RENAMES
    from tests.test_clinical_fields import GENEVA_EHR_LABELS

    variables = set(real_geneva.scalar_ts["variable"].astype(str))
    expected = {_VARIABLE_RENAMES.get(label, label) for label in GENEVA_EHR_LABELS}
    assert expected <= variables, sorted(expected - variables)

    fields = real_geneva.admission.loc[real_geneva.admission["field"] == "weight"]
    assert not fields.empty

    # §1.3.1: the binary imaging findings are registry (ADMISSION) fields.
    admission = real_geneva.admission
    for name in IMAGING_FINDING_FIELDS:
        values = set(admission.loc[admission["field"] == name, "value"])
        assert values and values <= {"True", "False"}, (name, values)
        assert name not in variables
