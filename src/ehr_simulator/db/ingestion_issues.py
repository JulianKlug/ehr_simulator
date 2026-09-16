"""ingestion_issues DAO: persist adapter issues so analyses can audit them.

Called from the lifespan after the dataset loader succeeds. Reads
``app.state.dataset.issues`` (synthetic has no ``.issues`` attribute and
falls through cleanly — review-fix R6).

Every row is tagged with the lifespan-scoped ``boot_id`` so re-recording
across boots doesn't conflate issues from different runs (review-fix R5).
The dedicated index ``ix_ingestion_issues_boot_id`` makes per-boot filters
fast.

All rows for one batch land inside a single transaction via
``executemany`` (P1 perf fix from spec §2).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from ehr_simulator.ingestion.exceptions import IngestionIssue


def record_batch(
    conn: sqlite3.Connection,
    dataset: str,
    boot_id: str,
    issues: Iterable[IngestionIssue],
) -> int:
    """Insert one row per ``IngestionIssue``; return the count."""
    rows = [(boot_id, dataset, issue.patient_id, issue.row_idx, issue.reason) for issue in issues]
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO ingestion_issues "
        "(boot_id, dataset, patient_id, row_idx, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return len(rows)
