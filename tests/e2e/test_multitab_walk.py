"""S11m browser walk: a second tab of the same case never becomes a writer.

Two pages in one browser context share the login cookie but not
``sessionStorage``, so they are two tabs with two tab ids::

    page A ─► Start case ─► claim granted ─► answers save
    page B ─► same URL   ─► claim refused ─► notice, answers cancelled
    page A closes ─► release ─► page B Retry ─► granted
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.e2e.test_telemetry_walk import _login_and_start

GRANTED = '#patient-view[data-tab-state="granted"]'
REFUSED = '#patient-view[data-tab-state="refused"]'
NOTICE = ".tab-conflict"
NO_WRITE_WAIT_MS = 1500


def _count(db: Path, sql: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def _first_radio(page: Page) -> str:
    form = page.query_selector('form.question[data-response-type="categorical"]')
    assert form is not None
    return f'form[data-question-id="{form.get_attribute("data-question-id")}"] input[type=radio]'


@pytest.mark.e2e
def test_second_tab_is_blocked_and_audited(
    page: Page, context: BrowserContext, live_telemetry_server: str, telemetry_db: Path
) -> None:
    _login_and_start(page, live_telemetry_server, "Dr. Two Tabs")
    page.wait_for_selector(GRANTED)

    second = context.new_page()
    second.goto(page.url)
    second.wait_for_selector(REFUSED)
    assert second.is_visible(NOTICE)

    answers_before = _count(telemetry_db, "SELECT COUNT(*) FROM answers")
    second.click(_first_radio(second), force=True)
    second.wait_for_timeout(NO_WRITE_WAIT_MS)
    assert _count(telemetry_db, "SELECT COUNT(*) FROM answers") == answers_before

    with page.expect_response(lambda res: "/answer" in res.url):
        page.click(_first_radio(page))
    assert _count(telemetry_db, "SELECT COUNT(*) FROM answers") == answers_before + 1
    assert _count(telemetry_db, "SELECT COUNT(*) FROM events WHERE kind = 'tab.conflict'") >= 1

    page.close()  # pagehide releases the lease
    second.click(f"{NOTICE} button")
    second.wait_for_selector(GRANTED)
    assert not second.is_visible(NOTICE)
