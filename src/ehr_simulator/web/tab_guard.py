"""S11m: one browser tab owns an active guarded measured case.

Spec: ``specs/session-11m-privacy-multitab-backups.md``.

A render of an active measured Phase 2 case whose pinned configuration
carries ``telemetry`` is *guarded* (``timepoint.render`` payload
``tab_guard: true``). Its tab must hold the case lease before it may save,
advance, pause, heartbeat or report telemetry::

    tab A  claim(R1) ──► lease (A, R1) ──► writes carry (A, R1) ✓
    tab B  claim(R2) ──► lease is live and A's ──► 409 + tab.conflict
    A gone (no heartbeat for TTL) ──► B claims ──► tab.lease_expired + tab.claimed

A lease is void when its session is no longer the case's open session
(pause, resume, completion) or the case is not active; a void or stale
lease counts as absent. Every decision runs under ``BEGIN IMMEDIATE`` and
commits its ``tab.*`` audit rows with it. ``app.state.clock`` is the only
time source.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import events, sessions, tab_leases, telemetry
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.db.tab_leases import TabLease
from ehr_simulator.db.telemetry import RenderRow
from ehr_simulator.web.case_contact import (
    HEARTBEAT_INTERVAL_SECONDS,
    CaseAccess,
    ContactResult,
    now,
)
from ehr_simulator.web.study_session import CaseConfiguration

__all__ = [
    "CONFLICT_HEADER",
    "CONFLICT_VALUE",
    "GUARD_PAYLOAD_KEY",
    "RENDER_ID_FIELD",
    "RENDER_ID_HEADER",
    "TAB_ID_FIELD",
    "TAB_ID_HEADER",
    "TAB_LEASE_TTL_SECONDS",
    "ClaimReason",
    "ReleaseReason",
    "TabClaimRefusedError",
    "TabConflictError",
    "TabGuardError",
    "claim",
    "clear_leases",
    "is_guarded",
    "move_to_render",
    "owner_identity",
    "release",
    "release_all",
    "require_owner",
]

#: Three missed heartbeats: the owner tab is gone (platform constant, never
#: study configuration, never in ``config_hash``).
TAB_LEASE_TTL_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS

#: Owner identity on a guarded write (htmx/fetch headers, plain form fields).
TAB_ID_HEADER = "X-Ehrsim-Tab-Id"
RENDER_ID_HEADER = "X-Ehrsim-Render-Id"
TAB_ID_FIELD = "ehrsim_tab_id"
RENDER_ID_FIELD = "ehrsim_render_id"

#: Marks a refusal the client guard must show as a tab conflict.
CONFLICT_HEADER = "X-Ehrsim-Tab"
CONFLICT_VALUE = "conflict"

#: ``timepoint.render`` payload key of a guarded render.
GUARD_PAYLOAD_KEY = "tab_guard"

_CLAIMED = "tab.claimed"
_RELEASED = "tab.released"
_CONFLICT = "tab.conflict"
_EXPIRED = "tab.lease_expired"
_LIVE_OTHER_TAB = "live_other_tab"
_TTL = "ttl"


class ClaimReason(StrEnum):
    INITIAL = "initial"  # no lease, or a void one
    RECLAIM = "reclaim"  # replaced a stale lease of another tab
    NAVIGATE = "navigate"  # the owner tab moved to a new render


class ReleaseReason(StrEnum):
    PAGEHIDE = "pagehide"
    LOGOUT = "logout"


class TabGuardError(Exception):
    """A guarded request without the lease (HTTP 409, nothing mutated)."""


class TabClaimRefusedError(TabGuardError):
    """The render cannot hold the lease: unknown, unguarded, stale session
    or a case that is not active."""


class TabConflictError(TabGuardError):
    """Another live tab owns the case (or the identity is missing)."""


def is_guarded(case: CaseConfiguration | None, contact: ContactResult | None) -> bool:
    """Active measured Phase 2 case pinned to ``telemetry`` (tab ids exist)."""
    return (
        case is not None
        and contact is not None
        and contact.access is CaseAccess.ACTIVE
        and case.observation_mode is ObservationMode.MEASURED
        and case.study.telemetry is not None
    )


def owner_identity(
    headers: Mapping[str, str], form: Mapping[str, Any] | None = None
) -> tuple[str | None, str | None]:
    """``(tab_id, render_id)`` from the headers, else the form fields."""
    tab_id = headers.get(TAB_ID_HEADER)
    render_id = headers.get(RENDER_ID_HEADER)
    if form is not None:
        tab_id = tab_id or _form_str(form.get(TAB_ID_FIELD))
        render_id = render_id or _form_str(form.get(RENDER_ID_FIELD))
    return tab_id or None, render_id or None


def _form_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


# ---------------------------------------------------------------------------
# Internals (callers hold the write lock)
# ---------------------------------------------------------------------------


def _bump(app_state: Any) -> None:
    app_state.write_counter = getattr(app_state, "write_counter", 0) + 1


def _audit(
    conn: sqlite3.Connection, kind: str, render: RenderRow, tab_id: str, reason: str
) -> None:
    events.append(
        conn,
        session_id=render.session_id,
        clinician_id=render.clinician_id,
        patient_id=render.patient_id,
        timepoint=render.timepoint,
        kind=kind,  # type: ignore[arg-type]
        payload={"reason": reason},
        render_id=render.render_id,
        tab_id=tab_id,
        commit=False,
    )


def _lease_render(conn: sqlite3.Connection, lease: TabLease) -> RenderRow:
    """The render a lease names (a minimal stand-in if it is gone)."""
    found = telemetry.fetch_renders(conn, [lease.render_id]).get(lease.render_id)
    if found is not None:
        return found
    return RenderRow(
        event_id=0,
        render_id=lease.render_id,
        session_id=lease.session_id,
        clinician_id=lease.clinician_id,
        patient_id=lease.patient_id,
        timepoint=None,
        payload={},
    )


def _open_session(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> str | None:
    """The case's open session while it is active, else ``None``."""
    lifecycle = lifecycle_dao.fetch(conn, clinician_id, patient_id)
    if lifecycle is None or lifecycle.state is not CaseState.ACTIVE:
        return None
    return sessions.find_open(conn, clinician_id, patient_id)


