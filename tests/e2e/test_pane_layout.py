"""Questions pane layout (S9b feedback round 1).

1. Saving an answer must not move any other question: the badge reserves
   its final line box while blank.
2. The pane is a right-hand drawer at every viewport width, toggled by its
   edge tab or ``q``, remembered per tab across timepoint swaps and reloads.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

PID = "synth_001"
DRAWER_STORAGE_KEY = "ehrsim:pane-collapsed"


def _login(page: Page, live_server: str, name: str) -> None:
    page.goto(f"{live_server}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_server}/")


def _question_boxes(page: Page) -> list[dict]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('form.question')).map(f => {
            const r = f.getBoundingClientRect();
            return {q: f.dataset.questionId, top: Math.round(r.top), h: Math.round(r.height)};
        })"""
    )


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1000, 1400], ids=["narrow", "wide"])
def test_saving_an_answer_does_not_shift_other_questions(
    page: Page, live_study_server: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 800})
    _login(page, live_study_server, f"Dr Shift {width}")
    page.goto(f"{live_study_server}/patient/{PID}/timepoint/0?chrome=epic")
    page.wait_for_selector("#advance-form")

    before = _question_boxes(page)
    with page.expect_response(lambda res: "/answer" in res.url):
        page.click('form[data-question-id="deterioration_6h"] input[value="No"]')
    page.wait_for_selector(
        'form[data-question-id="deterioration_6h"] .answer-status[data-state="saved"]'
    )
    after = _question_boxes(page)

    assert after == before, [(b, a) for b, a in zip(before, after, strict=True) if a != b]


@pytest.mark.e2e
def test_pane_is_a_toggleable_right_drawer(page: Page, live_study_server: str) -> None:
    page.set_viewport_size({"width": 1000, "height": 800})
    _login(page, live_study_server, "Dr Drawer")
    page.goto(f"{live_study_server}/patient/{PID}/timepoint/0?chrome=epic")
    page.wait_for_selector("#questions-pane")

    def pane_right_edge() -> float:
        return page.evaluate(
            "document.querySelector('#questions-pane').getBoundingClientRect().right"
        )

    def pane_left_edge() -> float:
        return page.evaluate(
            "document.querySelector('#questions-pane').getBoundingClientRect().left"
        )

    # Open by default, flush with the right viewport edge, chrome to its left.
    assert abs(pane_right_edge() - 1000) < 2
    chrome_right = page.evaluate(
        "document.querySelector('.patient-chrome').getBoundingClientRect().right"
    )
    assert chrome_right <= pane_left_edge() + 1
    tab = page.locator(".pane-tab")
    assert tab.get_attribute("aria-expanded") == "true"
    assert tab.get_attribute("aria-controls") == "questions-pane"
    assert "6" in tab.inner_text()

    # Collapse via the edge tab: pane leaves the viewport, tab stays visible.
    tab.click()
    page.wait_for_selector("#patient-view.pane-collapsed")
    page.wait_for_timeout(400)  # transition
    assert pane_left_edge() >= 1000 - 1
    assert tab.is_visible()
    assert tab.get_attribute("aria-expanded") == "false"
    assert page.get_attribute("#questions-pane", "aria-hidden") == "true"

    # Collapsed state survives a timepoint swap... (t=0 is the frontier, so
    # ] hits the CTA and is blocked; use the patient jumper for a real swap)
    page.click('.patient-link[href*="synth_002"]')
    page.wait_for_selector("#patient-view[data-patient-id='synth_002']")
    page.wait_for_selector("#patient-view.pane-collapsed")
    # ... and a reload.
    page.reload()
    page.wait_for_selector("#patient-view.pane-collapsed")
    assert page.evaluate(f"sessionStorage.getItem('{DRAWER_STORAGE_KEY}')") == "1"

    # q toggles it back open; the tab count tracks the OOB CTA after a save.
    page.keyboard.press("q")
    page.wait_for_selector("#patient-view:not(.pane-collapsed)")
    page.wait_for_timeout(400)
    assert abs(pane_right_edge() - 1000) < 2
    with page.expect_response(lambda res: "/answer" in res.url):
        page.click('form[data-question-id="deterioration_6h"] input[value="No"]')
    page.wait_for_selector('#advance-form[data-remaining="5"]')
    assert "5" in tab.inner_text()

    # q inside a text field must still type, not toggle.
    page.click('form[data-question-id="free_notes"] textarea')
    page.keyboard.type("q")
    assert page.input_value('form[data-question-id="free_notes"] textarea') == "q"
    assert page.query_selector("#patient-view.pane-collapsed") is None
