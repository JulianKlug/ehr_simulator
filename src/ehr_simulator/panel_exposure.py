"""S11k panel exposure episodes from raw browser primitives (pure).

Spec: ``specs/session-11k-panel-exposure.md``.

The browser reports only primitives (``panel.mount``, ``panel.viewport``
ratios, user ``panel.open``/``panel.close``) on top of the S11j render
timeline; episodes are reconstructed here, so the raw rows stay the only
source of truth::

    qualifying = mounted AND expanded AND ratio >= threshold
                 AND visible AND focused AND inside [enter, exit]

    ratio      ▁▁▁▇▇▇▇▇▇▇▁▁▁▁▇▇▇▇▇▇
    focused    ████████████████▁▁▁█
    episodes      [━━━━━]      [━]
                        ▲ scroll_out  ▲ focus_lost

Recent activity is deliberately absent: passive reading counts. Durations
are milliseconds of one render's monotonic clock; overlapping panels are
never summed into attention time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum

from ehr_simulator.behavioral_timing import (
    ENTER,
    EXIT,
    MS_PER_SECOND,
    STATE,
    TRUSTWORTHY,
    UNMEASURABLE,
    RenderTimeline,
    TelemetryStatus,
    aggregate_status,
    build_timelines,
    group_observations,
    sum_per_tab,
)
from ehr_simulator.domain_types import RenderRow, TabAuditRow, TelemetryRow

__all__ = [
    "PANEL_IDS",
    "EndReason",
    "Episode",
    "PanelSummary",
    "RenderPanelExposure",
    "derive_panel_summaries",
    "render_panel_exposure",
]

#: The instrumented information panels (``section[data-panel]``).
PANEL_IDS = ("admission", "vitals", "labs", "imaging", "ai")

#: Every panel primitive's kind starts with it.
PANEL_KIND_PREFIX = "panel."
MOUNT = "panel.mount"
VIEWPORT = "panel.viewport"
OPEN = "panel.open"
CLOSE = "panel.close"


class EndReason(StrEnum):
    SCROLL_OUT = "scroll_out"
    TAB_HIDDEN = "tab_hidden"
    FOCUS_LOST = "focus_lost"
    PANEL_COLLAPSED = "panel_collapsed"
    TIMEPOINT_EXIT = "timepoint_exit"
    PAGEHIDE = "pagehide"
    TRUNCATED = "truncated"  # the stream ended without an exit


_EXIT_REASONS = {"swap": EndReason.TIMEPOINT_EXIT, "pagehide": EndReason.PAGEHIDE}


@dataclass(frozen=True)
class Episode:
    start_ms: float
    end_ms: float
    start_client_ts: str | None
    end_client_ts: str | None
    end_reason: EndReason

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass(frozen=True)
class RenderPanelExposure:
    """One panel within one render."""

    render_id: str
    panel_id: str
    status: TelemetryStatus
    mounted: bool
    episodes: tuple[Episode, ...]
    open_count: int
    time_to_first_view_ms: float | None

    @property
    def qualifying_ms(self) -> float:
        return sum(e.duration_ms for e in self.episodes)


@dataclass
class _PanelState:
    mounted: bool = False
    expanded: bool = False
    ratio: float | None = None


def _cut_at_segment_end(open_start: tuple[float, str | None], last: TelemetryRow) -> Episode:
    """An episode still open when its segment ended: at its exit, else truncated."""
    reason = EndReason.TRUNCATED
    if last.kind == EXIT:
        reason = _EXIT_REASONS.get(str(last.payload["reason"]), reason)
    return Episode(open_start[0], last.client_mono_ms, open_start[1], last.client_ts, reason)


@dataclass
class _Replay:
    """Mutable replay state of one render for one panel."""

    panel: _PanelState
    visible: bool = False
    focused: bool = False
    open_count: int = 0

    def qualifying(self, viewport_threshold: float) -> bool:
        return (
            self.panel.mounted
            and self.panel.expanded
            and self.panel.ratio is not None
            and self.panel.ratio >= viewport_threshold
            and self.visible
            and self.focused
        )

    def apply(self, row: TelemetryRow, panel_id: str) -> None:
        """Fold one event into the state; other panels' rows are ignored."""
        mine = row.payload.get("panel_id") == panel_id
        if row.kind in (ENTER, STATE):
            self.visible, self.focused = bool(row.payload["visible"]), bool(row.payload["focused"])
        elif mine and row.kind == MOUNT:
            self.panel.mounted, self.panel.expanded = True, bool(row.payload["expanded"])
        elif mine and row.kind == VIEWPORT:
            self.panel.ratio = float(row.payload["intersection_ratio"])
        elif mine and row.kind == OPEN:
            self.panel.expanded = True
            self.open_count += 1
        elif mine and row.kind == CLOSE:
            self.panel.expanded = False