def _is_live(lease: TabLease | None, open_session: str | None, moment: datetime) -> bool:
    if lease is None or open_session is None or lease.session_id != open_session:
        return False  # absent or void
    return (moment - lease.last_seen_at).total_seconds() <= TAB_LEASE_TTL_SECONDS


def _claimable_render(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str, render_id: str
) -> RenderRow:
    """A guarded render of this case in its open session, or refuse."""
    render = telemetry.fetch_renders(conn, [render_id]).get(render_id)
    if (
        render is None
        or render.clinician_id != clinician_id
        or render.patient_id != patient_id
        or not render.payload.get(GUARD_PAYLOAD_KEY)
    ):
        raise TabClaimRefusedError("this view is not a guarded view of the case")

    open_session = _open_session(conn, clinician_id, patient_id)
    if open_session is None:
        raise TabClaimRefusedError("the case is not active")
    if render.session_id != open_session:
        raise TabClaimRefusedError("this view belongs to a closed session; reload the case")
    return render


def _acquire(conn: sqlite3.Connection, moment: datetime, render: RenderRow, tab_id: str) -> bool:
    """Grant ``render`` to ``tab_id`` unless another live tab holds it.

    Returns ``False`` after recording ``tab.conflict`` (the caller commits
    the audit row, then refuses).
    """
    lease = tab_leases.fetch(conn, render.clinician_id, render.patient_id)
    open_session = render.session_id
    if _is_live(lease, open_session, moment):
        assert lease is not None
        if lease.tab_id != tab_id:
            _audit(conn, _CONFLICT, render, tab_id, _LIVE_OTHER_TAB)
            return False

        moved = lease.render_id != render.render_id
        tab_leases.upsert(conn, replace(lease, render_id=render.render_id, last_seen_at=moment))
        if moved:
            _audit(conn, _CLAIMED, render, tab_id, ClaimReason.NAVIGATE)
        return True

    reason = ClaimReason.INITIAL
    if lease is not None and lease.session_id == open_session:
        # Same session, silent past the TTL: the old tab is gone.
        _audit(conn, _EXPIRED, _lease_render(conn, lease), lease.tab_id, _TTL)
        reason = ClaimReason.RECLAIM
    tab_leases.upsert(
        conn,
        TabLease(
            clinician_id=render.clinician_id,
            patient_id=render.patient_id,
            session_id=render.session_id or "",
            tab_id=tab_id,
            render_id=render.render_id,
            claimed_at=moment,
            last_seen_at=moment,
        ),
    )
    _audit(conn, _CLAIMED, render, tab_id, reason)
    return True


