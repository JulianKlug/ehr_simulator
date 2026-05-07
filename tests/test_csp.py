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
