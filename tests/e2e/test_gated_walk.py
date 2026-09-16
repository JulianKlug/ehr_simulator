"""Playwright walk through the S9b gate (spec §9 #44, #45).

Runs against ``live_study_server`` (synthetic study, 7-question fixture, 6
required). Each test logs in as its own clinician: progress is keyed per
(clinician, patient), so nothing leaks between tests on the shared server.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

PID = "synth_001"
HIGHLIGHT_MS = 1500  # mirrors advance.js
FREE_TEXT_ID = "free_notes"


def _login(page: Page, live_server: str, name: str) -> None:
    page.goto(f"{live_server}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server}/")


def _view_url(live_server: str, t_index: int) -> str:
    return f"{live_server}/patient/{PID}/timepoint/{t_index}?chrome=epic"


def _is_advance(res) -> bool:
    return "/advance" in res.url


def _answer_all_required(page: Page, t_index: int) -> None:
    """One valid answer per required question, each behind its /answer response."""
    answer_fragment = f"/patient/{PID}/timepoint/{t_index}/answer"
    for form in page.query_selector_all('form.question[data-required="true"]'):
        qid = form.get_attribute("data-question-id")
        rtype = form.get_attribute("data-response-type")
        base = f'form[data-question-id="{qid}"]'
        with page.expect_response(lambda res: answer_fragment in res.url):
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
def test_blocked_click_scrolls_and_focuses_first_unanswered(
    page: Page, live_study_server: str
) -> None:
    _login(page, live_study_server, "Dr. Blocked Click")
    page.goto(_view_url(live_study_server, 0))
    page.wait_for_selector('#advance-form[data-remaining="6"]')

    # force: Playwright treats aria-disabled as non-actionable; a real
    # pointer click lands fine (that is the whole point of aria-disabled).
    with page.expect_response(_is_advance) as blocked:
        page.click("#advance-btn", force=True)
    assert blocked.value.status == 409

    page.wait_for_selector('#advance-form[data-remaining="6"]')
    page.wait_for_selector("form.question.is-highlighted")
    focused_qid = page.evaluate(
        "document.activeElement.closest('form.question').dataset.questionId"
    )
    assert focused_qid == "deterioration_6h"
    assert (
        page.get_attribute("form.question.is-highlighted", "data-question-id") == "deterioration_6h"
    )
    page.wait_for_selector(
        "form.question.is-highlighted", state="detached", timeout=HIGHLIGHT_MS + 2000
    )
    assert page.url == _view_url(live_study_server, 0)

    # Answering flips the OOB CTA without touching focus (review-fix R3).
    radio = 'form[data-question-id="deterioration_6h"] input[value="No"]'
    with page.expect_response(lambda res: "/answer" in res.url):
        page.click(radio)
    page.wait_for_selector('#advance-form[data-remaining="5"]')
    assert page.evaluate("document.activeElement.value") == "No"


@pytest.mark.e2e
def test_gated_walk_to_completion(page: Page, live_study_server: str) -> None:
    _login(page, live_study_server, "Dr. Full Walk")
    page.goto(_view_url(live_study_server, 0))
    page.wait_for_selector("#questions-pane[data-mode='open']")

    # t=0: answer the six required, leave free_notes blank.
    _answer_all_required(page, 0)
    assert "Next timepoint" in page.inner_text("#advance-btn")
    assert page.get_attribute("#advance-btn", "aria-disabled") is None
    assert page.input_value(f'form[data-question-id="{FREE_TEXT_ID}"] textarea') == ""

    # ] at the frontier is the CTA; the URL follows the swap.
    with page.expect_response(_is_advance) as advanced:
        page.keyboard.press("]")
    assert advanced.value.status == 200
    page.wait_for_selector("#patient-view[data-t-index='1']")
    page.wait_for_url(_view_url(live_study_server, 1))

    # [ back: locked, read-only, resume link.
    page.keyboard.press("[")
    page.wait_for_selector("#patient-view[data-t-index='0']")
    page.wait_for_selector("#questions-pane[data-mode='locked']")
    assert len(page.query_selector_all("fieldset[disabled]")) == 7
    assert page.query_selector("#advance-form") is None
    assert page.is_visible(".pane-lock-note")
    page.click(".resume-link")
    page.wait_for_selector("#patient-view[data-t-index='1']")
    page.wait_for_selector("#questions-pane[data-mode='open']")

    # t=1 → t=2.
    _answer_all_required(page, 1)
    with page.expect_response(_is_advance):
        page.keyboard.press("]")
    page.wait_for_selector("#patient-view[data-t-index='2']")

    # t=2: the last timepoint finishes the patient — from the keyboard
    # (review-fix R5: the boundary guard must not swallow it).
    _answer_all_required(page, 2)
    assert "Finish patient" in page.inner_text("#advance-btn")
    with page.expect_response(_is_advance):
        page.keyboard.press("]")
    page.wait_for_url(f"{live_study_server}/")
    row = page.query_selector('.patient-list li[data-progress-state="complete"]')
    assert row is not None and PID in row.inner_text()

    # Read-only afterwards: everything viewable, nothing editable, no gate.
    page.goto(_view_url(live_study_server, 1))
    page.wait_for_selector("#questions-pane[data-mode='locked']")
    page.goto(_view_url(live_study_server, 2))
    page.wait_for_selector("#questions-pane[data-mode='locked']")
    assert "Patient complete" in page.inner_text(".pane-lock-note")

    # htmx history restore (review-fix R6): [ pushes t=1, back restores t=2
    # from the server and the document keeps its shell.
    page.keyboard.press("[")
    page.wait_for_url(_view_url(live_study_server, 1))
    page.go_back()
    page.wait_for_selector("#patient-view[data-t-index='2']")
    assert page.query_selector("#shortcut-overlay") is not None
    assert page.evaluate("Object.keys(localStorage).filter(k => k.startsWith('htmx')).length") == 0
