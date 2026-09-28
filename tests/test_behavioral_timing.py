"""S11j: foreground and active time derived from raw browser rows (pure).

Streams are written on one render's monotonic clock in milliseconds::

    enter(0) ── blur(10 s) ── focus(20 s) ── exit(30 s)
    foreground = 10 s + 10 s
"""

from __future__ import annotations

import pytest

from ehr_simulator.behavioral_timing import (
    TelemetryStatus,
    build_timeline,
    derive_observation_timings,
    derive_render_timing,
    intersect,
    measure,
    union,
)
from ehr_simulator.db.telemetry import RenderRow, TelemetryRow

TAB = "tab-a"
OTHER_TAB = "tab-b"
THRESHOLD_S = 60.0
S = 1000.0  # milliseconds per second


class Stream:
    """Builds one render's rows in the order the browser would send them."""

    def __init__(self, render_id: str = "r1", tab_id: str = TAB, seq_start: int = 1) -> None:
        self.render_id = render_id
        self.tab_id = tab_id
        self.seq = seq_start
        self.rows: list[TelemetryRow] = []

    def _add(self, kind: str, mono_s: float, payload: dict, seq: int | None = None) -> Stream:
        self.rows.append(
            TelemetryRow(
                event_id=len(self.rows) + 1,
                render_id=self.render_id,
                tab_id=self.tab_id,
                kind=kind,
                client_seq=self.seq if seq is None else seq,
                client_mono_ms=mono_s * S,
                client_ts=None,
                payload=payload,
            )
        )
        self.seq += 1
        return self

    def enter(self, at: float, *, visible: bool = True, focused: bool = True) -> Stream:
        return self._add("browser.timepoint_enter", at, {"visible": visible, "focused": focused})

    def state(self, at: float, *, visible: bool, focused: bool, reason: str = "blur") -> Stream:
        return self._add(
            "browser.state", at, {"visible": visible, "focused": focused, "reason": reason}
        )

    def activity(self, at: float, kind: str = "click", seq: int | None = None) -> Stream:
        return self._add("browser.activity", at, {"activity_kind": kind}, seq)

    def exit(self, at: float, reason: str = "swap") -> Stream:
        return self._add("browser.timepoint_exit", at, {"reason": reason})

    def gap(self, at: float) -> Stream:
        return self._add("browser.gap", at, {"dropped": 2})


def _render(
    render_id: str = "r1", t_index: int = 0, visit_kind: str = "primary", event_id: int = 1
) -> RenderRow:
    return RenderRow(
        event_id=event_id,
        render_id=render_id,
        session_id="s",
        clinician_id="c",
        patient_id="p",
        timepoint=0.0,
        payload={"t_index": t_index, "visit_kind": visit_kind},
    )


def _timing(stream: Stream, threshold_s: float = THRESHOLD_S):
    timeline = build_timeline(_render(stream.render_id), stream.rows)
    return derive_render_timing(timeline, inactivity_threshold_seconds=threshold_s)


# ---------------------------------------------------------------------------
# Interval arithmetic
# ---------------------------------------------------------------------------


def test_interval_helpers() -> None:
    assert union([(0, 2), (1, 3), (5, 6), (6, 7)]) == ((0, 3), (5, 7))
    assert intersect([(0, 10)], [(2, 3), (8, 12)]) == ((2, 3), (8, 10))
    assert measure([(0, 2), (1, 3)]) == 3


# ---------------------------------------------------------------------------
# Foreground
# ---------------------------------------------------------------------------


def test_visible_focused_interval_accumulates() -> None:
    timing = _timing(Stream().enter(0).exit(30))

    assert timing.timeline.status is TelemetryStatus.COMPLETE
    assert timing.foreground_ms == 30 * S


def test_hidden_tab_accumulates_nothing() -> None:
    stream = Stream().enter(0).state(5, visible=False, focused=False, reason="visibilitychange")
    timing = _timing(stream.exit(30))

    assert timing.foreground_ms == 5 * S


def test_unfocused_window_accumulates_nothing() -> None:
    timing = _timing(Stream().enter(0, focused=False).exit(30))

    assert timing.foreground_ms == 0


def test_refocus_resumes_and_intervals_sum() -> None:
    stream = (
        Stream()
        .enter(0)
        .state(10, visible=False, focused=False, reason="visibilitychange")
        .state(20, visible=True, focused=True, reason="visibilitychange")
        .exit(30)
    )
    timing = _timing(stream)

    assert timing.timeline.foreground == ((0, 10 * S), (20 * S, 30 * S))
    assert timing.foreground_ms == 20 * S


