"""Geneva AI predictions adapter (S7): precomputed model output → ``AI_OUTPUT``.

Only load the trusted, locally supplied Geneva model artifact. Python pickle
deserialisation is not safe for untrusted files. The restricted unpickler
below is defence in depth, not a claim of safety.

Neither artifact carries patient ids or timesteps; rows are positional::

    test_patient_ids.csv ── id of test patient i ───────────────┐
    test_predictions.pkl ── (y_true, y_prob), row r ─────────────┼─> AI_OUTPUT row
                            patient r // 72, timestep r % 72     │   patient_id = ids[r // 72]
    shap_explanations_over_time/                                 │   t_minutes  = (r % 72) * 60
      tree_explainer_shap_values_over_ts.pkl  shap[t][i, :]  ────┤   output_json = {probability,
      shap_feature_names.pkl                  column names  ─────┘                  explanation}
    xgb_final_model.model ── hashed only → model_system_version

Each file is read into memory once; its SHA256 and its parse come from the
same bytes. ``y_true`` is unpickled with the tuple and never used.

Produced by ``JulianKlug/OPSUM`` ``prediction/short_term_outcome_prediction``
(``testing/test_xgb.py``, ``testing/compute_shap_explanations_over_time.py``);
its features at timestep ``t`` use bins ``0..t`` only (spec §7.1).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import pickle
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from numpy._core.multiarray import _reconstruct

from ehr_simulator.ingestion._shared import _path_traversal_guard
from ehr_simulator.ingestion.canonical import CanonicalShape, validate
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue
from ehr_simulator.ingestion.provenance import AIArtifactProvenance

__all__ = [
    "GENEVA_CASE_ADMISSION_ID_PATTERN",
    "SHAP_PROBABILITY_TOLERANCE",
    "SOURCE_TIMESTEPS",
    "GenevaAIOutput",
    "GenevaAISource",
    "load_geneva_ai_predictions",
]

_DATASET_NAME = "geneva"

#: Hourly timesteps per test patient in the predictions and SHAP artifacts.
SOURCE_TIMESTEPS = 72
_MINUTES_PER_TIMESTEP = 60.0

#: Largest allowed ``|sigmoid(sum(shap row)) - y_prob|`` (observed max 3.6e-7).
SHAP_PROBABILITY_TOLERANCE = 1e-5

#: Canonical Geneva patient id, e.g. ``900001_0001``.
GENEVA_CASE_ADMISSION_ID_PATTERN = re.compile(r"^\d+_\d+$")

_SIDECAR_HEADER = "case_admission_id"
_SHAP_FEATURE_NAMES_FILE = "shap_feature_names.pkl"
_SHAP_VALUES_FILE = "tree_explainer_shap_values_over_ts.pkl"
_EXPLANATION_FILES = frozenset({_SHAP_FEATURE_NAMES_FILE, _SHAP_VALUES_FILE})

#: ``(y_true, y_prob)``; only ``y_prob`` is read.
_PREDICTION_TUPLE_LENGTH = 2
_Y_TRUE_INDEX = 0
_Y_PROB_INDEX = 1

#: Fixed digest roles (§5.1): renaming a file must not move the digest.
_ROLE_PATIENT_IDS = "patient_ids"
_ROLE_PREDICTIONS = "predictions"

_AI_KEY = ["patient_id", "t_minutes", "model_id"]

#: The only pickle globals the artifacts may reference. ``numpy._core`` is
#: the NumPy 2 spelling of a file re-saved after the upgrade.
_ALLOWED_PICKLE_GLOBALS: dict[tuple[str, str], Any] = {
    ("numpy", "dtype"): np.dtype,
    ("numpy", "ndarray"): np.ndarray,
    ("numpy.core.multiarray", "_reconstruct"): _reconstruct,
    ("numpy._core.multiarray", "_reconstruct"): _reconstruct,
}


@dataclass(frozen=True)
class GenevaAISource:
    """Resolved Geneva AI artifact paths plus the operator chosen ``model_id``."""

    predictions_path: Path
    patient_ids_path: Path
    model_path: Path
    model_id: str
    explanations_dir: Path | None = None


@dataclass(frozen=True)
class GenevaAIOutput:
    ai_output: pd.DataFrame
    provenance: AIArtifactProvenance


@dataclass(frozen=True)
class _Explanations:
    feature_names: tuple[str, ...]
    values: tuple[np.ndarray, ...]  # one (N, F + 1) array per timestep
    digest: str


def load_geneva_ai_predictions(
    ai_source: GenevaAISource,
    *,
    patient_ids: Sequence[str] | None = None,
) -> GenevaAIOutput:
    """Parse the Geneva AI artifact into a strict canonical ``AI_OUTPUT`` frame.

    ``patient_ids`` restricts the rows; a requested id absent from the test
    subset gets no rows. Any artifact problem raises :class:`AdapterError`.
    """
    model_id = ai_source.model_id
    if not model_id.strip():
        raise _error("model_id must be non-empty")

    root_str = os.environ.get("EHR_SIM_DATA_ROOT") or None
    root = Path(root_str) if root_str else None
    predictions_path = _guard(ai_source.predictions_path, root)
    patient_ids_path = _guard(ai_source.patient_ids_path, root)
    model_path = _guard(ai_source.model_path, root)
    explanations_dir = (
        None if ai_source.explanations_dir is None else _guard(ai_source.explanations_dir, root)
    )

    # Hash and parse the same in-memory bytes.
    sidecar_bytes = _read_bytes(patient_ids_path)
    predictions_bytes = _read_bytes(predictions_path)
    model_bytes = _read_bytes(model_path)

    test_ids = _parse_sidecar(sidecar_bytes)
    y_prob = _parse_predictions(predictions_bytes)
    n_patients = _patient_count(y_prob)
    if len(test_ids) != n_patients:
        raise _error(
            f"sidecar holds {len(test_ids)} ids but predictions cover {n_patients} "
            f"patients ({len(y_prob)} rows / {SOURCE_TIMESTEPS} timesteps)"
        )

    explanations = None
    if explanations_dir is not None:
        explanations = _parse_explanations(explanations_dir, n_patients)

    retained = _retained_positions(test_ids, patient_ids)
    if explanations is not None:
        _check_alignment(explanations, y_prob, retained)

    frame = _build_frame(test_ids, y_prob, explanations, retained, model_id)
    frame = validate(frame, CanonicalShape.AI_OUTPUT, strict=True, dataset=_DATASET_NAME)

    provenance = AIArtifactProvenance(
        prediction_sha256=_prediction_digest(sidecar_bytes, predictions_bytes),
        explanation_sha256=None if explanations is None else explanations.digest,
        model_system_version=_sha256(model_bytes),
    )
    return GenevaAIOutput(ai_output=frame, provenance=provenance)


# ---------------------------------------------------------------------------
# Raw bytes and restricted unpickling
# ---------------------------------------------------------------------------


class _RestrictedUnpickler(pickle.Unpickler):
    """Resolve only :data:`_ALLOWED_PICKLE_GLOBALS`; anything else is refused
    at the GLOBAL opcode, before the object it names is built."""

    def find_class(self, module: str, name: str) -> Any:
        allowed = _ALLOWED_PICKLE_GLOBALS.get((module, name))
        if allowed is None:
            raise _error(f"pickle global {module}.{name} is not allowed")
        return allowed

    def persistent_load(self, pid: Any) -> Any:
        raise _error("pickle persistent ids are not allowed")


def _unpickle(data: bytes, label: str) -> Any:
    try:
        return _RestrictedUnpickler(io.BytesIO(data)).load()
    except AdapterError:
        raise
    except Exception as exc:
        # Type name only: a parser message may quote artifact content.
        raise _error(f"{label} could not be unpickled ({type(exc).__name__})") from exc


def _read_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise _error(f"artifact file not found or not a regular file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise _error(f"artifact file could not be read: {path} ({type(exc).__name__})") from exc


def _guard(path: Path, root: Path | None) -> Path:
    return _path_traversal_guard(Path(path), root, dataset=_DATASET_NAME)


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def _parse_sidecar(data: bytes) -> tuple[str, ...]:
    """``case_admission_id`` header, then one unique ``<digits>_<digits>`` id per row."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _error("sidecar is not UTF-8") from exc

    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or rows[0] != [_SIDECAR_HEADER]:
        raise _error(f"sidecar header must be exactly {_SIDECAR_HEADER!r}")

    body = rows[1:]
    if any(len(row) != 1 for row in body):
        raise _error("sidecar rows must hold exactly one field")

    ids = tuple(row[0] for row in body)
    bad = sum(1 for pid in ids if not GENEVA_CASE_ADMISSION_ID_PATTERN.fullmatch(pid))
    if bad:
        # Count only: an id-format regression must not empty the AI frame silently.
        raise _error(f"{bad} sidecar ids do not match {GENEVA_CASE_ADMISSION_ID_PATTERN.pattern!r}")
    if len(set(ids)) != len(ids):
        raise _error("sidecar holds duplicate case_admission_id values")
    return ids


