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

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

import pandas as pd

from ehr_simulator.config.study import AIInterventionConfig
from ehr_simulator.ingestion.canonical import (
    AGGREGATE_BOUNDS,
    IMAGING_DERIVED_VAR_SET,
    IMAGING_FINDING_FIELDS,
    LAB_VAR_SET,
    NEURO_SUPPORT_VAR_SET,
    VITAL_VAR_SET,
)
from ehr_simulator.ingestion.provenance import AIArtifactProvenance
from ehr_simulator.web.ai_payload import check_payload, decode, exposed_ai_field_ids, plan_display
from ehr_simulator.web.clinical_fields import displayed_scalar_ids

PanelState = Literal[
    "loading", "empty-expected", "empty-unexpected", "partial", "error", "unavailable"
]
#: Every aggregate family id: medians and their bounds.
_FAMILY_MEMBERS = frozenset(AGGREGATE_BOUNDS) | {
    b for pair in AGGREGATE_BOUNDS.values() for b in pair
}

PanelName = Literal["vitals", "neuro_support", "labs", "admission", "imaging", "ai"]

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
    *,
    timepoints: Sequence[float] | None = None,
) -> PatientSlice:
    """Filter every frame to ``patient_id`` AND ``t_minutes <= t``, then derive
    panel states by inspecting the unsliced dataset (the only function authorized
    to do so).

    ADMISSION has no ``t_minutes`` column → filter only by ``patient_id``.
    Returns a frozen dataclass; renderers consume sliced frames + state labels only.

    ``timepoints`` (S8): the sequence the walk uses (study mode always passes
    it). Absent → the dataset derived fallback, which spans the whole stay.
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
        "neuro_support": None,
        "labs": None,
        "admission": None,
        "imaging": None,
        "ai": None,
    }

    for panel, variables in (
        ("vitals", VITAL_VAR_SET),
        ("neuro_support", NEURO_SUPPORT_VAR_SET),
        ("labs", LAB_VAR_SET),
    ):
        panel_states[panel] = _scalar_panel_state(
            sliced=scalar_at_or_before, full=scalar_full, variables=variables
        )
    panel_states["admission"] = "empty-expected" if admission_pid.empty else "loading"
    panel_states["imaging"] = _imaging_panel_state(
        imaging_at_or_before,
        imaging_full,
        scalar_sliced=scalar_at_or_before,
        scalar_full=scalar_full,
        admission=admission_pid,
    )
    panel_states["ai"] = _ai_panel_state(ai_at_or_before, ai_full, t=t)

    return PatientSlice(
        patient_id=pid,
        t_minutes=t,
        timepoint_index=timepoint_index,
        timepoints=(
            tuple(float(tp) for tp in timepoints)
            if timepoints is not None
            else patient_timepoints(dataset, pid)
        ),
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
) -> PanelState:
    """S8 §10.1: ``partial`` reads rows at or before ``t`` only.

    ``full`` only separates the two empty states, which render identically
    (§9.2). Sparse sampling and later first measurements are normal.
    """
    if not full.variable.isin(variables).any():
        return "empty-expected"
    rows = sliced.loc[sliced.variable.isin(variables)]
    if rows.empty:
        return "empty-unexpected"
    if _aggregate_inconsistent(rows):
        return "partial"
    return "loading"


def _aggregate_inconsistent(rows: pd.DataFrame) -> bool:
    """A family bucket has a bound without its median, or breaks
    ``min <= median <= max``. Example: ``hr_max=90`` alone at t=60 → True.

    One pivot over every family member present (this runs per request).
    """
    members = rows.loc[rows.variable.isin(_FAMILY_MEMBERS) & rows.value.notna()]
    if members.empty:
        return False
    wide = members.groupby(["t_minutes", "variable"])["value"].first().unstack()
    for median, (low, high) in AGGREGATE_BOUNDS.items():
        if low not in wide and high not in wide:
            continue
        family = wide.reindex(columns=[low, median, high])
        has_bound = family[low].notna() | family[high].notna()
        if (has_bound & family[median].isna()).any():
            return True
        if _exceeds(family[low], family[median]) or _exceeds(family[median], family[high]):
            return True
    return False


#: Inverse normalisation leaves float noise: Geneva NIHSS min 1.0000000000000009
#: with median 1.0 is one value, not an inconsistency.
_BOUND_ORDER_REL_TOL = 1e-9


def _exceeds(lower: pd.Series, upper: pd.Series) -> bool:
    """``lower > upper`` beyond float noise (NaN never exceeds)."""
    scale = upper.abs().clip(lower=1.0)
    return bool(((lower - upper) > _BOUND_ORDER_REL_TOL * scale).any())


def _imaging_panel_state(
    sliced: pd.DataFrame,
    full: pd.DataFrame,
    *,
    scalar_sliced: pd.DataFrame,
    scalar_full: pd.DataFrame,
    admission: pd.DataFrame,
) -> PanelState:
    """Canonical IMAGING rows, imaging derived scalars and the admission
    imaging findings (present from t=0) all count as imaging data (S8 §5.2)."""
    if admission["field"].isin(IMAGING_FINDING_FIELDS).any():
        return _imaging_report_state(sliced)
    if full.empty and not scalar_full.variable.isin(IMAGING_DERIVED_VAR_SET).any():
        return "empty-expected"
    if sliced.empty and not scalar_sliced.variable.isin(IMAGING_DERIVED_VAR_SET).any():
        return "empty-unexpected"
    return _imaging_report_state(sliced)


def _imaging_report_state(sliced: pd.DataFrame) -> PanelState:
    null_reports = sliced["report_text"].isna().sum() + (sliced["report_text"] == "").sum()
    if null_reports > 0:
        return "partial"
    return "loading"


def _ai_panel_state(sliced: pd.DataFrame, full: pd.DataFrame, *, t: float) -> PanelState:
    if full.empty:
        return "empty-expected"
    if sliced.empty:
        return "empty-unexpected"
    state: PanelState = "loading"
    for raw in sliced["output_json"]:
        payload = decode(raw)
        if payload is None:
            return "error"
        if check_payload(payload).state == "partial":
            state = "partial"
    return state


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
    payload = decode(measured.output_json)
    if payload is None:
        return "error", {}
    return check_payload(payload).state, payload


class AIDelivery(StrEnum):
    """S11l: what a study render's AI surface showed (``timepoint.render``)."""

    LEGACY = "legacy"  # Phase 1 / non study panels, not an S11l observation
    NONE = "none"  # measured no AI: no AI markup at all
    SHOWN = "shown"  # the pinned row for exactly t
    UNAVAILABLE = "unavailable"  # the S11g unavailable state (reason kept)
    ERROR = "error"  # the AI panel rendered in the error state


