"""Pure derivation tests for S10 wall-clock timing (specs/session10.md §4).

Exercises the exact rule set the export relies on:

1. earliest enter is the start;
2. first exit after the first enter is the end;
3. enter with no exit → open interval (start only);
4. no enter → all fields blank;
5. an exit before every enter is ignored;
6. end before start → TimingError;
7. later repeats never replace the first completed interval;
8. elapsed_seconds is integer wall-clock seconds, not dwell time;
9. derivation is scoped to the requested clinician/patient/timepoint;
10. ordering is by server_ts then event id.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from ehr_simulator import timing
from ehr_simulator.db import clinicians

CID = "alice"
PID = "synth_001"
T = 0.0

BASE = datetime(2026, 9, 18, 14, 0, 0)
MIN = timedelta(minutes=1)
SEC = lambda n: timedelta(seconds=n)  # noqa: E731


def ev(n: int, kind: str, ts: datetime, *, cid=CID, pid=PID, tp=T) -> timing.TimingEvent:
    return timing.TimingEvent(
        event_id=n,
        clinician_id=cid,
        patient_id=pid,
        timepoint=tp,
        kind=kind,
        server_ts=ts,
    )


def derive(*events: timing.TimingEvent) -> dict[float, timing.TimepointTiming]:
    return timing.derive_timepoint_timings(list(events), clinician_id=CID, patient_id=PID)


def test_completed_interval_derives_start_end_and_elapsed() -> None:
    out = derive(ev(1, "timepoint.enter", BASE), ev(2, "timepoint.exit", BASE + SEC(37)))
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.ended_at == BASE + SEC(37)
    assert tt.elapsed_seconds == 37


def test_zero_second_interval_is_fine() -> None:
    out = derive(ev(1, "timepoint.enter", BASE), ev(2, "timepoint.exit", BASE))
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.ended_at == BASE
    assert tt.elapsed_seconds == 0


def test_earliest_enter_is_the_start_even_if_listed_later() -> None:
    # Stream order is (server_ts, event_id), not call order: the enter at
    # BASE is the start even though it is passed second here.
    out = derive(
        ev(2, "timepoint.enter", BASE + MIN),
        ev(1, "timepoint.enter", BASE),
        ev(3, "timepoint.exit", BASE + MIN + SEC(5)),
    )
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.elapsed_seconds == int(MIN.total_seconds()) + 5


def test_first_exit_after_first_enter_is_the_end() -> None:
    out = derive(
        ev(1, "timepoint.enter", BASE),
        ev(2, "timepoint.exit", BASE + MIN),
        ev(3, "timepoint.exit", BASE + 5 * MIN),
    )
    assert out[T].ended_at == BASE + MIN
    assert out[T].elapsed_seconds == 60


def test_enter_without_exit_is_an_open_interval() -> None:
    out = derive(ev(1, "timepoint.enter", BASE))
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.ended_at is None
    assert tt.elapsed_seconds is None


def test_no_enter_yields_blank_timing_fields() -> None:
    out = derive(ev(1, "timepoint.exit", BASE))
    tt = out[T]
    assert tt.started_at is None
    assert tt.ended_at is None
    assert tt.elapsed_seconds is None


def test_exit_before_every_enter_is_ignored() -> None:
    # The early exit must not become the "end"; the next exit after the
    # first enter is paired instead.
    out = derive(
        ev(1, "timepoint.exit", BASE),
        ev(2, "timepoint.enter", BASE + MIN),
        ev(3, "timepoint.exit", BASE + 2 * MIN),
    )
    tt = out[T]
    assert tt.started_at == BASE + MIN
    assert tt.ended_at == BASE + 2 * MIN
    assert tt.elapsed_seconds == 60


def test_later_repeats_do_not_replace_the_first_interval() -> None:
    out = derive(
        ev(1, "timepoint.enter", BASE),
        ev(2, "timepoint.exit", BASE + 3 * MIN),
        ev(3, "timepoint.enter", BASE + MIN),
        ev(4, "timepoint.exit", BASE + 4 * MIN),
    )
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.ended_at == BASE + 3 * MIN
    assert tt.elapsed_seconds == 180


def test_end_before_start_raises_timing_error() -> None:
    # Defensive path (spec §4 rule 9): stream ordering normally prevents
    # this, but hand-assembled histories must not corrupt the derivation.
    with pytest.raises(timing.TimingError):
        timing._pair(
            [ev(1, "timepoint.enter", BASE), ev(2, "timepoint.exit", BASE - MIN)],
            clinician_id=CID,
            patient_id=PID,
            timepoint=T,
        )


def test_scoped_to_clinician_patient_and_timepoint() -> None:
    out = derive(
        ev(1, "timepoint.enter", BASE, cid="bob"),  # other clinician
        ev(2, "timepoint.enter", BASE, pid="synth_002"),  # other patient
        ev(3, "timepoint.enter", BASE + SEC(10), tp=60.0),  # other timepoint
        ev(4, "timepoint.enter", BASE + SEC(20)),
        ev(5, "timepoint.exit", BASE + SEC(40)),
    )
    assert set(out) == {T, 60.0}
    assert out[T].started_at == BASE + SEC(20)
    assert out[T].elapsed_seconds == 20
    assert out[60.0].ended_at is None


def test_other_event_kinds_are_never_paired() -> None:
    out = derive(
        ev(1, "advance.ok", BASE - MIN),
        ev(2, "timepoint.enter", BASE),
        ev(3, "advance.blocked", BASE + MIN),
        ev(4, "timepoint.exit", BASE + SEC(11)),
    )
    tt = out[T]
    assert tt.started_at == BASE
    assert tt.elapsed_seconds == 11


def test_format_ts_none_and_value() -> None:
    assert timing.format_ts(None) == ""
    assert timing.format_ts(BASE) == "2026-09-18 14:00:00"


def test_fetch_timing_events_reads_only_timing_rows(db: sqlite3.Connection) -> None:
    """The read helper keeps the two new kinds only, in server order."""
    from ehr_simulator.db import events

    cid = clinicians.lookup_or_create(db, "Alice")
    events.append(
        db,
        session_id=None,
        clinician_id=cid,
        patient_id=PID,
        timepoint=T,
        kind="timepoint.enter",
        payload={"t_index": 0},
    )
    events.append(
        db,
        session_id=None,
        clinician_id=cid,
        patient_id=PID,
        timepoint=60.0,
        kind="timepoint.exit",
        payload={"t_index": 1, "reason": "advance"},
    )
    events.append(
        db,
        session_id=None,
        clinician_id=cid,
        patient_id=PID,
        timepoint=T,
        kind="advance.ok",
    )
    got = timing.fetch_timing_events(db)
    assert [e.kind for e in got] == ["timepoint.enter", "timepoint.exit"]
    assert [e.patient_id for e in got] == [PID, PID]
    assert [e.timepoint for e in got] == [T, 60.0]
    assert all(isinstance(e.server_ts, datetime) for e in got)
    # Deterministic ordering: (server_ts, event_id).
    assert [e.event_id for e in got] == sorted(e.event_id for e in got)
