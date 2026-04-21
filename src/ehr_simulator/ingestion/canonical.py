"""Canonical in-memory data shapes for the EHR simulator.

This module is the single source of truth for how ingestion adapters must
present their data. Four pandera schemas define the accepted shapes:

``SCALAR_TS``
    Time-varying numerics (vitals, labs, perfusion-imaging scalars).
    Columns: ``patient_id, t_minutes, variable, value, unit, source``.
    No uniqueness constraint — duplicate ``t_minutes`` for the same
    ``(patient_id, variable)`` are allowed (e.g., two BP readings seconds
    apart). The ``source`` column accepts arbitrary non-empty strings;
    source-system semantics (e.g., filtering imputed rows via substring
    match on ``"imputed"``) are an adapter responsibility, not a
    schema-level constraint.

``ADMISSION``
    Static patient facts. Columns: ``patient_id, field, value``. Unique
    on ``(patient_id, field)``. Numeric admission values are str-coerced
    at the adapter boundary so the canonical shape stays stringly-typed.

``IMAGING``
    Per-timepoint imaging references. Columns: ``patient_id, t_minutes,
    modality, report_text, image_refs``. Unique on
    ``(patient_id, t_minutes, modality)``. ``image_refs`` is a JSON
    string (list of relative paths) when present.

``AI_OUTPUT``
    Precomputed per-timepoint model output (consumed, not produced).
    Columns: ``patient_id, t_minutes, model_id, output_json``. Unique on
    ``(patient_id, t_minutes, model_id)``. ``output_json`` is a
    non-null valid JSON string.

The public entry point is :func:`validate`.
"""

from __future__ import annotations

import json
from enum import StrEnum

import pandas as pd
import pandera.pandas as pa

from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue


class CanonicalShape(StrEnum):
    SCALAR_TS = "scalar_ts"
    ADMISSION = "admission"
    IMAGING = "imaging"
    AI_OUTPUT = "ai_output"


def _is_valid_json(val: object) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return False
    if not isinstance(val, str):
        return False
    try:
        json.loads(val)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _is_valid_json_list_or_null(val: object) -> bool:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return True
    if not isinstance(val, str):
        return False
    try:
        parsed = json.loads(val)
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, list) and all(isinstance(x, str) for x in parsed)


_non_empty_str = pa.Check.str_length(min_value=1)
_non_negative = pa.Check.ge(0)


SCALAR_TS_SCHEMA: pa.DataFrameSchema = pa.DataFrameSchema(
    columns={
        "patient_id": pa.Column(str, checks=_non_empty_str, nullable=False),
        "t_minutes": pa.Column(float, checks=_non_negative, nullable=False),
        "variable": pa.Column(str, checks=_non_empty_str, nullable=False),
        "value": pa.Column(float, nullable=True),
        "unit": pa.Column(str, nullable=True),
        "source": pa.Column(str, checks=_non_empty_str, nullable=False),
    },
    strict=True,
    coerce=True,
)


ADMISSION_SCHEMA: pa.DataFrameSchema = pa.DataFrameSchema(
    columns={
        "patient_id": pa.Column(str, checks=_non_empty_str, nullable=False),
        "field": pa.Column(str, checks=_non_empty_str, nullable=False),
        "value": pa.Column(str, nullable=True),
    },
    strict=True,
    coerce=True,
    unique=["patient_id", "field"],
)


IMAGING_SCHEMA: pa.DataFrameSchema = pa.DataFrameSchema(
    columns={
        "patient_id": pa.Column(str, checks=_non_empty_str, nullable=False),
        "t_minutes": pa.Column(float, checks=_non_negative, nullable=False),
        "modality": pa.Column(str, checks=_non_empty_str, nullable=False),
        "report_text": pa.Column(str, nullable=True),
        "image_refs": pa.Column(
            str,
            nullable=True,
            checks=pa.Check(
                lambda s: s.apply(_is_valid_json_list_or_null),
                element_wise=False,
                error="image_refs must be a JSON list of strings when present",
            ),
        ),
    },
    strict=True,
    coerce=True,
    unique=["patient_id", "t_minutes", "modality"],
)


