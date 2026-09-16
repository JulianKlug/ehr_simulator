"""Playwright walk through the S9a questions pane (spec §9 #36, #36b, #36c).

Runs against ``live_study_server`` (synthetic study + 7-question fixture).
Each test logs in as a different clinician so pre-fill from one test never
leaks into another (the server and its DB are session-scoped).
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

PATIENT_URL = "/patient/synth_001/timepoint/0?chrome=epic"
ANSWER_URL_FRAGMENT = "/patient/synth_001/timepoint/0/answer"
FREE_TEXT_AUTOSAVE_WAIT_MS = 4000


def _login(page: Page, live_server: str, name: str) -> None:
    page.goto(f"{live_server}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server}/")


def _badge(question_id: str, state: str) -> str:
    return f'form[data-question-id="{question_id}"] .answer-status[data-state="{state}"]'


@pytest.mark.e2e
def test_answer_autosave_and_prefill(page: Page, live_study_server: str) -> None:
    _login(page, live_study_server, "Dr. Answer Walk")
    page.goto(f"{live_study_server}{PATIENT_URL}")
    page.wait_for_selector("#patient-view[data-t-index='0']")
    page.wait_for_selector("#questions-pane")

    radio = 'form[data-question-id="deterioration_6h"] input[value="No"]'
    with page.expect_request(lambda req: ANSWER_URL_FRAGMENT in req.url):
        page.click(radio)
    page.wait_for_selector(_badge("deterioration_6h", "saved"))

    # Review-fix R19: the radio still has focus; ] must still navigate.
    page.keyboard.press("]")
    page.wait_for_selector("#patient-view[data-t-index='1']")
    page.keyboard.press("[")
    page.wait_for_selector("#patient-view[data-t-index='0']")
    assert page.is_checked(radio)

    textarea = 'form[data-question-id="free_notes"] textarea'
    page.fill(textarea, "typed in e2e")
    with page.expect_request(lambda req: ANSWER_URL_FRAGMENT in req.url):
        page.keyboard.press("Tab")
    page.wait_for_selector(_badge("free_notes", "saved"))

    page.reload()
    page.wait_for_selector("#questions-pane")
    assert page.is_checked(radio)
    assert page.input_value(textarea) == "typed in e2e"
    page.wait_for_selector(_badge("deterioration_6h", "saved"))


@pytest.mark.e2e
def test_free_text_autosaves_without_blur(page: Page, live_study_server: str) -> None:
    _login(page, live_study_server, "Dr. No Blur")
    page.goto(f"{live_study_server}{PATIENT_URL}")
    page.wait_for_selector("#questions-pane")

    textarea = 'form[data-question-id="free_notes"] textarea'
    page.click(textarea)
    page.keyboard.type("saved while still focused")
    # No blur, no Tab: the debounced `input` trigger must fire on its own.
    page.wait_for_selector(_badge("free_notes", "saved"), timeout=FREE_TEXT_AUTOSAVE_WAIT_MS)
    assert page.evaluate("document.activeElement.tagName") == "TEXTAREA"


@pytest.mark.e2e
def test_enter_in_probability_field_does_not_reload(page: Page, live_study_server: str) -> None:
    _login(page, live_study_server, "Dr. Enter Key")
    page.goto(f"{live_study_server}{PATIENT_URL}")
    page.wait_for_selector("#questions-pane")
    url_before = page.url

    number = 'form[data-question-id="good_outcome_3mo"] input[type="number"]'
    page.click(number)
    page.keyboard.type("65")
    with page.expect_request(lambda req: ANSWER_URL_FRAGMENT in req.url) as req_info:
        page.keyboard.press("Enter")
    assert req_info.value.method == "POST"

    page.wait_for_selector(_badge("good_outcome_3mo", "saved"))
    assert page.url == url_before
    assert page.query_selector("#questions-pane") is not None
    assert page.input_value(number) == "65"