def _end_reason(before: _Replay, after: _Replay) -> EndReason:
    """Why a qualifying episode stopped: the first condition that dropped."""
    if before.visible and not after.visible:
        return EndReason.TAB_HIDDEN
    if before.focused and not after.focused:
        return EndReason.FOCUS_LOST
    if before.panel.expanded and not after.panel.expanded:
        return EndReason.PANEL_COLLAPSED
    return EndReason.SCROLL_OUT


def _close_at_stream_end(
    open_start: tuple[float, str | None], timeline: RenderTimeline
) -> Episode | None:
    """An episode still open after the last row: ends at the timeline end."""
    if timeline.end_ms is None:
        return None

    reason = _EXIT_REASONS.get(timeline.exit_reason or "", EndReason.TRUNCATED)
    end_ts = timeline.events[-1].client_ts if timeline.events else None
    return Episode(open_start[0], timeline.end_ms, open_start[1], end_ts, reason)


def _time_to_first_view_ms(kept: tuple[Episode, ...], timeline: RenderTimeline) -> float | None:
    """First episode start relative to the render start (needs an enter)."""
    has_enter = any(r.kind == ENTER for r in timeline.events)
    if not kept or not has_enter or timeline.start_ms is None:
        return None
    return kept[0].start_ms - timeline.start_ms


def render_panel_exposure(
    timeline: RenderTimeline, panel_id: str, *, viewport_threshold: float
) -> RenderPanelExposure:
    """Replay one render's events and cut ``panel_id``'s episodes."""
    state = _Replay(panel=_PanelState())
    episodes: list[Episode] = []
    open_start: tuple[float, str | None] | None = None

    # S11m: a restarted segment (second enter) re-reports everything; the
    # unobserved time before it ends any open episode at the previous row.
    restarts = {start for start, _ in timeline.segments[1:]}
    previous: TelemetryRow | None = None
    for row in timeline.events:
        if row.kind == ENTER and row.client_mono_ms in restarts and previous is not None:
            if open_start is not None:
                episodes.append(_cut_at_segment_end(open_start, previous))
                open_start = None
            state.panel = _PanelState()
        previous = row

        before = replace(state, panel=replace(state.panel))
        was_qualifying = before.qualifying(viewport_threshold)
        state.apply(row, panel_id)
        is_qualifying = state.qualifying(viewport_threshold)

        if not was_qualifying and is_qualifying:
            open_start = (row.client_mono_ms, row.client_ts)
        elif was_qualifying and not is_qualifying and open_start is not None:
            reason = _end_reason(before, state)
            episodes.append(
                Episode(open_start[0], row.client_mono_ms, open_start[1], row.client_ts, reason)
            )
            open_start = None

    if open_start is not None:
        last = _close_at_stream_end(open_start, timeline)
        if last is not None:
            episodes.append(last)

    kept = tuple(e for e in episodes if e.duration_ms > 0)
    return RenderPanelExposure(
        render_id=timeline.render.render_id,
        panel_id=panel_id,
        status=timeline.status,
        mounted=state.panel.mounted,
        episodes=kept,
        open_count=state.open_count,
        time_to_first_view_ms=_time_to_first_view_ms(kept, timeline),
    )