def test_missing_exit_ends_at_last_event_and_is_incomplete() -> None:
    timing = _timing(Stream().enter(0).activity(12))

    assert timing.timeline.status is TelemetryStatus.INCOMPLETE
    assert timing.foreground_ms == 12 * S


def test_events_after_exit_are_ignored() -> None:
    timing = _timing(Stream().enter(0).exit(10).activity(50))

    assert timing.foreground_ms == 10 * S


def test_render_without_events_is_missing_not_zero() -> None:
    timing = _timing(Stream())

    assert timing.timeline.status is TelemetryStatus.MISSING
    assert timing.foreground_ms is None and timing.active_ms is None


def test_reported_gap_is_gapped_not_incomplete() -> None:
    # A lost event may be the one that ended foreground: no lower bound.
    timing = _timing(Stream().enter(0).exit(10).gap(11))

    assert timing.timeline.status is TelemetryStatus.GAPPED


def test_gapped_observation_reports_no_seconds() -> None:
    stream = Stream("r1").enter(0).gap(1).exit(20)
    result = _observations([_render("r1")], [stream])[(0, "primary")]

    assert result.status is TelemetryStatus.GAPPED
    assert result.foreground_seconds is None and result.active_seconds is None


def test_gap_outranks_tail_truncation_across_renders() -> None:
    truncated = Stream("r1").enter(0).activity(5)
    gapped = Stream("r2", seq_start=20).enter(0).gap(1).exit(5)
    result = _observations([_render("r1"), _render("r2", event_id=2)], [truncated, gapped])

    assert result[(0, "primary")].status is TelemetryStatus.GAPPED


def test_backwards_monotonic_time_is_invalid() -> None:
    stream = Stream().enter(10).state(5, visible=True, focused=False).exit(20)

    assert _timing(stream).timeline.status is TelemetryStatus.INVALID


def test_trailing_activity_behind_its_sequence_is_valid() -> None:
    stream = Stream().enter(0).state(0.5, visible=True, focused=True, reason="periodic")
    stream.activity(0.2).exit(1)

    assert _timing(stream).timeline.status is TelemetryStatus.COMPLETE


# ---------------------------------------------------------------------------
# Active
# ---------------------------------------------------------------------------


def test_enter_starts_eligibility() -> None:
    timing = _timing(Stream().enter(0).exit(30))

    assert timing.active_ms == 30 * S


def test_activity_within_threshold_extends_active() -> None:
    timing = _timing(Stream().enter(0).activity(50).exit(100))

    assert timing.active_ms == 100 * S


def test_inactivity_beyond_threshold_is_not_active() -> None:
    timing = _timing(Stream().enter(0).exit(100))

    assert timing.foreground_ms == 100 * S
    assert timing.active_ms == 60 * S


def test_later_click_resumes_from_itself() -> None:
    timing = _timing(Stream().enter(0).activity(90).exit(120))

    assert timing.active_ms == (60 + 30) * S  # 60..90 stays inactive


@pytest.mark.parametrize("kind", ["scroll", "keyboard", "answer_change", "touch"])
def test_other_activity_kinds_resume(kind: str) -> None:
    timing = _timing(Stream().enter(0).activity(100, kind).exit(130))

    assert timing.active_ms == (60 + 30) * S


def test_mouse_movement_alone_never_resumes() -> None:
    # The client never reports mousemove: a silent stretch stays inactive.
    timing = _timing(Stream().enter(0).exit(200))

    assert timing.active_ms == 60 * S


def test_activity_while_hidden_never_counts() -> None:
    stream = (
        Stream()
        .enter(0)
        .state(10, visible=False, focused=False, reason="visibilitychange")
        .activity(70)  # injected while hidden
        .state(80, visible=True, focused=True, reason="visibilitychange")
        .exit(100)
    )
    timing = _timing(stream)

    assert timing.foreground_ms == 30 * S
    assert timing.active_ms == 10 * S  # only 0..10; 80..100 has no foreground activity


def test_threshold_is_configurable() -> None:
    timing = _timing(Stream().enter(0).exit(100), threshold_s=10)

    assert timing.active_ms == 10 * S


def test_throttled_samples_match_the_full_stream() -> None:
    every_100ms = Stream().enter(0)
    for i in range(1, 51):
        every_100ms.activity(i * 0.1)
    every_100ms.exit(200)

    # Leading + trailing edge of each 1 s window, as static/telemetry.js sends.
    throttled = Stream().enter(0)
    for at in (1.0, 1.9, 2.0, 2.9, 3.0, 3.9, 4.0, 4.9, 5.0):
        throttled.activity(at)
    throttled.exit(200)

    assert _timing(every_100ms).active_ms == _timing(throttled).active_ms == 65 * S


