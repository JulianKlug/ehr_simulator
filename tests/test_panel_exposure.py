"""S11k: panel exposure episodes, summaries and the panel DOM contract.

Derivation streams reuse the S11j ``Stream`` builder (seconds on one render's
monotonic clock) plus the panel primitives::

    enter ── mount(vitals) ── viewport(0.3) ━━ exposure ━━ viewport(0) ── exit
"""

from __future__ import annotations

from typing import Any, get_args

import pytest
from bs4 import BeautifulSoup

from ehr_simulator.behavioral_timing import TelemetryStatus, build_timeline
from ehr_simulator.panel_exposure import (
    PANEL_IDS,
    EndReason,
    derive_panel_summaries,
    render_panel_exposure,
)
from ehr_simulator.web.telemetry import PanelId
from tests.test_behavioral_timing import S, Stream, _render
from tests.test_case_start import Harness, _start, _started_patient, harness  # noqa: F401
from tests.test_intervention import _cases_by_arm, _page
from tests.test_telemetry import TELEMETRY

VIEWPORT = 0.05
VIEWED_S = 2.0
PANEL = "vitals"


class PanelStream(Stream):
    def mount(self, at: float, panel: str = PANEL, *, expanded: bool = True) -> PanelStream:
        self._add(
            "panel.mount",
            at,
            {"panel_id": panel, "expanded": expanded, "collapsible": True, "state": "loading"},
        )
        return self

    def ratio(self, at: float, value: float, panel: str = PANEL) -> PanelStream:
        self._add("panel.viewport", at, {"panel_id": panel, "intersection_ratio": value})
        return self

    def open(self, at: float, panel: str = PANEL) -> PanelStream:
        self._add("panel.open", at, {"panel_id": panel})
        return self

    def close(self, at: float, panel: str = PANEL) -> PanelStream:
        self._add("panel.close", at, {"panel_id": panel})
        return self


def _exposure(stream: Stream, panel: str = PANEL, threshold: float = VIEWPORT):
    timeline = build_timeline(_render(stream.render_id), stream.rows)
    return render_panel_exposure(timeline, panel, viewport_threshold=threshold)


def _visible_from_zero() -> PanelStream:
    return PanelStream().enter(0).mount(0).ratio(0, 0.5)  # type: ignore[return-value]


def _summary(streams: list[Stream], renders: list[Any] | None = None, panel: str = PANEL):
    rows = [row for s in streams for row in s.rows]
    renders = renders or [_render(s.render_id, event_id=i + 1) for i, s in enumerate(streams)]
    summaries = derive_panel_summaries(
        renders, rows, viewport_threshold=VIEWPORT, viewed_threshold_seconds=VIEWED_S
    )
    return summaries


# ---------------------------------------------------------------------------
# Viewport threshold
# ---------------------------------------------------------------------------


def test_ratio_below_threshold_does_not_qualify() -> None:
    stream = PanelStream().enter(0).mount(0).ratio(0, 0.049).exit(10)

    assert _exposure(stream).episodes == ()


def test_ratio_at_threshold_qualifies() -> None:
    stream = PanelStream().enter(0).mount(0).ratio(0, 0.05).exit(10)

    assert _exposure(stream).qualifying_ms == 10 * S


def test_ratio_is_not_rounded_up() -> None:
    stream = PanelStream().enter(0).mount(0).ratio(0, 0.0499999).exit(10)

    assert _exposure(stream).qualifying_ms == 0


def test_ratio_unknown_until_first_viewport_event() -> None:
    stream = PanelStream().enter(0).mount(0).ratio(4, 1.0).exit(10)

    assert _exposure(stream).qualifying_ms == 6 * S


# ---------------------------------------------------------------------------
# Episodes and end reasons
# ---------------------------------------------------------------------------


def test_scroll_out_and_back_makes_two_episodes() -> None:
    stream = _visible_from_zero().ratio(3, 0.0).ratio(5, 0.4).exit(8)
    exposure = _exposure(stream)

    assert [e.end_reason for e in exposure.episodes] == [
        EndReason.SCROLL_OUT,
        EndReason.TIMEPOINT_EXIT,
    ]
    assert exposure.qualifying_ms == (3 + 3) * S


def test_hide_ends_with_tab_hidden_and_return_restarts() -> None:
    stream = _visible_from_zero()
    stream.state(2, visible=False, focused=False, reason="visibilitychange")
    stream.state(4, visible=True, focused=True, reason="visibilitychange").exit(6)
    exposure = _exposure(stream)

    assert [e.end_reason for e in exposure.episodes] == [
        EndReason.TAB_HIDDEN,
        EndReason.TIMEPOINT_EXIT,
    ]
    assert exposure.qualifying_ms == 4 * S


def test_blur_ends_with_focus_lost_and_refocus_restarts() -> None:
    stream = _visible_from_zero()
    stream.state(2, visible=True, focused=False, reason="blur")
    stream.state(3, visible=True, focused=True, reason="focus").exit(5)
    exposure = _exposure(stream)

    assert exposure.episodes[0].end_reason is EndReason.FOCUS_LOST
    assert len(exposure.episodes) == 2


