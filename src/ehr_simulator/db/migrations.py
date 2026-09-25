"""SQLite schema migrations runner.

``MIGRATIONS`` is a module-level tuple of :class:`Migration` namedtuples
ordered by ``version``. ``apply_migrations`` filters out already-applied
versions, executes the rest, and records each in the ``schema_migrations``
table. Idempotent (no-op when no pending migrations).

Atomicity note: :meth:`sqlite3.Connection.executescript` issues implicit
COMMIT both on entry and exit, so wrapping it in ``with conn:`` does NOT
give transactional rollback. Instead, every CREATE statement in the DDL
uses ``IF NOT EXISTS`` and column additions go through
``Migration.add_columns`` (skipped when already present). A mid-DDL crash
leaves a partial schema that a subsequent ``apply_migrations`` call cleanly extends — the
``schema_migrations`` row is the source-of-truth lock (per review-fix R2).
"""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

from ehr_simulator.logging import get_logger


class Migration(NamedTuple):
    version: int
    name: str
    up_sql: str
    # (table, column, declaration) added only when missing: SQLite has no
    # ``ADD COLUMN IF NOT EXISTS``, so a raw ALTER would wedge the retry.
    add_columns: tuple[tuple[str, str, str], ...] = ()
    # DDL that references ``add_columns`` (indexes, triggers): runs after them.
    post_sql: str = ""


_INITIAL_DDL = """
CREATE TABLE IF NOT EXISTS clinicians (
    clinician_id     TEXT PRIMARY KEY,
    name_normalized  TEXT NOT NULL UNIQUE,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    TEXT PRIMARY KEY,
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at      TIMESTAMP,
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS arm_assignments (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    arm           TEXT NOT NULL,
    arm_source    TEXT NOT NULL,
    seed          INTEGER,
    assigned_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_hash   TEXT NOT NULL,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE IF NOT EXISTS answers (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    timepoint     REAL NOT NULL,
    question_id   TEXT NOT NULL,
    value         TEXT NOT NULL,
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    ts_recorded   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ux_answers_cell UNIQUE (clinician_id, patient_id, timepoint, question_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT REFERENCES sessions(session_id),
    clinician_id   TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id     TEXT,
    timepoint      REAL,
    kind           TEXT NOT NULL,
    payload_json   TEXT NOT NULL DEFAULT '{}',
    client_ts      TIMESTAMP,
    server_ts      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    client_seq     INTEGER
);

CREATE TABLE IF NOT EXISTS ingestion_issues (
    boot_id      TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    patient_id   TEXT,
    row_idx      INTEGER,
    reason       TEXT NOT NULL,
    loaded_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_events_session_id        ON events (session_id);
CREATE INDEX IF NOT EXISTS ix_events_patient_timepoint ON events (patient_id, timepoint);
CREATE INDEX IF NOT EXISTS ix_answers_patient_clinician ON answers (patient_id, clinician_id);
CREATE INDEX IF NOT EXISTS ix_arm_clinician_patient
    ON arm_assignments (clinician_id, patient_id);
CREATE INDEX IF NOT EXISTS ix_ingestion_issues_boot_id ON ingestion_issues (boot_id);
"""


# S9a: the service layer makes ``sessions.start_or_resume`` a per-request
# check-then-insert. The partial unique index turns "one open session per
# (clinician, patient)" from reviewer discipline into a schema invariant.
_SESSIONS_OPEN_UNIQUE_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open
    ON sessions (clinician_id, patient_id) WHERE ended_at IS NULL;
