"""``panel_summaries.csv`` rows of one case."""

from __future__ import annotations

from ehr_simulator.db.arm_assignments import ARM_AI
from ehr_simulator.export_phase2.cases import CaseRecord, _conflict, _keys
from ehr_simulator.export_phase2.cells import _bool, _num, _text
from ehr_simulator.export_phase2.model import PRIMARY, REVISIT
from ehr_simulator.panel_exposure import PANEL_IDS
from ehr_simulator.study_variables import AI_PANEL


def _has_ai_panel_events(case: CaseRecord, render_ids: set[str]) -> bool:
    return any(
        r.render_id in render_ids
        and r.kind.startswith("panel.")
        and r.payload.get("panel_id") == AI_PANEL
        for r in case.telemetry_rows
    )


def _panel_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    out = []
    no_ai = case.assignment.arm != ARM_AI
    kinds_order = {PRIMARY: 0, REVISIT: 1}
    keys = sorted(
        case.panels,
        key=lambda k: (k[0], kinds_order.get(k[1], len(kinds_order)), k[1], PANEL_IDS.index(k[2])),
    )
    for t_index, visit_kind, panel_id in keys:
        summary = case.panels[(t_index, visit_kind, panel_id)]
        if (
            no_ai
            and panel_id == AI_PANEL
            and not _has_ai_panel_events(case, set(summary.render_ids))
        ):
            continue  # no AI panel existed: nothing is fabricated
        out.append(
            (
                *_keys(study_id, case, t_index),
                visit_kind,
                panel_id,
                _bool(summary.mounted),
                str(summary.status),
                _num(summary.qualifying_seconds),
                _bool(summary.viewed),
                _num(summary.episode_count),
                _num(summary.panel_open_count),
                _num(summary.time_to_first_view_seconds),
                _text(summary.first_view_client_ts),
                _text(summary.last_view_client_ts),
                _bool(_conflict(case, t_index, visit_kind)),
            )
        )
    return out
