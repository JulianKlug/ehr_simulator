"""clinicians DAO: case-folded name → pseudonymizable ``clinician_id``.

``lookup_or_create`` is the only write path. It normalizes the raw name
(``" ".join(raw.casefold().split())`` — case-fold + collapse whitespace),
truncates SHA256 to 16 hex chars for the ``clinician_id``, and INSERT-OR-IGNOREs
into ``clinicians``. The function is called from the ``/login`` POST handler;
the optional ``known_clinicians`` set is the lifespan-scoped cache the
``_require_clinician`` preamble reads from (review-fix R11).

Two round-trips on existing clinician (INSERT-OR-IGNORE returns no row,
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
    conn.execute(
        "INSERT OR IGNORE INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
        (clinician_id, name_normalized),
    )
    conn.commit()
    if known_clinicians is not None:
        known_clinicians.add(clinician_id)
    return clinician_id
