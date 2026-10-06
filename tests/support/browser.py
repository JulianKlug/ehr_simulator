"""Playwright helpers shared by the e2e walks."""

from __future__ import annotations

from playwright.sync_api import Page


def _login_and_start(page: Page, base_url: str, name: str, chrome: str = "epic") -> str:
    page.goto(f"{base_url}/login")
    page.fill('input[name="clinician_name"]', name)
    page.click('button[type="submit"]')
    page.wait_for_url(f"{base_url}/")
    page.click("button.case-start")
    page.wait_for_selector("#patient-view[data-render-id]")
    if chrome != "epic":
        page.goto(page.url.replace("chrome=epic", f"chrome={chrome}"))
        page.wait_for_selector("#patient-view[data-render-id]")
    return page.url.split("/patient/")[1].split("/")[0]
