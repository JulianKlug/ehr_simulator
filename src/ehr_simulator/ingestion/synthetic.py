"""Synthetic dataset adapter.

Produces three deterministic patients in all four canonical shapes, for demos
and as a living reference implementation that every real adapter can copy.

The values are physiologically plausible but intentionally not representative
of real stroke patients. ``t=0`` is an arbitrary anchor; there is no real
admission time.

``synth_003`` emits scalar-timeseries rows whose ``source`` values carry
the ``"imputed"`` substring (e.g., ``"synthetic_pop_imputed"``,
``"notes_locf_imputed"``). The adapter drops these rows via substring match
before calling :func:`validate`, mirroring the real-world failure mode for
the MIMIC and Geneva CSVs where composite source strings like
``EHR_pop_imputed`` or ``stroke_registry_locf_imputed`` must be filtered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import numpy as np
import pandas as pd

from ehr_simulator.ingestion.canonical import CanonicalShape, validate

_TIMEPOINTS: tuple[float, ...] = (0.0, 60.0, 180.0)
_MODEL_ID = "demo_v0"
_DATASET_NAME = "synthetic"

_VITALS: tuple[tuple[str, str, float, float], ...] = (
    ("hr", "bpm", 60.0, 100.0),
    ("sbp", "mmHg", 110.0, 160.0),
    ("dbp", "mmHg", 60.0, 95.0),
    ("spo2", "%", 94.0, 100.0),
    ("temp", "degC", 36.2, 37.8),
)

_LABS: tuple[tuple[str, str, float, float], ...] = (
    ("hgb", "g/dL", 11.0, 15.0),
    ("na", "mmol/L", 135.0, 145.0),
    ("cr", "mg/dL", 0.6, 1.3),
    ("glucose", "mg/dL", 80.0, 180.0),
)

_ADMISSION_FACTS: tuple[tuple[str, dict[str, str]], ...] = (
    (
        "synth_001",
        {
            "age": "67",
            "sex": "M",
            "nihss_admission": "8",
            "stroke_location": "left_MCA",
            "time_of_onset_minutes": "45",
        },
    ),
    (
        "synth_002",
        {
            "age": "74",
            "sex": "F",
            "nihss_admission": "15",
            "stroke_location": "right_MCA",
            "time_of_onset_minutes": "120",
        },
    ),
    (
        "synth_003",
        {
            "age": "59",
            "sex": "F",
            "nihss_admission": "3",
            "stroke_location": "no_stroke",
            "time_of_onset_minutes": "30",
        },
    ),
)


@dataclass(frozen=True)
class SyntheticDataset:
    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame


def load_synthetic(*, seed: int = 42) -> SyntheticDataset:
    """Return three deterministic synthetic patients for demos and tests.

    Calls :func:`validate` with ``strict=True`` on every frame before
    returning, so a regression in the generator surfaces as a test failure
    rather than silent schema drift.
    """
    rng = np.random.default_rng(seed)

    scalar_ts = _build_scalar_ts(rng)
    admission = _build_admission()
    imaging = _build_imaging()
    ai_output = _build_ai_output(rng)

    scalar_ts = validate(scalar_ts, CanonicalShape.SCALAR_TS, strict=True, dataset=_DATASET_NAME)
    admission = validate(admission, CanonicalShape.ADMISSION, strict=True, dataset=_DATASET_NAME)
    imaging = validate(imaging, CanonicalShape.IMAGING, strict=True, dataset=_DATASET_NAME)
    ai_output = validate(ai_output, CanonicalShape.AI_OUTPUT, strict=True, dataset=_DATASET_NAME)

    return SyntheticDataset(
        scalar_ts=scalar_ts,
        admission=admission,
        imaging=imaging,
        ai_output=ai_output,
    )


def _build_scalar_ts(rng: np.random.Generator) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for pid, _ in _ADMISSION_FACTS:
        for t in _TIMEPOINTS:
            for var, unit, lo, hi in _VITALS:
                rows.append(
                    {
                        "patient_id": pid,
                        "t_minutes": t,
                        "variable": var,
                        "value": float(rng.uniform(lo, hi)),
                        "unit": unit,
                        "source": "synthetic",
                    }
                )
            if pid == "synth_002" and t == 60.0:
                continue
            for var, unit, lo, hi in _LABS:
                rows.append(
                    {
                        "patient_id": pid,
                        "t_minutes": t,
                        "variable": var,
                        "value": float(rng.uniform(lo, hi)),
                        "unit": unit,
                        "source": "synthetic",
                    }
                )

    rows.extend(
        [
            {
                "patient_id": "synth_003",
                "t_minutes": 0.0,
                "variable": "hr",
                "value": 999.0,
                "unit": "bpm",
                "source": "synthetic_pop_imputed",
            },
            {
                "patient_id": "synth_003",
                "t_minutes": 60.0,
                "variable": "hgb",
                "value": 999.0,
                "unit": "g/dL",
                "source": "synthetic_locf_imputed",
            },
            {
                "patient_id": "synth_003",
                "t_minutes": 180.0,
                "variable": "na",
                "value": 999.0,
                "unit": "mmol/L",
                "source": "notes_locf_imputed",
            },
        ]
    )

    frame = pd.DataFrame(rows)
    keep = ~frame["source"].str.contains("imputed", na=False)
    return frame[keep].reset_index(drop=True)


def _build_admission() -> pd.DataFrame:
    rows = [
        {"patient_id": pid, "field": field, "value": value}
        for pid, facts in _ADMISSION_FACTS
        for field, value in facts.items()
    ]
    return pd.DataFrame(rows)


def _build_imaging() -> pd.DataFrame:
    rows = [
        {
            "patient_id": "synth_001",
            "t_minutes": 0.0,
            "modality": "CT",
            "report_text": "Non-contrast head CT: early ischaemic changes in left MCA territory.",
            "image_refs": None,
        },
        {
            "patient_id": "synth_002",
            "t_minutes": 0.0,
            "modality": "CT",
            "report_text": "Non-contrast head CT: hyperdense right MCA sign.",
            "image_refs": None,
        },
    ]
    return pd.DataFrame(rows)


def _build_ai_output(rng: np.random.Generator) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for pid, _ in _ADMISSION_FACTS:
        for t in _TIMEPOINTS:
            payload = {
                "prob_deterioration_6h": float(rng.uniform(0.05, 0.5)),
                "prob_mrs_0_2_90d": float(rng.uniform(0.3, 0.8)),
            }
            rows.append(
                {
                    "patient_id": pid,
                    "t_minutes": t,
                    "model_id": _MODEL_ID,
                    "output_json": json.dumps(payload, sort_keys=True),
                }
            )
    return pd.DataFrame(rows)
