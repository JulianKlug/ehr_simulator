"""S11h Playwright walk: server-rendered branch swaps in a real browser.

Runs against ``live_branching_server`` (first use case questions): the
answer response swaps dependent slots out of band; no condition logic runs
in the browser.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

PATIENT_URL = "/patient/synth_001/timepoint/0?chrome=epic"
ANSWER_URL_FRAGMENT = "/patient/synth_001/timepoint/0/answer"


def _login(page: Page, base_url: str, name: str) -> None:
    page.goto(f"{base_url}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/")


def _choose(page: Page, question_id: str, value: str) -> None:
    radio = f'form[data-question-id="{question_id}"] input[value="{value}"]'
    with page.expect_response(lambda res: ANSWER_URL_FRAGMENT in res.url):
        page.click(radio)


@pytest.mark.e2e
def test_branches_swap_without_reload(page: Page, live_branching_server: str) -> None:
    _login(page, live_branching_server, "Dr. Branch Walk")
    page.goto(f"{live_branching_server}{PATIENT_URL}")
    page.wait_for_selector("#questions-pane")
    assert page.locator('form[data-question-id="primary_cause"]').count() == 0

    _choose(page, "deterioration_6h", "Yes")
    page.wait_for_selector('#q-slot-primary_cause[data-q-state="editable"] form')
    _choose(page, "primary_cause", "Other")

    _choose(page, "deterioration_6h", "No")
    page.wait_for_selector('#q-slot-primary_cause[data-q-state="hidden"]', state="attached")

    _choose(page, "good_outcome_3mo", "Yes")
    page.wait_for_selector('#q-slot-death_3mo[data-q-state="derived"]')
    assert page.is_checked('#q-slot-death_3mo input[value="No"]')
    assert page.is_disabled('#q-slot-death_3mo input[value="Yes"]')

    page.reload()
    page.wait_for_selector('#q-slot-death_3mo[data-q-state="derived"]')
    page.wait_for_selector('#q-slot-primary_cause[data-q-state="hidden"]', state="attached")
