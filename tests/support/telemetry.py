"""Browser telemetry (S11j) batch builders."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

TELEMETRY = {
    "inactivity_threshold_seconds": 60,
    "panel_viewport_threshold": 0.05,
    "panel_viewed_threshold_seconds": 2.0,
}

TELEMETRY_URL = "/telemetry/events"
TAB_ID = "0b6f7c1e-3f5a-4c2d-9e8b-7a6d5c4b3a21"
OTHER_TAB_ID = "5d1c9a0e-8b7f-4e6d-a5c4-3b2a1f0e9d8c"


def _view(html: str) -> Any:
    return BeautifulSoup(html, "html.parser").select_one("#patient-view")


def _event(render_id: str, seq: int, kind: str = "browser.state", **overrides: Any) -> dict:
    payloads = {
        "browser.timepoint_enter": {"visible": True, "focused": True},
        "browser.state": {"visible": True, "focused": False, "reason": "blur"},
        "browser.activity": {"activity_kind": "click"},
        "browser.timepoint_exit": {"reason": "swap"},
        "browser.gap": {"dropped": 3},
        "panel.mount": {
            "panel_id": "vitals",
            "expanded": True,
            "collapsible": True,
            "state": "loading",
        },
        "panel.viewport": {"panel_id": "vitals", "intersection_ratio": 0.049},
        "panel.open": {"panel_id": "labs"},
        "panel.close": {"panel_id": "admission"},
    }
    event = {
        "kind": kind,
        "render_id": render_id,
        "client_seq": seq,
        "client_mono_ms": 100.0 * seq,
        "client_ts": "2026-09-27T10:00:00.000Z",
        "payload": payloads[kind],
    }
    event.update(overrides)
    return event


def _post(client: TestClient, events_: list[dict], tab_id: str = TAB_ID) -> Any:
    return client.post(TELEMETRY_URL, json={"tab_id": tab_id, "events": events_})
