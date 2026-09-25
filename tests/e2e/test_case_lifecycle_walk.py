"""S11e browser walk: Start case → heartbeat fires → Pause → Resume.

Proves ``static/heartbeat.js`` really POSTs from a live page (CSP allows
it) and the pause/resume forms round-trip through the interstitial.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

HEARTBEAT_WAIT_MS = 20_000  # one 15 s interval plus slack
HTTP_NO_CONTENT = 204


@pytest.mark.e2e
def test_heartbeat_pause_and_resume(page: Page, live_lifecycle_server: str) -> None:
    page.goto(f"{live_lifecycle_server}/login")
    page.fill('input[name="clinician_name"]', "Dr. Lifecycle Walk")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_lifecycle_server}/")

    page.click("button.case-start")
    page.wait_for_selector("#case-heartbeat", state="attached")

    with page.expect_response(
        lambda r: r.url.endswith("/heartbeat"), timeout=HEARTBEAT_WAIT_MS
    ) as beat:
        pass
    assert beat.value.status == HTTP_NO_CONTENT

    page.click("button.case-pause-btn")
    page.wait_for_selector('[data-case-action="paused"]')
    assert page.locator("#questions-pane").count() == 0

    page.click("button.case-resume")
    page.wait_for_selector("#questions-pane")
    page.wait_for_selector("#case-heartbeat", state="attached")
