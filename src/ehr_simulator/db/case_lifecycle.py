"""case_lifecycle DAO (S11e): sole writer of the ``case_lifecycle`` table.

One row per realised Phase 2 case. Every transition is a compare-and-set on
the expected source state; a miss raises :class:`CaseLifecycleError` and
writes nothing::

    activation ──► active ◄──► paused
                     │  \\        │
                     ▼   \\       ▼
                completed  └─► incomplete

Timestamps are supplied by the caller (the service's injected clock) and
stored as UTC ``YYYY-MM-DD HH:MM:SS`` like every ``CURRENT_TIMESTAMP``
column. ``commit=False`` leaves a write in the caller's transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ehr_simulator.db.exceptions import CaseLifecycleError

_DB_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


class CaseState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class IncompleteReason(StrEnum):
    RECONNECTION_TIMEOUT = "reconnection_timeout"
    PAUSE_TIMEOUT = "pause_timeout"
    OPERATOR_ABANDONED = "operator_abandoned"


OPEN_STATES = frozenset({CaseState.ACTIVE, CaseState.PAUSED})


@dataclass(frozen=True)
class CaseLifecycle:
    clinician_id: str
    patient_id: str
    state: CaseState
    state_changed_at: datetime
    last_seen_at: datetime
    paused_at: datetime | None
    completed_at: datetime | None
    incomplete_at: datetime | None
    incomplete_reason: IncompleteReason | None

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES


@dataclass(frozen=True)
class LifecycleCounts:
    active: int = 0
    paused: int = 0
    completed: int = 0
    incomplete: int = 0

    @property
    def activated(self) -> int:
        """Every realised case, whatever its state."""
        return self.active + self.paused + self.completed + self.incomplete


_COLUMNS = (
    "clinician_id, patient_id, state, state_changed_at, last_seen_at, "
    "paused_at, completed_at, incomplete_at, incomplete_reason"
)


def to_db_timestamp(moment: datetime) -> str:
    """Aware or naive-UTC datetime → the stored UTC text form."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.strftime(_DB_TIMESTAMP_FORMAT)


