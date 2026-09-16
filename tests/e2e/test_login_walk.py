"""Playwright walk through the S6 login flow (specs §9 test #34).

Asserts GET ``/login`` renders the form, POST sets the cookie, the index
page lists synthetic patients, the chrome stripe shows the display name,
and clicking a patient lands on ``/patient/<pid>/timepoint/0``.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page


@pytest.mark.e2e
def test_login_to_patient_walk(page: Page, live_server: str) -> None:
    # GET /login renders the form.
    page.goto(f"{live_server}/login")
    page.wait_for_selector('input[name="clinician_name"]')

    # POST /login lands on / via 303.
    page.fill('input[name="clinician_name"]', "Dr. Login Walk")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server}/")

    # Index shows synthetic patients + the logged-in stripe with the
    # normalized display name ("dr. login walk").
    body_text = page.locator("body").inner_text().lower()
    assert "logged in as" in body_text
    assert "dr. login walk" in body_text
    assert "synth_001" in body_text

    # Click the first patient's epic-chrome link.
    page.click('a[href="/patient/synth_001/timepoint/0?chrome=epic"]')
    page.wait_for_selector("#patient-view[data-t-index='0']")
