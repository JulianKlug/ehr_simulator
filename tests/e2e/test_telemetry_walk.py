"""S11j/S11k browser walks: static/telemetry.js really reports from a live page.

Runs against ``live_telemetry_server`` (Phase 2, ``telemetry`` pinned) and
reads what landed in its SQLite DB::

    Start case ─► #patient-view[data-render-id] ─► POST /telemetry/events
    advance    ─► exit(swap) of the old render, enter of the new one
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.support.browser import _login_and_start

TAB_ID_KEY = "ehrsim:tab-id"
UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
POLL_TIMEOUT_S = 12.0  # one 5 s flush interval plus slack
POLL_STEP_S = 0.2
TELEMETRY_GLOB = "**/telemetry/events"


def _wait_for_new_render(page: Page, old: str) -> None:
    # A function predicate, not a string: CSP forbids eval.
    page.wait_for_function(
        "(old) => document.querySelector('#patient-view').dataset.renderId !== old", arg=old
    )


def _render_id(page: Page) -> str:
    return page.get_attribute("#patient-view", "data-render-id") or ""


def _rows(db: Path, render_id: str) -> list[tuple[str, dict, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT kind, payload_json, tab_id FROM events "
            "WHERE render_id = ? AND tab_id IS NOT NULL AND kind NOT LIKE 'tab.%' "
            "ORDER BY client_mono_ms, client_seq",
            (render_id,),
        ).fetchall()
    finally:
        conn.close()
    return [(k, json.loads(p), t) for k, p, t in rows]


def _wait_for(db: Path, render_id: str, predicate) -> list[tuple[str, dict, str]]:
    deadline = time.monotonic() + POLL_TIMEOUT_S
    while True:
        rows = _rows(db, render_id)
        if predicate(rows):
            return rows
        if time.monotonic() > deadline:
            raise AssertionError(f"telemetry never arrived for {render_id}: {rows}")
        time.sleep(POLL_STEP_S)


def _kinds(rows: list[tuple[str, dict, str]]) -> list[str]:
    return [k for k, _p, _t in rows]


def _answer_all_required(page: Page) -> None:
    for form in page.query_selector_all('form.question[data-required="true"]'):
        qid = form.get_attribute("data-question-id")
        rtype = form.get_attribute("data-response-type")
        base = f'form[data-question-id="{qid}"]'
        with page.expect_response(lambda res: "/answer" in res.url):
            if rtype in {"categorical", "likert"}:
                page.click(f"{base} input[type=radio]")
            elif rtype == "multi-select":
                page.check(f"{base} input[type=checkbox]")
            elif rtype == "probability-0-100":
                page.fill(f"{base} input[type=number]", "50")
                page.keyboard.press("Tab")
            else:
                page.fill(f"{base} textarea", "x")
                page.keyboard.press("Tab")
        page.wait_for_selector(f'{base} .answer-status[data-state="saved"]')
    page.wait_for_selector('#advance-form[data-remaining="0"]')


@pytest.mark.e2e
def test_enter_snapshot_and_panel_mounts_arrive(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Enter")
    rows = _wait_for(telemetry_db, _render_id(page), lambda r: "panel.mount" in _kinds(r))

    kind, payload, tab_id = rows[0]
    assert kind == "browser.timepoint_enter"
    assert set(payload) == {"visible", "focused"}
    assert UUID_V4.fullmatch(tab_id)
    mounted = {p["panel_id"] for k, p, _ in rows if k == "panel.mount"}
    assert {"admission", "vitals", "labs", "imaging"} <= mounted


@pytest.mark.e2e
def test_tab_id_is_stable_across_swap_and_reload(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Tab")
    first = page.evaluate(f"sessionStorage.getItem('{TAB_ID_KEY}')")
    first_render = _render_id(page)

    _answer_all_required(page)
    page.click("#advance-btn")
    _wait_for_new_render(page, first_render)
    swapped = page.evaluate(f"sessionStorage.getItem('{TAB_ID_KEY}')")
    page.reload()
    page.wait_for_selector("#patient-view[data-render-id]")
    reloaded = page.evaluate(f"sessionStorage.getItem('{TAB_ID_KEY}')")

    assert UUID_V4.fullmatch(first)
    assert first == swapped == reloaded
    rows = _wait_for(telemetry_db, _render_id(page), lambda r: bool(r))
    assert {t for _k, _p, t in rows} == {first}


@pytest.mark.e2e
def test_swap_closes_old_render_before_new_enter(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Swap")
    old = _render_id(page)
    _answer_all_required(page)
    page.click("#advance-btn")
    _wait_for_new_render(page, old)
    new = _render_id(page)

    old_rows = _wait_for(telemetry_db, old, lambda r: "browser.timepoint_exit" in _kinds(r))
    new_rows = _wait_for(telemetry_db, new, lambda r: "browser.timepoint_enter" in _kinds(r))
    exits = [p for k, p, _ in old_rows if k == "browser.timepoint_exit"]
    assert exits == [{"reason": "swap"}]
    assert "browser.activity" in _kinds(old_rows)  # answering was activity
    assert _kinds(new_rows)[0] == "browser.timepoint_enter"


@pytest.mark.e2e
def test_blur_and_visibility_changes_are_reported(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Focus")
    render_id = _render_id(page)
    _wait_for(telemetry_db, render_id, lambda r: bool(r))

    # Headless windows neither blur nor hide: emulate the browser's signals.
    page.evaluate("""() => {
        document.hasFocus = () => false;
        window.dispatchEvent(new Event('blur'));
        Object.defineProperty(document, 'visibilityState', {value: 'hidden', configurable: true});
        document.dispatchEvent(new Event('visibilitychange'));
    }""")
    rows = _wait_for(telemetry_db, render_id, lambda r: _kinds(r).count("browser.state") >= 2)

    states = [p for k, p, _ in rows if k == "browser.state"]
    assert {"focused": False, "reason": "blur"}.items() <= states[0].items()
    assert states[1]["visible"] is False and states[1]["reason"] == "visibilitychange"


@pytest.mark.e2e
def test_user_tab_change_posts_close_and_open(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Tabs")
    render_id = _render_id(page)
    page.click('[role="tab"][data-tab="labs"]')

    rows = _wait_for(telemetry_db, render_id, lambda r: "panel.open" in _kinds(r))
    toggles = [(k, p["panel_id"]) for k, p, _ in rows if k in ("panel.open", "panel.close")]
    assert toggles == [("panel.close", "admission"), ("panel.open", "labs")]
    ratios = [p["intersection_ratio"] for k, p, _ in rows if k == "panel.viewport"]
    assert ratios and all(0 <= r <= 1 for r in ratios)


@pytest.mark.e2e
def test_dense_scroll_reports_viewport_ratio(
    page: Page, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Dense", chrome="dense")
    render_id = _render_id(page)
    page.set_viewport_size({"width": 1200, "height": 400})
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")

    rows = _wait_for(
        telemetry_db,
        render_id,
        lambda r: (
            any(k == "panel.viewport" and p["panel_id"] == "vitals" for k, p, _ in r)
            and "browser.activity" in _kinds(r)
        ),
    )
    mounts = [p for k, p, _ in rows if k == "panel.mount"]
    assert mounts and all(p["collapsible"] is False and p["expanded"] for p in mounts)
    assert "panel.open" not in _kinds(rows)


@pytest.mark.e2e
def test_workflow_continues_when_telemetry_fails(page: Page, live_telemetry_server: str) -> None:
    page.route(TELEMETRY_GLOB, lambda route: route.abort())
    _login_and_start(page, live_telemetry_server, "Dr. Telemetry Offline")
    old = _render_id(page)
    _answer_all_required(page)
    page.click("#advance-btn")

    _wait_for_new_render(page, old)
    assert page.get_attribute("#patient-view", "data-t-index") == "1"
