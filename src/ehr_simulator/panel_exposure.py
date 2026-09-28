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

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ehr_simulator.behavioral_timing import (
    ENTER,
    MS_PER_SECOND,
    STATE,
    UNMEASURABLE,
    RenderTimeline,
    TelemetryStatus,
    aggregate_status,
    build_timeline,
    group_observations,
    rows_by_render,
)
from ehr_simulator.db.telemetry import RenderRow, TelemetryRow

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

MOUNT = "panel.mount"
VIEWPORT = "panel.viewport"
OPEN = "panel.open"
CLOSE = "panel.close"

_TRUSTWORTHY = (TelemetryStatus.COMPLETE, TelemetryStatus.INCOMPLETE)


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


def render_panel_exposure(
    timeline: RenderTimeline, panel_id: str, *, viewport_threshold: float
) -> RenderPanelExposure:
    """Replay one render's events and cut ``panel_id``'s episodes."""
    panel = _PanelState()
    visible = focused = False
    episodes: list[Episode] = []
    open_start: tuple[float, str | None] | None = None
    open_count = 0

    def qualifying() -> bool:
        return (
            panel.mounted
            and panel.expanded
            and panel.ratio is not None
            and panel.ratio >= viewport_threshold
            and visible
            and focused
        )

    for row in timeline.events:
        mine = row.payload.get("panel_id") == panel_id
        before = qualifying()
        was_visible, was_focused, was_expanded = visible, focused, panel.expanded
        if row.kind in (ENTER, STATE):
            visible, focused = bool(row.payload["visible"]), bool(row.payload["focused"])
        elif mine and row.kind == MOUNT:
            panel.mounted, panel.expanded = True, bool(row.payload["expanded"])
        elif mine and row.kind == VIEWPORT:
            panel.ratio = float(row.payload["intersection_ratio"])
        elif mine and row.kind == OPEN:
            panel.expanded = True
            open_count += 1
        elif mine and row.kind == CLOSE:
            panel.expanded = False
        after = qualifying()

        if not before and after:
            open_start = (row.client_mono_ms, row.client_ts)
        elif before and not after and open_start is not None:
            if was_visible and not visible:
                reason = EndReason.TAB_HIDDEN
            elif was_focused and not focused:
                reason = EndReason.FOCUS_LOST
            elif was_expanded and not panel.expanded:
                reason = EndReason.PANEL_COLLAPSED
            else:
                reason = EndReason.SCROLL_OUT
            episodes.append(
                Episode(open_start[0], row.client_mono_ms, open_start[1], row.client_ts, reason)
            )
            open_start = None

    if open_start is not None and timeline.end_ms is not None:
        reason = _EXIT_REASONS.get(timeline.exit_reason or "", EndReason.TRUNCATED)
        end_ts = timeline.events[-1].client_ts if timeline.events else None
        episodes.append(Episode(open_start[0], timeline.end_ms, open_start[1], end_ts, reason))

    kept = tuple(e for e in episodes if e.duration_ms > 0)
    has_enter = any(r.kind == ENTER for r in timeline.events)
    first_view = (
        kept[0].start_ms - timeline.start_ms
        if kept and has_enter and timeline.start_ms is not None
        else None
    )
    return RenderPanelExposure(
        render_id=timeline.render.render_id,
        panel_id=panel_id,
        status=timeline.status,
        mounted=panel.mounted,
        episodes=kept,
        open_count=open_count,
        time_to_first_view_ms=first_view,
    )


@dataclass(frozen=True)
class PanelSummary:
    """One panel of one clinician × patient × timepoint × visit kind.

    ``qualifying_seconds`` is a measured lower bound (``None`` when nothing
    trustworthy exists: a gapped stream is not a lower bound). ``viewed``:
    ``True`` once the bound reaches the threshold, ``False`` only on
    complete telemetry, else ``None``.
    """

    t_index: int
    visit_kind: str
    panel_id: str
    status: TelemetryStatus
    mounted: bool
    qualifying_seconds: float | None
    viewed: bool | None
    episode_count: int
    panel_open_count: int
    time_to_first_view_seconds: float | None
    first_view_client_ts: str | None
    last_view_client_ts: str | None
    render_ids: tuple[str, ...]
    per_tab_seconds: Mapping[str, float | None]  # None: a gapped/invalid render


def _viewed(status: TelemetryStatus, qualifying_ms: float, threshold_seconds: float) -> bool | None:
    if status not in _TRUSTWORTHY:
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
) -> dict[tuple[int, str, str], PanelSummary]:
    """Every panel summary of one clinician × patient, keyed by
    ``(t_index, visit_kind, panel_id)``. Revisits never extend primaries."""
    by_render = rows_by_render(rows)
    out: dict[tuple[int, str, str], PanelSummary] = {}
    for (t_index, visit_kind), group in group_observations(renders).items():
        timelines = [build_timeline(r, by_render.get(r.render_id, [])) for r in group]
        status = aggregate_status(timelines)
        for panel_id in PANEL_IDS:
            exposures = [
                render_panel_exposure(t, panel_id, viewport_threshold=viewport_threshold)
                for t in timelines
            ]
            out[(t_index, visit_kind, panel_id)] = _summarise(
                t_index,
                visit_kind,
                panel_id,
                status,
                timelines,
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
    per_tab: dict[str, float | None] = {}
    for timeline, exposure in zip(timelines, exposures, strict=True):
        for tab in timeline.tab_ids:
            so_far = per_tab.get(tab, 0.0)
            if so_far is None or timeline.status in UNMEASURABLE:
                per_tab[tab] = None  # no partial sum from an unmeasurable render
                continue
            per_tab[tab] = so_far + exposure.qualifying_ms / MS_PER_SECOND

    episodes = [e for x in exposures for e in x.episodes]
    total_ms = sum(x.qualifying_ms for x in exposures)
    trustworthy = status in _TRUSTWORTHY
    first = exposures[0] if exposures else None
    return PanelSummary(
        t_index=t_index,
        visit_kind=visit_kind,
        panel_id=panel_id,
        status=status,
        mounted=any(x.mounted for x in exposures),
        qualifying_seconds=total_ms / MS_PER_SECOND if trustworthy else None,
        viewed=_viewed(status, total_ms, viewed_threshold_seconds),
        episode_count=len(episodes),
        panel_open_count=sum(x.open_count for x in exposures),
        time_to_first_view_seconds=(
            first.time_to_first_view_ms / MS_PER_SECOND
            if first is not None and first.time_to_first_view_ms is not None
            else None
        ),
        first_view_client_ts=episodes[0].start_client_ts if episodes else None,
        last_view_client_ts=episodes[-1].end_client_ts if episodes else None,
        render_ids=tuple(x.render_id for x in exposures),
        per_tab_seconds=dict(per_tab),
    )