"""


# S9b: the per-(clinician, patient) walk frontier. ``unlocked_t_index`` is
# the highest study ``t_index`` the clinician may view (0 = not started);
# ``completed_at`` is set once by the final advance; ``config_hash`` is the
# hash the walk *started* under (never overwritten, so drift stays visible).
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_PROGRESS_DDL = """
CREATE TABLE IF NOT EXISTS progress (
    clinician_id      TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id        TEXT NOT NULL,
    unlocked_t_index  INTEGER NOT NULL DEFAULT 0,
    completed_at      TIMESTAMP,
    config_hash       TEXT NOT NULL,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (clinician_id, patient_id)
);
"""


# S11a: one database == one study. ``study_identity`` is a singleton table —
# the CHECK on ``singleton`` allows at most one row (always 1). Migration 4
# creates ONLY this table: no ALTER of existing tables, no new columns or
# indexes elsewhere (spec 14). Populated by ``db.study_identity.bind``
# on a fresh, empty database; read-verified by ``db.study_identity.require``.
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_STUDY_IDENTITY_DDL = """
CREATE TABLE IF NOT EXISTS study_identity (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    study_id    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


# S11b: configuration version history + per-case provenance.
# ``configuration_history`` is the append-only register of explicit
# activations (one row per ``config_version``) carrying the immutable study
# and question snapshots; ``active_configuration`` is a two-column singleton
# pointing at exactly one registered version. The four case tables gain a
# nullable ``config_version`` (nullable only for S11a migration
# compatibility — the DAOs refuse a NULL once history exists). Existing
# ``config_hash`` values are never rewritten and gain no uniqueness.
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_S11B_CONFIG_HISTORY = """
CREATE TABLE IF NOT EXISTS configuration_history (
    config_version      TEXT PRIMARY KEY,
    study_id            TEXT NOT NULL REFERENCES study_identity(study_id),
    config_hash         TEXT NOT NULL,
    activated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    change_description  TEXT NOT NULL,
    change_reason       TEXT,
    study_json          TEXT NOT NULL,
    questions_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS active_configuration (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_version  TEXT NOT NULL REFERENCES configuration_history(config_version)
);
"""

_S11B_PROVENANCE_COLUMNS = tuple(
    (table, "config_version", "TEXT")
    for table in ("arm_assignments", "sessions", "progress", "answers")
)


# S11c: one immutable planned schedule per clinician (UNIQUE study/clinician)
# plus its ordered items. ``schedule_id`` is the SHA256 of the canonical
# generation context, so the same inputs always name the same schedule.
# Planned only: no activation, completion or replacement columns.
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_S11C_RANDOMISATION_DDL = """
CREATE TABLE IF NOT EXISTS randomisation_schedules (
    schedule_id                 TEXT PRIMARY KEY,
    study_id                    TEXT NOT NULL,
    clinician_id                TEXT NOT NULL,
    generated_at                TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_version              TEXT NOT NULL,
    config_hash                 TEXT NOT NULL,
    algorithm_version           TEXT NOT NULL,
    master_seed                 INTEGER NOT NULL,
    derived_seed_hex            TEXT NOT NULL,
    allocation_state_json       TEXT NOT NULL,
    starting_ai_count           INTEGER NOT NULL,
    starting_no_ai_count        INTEGER NOT NULL,
    starting_arm                TEXT NOT NULL CHECK (starting_arm IN ('ai', 'no_ai')),
    block_length                INTEGER NOT NULL,
    block_sequence_json         TEXT NOT NULL,
    UNIQUE (study_id, clinician_id)
);

CREATE TABLE IF NOT EXISTS randomisation_schedule_items (
    schedule_id                 TEXT NOT NULL REFERENCES randomisation_schedules(schedule_id),
    case_position               INTEGER NOT NULL,
    patient_id                  TEXT NOT NULL,
    planned_arm                 TEXT NOT NULL CHECK (planned_arm IN ('ai', 'no_ai')),
    block_number                INTEGER NOT NULL,
    position_in_block           INTEGER NOT NULL,
    preceding_block_arm         TEXT,
    planned_cases_since_ai      INTEGER,
    assignment_seed             INTEGER NOT NULL,
    PRIMARY KEY (schedule_id, case_position),
    UNIQUE (schedule_id, patient_id)
);
"""


# S11d: realised Phase 2 activation provenance on ``arm_assignments``.
# ``activated_at`` (non-NULL only for an explicit Start case) is distinct from
# ``assigned_at`` (defaulted on every row, phase1_stub included). SQLite's
# ALTER cannot add a CHECK, so triggers enforce completeness + immutability.
_S11D_ACTIVATION_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("arm_assignments", "schedule_id", "TEXT"),
    ("arm_assignments", "case_position", "INTEGER"),
    ("arm_assignments", "activated_at", "TIMESTAMP"),
)

_S11D_ACTIVATION_DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_arm_schedule_position
    ON arm_assignments (schedule_id, case_position) WHERE schedule_id IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_arm_phase2_complete
