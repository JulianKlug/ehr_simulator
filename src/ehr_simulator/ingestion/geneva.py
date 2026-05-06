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
"""

from __future__ import annotations

import ast
import functools
import importlib.resources
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame, validate
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue

_DATASET_NAME = "geneva"
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)
_NON_IMPUTED_SOURCES: tuple[str, ...] = ("EHR", "stroke_registry")
_CHUNKSIZE = 500_000


@dataclass(frozen=True)
class CategoricalGroup:
    """One-hot group decoded back to a single label.

    ``one_hot_columns`` is computed via the deterministic naming convention
    ``(group_name + "_" + label).lower().replace(" ", "_")`` — verified
    against all 19 groups in the Geneva ``categorical_variable_encoding.csv``
    on 2026-05-06. Mismatches surface at load time (see
    :func:`_load_categorical_encoding`).
    """

    group_name: str
    baseline: str
    other_labels: tuple[str, ...]
    one_hot_columns: tuple[str, ...]


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
    csv_path = _path_traversal_guard(csv_path, root)
    params_dir = _path_traversal_guard(params_dir, root)

    issues: list[IngestionIssue] = []

    frame = _read_geneva_csv(csv_path)
    norm_params = _load_normalisation_params(params_dir / "normalisation_parameters.csv")
    sample_labels = set(frame["sample_label"].unique().tolist())
    cat_groups = _load_categorical_encoding(
        params_dir / "categorical_variable_encoding.csv",
        sample_labels,
    )
    units = _load_units()

    frame = frame.copy()
    frame["t_minutes"] = frame["relative_sample_date_hourly_cat"].astype(float) * 60.0
    frame = frame.rename(columns={"case_admission_id": "patient_id"})

    ehr_rows = frame[frame["source"] == "EHR"]
    registry_rows = frame[frame["source"] == "stroke_registry"]

    scalar_ts, scalar_issues = _build_scalar_ts(ehr_rows, norm_params, units)
    issues.extend(scalar_issues)

    admission, admission_issues = _build_admission(
        registry_rows, norm_params, cat_groups, strict=strict
    )
    issues.extend(admission_issues)

    imaging = empty_frame(CanonicalShape.IMAGING)
    ai_output = empty_frame(CanonicalShape.AI_OUTPUT)

    scalar_ts = _validate_and_collect(
        scalar_ts, CanonicalShape.SCALAR_TS, strict=strict, issues=issues
    )
    scalar_ts = _apply_scalar_ts_inverse_normalize(scalar_ts, norm_params)
    admission = _validate_and_collect(
        admission, CanonicalShape.ADMISSION, strict=strict, issues=issues
    )
    imaging = _validate_and_collect(imaging, CanonicalShape.IMAGING, strict=strict, issues=issues)
    ai_output = _validate_and_collect(
        ai_output, CanonicalShape.AI_OUTPUT, strict=strict, issues=issues
    )

    return GenevaDataset(
        scalar_ts=scalar_ts,
        admission=admission,
        imaging=imaging,
        ai_output=ai_output,
        issues=issues,
    )


def _read_geneva_csv(csv_path: Path) -> pd.DataFrame:
    """Read the Geneva CSV in chunks and apply ``_drop_imputed`` per chunk.

    Per-chunk filtering cuts ~75% of rows before concat, which keeps peak
    memory well below the full ~20M-row footprint. The ``value`` column is
    intentionally NOT in the dtype dict so pandera's ``coerce=True`` can
    drop a malformed-value row in lenient mode instead of ``read_csv``
    raising.
    """
    header = pd.read_csv(csv_path, nrows=0)
    missing = set(_REQUIRED_COLUMNS) - set(header.columns)
    if missing:
        raise AdapterError(
            f"[{_DATASET_NAME}] CSV missing required columns: {sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=f"missing required columns: {sorted(missing)}",
                )
            ],
        )

    chunks: list[pd.DataFrame] = []
    reader = pd.read_csv(
        csv_path,
        usecols=list(_REQUIRED_COLUMNS),
        dtype={
            "case_admission_id": str,
            "sample_label": str,
            "source": str,
        },
        chunksize=_CHUNKSIZE,
    )
    for chunk in reader:
        chunks.append(_drop_imputed(chunk))

    if not chunks:
        return pd.DataFrame(columns=list(_REQUIRED_COLUMNS))
    return pd.concat(chunks, ignore_index=True)


def _drop_imputed(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.reset_index(drop=True)
    keep = ~frame["source"].astype(str).str.contains("imputed", na=False)
    return frame[keep].reset_index(drop=True)


def _inverse_normalize(
    z: float | pd.Series,
    mean: float,
    std: float,
) -> float | pd.Series:
    return z * std + mean


def _decode_categorical(
    rows_for_group: pd.DataFrame,
    group: CategoricalGroup,
    *,
    strict: bool,
    patient_id: str,
) -> tuple[str, IngestionIssue | None]:
    """Decode one-hot rows for one group at one patient back to a single label.

    Tiered behavior:

    * empty ``rows_for_group``: strict raises; lenient returns
      ``(group.baseline, IngestionIssue(...))``.
    * all values ``< 0.5``: returns ``(group.baseline, None)``.
    * exactly one value ``>= 0.5``: returns ``(matching_label, None)``.
    * multiple values ``>= 0.5``: strict raises; lenient picks ``argmax``
      and appends an issue.
    """
    if rows_for_group.empty:
        reason = f"empty rows for categorical group {group.group_name}"
        if strict:
            raise AdapterError(
                f"[{_DATASET_NAME}] {reason}",
                issues=[
                    IngestionIssue(
                        dataset=_DATASET_NAME,
                        patient_id=patient_id,
                        row_idx=None,
                        reason=reason,
                    )
                ],
            )
        return group.baseline, IngestionIssue(
            dataset=_DATASET_NAME,
            patient_id=patient_id,
            row_idx=None,
            reason=reason,
        )

    label_for_column = dict(zip(group.one_hot_columns, group.other_labels, strict=True))
    values = rows_for_group["value"].astype(float)
    labels = rows_for_group["sample_label"].astype(str)
    above = values >= 0.5
    n_above = int(above.sum())

    if n_above == 0:
        return group.baseline, None
    if n_above == 1:
        idx = values[above].index[0]
        return label_for_column[labels.loc[idx]], None

    reason_strict = (
        f"ambiguous categorical decode for {group.group_name}: {n_above} candidates >=0.5"
    )
    if strict:
        raise AdapterError(
            f"[{_DATASET_NAME}] {reason_strict}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=patient_id,
                    row_idx=None,
                    reason=reason_strict,
                )
            ],
        )

    argmax_idx = values.idxmax()
    picked_label = label_for_column[labels.loc[argmax_idx]]
    issue = IngestionIssue(
        dataset=_DATASET_NAME,
        patient_id=patient_id,
        row_idx=None,
        reason=(
            f"ambiguous categorical decode for {group.group_name}: "
            f"picked {picked_label} from {n_above} candidates"
        ),
    )
    return picked_label, issue


def _path_traversal_guard(path: Path, root: Path | None) -> Path:
    resolved = path.resolve(strict=False)
    if root is None:
        return resolved
    root_resolved = root.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        reason = f"path traversal: {resolved} not under EHR_SIM_DATA_ROOT={root_resolved}"
        raise AdapterError(
            f"[{_DATASET_NAME}] {reason}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=reason,
                )
            ],
        )
    return resolved


def _load_normalisation_params(path: Path) -> dict[str, tuple[float, float]]:
    df = pd.read_csv(path)
    required = {"variable", "original_mean", "original_std"}
    missing = required - set(df.columns)
    if missing:
        raise AdapterError(
            f"[{_DATASET_NAME}] normalisation_parameters.csv missing columns: {sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=f"missing columns in normalisation_parameters.csv: {sorted(missing)}",
                )
            ],
        )
    return {
        str(row.variable): (float(row.original_mean), float(row.original_std))
        for row in df.itertuples(index=False)
    }


def _load_categorical_encoding(
    path: Path,
    sample_labels: set[str],
) -> dict[str, CategoricalGroup]:
    """Load categorical group definitions and self-check naming convention.

    Each row in the CSV defines one group. ``baseline_value`` and
    ``other_categories`` are Python-list literals (parsed via
    ``ast.literal_eval``, never ``eval``). The Geneva CSV's one-hot column
    names follow the deterministic convention
    ``(group_name + "_" + label).lower().replace(" ", "_")``. Every
    computed name is asserted to be present in ``sample_labels`` so a
    naming-convention drift surfaces here, not as silent missing-data later.
    """
    df = pd.read_csv(path)
    required = {"sample_label", "baseline_value", "other_categories"}
    missing = required - set(df.columns)
    if missing:
        raise AdapterError(
            f"[{_DATASET_NAME}] categorical_variable_encoding.csv missing columns: "
            f"{sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=(
                        f"missing columns in categorical_variable_encoding.csv: {sorted(missing)}"
                    ),
                )
            ],
        )

    groups: dict[str, CategoricalGroup] = {}
    naming_drift: list[str] = []
    for idx, row in df.iterrows():
        group_name = str(row["sample_label"])
        try:
            baseline_list = ast.literal_eval(str(row["baseline_value"]))
            other_list = ast.literal_eval(str(row["other_categories"]))
        except (SyntaxError, ValueError) as exc:
            raise AdapterError(
                f"[{_DATASET_NAME}] categorical_variable_encoding.csv row {idx} "
                f"({group_name}): malformed cell — {exc}",
                issues=[
                    IngestionIssue(
                        dataset=_DATASET_NAME,
                        patient_id=None,
                        row_idx=int(idx),
                        reason=(
                            f"malformed categorical-encoding cell at row {idx} "
                            f"({group_name}): {exc}"
                        ),
                    )
                ],
            ) from exc

        baseline = str(baseline_list[0])
        other_labels = tuple(str(x) for x in other_list)
        one_hot_columns = tuple(_one_hot_column_name(group_name, label) for label in other_labels)
        for col in one_hot_columns:
            if col not in sample_labels:
                naming_drift.append(col)
        groups[group_name] = CategoricalGroup(
            group_name=group_name,
            baseline=baseline,
            other_labels=other_labels,
            one_hot_columns=one_hot_columns,
        )

    if naming_drift:
        raise AdapterError(
            f"[{_DATASET_NAME}] categorical naming-convention drift: "
            f"{naming_drift} not in CSV sample_label",
            issues=[
                IngestionIssue(
                    dataset=_DATASET_NAME,
                    patient_id=None,
                    row_idx=None,
                    reason=(
                        f"categorical naming-convention drift: {naming_drift} "
                        "not in CSV sample_label"
                    ),
                )
            ],
        )

    return groups


def _one_hot_column_name(group_name: str, label: str) -> str:
    return f"{group_name}_{label}".lower().replace(" ", "_")


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


def _build_scalar_ts(
    ehr_rows: pd.DataFrame,
    norm_params: dict[str, tuple[float, float]],
    units: dict[str, str],
) -> tuple[pd.DataFrame, list[IngestionIssue]]:
    """Build a SCALAR_TS frame with the raw (z-scored) value column.

    Inverse-normalization is deferred until after pandera validation: the
    ``value`` column is left untouched so a malformed string can be caught
    by pandera's ``coerce=True`` (strict raises, lenient drops the row).
    Apply :func:`_apply_scalar_ts_inverse_normalize` to the validated frame.
    """
    issues: list[IngestionIssue] = []
    if ehr_rows.empty:
        return (
            pd.DataFrame(
                columns=["patient_id", "t_minutes", "variable", "value", "unit", "source"]
            ),
            issues,
        )

    variables = ehr_rows["sample_label"].astype(str).to_numpy()
    missing_vars = sorted({v for v in variables if v not in norm_params})
    for var in missing_vars:
        issues.append(
            IngestionIssue(
                dataset=_DATASET_NAME,
                patient_id=None,
                row_idx=None,
                reason=f"variable {var} missing from normalisation_parameters",
            )
        )

    out = pd.DataFrame(
        {
            "patient_id": ehr_rows["patient_id"].astype(str).to_numpy(),
            "t_minutes": ehr_rows["t_minutes"].astype(float).to_numpy(),
            "variable": variables,
            "value": ehr_rows["value"].to_numpy(),
            "unit": [units.get(v) for v in variables],
            "source": ["EHR"] * len(ehr_rows),
        }
    )
    return out, issues


def _apply_scalar_ts_inverse_normalize(
    frame: pd.DataFrame,
    norm_params: dict[str, tuple[float, float]],
) -> pd.DataFrame:
    if frame.empty:
        return frame
    out = frame.copy()
    for variable, (mean, std) in norm_params.items():
        mask = out["variable"] == variable
        if mask.any():
            out.loc[mask, "value"] = out.loc[mask, "value"].astype(float) * std + mean
    return out


def _build_admission(
    registry_rows: pd.DataFrame,
    norm_params: dict[str, tuple[float, float]],
    cat_groups: dict[str, CategoricalGroup],
    *,
    strict: bool,
) -> tuple[pd.DataFrame, list[IngestionIssue]]:
    issues: list[IngestionIssue] = []
    if registry_rows.empty:
        return pd.DataFrame(columns=["patient_id", "field", "value"]), issues

    t0_rows = registry_rows[registry_rows["t_minutes"] == 0.0].copy()
    if t0_rows.empty:
        return pd.DataFrame(columns=["patient_id", "field", "value"]), issues

    one_hot_lookup: dict[str, CategoricalGroup] = {}
    for group in cat_groups.values():
        for col in group.one_hot_columns:
            one_hot_lookup[col] = group

    out_rows: list[dict[str, str]] = []
    for patient_id, patient_rows in t0_rows.groupby("patient_id", sort=True):
        for group in cat_groups.values():
            mask = patient_rows["sample_label"].isin(group.one_hot_columns)
            sub = patient_rows[mask]
            decoded, maybe_issue = _decode_categorical(
                sub, group, strict=strict, patient_id=str(patient_id)
            )
            if maybe_issue is not None:
                issues.append(maybe_issue)
            out_rows.append(
                {
                    "patient_id": str(patient_id),
                    "field": group.group_name,
                    "value": decoded,
                }
            )

        for _, row in patient_rows.iterrows():
            label = str(row["sample_label"])
            if label in one_hot_lookup:
                continue
            params = norm_params.get(label)
            if params is None:
                issues.append(
                    IngestionIssue(
                        dataset=_DATASET_NAME,
                        patient_id=str(patient_id),
                        row_idx=None,
                        reason=f"orphan registry variable: {label}",
                    )
                )
                continue
            mean, std = params
            raw = _inverse_normalize(float(row["value"]), mean, std)
            out_rows.append(
                {
                    "patient_id": str(patient_id),
                    "field": label,
                    "value": str(round(raw, 2)),
                }
            )

    return pd.DataFrame(out_rows, columns=["patient_id", "field", "value"]), issues


def _validate_and_collect(
    frame: pd.DataFrame,
    shape: CanonicalShape,
    *,
    strict: bool,
    issues: list[IngestionIssue],
) -> pd.DataFrame:
    validated = validate(frame, shape, strict=strict, dataset=_DATASET_NAME)
    if not strict:
        adapter_error = validated.attrs.get("adapter_error")
        if adapter_error is not None:
            issues.extend(adapter_error.issues)
    return validated
