"""Content-Security-Policy header tests.

One parametrized test, three route shapes (root / htmx_swap / error_404)
per /plan-eng-review tension D — coverage identical to three discrete tests
but case count stays grep-friendly.

Asserts the exact locked header value AND that ``script-src`` does not
contain ``'unsafe-inline'`` (per /plan-eng-review issue 2.1).
"""

from __future__ import annotations

import pytest

from ehr_simulator.web.middleware import _CSP_HEADER_VALUE


@pytest.mark.parametrize(
    ("path", "headers", "expected_status"),
    [
        pytest.param("/", {}, 200, id="root"),
        pytest.param(
            "/patient/synth_001/timepoint/0",
            {"HX-Request": "true"},
            200,
            id="htmx_swap",
        ),
        pytest.param("/no-such-route", {}, 404, id="error_404"),
    ],
)
def test_csp_header_present(
    client, path: str, headers: dict[str, str], expected_status: int
) -> None:
    response = client.get(path, headers=headers)
    assert response.status_code == expected_status
    assert response.headers["content-security-policy"] == _CSP_HEADER_VALUE
    # Per /plan-eng-review issue 2.1: script-src must not allow inline.
    script_directive = next(
        part.strip()
        for part in _CSP_HEADER_VALUE.split(";")
        if part.strip().startswith("script-src")
    )
    assert "'unsafe-inline'" not in script_directive


def test_questions_pane_is_csp_clean_and_answers_js_served(study_client) -> None:
    """S9a: the pane adds no inline script / on*= handlers; answers.js is a
    same-origin file, so the locked CSP needs no change."""
    from bs4 import BeautifulSoup

    page = study_client.get("/patient/synth_001/timepoint/0")
    assert page.status_code == 200
    soup = BeautifulSoup(page.text, "html.parser")
    pane = soup.select_one("#questions-pane")
    assert pane is not None
    assert not pane.select("script")
    for el in pane.find_all(True):
        assert not [a for a in el.attrs if a.lower().startswith("on")], el
    assert soup.select_one('script[src="/static/answers.js"]') is not None

    js = study_client.get("/static/answers.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "htmx:configRequest" in js.text


def test_advance_js_served_and_gated_fragments_csp_clean(study_client) -> None:
    """S9b: open pane, locked pane, 409 CTA and 412 view add no inline script
    or on*= handlers; advance.js is same-origin; htmx history caching is off."""
    from bs4 import BeautifulSoup

    from tests.conftest import answer_all_required

    def assert_clean(html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        assert not soup.select("script:not([src])")
        for el in soup.find_all(True):
            assert not [a for a in el.attrs if a.lower().startswith("on")], el

    hx = {"HX-Request": "true"}
    page = study_client.get("/patient/synth_001/timepoint/0")
    assert 'hx-history="false"' in page.text
    assert BeautifulSoup(page.text, "html.parser").select_one('script[src="/static/advance.js"]')
    assert_clean(page.text)

    blocked = study_client.post("/patient/synth_001/timepoint/0/advance", headers=hx)
    assert blocked.status_code == 409
    assert_clean(blocked.text)

    answer_all_required(study_client, "synth_001", 0)
    assert (
        study_client.post("/patient/synth_001/timepoint/0/advance", headers=hx).status_code == 200
    )
    stale = study_client.post("/patient/synth_001/timepoint/0/advance", headers=hx)
    assert stale.status_code == 412
    assert_clean(stale.text)

    locked = study_client.get("/patient/synth_001/timepoint/0", headers=hx)
    assert 'data-mode="locked"' in locked.text
    assert_clean(locked.text)

    js = study_client.get("/static/advance.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
    assert "htmx:beforeSwap" in js.text and "htmx:afterSwap" in js.text
    # review #10: one shared per-tab counter, loaded before both consumers.
    assert BeautifulSoup(page.text, "html.parser").select_one('script[src="/static/client_seq.js"]')
    shared = study_client.get("/static/client_seq.js")
    assert shared.status_code == 200 and "nextClientSeq" in shared.text
    assert "nextClientSeq" in js.text
    assert "nextClientSeq" in study_client.get("/static/answers.js").text
    # feedback R1/F2: drawer toggle lives in pane.js, no inline handler on the tab.
    pane_js = study_client.get("/static/pane.js")
    assert pane_js.status_code == 200 and "toggle-pane" in pane_js.text
    assert BeautifulSoup(page.text, "html.parser").select_one('script[src="/static/pane.js"]')