def _parse_predictions(data: bytes) -> np.ndarray:
    """Return ``y_prob``; ``y_true`` is only shape checked."""
    obj = _unpickle(data, "predictions")
    if not isinstance(obj, tuple) or len(obj) != _PREDICTION_TUPLE_LENGTH:
        raise _error(f"predictions must be a {_PREDICTION_TUPLE_LENGTH}-tuple (y_true, y_prob)")

    y_true, y_prob = obj[_Y_TRUE_INDEX], obj[_Y_PROB_INDEX]
    if not all(isinstance(a, np.ndarray) and a.ndim == 1 for a in (y_true, y_prob)):
        raise _error("predictions tuple items must be 1-D arrays")
    if len(y_true) != len(y_prob):
        raise _error("predictions arrays differ in length")
    if not np.issubdtype(y_prob.dtype, np.floating):
        raise _error(f"y_prob must be a float array; got dtype {y_prob.dtype}")
    if not np.isfinite(y_prob).all():
        raise _error("y_prob holds NaN or infinite values")
    if ((y_prob < 0) | (y_prob > 1)).any():
        raise _error("y_prob holds values outside [0, 1]")
    return y_prob


def _patient_count(y_prob: np.ndarray) -> int:
    n_rows = len(y_prob)
    if n_rows == 0 or n_rows % SOURCE_TIMESTEPS:
        raise _error(
            f"predictions length {n_rows} is not a positive multiple of {SOURCE_TIMESTEPS}"
        )
    return n_rows // SOURCE_TIMESTEPS


