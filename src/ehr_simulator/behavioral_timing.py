"""S11j foreground and active time from raw browser telemetry (pure).

Spec: ``specs/session-11j-browser-telemetry.md``.

Input: the ``timepoint.render`` rows of one clinician × patient and the
browser rows bound to them (``db/telemetry.py``). Durations use only
``client_mono_ms``, compared only within one render (one document, one
``performance.now()`` origin); ``server_ts`` never enters a duration::

    render interval   [enter ─────────────────────────────── exit]
    foreground              ████████████      ██████████████
    activity (+T)       a━━━━━━━━━━━━━━┫    b━━━━━━━┫
    active                  ███████████       ███████

* **foreground** = visible AND focused, clipped to ``[enter, exit]``;
* **active** = foreground ∩ ⋃ ``[a, a + threshold]`` over activities ``a``
  (the enter included) that happened while foreground. A later activity
  resumes active time from itself; gaps are never filled backwards.

A render without an exit ends at its last event and is ``incomplete`` (its
seconds are a lower bound); a reported loss or a lost enter is ``gapped``
(no seconds: the lost event may have ended foreground); missing telemetry
is ``missing`` (no seconds), never zero. Renders of one
observation (clinician × patient × timepoint × visit kind) in one tab are
sequential and sum. More than one tab is ``multi_tab`` unless the tabs
provably took turns (S11m, :func:`took_turns`), then they sum too::

    tab A  claimed ── reports ── released / lease expired
    tab B                                         claimed ── reports
"""

from __future__ import annotations

import bisect
import itertools
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from ehr_simulator.db.telemetry import RenderRow, TabAuditRow, TelemetryRow

__all__ = [
    "Interval",
    "ObservationTiming",
    "RenderTimeline",
    "RenderTiming",
    "StateChange",
    "UNMEASURABLE",
    "TelemetryStatus",
    "aggregate_status",
    "build_timeline",
    "derive_observation_timings",
    "derive_render_timing",
    "group_observations",
    "intersect",
    "measure",
    "rows_by_render",
    "took_turns",
    "union",
    "worst_status",
]

MS_PER_SECOND = 1000.0

ENTER = "browser.timepoint_enter"
STATE = "browser.state"
ACTIVITY = "browser.activity"
EXIT = "browser.timepoint_exit"
GAP = "browser.gap"

#: Kinds whose ``client_mono_ms`` may run behind their ``client_seq``: a
#: trailing activity keeps its own (earlier) time; a gap's time is unused.
_OUT_OF_SEQUENCE_KINDS = frozenset({ACTIVITY, GAP})

#: ``(start_ms, end_ms)`` on one render's monotonic clock.
Interval = tuple[float, float]


class TelemetryStatus(StrEnum):
    COMPLETE = "complete"  # enter + exit, nothing lost
    INCOMPLETE = "incomplete"  # tail truncated (no exit) or unobserved renders: lower bound
    GAPPED = "gapped"  # events lost inside the stream: no lower bound
    MISSING = "missing"  # rendered, but no browser event ever arrived
    MULTI_TAB = "multi_tab"  # more than one tab reported the observation
    INVALID = "invalid"  # monotonic time runs backwards


_STATUS_RANK = {
    TelemetryStatus.COMPLETE: 0,
    TelemetryStatus.INCOMPLETE: 1,
    TelemetryStatus.GAPPED: 2,
    TelemetryStatus.MISSING: 3,
    TelemetryStatus.MULTI_TAB: 4,
    TelemetryStatus.INVALID: 5,
}


#: Render statuses whose measured milliseconds are no lower bound (or none exist).
UNMEASURABLE = frozenset({TelemetryStatus.MISSING, TelemetryStatus.GAPPED, TelemetryStatus.INVALID})


def worst_status(statuses: Iterable[TelemetryStatus]) -> TelemetryStatus:
    return max(statuses, key=_STATUS_RANK.__getitem__, default=TelemetryStatus.MISSING)


# ---------------------------------------------------------------------------
# Interval arithmetic
# ---------------------------------------------------------------------------


