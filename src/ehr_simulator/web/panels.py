"""Panel slicing + state detection — the data-locality choke point.

:func:`slice_to_timepoint` is the **only** function in the codebase that reads
the unsliced dataset. It computes per-panel state (which requires peeking at
``t > t_minutes`` for ``empty-unexpected`` detection) and returns it alongside
the sliced frames. Renderers receive the slice plus the per-panel state label
only — they have no path to future data.

The data-locality invariant becomes a structural property of the codebase,
not a discipline. (Decision **D5**.)

S11g intervention selection runs *after* the slice, on the already time
bounded frames::

    slice_to_timepoint ─▶ PatientSlice ─▶ select_measured_ai(slice, ctx)
                                              │  row at exactly t, model_id
                                              ▼  from the pinned config
                                          MeasuredAI(row | unavailable reason)

:func:`exposed_field_ids` is the clinician facing field inventory preflight
checks against the study's prohibited fields; it mirrors what the panel
renderers display.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

from ehr_simulator.config.study import AIInterventionConfig
from ehr_simulator.ingestion.canonical import LAB_VAR_SET as _LAB_VARS
from ehr_simulator.ingestion.canonical import VITAL_VAR_SET as _VITAL_VARS
from ehr_simulator.ingestion.provenance import AIArtifactProvenance

PanelState = Literal[
    "loading", "empty-expected", "empty-unexpected", "partial", "error", "unavailable"
]
PanelName = Literal["vitals", "labs", "admission", "imaging", "ai"]

#: S11g: imaging columns the imaging panel shows.
_IMAGING_SHOWN_COLUMNS = ("modality", "report_text")


@runtime_checkable
class DatasetLike(Protocol):
    """Structural type for any adapter dataset.

    Per /plan-eng-review tension B: ``slice_to_timepoint`` and
    ``patient_timepoints`` were originally typed against ``SyntheticDataset``
    only, which prevented ``walk_preflight`` from compiling against
    Geneva/MIMIC. The three adapter dataclasses (``SyntheticDataset``,
    ``GenevaDataset``, ``MimicDataset``) all expose the four canonical-frame
    attrs and so satisfy this Protocol structurally.
    """

    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame


_AI_REQUIRED_KEYS = frozenset({"prob_deterioration_6h", "prob_mrs_0_2_90d"})


@dataclass(frozen=True)
class PatientSlice:
    """Per-patient view of the dataset filtered to ``t_minutes <= t``.

    ``panel_states`` is computed inside :func:`slice_to_timepoint` (the only
    function authorized to inspect the unsliced dataset). Renderers consume
    the sliced frames and the state labels — they have no path to future
    data.
    """

    patient_id: str
    t_minutes: float
    timepoint_index: int
    timepoints: tuple[float, ...]
    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame
    panel_states: dict[str, PanelState]
    panel_errors: dict[str, str | None]


def patient_timepoints(dataset: DatasetLike, patient_id: str) -> tuple[float, ...]:
    """Sorted distinct ``t_minutes`` for ``patient_id`` across all time-varying shapes.

    The ordinal URL index (``t_index``) maps into this tuple. Patients with
    no scalar_ts/imaging/ai_output rows still get the dataset-wide timepoints
    (so the URL surface is consistent). Empty fallback yields ``(0.0,)``.
    """
    candidates: set[float] = set()
    for frame in (dataset.scalar_ts, dataset.imaging, dataset.ai_output):
        if "patient_id" not in frame.columns:
            continue
        rows = frame.loc[frame.patient_id == patient_id]
        if not rows.empty:
            candidates.update(float(t) for t in rows["t_minutes"].tolist())
    if candidates:
        return tuple(sorted(candidates))
    fallback: set[float] = set()
    for frame in (dataset.scalar_ts, dataset.imaging, dataset.ai_output):
        if "t_minutes" in frame.columns:
            fallback.update(float(t) for t in frame["t_minutes"].tolist())
    return tuple(sorted(fallback)) or (0.0,)


def slice_to_timepoint(
    dataset: DatasetLike,
    patient_id: str,
    t_minutes: float,
    timepoint_index: int,
) -> PatientSlice:
    """Filter every frame to ``patient_id`` AND ``t_minutes <= t``, then derive
    panel states by inspecting the unsliced dataset (the only function authorized
    to do so).

    ADMISSION has no ``t_minutes`` column → filter only by ``patient_id``.
    Returns a frozen dataclass; renderers consume sliced frames + state labels only.
    """
    pid = patient_id
    t = float(t_minutes)

    scalar_full = dataset.scalar_ts.loc[dataset.scalar_ts.patient_id == pid]
    imaging_full = dataset.imaging.loc[dataset.imaging.patient_id == pid]
    ai_full = dataset.ai_output.loc[dataset.ai_output.patient_id == pid]
    admission_pid = dataset.admission.loc[dataset.admission.patient_id == pid].reset_index(
        drop=True
    )

    scalar_at_or_before = scalar_full.loc[scalar_full.t_minutes <= t].reset_index(drop=True)
    imaging_at_or_before = imaging_full.loc[imaging_full.t_minutes <= t].reset_index(drop=True)
    ai_at_or_before = ai_full.loc[ai_full.t_minutes <= t].reset_index(drop=True)

    panel_states: dict[str, PanelState] = {}
    panel_errors: dict[str, str | None] = {
        "vitals": None,
        "labs": None,
        "admission": None,
        "imaging": None,
        "ai": None,
    }

    panel_states["vitals"] = _scalar_panel_state(
        sliced=scalar_at_or_before,
        full=scalar_full,
        variables=_VITAL_VARS,
        t=t,
    )
    panel_states["labs"] = _scalar_panel_state(
        sliced=scalar_at_or_before,
        full=scalar_full,
        variables=_LAB_VARS,
        t=t,
    )
    panel_states["admission"] = "empty-expected" if admission_pid.empty else "loading"
    panel_states["imaging"] = _imaging_panel_state(imaging_at_or_before, imaging_full, t=t)
    panel_states["ai"] = _ai_panel_state(ai_at_or_before, ai_full, t=t)

    return PatientSlice(
        patient_id=pid,
        t_minutes=t,
        timepoint_index=timepoint_index,
        timepoints=patient_timepoints(dataset, pid),
        scalar_ts=scalar_at_or_before,
        admission=admission_pid,
        imaging=imaging_at_or_before,
        ai_output=ai_at_or_before,
        panel_states=panel_states,
        panel_errors=panel_errors,
    )


def _scalar_panel_state(
    *,
    sliced: pd.DataFrame,
    full: pd.DataFrame,
    variables: frozenset[str],
    t: float,
) -> PanelState:
    sliced_vars = set(sliced.loc[sliced.variable.isin(variables), "variable"].unique())
    full_vars = set(full.loc[full.variable.isin(variables), "variable"].unique())

    if not full_vars:
        return "empty-expected"
    if not sliced_vars:
        return "empty-unexpected"

    vars_at_current_t = set(
        sliced.loc[
            (sliced.variable.isin(variables)) & (sliced.t_minutes == t),
            "variable",
        ].unique()
    )
    if vars_at_current_t != sliced_vars:
        return "partial"
    if full_vars - sliced_vars:
        return "partial"
    return "loading"


def _imaging_panel_state(sliced: pd.DataFrame, full: pd.DataFrame, *, t: float) -> PanelState:
    if full.empty:
        return "empty-expected"
    if sliced.empty:
        return "empty-unexpected"
    null_reports = sliced["report_text"].isna().sum() + (sliced["report_text"] == "").sum()
    if null_reports > 0:
        return "partial"
    return "loading"


def _ai_panel_state(sliced: pd.DataFrame, full: pd.DataFrame, *, t: float) -> PanelState:
    if full.empty:
        return "empty-expected"
    if sliced.empty:
        return "empty-unexpected"
    for raw in sliced["output_json"]:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return "error"
        if not isinstance(payload, dict):
            return "error"
        if not _AI_REQUIRED_KEYS.issubset(payload.keys()):
            return "partial"
    return "loading"


# ---------------------------------------------------------------------------
# S11g: measured intervention selection
# ---------------------------------------------------------------------------


class InterventionMode(StrEnum):
    LEGACY = "legacy"  # Phase 1 / non study: every panel, cumulative AI slice
    AI = "ai"  # measured AI case: the frozen row for exactly t
    NO_AI = "no_ai"  # measured no AI case: no AI surface at all


class AIUnavailableReason(StrEnum):
    NOT_CONFIGURED = "not_configured"  # pinned snapshot predates ai_intervention
    ARTIFACT_MISMATCH = "artifact_mismatch"  # pinned artifact is not the loaded one
    MISSING_ROW = "missing_row"  # no row for (patient, t, model_id)


@dataclass(frozen=True)
class InterventionContext:
    mode: InterventionMode
    intervention: AIInterventionConfig | None = None  # from the case pinned snapshot
    loaded: AIArtifactProvenance | None = None  # from the dataset


LEGACY_INTERVENTION = InterventionContext(InterventionMode.LEGACY)


@dataclass(frozen=True)
class MeasuredAI:
    """The AI row a measured AI case shows, or why none can be shown."""

    t_minutes: float | None = None
    model_id: str | None = None
    output_json: str | None = None
    unavailable: AIUnavailableReason | None = None


def provenance_mismatches(
    configured: AIInterventionConfig, loaded: AIArtifactProvenance | None
) -> list[str]:
    """Human-readable differences between the frozen and the loaded artifact.

    Empty means they match. Shared by the boot gate, preflight and rendering.
    """
    if loaded is None:
        return ["the dataset exposes no AI artifact provenance"]

    pairs = (
        ("prediction SHA256", configured.prediction_artifact_sha256, loaded.prediction_sha256),
        ("explanation SHA256", configured.explanation_artifact_sha256, loaded.explanation_sha256),
        ("model/system version", configured.model_system_version, loaded.model_system_version),
    )
    return [
        f"{label}: configured {want!r}, loaded {got!r}" for label, want, got in pairs if want != got
    ]


def select_measured_ai(patient_slice: PatientSlice, ctx: InterventionContext) -> MeasuredAI:
    """Pick the pinned model's row at exactly the current timepoint.

    Never falls back to an earlier or later row: a gap is ``MISSING_ROW``.
    """
    configured = ctx.intervention
    if configured is None:
        return MeasuredAI(unavailable=AIUnavailableReason.NOT_CONFIGURED)
    if provenance_mismatches(configured, ctx.loaded):
        return MeasuredAI(unavailable=AIUnavailableReason.ARTIFACT_MISMATCH)

    frame = patient_slice.ai_output
    rows = frame.loc[
        (frame.t_minutes == patient_slice.t_minutes) & (frame.model_id == configured.model_id)
    ]
    if rows.empty:
        return MeasuredAI(unavailable=AIUnavailableReason.MISSING_ROW)

    row = rows.iloc[0]
    return MeasuredAI(
        t_minutes=float(row.t_minutes), model_id=str(row.model_id), output_json=row.output_json
    )


def measured_ai_state(measured: MeasuredAI) -> tuple[PanelState, dict[str, Any]]:
    """Panel state + decoded payload of a measured AI row."""
    if measured.unavailable is not None:
        return "unavailable", {}
    try:
        payload = json.loads(measured.output_json or "")
    except (TypeError, ValueError):
        return "error", {}
    if not isinstance(payload, dict):
        return "error", {}
    if not _AI_REQUIRED_KEYS.issubset(payload.keys()):
        return "partial", payload
    return "loading", payload


def exposed_field_ids(
    patient_slice: PatientSlice, ai_payload_keys: Iterable[str] = ()
) -> frozenset[str]:
    """Clinician facing ``<source>:<name>`` ids for one slice.

    Mirrors the renderers: every admission field; only vital/lab scalar
    variables (other variables are counted, never shown); the imaging
    columns the imaging panel shows; the keys of the AI payload on screen.
    Example: ``{"admission:age", "scalar:hr", "ai:prob_deterioration_6h"}``.
    """
    ids = {f"admission:{field}" for field in patient_slice.admission["field"].astype(str)}

    shown_vars = _VITAL_VARS | _LAB_VARS
    scalar_vars = set(patient_slice.scalar_ts["variable"].astype(str))
    ids.update(f"scalar:{var}" for var in scalar_vars & shown_vars)

    if not patient_slice.imaging.empty:
        ids.update(f"imaging:{column}" for column in _IMAGING_SHOWN_COLUMNS)

    ids.update(f"ai:{key}" for key in ai_payload_keys)
    return frozenset(ids)
