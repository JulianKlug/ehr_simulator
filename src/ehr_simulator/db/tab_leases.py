"""S11m case tab leases DAO: the one tab that owns an active measured case.

Sole writer of ``case_tab_leases``. The table is mutable operational state
(one row per clinician × patient, refreshed, moved, deleted, cleared at
boot); ownership decisions live in ``web/tab_guard.py`` and their audit in
the ``tab.*`` events. No function here commits: callers own the
``BEGIN IMMEDIATE`` transaction a decision runs in.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from ehr_simulator.db.case_lifecycle import to_db_timestamp

__all__ = [
    "TabLease",
    "clear_all",
    "delete",
    "delete_for_clinician",
    "fetch",
    "list_for_clinician",
    "upsert",
]

_COLUMNS = "clinician_id, patient_id, session_id, tab_id, render_id, claimed_at, last_seen_at"


@dataclass(frozen=True)
class TabLease:
    clinician_id: str
    patient_id: str
    session_id: str
    tab_id: str
    render_id: str
    claimed_at: datetime
    last_seen_at: datetime


def _as_utc(value: datetime | str) -> datetime:
    # PARSE_DECLTYPES yields naive UTC datetimes; a raw string may come back too.
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _row(row: tuple) -> TabLease:
    return TabLease(
        clinician_id=row[0],
        patient_id=row[1],
        session_id=row[2],
        tab_id=row[3],
        render_id=row[4],
        claimed_at=_as_utc(row[5]),
        last_seen_at=_as_utc(row[6]),
    )


def fetch(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> TabLease | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM case_tab_leases WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else _row(row)


def list_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> tuple[TabLease, ...]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM case_tab_leases WHERE clinician_id = ? ORDER BY patient_id",
        (clinician_id,),
    ).fetchall()
    return tuple(_row(r) for r in rows)


def upsert(conn: sqlite3.Connection, lease: TabLease) -> None:
    """Insert or replace the pair's lease (claim, reclaim, refresh, move)."""
    conn.execute(
        f"INSERT OR REPLACE INTO case_tab_leases ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            lease.clinician_id,
            lease.patient_id,
            lease.session_id,
            lease.tab_id,
            lease.render_id,
            to_db_timestamp(lease.claimed_at),
            to_db_timestamp(lease.last_seen_at),
        ),
    )


def delete(conn: sqlite3.Connection, clinician_id: str, patient_id: str) -> int:
    cursor = conn.execute(
        "DELETE FROM case_tab_leases WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    )
    return cursor.rowcount


def delete_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> tuple[TabLease, ...]:
    """Remove every lease of one clinician (logout); return what was removed."""
    removed = list_for_clinician(conn, clinician_id)
    conn.execute("DELETE FROM case_tab_leases WHERE clinician_id = ?", (clinician_id,))
    return removed


def clear_all(conn: sqlite3.Connection) -> int:
    """Boot: a lease of a previous server process proves nothing live."""
    return conn.execute("DELETE FROM case_tab_leases").rowcount
