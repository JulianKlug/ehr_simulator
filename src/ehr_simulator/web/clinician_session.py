"""Clinician login/logout + display name: the service between the auth
routes and the ``clinicians`` / ``events`` DAOs."""

from __future__ import annotations

import sqlite3
from typing import Any

from ehr_simulator.db import clinicians, events
from ehr_simulator.web import tab_guard

_LOGIN_KIND = "clinician.login"
_LOGOUT_KIND = "clinician.logout"


def log_in(conn: sqlite3.Connection, app_state: Any, raw_name: str) -> str:
    """Resolve (or create) the clinician, cache it, record the login."""
    clinician_id = clinicians.lookup_or_create(
        conn, raw_name, known_clinicians=app_state.known_clinicians
    )
    events.append(
        conn,
        session_id=None,
        clinician_id=clinician_id,
        patient_id=None,
        timepoint=None,
        kind=_LOGIN_KIND,
        payload={},  # S11m: the row's clinician_id is the only identity
        app_state=app_state,
    )
    return clinician_id


def log_out(conn: sqlite3.Connection, app_state: Any, clinician_id: str) -> None:
    """Release every tab lease of the clinician, then record the logout."""
    tab_guard.release_all(conn, app_state, clinician_id=clinician_id)
    events.append(
        conn,
        session_id=None,
        clinician_id=clinician_id,
        patient_id=None,
        timepoint=None,
        kind=_LOGOUT_KIND,
        payload={},
        app_state=app_state,
    )


def display_name(conn: sqlite3.Connection, clinician_id: str) -> str | None:
    """The case-folded name shown in the chrome stripe."""
    return clinicians.fetch_name(conn, clinician_id)