def test_collapse_ends_with_panel_collapsed_and_reopen_restarts() -> None:
    stream = _visible_from_zero().close(2).ratio(2.1, 0.0).open(4).ratio(4.1, 0.5).exit(6)
    exposure = _exposure(stream)

    assert [e.end_reason for e in exposure.episodes] == [
        EndReason.PANEL_COLLAPSED,
        EndReason.TIMEPOINT_EXIT,
    ]
    assert exposure.qualifying_ms == (2 + 1.9) * S
    assert exposure.open_count == 1


@pytest.mark.parametrize(
    ("exit_reason", "expected"),
    [("swap", EndReason.TIMEPOINT_EXIT), ("pagehide", EndReason.PAGEHIDE)],
)
def test_exit_reason_ends_the_open_episode(exit_reason: str, expected: EndReason) -> None:
    exposure = _exposure(_visible_from_zero().exit(3, exit_reason))

    assert exposure.episodes[-1].end_reason is expected


def test_stream_without_exit_is_truncated_and_incomplete() -> None:
    exposure = _exposure(_visible_from_zero().activity(4))

    assert exposure.episodes[-1].end_reason is EndReason.TRUNCATED
    assert exposure.status is TelemetryStatus.INCOMPLETE
    assert exposure.qualifying_ms == 4 * S  # never extended past the last event


def test_episodes_never_overlap() -> None:
    stream = _visible_from_zero().ratio(1, 0.6).ratio(2, 0.9).ratio(3, 0.0).exit(4)
    episodes = _exposure(stream).episodes

    assert len(episodes) == 1
    assert all(a.end_ms <= b.start_ms for a, b in zip(episodes, episodes[1:], strict=False))


def test_open_without_viewport_qualification_is_zero() -> None:
    stream = PanelStream().enter(0).mount(0, expanded=False).open(1).exit(5)
    exposure = _exposure(stream)

    assert exposure.open_count == 1
    assert exposure.qualifying_ms == 0


def test_collapsed_panel_accumulates_nothing() -> None:
    stream = PanelStream().enter(0).mount(0, expanded=False).ratio(0, 0.0).exit(5)

    assert _exposure(stream).qualifying_ms == 0


def test_passive_reading_continues_after_active_time_expired() -> None:
    exposure = _exposure(_visible_from_zero().exit(120))

    assert exposure.qualifying_ms == 120 * S  # no activity after entry, still exposed


def test_unfocused_from_the_start_contributes_nothing() -> None:
    stream = PanelStream().enter(0, focused=False).mount(0).ratio(0, 1.0).exit(10)

    assert _exposure(stream).qualifying_ms == 0


def test_other_panels_events_do_not_move_this_panel() -> None:
    stream = _visible_from_zero().close(2, panel="labs").ratio(3, 0.0, panel="labs").exit(5)

    assert _exposure(stream).qualifying_ms == 5 * S


def test_time_to_first_view_from_render_enter() -> None:
    stream = PanelStream().enter(0).mount(0).ratio(0, 0.0).ratio(7, 0.3).exit(9)

    assert _exposure(stream).time_to_first_view_ms == 7 * S


# ---------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------


def _viewed_for(seconds: float) -> Any:
    stream = _visible_from_zero().ratio(seconds, 0.0).exit(10)
    return _summary([stream])[(0, "primary", PANEL)]


def test_two_one_second_episodes_are_viewed() -> None:
    stream = _visible_from_zero().ratio(1, 0.0).ratio(5, 0.3).ratio(6, 0.0).exit(10)
    summary = _summary([stream])[(0, "primary", PANEL)]

    assert summary.qualifying_seconds == 2.0
    assert summary.viewed is True
    assert summary.episode_count == 2


@pytest.mark.parametrize(("seconds", "viewed"), [(1.99, False), (2.0, True), (2.01, True)])
def test_viewed_threshold(seconds: float, viewed: bool) -> None:
    summary = _viewed_for(seconds)

    assert summary.viewed is viewed
    assert summary.qualifying_seconds == pytest.approx(seconds)


def test_next_timepoint_starts_from_zero() -> None:
    t0 = _visible_from_zero().exit(10)
    t1 = PanelStream("r2", seq_start=50).enter(0).mount(0).ratio(0, 0.0).exit(10)
    renders = [_render("r1"), _render("r2", t_index=1, event_id=2)]
    summaries = _summary([t0, t1], renders)

    assert summaries[(0, "primary", PANEL)].qualifying_seconds == 10
    assert summaries[(1, "primary", PANEL)].qualifying_seconds == 0
    assert summaries[(1, "primary", PANEL)].viewed is False


def test_incomplete_above_threshold_is_viewed_below_is_unknown() -> None:
    above = _summary([_visible_from_zero().activity(3)])[(0, "primary", PANEL)]
    below = _summary([_visible_from_zero().activity(1)])[(0, "primary", PANEL)]

    assert above.status is TelemetryStatus.INCOMPLETE and above.viewed is True
    assert below.viewed is None