def _parse_explanations(directory: Path, n_patients: int) -> _Explanations:
    """Exactly the two SHAP files: 72 float32 ``(N, F + 1)`` arrays + F names."""
    if directory.is_symlink() or not directory.is_dir():
        raise _error(f"explanations directory not found: {directory}")

    with os.scandir(directory) as entries:
        listing = list(entries)
    for entry in listing:
        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
            raise _error(f"explanations directory holds a non regular entry: {entry.name}")

    names_found = {entry.name for entry in listing}
    if names_found != _EXPLANATION_FILES:
        missing = sorted(_EXPLANATION_FILES - names_found)
        extra = sorted(names_found - _EXPLANATION_FILES)
        raise _error(f"explanations directory drift: missing {missing}, unexpected {extra}")

    contents = {name: _read_bytes(directory / name) for name in sorted(names_found)}

    names = _unpickle(contents[_SHAP_FEATURE_NAMES_FILE], _SHAP_FEATURE_NAMES_FILE)
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise _error("SHAP feature names must be a list of strings")
    if not names or len(set(names)) != len(names):
        raise _error("SHAP feature names must be non-empty and unique")

    values = _unpickle(contents[_SHAP_VALUES_FILE], _SHAP_VALUES_FILE)
    expected_shape = (n_patients, len(names) + 1)  # + bias column
    if not isinstance(values, list) or len(values) != SOURCE_TIMESTEPS:
        raise _error(f"SHAP values must be a list of {SOURCE_TIMESTEPS} arrays")
    for array in values:
        if not isinstance(array, np.ndarray) or array.dtype != np.float32:
            raise _error("SHAP values must be float32 arrays")
        if array.shape != expected_shape:
            raise _error(f"SHAP array shape {array.shape} != expected {expected_shape}")
        if not np.isfinite(array).all():
            raise _error("SHAP values hold NaN or infinite values")

    return _Explanations(
        feature_names=tuple(names),
        values=tuple(values),
        digest=_directory_digest(contents),
    )


def _retained_positions(test_ids: tuple[str, ...], requested: Sequence[str] | None) -> list[int]:
    """Test-subset positions kept, in sidecar order. Unknown requests get no rows."""
    if requested is None:
        return list(range(len(test_ids)))
    wanted = set(requested)
    return [i for i, pid in enumerate(test_ids) if pid in wanted]


