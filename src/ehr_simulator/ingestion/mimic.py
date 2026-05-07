"""MIMIC-III preprocessed-features adapter.

Loads the MIMIC stroke-cohort CSV at the path configured in
``.EXAMPLE_DATA_PATHS`` into the four canonical in-memory shapes from
:mod:`ehr_simulator.ingestion.canonical`. MIMIC mirrors Geneva's
preprocessed layout one-for-one with three deliberate differences:

==============================  ==========================================
Difference                      Handling
==============================  ==========================================
no unnamed-index column         ``pd.read_csv`` is called WITHOUT
                                ``index_col=0`` (Geneva has one; MIMIC
                                does not)
``notes`` replaces              ``stroke_registry`` is never matched;
``stroke_registry``             ``frame[frame.source == "notes"]`` routes
                                to ``ADMISSION``
no units source                 every SCALAR_TS row ships ``unit=None``;
                                ``geneva_units.json`` is not imported
==============================  ==========================================

Routing rules (encoded in :func:`load_mimic`):

==============================  ==========================================
``source`` value                Action
==============================  ==========================================
contains ``"imputed"``          drop before validation (substring match,
                                via :func:`_shared._drop_imputed`)
``"EHR"`` (exact)               route to ``SCALAR_TS``; inverse-normalize via
                                ``reference_population_normalisation_parameters.csv``
                                when ``variable`` is in the params (passthrough
                                + issue when not, per S3 post-review-3.2);
                                ``unit = None``;
                                ``t_minutes = relative_sample_date_hourly_cat
                                * 60.0``
``"notes"`` (exact)             route to ``ADMISSION``; take the ``t=0``
                                slice only; for one-hot categorical groups
                                listed in ``categorical_variable_encoding.csv``,
                                apply the ≥0.5 thresholding via
                                :func:`_shared._decode_categorical`; for
                                continuous registry vars, inverse-normalize
                                and str-coerce
==============================  ==========================================

``IMAGING`` and ``AI_OUTPUT`` are returned as empty-but-conforming
DataFrames via :func:`canonical.empty_frame`. MIMIC imaging-derived
scalars (``cbf_lt_30``, ``tmax_gt_6``, ``hypoperfusion_with_mismatch``,
…) live in ``EHR`` rows and route through ``SCALAR_TS`` — same as Geneva.
AI predictions for MIMIC are not in scope for any current session (no
upstream pkl exists).

The ``EHR_SIM_DATA_ROOT`` environment variable, when set, scopes both
``csv_path`` and ``params_dir`` via :func:`_shared._path_traversal_guard`.
Empty-string handling matches Geneva's S3 contract:
``os.environ.get("EHR_SIM_DATA_ROOT") or None`` is used, so an explicitly-
empty value is treated identically to unset.
"""

from __future__ import annotations

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
from ehr_simulator.ingestion.exceptions import IngestionIssue

__all__ = [
    "CategoricalGroup",
    "MimicDataset",
    "load_mimic",
    "_apply_scalar_ts_inverse_normalize",
    "_build_admission",
    "_build_scalar_ts",
    "_decode_categorical",
    "_drop_imputed",
    "_inverse_normalize",
    "_load_categorical_encoding",
    "_load_normalisation_params",
    "_one_hot_column_name",
    "_path_traversal_guard",
    "_read_features_csv",
    "_validate_and_collect",
]

_DATASET_NAME = "mimic"
_NORM_PARAMS_FILENAME = "reference_population_normalisation_parameters.csv"
_CATEGORICAL_ENCODING_FILENAME = "categorical_variable_encoding.csv"
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)
_NON_IMPUTED_SOURCES: tuple[str, ...] = ("EHR", "notes")
_REGISTRY_SOURCE = "notes"


@dataclass
class MimicDataset:
    """Four canonical frames + accumulated lenient-mode issues.

    No units handling: every SCALAR_TS row ships ``unit=None`` because
    MIMIC has no upstream xlsx units source. Locked by
    ``test_mimic.py::test_load_mimic_scalar_ts_unit_is_none_for_all_rows``.
    """

    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame
    issues: list[IngestionIssue] = field(default_factory=list)


def load_mimic(
    csv_path: Path,
    params_dir: Path,
    *,
    strict: bool = True,
) -> MimicDataset:
    """Load the MIMIC preprocessed-features CSV into the four canonical shapes.

    ``params_dir`` must contain ``reference_population_normalisation_parameters.csv``
    and ``categorical_variable_encoding.csv``. Both ``csv_path`` and
    ``params_dir`` are validated via :func:`_shared._path_traversal_guard`
    against ``EHR_SIM_DATA_ROOT`` if set.

    ``strict=True`` (default): every frame is validated with pandera's
    eager mode; first violation raises :class:`AdapterError`.

    ``strict=False``: lenient validation collects offending rows into
    ``MimicDataset.issues`` and returns the surviving rows. Imaging /
    AI_OUTPUT frames are empty either way. Every SCALAR_TS row ships
    ``unit=None`` (MIMIC has no upstream units source).
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
        params_dir / _NORM_PARAMS_FILENAME, dataset=_DATASET_NAME
    )
    sample_labels = set(frame["sample_label"].unique().tolist())
    cat_groups = _load_categorical_encoding(
        params_dir / _CATEGORICAL_ENCODING_FILENAME,
        sample_labels,
        dataset=_DATASET_NAME,
    )

    frame = frame.copy()
    frame["t_minutes"] = frame["relative_sample_date_hourly_cat"].astype(float) * 60.0
    frame = frame.rename(columns={"case_admission_id": "patient_id"})

    ehr_rows = frame[frame["source"] == "EHR"]
    registry_rows = frame[frame["source"] == _REGISTRY_SOURCE]

    scalar_ts, scalar_issues = _build_scalar_ts(
        ehr_rows, norm_params, units=None, dataset=_DATASET_NAME
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
    admission = _validate_and_collect(
        admission, CanonicalShape.ADMISSION, strict=strict, issues=issues, dataset=_DATASET_NAME
    )
    imaging = _validate_and_collect(
        imaging, CanonicalShape.IMAGING, strict=strict, issues=issues, dataset=_DATASET_NAME
    )
    ai_output = _validate_and_collect(
        ai_output, CanonicalShape.AI_OUTPUT, strict=strict, issues=issues, dataset=_DATASET_NAME
    )

    return MimicDataset(
        scalar_ts=scalar_ts,
        admission=admission,
        imaging=imaging,
        ai_output=ai_output,
        issues=issues,
    )
