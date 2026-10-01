"""Deterministic, fully synthetic Geneva AI artifact fixture (S7).

Reproduces the observed structure of the production artifact (spec
``session-07.md`` §1) with fabricated values, a small ``N`` and ``F`` but the
real ``T = 72``::

    test_predictions.pkl            (y_true float64, y_prob float32), patient major
    test_patient_ids.csv            case_admission_id header + N ids
    shap_explanations_over_time/
      shap_feature_names.pkl        F names
      tree_explainer_shap_values_over_ts.pkl
                                    72 float32 (N, F + 1); last column = bias
    xgb_final_model.model           a few bytes (only hashed)
    geneva_ai_sample.csv            clinical CSV: the N ids + one non test patient
    test_split.pth                  fake torch zip for scripts/export_geneva_test_ids.py
    expected_ai_output.csv          canonical rows, computed here independently

``y_prob = sigmoid(row sum)`` so the alignment check passes. ``y_true`` is the
sentinel :data:`Y_TRUE_SENTINEL` (greppable in leakage tests) except for the
positive rows of patient 1, which the fake split lists as an outcome event.

Run:
    uv run python tests/fixtures/geneva_ai/build_geneva_ai_fixture.py
    uv run python tests/fixtures/geneva_ai/build_geneva_ai_fixture.py --check
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import pickle
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

FIXTURE_DIR = Path(__file__).resolve().parent
CLINICAL_SOURCE = FIXTURE_DIR.parent / "geneva" / "geneva_sample.csv"

TIMESTEPS = 72
PICKLE_PROTOCOL = 4  # observed in the production artifact
TEST_IDS = ("900001_0001", "900002_0001", "900003_0002")
NON_TEST_ID = "900009_0001"
FEATURE_NAMES = ("ALAT", "avg_heart_rate", "lag2_glucose", "timestep_idx")
BIAS = -0.640625  # exact in float32
MODEL_ID = "fixture_xgb"
MODEL_BYTES = b"fabricated xgboost model bytes - hashed, never loaded\n"

#: Never a probability the adapter may emit; leakage tests grep for it.
Y_TRUE_SENTINEL = 0.918273645
POSITIVE_PATIENT = 1
POSITIVE_TIMESTEPS = range(10, 16)

#: Clinical fixture ids → S7 ids (``geneva_fixture_NNN`` don't match ``^\d+_\d+$``).
CLINICAL_ID_MAP = {
    TEST_IDS[0]: "geneva_fixture_001",
    TEST_IDS[1]: "geneva_fixture_002",
    TEST_IDS[2]: "geneva_fixture_001",
    NON_TEST_ID: "geneva_fixture_002",
}

PREDICTIONS_NAME = "test_predictions.pkl"
SIDECAR_NAME = "test_patient_ids.csv"
EXPLANATIONS_DIR = "shap_explanations_over_time"
FEATURE_NAMES_FILE = "shap_feature_names.pkl"
SHAP_VALUES_FILE = "tree_explainer_shap_values_over_ts.pkl"
MODEL_NAME = "xgb_final_model.model"
CLINICAL_NAME = "geneva_ai_sample.csv"
SPLIT_NAME = "test_split.pth"
SPLIT_ARCHIVE_DIR = "test_split"
EXPECTED_NAME = "expected_ai_output.csv"

#: Fixed zip timestamp so the fake split is byte stable.
ZIP_DATE_TIME = (2026, 1, 1, 0, 0, 0)
SPLIT_SAMPLE_LABELS = ("NIHSS", "glucose")


def shap_values() -> list[np.ndarray]:
    """``shap[t][i, j]``: small fabricated log odds, bias in the last column."""
    arrays = []
    for t in range(TIMESTEPS):
        array = np.empty((len(TEST_IDS), len(FEATURE_NAMES) + 1), dtype=np.float32)
        for i in range(len(TEST_IDS)):
            for j in range(len(FEATURE_NAMES)):
                array[i, j] = (((i + 1) * (j + 1) * (t + 1)) % 17 - 8) / 100
            array[i, -1] = BIAS
        arrays.append(array)
    return arrays


def predictions(shap: list[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Patient major: row ``r`` → patient ``r // 72``, timestep ``r % 72``."""
    y_prob = np.empty(len(TEST_IDS) * TIMESTEPS, dtype=np.float32)
    y_true = np.full(len(TEST_IDS) * TIMESTEPS, Y_TRUE_SENTINEL, dtype=np.float64)
    for i in range(len(TEST_IDS)):
        for t in range(TIMESTEPS):
            logit = shap[t][i].astype(np.float64).sum()
            y_prob[i * TIMESTEPS + t] = 1.0 / (1.0 + np.exp(-logit))
    for t in POSITIVE_TIMESTEPS:
        y_true[POSITIVE_PATIENT * TIMESTEPS + t] = 1.0
    return y_true, y_prob


