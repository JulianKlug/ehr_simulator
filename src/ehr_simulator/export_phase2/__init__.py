"""S11n linked Phase 2 research export: build and validate one bundle.

Spec: ``specs/session-11n-phase2-exports.md``.

Pure over one read snapshot: everything is read inside a single
``BEGIN … ROLLBACK``, validated, and returned as in-memory tables; file
output lives in ``export_bundle.py``. Every case is interpreted under the
configuration snapshot it was activated with, never the active one::

    configuration_history ─► registry {version: (hash, study, questions)}
    schedules ─┬─► randomisation_audit.csv  (planned vs realised, replacements)
    arm_assignments (phase2_randomized) ─► one CaseRecord per case
               │     S10 timing · S11j foreground/active · S11k panels
               │     S11l delivery / viewed / PP / missing responses
               ├─► timepoints.csv · answers.csv · panel_summaries.csv
               └─► behavioral_events.csv (raw source rows)
    configuration_history.csv · configuration_counts.csv · clinicians.csv (S11p)

Module layout::

    __init__ ── build_phase2_bundle / _build (one snapshot → Phase2Bundle)
      ├─ model            Phase2Bundle · Table · Phase2ExportError · constants
      ├─ headers          CSV headers · linked id column names
      ├─ cells            _bool _num _ts … (value → cell string)
      ├─ registry         PinnedConfig · _registry · _pinned
      ├─ cases            CaseRecord · _case_record
      ├─ tables/          timepoints · answers · panels · events · clinicians
      ├─ audit            randomisation_audit.csv
      ├─ history          configuration_history.csv · configuration_counts.csv
      ├─ practice_tables  practice_*.csv (opt in)
      └─ pseudonyms       linked ids → keyed pseudonyms, after validation

The exporter derives nothing of its own: every research value comes from
``timing``, ``behavioral_timing``, ``panel_exposure`` or ``study_variables``.
Provenance and linkage failures raise :class:`Phase2ExportError`; S11l
integrity warnings are exported in ``integrity_warnings``. No ``web`` import.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from ehr_simulator import timing
from ehr_simulator.db import (
    arm_assignments,
    clinicians,
    config_history,
    events,
    practice,
    replacements,
    sessions,
    study_identity,
)
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import migrations as migrations_dao
from ehr_simulator.db import randomisation as schedules_dao
from ehr_simulator.db.observation import ObservationMode
from ehr_simulator.export_phase2.audit import _audit_rows
from ehr_simulator.export_phase2.cases import _case_record
from ehr_simulator.export_phase2.headers import (
    ANSWERS_HEADER,
    AUDIT_HEADER,
    CLINICIAN_COLUMN,
    CLINICIANS_HEADER,
    COUNTS_HEADER,
    EVENT_ORDER_COLUMN,
    EVENTS_HEADER,
    HISTORY_HEADER,
    LINKED_ID_COLUMNS,
    LINKED_ID_PAYLOAD_KEYS,
    PANELS_HEADER,
    PAYLOAD_COLUMN,
    PRACTICE_ANSWERS_HEADER,
    PRACTICE_TIMEPOINTS_HEADER,
    PROFILE_COMPLETE,
    PROFILE_MISSING,
    TIMEPOINTS_HEADER,
)
from ehr_simulator.export_phase2.history import _count_rows, _history_order, _history_rows
from ehr_simulator.export_phase2.model import (
    EXPORT_SCHEMA_VERSION,
    FALSE,
    FREE_TEXT,
    LIST_SEPARATOR,
    NOT_CONFIGURED,
    PRIMARY,
    RESEARCH_EVENT_PREFIXES,
    REVISIT,
    TRUE,
    KeyfileRequest,
    Phase2Bundle,
    Phase2ExportError,
    PracticeExport,
    Table,
)
from ehr_simulator.export_phase2.practice_tables import _practice_tables
from ehr_simulator.export_phase2.pseudonyms import _pseudonymized
from ehr_simulator.export_phase2.registry import PinnedConfig, _registry
from ehr_simulator.export_phase2.tables.answers import _answers_rows
from ehr_simulator.export_phase2.tables.clinicians import _clinician_rows
from ehr_simulator.export_phase2.tables.events import _event_rows
from ehr_simulator.export_phase2.tables.panels import _panel_rows
from ehr_simulator.export_phase2.tables.timepoints import _timepoint_rows

__all__ = [
    "ANSWERS_HEADER",
    "AUDIT_HEADER",
    "CLINICIANS_HEADER",
    "CLINICIAN_COLUMN",
    "COUNTS_HEADER",
    "EVENTS_HEADER",
    "EVENT_ORDER_COLUMN",
    "EXPORT_SCHEMA_VERSION",
    "FALSE",
    "FREE_TEXT",
    "HISTORY_HEADER",
    "LINKED_ID_COLUMNS",
    "LINKED_ID_PAYLOAD_KEYS",
    "LIST_SEPARATOR",
    "NOT_CONFIGURED",
    "PANELS_HEADER",
    "PAYLOAD_COLUMN",
    "PRACTICE_ANSWERS_HEADER",
    "PRACTICE_TIMEPOINTS_HEADER",
    "PRIMARY",
    "PROFILE_COMPLETE",
    "PROFILE_MISSING",
    "RESEARCH_EVENT_PREFIXES",
    "REVISIT",
    "TIMEPOINTS_HEADER",
    "TRUE",
    "KeyfileRequest",
    "Phase2Bundle",
    "Phase2ExportError",
    "PinnedConfig",
    "PracticeExport",
    "Table",
    "build_phase2_bundle",
]


def build_phase2_bundle(
    conn: sqlite3.Connection,
    *,
    study_id: str,
    pseudonym_secret: bytes,
    practice_export: PracticeExport = PracticeExport.EXCLUDE,
    keyfile: KeyfileRequest = KeyfileRequest.NONE,
) -> Phase2Bundle:
    """Read, validate and derive the whole bundle from one snapshot.

    Every linked id (``LINKED_ID_COLUMNS``, payload ids, keyfile ids) is
    exported as ``pseudonymize(pseudonym_secret, id)``; DB ids never leave.

    Raises:
        Phase2ExportError: any provenance or linkage failure (nothing to publish).
    """
    if conn.in_transaction:
        raise Phase2ExportError("build_phase2_bundle takes its own snapshot; no open transaction")

    conn.execute("BEGIN")
    try:
        bundle = _build(conn, study_id, practice_export, keyfile)
    finally:
        conn.rollback()
    return _pseudonymized(bundle, pseudonym_secret)


def _schema_version(conn: sqlite3.Connection) -> int:
    value = migrations_dao.schema_version(conn)
    if value is None:
        raise Phase2ExportError("the database has no schema version")
    return value


def _build(
    conn: sqlite3.Connection,
    study_id: str,
    practice_export: PracticeExport,
    keyfile: KeyfileRequest,
) -> Phase2Bundle:
    stored_id = study_identity.fetch(conn)
    if stored_id != study_id:
        raise Phase2ExportError(f"database belongs to study {stored_id!r}, not {study_id!r}")
    schema_version = _schema_version(conn)

    history = config_history.list_all(conn)
    registry = _registry(history, study_id)

    foreign = schedules_dao.count_foreign_schedules(conn, study_id)
    if foreign:
        raise Phase2ExportError(f"{foreign} randomisation schedule(s) belong to another study")
    schedules = schedules_dao.list_schedules(conn, study_id)
    assignments = arm_assignments.list_phase2(conn)
    lifecycles = lifecycle_dao.list_all(conn)
    plans = replacements.list_all(conn)
    audit = _audit_rows(study_id, schedules, registry, assignments, lifecycles, plans)

    all_sessions = {s.session_id: s for s in sessions.list_all(conn)}
    sessions_by_pair: dict[tuple[str, str], list[sessions.SessionRow]] = defaultdict(list)
    for s in all_sessions.values():
        if s.observation_mode == ObservationMode.MEASURED:
            sessions_by_pair[(s.clinician_id, s.patient_id)].append(s)
    timing_events = timing.fetch_timing_events(conn)

    cases = sorted(
        (
            _case_record(
                conn,
                a,
                registry,
                lifecycles.get((a.clinician_id, a.patient_id)),
                sessions_by_pair.get((a.clinician_id, a.patient_id), []),
                timing_events,
            )
            for a in assignments
        ),
        key=lambda c: c.sort_key,
    )
    by_key = {c.key: c for c in cases}

    answers_by_case = {c.key: _answers_rows(study_id, c) for c in cases}
    event_rows = _event_rows(
        study_id, events.list_by_prefix(conn, RESEARCH_EVENT_PREFIXES), by_key, all_sessions
    )

    tables = [
        Table(
            "timepoints.csv",
            TIMEPOINTS_HEADER,
            tuple(r for c in cases for r in _timepoint_rows(study_id, c)),
        ),
        Table(
            "answers.csv", ANSWERS_HEADER, tuple(r for c in cases for r in answers_by_case[c.key])
        ),
        Table(
            "panel_summaries.csv",
            PANELS_HEADER,
            tuple(r for c in cases for r in _panel_rows(study_id, c)),
        ),
        Table("behavioral_events.csv", EVENTS_HEADER, tuple(event_rows)),
        Table("randomisation_audit.csv", AUDIT_HEADER, tuple(audit)),
        Table("configuration_history.csv", HISTORY_HEADER, tuple(_history_rows(study_id, history))),
        Table(
            "configuration_counts.csv",
            COUNTS_HEADER,
            tuple(_count_rows(study_id, history, schedules, cases, answers_by_case)),
        ),
    ]

    clinician_ids = {c.assignment.clinician_id for c in cases}
    clinician_ids |= {s.schedule.clinician_id for s in schedules}
    included = practice_export is PracticeExport.INCLUDE
    if included:
        practice_cases = practice.list_all(conn)
        p_timepoints, p_answers = _practice_tables(
            conn, study_id, practice_cases, registry, timing_events
        )
        tables.append(
            Table("practice_timepoints.csv", PRACTICE_TIMEPOINTS_HEADER, tuple(p_timepoints))
        )
        tables.append(Table("practice_answers.csv", PRACTICE_ANSWERS_HEADER, tuple(p_answers)))
        clinician_ids |= {p.clinician_id for p in practice_cases}

    ids = tuple(sorted(clinician_ids))
    tables.insert(
        len(tables) - (2 if included else 0),
        Table("clinicians.csv", CLINICIANS_HEADER, tuple(_clinician_rows(conn, study_id, ids))),
    )
    keyfile_rows = None
    if keyfile is KeyfileRequest.REQUESTED:
        keyfile_rows = tuple(clinicians.fetch_by_ids(conn, ids)) if ids else ()
        if {cid for cid, _ in keyfile_rows} != set(ids):
            raise Phase2ExportError("clinicians lookup did not round-trip for the keyfile")

    return Phase2Bundle(
        study_id=study_id,
        source_schema_version=schema_version,
        config_versions=tuple(r.config_version for r in _history_order(history)),
        practice_included=included,
        tables=tuple(tables),
        clinician_ids=ids,
        keyfile_rows=keyfile_rows,
    )
