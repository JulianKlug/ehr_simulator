"""``configuration_history.csv`` and ``configuration_counts.csv``."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.config_history import ConfigHistoryRow
from ehr_simulator.db.randomisation import StoredSchedule
from ehr_simulator.export_phase2.cases import CaseRecord
from ehr_simulator.export_phase2.cells import _text, _ts
from ehr_simulator.export_phase2.headers import ANSWERS_HEADER
from ehr_simulator.study_variables import ResponseStatus


def _history_order(rows: Sequence[ConfigHistoryRow]) -> list[ConfigHistoryRow]:
    return sorted(rows, key=lambda r: (_ts(r.activated_at), r.config_version))


def _history_rows(study_id: str, rows: Sequence[ConfigHistoryRow]) -> list[tuple[str, ...]]:
    return [
        (
            study_id,
            r.config_version,
            r.config_hash,
            _ts(r.activated_at),
            r.change_description,
            _text(r.change_reason),
            r.study_json,
            r.questions_json,
        )
        for r in _history_order(rows)
    ]


def _count_rows(
    study_id: str,
    history: Sequence[ConfigHistoryRow],
    schedules: Sequence[StoredSchedule],
    cases: Sequence[CaseRecord],
    answer_rows: Mapping[tuple[str, str], list[tuple[str, ...]]],
) -> list[tuple[str, ...]]:
    status_col = ANSWERS_HEADER.index("response_status")
    out = []
    for row in _history_order(history):
        version = row.config_version
        mine = [c for c in cases if c.assignment.config_version == version]
        states = [c.lifecycle.state if c.lifecycle else None for c in mine]
        out.append(
            (
                study_id,
                version,
                row.config_hash,
                str(
                    sum(
                        len(s.schedule.items)
                        for s in schedules
                        if s.schedule.config_version == version
                    )
                ),
                str(len(mine)),
                str(sum(1 for s in states if s is CaseState.COMPLETED)),
                str(sum(1 for s in states if s is CaseState.INCOMPLETE)),
                str(sum(1 for s in states if s in (CaseState.ACTIVE, CaseState.PAUSED))),
                str(sum(len(c.pinned.timepoints) for c in mine)),
                str(sum(1 for c in mine for o in c.variables.observations if o.reached)),
                str(
                    sum(
                        1
                        for c in mine
                        for r in answer_rows[c.key]
                        if r[status_col] == ResponseStatus.ANSWERED
                    )
                ),
            )
        )
    return out
