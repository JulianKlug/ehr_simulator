"""Vendored htmx integrity check (Decision D15).

The pin enforces the htmx version so silent file swaps fail CI. The constant
below is updated only when the version is intentionally upgraded; bump the
version string in :mod:`ehr_simulator.web.static.keyboard` (top comment) at
the same time so a future upgrade is greppable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

EXPECTED_HTMX_VERSION = "2.0.4"
EXPECTED_HTMX_SHA256 = "e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447"

_HTMX_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "ehr_simulator"
    / "web"
    / "static"
    / "htmx.min.js"
)


def test_htmx_min_js_sha256_matches_pin() -> None:
    assert _HTMX_PATH.exists(), f"vendored htmx not found at {_HTMX_PATH}"
    digest = hashlib.sha256(_HTMX_PATH.read_bytes()).hexdigest()
    assert digest == EXPECTED_HTMX_SHA256, (
        f"htmx.min.js sha256 changed (got {digest}, expected {EXPECTED_HTMX_SHA256}); "
        f"if intentional, bump EXPECTED_HTMX_VERSION + EXPECTED_HTMX_SHA256 in this file "
        "and the version comment in keyboard.js."
    )
