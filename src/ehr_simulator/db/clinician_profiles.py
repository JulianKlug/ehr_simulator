"""S11p clinician profiles DAO: sole writer of ``clinician_profiles``.

One row per clinician. SQLite enforces the role/specialty rule, the lock
after the first measured case, and that rows are never deleted; this module
checks the lock first so the caller gets a clear error instead of a raw
trigger message.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from ehr_simulator.db._timestamps import _as_utc, to_db_timestamp
from ehr_simulator.db.arm_assignments import ARM_SOURCE_PHASE2

__all__ = [
    "ProfileLockedError",
    "StoredProfile",
    "fetch",
    "fetch_by_ids",
    "is_locked",
    "save",
]

_COLUMNS = (
    "clinician_id, professional_role, years_of_practice, country_of_practice, "
    "primary_specialty, recorded_at, updated_at"
)


class ProfileLockedError(Exception):
    """The clinician already holds a measured case; nothing was written."""


@dataclass(frozen=True)
class StoredProfile:
    clinician_id: str
    professional_role: str
    years_of_practice: float
    country_of_practice: str
    primary_specialty: str | None
    recorded_at: datetime
    updated_at: datetime


def _row(row: tuple) -> StoredProfile:
    return StoredProfile(
        clinician_id=row[0],
        professional_role=row[1],
        years_of_practice=float(row[2]),
        country_of_practice=row[3],
        primary_specialty=row[4],
        recorded_at=_as_utc(row[5]),
        updated_at=_as_utc(row[6]),
    )


def fetch(conn: sqlite3.Connection, clinician_id: str) -> StoredProfile | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM clinician_profiles WHERE clinician_id = ?", (clinician_id,)
    ).fetchone()
    return None if row is None else _row(row)


def fetch_by_ids(conn: sqlite3.Connection, ids: tuple[str, ...]) -> dict[str, StoredProfile]:
    """S11p export: the profiles of ``ids`` (clinicians without one are absent)."""
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM clinician_profiles WHERE clinician_id IN ({placeholders})",
        ids,
    ).fetchall()
    return {r[0]: _row(r) for r in rows}


def is_locked(conn: sqlite3.Connection, clinician_id: str) -> bool:
    """True once the clinician holds a measured Phase 2 case."""
    row = conn.execute(
        "SELECT 1 FROM arm_assignments WHERE clinician_id = ? AND arm_source = ? LIMIT 1",
        (clinician_id, ARM_SOURCE_PHASE2),
    ).fetchone()
    return row is not None


def save(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    professional_role: str,
    years_of_practice: float,
    country_of_practice: str,
    primary_specialty: str | None,
    now: datetime,
) -> bool:
    """Insert or update the profile (no commit); ``True`` when newly created.

    Raises:
        ProfileLockedError: a measured case exists; nothing written.
    """
    if is_locked(conn, clinician_id):
        raise ProfileLockedError("the profile is locked once a measured case has started")

    stamp = to_db_timestamp(now)
    existing = fetch(conn, clinician_id)
    if existing is None:
        conn.execute(
            f"INSERT INTO clinician_profiles ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                clinician_id,
                professional_role,
                years_of_practice,
                country_of_practice,
                primary_specialty,
                stamp,
                stamp,
            ),
        )
        return True

    conn.execute(
        "UPDATE clinician_profiles SET professional_role = ?, years_of_practice = ?, "
        "country_of_practice = ?, primary_specialty = ?, updated_at = ? WHERE clinician_id = ?",
        (
            professional_role,
            years_of_practice,
            country_of_practice,
            primary_specialty,
            stamp,
            clinician_id,
        ),
    )
    return False
