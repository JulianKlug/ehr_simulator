"""Adapter helpers shared between Geneva (S3) and MIMIC (S4).

Every helper here was first authored at module scope inside
:mod:`ehr_simulator.ingestion.geneva` during S3 and lifted here in S4 once
MIMIC confirmed the signatures match. Helpers that previously hardcoded
``_DATASET_NAME = "geneva"`` for error-message formatting now accept
``dataset`` as a keyword-only argument so call sites are explicit at the
import-site level. Pure helpers (``_drop_imputed``, ``_inverse_normalize``,
``_one_hot_column_name``) have no dataset coupling and lift verbatim.

The :class:`CategoricalGroup` dataclass lives here, not in either adapter,
because both Geneva and MIMIC categorical encoding files share the same
shape (``sample_label, baseline_value, other_categories``) and the same
one-hot expansion convention. The naming-convention self-check in
:func:`_load_categorical_encoding` re-runs against MIMIC's 20 groups in S4.

Two-step normalize-after-validate is load-bearing:
:func:`_apply_scalar_ts_inverse_normalize` runs AFTER
:func:`_validate_and_collect` so pandera's ``coerce=True`` can drop a
malformed-string row in lenient mode before normalization eats it. This
depends on the canonical SCALAR_TS schema having no range check on
``value`` (``canonical.py`` line 88, nullable, no checks); a future schema
change adding e.g. ``value >= 0`` would break both adapters.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import structlog

from ehr_simulator.ingestion.canonical import CanonicalShape, validate
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue

_LOG = structlog.get_logger("ehr_simulator")

__all__ = [
    "CategoricalGroup",
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


@dataclass(frozen=True)
class CategoricalGroup:
    """One-hot group decoded back to a single label.

    ``one_hot_columns`` is computed via the deterministic naming convention
    ``(group_name + "_" + label).lower().replace(" ", "_")`` — verified
    against all 19 Geneva groups and all 20 MIMIC groups. Mismatches surface
    at load time (see :func:`_load_categorical_encoding`).
    """

    group_name: str
    baseline: str
    other_labels: tuple[str, ...]
    one_hot_columns: tuple[str, ...]


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


def _one_hot_column_name(group_name: str, label: str) -> str:
    return f"{group_name}_{label}".lower().replace(" ", "_")


def _path_traversal_guard(path: Path, root: Path | None, *, dataset: str) -> Path:
    resolved = path.resolve(strict=False)
    if root is None:
        return resolved
    root_resolved = root.resolve(strict=False)
    if not resolved.is_relative_to(root_resolved):
        reason = f"path traversal: {resolved} not under EHR_SIM_DATA_ROOT={root_resolved}"
        raise AdapterError(
            f"[{dataset}] {reason}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=reason,
                )
            ],
        )
    return resolved


def _load_normalisation_params(path: Path, *, dataset: str) -> dict[str, tuple[float, float]]:
    """Load (variable, mean, std) triples from a normalisation-params CSV.

    Wraps :class:`FileNotFoundError` as :class:`AdapterError` so a missing
    file surfaces a clean error with ``dataset`` flowing into the issue,
    not a raw pandas trace.
    """
    try:
        df = pd.read_csv(path)
    except FileNotFoundError as exc:
        raise AdapterError(
            f"[{dataset}] {Path(path).name} not found at {path}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=f"{Path(path).name} not found at {path}",
                )
            ],
        ) from exc
    required = {"variable", "original_mean", "original_std"}
    missing = required - set(df.columns)
    if missing:
        raise AdapterError(
            f"[{dataset}] {Path(path).name} missing columns: {sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=f"missing columns in {Path(path).name}: {sorted(missing)}",
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
    *,
    dataset: str,
) -> dict[str, CategoricalGroup]:
    """Load categorical group definitions and self-check naming convention.

    ``baseline_value`` and ``other_categories`` are Python-list literals
    (parsed via :func:`ast.literal_eval`, never :func:`eval`). The
    one-hot column names follow ``(group_name + "_" + label).lower()
    .replace(" ", "_")`` — every computed name is asserted to be present in
    ``sample_labels`` so a naming-convention drift surfaces here, not as
    silent missing-data later.

    Wraps :class:`FileNotFoundError` as :class:`AdapterError` carrying
    ``dataset`` so a missing file surfaces a clean error, not a raw pandas
    trace.
    """
    try:
        df = pd.read_csv(path)
    except FileNotFoundError as exc:
        raise AdapterError(
            f"[{dataset}] {Path(path).name} not found at {path}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=f"{Path(path).name} not found at {path}",
                )
            ],
        ) from exc
    required = {"sample_label", "baseline_value", "other_categories"}
    missing = required - set(df.columns)
    if missing:
        raise AdapterError(
            f"[{dataset}] {Path(path).name} missing columns: {sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=f"missing columns in {Path(path).name}: {sorted(missing)}",
                )
            ],
        )

    groups: dict[str, CategoricalGroup] = {}
    naming_drift: list[str] = []
    for idx, row in df.iterrows():
        group_name = str(row["sample_label"])
        raw_baseline = str(row["baseline_value"])
        try:
            if raw_baseline.startswith("["):
                parsed_baseline = ast.literal_eval(raw_baseline)
            else:
                try:
                    parsed_baseline = ast.literal_eval(raw_baseline)
                except (SyntaxError, ValueError):
                    parsed_baseline = raw_baseline
            other_list = ast.literal_eval(str(row["other_categories"]))
        except (SyntaxError, ValueError) as exc:
            raise AdapterError(
                f"[{dataset}] {Path(path).name} row {idx} ({group_name}): malformed cell — {exc}",
                issues=[
                    IngestionIssue(
                        dataset=dataset,
                        patient_id=None,
                        row_idx=int(idx),
                        reason=(
                            f"malformed categorical-encoding cell at row {idx} "
                            f"({group_name}): {exc}"
                        ),
                    )
                ],
            ) from exc

        if isinstance(parsed_baseline, list):
            baseline = str(parsed_baseline[0])
        else:
            baseline = str(parsed_baseline)
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
            f"[{dataset}] categorical naming-convention drift: "
            f"{naming_drift} not in CSV sample_label",
            issues=[
                IngestionIssue(
                    dataset=dataset,
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


def _decode_categorical(
    rows_for_group: pd.DataFrame,
    group: CategoricalGroup,
    *,
    strict: bool,
    patient_id: str,
    dataset: str,
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
                f"[{dataset}] {reason}",
                issues=[
                    IngestionIssue(
                        dataset=dataset,
                        patient_id=patient_id,
                        row_idx=None,
                        reason=reason,
                    )
                ],
            )
        return group.baseline, IngestionIssue(
            dataset=dataset,
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
            f"[{dataset}] {reason_strict}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=patient_id,
                    row_idx=None,
                    reason=reason_strict,
                )
            ],
        )

    argmax_idx = values.idxmax()
    picked_label = label_for_column[labels.loc[argmax_idx]]
    _LOG.warning(
        "categorical decode fell back to argmax",
        event_kind="ingest.categorical.argmax_fallback",
        dataset=dataset,
        patient_id=patient_id,
        group_name=group.group_name,
        winner_label=picked_label,
        candidate_count=n_above,
    )
    issue = IngestionIssue(
        dataset=dataset,
        patient_id=patient_id,
        row_idx=None,
        reason=(
            f"ambiguous categorical decode for {group.group_name}: "
            f"picked {picked_label} from {n_above} candidates"
        ),
    )
    return picked_label, issue


def _read_features_csv(
    csv_path: Path,
    *,
    required_columns: tuple[str, ...],
    dataset: str,
    known_sources: tuple[str, ...],
    chunksize: int = 500_000,
    patient_ids: tuple[str, ...] | None = None,
) -> tuple[pd.DataFrame, list[IngestionIssue]]:
    """Read a preprocessed-features CSV in chunks and apply per-chunk filters.

    Per-chunk ``_drop_imputed`` cuts most rows before concat, keeping peak
    memory well below the full row footprint. The ``value`` column is
    intentionally NOT in the dtype map so pandera's ``coerce=True`` can drop
    a malformed-value row in lenient mode rather than ``read_csv`` raising.

    When ``patient_ids`` is set, each chunk is additionally filtered to keep
    only rows whose ``case_admission_id`` is in the provided set. For pilot
    use cases (a 3-50 patient subset of a 3K-patient dataset) this collapses
    peak memory + load time by 50-1000× since most chunks drop to empty
    after imputed + patient_id filtering.

    Defensive: any chunk row whose ``source`` is not in ``known_sources``
    (after imputed-substring filtering) is dropped and surfaces an
    :class:`IngestionIssue` once per unique unrecognized value. Locks the
    silent-drop failure mode for both adapters so a future Geneva CSV
    growing a ``notes`` row (or any new vocab drift) is visible, not lost.
    """
    patient_filter = set(patient_ids) if patient_ids is not None else None
    header = pd.read_csv(csv_path, nrows=0)
    missing = set(required_columns) - set(header.columns)
    if missing:
        raise AdapterError(
            f"[{dataset}] CSV missing required columns: {sorted(missing)}",
            issues=[
                IngestionIssue(
                    dataset=dataset,
                    patient_id=None,
                    row_idx=None,
                    reason=f"missing required columns: {sorted(missing)}",
                )
            ],
        )

    known = set(known_sources)
    issues: list[IngestionIssue] = []
    seen_unknown: set[str] = set()

    chunks: list[pd.DataFrame] = []
    reader = pd.read_csv(
        csv_path,
        usecols=list(required_columns),
        dtype={
            "case_admission_id": str,
            "sample_label": str,
            "source": str,
        },
        chunksize=chunksize,
    )
    for chunk in reader:
        chunk = _drop_imputed(chunk)
        if chunk.empty:
            chunks.append(chunk)
            continue
        if patient_filter is not None:
            mask = chunk["case_admission_id"].astype(str).isin(patient_filter)
            chunk = chunk[mask].reset_index(drop=True)
            if chunk.empty:
                chunks.append(chunk)
                continue
        sources = chunk["source"].astype(str)
        unknown_in_chunk = set(sources.unique()) - known
        for src in unknown_in_chunk:
            if src not in seen_unknown:
                seen_unknown.add(src)
                _LOG.warning(
                    "unrecognized source value",
                    event_kind="ingest.source.unrecognized",
                    dataset=dataset,
                    source_value=src,
                )
                issues.append(
                    IngestionIssue(
                        dataset=dataset,
                        patient_id=None,
                        row_idx=None,
                        reason=f"unrecognized source value: {src}",
                    )
                )
        if unknown_in_chunk:
            chunk = chunk[sources.isin(known)].reset_index(drop=True)
        chunks.append(chunk)

    if not chunks:
        return pd.DataFrame(columns=list(required_columns)), issues
    return pd.concat(chunks, ignore_index=True), issues


def _build_scalar_ts(
    ehr_rows: pd.DataFrame,
    norm_params: dict[str, tuple[float, float]],
    *,
    units: dict[str, str] | None = None,
    dataset: str,
) -> tuple[pd.DataFrame, list[IngestionIssue]]:
    """Build a SCALAR_TS frame with the raw (z-scored) value column.

    Inverse-normalization is deferred until after pandera validation: the
    ``value`` column is left untouched so a malformed string can be caught
    by pandera's ``coerce=True`` (strict raises, lenient drops the row).
    Apply :func:`_apply_scalar_ts_inverse_normalize` to the validated frame.

    ``units=None`` means "every output row gets ``unit=None``" (MIMIC's
    case — no upstream units source). ``units={...}`` means "look up unit
    per variable, fall back to ``None`` when missing" (Geneva's case).
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
                dataset=dataset,
                patient_id=None,
                row_idx=None,
                reason=f"variable {var} missing from normalisation_parameters",
            )
        )

    if units is None:
        unit_col: list[str | None] = [None] * len(ehr_rows)
    else:
        unit_col = [units.get(v) for v in variables]

    out = pd.DataFrame(
        {
            "patient_id": ehr_rows["patient_id"].astype(str).to_numpy(),
            "t_minutes": ehr_rows["t_minutes"].astype(float).to_numpy(),
            "variable": variables,
            "value": ehr_rows["value"].to_numpy(),
            "unit": unit_col,
            "source": ["EHR"] * len(ehr_rows),
        }
    )
    return out, issues


