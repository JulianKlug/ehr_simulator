"""Swap every linked id for its keyed pseudonym after validation."""

from __future__ import annotations

import json
from dataclasses import replace

from ehr_simulator.export_phase2.cells import _canonical_json
from ehr_simulator.export_phase2.headers import (
    CLINICIAN_COLUMN,
    EVENT_ORDER_COLUMN,
    LINKED_ID_COLUMNS,
    LINKED_ID_PAYLOAD_KEYS,
    PAYLOAD_COLUMN,
)
from ehr_simulator.export_phase2.model import Phase2Bundle, Table
from ehr_simulator.pseudonym import pseudonymize


def _pseudonymized(bundle: Phase2Bundle, secret: bytes) -> Phase2Bundle:
    """Swap every linked id for its keyed pseudonym, after all validation."""
    keyfile_rows = bundle.keyfile_rows
    if keyfile_rows is not None:
        keyfile_rows = tuple((pseudonymize(secret, cid), name) for cid, name in keyfile_rows)

    return replace(
        bundle,
        tables=tuple(_pseudonymized_table(t, secret) for t in bundle.tables),
        clinician_ids=tuple(pseudonymize(secret, cid) for cid in bundle.clinician_ids),
        keyfile_rows=keyfile_rows,
    )


def _pseudonymized_table(table: Table, secret: bytes) -> Table:
    columns = [i for i, name in enumerate(table.header) if name in LINKED_ID_COLUMNS]
    payload = table.header.index(PAYLOAD_COLUMN) if PAYLOAD_COLUMN in table.header else None
    if not columns and payload is None:
        return table

    rows = []
    for row in table.rows:
        cells = list(row)
        for i in columns:
            if cells[i]:
                cells[i] = pseudonymize(secret, cells[i])
        if payload is not None:
            cells[payload] = _pseudonymized_payload(cells[payload], secret)
        rows.append(tuple(cells))

    # Clinician-first tables were ordered by DB id; that order would let a
    # roster match pseudonyms. Re-order by pseudonym (stable: inner order kept).
    if CLINICIAN_COLUMN in table.header and EVENT_ORDER_COLUMN not in table.header:
        clinician = table.header.index(CLINICIAN_COLUMN)
        rows.sort(key=lambda r: r[clinician])
    return Table(table.name, table.header, tuple(rows))


def _pseudonymized_payload(raw: str, secret: bytes) -> str:
    """``{"schedule_id": "<sha256>"}`` → ``{"schedule_id": "<pseudonym>"}``."""
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        return raw

    linked = [k for k in LINKED_ID_PAYLOAD_KEYS & payload.keys() if isinstance(payload[k], str)]
    if not linked:
        return raw

    for key in linked:
        payload[key] = pseudonymize(secret, payload[key])
    return _canonical_json(json.dumps(payload))