def test_network_delay_does_not_change_durations() -> None:
    # server_ts is not an input at all: only client monotonic time is read.
    near = _timing(Stream().enter(0).exit(30))
    rows = Stream().enter(0).exit(30).rows
    late = derive_render_timing(
        build_timeline(_render(), list(reversed(rows))), inactivity_threshold_seconds=THRESHOLD_S
    )

    assert near.foreground_ms == late.foreground_ms == 30 * S


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


def _observations(renders: list[RenderRow], streams: list[Stream]):
    rows = [row for stream in streams for row in stream.rows]
    return derive_observation_timings(renders, rows, inactivity_threshold_seconds=THRESHOLD_S)


def test_same_tab_reload_sums_renders() -> None:
    first = Stream("r1").enter(0).exit(20, "pagehide")
    second = Stream("r2", seq_start=10).enter(0).exit(15)
    result = _observations([_render("r1"), _render("r2", event_id=2)], [first, second])[
        (0, "primary")
    ]

    assert result.status is TelemetryStatus.COMPLETE
    assert result.foreground_seconds == 35
    assert result.render_ids == ("r1", "r2")


def test_revisit_never_extends_primary() -> None:
    primary = Stream("r1").enter(0).exit(20)
    revisit = Stream("r2", seq_start=10).enter(0).exit(50)
    result = _observations(
        [_render("r1"), _render("r2", visit_kind="revisit", event_id=2)], [primary, revisit]
    )

    assert result[(0, "primary")].foreground_seconds == 20
    assert result[(0, "revisit")].foreground_seconds == 50


def test_two_tabs_are_multi_tab_not_summed() -> None:
    a = Stream("r1").enter(0).exit(20)
    b = Stream("r2", tab_id=OTHER_TAB).enter(0).exit(20)
    result = _observations([_render("r1"), _render("r2", event_id=2)], [a, b])[(0, "primary")]

    assert result.status is TelemetryStatus.MULTI_TAB
    assert result.foreground_seconds is None and result.active_seconds is None
    assert result.per_tab == {TAB: (20, 20), OTHER_TAB: (20, 20)}


def test_duplicated_tab_sequence_collision_is_multi_tab() -> None:
    a = Stream("r1", seq_start=5).enter(0).exit(20)
    b = Stream("r2", seq_start=5).enter(0).exit(20)  # same id, same counter
    result = _observations([_render("r1"), _render("r2", event_id=2)], [a, b])[(0, "primary")]

    assert result.status is TelemetryStatus.MULTI_TAB


def test_one_unreported_render_makes_the_observation_incomplete() -> None:
    reported = Stream("r1").enter(0).exit(20)
    result = _observations([_render("r1"), _render("r2", event_id=2)], [reported])[(0, "primary")]

    assert result.status is TelemetryStatus.INCOMPLETE
    assert result.foreground_seconds == 20  # a lower bound, flagged


def test_all_missing_is_missing_without_seconds() -> None:
    result = _observations([_render("r1")], [])[(0, "primary")]

    assert result.status is TelemetryStatus.MISSING
    assert result.foreground_seconds is None


def test_timepoints_are_separate_observations() -> None:
    t0 = Stream("r1").enter(0).exit(10)
    t1 = Stream("r2", seq_start=10).enter(0).exit(40)
    result = _observations([_render("r1"), _render("r2", t_index=1, event_id=2)], [t0, t1])

    assert result[(0, "primary")].foreground_seconds == 10
    assert result[(1, "primary")].foreground_seconds == 40


def test_gapped_render_measures_nothing() -> None:
    timing = _timing(Stream().enter(0).gap(1).exit(20))

    assert timing.foreground_ms is None and timing.active_ms is None


def test_invalid_render_measures_nothing() -> None:
    timing = _timing(Stream().enter(10).state(5, visible=True, focused=False).exit(20))

    assert timing.foreground_ms is None and timing.active_ms is None


def test_gapped_tab_has_no_per_tab_seconds() -> None:
    # Diagnostics follow the observation rule: a gapped tab is no lower bound.
    gapped = Stream("r1").enter(0).gap(1).exit(20)
    clean = Stream("r2", tab_id=OTHER_TAB).enter(0).exit(10)
    result = _observations([_render("r1"), _render("r2", event_id=2)], [gapped, clean])[
        (0, "primary")
    ]

    assert result.per_tab == {TAB: None, OTHER_TAB: (10, 10)}