def sidecar_bytes(ids: tuple[str, ...] = TEST_IDS) -> bytes:
    return ("case_admission_id\n" + "".join(f"{pid}\n" for pid in ids)).encode("utf-8")


def expected_rows(shap: list[np.ndarray], y_prob: np.ndarray) -> list[dict[str, str]]:
    """Canonical rows built without the adapter, for exact comparison."""
    rows = []
    for i, pid in enumerate(TEST_IDS):
        for t in range(TIMESTEPS):
            payload = {
                "probability": float(y_prob[i * TIMESTEPS + t]),
                "explanation": {
                    "base_value": float(shap[t][i, -1]),
                    "contributions": {
                        name: float(shap[t][i, j]) for j, name in enumerate(FEATURE_NAMES)
                    },
                },
            }
            rows.append(
                {
                    "patient_id": pid,
                    "t_minutes": repr(t * 60.0),
                    "model_id": MODEL_ID,
                    "output_json": json.dumps(payload, sort_keys=True, separators=(",", ":")),
                }
            )
    return rows


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _clinical_bytes() -> bytes:
    source = pd.read_csv(CLINICAL_SOURCE, dtype=str, keep_default_na=False)
    frames = []
    for new_id, old_id in CLINICAL_ID_MAP.items():
        frame = source[source["case_admission_id"] == old_id].copy()
        frame["case_admission_id"] = new_id
        frames.append(frame)
    return pd.concat(frames).to_csv(index=False, lineterminator="\n").encode("utf-8")


def _split_bytes() -> bytes:
    """Fake torch zip: ``<dir>/data.pkl`` = ``(X, outcome_events)``, ``<dir>/version``."""
    labels = len(SPLIT_SAMPLE_LABELS)
    x = np.empty((len(TEST_IDS), TIMESTEPS, labels, 4), dtype=object)
    for i, pid in enumerate(TEST_IDS):
        for t in range(TIMESTEPS):
            for k, label in enumerate(SPLIT_SAMPLE_LABELS):
                x[i, t, k] = [pid, t, label, 0.0]
    events = pd.DataFrame(
        {
            "case_admission_id": pd.Series([TEST_IDS[POSITIVE_PATIENT]] * 2, dtype=object),
            "relative_sample_date_hourly_cat": [12.0, 14.0],
        }
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for name, data in (
            ("data.pkl", pickle.dumps((x, events), protocol=2)),
            ("version", b"3\n"),
        ):
            archive.writestr(zipfile.ZipInfo(f"{SPLIT_ARCHIVE_DIR}/{name}", ZIP_DATE_TIME), data)
    return buffer.getvalue()


def build() -> dict[str, bytes]:
    """Every fixture file as ``relative path → bytes``."""
    shap = shap_values()
    y_true, y_prob = predictions(shap)
    return {
        PREDICTIONS_NAME: pickle.dumps((y_true, y_prob), protocol=PICKLE_PROTOCOL),
        SIDECAR_NAME: sidecar_bytes(),
        f"{EXPLANATIONS_DIR}/{FEATURE_NAMES_FILE}": pickle.dumps(
            list(FEATURE_NAMES), protocol=PICKLE_PROTOCOL
        ),
        f"{EXPLANATIONS_DIR}/{SHAP_VALUES_FILE}": pickle.dumps(shap, protocol=PICKLE_PROTOCOL),
        MODEL_NAME: MODEL_BYTES,
        CLINICAL_NAME: _clinical_bytes(),
        SPLIT_NAME: _split_bytes(),
        EXPECTED_NAME: _csv_bytes(expected_rows(shap, y_prob)),
    }


def write(directory: Path) -> None:
    for relative, data in build().items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _check() -> int:
    drift = [rel for rel, data in build().items() if _read(FIXTURE_DIR / rel) != data]
    if drift:
        print(f"Geneva AI fixture drift: {drift}; rerun without --check", file=sys.stderr)
        return 1
    return 0


def _read(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="exit 1 when on-disk files drift")
    parser.add_argument("--out", type=Path, default=None, help="write into this directory")
    args = parser.parse_args(argv[1:])

    if args.check:
        return _check()

    write(args.out or FIXTURE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
