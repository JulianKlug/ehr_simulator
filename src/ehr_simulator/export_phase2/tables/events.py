"""``behavioral_events.csv`` rows: raw research events of measured cases."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ehr_simulator.db import sessions, telemetry
from ehr_simulator.db.events import PROHIBITED_PAYLOAD_KEY, EventRow
from ehr_simulator.export_phase2.cases import CaseRecord
from ehr_simulator.export_phase2.cells import _canonical_json, _minutes, _num, _text, _ts
from ehr_simulator.export_phase2.model import Phase2ExportError


def _has_prohibited_key(payload: Any) -> bool:
    if isinstance(payload, dict):
        return PROHIBITED_PAYLOAD_KEY in payload or any(
            _has_prohibited_key(v) for v in payload.values()
        )
    if isinstance(payload, list):
        return any(_has_prohibited_key(v) for v in payload)
    return False


def _event_rows(
    study_id: str,
    rows: Sequence[EventRow],
    cases: Mapping[tuple[str, str], CaseRecord],
    all_sessions: Mapping[str, sessions.SessionRow],
) -> list[tuple[str, ...]]:
    visit_kinds = {
        r.render_id: str(json.loads(r.payload_json).get("visit_kind", ""))
        for r in rows
        if r.kind == telemetry.RENDER_KIND and r.render_id is not None
    }
    out = []
    for row in rows:
        if row.patient_id is None:
            continue  # clinician level: not a case event
        case = cases.get((row.clinician_id, row.patient_id))
        if case is None:
            continue  # practice or pre Phase 2 pair: not a measured case
        where = f"event {row.event_id} of patient {row.patient_id!r}"
        if row.session_id is not None:
            session = all_sessions.get(row.session_id)
            if session is None or (session.clinician_id, session.patient_id) != case.key:
                raise Phase2ExportError(f"{where} names a session of another case")
        t_index = ""
        if row.timepoint is not None:
            timepoints = case.pinned.timepoints
            if float(row.timepoint) not in timepoints:
                raise Phase2ExportError(f"{where} is at a timepoint the pinned config lacks")
            t_index = str(timepoints.index(float(row.timepoint)))
        payload = json.loads(row.payload_json)
        if _has_prohibited_key(payload):
            raise Phase2ExportError(f"{where} carries {PROHIBITED_PAYLOAD_KEY!r}")
        out.append(
            (
                study_id,
                str(row.event_id),
                _text(row.session_id),
                row.clinician_id,
                row.patient_id,
                _minutes(row.timepoint) if row.timepoint is not None else "",
                t_index,
                visit_kinds.get(row.render_id or "", ""),
                case.assignment.config_version or "",
                case.assignment.config_hash,
                _text(row.render_id),
                _text(row.tab_id),
                row.kind,
                _ts(row.client_ts) if row.client_ts else "",
                _ts(row.server_ts),
                _num(row.client_seq),
                _num(row.client_mono_ms),
                _canonical_json(row.payload_json),
            )
        )
    return out
