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


# ---------------------------------------------------------------------------
# S8 §12.3: headless render walk SLA (30 patients × 12 timepoints)
# ---------------------------------------------------------------------------

AI_MODEL_DIR = Path(
    "/mnt/data1/klug/output/opsum/short_term_outcomes/end_with_imaging/best_xgb_final_model"
)
AI_SPLIT = REAL_CSV.parent / "test_data_early_neurological_deterioration_ts0.8_rs42_ns5.pth"
EXPORT_SCRIPT = Path(__file__).parents[1] / "scripts" / "export_geneva_test_ids.py"
SLA_PATIENTS = 30
SLA_TIMEPOINTS = tuple(60.0 * h for h in range(12))  # 0, 60, …, 660
SLA_SECONDS = 10.0


@pytest.mark.real_data
def test_render_walk_sla(tmp_path: Path) -> None:
    import importlib.util

    import pandas as pd

    from ehr_simulator.config.study import AIInterventionConfig
    from ehr_simulator.ingestion import GenevaAISource
    from ehr_simulator.web.panel_render import _render_panels_locked
    from ehr_simulator.web.panels import InterventionContext, InterventionMode, slice_to_timepoint
    from tests.support.geneva_ui import stub_request

    if not REAL_CSV.is_file() or not AI_MODEL_DIR.is_dir() or not AI_SPLIT.is_file():
        pytest.skip("real Geneva data or AI artifact not available")

    spec = importlib.util.spec_from_file_location("export_geneva_test_ids", EXPORT_SCRIPT)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    sidecar = tmp_path / "test_patient_ids.csv"
    script.export(AI_SPLIT, AI_MODEL_DIR / "test_predictions.pkl", sidecar)

    present = set(
        pd.read_csv(REAL_CSV, usecols=["case_admission_id"], dtype=str)["case_admission_id"]
    )
    test_ids = pd.read_csv(sidecar, dtype=str)["case_admission_id"].tolist()
    patients = tuple([pid for pid in test_ids if pid in present][:SLA_PATIENTS])
    assert len(patients) == SLA_PATIENTS

    model_id = "opsum_end_xgb"
    dataset = load_geneva(
        REAL_CSV,
        REAL_PARAMS_DIR,
        strict=False,
        patient_ids=patients,
        ai_source=GenevaAISource(
            predictions_path=AI_MODEL_DIR / "test_predictions.pkl",
            patient_ids_path=sidecar,
            model_path=AI_MODEL_DIR / "xgb_final_model.model",
            model_id=model_id,
            explanations_dir=AI_MODEL_DIR / "shap_explanations_over_time",
        ),
    )
    loaded = dataset.ai_provenance
    configured = AIInterventionConfig(
        model_id=model_id,
        model_system_version=loaded.model_system_version,
        prediction_artifact_sha256=loaded.prediction_sha256,
        explanation_artifact_sha256=loaded.explanation_sha256,
        presentation_version="s8",
        intervention_build_id="sla",
    )
    intervention = InterventionContext(InterventionMode.AI, configured, loaded)
    request = stub_request()

    start = time.monotonic()
    errors = []
    for patient_id in patients:
        for t_index, t in enumerate(SLA_TIMEPOINTS):
            sliced = slice_to_timepoint(dataset, patient_id, t, t_index, timepoints=SLA_TIMEPOINTS)
            _render_panels_locked(sliced, request, intervention)
            errors += [(patient_id, t, p) for p, e in sliced.panel_errors.items() if e]
    elapsed = time.monotonic() - start

    assert errors == []
    print(f"S8 render walk: {SLA_PATIENTS}×{len(SLA_TIMEPOINTS)} in {elapsed:.1f}s")
    assert elapsed < SLA_SECONDS, f"render walk exceeded {SLA_SECONDS}s ({elapsed:.1f}s)"
