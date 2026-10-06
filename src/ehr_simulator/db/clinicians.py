"""clinicians DAO: case-folded name → pseudonymizable ``clinician_id``.

``lookup_or_create`` is the only write path; ``lookup`` (S9b) is its read-only
sibling for operator commands. It normalizes the raw name
(``" ".join(raw.casefold().split())`` — case-fold + collapse whitespace),
truncates SHA256 to 16 hex chars for the ``clinician_id``, and inserts into
``clinicians`` (``ON CONFLICT(clinician_id) DO NOTHING``). The function is
called from the ``/login`` POST handler; the optional ``known_clinicians``
set is the lifespan-scoped cache the ``_require_clinician`` preamble reads
from (review-fix R11).

Two round-trips on existing clinician (the insert does nothing,
fallback SELECT resolves the canonical id). Fine at the pilot scale of one
POST per session.
"""

from __future__ import annotations

import hashlib
import sqlite3


def _normalize(raw_name: str) -> str:
    return " ".join(raw_name.casefold().split())


def lookup_or_create(
    conn: sqlite3.Connection,
    raw_name: str,
    *,
    known_clinicians: set[str] | None = None,
) -> str:
    """Return the canonical ``clinician_id`` for ``raw_name``, creating the
    row if needed. Raises :class:`ValueError` when the name is empty after
    trimming."""
    name_normalized = _normalize(raw_name)
    if not name_normalized:
        raise ValueError("name must be non-empty after trimming")
    clinician_id = hashlib.sha256(name_normalized.encode("utf-8")).hexdigest()[:16]
    # Only an existing id is benign; any other constraint failure raises.
    conn.execute(
        "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?) "
        "ON CONFLICT(clinician_id) DO NOTHING",
        (clinician_id, name_normalized),
    )
    conn.commit()
    if known_clinicians is not None:
        known_clinicians.add(clinician_id)
    return clinician_id


def fetch_by_ids(
    conn: sqlite3.Connection, clinician_ids: tuple[str, ...] | list[str]
) -> tuple[tuple[str, str], ...]:
    """``(clinician_id, name_normalized)`` pairs for requested ids, id-sorted."""
    ids = tuple(sorted(set(clinician_ids)))
    if not ids:
        return ()

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        "SELECT clinician_id, name_normalized FROM clinicians "
        f"WHERE clinician_id IN ({placeholders}) "
        "ORDER BY clinician_id",
        ids,
    ).fetchall()
    return tuple((row[0], row[1]) for row in rows)


def fetch_all_ids(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Every ``clinician_id`` in the table, id-sorted (S10 integrity check)."""
    rows = conn.execute("SELECT clinician_id FROM clinicians ORDER BY clinician_id").fetchall()
    return tuple(row[0] for row in rows)


def lookup(conn: sqlite3.Connection, raw_name: str) -> str | None:
    """Return the ``clinician_id`` for ``raw_name`` if the clinician exists; never writes.

    Operator paths (S9b ``reset-progress``) must not create a clinician by
    mistyping a name.
    """
    name_normalized = _normalize(raw_name)
    if not name_normalized:
        return None
    row = conn.execute(
        "SELECT clinician_id FROM clinicians WHERE name_normalized = ?", (name_normalized,)
    ).fetchone()
    return None if row is None else row[0]


def fetch_name(conn: sqlite3.Connection, clinician_id: str) -> str | None:
    """The clinician's case-folded display name, or ``None`` if unknown."""
    row = conn.execute(
        "SELECT name_normalized FROM clinicians WHERE clinician_id = ?", (clinician_id,)
    ).fetchone()
    return None if row is None else row[0]