def ai_delivery(patient_slice: PatientSlice, ctx: InterventionContext) -> dict[str, str]:
    """The render payload fields recording AI delivery.

    A set ``panel_errors["ai"]`` means the AI renderer raised and the error
    panel was shown instead.
    Examples: ``{"ai": "none"}``, ``{"ai": "unavailable",
    "ai_unavailable_reason": "missing_row"}``.
    """
    if ctx.mode is InterventionMode.LEGACY:
        return {"ai": AIDelivery.LEGACY}
    if ctx.mode is InterventionMode.NO_AI:
        return {"ai": AIDelivery.NONE}
    if patient_slice.panel_errors.get("ai") is not None:
        return {"ai": AIDelivery.ERROR}

    measured = select_measured_ai(patient_slice, ctx)
    if measured.unavailable is not None:
        return {"ai": AIDelivery.UNAVAILABLE, "ai_unavailable_reason": str(measured.unavailable)}
    state, _payload = measured_ai_state(measured)
    if state == "error":
        return {"ai": AIDelivery.ERROR}
    return {"ai": AIDelivery.SHOWN}


def exposed_field_ids(
    patient_slice: PatientSlice, ai_payloads: Sequence[Mapping[str, Any]] = ()
) -> frozenset[str]:
    """Clinician facing ``<source>:<name>`` ids for one slice.

    Mirrors the renderers: every admission field; only the scalar ids a
    panel shows, aggregate bounds included (other variables are counted,
    never shown); the imaging columns the imaging panel shows; the AI
    fields the renderer shows of ``ai_payloads`` (the decoded payloads on
    screen, time order; legacy Geneva history rows show probability only).
    Example: ``{"admission:age", "scalar:hr_max", "ai:probability"}``.
    """
    ids = {f"admission:{field}" for field in patient_slice.admission["field"].astype(str)}

    shown_vars = displayed_scalar_ids()
    scalar_vars = set(patient_slice.scalar_ts["variable"].astype(str))
    ids.update(f"scalar:{var}" for var in scalar_vars & shown_vars)

    if not patient_slice.imaging.empty:
        ids.update(f"imaging:{column}" for column in _IMAGING_SHOWN_COLUMNS)

    for payload, detail in zip(ai_payloads, plan_display(ai_payloads), strict=True):
        ids.update(exposed_ai_field_ids(payload, detail))
    return frozenset(ids)