def _as_utc(value: datetime | str | None) -> datetime | None:
    # PARSE_DECLTYPES yields naive datetimes; a raw string can still come back
    # from a connection opened without it. Both are UTC.
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _row(row: tuple) -> CaseLifecycle:
    return CaseLifecycle(
        clinician_id=row[0],
        patient_id=row[1],
        state=CaseState(row[2]),
        state_changed_at=_as_utc(row[3]),  # type: ignore[arg-type]
        last_seen_at=_as_utc(row[4]),  # type: ignore[arg-type]
        paused_at=_as_utc(row[5]),
        completed_at=_as_utc(row[6]),
        incomplete_at=_as_utc(row[7]),
        incomplete_reason=IncompleteReason(row[8]) if row[8] is not None else None,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def fetch(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> CaseLifecycle | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM case_lifecycle WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else _row(row)


def list_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> dict[str, CaseLifecycle]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM case_lifecycle WHERE clinician_id = ?", (clinician_id,)
    ).fetchall()
    return {row[1]: _row(row) for row in rows}


def list_open(conn: sqlite3.Connection) -> list[CaseLifecycle]:
    """Every ``active`` or ``paused`` case, across clinicians."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM case_lifecycle WHERE state IN (?, ?) "
        "ORDER BY clinician_id, patient_id",
        (CaseState.ACTIVE, CaseState.PAUSED),
    ).fetchall()
    return [_row(row) for row in rows]


def counts_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> LifecycleCounts:
    return counts_by_clinician(conn).get(clinician_id, LifecycleCounts())


def counts_by_clinician(conn: sqlite3.Connection) -> dict[str, LifecycleCounts]:
    """Per-clinician state counts across every configuration version."""
    tallies: dict[str, dict[str, int]] = {}
    for clinician_id, state, n in conn.execute(
        "SELECT clinician_id, state, COUNT(*) FROM case_lifecycle GROUP BY clinician_id, state"
    ):
        tallies.setdefault(clinician_id, {})[state] = n
    return {cid: LifecycleCounts(**states) for cid, states in sorted(tallies.items())}


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def insert_active(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    now: datetime,
    commit: bool = True,
) -> None:
    """Record a freshly activated case; an existing active row is an exact retry."""
    existing = fetch(conn, clinician_id, patient_id)
    if existing is not None:
        if existing.state is CaseState.ACTIVE:
            return

        raise CaseLifecycleError(
            f"case {patient_id!r} already has lifecycle state {existing.state}; "
            "refusing a second activation"
        )

    stamp = to_db_timestamp(now)
    conn.execute(
        "INSERT INTO case_lifecycle "
        "(clinician_id, patient_id, state, state_changed_at, last_seen_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (clinician_id, patient_id, CaseState.ACTIVE, stamp, stamp),
    )
    if commit:
        conn.commit()


def _transition(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    expected: CaseState,
    assignments: str,
    params: tuple,
    commit: bool,
) -> None:
    """Compare-and-set ``UPDATE`` guarded on ``expected``; a miss writes nothing."""
    cursor = conn.execute(
        f"UPDATE case_lifecycle SET {assignments} "
        "WHERE clinician_id = ? AND patient_id = ? AND state = ?",
        (*params, clinician_id, patient_id, expected),
    )
    if commit:
        # Also ends the empty transaction a missed UPDATE opened.
        conn.commit()
    if cursor.rowcount == 1:
        return

    current = fetch(conn, clinician_id, patient_id)
    found = "no lifecycle row" if current is None else f"state {current.state}"
    raise CaseLifecycleError(f"case {patient_id!r} expected state {expected}, found {found}")


def touch(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    now: datetime,
    commit: bool = True,
) -> None:
    """Move ``last_seen_at`` of an active case to ``now``."""
    _transition(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        expected=CaseState.ACTIVE,
        assignments="last_seen_at = ?",
        params=(to_db_timestamp(now),),
        commit=commit,
    )


def pause(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    now: datetime,
    commit: bool = True,
) -> None:
    stamp = to_db_timestamp(now)
    _transition(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        expected=CaseState.ACTIVE,
        assignments="state = ?, state_changed_at = ?, paused_at = ?, last_seen_at = ?",
        params=(CaseState.PAUSED, stamp, stamp, stamp),
        commit=commit,
    )


def resume(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    now: datetime,
    commit: bool = True,
) -> None:
    stamp = to_db_timestamp(now)
    _transition(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        expected=CaseState.PAUSED,
        assignments="state = ?, state_changed_at = ?, paused_at = NULL, last_seen_at = ?",
        params=(CaseState.ACTIVE, stamp, stamp),
        commit=commit,
    )


def complete(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    now: datetime,
    commit: bool = True,
) -> None:
    stamp = to_db_timestamp(now)
    _transition(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        expected=CaseState.ACTIVE,
        assignments="state = ?, state_changed_at = ?, completed_at = ?, last_seen_at = ?",
        params=(CaseState.COMPLETED, stamp, stamp, stamp),
        commit=commit,
    )


def mark_incomplete(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    reason: IncompleteReason,
    from_state: CaseState,
    now: datetime,
    commit: bool = True,
) -> None:
    """Terminal ``incomplete``; ``last_seen_at``/``paused_at`` stay as evidence."""
    if from_state not in OPEN_STATES:
        raise CaseLifecycleError(f"only open cases can become incomplete; got {from_state}")

    stamp = to_db_timestamp(now)
    _transition(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        expected=from_state,
        assignments="state = ?, state_changed_at = ?, incomplete_at = ?, incomplete_reason = ?",
        params=(CaseState.INCOMPLETE, stamp, stamp, reason),
        commit=commit,
    )