def union(intervals: Iterable[Interval]) -> tuple[Interval, ...]:
    """Merge overlapping or touching intervals. ``[(0,2),(1,3)] → ((0,3),)``."""
    merged: list[list[float]] = []
    for start, end in sorted(i for i in intervals if i[1] > i[0]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
            continue
        merged.append([start, end])
    return tuple((s, e) for s, e in merged)


def intersect(a: Iterable[Interval], b: Iterable[Interval]) -> tuple[Interval, ...]:
    """Pairwise overlap of two interval sets, merged."""
    left, right = union(a), union(b)
    out: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            out.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return union(out)


def measure(intervals: Iterable[Interval]) -> float:
    return sum(end - start for start, end in union(intervals))


# ---------------------------------------------------------------------------
# One render
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StateChange:
    mono_ms: float
    visible: bool
    focused: bool

    @property
    def foreground(self) -> bool:
        return self.visible and self.focused


@dataclass(frozen=True)
class RenderTimeline:
    """One render's events on its own clock. ``events`` are the in-interval
    rows sorted by ``(client_mono_ms, client_seq)``; ``states`` start at the
    enter snapshot. S11k builds panel exposure on the same timeline."""

    render: RenderRow
    status: TelemetryStatus
    tab_ids: frozenset[str]
    start_ms: float | None
    end_ms: float | None
    exit_reason: str | None
    states: tuple[StateChange, ...]
    events: tuple[TelemetryRow, ...]
    #: S11m: server arrival (event_id) of each tab's last row, in or out of
    #: the interval — a row after another tab's grant means the tabs overlapped.
    last_arrival: Mapping[str, int] = field(default_factory=dict)

    @property
    def foreground(self) -> tuple[Interval, ...]:
        if self.start_ms is None or self.end_ms is None:
            return ()
        spans: list[Interval] = []
        for current, following in zip(self.states, (*self.states[1:], None), strict=True):
            if not current.foreground:
                continue
            end = following.mono_ms if following is not None else self.end_ms
            spans.append((max(current.mono_ms, self.start_ms), min(end, self.end_ms)))
        return union(spans)

    def state_at(self, mono_ms: float) -> StateChange | None:
        """The state in force at ``mono_ms`` (a change at that instant applies)."""
        times = [s.mono_ms for s in self.states]
        index = bisect.bisect_right(times, mono_ms) - 1
        return self.states[index] if index >= 0 else None


def _sort_key(row: TelemetryRow) -> tuple[float, int]:
    return (row.client_mono_ms, row.client_seq)


def _runs_backwards(rows: Sequence[TelemetryRow]) -> bool:
    ordered = sorted(
        (r for r in rows if r.kind not in _OUT_OF_SEQUENCE_KINDS), key=lambda r: r.client_seq
    )
    return any(b.client_mono_ms < a.client_mono_ms for a, b in itertools.pairwise(ordered))


def _last_arrival(rows: Sequence[TelemetryRow]) -> dict[str, int]:
    last: dict[str, int] = {}
    for row in rows:
        last[row.tab_id] = max(last.get(row.tab_id, row.event_id), row.event_id)
    return last


def build_timeline(render: RenderRow, rows: Sequence[TelemetryRow]) -> RenderTimeline:
    """Order one render's rows and fix its interval, states and status."""
    tab_ids = frozenset(r.tab_id for r in rows)
    ordered = sorted(rows, key=_sort_key)
    timed = [r for r in ordered if r.kind != GAP]
    if not timed:
        return RenderTimeline(
            render, TelemetryStatus.MISSING, tab_ids, None, None, None, (), (), _last_arrival(rows)
        )

    status = TelemetryStatus.COMPLETE
    enter = next((r for r in timed if r.kind == ENTER), None)
    start = enter.client_mono_ms if enter is not None else timed[0].client_mono_ms
    exit_row = next((r for r in timed if r.kind == EXIT and r.client_mono_ms >= start), None)
    end = exit_row.client_mono_ms if exit_row is not None else timed[-1].client_mono_ms
    if exit_row is None:
        status = TelemetryStatus.INCOMPLETE  # closed at the last event: a lower bound
    if enter is None or len(timed) != len(ordered):
        # A lost enter or a reported gap: the missing event may be the one
        # that ended foreground or exposure, so nothing here is a lower bound.
        status = TelemetryStatus.GAPPED

    inside = tuple(r for r in timed if start <= r.client_mono_ms <= end)
    states = [
        StateChange(r.client_mono_ms, bool(r.payload["visible"]), bool(r.payload["focused"]))
        for r in inside
        if r.kind in (ENTER, STATE)
    ]

    if len(tab_ids) > 1:
        status = TelemetryStatus.MULTI_TAB
    if _runs_backwards(rows):
        status = TelemetryStatus.INVALID
    return RenderTimeline(
        render=render,
        status=status,
        tab_ids=tab_ids,
        start_ms=start,
        end_ms=end,
        exit_reason=str(exit_row.payload["reason"]) if exit_row is not None else None,
        states=tuple(states),
        events=inside,
        last_arrival=_last_arrival(rows),
    )


@dataclass(frozen=True)
class RenderTiming:
    timeline: RenderTimeline
    foreground_ms: float | None
    active_ms: float | None


def derive_render_timing(
    timeline: RenderTimeline, *, inactivity_threshold_seconds: float
) -> RenderTiming:
    """Foreground and active milliseconds of one render; ``None`` when the
    render is missing, gapped or invalid (nothing there is a lower bound)."""
    if timeline.status in UNMEASURABLE:
        return RenderTiming(timeline, None, None)

    foreground = timeline.foreground
    window = inactivity_threshold_seconds * MS_PER_SECOND
    eligibility: list[Interval] = []
    for row in timeline.events:
        if row.kind not in (ENTER, ACTIVITY):
            continue
        state = timeline.state_at(row.client_mono_ms)
        if state is None or not state.foreground:
            continue  # activity while hidden or unfocused never counts
        eligibility.append((row.client_mono_ms, row.client_mono_ms + window))

    return RenderTiming(
        timeline=timeline,
        foreground_ms=measure(foreground),
        active_ms=measure(intersect(foreground, eligibility)),
    )


# ---------------------------------------------------------------------------
# One observation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationTiming:
    """Foreground/active seconds of one clinician × patient × timepoint ×
    visit kind. Seconds are measured lower bounds; ``None`` when nothing
    trustworthy exists (gapped, missing, invalid, or multi tab — see
    ``per_tab``)."""

    t_index: int
    visit_kind: str
    status: TelemetryStatus
    foreground_seconds: float | None
    active_seconds: float | None
    render_ids: tuple[str, ...]
    tab_ids: frozenset[str]
    per_tab: Mapping[str, tuple[float, float] | None]  # None: a gapped/invalid render


def rows_by_render(rows: Iterable[TelemetryRow]) -> dict[str, list[TelemetryRow]]:
    grouped: dict[str, list[TelemetryRow]] = defaultdict(list)
    for row in rows:
        grouped[row.render_id].append(row)
    return grouped


def _has_seq_collision(timelines: Sequence[RenderTimeline]) -> bool:
    """Two renders reporting the same ``(tab_id, client_seq)``: a duplicated
    tab shares its storage, and with it the id and the counter."""
    owner: dict[tuple[str, int], str] = {}
    for timeline in timelines:
        for row in timeline.events:
            key = (row.tab_id, row.client_seq)
            if owner.setdefault(key, row.render_id) != row.render_id:
                return True
    return False


def group_observations(
    renders: Iterable[RenderRow],
) -> dict[tuple[int, str], list[RenderRow]]:
    """Renders keyed by ``(t_index, visit_kind)``, in render order."""
    groups: dict[tuple[int, str], list[RenderRow]] = defaultdict(list)
    for render in sorted(renders, key=lambda r: r.event_id):
        groups[(render.t_index, render.visit_kind)].append(render)
    return groups


_CLAIMED = "tab.claimed"
_GAVE_UP = frozenset({"tab.released", "tab.lease_expired"})


def took_turns(timelines: Sequence[RenderTimeline], audit: Sequence[TabAuditRow]) -> bool:
    """True when every tab of an observation held the lease alone, in turn.

    Tabs are ordered by their first grant of one of the observation's
    renders. For each tab A followed by tab B, A must have released its
    lease (or had it expired) before B's grant, and none of A's rows may
    arrive after that grant — a tab that kept reporting overlapped. Event
    ids are the server's arrival order; client clocks are never compared.
    A tab without a grant (legacy S11j data) never took turns.
    """
    render_ids = {t.render.render_id for t in timelines}
    grants: dict[str, int] = {}
    for row in audit:
        if row.kind == _CLAIMED and row.render_id in render_ids:
            grants.setdefault(row.tab_id, row.event_id)

    tabs = frozenset().union(*(t.tab_ids for t in timelines))
    if not tabs <= grants.keys():
        return False

    last_row = {
        tab: max(t.last_arrival[tab] for t in timelines if tab in t.last_arrival) for tab in tabs
    }
    order = sorted(tabs, key=grants.__getitem__)
    for first, second in itertools.pairwise(order):
        handed_over = any(
            row.kind in _GAVE_UP and row.tab_id == first and grants[first] < row.event_id
            for row in audit
            if row.event_id < grants[second]
        )
        if not handed_over or last_row[first] > grants[second]:
            return False
    return True


def aggregate_status(
    timelines: Sequence[RenderTimeline], tab_audit: Sequence[TabAuditRow] = ()
) -> TelemetryStatus:
    """One observation's status from its renders (S11j rules; S11k reuses it).

    S11m: several tabs are ``multi_tab`` unless they :func:`took_turns`.
    """
    reported = [t for t in timelines if t.status is not TelemetryStatus.MISSING]
    tabs = frozenset().union(*(t.tab_ids for t in reported))
    if any(t.status is TelemetryStatus.INVALID for t in reported):
        return TelemetryStatus.INVALID
    if (len(tabs) > 1 and not took_turns(reported, tab_audit)) or _has_seq_collision(reported):
        return TelemetryStatus.MULTI_TAB
    if not reported:
        return TelemetryStatus.MISSING
    worst = worst_status(t.status for t in reported)
    if len(reported) != len(timelines):
        return worst_status((worst, TelemetryStatus.INCOMPLETE))
    return worst


def derive_observation_timings(
    renders: Iterable[RenderRow],
    rows: Iterable[TelemetryRow],
    *,
    inactivity_threshold_seconds: float,
    tab_audit: Sequence[TabAuditRow] = (),
) -> dict[tuple[int, str], ObservationTiming]:
    """Every observation of one clinician × patient keyed by
    ``(t_index, visit_kind)``. Revisit renders never extend primary ones;
    ``tab_audit`` (S11m ``tab.*`` rows) lets tabs that took turns sum."""
    by_render = rows_by_render(rows)
    out: dict[tuple[int, str], ObservationTiming] = {}
    for (t_index, visit_kind), group in group_observations(renders).items():
        timings = [
            derive_render_timing(
                build_timeline(r, by_render.get(r.render_id, [])),
                inactivity_threshold_seconds=inactivity_threshold_seconds,
            )
            for r in group
        ]
        timelines = [t.timeline for t in timings]
        status = aggregate_status(timelines, tab_audit)

        # Per tab diagnostics follow the observation rule: one unmeasurable
        # render makes its tab's value None, never a partial sum.
        per_tab: dict[str, tuple[float, float] | None] = {}
        for timing in timings:
            for tab in timing.timeline.tab_ids:
                so_far = per_tab.get(tab, (0.0, 0.0))
                if so_far is None or timing.foreground_ms is None or timing.active_ms is None:
                    per_tab[tab] = None
                    continue
                per_tab[tab] = (
                    so_far[0] + timing.foreground_ms / MS_PER_SECOND,
                    so_far[1] + timing.active_ms / MS_PER_SECOND,
                )

        measured = [v for v in per_tab.values() if v is not None]
        trustworthy = status in (TelemetryStatus.COMPLETE, TelemetryStatus.INCOMPLETE)
        foreground = sum(fg for fg, _ in measured) if trustworthy else None
        active = sum(a for _, a in measured) if trustworthy else None
        out[(t_index, visit_kind)] = ObservationTiming(
            t_index=t_index,
            visit_kind=visit_kind,
            status=status,
            foreground_seconds=foreground,
            active_seconds=active,
            render_ids=tuple(r.render_id for r in group),
            tab_ids=frozenset(per_tab),
            per_tab=dict(per_tab),
        )
    return out
