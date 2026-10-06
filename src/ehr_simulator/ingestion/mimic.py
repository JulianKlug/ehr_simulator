"""MIMIC-III preprocessed-features adapter.

Loads the MIMIC stroke-cohort CSV at the path configured in
``.EXAMPLE_DATA_PATHS`` into the four canonical in-memory shapes from
:mod:`ehr_simulator.ingestion.canonical`. MIMIC mirrors Geneva's
preprocessed layout one-for-one with three deliberate differences:

==============================  ==========================================
Difference                      Handling
==============================  ==========================================
no unnamed-index column         none needed: both adapters read only
                                ``_REQUIRED_COLUMNS`` (``usecols``)
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
                                (a variable missing from the params: strict
                                raises, lenient drops its rows + issue);
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
    _FeatureLayout,
    _inverse_normalize,
    _load_categorical_encoding,
    _load_feature_frames,
    _load_normalisation_params,
    _one_hot_column_name,
    _path_traversal_guard,
    _read_features_csv,
    _validate_and_collect,
)
from ehr_simulator.ingestion.exceptions import IngestionIssue
from ehr_simulator.ingestion.provenance import AIArtifactProvenance

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
_LAYOUT = _FeatureLayout(
    dataset=_DATASET_NAME,
    norm_params_filename=_NORM_PARAMS_FILENAME,
    categorical_encoding_filename=_CATEGORICAL_ENCODING_FILENAME,
    required_columns=_REQUIRED_COLUMNS,
    known_sources=_NON_IMPUTED_SOURCES,
    registry_source=_REGISTRY_SOURCE,
)


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
    # S11g: no AI artifact is loaded for this dataset (S7 adds one).
    ai_provenance: AIArtifactProvenance | None = None


def load_mimic(
    csv_path: Path,
    params_dir: Path,
    *,
    strict: bool = True,
    patient_ids: tuple[str, ...] | None = None,
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
    frames = _load_feature_frames(
        csv_path, params_dir, _LAYOUT, units=None, strict=strict, patient_ids=patient_ids
    )

    return MimicDataset(
        scalar_ts=frames.scalar_ts,
        admission=frames.admission,
        imaging=frames.imaging,
        ai_output=frames.ai_output,
        issues=frames.issues,
    )
