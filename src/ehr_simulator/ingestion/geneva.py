"""Geneva preprocessed-features adapter.

Loads the Geneva stroke-unit CSV at the path configured in
``.EXAMPLE_DATA_PATHS`` into the four canonical in-memory shapes from
:mod:`ehr_simulator.ingestion.canonical`.

Routing rules (encoded in :func:`load_geneva`):

==============================  ==========================================
``source`` value                Action
==============================  ==========================================
contains ``"imputed"``          drop before validation (substring match)
``"EHR"`` (exact)               route to ``SCALAR_TS``; inverse-normalize via
                                ``normalisation_parameters.csv`` when
                                ``variable`` is in the params; unit from
                                ``geneva_units.json`` (sourced from the
                                upstream OPSUM xlsx — see
                                ``scripts/build_geneva_units.py``);
                                ``t_minutes = relative_sample_date_hourly_cat
                                * 60.0``
``"stroke_registry"`` (exact)   route to ``ADMISSION``; take the ``t=0``
                                slice only; for one-hot categorical groups
                                listed in
                                ``categorical_variable_encoding.csv``,
                                apply ``>=0.5`` thresholding + group
                                re-expansion via
                                :func:`_decode_categorical`; for continuous
                                registry vars (``age``, ``weight``,
                                ``prestroke_disability_(rankin)_*``)
                                inverse-normalize and str-coerce
==============================  ==========================================

``IMAGING`` and ``AI_OUTPUT`` are returned as empty-but-conforming
DataFrames. Geneva imaging-derived scalars (``cbf_lt_30``, ``tmax_gt_6``,
…) live in ``EHR`` rows and route through ``SCALAR_TS``; AI predictions
land in S7. Empty frames keep downstream code special-case-free.

The ``EHR_SIM_DATA_ROOT`` environment variable, when set to a non-empty
value, scopes both ``csv_path`` and ``params_dir`` via
:func:`_path_traversal_guard`. Empty-string and unset are treated
identically (passthrough). S5's ``validate-adapter`` CLI tightens this to
required (deferred).

S4 lifted every dataset-agnostic helper into
:mod:`ehr_simulator.ingestion._shared`; this module retains only the
Geneva-specific ``_load_units`` (geneva_units.json) and the public
``load_geneva`` orchestrator that supplies Geneva-specific arguments to
the lifted helpers.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ehr_simulator.ingestion._shared import (
    CategoricalGroup,
    _apply_scalar_ts_inverse_normalize,
    _build_admission,
    _build_scalar_ts,
    _decode_categorical,
    _drop_imputed,
    _inverse_normalize,
    _load_categorical_encoding,
    _load_normalisation_params,
    _one_hot_column_name,
    _path_traversal_guard,
    _read_features_csv,
    _validate_and_collect,
)
from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue

__all__ = [
    "CategoricalGroup",
    "GenevaDataset",
    "load_geneva",
    "_apply_scalar_ts_inverse_normalize",
    "_build_admission",
    "_build_scalar_ts",
    "_decode_categorical",
    "_drop_imputed",
    "_inverse_normalize",
    "_load_categorical_encoding",
    "_load_normalisation_params",
    "_load_units",
    "_one_hot_column_name",
    "_path_traversal_guard",
    "_read_features_csv",
    "_validate_and_collect",
]

_DATASET_NAME = "geneva"
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)
_NON_IMPUTED_SOURCES: tuple[str, ...] = ("EHR", "stroke_registry")
_REGISTRY_SOURCE = "stroke_registry"

# Map Geneva's per-hour-aggregate variable names to the canonical short codes
# the panel layer (web/panels.py) filters on. Geneva ships min/median/max
# triplets per vital per hour bucket; for now we surface the median under the
# canonical name so the vitals panel renders. min_* and max_* rows pass
# through unchanged — they stay in scalar_ts but won't match the vitals
# filter, leaving room for an S8 band-rendering pass.
#
# Lab renames map French Geneva names to the canonical English short codes
# the labs panel expects (hgb, na, cr, glucose, wbc, plt). `glucose` maps to
# itself.
_VARIABLE_RENAMES: dict[str, str] = {
    # Vitals
    "median_heart_rate": "hr",
    "median_systolic_blood_pressure": "sbp",
    "median_diastolic_blood_pressure": "dbp",
    "median_respiratory_rate": "rr",
    "median_oxygen_saturation": "spo2",
    "temperature": "temp",
    # Labs
    "hemoglobine": "hgb",
    "sodium": "na",
    "creatinine": "cr",
    "leucocytes": "wbc",
    "thrombocytes": "plt",
}


@dataclass
class GenevaDataset:
    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame
    issues: list[IngestionIssue] = field(default_factory=list)


def load_geneva(
    csv_path: Path,
    params_dir: Path,
    *,
    strict: bool = True,
) -> GenevaDataset:
    """Load the Geneva preprocessed-features CSV into the four canonical shapes.

    ``params_dir`` must contain ``normalisation_parameters.csv`` and
    ``categorical_variable_encoding.csv``. Both ``csv_path`` and
    ``params_dir`` are validated via :func:`_path_traversal_guard` against
    ``EHR_SIM_DATA_ROOT`` if set to a non-empty value.

    ``strict=True`` (default): every frame is validated with pandera's
    eager mode; first violation raises :class:`AdapterError`. Ambiguous
    categorical decodes also raise.

    ``strict=False``: lenient validation collects offending rows into
    ``GenevaDataset.issues`` and returns the surviving rows; ambiguous
    categorical decodes pick ``argmax`` and append an issue. Imaging and
    AI_OUTPUT frames are empty either way.
    """
    csv_path = Path(csv_path)
    params_dir = Path(params_dir)
    root_str = os.environ.get("EHR_SIM_DATA_ROOT") or None
    root = Path(root_str) if root_str else None
    csv_path = _path_traversal_guard(csv_path, root, dataset=_DATASET_NAME)
    params_dir = _path_traversal_guard(params_dir, root, dataset=_DATASET_NAME)

    issues: list[IngestionIssue] = []

    frame, read_issues = _read_features_csv(
        csv_path,
        required_columns=_REQUIRED_COLUMNS,
        dataset=_DATASET_NAME,
        known_sources=_NON_IMPUTED_SOURCES,
    )
    issues.extend(read_issues)
    norm_params = _load_normalisation_params(
        params_dir / "normalisation_parameters.csv", dataset=_DATASET_NAME
    )
    sample_labels = set(frame["sample_label"].unique().tolist())
    cat_groups = _load_categorical_encoding(
        params_dir / "categorical_variable_encoding.csv",
        sample_labels,
        dataset=_DATASET_NAME,
    )
    units = _load_units()

    frame = frame.copy()
    frame["t_minutes"] = frame["relative_sample_date_hourly_cat"].astype(float) * 60.0
    frame = frame.rename(columns={"case_admission_id": "patient_id"})

    ehr_rows = frame[frame["source"] == "EHR"]
    registry_rows = frame[frame["source"] == _REGISTRY_SOURCE]

    scalar_ts, scalar_issues = _build_scalar_ts(
        ehr_rows, norm_params, units=units, dataset=_DATASET_NAME
    )
    issues.extend(scalar_issues)

    admission, admission_issues = _build_admission(
        registry_rows, norm_params, cat_groups, strict=strict, dataset=_DATASET_NAME
    )
    issues.extend(admission_issues)

    imaging = empty_frame(CanonicalShape.IMAGING)
    ai_output = empty_frame(CanonicalShape.AI_OUTPUT)

    scalar_ts = _validate_and_collect(
        scalar_ts, CanonicalShape.SCALAR_TS, strict=strict, issues=issues, dataset=_DATASET_NAME
    )
    scalar_ts = _apply_scalar_ts_inverse_normalize(scalar_ts, norm_params)
    if not scalar_ts.empty:
        scalar_ts = scalar_ts.copy()
        scalar_ts["variable"] = scalar_ts["variable"].replace(_VARIABLE_RENAMES)
    admission = _validate_and_collect(
        admission, CanonicalShape.ADMISSION, strict=strict, issues=issues, dataset=_DATASET_NAME
    )
    imaging = _validate_and_collect(
        imaging, CanonicalShape.IMAGING, strict=strict, issues=issues, dataset=_DATASET_NAME
    )
    ai_output = _validate_and_collect(
        ai_output, CanonicalShape.AI_OUTPUT, strict=strict, issues=issues, dataset=_DATASET_NAME
    )

    return GenevaDataset(
        scalar_ts=scalar_ts,
        admission=admission,
        imaging=imaging,
        ai_output=ai_output,
        issues=issues,
    )


@functools.lru_cache(maxsize=1)
def _load_units() -> dict[str, str]:
    try:
        package = importlib.resources.files("ehr_simulator.ingestion")
        resource = package / "data" / "geneva_units.json"
        text = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise AdapterError(
            f"[{_DATASET_NAME}] geneva_units.json not found in package data — "
            "wheel build is missing the file",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason="geneva_units.json missing from package data",
                )
            ],
        ) from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AdapterError(
            f"[{_DATASET_NAME}] geneva_units.json is not valid JSON: {exc}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=f"geneva_units.json is not valid JSON: {exc}",
                )
            ],
        ) from exc

    if not isinstance(data, dict):
        raise AdapterError(
            f"[{_DATASET_NAME}] geneva_units.json must be a JSON object",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason="geneva_units.json must be a JSON object",
                )
            ],
        )
    return {str(k): str(v) for k, v in data.items()}
