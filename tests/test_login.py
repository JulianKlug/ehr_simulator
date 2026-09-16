"""Login route tests (specs/session-06-sqlite-persistence.md §9 #22-#27).

Covers GET form render, POST normalize/create/cookie/redirect, logout,
protected-route 303, HTMX HX-Redirect, unknown-clinician cookie cache miss,
and the empty-name 400 path.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient


def test_get_login_renders_form(anonymous_client: TestClient) -> None:
    response = anonymous_client.get("/login")
    assert response.status_code == 200
    assert "<form" in response.text
    assert 'name="clinician_name"' in response.text


def test_post_login_creates_clinician_and_sets_cookie_and_redirects(
    anonymous_client: TestClient,
) -> None:
    response = anonymous_client.post(
        "/login",
        data={"clinician_name": "Dr. Smith"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    set_cookie_lower = response.headers["set-cookie"].lower()
    assert "ehrsim_clinician_id=" in set_cookie_lower
    assert "httponly" in set_cookie_lower
    assert "samesite=strict" in set_cookie_lower
    assert "path=/" in set_cookie_lower

    # Verify the row landed AND a clinician.login event with NULL session_id.
    from ehr_simulator.db import connect

    app = anonymous_client.app
    db = app.state.db
    rows = db.execute(
        "SELECT name_normalized FROM clinicians WHERE name_normalized = ?",
        ("dr. smith",),
    ).fetchall()
    assert len(rows) == 1
    login_events = db.execute(
        "SELECT session_id FROM events WHERE kind = 'clinician.login'"
    ).fetchall()
    assert len(login_events) == 1
    assert login_events[0][0] is None

    # Connect helper is exercised elsewhere; this is just a smoke import.
    _ = connect


def test_post_login_normalizes_name(anonymous_client: TestClient) -> None:
    r1 = anonymous_client.post(
        "/login", data={"clinician_name": "  DR. SMITH  "}, follow_redirects=False
    )
    r2 = anonymous_client.post(
        "/login", data={"clinician_name": "dr. smith"}, follow_redirects=False
    )
    assert r1.status_code == 303
    assert r2.status_code == 303
    # Same cookie value means same clinician_id.
    cookie1 = r1.headers["set-cookie"].split(";", 1)[0]
    cookie2 = r2.headers["set-cookie"].split(";", 1)[0]
    assert cookie1 == cookie2

    db = anonymous_client.app.state.db
    rows = db.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0]
    assert rows == 1


def test_post_login_empty_name_returns_400(anonymous_client: TestClient) -> None:
    for value in ("", "   "):
        response = anonymous_client.post(
            "/login",
            data={"clinician_name": value},
            follow_redirects=False,
        )
        assert response.status_code == 400
        assert "Name required." in response.text
    db = anonymous_client.app.state.db
    rows = db.execute("SELECT COUNT(*) FROM clinicians").fetchone()[0]
    assert rows == 0


def test_post_logout_clears_cookie_and_redirects(logged_in_client: TestClient) -> None:
    response = logged_in_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    set_cookie_lower = response.headers["set-cookie"].lower()
    assert "ehrsim_clinician_id=" in set_cookie_lower
    # delete_cookie expires the cookie by setting it to a past date; either
    # ``Max-Age=0`` or an Expires past timestamp is acceptable.
    assert "max-age=0" in set_cookie_lower or "expires=" in set_cookie_lower


def test_protected_route_redirects_to_login_when_no_cookie(
    anonymous_client: TestClient,
) -> None:
    for path in ("/", "/patient/synth_001/timepoint/0"):
        response = anonymous_client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_protected_route_htmx_uses_hx_redirect(anonymous_client: TestClient) -> None:
    """review-fix R10: HTMX requests get ``HX-Redirect`` header, not 303."""
    response = anonymous_client.get(
        "/patient/synth_001/timepoint/0",
        headers={"HX-Request": "true"},
        follow_redirects=False,
    )
    assert response.status_code == 200
    assert response.headers.get("HX-Redirect") == "/login"


def test_protected_route_unknown_clinician_id_redirects(
    anonymous_client: TestClient,
) -> None:
    """review-fix R11: a tampered 16-hex cookie that isn't in the cache fails."""
    anonymous_client.cookies.set("ehrsim_clinician_id", "ffffffffffffffff")
    response = anonymous_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_protected_route_succeeds_with_logged_in_client(
    logged_in_client: TestClient,
) -> None:
    response = logged_in_client.get("/", follow_redirects=False)
    assert response.status_code == 200
    # The chrome stripe carries the display name.
    assert "Logged in as" in response.text


def test_login_cookie_id_matches_sha256_truncation(anonymous_client: TestClient) -> None:
    """Lock the cookie's 16-hex value to the SHA256 truncation of the normalized name."""
    response = anonymous_client.post(
        "/login", data={"clinician_name": "Dr. Smith"}, follow_redirects=False
    )
    set_cookie = response.headers["set-cookie"]
    cookie_value = set_cookie.split(";", 1)[0].split("=", 1)[1]
    expected = hashlib.sha256(b"dr. smith").hexdigest()[:16]
    assert cookie_value == expected