def _apply_scalar_ts_inverse_normalize(
    frame: pd.DataFrame,
    norm_params: dict[str, tuple[float, float]],
) -> pd.DataFrame:
    """Per-variable mask + multiply, applied AFTER pandera validates.

    Depends on the canonical SCALAR_TS schema having no range check on
    ``value`` (``canonical.py`` line 88 — nullable, no checks); a future
    schema change adding e.g. ``value >= 0`` would break both adapters.
    """
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
    dataset: str,
) -> tuple[pd.DataFrame, list[IngestionIssue]]:
    """Build the ADMISSION frame from the ``t=0`` slice of the registry rows.

    ``notes`` rows (MIMIC) and ``stroke_registry`` rows (Geneva) both
    repeat across all hour buckets; the canonical anchor for both is
    ``t_minutes == 0.0``. Categorical groups are decoded via
    :func:`_decode_categorical`; continuous registry vars are
    inverse-normalized and ``str(round(raw, 2))``-coerced. Orphans
    (neither categorical nor in ``norm_params``) emit issues, not silent
    drops.
    """
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
    seen_flat_binary: set[str] = set()
    for patient_id, patient_rows in t0_rows.groupby("patient_id", sort=True):
        for group in cat_groups.values():
            mask = patient_rows["sample_label"].isin(group.one_hot_columns)
            sub = patient_rows[mask]
            decoded, maybe_issue = _decode_categorical(
                sub, group, strict=strict, patient_id=str(patient_id), dataset=dataset
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
                # Tier 3: flat binary registry variable. Geneva ships some
                # registry features (e.g. ``vascular_occlusion``) as plain
                # 0/1 flags rather than as one-hot expansions of a declared
                # categorical group. Under ``strict=False``, decode 0/1 to
                # "False"/"True" (matching the existing categorical decode
                # convention) and emit a once-per-variable WARNING so the
                # gap is visible. Under strict, fall through to the orphan
                # issue branch unchanged.
                raw_value = float(row["value"])
                if not strict and raw_value in (0.0, 1.0):
                    decoded = "True" if raw_value == 1.0 else "False"
                    if label not in seen_flat_binary:
                        seen_flat_binary.add(label)
                        _LOG.warning(
                            "flat binary registry variable decoded as True/False",
                            event_kind="ingest.registry.flat_binary",
                            dataset=dataset,
                            sample_label=label,
                        )
                    out_rows.append(
                        {
                            "patient_id": str(patient_id),
                            "field": label,
                            "value": decoded,
                        }
                    )
                    continue
                issues.append(
                    IngestionIssue(
                        dataset=dataset,
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
    dataset: str,
) -> pd.DataFrame:
    validated = validate(frame, shape, strict=strict, dataset=dataset)
    if not strict:
        adapter_error = validated.attrs.get("adapter_error")
        if adapter_error is not None:
            issues.extend(adapter_error.issues)
    return validated
