"""Panel slicing + state detection — the data-locality choke point.

:func:`slice_to_timepoint` is the **only** function in the codebase that reads
the unsliced dataset. It computes per-panel state (which requires peeking at
``t > t_minutes`` for ``empty-unexpected`` detection) and returns it alongside
the sliced frames. Renderers receive the slice plus the per-panel state label
only — they have no path to future data.

The data-locality invariant becomes a structural property of the codebase,
not a discipline. (Decision **D5**.)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

import pandas as pd

PanelState = Literal["loading", "empty-expected", "empty-unexpected", "partial", "error"]
PanelName = Literal["vitals", "labs", "admission", "imaging", "ai"]


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


_VITAL_VARS = frozenset({"hr", "sbp", "dbp", "rr", "spo2", "temp"})
_LAB_VARS = frozenset({"hgb", "na", "cr", "glucose", "wbc", "plt"})
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
