"""Opt-in S7 smoke against the local production Geneva AI artifact.

Skipped by default. Run via ``uv run pytest -m real_data tests/test_geneva_ai_real.py -s``.
Exports the sidecar with ``scripts/export_geneva_test_ids.py`` into
``tmp_path`` (never committed), then loads every test patient.
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.ingestion import (
    CanonicalShape,
    GenevaAISource,
    load_geneva_ai_predictions,
    validate,
)

MODEL_DIR = Path(
    "/mnt/data1/klug/output/opsum/short_term_outcomes/end_with_imaging/best_xgb_final_model"
)
GENEVA_DIR = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "gsu_Extraction_20220815_prepro_30012026_154047"
)
SPLIT = GENEVA_DIR / "test_data_early_neurological_deterioration_ts0.8_rs42_ns5.pth"
CLINICAL_CSV = GENEVA_DIR / "preprocessed_features_30012026_154047.csv"
SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "export_geneva_test_ids.py"

EXPECTED_PATIENTS = 533
EXPECTED_ROWS = 38_376


@pytest.mark.real_data
def test_load_real_geneva_ai_artifact(tmp_path: Path) -> None:
    if not MODEL_DIR.is_dir() or not SPLIT.is_file():
        pytest.skip(f"real Geneva AI artifact not available at {MODEL_DIR}")

    spec = importlib.util.spec_from_file_location("export_geneva_test_ids", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    sidecar = tmp_path / "test_patient_ids.csv"
    assert script.export(SPLIT, MODEL_DIR / "test_predictions.pkl", sidecar) == EXPECTED_PATIENTS

    source = GenevaAISource(
        predictions_path=MODEL_DIR / "test_predictions.pkl",
        patient_ids_path=sidecar,
        model_path=MODEL_DIR / "xgb_final_model.model",
        model_id="opsum_end_xgb",
        explanations_dir=MODEL_DIR / "shap_explanations_over_time",
    )
    start = time.monotonic()
    output = load_geneva_ai_predictions(source)  # alignment check included
    elapsed = time.monotonic() - start

    frame = output.ai_output
    validate(frame, CanonicalShape.AI_OUTPUT, strict=True, dataset="geneva")
    assert len(frame) == EXPECTED_ROWS
    assert frame["patient_id"].nunique() == EXPECTED_PATIENTS
    assert not frame.duplicated(["patient_id", "t_minutes", "model_id"]).any()
    assert output.provenance.explanation_sha256 is not None

    clinical_ids = set(
        pd.read_csv(CLINICAL_CSV, usecols=["case_admission_id"], dtype=str)["case_admission_id"]
    )
    assert set(frame["patient_id"]) <= clinical_ids
    print(f"real Geneva AI load (parse + hash + serialise, all patients): {elapsed:.1f}s")
