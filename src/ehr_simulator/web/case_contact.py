"""S11e: lifecycle gate run on every meaningful contact with a case.

::

    route ─► check(case)                         before the action
               ├─ no lifecycle row → UNTRACKED   (Phase 1 walks: unchanged)
               ├─ timed out        → expire + COMMIT → INCOMPLETE (expired)
               └─ ACTIVE / PAUSED / COMPLETED / INCOMPLETE
             action (answer, advance, render, heartbeat)
           ─► touch(result)                      after the action succeeded:
                                                 last_seen_at + case.reconnected

The timeout is decided against the policy pinned by the case's activation
configuration, never the active one. ``app.state.clock`` is the only time
source, so tests can move it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ehr_simulator import case_lifecycle
from ehr_simulator.case_lifecycle import LifecyclePolicy
from ehr_simulator.db import arm_assignments
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db.case_lifecycle import CaseLifecycle, CaseState
from ehr_simulator.db.exceptions import CaseLifecycleError
from ehr_simulator.logging import get_logger
from ehr_simulator.web.study_session import CaseConfiguration

#: Browser heartbeat period while a measured case page is open. Must stay
#: well below ``config.study.MIN_RECONNECTION_GRACE_SECONDS`` (lockstep test).
HEARTBEAT_INTERVAL_SECONDS = 15

#: A contact after this much silence (three missed heartbeats) is recorded
#: as ``case.reconnected`` — an auditable short interruption.
RECONNECT_GAP_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS

Clock = Callable[[], datetime]


class CaseAccess(StrEnum):
    UNTRACKED = "untracked"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class ContactResult:
    access: CaseAccess
    case: CaseLifecycle | None
    expired: bool = False  # this contact is what timed the case out


def system_clock() -> datetime:
    return datetime.now(UTC)


def now(app_state: Any) -> datetime:
    """Current time from the app's injected clock (aware UTC)."""
    clock: Clock = getattr(app_state, "clock", None) or system_clock
    return clock()


def policy_for(case: CaseConfiguration | None) -> LifecyclePolicy:
    """Lifecycle rules pinned by the case's activation configuration."""
    config = case.study.case_lifecycle if case is not None else None
    return LifecyclePolicy.from_config(config)


def _bump(app_state: Any) -> None:
    app_state.write_counter = getattr(app_state, "write_counter", 0) + 1


def _expire_atomically(
    conn: sqlite3.Connection, app_state: Any, row: CaseLifecycle, policy: LifecyclePolicy
) -> CaseLifecycle:
    """Re-read under the write lock, then time the case out in one commit."""
    moment = now(app_state)
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = lifecycle_dao.fetch(conn, row.clinician_id, row.patient_id)
        timeout = case_lifecycle.evaluate(current, policy, moment) if current else None
        if current is None or timeout is None:
            conn.rollback()
            return current or row

        case_lifecycle.expire(conn, current, timeout, moment)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _bump(app_state)
    get_logger().info("case timed out", event_kind="case.incomplete", reason=str(timeout.reason))
    return lifecycle_dao.fetch(conn, row.clinician_id, row.patient_id)  # type: ignore[return-value]


def check(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    patient_id: str,
    case: CaseConfiguration | None,
) -> ContactResult:
    """Evaluate the lazy timeout, then report what the case allows."""
    row = lifecycle_dao.fetch(conn, clinician_id, patient_id)
    if row is None:
        return ContactResult(CaseAccess.UNTRACKED, None)

    policy = policy_for(case)
    if row.is_open and case_lifecycle.evaluate(row, policy, now(app_state)) is not None:
        row = _expire_atomically(conn, app_state, row, policy)
        return ContactResult(CaseAccess(row.state), row, expired=row.state is CaseState.INCOMPLETE)

    return ContactResult(CaseAccess(row.state), row)


def touch(conn: sqlite3.Connection, app_state: Any, result: ContactResult) -> None:
    """Record a successful contact with an active case (own commit).

    Re-reads the row: an action that just completed or paused the case
    leaves nothing to touch.
    """
    if result.access is not CaseAccess.ACTIVE or result.case is None:
        return

    current = lifecycle_dao.fetch(conn, result.case.clinician_id, result.case.patient_id)
    if current is None or current.state is not CaseState.ACTIVE:
        return

    try:
        case_lifecycle.record_contact(
            conn, current, now=now(app_state), reconnect_gap_seconds=RECONNECT_GAP_SECONDS
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _bump(app_state)


def _locked_transition(
    conn: sqlite3.Connection,
    app_state: Any,
    result: ContactResult,
    expected: CaseState,
    write: Callable[[CaseLifecycle, datetime], object],
) -> None:
    """Re-read under ``BEGIN IMMEDIATE``; apply ``write`` only in ``expected``."""
    if result.case is None:
        raise CaseLifecycleError("no lifecycle state for this case")

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = lifecycle_dao.fetch(conn, result.case.clinician_id, result.case.patient_id)
        if current is None or current.state is not expected:
            found = "no lifecycle row" if current is None else f"state {current.state}"
            raise CaseLifecycleError(f"case expected state {expected}, found {found}")

        write(current, now(app_state))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    _bump(app_state)


def pause_case(conn: sqlite3.Connection, app_state: Any, result: ContactResult) -> None:
    """Active → paused (policy already checked by the caller), one commit."""
    _locked_transition(
        conn,
        app_state,
        result,
        CaseState.ACTIVE,
        lambda current, moment: case_lifecycle.pause(conn, current, now=moment),
    )


def resume_case(
    conn: sqlite3.Connection,
    app_state: Any,
    result: ContactResult,
    case: CaseConfiguration | None,
) -> None:
    """Paused → active with a new session under the case's pinned provenance."""

    def _resume(current: CaseLifecycle, moment: datetime) -> None:
        assignment = arm_assignments.fetch_for_pair(conn, current.clinician_id, current.patient_id)
        if assignment is None or case is None:
            raise CaseLifecycleError("a paused case must hold an assignment")

        case_lifecycle.resume(
            conn,
            current,
            now=moment,
            arm=assignment.arm,
            config_hash=case.config_hash,
            config_version=case.config_version,
        )

    _locked_transition(conn, app_state, result, CaseState.PAUSED, _resume)
