"""Telemetry row builders for the pure timeline derivations (S11j, S11k)."""

from __future__ import annotations

from ehr_simulator.db.telemetry import RenderRow, TelemetryRow

TAB = "tab-a"
S = 1000.0  # milliseconds per second


class Stream:
    """Builds one render's rows in the order the browser would send them."""

    def __init__(
        self, render_id: str = "r1", tab_id: str = TAB, seq_start: int = 1, event_base: int = 0
    ) -> None:
        self.render_id = render_id
        self.tab_id = tab_id
        self.seq = seq_start
        self.event_base = event_base  # server event_id offset (cross-tab ordering)
        self.rows: list[TelemetryRow] = []

    def _add(self, kind: str, mono_s: float, payload: dict, seq: int | None = None) -> Stream:
        self.rows.append(
            TelemetryRow(
                event_id=self.event_base + len(self.rows) + 1,
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