AI_OUTPUT_SCHEMA: pa.DataFrameSchema = pa.DataFrameSchema(
    columns={
        "patient_id": pa.Column(str, checks=_non_empty_str, nullable=False),
        "t_minutes": pa.Column(float, checks=_non_negative, nullable=False),
        "model_id": pa.Column(str, checks=_non_empty_str, nullable=False),
        "output_json": pa.Column(
            str,
            nullable=False,
            checks=pa.Check(
                lambda s: s.apply(_is_valid_json),
                element_wise=False,
                error="output_json must be a valid JSON string",
            ),
        ),
    },
    strict=True,
    coerce=True,
    unique=["patient_id", "t_minutes", "model_id"],
)


SCHEMAS: dict[CanonicalShape, pa.DataFrameSchema] = {
    CanonicalShape.SCALAR_TS: SCALAR_TS_SCHEMA,
    CanonicalShape.ADMISSION: ADMISSION_SCHEMA,
    CanonicalShape.IMAGING: IMAGING_SCHEMA,
    CanonicalShape.AI_OUTPUT: AI_OUTPUT_SCHEMA,
}


def validate(
    frame: pd.DataFrame,
    shape: CanonicalShape,
    *,
    strict: bool = True,
    dataset: str = "unknown",
) -> pd.DataFrame:
    """Validate ``frame`` against the canonical ``shape`` schema.

    In ``strict=True`` mode, pandera's eager validation runs and the first
    violation is wrapped in :class:`AdapterError`.

    In ``strict=False`` mode, pandera's lazy validation collects every
    failure; offending rows are dropped and the cleaned frame is returned.
    The accumulated :class:`AdapterError` (with :attr:`AdapterError.issues`
    populated) is attached to ``frame.attrs["adapter_error"]`` so the
    caller can surface or escalate issues without losing the good rows.
    """
    schema = SCHEMAS[shape]

    if strict:
        try:
            return schema.validate(frame, lazy=False)
        except pa.errors.SchemaError as exc:
            raise AdapterError(
                f"[{dataset}/{shape.value}] schema validation failed: {exc}",
                issues=[
                    IngestionIssue(
                        dataset=dataset,
                        patient_id=_extract_patient_id(frame, exc),
                        row_idx=_extract_row_idx(exc),
                        reason=str(exc),
                    )
                ],
            ) from exc
        except pa.errors.SchemaErrors as exc:
            raise AdapterError(
                f"[{dataset}/{shape.value}] schema validation failed: {exc}",
                issues=_issues_from_failure_cases(frame, exc, dataset),
            ) from exc

    try:
        validated = schema.validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        failing_idx = _failing_indices(exc)
        cleaned = frame.drop(index=failing_idx).reset_index(drop=True)
        issues = _issues_from_failure_cases(frame, exc, dataset)
        cleaned.attrs["adapter_error"] = AdapterError(
            f"[{dataset}/{shape.value}] {len(issues)} row(s) dropped during lenient validation",
            issues=issues,
        )
        return cleaned

    validated.attrs["adapter_error"] = None
    return validated


def _failing_indices(exc: pa.errors.SchemaErrors) -> list[int]:
    fc = exc.failure_cases
    if "index" not in fc.columns:
        return []
    return sorted({int(i) for i in fc["index"].dropna().tolist()})


def _issues_from_failure_cases(
    frame: pd.DataFrame,
    exc: pa.errors.SchemaErrors,
    dataset: str,
) -> list[IngestionIssue]:
    fc = exc.failure_cases
    issues: list[IngestionIssue] = []
    for _, row in fc.iterrows():
        raw_idx = row.get("index")
        idx = int(raw_idx) if pd.notna(raw_idx) else None
        pid: str | None = None
        if idx is not None and "patient_id" in frame.columns and idx in frame.index:
            candidate = frame.at[idx, "patient_id"]
            if isinstance(candidate, str):
                pid = candidate
        reason = (
            f"column={row.get('column')} check={row.get('check')} value={row.get('failure_case')!r}"
        )
        issues.append(IngestionIssue(dataset=dataset, patient_id=pid, row_idx=idx, reason=reason))
    return issues


def _extract_row_idx(exc: pa.errors.SchemaError) -> int | None:
    fc = getattr(exc, "failure_cases", None)
    if fc is None or "index" not in getattr(fc, "columns", []):
        return None
    values = fc["index"].dropna().tolist()
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _extract_patient_id(frame: pd.DataFrame, exc: pa.errors.SchemaError) -> str | None:
    idx = _extract_row_idx(exc)
    if idx is None or "patient_id" not in frame.columns or idx not in frame.index:
        return None
    candidate = frame.at[idx, "patient_id"]
    return candidate if isinstance(candidate, str) else None
