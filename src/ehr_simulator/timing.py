"""Behavioral timing derived from ``timepoint.enter``/``timepoint.exit`` events (S10).

Spec: ``specs/session10.md`` §2/§4.

This module is **pure**: no ``sqlite3`` import, no ``web/`` import, no I/O
except the small :func:`fetch_timing_events` read helper. Timing is
deterministic data-derivation, not measurement — the authoritative clock is
the server (``server_ts``), and the same event history always derives the
same timings.

Public API::

    @dataclass(frozen=True)
    class TimepointTiming:
        clinician_id: str
        patient_id: str
        timepoint: float
        started_at: datetime | None
        ended_at: datetime | None
        elapsed_seconds: int | None


    class TimingError(ValueError): ...

    def derive_timepoint_timings(
        events,
        *,
        clinician_id: str,
        patient_id: str,
    ) -> dict[float, TimepointTiming]: ...

Derivation rules (spec §4):

1. group by clinician, patient, and timepoint;
2. sort by ``server_ts``, then deterministic event id/order;
3. ``started_at`` is the earliest ``timepoint.enter``;
4. ``ended_at`` is the earliest ``timepoint.exit`` at or after that enter;
5. later enters/exits do not replace the first completed interval;
6. if no enter exists, all timing fields are blank;
7. if enter exists but no exit after it, start is set and end/elapsed blank;
8. an exit preceding every enter is ignored;
9. if the selected end timestamp is earlier than the start, raise
   :class:`TimingError` (defensive — real emissions happen in order);
10. never infer missing timestamps from answer ``ts_recorded``, progress,
    or session timestamps.

``elapsed_seconds`` is wall-clock integer seconds (ended − started). It is
**not** active dwell time; callers must not label it that.

Legacy data without ``timepoint.enter`` derives blank timing fields and
remains exportable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

__all__ = [
    "ENTER_KIND",
    "EXIT_KIND",
    "TS_FORMAT",
    "TimingEvent",
    "TimingError",
    "TimepointTiming",
    "derive_timepoint_timings",
    "fetch_timing_events",
    "format_ts",
]

ENTER_KIND = "timepoint.enter"
EXIT_KIND = "timepoint.exit"

#: ``YYYY-MM-DD HH:MM:SS`` — the timestamp serialization locked in spec §4.
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


class TimingError(ValueError):
    """The event history cannot be derived into the locked timing fields."""


@dataclass(frozen=True, slots=True)
class TimingEvent:
    """One ``timepoint.enter``/``timepoint.exit`` row, in server order.

    ``server_ts`` is a naive UTC ``datetime`` (SQLite
    ``CURRENT_TIMESTAMP``), the authoritative timestamp.
    """

    event_id: int
    clinician_id: str
    patient_id: str | None
    timepoint: float | None
    kind: str
    server_ts: datetime


@dataclass(frozen=True, slots=True)
class TimepointTiming:
    clinician_id: str
    patient_id: str
    timepoint: float
    started_at: datetime | None
    ended_at: datetime | None
    elapsed_seconds: int | None


def _pair(
    ordered: Sequence[TimingEvent],
    *,
    clinician_id: str,
    patient_id: str,
    timepoint: float,
) -> TimepointTiming:
    """Derive one timepoint's interval from its stream-ordered events.

    Rule set 3–9 from the module docstring; exported so the ``TimingError``
    path is testable directly.
    """
    entries = [e for e in ordered if e.kind == ENTER_KIND]
    if not entries:
        # Rule 6/8: an exit before any enter (or with no enter at all) is
        # ignored — the whole interval is blank.
        return TimepointTiming(
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoint=timepoint,
            started_at=None,
            ended_at=None,
            elapsed_seconds=None,
        )

    start = entries[0].server_ts
    start_pos = ordered.index(entries[0])
    # Rule 4: first exit at or after the first enter in stream order.
    exit_event = next(
        (e for e in ordered[start_pos + 1 :] if e.kind == EXIT_KIND),
        None,
    )
    if exit_event is None:
        # Rule 7: started, but the interval is still open.
        return TimepointTiming(
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoint=timepoint,
            started_at=start,
            ended_at=None,
            elapsed_seconds=None,
        )

    end = exit_event.server_ts
    if end < start:
        # Rule 9: defensive — the selected end must not precede the start.
        raise TimingError(
            f"patient {patient_id!r}, clinician {clinician_id}, timepoint {timepoint}: "
            f"selected exit {end.isoformat()} precedes start {start.isoformat()}"
        )
    return TimepointTiming(
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=timepoint,
        started_at=start,
        ended_at=end,
        elapsed_seconds=int((end - start).total_seconds()),
    )


def derive_timepoint_timings(
    events: Sequence[TimingEvent],
    *,
    clinician_id: str,
    patient_id: str,
) -> dict[float, TimepointTiming]:
    """Derive per-timepoint wall-clock intervals for one clinician-patient.

    Returns a dict keyed by the timepoint (minutes) that has at least one
    timing event; timepoints with no events have no entry (callers render
    those as blank cells). See the module docstring for the locked rules.
    """
    mine: list[TimingEvent] = [
        e
        for e in events
        if e.kind in (ENTER_KIND, EXIT_KIND)
        and e.clinician_id == clinician_id
        and e.patient_id == patient_id
        and e.timepoint is not None
    ]
    by_tp: dict[float, list[TimingEvent]] = {}
    for e in mine:
        by_tp.setdefault(float(e.timepoint), []).append(e)

    out: dict[float, TimepointTiming] = {}
    for tp, group in by_tp.items():
        ordered = sorted(group, key=lambda e: (e.server_ts, e.event_id))
        out[tp] = _pair(ordered, clinician_id=clinician_id, patient_id=patient_id, timepoint=tp)
    return out


def format_ts(value: datetime | None) -> str:
    """Serialize a timestamp as ``YYYY-MM-DD HH:MM:SS``; ``None`` → ``""``."""
    if value is None:
        return ""
    return value.strftime(TS_FORMAT)


if TYPE_CHECKING:
    import sqlite3


def fetch_timing_events(conn: sqlite3.Connection) -> tuple[TimingEvent, ...]:
    """Read the enter/exit event history for one patient-free derivation pass.

    Runs one indexed ``SELECT``; callers that need a torn-state guarantee
    (the S9c export) must run this inside their existing explicit read
    transaction.
    """
    rows = conn.execute(
        """
        SELECT event_id, clinician_id, patient_id, timepoint, kind, server_ts
        FROM events
        WHERE kind IN (?, ?)
          AND patient_id IS NOT NULL
          AND timepoint IS NOT NULL
        ORDER BY server_ts, event_id
        """,
        (ENTER_KIND, EXIT_KIND),
    ).fetchall()
    return tuple(
        TimingEvent(
            event_id=event_id,
            clinician_id=clinician_id,
            patient_id=patient_id,
            timepoint=timepoint,
            kind=kind,
            server_ts=server_ts,
        )
        for event_id, clinician_id, patient_id, timepoint, kind, server_ts in rows
    )
