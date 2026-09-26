"""S11e/S11f browser walks: Start case → heartbeat fires → Pause → Resume;
operator abandon while open; timeout → replacement.

Proves ``static/heartbeat.js`` really POSTs from a live page (CSP allows
it), follows a 409 back to the index, and the pause/resume forms
round-trip through the interstitial.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

HEARTBEAT_WAIT_MS = 20_000  # one 15 s interval plus slack
HTTP_NO_CONTENT = 204
HTTP_CONFLICT = 409
PAST_GRACE_SECONDS = 301  # study_lifecycle_replacement.yaml grace (300) + 1


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


def _login_and_start(page: Page, base_url: str, name: str) -> str:
    """Log in, press Start case; return the activated patient id."""
    page.goto(f"{base_url}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/")

    page.click("button.case-start")
    page.wait_for_selector("#case-heartbeat", state="attached")
    return page.url.split("/patient/")[1].split("/")[0]


def _marker(page: Page, patient_id: str) -> str:
    row = page.locator("ul.patient-list li", has=page.locator(f"strong:text-is('{patient_id}')"))
    return row.locator(".progress-marker").inner_text().strip()


@pytest.mark.e2e
def test_abandon_while_open_sends_page_to_index(
    page: Page, live_lifecycle_server: str, lifecycle_cli
) -> None:
    """S11e: an operator abandon closes the open page on its next heartbeat."""
    name = "Dr. Abandon Walk"
    pid = _login_and_start(page, live_lifecycle_server, name)

    lifecycle_cli("abandon-case", "--clinician", name, "--patient", pid)

    # heartbeat.js follows the 409's HX-Redirect back to the index.
    with page.expect_response(
        lambda r: r.url.endswith("/heartbeat"), timeout=HEARTBEAT_WAIT_MS
    ) as beat:
        pass
    assert beat.value.status == HTTP_CONFLICT
    page.wait_for_url(f"{live_lifecycle_server}/")
    assert _marker(page, pid).startswith("incomplete")


@pytest.mark.e2e
def test_timeout_then_replacement_visible_on_index(page: Page, live_clock_server) -> None:
    """S11f: a lazily timed-out case gets a replacement Start case activates."""
    base_url = live_clock_server.base_url
    original = _login_and_start(page, base_url, "Dr. Replacement Walk")
    case_url = page.url

    # Jump past the grace; the next contact (this GET) times the case out.
    live_clock_server.clock.advance(PAST_GRACE_SECONDS)
    page.goto(case_url)
    page.wait_for_url(f"{base_url}/")
    assert _marker(page, original) == "incomplete · replacement pending"

    page.click("button.case-start")
    page.wait_for_selector("#questions-pane")
    replacement = page.url.split("/patient/")[1].split("/")[0]
    assert replacement != original

    page.goto(f"{base_url}/")
    assert _marker(page, original) == "incomplete · replaced"
    assert _marker(page, replacement).startswith("in progress")
