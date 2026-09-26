"""S11e case lifecycle service: pure timeout policy + transactional transitions.

::

    routes ─► web/case_contact.py / web/case_start.py / web/gating.py
                           │
                           ▼
              case_lifecycle.py      (this module: policy + transitions)
                           │
                           ▼
              db/case_lifecycle.py   (sole writer of case_lifecycle)

No background worker: a timeout is evaluated lazily, on the next contact
with the case, against the policy pinned by the case's activation
configuration. Example with ``reconnection_grace_seconds: 300``::

    last_seen 10:00:00, contact 10:04:59 → continue
    last_seen 10:00:00, contact 10:05:01 → incomplete (reconnection_timeout,
                                            deadline 10:05:00)

Every writer here takes ``commit`` and never commits unless asked, so a
caller can fold the transition into its own transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ehr_simulator.config.snapshot import parse_study_snapshot
from ehr_simulator.config.study import CaseLifecycleConfig
from ehr_simulator.db import arm_assignments, config_history, events, sessions
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db.case_lifecycle import (
    CaseLifecycle,
    CaseState,
    IncompleteReason,
    LifecycleCounts,
)
from ehr_simulator.db.exceptions import CaseLifecycleError, ConfigurationProvenanceError


@dataclass(frozen=True)
class LifecyclePolicy:
    """The timeout/pause rules one case runs under (``None`` = not enforced)."""

    reconnection_grace_seconds: int | None
    pause_grace_seconds: int | None

    @property
    def pause_enabled(self) -> bool:
        return self.pause_grace_seconds is not None

    @classmethod
    def from_config(cls, config: CaseLifecycleConfig | None) -> LifecyclePolicy:
        if config is None:
            return cls(reconnection_grace_seconds=None, pause_grace_seconds=None)

        return cls(
            reconnection_grace_seconds=config.reconnection_grace_seconds,
            pause_grace_seconds=config.effective_pause_grace_seconds,
        )


@dataclass(frozen=True)
class Timeout:
    reason: IncompleteReason
    deadline: datetime
    grace_seconds: int


# ---------------------------------------------------------------------------
# Pure policy
# ---------------------------------------------------------------------------


def evaluate(case: CaseLifecycle, policy: LifecyclePolicy, now: datetime) -> Timeout | None:
    """``Timeout`` when an open case outlived its grace period, else ``None``."""
    if case.state is CaseState.ACTIVE and policy.reconnection_grace_seconds is not None:
        grace = policy.reconnection_grace_seconds
        deadline = case.last_seen_at + timedelta(seconds=grace)
        if now > deadline:
            return Timeout(IncompleteReason.RECONNECTION_TIMEOUT, deadline, grace)
        return None

    if case.state is CaseState.PAUSED and case.paused_at is not None:
        # A paused case under a policy that no longer allows pausing cannot
        # exist (pause is pinned), so a missing grace means "never expires".
        if policy.pause_grace_seconds is None:
            return None

        grace = policy.pause_grace_seconds
        deadline = case.paused_at + timedelta(seconds=grace)
        if now > deadline:
            return Timeout(IncompleteReason.PAUSE_TIMEOUT, deadline, grace)

    return None


def pinned_policy(conn: sqlite3.Connection, case: CaseLifecycle) -> LifecyclePolicy:
    """Policy of the configuration the case was activated under."""
    assignment = arm_assignments.fetch_for_pair(conn, case.clinician_id, case.patient_id)
    if assignment is None or assignment.config_version is None:
        raise ConfigurationProvenanceError(
            f"case {case.patient_id!r} has no activation configuration"
        )

    row = config_history.require_known(conn, assignment.config_version, assignment.config_hash)
    return LifecyclePolicy.from_config(parse_study_snapshot(row.study_json).case_lifecycle)


def overdue_cases(conn: sqlite3.Connection, now: datetime) -> list[tuple[CaseLifecycle, Timeout]]:
    """Open cases past their pinned grace at ``now``; never writes.

    Covers clinicians who never come back, whose case no contact would
    ever time out lazily.
    """
    overdue = []
    for case in lifecycle_dao.list_open(conn):
        timeout = evaluate(case, pinned_policy(conn, case), now)
        if timeout is not None:
            overdue.append((case, timeout))
    return overdue


def limit_reached(counts: LifecycleCounts, config: CaseLifecycleConfig | None) -> bool:
    """Clinician stopping rule of the active configuration; no section → never."""
    if config is None:
        return False

    return (
        counts.completed >= config.target_completed_cases_per_clinician
        or counts.activated >= config.max_activated_cases_per_clinician
    )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _session_for_event(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> str | None:
    return sessions.find_open(conn, clinician_id, patient_id) or sessions.find_latest(
        conn, clinician_id, patient_id
    )


def _append(
    conn: sqlite3.Connection,
    *,
    session_id: str | None,
    clinician_id: str,
    patient_id: str,
    kind: events.EventKind,
    payload: dict[str, Any],
) -> None:
    events.append(
        conn,
        session_id=session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=None,
        kind=kind,
        payload=payload,
        commit=False,
    )


def _close_open_session(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> None:
    open_session = sessions.find_open(conn, clinician_id, patient_id)
    if open_session is not None:
        sessions.close(conn, open_session, commit=False)


def mark_incomplete(
    conn: sqlite3.Connection,
    case: CaseLifecycle,
    *,
    reason: IncompleteReason,
    now: datetime,
    timeout: Timeout | None = None,
) -> None:
    """Open case → ``incomplete``: state, session close and event (uncommitted).

    Assignment, progress, answers and telemetry are never touched.
    """
    session_id = _session_for_event(conn, case.clinician_id, case.patient_id)
    lifecycle_dao.mark_incomplete(
        conn,
        clinician_id=case.clinician_id,
        patient_id=case.patient_id,
        reason=reason,
        from_state=case.state,
        now=now,
        commit=False,
    )
    _close_open_session(conn, case.clinician_id, case.patient_id)
    _append(
        conn,
        session_id=session_id,
        clinician_id=case.clinician_id,
        patient_id=case.patient_id,
        kind="case.incomplete",
        payload={
            "reason": str(reason),
            "deadline": lifecycle_dao.to_db_timestamp(timeout.deadline) if timeout else None,
            "grace_seconds": timeout.grace_seconds if timeout else None,
        },
    )


def expire(conn: sqlite3.Connection, case: CaseLifecycle, timeout: Timeout, now: datetime) -> None:
    """Apply a lazy timeout decision (uncommitted)."""
    mark_incomplete(conn, case, reason=timeout.reason, now=now, timeout=timeout)


def expire_if_overdue(
    conn: sqlite3.Connection, case: CaseLifecycle, policy: LifecyclePolicy, now: datetime
) -> Timeout | None:
    """Re-read under the write lock, then time the case out in one commit.

    ``None``: the case is no longer open or no longer overdue (a racing
    contact won); nothing is written.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = lifecycle_dao.fetch(conn, case.clinician_id, case.patient_id)
        timeout = evaluate(current, policy, now) if current else None
        if current is None or timeout is None:
            conn.rollback()
            return None

        expire(conn, current, timeout, now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return timeout


def pause(conn: sqlite3.Connection, case: CaseLifecycle, *, now: datetime) -> None:
    """Active → paused; closes the open session (uncommitted)."""
    session_id = _session_for_event(conn, case.clinician_id, case.patient_id)
    lifecycle_dao.pause(
        conn, clinician_id=case.clinician_id, patient_id=case.patient_id, now=now, commit=False
    )
    _close_open_session(conn, case.clinician_id, case.patient_id)
    _append(
        conn,
        session_id=session_id,
        clinician_id=case.clinician_id,
        patient_id=case.patient_id,
        kind="case.paused",
        payload={},
    )


def resume(
    conn: sqlite3.Connection,
    case: CaseLifecycle,
    *,
    now: datetime,
    arm: str,
    config_hash: str,
    config_version: str | None,
) -> str:
    """Paused → active with a new session carrying the pinned provenance.

    Returns the new ``session_id`` (uncommitted).
    """
    if case.paused_at is None:
        raise CaseLifecycleError(f"case {case.patient_id!r} is not paused")

    lifecycle_dao.resume(
        conn, clinician_id=case.clinician_id, patient_id=case.patient_id, now=now, commit=False
    )
    session_id = sessions.start_or_resume(
        conn,
        case.clinician_id,
        case.patient_id,
        arm=arm,
        config_hash=config_hash,
        config_version=config_version,
        commit=False,
    )
    paused_seconds = int((now - case.paused_at).total_seconds())
    common = {
        "session_id": session_id,
        "clinician_id": case.clinician_id,
        "patient_id": case.patient_id,
    }
    _append(conn, **common, kind="case.resumed", payload={"paused_seconds": paused_seconds})
    _append(conn, **common, kind="session.start", payload={"arm": arm})
    return session_id


def record_contact(
    conn: sqlite3.Connection,
    case: CaseLifecycle,
    *,
    now: datetime,
    reconnect_gap_seconds: int,
) -> None:
    """Touch an active case; a long silent gap is recorded as ``case.reconnected``."""
    gap_seconds = int((now - case.last_seen_at).total_seconds())
    if gap_seconds > reconnect_gap_seconds:
        _append(
            conn,
            session_id=_session_for_event(conn, case.clinician_id, case.patient_id),
            clinician_id=case.clinician_id,
            patient_id=case.patient_id,
            kind="case.reconnected",
            payload={"gap_seconds": gap_seconds},
        )

    lifecycle_dao.touch(
        conn, clinician_id=case.clinician_id, patient_id=case.patient_id, now=now, commit=False
    )


def complete(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    session_id: str,
    now: datetime,
) -> bool:
    """Active → completed + ``case.completed`` (uncommitted).

    Returns ``False`` for a pair without a lifecycle row (Phase 1 walks).
    """
    if lifecycle_dao.fetch(conn, clinician_id, patient_id) is None:
        return False

    lifecycle_dao.complete(
        conn, clinician_id=clinician_id, patient_id=patient_id, now=now, commit=False
    )
    _append(
        conn,
        session_id=session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        kind="case.completed",
        payload={},
    )
    return True


def abandon(
    conn: sqlite3.Connection, *, clinician_id: str, patient_id: str, now: datetime
) -> CaseLifecycle:
    """Operator path: open case → ``incomplete`` (``operator_abandoned``), committed.

    Raises:
        CaseLifecycleError: no lifecycle row, or the case is already terminal.
    """
    case = lifecycle_dao.fetch(conn, clinician_id, patient_id)
    if case is None:
        raise CaseLifecycleError(f"patient {patient_id!r} is not a realised case")
    if not case.is_open:
        raise CaseLifecycleError(f"case {patient_id!r} is already {case.state}")

    try:
        mark_incomplete(conn, case, reason=IncompleteReason.OPERATOR_ABANDONED, now=now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return lifecycle_dao.fetch(conn, clinician_id, patient_id)  # type: ignore[return-value]
