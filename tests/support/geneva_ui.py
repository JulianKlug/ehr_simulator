"""S8: a Geneva shaped canonical dataset (post adapter) for UI tests.

The committed CSV fixture holds two EHR variables only, so presentation
tests build the canonical frames directly, with the ids, admission field
names and AI payload shape the real adapter produces. Patient ids match
``tests/fixtures/study/study_geneva.yaml`` so study mode can use it.

``FUTURE_VALUE`` marks every scalar row after :data:`LEAK_T`: a page at or
before ``LEAK_T`` must never contain it.
"""

from __future__ import annotations

import json

import pandas as pd

from ehr_simulator.ingestion import GenevaDataset
from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame
from ehr_simulator.ingestion.provenance import AIArtifactProvenance

PATIENT = "geneva_fixture_001"
SPARSE_PATIENT = "geneva_fixture_002"
MODEL_ID = "fixture_xgb"
LEAK_T = 120.0
FUTURE_VALUE = 7777.77
FUTURE_PROBABILITY = 0.8642  # renders 86.4 %
FUTURE_FEATURE = "lag3_proBNP"
TIMES = (0.0, 60.0, 120.0, 240.0, 300.0)  # 180 missing: the median line breaks
AI_TIMES = (0.0, 60.0, 120.0, 180.0, 240.0, 300.0)

_VITAL_FAMILIES = {
    "hr": (70.0, "bpm"),
    "map": (90.0, "mmHg"),
    "sbp": (140.0, "mmHg"),
    "dbp": (80.0, "mmHg"),
    "spo2": (96.0, "%"),
    "rr": (16.0, "/min"),
    "nihss": (8.0, "score"),
}

ADMISSION = {
    "age": "71.5",
    "Sex": "Male",
    "weight": "82.0",
    "Anticoagulants": "no",
    "MedHist CHD": "yes",
    "MedHist Hypertension": "yes",
    "wake_up_stroke": "False",
    "Prestroke disability (Rankin)": "1.0",
    "categorical_IVT": "91-270min",
    "categorical_IAT": "no_IAT",
    "categorical_onset_to_admission_time": "<270min",
    "Referral": "Self referral or GP",
    "hypoperfusion_with_mismatch": "True",
    "vascular_occlusion": "False",
    "zz_unregistered": "kept",
}


def contributions(t_minutes: float) -> dict[str, float]:
    """Twelve non zero contributions plus a zero, ranked by magnitude."""
    base = {
        "lag2_glucose": 0.30,
        "median_NIHSS": 0.25,
        "age": 0.20,
        "avg_median_heart_rate": 0.15,
        "max_systolic_blood_pressure": 0.10,
        "timestep_idx": 0.05,  # sixth positive: never shown
        "creatinine": -0.28,
        "sex_male": -0.22,
        "unmapped_feature_x": -0.18,
        "cbf_lt_30": -0.12,
        "rolling_mean_FIO2": -0.06,
        "weight": -0.01,  # sixth negative: never shown
        "std_glucose": 0.0,
    }
    if t_minutes > LEAK_T:
        base[FUTURE_FEATURE] = 0.99
    return base


def geneva_payload(t_minutes: float) -> str:
    probability = FUTURE_PROBABILITY if t_minutes > LEAK_T else 0.2 + t_minutes / 1000
    return json.dumps(
        {
            "probability": probability,
            "explanation": {"base_value": -0.64, "contributions": contributions(t_minutes)},
        },
        sort_keys=True,
    )


def _scalar_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []

    def add(pid: str, t: float, variable: str, value: float, unit: str) -> None:
        if t > LEAK_T:
            value = FUTURE_VALUE
        rows.append(
            {
                "patient_id": pid,
                "t_minutes": t,
                "variable": variable,
                "value": value,
                "unit": unit,
                "source": "EHR",
            }
        )

    for t in TIMES:
        for median, (value, unit) in _VITAL_FAMILIES.items():
            if median == "nihss" and t > 60.0:
                continue
            add(PATIENT, t, f"{median}_min", value - 5, unit)
            add(PATIENT, t, median, value, unit)
            add(PATIENT, t, f"{median}_max", value + 5, unit)
    for t in (0.0, 120.0, 300.0):
        add(PATIENT, t, "temp", 37.2, "°C")
    add(PATIENT, 0.0, "gcs", 14.0, "score")
    for t in (0.0, 60.0):
        add(PATIENT, t, "fio2", 21.0, "%")
    # Sparse labs: glucose twice, creatinine once, CRP only later.
    add(PATIENT, 0.0, "glucose", 6.1, "mmol/L")
    add(PATIENT, 120.0, "glucose", 7.4, "mmol/L")
    add(PATIENT, 0.0, "cr", 88.0, "µmol/L")
    add(PATIENT, 0.0, "hct", 0.41, "L/L")
    add(PATIENT, 240.0, "crp", 12.0, "mg/L")
    # Imaging derived perfusion volumes.
    add(PATIENT, 0.0, "tmax_gt_6", 23.5, "ml")
    add(PATIENT, 0.0, "cbf_lt_30", 4.2, "ml")
    add(PATIENT, 300.0, "cbv_lt_42", 9.0, "ml")
    # An unregistered variable stays in the frame, unshown.
    add(PATIENT, 0.0, "mystery_score", 3.0, "")

    # Second patient: vitals median only; labs only after LEAK_T.
    for t in TIMES:
        add(SPARSE_PATIENT, t, "hr", 80.0, "bpm")
    add(SPARSE_PATIENT, 300.0, "glucose", 5.5, "mmol/L")
    return rows


def geneva_shaped_dataset(*, with_ai: bool = True) -> GenevaDataset:
    """Canonical frames shaped like ``load_geneva`` output, two patients."""
    admission = pd.DataFrame(
        [{"patient_id": PATIENT, "field": k, "value": v} for k, v in ADMISSION.items()]
        + [
            {"patient_id": SPARSE_PATIENT, "field": "age", "value": "60.0"},
            {"patient_id": SPARSE_PATIENT, "field": "Sex", "value": "Female"},
        ]
    )
    ai_output = empty_frame(CanonicalShape.AI_OUTPUT)
    provenance = None
    if with_ai:
        ai_output = pd.DataFrame(
            [
                {
                    "patient_id": pid,
                    "t_minutes": t,
                    "model_id": MODEL_ID,
                    "output_json": geneva_payload(t),
                }
                for pid in (PATIENT, SPARSE_PATIENT)
                for t in AI_TIMES
            ]
        )
        provenance = AIArtifactProvenance("a" * 64, "b" * 64, "c" * 64)
    return GenevaDataset(
        scalar_ts=pd.DataFrame(_scalar_rows()),
        admission=admission,
        imaging=empty_frame(CanonicalShape.IMAGING),
        ai_output=ai_output,
        ai_provenance=provenance,
    )
