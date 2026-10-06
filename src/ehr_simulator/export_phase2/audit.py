"""``randomisation_audit.csv``: every schedule item, planned vs realised."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ehr_simulator.db.arm_assignments import ActivatedAssignment
from ehr_simulator.db.case_lifecycle import CaseLifecycle
from ehr_simulator.db.randomisation import ScheduleItem, StoredSchedule
from ehr_simulator.db.replacements import ReplacementPlan
from ehr_simulator.export_phase2.cells import _bool, _canonical_json, _num, _text, _ts
from ehr_simulator.export_phase2.model import Phase2ExportError
from ehr_simulator.export_phase2.registry import PinnedConfig, _pinned


def _audit_rows(
    study_id: str,
    schedules: Sequence[StoredSchedule],
    registry: Mapping[str, PinnedConfig],
    assignments: Sequence[ActivatedAssignment],
    lifecycles: Mapping[tuple[str, str], CaseLifecycle],
    plans: Sequence[ReplacementPlan],
) -> list[tuple[str, ...]]:
    items: dict[tuple[str, int], tuple[StoredSchedule, ScheduleItem]] = {}
    for stored in schedules:
        s = stored.schedule
        _pinned(registry, s.config_version, s.config_hash, f"schedule {s.schedule_id[:8]}…")
        for item in s.items:
            items[(s.schedule_id, item.case_position)] = (stored, item)

    realised: dict[tuple[str, int], ActivatedAssignment] = {}
    for a in assignments:
        where = f"assignment of patient {a.patient_id!r}, clinician {a.clinician_id}"
        if a.schedule_id is None or a.case_position is None:
            raise Phase2ExportError(f"{where} names no schedule item")
        found = items.get((a.schedule_id, a.case_position))
        if found is None:
            raise Phase2ExportError(f"{where} names a missing schedule item")
        stored, item = found
        if stored.schedule.clinician_id != a.clinician_id or item.patient_id != a.patient_id:
            raise Phase2ExportError(f"{where} disagrees with its schedule item")
        if item.planned_arm != a.arm:
            raise Phase2ExportError(f"{where} realised an arm other than the planned one")
        realised[(a.schedule_id, a.case_position)] = a

    held = {(a.clinician_id, a.patient_id) for a in assignments}
    for key in lifecycles:
        if key not in held:
            raise Phase2ExportError(f"lifecycle row of patient {key[1]!r} has no assignment")

    replacing: dict[tuple[str, int], ReplacementPlan] = {}
    replaced: dict[tuple[str, str], ReplacementPlan] = {}
    for plan in plans:
        where = f"replacement {plan.replacement_id[:8]}…"
        if (plan.clinician_id, plan.original_patient_id) not in held:
            raise Phase2ExportError(f"{where} replaces a case that was never activated")
        position = (plan.replacement_schedule_id, plan.replacement_case_position)
        if position not in items:
            raise Phase2ExportError(f"{where} points to a missing schedule item")
        stored, item = items[position]
        if (
            stored.schedule.clinician_id != plan.clinician_id
            or item.patient_id != plan.replacement_patient_id
            or item.planned_arm != plan.planned_arm
        ):
            raise Phase2ExportError(f"{where} disagrees with the schedule item it names")
        if plan.activated_at is not None and position not in realised:
            raise Phase2ExportError(f"{where} is activated but its case is missing")
        replacing[position] = plan
        replaced[(plan.clinician_id, plan.original_patient_id)] = plan

    out = []
    ordered = sorted(items.items(), key=lambda kv: (kv[1][0].schedule.clinician_id, kv[0]))
    for position, (stored, item) in ordered:
        s = stored.schedule
        a = realised.get(position)
        life = lifecycles.get((s.clinician_id, item.patient_id)) if a is not None else None
        into = replacing.get(position)
        out_of = replaced.get((s.clinician_id, item.patient_id)) if a is not None else None
        out.append(
            (
                study_id,
                s.clinician_id,
                s.schedule_id,
                s.config_version,
                s.config_hash,
                _ts(stored.generated_at),
                s.algorithm_version,
                str(s.master_seed),
                s.derived_seed_hex,
                _canonical_json(s.allocation_state_json),
                str(s.starting_ai_count),
                str(s.starting_no_ai_count),
                s.starting_arm,
                str(s.block_length),
                _canonical_json(s.block_sequence_json),
                str(item.case_position),
                item.patient_id,
                item.planned_arm,
                str(item.block_number),
                str(item.position_in_block),
                _text(item.preceding_block_arm),
                _num(item.planned_cases_since_ai),
                str(item.assignment_seed),
                _bool(a is not None),
                _ts(a.activated_at) if a else "",
                (a.config_version or "") if a else "",
                a.config_hash if a else "",
                a.arm if a else "",
                _text(life.state if life else None),
                _ts(life.completed_at if life else None),
                _ts(life.incomplete_at if life else None),
                _text(life.incomplete_reason if life else None),
                into.replacement_id if into else "",
                into.original_patient_id if into else "",
                _ts(into.generated_at) if into else "",
                _ts(into.activated_at) if into else "",
                out_of.replacement_id if out_of else "",
                out_of.replacement_patient_id if out_of else "",
            )
        )
    return out