def _locked(conn: sqlite3.Connection, app_state: Any, decide: Any) -> Any:
    """Run ``decide()`` under ``BEGIN IMMEDIATE``; commit whatever it wrote."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        outcome = decide()
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _bump(app_state)
    return outcome


# ---------------------------------------------------------------------------
# Public decisions
# ---------------------------------------------------------------------------


def claim(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    tab_id: str,
    render_id: str,
) -> None:
    """Grant the case lease to ``(tab_id, render_id)`` or raise."""

    def decide() -> bool:
        render = _claimable_render(conn, clinician_id, patient_id, render_id)
        return _acquire(conn, now(app_state), render, tab_id)

    if not _locked(conn, app_state, decide):
        raise TabConflictError("this case is open in another tab or window")


def require_owner(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    tab_id: str | None,
    render_id: str | None,
) -> None:
    """Allow a guarded write from the lease holder (refreshing it), or raise.

    No lease, a stale or a void one: acquired exactly as :func:`claim`
    would (a restarted server cleared every lease). The owner tab's older
    render is refused: a duplicated tab shares the tab id, never the render.
    """
    if not tab_id or not render_id:
        raise TabConflictError("this request does not name its tab; reload the case")

    def decide() -> bool:
        moment = now(app_state)
        lease = tab_leases.fetch(conn, clinician_id, patient_id)
        if _is_live(lease, _open_session(conn, clinician_id, patient_id), moment):
            assert lease is not None
            if lease.tab_id == tab_id and lease.render_id == render_id:
                tab_leases.upsert(conn, replace(lease, last_seen_at=moment))
                return True
            if lease.tab_id == tab_id:
                raise TabConflictError("this view is outdated; reload the case")

        render = _claimable_render(conn, clinician_id, patient_id, render_id)
        return _acquire(conn, moment, render, tab_id)

    if not _locked(conn, app_state, decide):
        raise TabConflictError("this case is open in another tab or window")


def move_to_render(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    tab_id: str,
    render_id: str,
) -> None:
    """After a guarded advance: the owner's lease follows its new render."""

    def decide() -> None:
        lease = tab_leases.fetch(conn, clinician_id, patient_id)
        if lease is None or lease.tab_id != tab_id:
            return
        render = _claimable_render(conn, clinician_id, patient_id, render_id)
        _acquire(conn, now(app_state), render, tab_id)

    _locked(conn, app_state, decide)


def release(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    tab_id: str,
    render_id: str,
    reason: ReleaseReason,
) -> bool:
    """Drop the lease if ``(tab_id, render_id)`` holds it; ``False`` = no-op."""

    def decide() -> bool:
        lease = tab_leases.fetch(conn, clinician_id, patient_id)
        if lease is None or lease.tab_id != tab_id or lease.render_id != render_id:
            return False
        tab_leases.delete(conn, clinician_id, patient_id)
        _audit(conn, _RELEASED, _lease_render(conn, lease), tab_id, reason)
        return True

    return bool(_locked(conn, app_state, decide))


def release_all(conn: sqlite3.Connection, app_state: Any, *, clinician_id: str) -> int:
    """Logout: every lease of the clinician goes."""

    def decide() -> int:
        removed = tab_leases.delete_for_clinician(conn, clinician_id)
        for lease in removed:
            _audit(conn, _RELEASED, _lease_render(conn, lease), lease.tab_id, ReleaseReason.LOGOUT)
        return len(removed)

    return int(_locked(conn, app_state, decide))


def clear_leases(conn: sqlite3.Connection) -> int:
    """Boot: no lease of a previous process is live (own commit, no events)."""
    removed = tab_leases.clear_all(conn)
    conn.commit()
    return removed
