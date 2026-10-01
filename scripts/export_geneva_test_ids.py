"""Export the Geneva test-subset patient order into ``test_patient_ids.csv`` (S7).

One off, operator run. The AI predictions and SHAP files carry no patient
ids; their row order is the test split's. This script reads that order from
the split ``.pth`` once so the simulator never has to::

    test_data_..._ts0.8_rs42_ns5.pth ──> X[:, 0, 0, 0] ──┐
                                         outcome_events ─┼─ cross check ──> test_patient_ids.csv
    test_predictions.pkl ───────────────> y_true ────────┘

Checks: the id is constant per patient, the timestep field equals
``0..71``, and the patients with an outcome event are exactly the patients
with any ``y_true = 1`` (else exit 1, nothing written). Prints counts only:
no ids, values or outcomes.

Only run on the trusted local artifact: pickle is executable input. The
unpickler resolves numpy and pandas globals only (plus the ``Int64Index``
shim for files pickled with pandas < 2); torch is not needed.

Run:
    uv run python scripts/export_geneva_test_ids.py SPLIT.pth PREDICTIONS.pkl OUT.csv
"""

from __future__ import annotations

import argparse
import io
import os
import pickle
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

SOURCE_TIMESTEPS = 72
SIDECAR_HEADER = "case_admission_id"
DATA_PICKLE_SUFFIX = "/data.pkl"
POSITIVE_LABEL = 1.0

#: Fields of the split's ``X[patient, timestep, sample]`` record.
ID_FIELD = 0
TIMESTEP_FIELD = 1

_ALLOWED_MODULE_ROOTS = ("numpy", "pandas")
_ALLOWED_BUILTINS = {("__builtin__", "slice"), ("builtins", "slice"), ("_codecs", "encode")}

#: Removed in pandas 2; its pickles rebuild as a plain ``pandas.Index``.
_PANDAS_SHIMS = {("pandas.core.indexes.numeric", "Int64Index"): pd.Index}


class ExportError(Exception):
    pass


class _SplitUnpickler(pickle.Unpickler):
    def find_class(self, module: str, name: str) -> Any:
        shim = _PANDAS_SHIMS.get((module, name))
        if shim is not None:
            return shim
        root = module.split(".", 1)[0]
        if root in _ALLOWED_MODULE_ROOTS or (module, name) in _ALLOWED_BUILTINS:
            return super().find_class(module, name)
        raise ExportError(f"pickle global {module}.{name} is not allowed")

    def persistent_load(self, pid: Any) -> Any:
        raise ExportError("pickle persistent ids (tensors) are not expected in the split")


def _unpickle(data: bytes) -> Any:
    return _SplitUnpickler(io.BytesIO(data)).load()


def read_split(path: Path) -> tuple[np.ndarray, pd.DataFrame]:
    """``(X, outcome_events)`` from the torch zip, without torch."""
    with zipfile.ZipFile(path) as archive:
        names = [n for n in archive.namelist() if n.endswith(DATA_PICKLE_SUFFIX)]
        if len(names) != 1:
            raise ExportError(f"expected one {DATA_PICKLE_SUFFIX} in the split, found {len(names)}")
        obj = _unpickle(archive.read(names[0]))

    if not isinstance(obj, tuple) or len(obj) != 2:
        raise ExportError("split data.pkl must be a (X, outcome_events) tuple")
    x, events = obj
    if not isinstance(x, np.ndarray) or x.ndim != 4:
        raise ExportError("split X must be a 4-D array (patient, timestep, sample, field)")
    if not isinstance(events, pd.DataFrame) or SIDECAR_HEADER not in events.columns:
        raise ExportError(f"split outcome_events must be a DataFrame with {SIDECAR_HEADER!r}")
    return x, events


def patient_order(x: np.ndarray) -> list[str]:
    """``X[:, 0, 0, 0]``, after checking the id and timestep fields are consistent."""
    if x.shape[1] != SOURCE_TIMESTEPS:
        raise ExportError(f"split has {x.shape[1]} timesteps, expected {SOURCE_TIMESTEPS}")

    ids = [str(v) for v in x[:, 0, 0, ID_FIELD]]
    if not (x[:, :, :, ID_FIELD] == x[:, :1, :1, ID_FIELD]).all():
        raise ExportError("case_admission_id is not constant per patient")
    timesteps = x[:, :, :, TIMESTEP_FIELD].astype(np.int64)
    expected = np.arange(SOURCE_TIMESTEPS).reshape(1, -1, 1)
    if not (timesteps == expected).all():
        raise ExportError(f"timestep field does not equal 0..{SOURCE_TIMESTEPS - 1}")
    if len(set(ids)) != len(ids):
        raise ExportError("split holds duplicate case_admission_id values")
    return ids


def read_y_true(path: Path) -> np.ndarray:
    obj = _unpickle(path.read_bytes())
    if not isinstance(obj, tuple) or len(obj) != 2 or not isinstance(obj[0], np.ndarray):
        raise ExportError("predictions must be a (y_true, y_prob) tuple")
    return obj[0]


def cross_check(ids: list[str], events: pd.DataFrame, y_true: np.ndarray) -> None:
    """Outcome-event patients must be exactly the patients with any ``y_true = 1``."""
    if len(y_true) != len(ids) * SOURCE_TIMESTEPS:
        raise ExportError(
            f"predictions hold {len(y_true)} rows, expected {len(ids)} x {SOURCE_TIMESTEPS}"
        )
    positive = (y_true.reshape(len(ids), SOURCE_TIMESTEPS) == POSITIVE_LABEL).any(axis=1)
    by_label = {pid for pid, hit in zip(ids, positive, strict=True) if hit}
    by_event = set(events[SIDECAR_HEADER].astype(str))
    if by_label != by_event:
        raise ExportError(
            f"patient order cross check failed: {len(by_event ^ by_label)} patients differ "
            "between outcome events and y_true"
        )


def write_sidecar(ids: list[str], out: Path) -> None:
    """Atomic: a temp file in the target directory, then ``os.replace``."""
    out.parent.mkdir(parents=True, exist_ok=True)
    text = SIDECAR_HEADER + "\n" + "".join(f"{pid}\n" for pid in ids)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, out)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def export(split_path: Path, predictions_path: Path, out: Path) -> int:
    """Write the sidecar; return the patient count."""
    x, events = read_split(split_path)
    ids = patient_order(x)
    cross_check(ids, events, read_y_true(predictions_path))
    write_sidecar(ids, out)
    return len(ids)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("split", type=Path, help="test split .pth")
    parser.add_argument("predictions", type=Path, help="test_predictions.pkl")
    parser.add_argument("out", type=Path, help="sidecar CSV to write")
    args = parser.parse_args(argv[1:])

    try:
        count = export(args.split, args.predictions, args.out)
    except (ExportError, OSError, zipfile.BadZipFile, pickle.UnpicklingError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"wrote {count} test patient ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
