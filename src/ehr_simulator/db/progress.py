"""progress DAO: the per-(clinician, patient) walk frontier (S9b).

One row per pair, created by the first ``/advance``. A missing row *is* the
"not started" state — reads never insert, so browsing stays a pure read on
this table::

    unlocked_t_index   highest study t_index the clinician may view
    completed_at       set once by the final advance (NULL while walking)
    config_hash        the hash the walk STARTED under — written on INSERT
                       only, so a mid-pilot config edit stays detectable

``unlock`` is a compare-and-set on the frontier the caller observed: a
racing second advance finds the row already moved, gets ``False`` back and
the service degrades it to "stale". Correctness lives in SQL, not in
event-loop scheduling.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ehr_simulator.db.answers import _require_version_provenance
from ehr_simulator.db.exceptions import ConfigurationProvenanceError


@dataclass(frozen=True)
class Progress:
    clinician_id: str
    patient_id: str
    unlocked_t_index: int
    completed_at: datetime | None
    config_hash: str
    config_version: str | None = None


_SELECT_COLUMNS = (
    "clinician_id, patient_id, unlocked_t_index, completed_at, config_hash, config_version"
)


def _row_to_progress(row: tuple) -> Progress:
    return Progress(
        clinician_id=row[0],
        patient_id=row[1],
        unlocked_t_index=int(row[2]),
        completed_at=row[3],
        config_hash=row[4],
        config_version=row[5],
    )


def _bump(app_state: Any) -> None:
    if app_state is not None:
        app_state.write_counter = getattr(app_state, "write_counter", 0) + 1


def _require_row_provenance(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    config_hash: str,
    config_version: str | None,
) -> None:
    """Refuse to move a walk pinned to another configuration (S11b).

    E.g. a walk started under v1/h1 cannot be unlocked or completed by a
    v2/h2 caller. No row yet → nothing to check.

    Raises:
        ConfigurationProvenanceError: stored (version, hash) differ.
    """
    row = fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
    if row is None:
        return

    if row.config_hash != config_hash or row.config_version != config_version:
        raise ConfigurationProvenanceError(
            "refusing to modify progress recorded under a different configuration "
            f"provenance (clinician={clinician_id}, patient={patient_id}, "
            f"stored version={row.config_version!r}, write version={config_version!r})"
        )


def fetch_all(conn: sqlite3.Connection) -> dict[tuple[str, str], Progress]:
    """Every walk frontier keyed by ``(clinician_id, patient_id)``.

    Query order is deterministic, so dict insertion order is deterministic too.
    """
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM progress ORDER BY clinician_id, patient_id"
    ).fetchall()
    result: dict[tuple[str, str], Progress] = {}
    for row in rows:
        item = _row_to_progress(row)
        result[(item.clinician_id, item.patient_id)] = item
    return result


def fetch(conn: sqlite3.Connection, *, clinician_id: str, patient_id: str) -> Progress | None:
    """Return the pair's row, or ``None`` when the walk has not started."""
    row = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM progress WHERE clinician_id = ? AND patient_id = ?",
        (clinician_id, patient_id),
    ).fetchone()
    return None if row is None else _row_to_progress(row)


def unlock(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    from_t_index: int,
    to_t_index: int,
    config_hash: str,
    config_version: str | None = None,
    app_state: Any = None,
    commit: bool = True,
) -> bool:
    """Move the frontier ``from_t_index → to_t_index`` iff it still sits at ``from_t_index``.

    Returns ``True`` when the frontier moved. ``False`` means the observed
    frontier was stale (someone else moved it) — nothing is written.
    A missing row counts as a frontier at 0, so ``from_t_index == 0`` may
    insert; ``config_hash`` is recorded only on that first insert.

    S10: ``commit=False`` leaves the write in the connection's open
    transaction and defers the write-counter bump; ``web/gating.py`` uses
    this to commit the unlock and the ``timepoint.exit`` event atomically
    (a failed event write rolls the frontier move back with it).
    """
    _require_version_provenance(conn, config_version)
    _require_row_provenance(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        config_hash=config_hash,
        config_version=config_version,
    )
    cursor = conn.execute(
        "UPDATE progress SET unlocked_t_index = ?, updated_at = CURRENT_TIMESTAMP "
        "WHERE clinician_id = ? AND patient_id = ? AND unlocked_t_index = ?",
        (to_t_index, clinician_id, patient_id, from_t_index),
    )
    moved = cursor.rowcount == 1

    if not moved and from_t_index == 0:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO progress "
            "(clinician_id, patient_id, unlocked_t_index, config_hash, config_version) "
            "VALUES (?, ?, ?, ?, ?)",
            (clinician_id, patient_id, to_t_index, config_hash, config_version),
        )
        moved = cursor.rowcount == 1

    if commit:
        conn.commit()
    if moved and commit:
        _bump(app_state)
    return moved


def mark_complete(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    unlocked_t_index: int,
    config_hash: str,
    config_version: str | None = None,
    app_state: Any = None,
    commit: bool = True,
) -> None:
    """Set ``completed_at`` once (upsert; a second call leaves the first timestamp).

    S10: ``commit=False`` defers commit + bump so the final advance's
    ``advance.ok`` / ``timepoint.exit`` / ``session.end`` ride the same
    transaction (see ``web/gating.py``).
    """
    _require_version_provenance(conn, config_version)
    _require_row_provenance(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        config_hash=config_hash,
        config_version=config_version,
    )
    conn.execute(
        "INSERT INTO progress "
        "(clinician_id, patient_id, unlocked_t_index, completed_at, config_hash, config_version) "
        "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?) "
        "ON CONFLICT(clinician_id, patient_id) DO UPDATE SET "
        "completed_at = COALESCE(progress.completed_at, CURRENT_TIMESTAMP), "
        "updated_at = CURRENT_TIMESTAMP",
        (clinician_id, patient_id, unlocked_t_index, config_hash, config_version),
    )
    if commit:
        conn.commit()
        _bump(app_state)


def reset(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_id: str,
    to_t_index: int,
    app_state: Any = None,
) -> int:
    """Rewind the frontier to ``to_t_index`` and re-open a completed walk.

    Operator path (``ehr-simulator reset-progress``). Returns the rowcount:
    0 means the pair has no walk to rewind.
    """
    cursor = conn.execute(
        "UPDATE progress SET unlocked_t_index = ?, completed_at = NULL, "
        "updated_at = CURRENT_TIMESTAMP WHERE clinician_id = ? AND patient_id = ?",
        (to_t_index, clinician_id, patient_id),
    )
    conn.commit()
    if cursor.rowcount > 0:
        _bump(app_state)
    return cursor.rowcount


def list_for_clinician(conn: sqlite3.Connection, clinician_id: str) -> dict[str, Progress]:
    """Every walk this clinician has started, keyed by ``patient_id``."""
    rows = conn.execute(
        f"SELECT {_SELECT_COLUMNS} FROM progress WHERE clinician_id = ?",
        (clinician_id,),
    ).fetchall()
    return {row[1]: _row_to_progress(row) for row in rows}
