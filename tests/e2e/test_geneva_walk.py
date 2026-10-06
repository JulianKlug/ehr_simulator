"""S8 item 39: walk a Geneva shaped patient end to end, every epic tab."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from tests.support.geneva_ui import AI_TIMES, PATIENT

TABS = ("admission", "vitals", "neuro_support", "labs", "imaging", "ai")


@pytest.mark.e2e
def test_geneva_patient_walks_first_to_last_without_render_error(
    page: Page, live_geneva_server: str
) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: msg.type == "error" and errors.append(msg.text))

    page.goto(f"{live_geneva_server}/login")
    page.fill('input[name="clinician_name"]', "Dr. Geneva")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_geneva_server}/")

    page.goto(f"{live_geneva_server}/patient/{PATIENT}/timepoint/0?chrome=epic")
    for t_index, t_minutes in enumerate(AI_TIMES):
        page.wait_for_selector(f"#patient-view[data-t-minutes='{t_minutes}']")
        for tab in TABS:
            page.click(f"button[data-tab='{tab}']")
            panel = page.locator(f"#tab-{tab} section[data-panel='{tab}']")
            assert panel.is_visible(), (t_index, tab)
            assert panel.get_attribute("data-state") != "error", (t_index, tab)
        if t_index < len(AI_TIMES) - 1:
            with page.expect_request(lambda req, i=t_index: f"/timepoint/{i + 1}" in req.url):
                page.keyboard.press("]")

    assert errors == []
