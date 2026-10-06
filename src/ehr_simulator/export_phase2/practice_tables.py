"""``practice_timepoints.csv`` and ``practice_answers.csv`` (opt in)."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence

from ehr_simulator import timing
from ehr_simulator.behavioral_timing import ObservationTiming, derive_observation_timings
from ehr_simulator.db import answers, telemetry
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.db.practice import PracticeCase
from ehr_simulator.export_phase2.cases import _answer_rows_by_cell
from ehr_simulator.export_phase2.cells import _minutes, _num, _ts
from ehr_simulator.export_phase2.model import NOT_CONFIGURED, PRIMARY, Phase2ExportError
from ehr_simulator.export_phase2.registry import PinnedConfig, _pinned
from ehr_simulator.export_phase2.tables.answers import _answer_cells


def _practice_tables(
    conn: sqlite3.Connection,
    study_id: str,
    cases: Sequence[PracticeCase],
    registry: Mapping[str, PinnedConfig],
    timing_events: Sequence[timing.TimingEvent],
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    timepoint_rows: list[tuple[str, ...]] = []
    answer_rows: list[tuple[str, ...]] = []
    mode = str(ObservationMode.PRACTICE)
    for case in cases:
        where = f"practice case of patient {case.patient_id!r}, clinician {case.clinician_id}"
        pinned = _pinned(registry, case.config_version, case.config_hash, where)
        rows = [
            r
            for r in answers.fetch_for_pair(conn, case.clinician_id, case.patient_id)
            if r.observation_mode == ObservationMode.PRACTICE
        ]
        for r in rows:
            if (r.config_version, r.config_hash) != (case.config_version, case.config_hash):
                raise Phase2ExportError(f"answer of {where} disagrees with its configuration")
        by_cell = _answer_rows_by_cell(rows, pinned, where)
        recorded = answers.fetch_recorded_at(conn, case.clinician_id, case.patient_id)
        try:
            s10 = timing.derive_timepoint_timings(
                timing_events, clinician_id=case.clinician_id, patient_id=case.patient_id
            )
        except timing.TimingError as exc:
            raise Phase2ExportError(f"cannot derive timing of {where}: {exc}") from exc

        timings: dict[tuple[int, str], ObservationTiming] = {}
        if pinned.study.telemetry is not None:
            timings = derive_observation_timings(
                telemetry.load_render_rows(conn, case.clinician_id, case.patient_id),
                telemetry.load_telemetry_rows(conn, case.clinician_id, case.patient_id),
                inactivity_threshold_seconds=pinned.study.telemetry.inactivity_threshold_seconds,
            )

        for t_index, minutes in enumerate(pinned.timepoints):
            keys = (
                study_id,
                case.clinician_id,
                case.patient_id,
                str(t_index),
                _minutes(minutes),
                case.config_version,
                case.config_hash,
            )
            tt = s10.get(minutes)
            primary = timings.get((t_index, PRIMARY))
            status = NOT_CONFIGURED
            if pinned.study.telemetry is not None:
                status = str(primary.status) if primary is not None else "missing"
            timepoint_rows.append(
                (
                    *keys,
                    mode,
                    case.arm,
                    _ts(case.started_at),
                    _ts(case.completed_at),
                    timing.format_ts(tt.started_at) if tt else "",
                    timing.format_ts(tt.ended_at) if tt else "",
                    _num(tt.elapsed_seconds) if tt else "",
                    status,
                    _num(primary.foreground_seconds) if primary else "",
                    _num(primary.active_seconds) if primary else "",
                )
            )
            cells = _answer_cells(
                study_id,
                keys,
                pinned.questions,
                pinned.study,
                t_index,
                by_cell,
                recorded,
                None,
            )
            answer_rows.extend((*cell, mode) for cell in cells)
    return timepoint_rows, answer_rows
