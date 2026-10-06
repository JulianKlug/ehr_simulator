"""``timepoints.csv`` rows of one case."""

from __future__ import annotations

from ehr_simulator import timing
from ehr_simulator.export_phase2.cases import CaseRecord, _keys
from ehr_simulator.export_phase2.cells import _bool, _joined, _num, _text, _ts
from ehr_simulator.export_phase2.model import NOT_CONFIGURED, PRIMARY


def _timepoint_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    a = case.assignment
    life = case.lifecycle
    telemetry_on = case.pinned.study.telemetry is not None
    out = []
    for obs in case.variables.observations:
        t = obs.t_index
        s10 = case.s10.get(case.pinned.timepoints[t])
        primary = case.timings.get((t, PRIMARY))
        status = NOT_CONFIGURED
        if telemetry_on:
            status = str(primary.status) if primary is not None else "missing"
        out.append(
            (
                *_keys(study_id, case, t),
                a.arm,
                a.arm_source,
                _text(a.schedule_id),
                _num(a.case_position),
                _ts(a.activated_at),
                _text(life.state if life else None),
                _ts(life.completed_at if life else None),
                _ts(life.incomplete_at if life else None),
                _text(life.incomplete_reason if life else None),
                _bool(obs.reached),
                timing.format_ts(s10.started_at) if s10 else "",
                timing.format_ts(s10.ended_at) if s10 else "",
                _num(s10.elapsed_seconds) if s10 else "",
                status,
                _num(primary.foreground_seconds) if primary else "",
                _num(primary.active_seconds) if primary else "",
                _bool(obs.tab_conflict_detected),
                _bool(obs.ai_delivered),
                _bool(obs.ai_viewed),
                _text(obs.ai_viewing_status),
                _num(obs.ai_exposure_seconds),
                _num(obs.ai_episode_count),
                _bool(obs.intervention_failure),
                _joined(obs.intervention_failure_reasons),
                _bool(obs.intervention_leakage),
                _bool(obs.pp_compliant),
                _bool(obs.pp_determinate),
                _joined(obs.integrity_warnings),
            )
        )
    return out