def test_missing_telemetry_is_not_not_viewed() -> None:
    summary = _summary([Stream()])[(0, "primary", PANEL)]

    assert summary.status is TelemetryStatus.MISSING
    assert summary.viewed is None and summary.qualifying_seconds is None


def test_summary_counts_and_client_timestamps() -> None:
    stream = PanelStream().enter(0).mount(0, expanded=False).ratio(0, 0.0)
    stream.open(3).ratio(3.5, 0.5).close(6).open(7).ratio(7.1, 0.5).exit(9)
    for i, row in enumerate(stream.rows):
        object.__setattr__(row, "client_ts", f"ts{i}")
    summary = _summary([stream])[(0, "primary", PANEL)]

    assert summary.panel_open_count == 2
    assert summary.episode_count == 2
    assert summary.time_to_first_view_seconds == 3.5
    assert summary.first_view_client_ts == "ts4"  # the viewport event that started it
    assert summary.last_view_client_ts == "ts8"  # the exit that ended the last


def test_non_collapsible_panel_has_no_open_count() -> None:
    stream = PanelStream().enter(0)
    stream._add(
        "panel.mount",
        0,
        {"panel_id": PANEL, "expanded": True, "collapsible": False, "state": "loading"},
    )
    stream.ratio(0, 0.5).exit(4)
    summary = _summary([stream])[(0, "primary", PANEL)]

    assert summary.panel_open_count == 0
    assert summary.viewed is True


def test_revisit_exposure_is_separate() -> None:
    primary = _visible_from_zero().ratio(1, 0.0).exit(10)
    revisit = PanelStream("r2", seq_start=50).enter(0).mount(0).ratio(0, 0.5).exit(30)
    renders = [_render("r1"), _render("r2", visit_kind="revisit", event_id=2)]
    summaries = _summary([primary, revisit], renders)

    assert summaries[(0, "primary", PANEL)].viewed is False
    assert summaries[(0, "revisit", PANEL)].qualifying_seconds == 30


def test_multi_tab_is_not_summed() -> None:
    a = _visible_from_zero().exit(1.5)
    b = PanelStream("r2", tab_id="tab-b").enter(0).mount(0).ratio(0, 0.5).exit(1.5)
    summary = _summary([a, b])[(0, "primary", PANEL)]

    assert summary.status is TelemetryStatus.MULTI_TAB
    assert summary.qualifying_seconds is None and summary.viewed is None
    assert summary.per_tab_seconds == {"tab-a": 1.5, "tab-b": 1.5}


def test_unmounted_panel_is_reported_unmounted() -> None:
    summary = _summary([_visible_from_zero().exit(5)])[(0, "primary", "ai")]

    assert summary.mounted is False
    assert summary.qualifying_seconds == 0 and summary.viewed is False


def test_panel_ids_lockstep_with_payload_contract() -> None:
    assert set(get_args(PanelId)) == set(PANEL_IDS)


# ---------------------------------------------------------------------------
# DOM contract
# ---------------------------------------------------------------------------


@pytest.fixture
def telemetry_study(harness: Harness):  # noqa: F811
    config = harness.variant("v2", telemetry=TELEMETRY)
    harness.activate(config)
    return harness, config


def _two_case_pages(study: Harness, config: Any, chrome: str) -> list[BeautifulSoup]:
    """The first timepoint of one AI and one no AI case."""
    with study.boot(config) as client:
        cases = _cases_by_arm(study, client)
        return [
            BeautifulSoup(_page(client, cases[arm], chrome=chrome), "html.parser")
            for arm in ("ai", "no_ai")
        ]


@pytest.mark.parametrize("chrome", ["epic", "dense"])
def test_every_panel_has_one_content_element_without_header(
    telemetry_study: Any, chrome: str
) -> None:
    study, config = telemetry_study
    for soup in _two_case_pages(study, config, chrome):
        sections = soup.select("section[data-panel]")
        assert {s["data-panel"] for s in sections} <= set(PANEL_IDS)
        for section in sections:
            contents = section.select("[data-panel-content]")
            assert len(contents) == 1, section["data-panel"]
            assert contents[0].find("header") is None
            assert section.find("header", recursive=False) is not None


def test_ai_section_only_in_the_ai_arm(telemetry_study: Any) -> None:
    study, config = telemetry_study
    ai_page, no_ai_page = _two_case_pages(study, config, "epic")

    assert len(ai_page.select("section[data-panel='ai'] [data-panel-content]")) == 1
    assert no_ai_page.select("section[data-panel='ai']") == []


def test_no_threshold_attribute_without_telemetry(harness: Harness) -> None:  # noqa: F811
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))
        soup = BeautifulSoup(client.get(f"/patient/{patient_id}/timepoint/0").text, "html.parser")

    assert not soup.select_one("#patient-view").has_attr("data-viewport-threshold")
