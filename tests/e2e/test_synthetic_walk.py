"""End-to-end: walk synth_001 via [/] keyboard shortcuts (Decision D16).

``page.expect_request(...)`` is the synchronization barrier — without it the
assertion races the network. Boundary presses must NOT trigger a network
request and must surface the flash message in ``#summary-flash``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError


@pytest.mark.e2e
def test_e2e_walk_synth_001_via_keyboard(page: Page, live_server: str) -> None:
    page.goto(f"{live_server}/patient/synth_001/timepoint/0?chrome=dense")
    page.wait_for_selector("#patient-view[data-t-index='0']")

    # ] → t_index=1
    with page.expect_request(
        lambda req: "/patient/synth_001/timepoint/1" in req.url,
    ):
        page.keyboard.press("]")
    page.wait_for_selector("#patient-view[data-t-minutes='60.0']")

    # ] → t_index=2
    with page.expect_request(
        lambda req: "/patient/synth_001/timepoint/2" in req.url,
    ):
        page.keyboard.press("]")
    page.wait_for_selector("#patient-view[data-t-minutes='180.0']")

    # ] past the last → no network request, flash message shown
    with (
        pytest.raises(PlaywrightTimeoutError),
        page.expect_request(
            lambda req: "/patient/synth_001/timepoint/3" in req.url,
            timeout=1000,
        ),
    ):
        page.keyboard.press("]")
    flash_text = page.locator("#summary-flash").inner_text()
    assert "last timepoint" in flash_text.lower()

    # [ twice → back to t_index=0
    with page.expect_request(
        lambda req: "/patient/synth_001/timepoint/1" in req.url,
    ):
        page.keyboard.press("[")
    page.wait_for_selector("#patient-view[data-t-minutes='60.0']")

    with page.expect_request(
        lambda req: "/patient/synth_001/timepoint/0" in req.url,
    ):
        page.keyboard.press("[")
    page.wait_for_selector("#patient-view[data-t-minutes='0.0']")

    # [ past the first → no request, flash
    with (
        pytest.raises(PlaywrightTimeoutError),
        page.expect_request(
            lambda req: "/patient/synth_001/timepoint/-1" in req.url,
            timeout=1000,
        ),
    ):
        page.keyboard.press("[")
    flash_text = page.locator("#summary-flash").inner_text()
    assert "first timepoint" in flash_text.lower()