@dataclass(frozen=True)
class PanelSummary:
    """One panel of one clinician × patient × timepoint × visit kind.

    ``qualifying_seconds`` is a measured lower bound (``None`` when nothing
    trustworthy exists: a gapped stream is not a lower bound). ``viewed``:
    ``True`` once the bound reaches the threshold, ``False`` only on
    complete telemetry, else ``None``. ``episode_count`` and
    ``time_to_first_view_seconds`` are ``None`` with the seconds.
    """

    t_index: int
    visit_kind: str
    panel_id: str
    status: TelemetryStatus
    mounted: bool
    qualifying_seconds: float | None
    viewed: bool | None
    episode_count: int | None
    panel_open_count: int
    time_to_first_view_seconds: float | None
    first_view_client_ts: str | None
    last_view_client_ts: str | None
    render_ids: tuple[str, ...]
    per_tab_seconds: Mapping[str, float | None]  # None: a gapped/invalid render


def _viewed(status: TelemetryStatus, qualifying_ms: float, threshold_seconds: float) -> bool | None:
    if status not in TRUSTWORTHY:
        return None
    if qualifying_ms >= threshold_seconds * MS_PER_SECOND:
        return True
    return False if status is TelemetryStatus.COMPLETE else None


def derive_panel_summaries(
    renders: Iterable[RenderRow],
    rows: Iterable[TelemetryRow],
    *,
    viewport_threshold: float,
    viewed_threshold_seconds: float,
    tab_audit: Sequence[TabAuditRow] = (),
    timelines: Mapping[str, RenderTimeline] | None = None,
) -> dict[tuple[int, str, str], PanelSummary]:
    """Every panel summary of one clinician × patient, keyed by
    ``(t_index, visit_kind, panel_id)``. Revisits never extend primaries.
    ``timelines``: prebuilt from the same renders and rows (``build_timelines``)."""
    renders = list(renders)
    if timelines is None:
        timelines = build_timelines(renders, rows)

    out: dict[tuple[int, str, str], PanelSummary] = {}
    for (t_index, visit_kind), group in group_observations(renders).items():
        group_timelines = [timelines[r.render_id] for r in group]
        status = aggregate_status(group_timelines, tab_audit)
        for panel_id in PANEL_IDS:
            exposures = [
                render_panel_exposure(t, panel_id, viewport_threshold=viewport_threshold)
                for t in group_timelines
            ]
            out[(t_index, visit_kind, panel_id)] = _summarise(
                t_index,
                visit_kind,
                panel_id,
                status,
                group_timelines,
                exposures,
                viewed_threshold_seconds,
            )
    return out


def _summarise(
    t_index: int,
    visit_kind: str,
    panel_id: str,
    status: TelemetryStatus,
    timelines: list[RenderTimeline],
    exposures: list[RenderPanelExposure],
    viewed_threshold_seconds: float,
) -> PanelSummary:
    per_tab = sum_per_tab(
        (
            timeline,
            None if timeline.status in UNMEASURABLE else exposure.qualifying_ms / MS_PER_SECOND,
        )
        for timeline, exposure in zip(timelines, exposures, strict=True)
    )

    episodes = [e for x in exposures for e in x.episodes]
    total_ms = sum(x.qualifying_ms for x in exposures)
    trustworthy = status in TRUSTWORTHY
    first = exposures[0] if exposures else None
    # S11k: like the seconds, counts and first view exist only on a lower bound.
    first_view_s = (
        first.time_to_first_view_ms / MS_PER_SECOND
        if trustworthy and first is not None and first.time_to_first_view_ms is not None
        else None
    )
    return PanelSummary(
        t_index=t_index,
        visit_kind=visit_kind,
        panel_id=panel_id,
        status=status,
        mounted=any(x.mounted for x in exposures),
        qualifying_seconds=total_ms / MS_PER_SECOND if trustworthy else None,
        viewed=_viewed(status, total_ms, viewed_threshold_seconds),
        episode_count=len(episodes) if trustworthy else None,
        panel_open_count=sum(x.open_count for x in exposures),
        time_to_first_view_seconds=first_view_s,
        first_view_client_ts=episodes[0].start_client_ts if episodes else None,
        last_view_client_ts=episodes[-1].end_client_ts if episodes else None,
        render_ids=tuple(x.render_id for x in exposures),
        per_tab_seconds=dict(per_tab),
    )