BEFORE INSERT ON arm_assignments
WHEN NEW.arm_source = 'phase2_randomized'
 AND (NEW.schedule_id IS NULL OR NEW.case_position IS NULL
      OR NEW.activated_at IS NULL OR NEW.seed IS NULL
      OR NEW.config_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignment requires activation provenance');
END;

CREATE TRIGGER IF NOT EXISTS trg_arm_phase2_immutable
BEFORE UPDATE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_arm_phase2_no_delete
BEFORE DELETE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;
"""


# S11e: lifecycle state of every realised Phase 2 case. Timestamps come from
# the service's injected clock (no defaults). The CHECKs tie each state to its
# timestamp (``paused_at`` survives a pause timeout as evidence); triggers
# make ``completed``/``incomplete`` rows immutable and forbid deletes. The
# backfill copies S11d cases without inventing pause/incomplete history:
# completed progress → ``completed``, anything else → ``active`` last seen at
# its latest recorded contact.
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_S11E_CASE_LIFECYCLE_DDL = """
CREATE TABLE IF NOT EXISTS case_lifecycle (
    clinician_id       TEXT NOT NULL,
    patient_id         TEXT NOT NULL,
    state              TEXT NOT NULL
        CHECK (state IN ('active', 'paused', 'completed', 'incomplete')),
    state_changed_at   TIMESTAMP NOT NULL,
    last_seen_at       TIMESTAMP NOT NULL,
    paused_at          TIMESTAMP,
    completed_at       TIMESTAMP,
    incomplete_at      TIMESTAMP,
    incomplete_reason  TEXT
        CHECK (incomplete_reason IS NULL OR incomplete_reason IN
               ('reconnection_timeout', 'pause_timeout', 'operator_abandoned')),
    PRIMARY KEY (clinician_id, patient_id),
    FOREIGN KEY (clinician_id, patient_id)
        REFERENCES arm_assignments(clinician_id, patient_id),
    CHECK (state <> 'paused' OR paused_at IS NOT NULL),
    CHECK ((state = 'completed') = (completed_at IS NOT NULL)),
    CHECK ((state = 'incomplete') = (incomplete_at IS NOT NULL AND incomplete_reason IS NOT NULL))
);

CREATE TRIGGER IF NOT EXISTS trg_case_lifecycle_terminal
BEFORE UPDATE ON case_lifecycle
WHEN OLD.state IN ('completed', 'incomplete')
BEGIN
    SELECT RAISE(ABORT, 'completed and incomplete cases are terminal');
END;

CREATE TRIGGER IF NOT EXISTS trg_case_lifecycle_no_delete
BEFORE DELETE ON case_lifecycle
BEGIN
    SELECT RAISE(ABORT, 'case lifecycle rows are permanent');
END;

INSERT OR IGNORE INTO case_lifecycle
    (clinician_id, patient_id, state, state_changed_at, last_seen_at, completed_at)
SELECT
    a.clinician_id,
    a.patient_id,
    CASE WHEN p.completed_at IS NOT NULL THEN 'completed' ELSE 'active' END,
    COALESCE(p.completed_at, a.activated_at),
    MAX(a.activated_at, COALESCE(
        (SELECT MAX(e.server_ts) FROM events e
          WHERE e.clinician_id = a.clinician_id AND e.patient_id = a.patient_id),
        a.activated_at)),
    p.completed_at
FROM arm_assignments a
LEFT JOIN progress p
       ON p.clinician_id = a.clinician_id AND p.patient_id = a.patient_id
WHERE a.arm_source = 'phase2_randomized' AND a.activated_at IS NOT NULL;
"""


# S11f: one immutable replacement plan per incomplete original case. The
# plan names an existing schedule item (FK) realised earlier than planned;
# ``activated_at`` is set exactly once by Start case. Triggers refuse deletes
# and any other update. Timestamps come from the injected clock.
# No inline SQL comments: sqlite_master stores the DDL verbatim and the
# schema-snapshot test compares it byte-for-byte.
_S11F_CASE_REPLACEMENTS_DDL = """
CREATE TABLE IF NOT EXISTS case_replacements (
    replacement_id              TEXT PRIMARY KEY,
    clinician_id                TEXT NOT NULL,
    original_patient_id         TEXT NOT NULL,
    replacement_patient_id      TEXT NOT NULL,
    replacement_schedule_id     TEXT NOT NULL,
    replacement_case_position   INTEGER NOT NULL,
    planned_arm                 TEXT NOT NULL CHECK (planned_arm IN ('ai', 'no_ai')),
    generated_at                TIMESTAMP NOT NULL,
    activated_at                TIMESTAMP,
    UNIQUE (clinician_id, original_patient_id),
    UNIQUE (clinician_id, replacement_patient_id),
    FOREIGN KEY (clinician_id, original_patient_id)
        REFERENCES arm_assignments(clinician_id, patient_id),
    FOREIGN KEY (replacement_schedule_id, replacement_case_position)
        REFERENCES randomisation_schedule_items(schedule_id, case_position)
);

CREATE TRIGGER IF NOT EXISTS trg_case_replacements_activate_once
BEFORE UPDATE ON case_replacements
WHEN OLD.activated_at IS NOT NULL
  OR NEW.activated_at IS NULL
  OR NEW.replacement_id IS NOT OLD.replacement_id
  OR NEW.clinician_id IS NOT OLD.clinician_id
  OR NEW.original_patient_id IS NOT OLD.original_patient_id
  OR NEW.replacement_patient_id IS NOT OLD.replacement_patient_id
  OR NEW.replacement_schedule_id IS NOT OLD.replacement_schedule_id
  OR NEW.replacement_case_position IS NOT OLD.replacement_case_position
  OR NEW.planned_arm IS NOT OLD.planned_arm
  OR NEW.generated_at IS NOT OLD.generated_at
BEGIN
    SELECT RAISE(ABORT, 'replacement plans are immutable; activated_at is set once');
END;

CREATE TRIGGER IF NOT EXISTS trg_case_replacements_no_delete
BEFORE DELETE ON case_replacements
BEGIN
    SELECT RAISE(ABORT, 'replacement plans are permanent');
END;
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial", up_sql=_INITIAL_DDL),
    Migration(version=2, name="sessions_open_unique", up_sql=_SESSIONS_OPEN_UNIQUE_DDL),
    Migration(version=3, name="progress", up_sql=_PROGRESS_DDL),
    Migration(version=4, name="study_identity", up_sql=_STUDY_IDENTITY_DDL),
    Migration(
        version=5,
        name="s11b_config_version_history",
        up_sql=_S11B_CONFIG_HISTORY,
        add_columns=_S11B_PROVENANCE_COLUMNS,
    ),
    Migration(version=6, name="s11c_randomisation_schedules", up_sql=_S11C_RANDOMISATION_DDL),
    Migration(
        version=7,
        name="s11d_case_activation",
        up_sql="",
        add_columns=_S11D_ACTIVATION_COLUMNS,
        post_sql=_S11D_ACTIVATION_DDL,
    ),
    Migration(version=8, name="s11e_case_lifecycle", up_sql=_S11E_CASE_LIFECYCLE_DDL),
    Migration(version=9, name="s11f_case_replacements", up_sql=_S11F_CASE_REPLACEMENTS_DDL),
)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    """Retry-safe ``ALTER TABLE … ADD COLUMN`` (identifiers are module constants)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing:
        return

    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration; return the list of versions applied.

    Idempotent: a second call after the first has no effect and returns ``[]``.
    Logs ``event_kind="db.migrate.noop"`` on no-op and
    ``event_kind="db.migrate.applied"`` once per migration on a forward pass.
    """
    log = get_logger()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.commit()
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    pending = [m for m in MIGRATIONS if m.version not in applied]
    if not pending:
        log.info("migrations noop", event_kind="db.migrate.noop")
        return []
    versions: list[int] = []
    for m in pending:
        conn.executescript(m.up_sql)
        for table, column, declaration in m.add_columns:
            _add_column_if_missing(conn, table, column, declaration)
        if m.post_sql:
            conn.executescript(m.post_sql)
        conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (m.version, m.name),
        )
        conn.commit()
        log.info(
            "migration applied",
            event_kind="db.migrate.applied",
            version=m.version,
            name=m.name,
        )
        versions.append(m.version)
    return versions