def _check_alignment(explanations: _Explanations, y_prob: np.ndarray, retained: list[int]) -> None:
    """``sigmoid(sum(shap[t][i, :]))`` must reproduce ``y_prob[72 i + t]``.

    The only guard against a reordered or swapped positional file.
    """
    if not retained:
        return

    by_patient = y_prob.reshape(-1, SOURCE_TIMESTEPS)[retained]  # (kept, T)
    failures = 0
    for t, shap_t in enumerate(explanations.values):
        logits = shap_t[retained].astype(np.float64).sum(axis=1)
        reconstructed = 1.0 / (1.0 + np.exp(-logits))
        deviation = np.abs(reconstructed - by_patient[:, t].astype(np.float64))
        failures += int((deviation > SHAP_PROBABILITY_TOLERANCE).sum())
    if failures:
        raise _error(
            f"SHAP/prediction alignment failed for {failures} rows "
            f"(tolerance {SHAP_PROBABILITY_TOLERANCE})"
        )


# ---------------------------------------------------------------------------
# Canonical rows
# ---------------------------------------------------------------------------


def _build_frame(
    test_ids: tuple[str, ...],
    y_prob: np.ndarray,
    explanations: _Explanations | None,
    retained: list[int],
    model_id: str,
) -> pd.DataFrame:
    """One row per retained ``(patient, timestep)``; row ``t`` reads ``shap[t]`` only."""
    records: list[dict[str, Any]] = []
    for i in retained:
        for t in range(SOURCE_TIMESTEPS):
            payload: dict[str, Any] = {
                "probability": _to_json_native(y_prob[i * SOURCE_TIMESTEPS + t])
            }
            if explanations is not None:
                payload["explanation"] = _explanation_payload(explanations, t, i)
            records.append(
                {
                    "patient_id": test_ids[i],
                    "t_minutes": t * _MINUTES_PER_TIMESTEP,
                    "model_id": model_id,
                    "output_json": _dumps(payload),
                }
            )

    frame = pd.DataFrame.from_records(
        records, columns=["patient_id", "t_minutes", "model_id", "output_json"]
    )
    if frame.duplicated(_AI_KEY).any():
        raise _error("duplicate (patient_id, t_minutes, model_id) AI rows")
    return frame


def _explanation_payload(explanations: _Explanations, t: int, i: int) -> dict[str, Any]:
    # Last column is the bias; the others follow the feature-name order.
    row = _to_json_native(explanations.values[t][i])
    return {
        "base_value": row[-1],
        "contributions": dict(zip(explanations.feature_names, row[:-1], strict=True)),
    }


def _to_json_native(value: Any) -> Any:
    """Convert to JSON-native Python values (§2.3); refuse NaN, inf, unknown types.

    Example: ``np.float32(0.5)`` → ``0.5``; ``np.array([1, 2])`` → ``[1, 2]``.
    """
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, np.ndarray):
        if not np.issubdtype(value.dtype, np.number):
            return [_to_json_native(v) for v in value.tolist()]
        # Numeric fast path: tolist() already yields native Python scalars.
        if not np.isfinite(value).all():
            raise _error("AI payload holds NaN or infinite values")
        return value.tolist()
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.generic):
        return _to_json_native(value.item())
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _error("AI payload holds NaN or infinite values")
        return value
    if isinstance(value, Mapping):
        return {str(k): _to_json_native(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_json_native(v) for v in value]
    raise _error(f"AI payload holds a non JSON value of type {type(value).__name__}")


def _dumps(payload: Mapping[str, Any]) -> str:
    try:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _error(f"AI payload is not JSON serialisable ({type(exc).__name__})") from exc


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(entries: list[dict[str, str]]) -> str:
    return _sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _prediction_digest(sidecar_bytes: bytes, predictions_bytes: bytes) -> str:
    """Predictions are positional: the sidecar decides whose they are, so both count."""
    return _canonical_digest(
        [
            {"role": _ROLE_PATIENT_IDS, "sha256": _sha256(sidecar_bytes)},
            {"role": _ROLE_PREDICTIONS, "sha256": _sha256(predictions_bytes)},
        ]
    )


def _directory_digest(contents: Mapping[str, bytes]) -> str:
    """Path-sorted ``{path, sha256}`` entries; independent of enumeration order."""
    return _canonical_digest(
        [{"path": name, "sha256": _sha256(contents[name])} for name in sorted(contents)]
    )


def _error(reason: str) -> AdapterError:
    return AdapterError(
        f"[{_DATASET_NAME}/ai] {reason}",
        issues=[
            IngestionIssue(dataset=_DATASET_NAME, patient_id=None, row_idx=None, reason=reason)
        ],
    )
