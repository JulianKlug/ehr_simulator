"""SQLite schema migrations runner.

``MIGRATIONS`` is a module-level tuple of :class:`Migration` namedtuples
ordered by ``version``. ``apply_migrations`` filters out already-applied
versions, executes the rest, and records each in the ``schema_migrations``
table. Idempotent (no-op when no pending migrations).

Atomicity note: :meth:`sqlite3.Connection.executescript` issues implicit
COMMIT both on entry and exit, so wrapping it in ``with conn:`` does NOT
give transactional rollback. Instead, every CREATE statement in the DDL
uses ``IF NOT EXISTS``. A mid-DDL crash leaves a partial schema that a
subsequent ``apply_migrations`` call cleanly extends — the
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

ALTER TABLE arm_assignments ADD COLUMN config_version TEXT;
ALTER TABLE sessions       ADD COLUMN config_version TEXT;
ALTER TABLE progress       ADD COLUMN config_version TEXT;
ALTER TABLE answers        ADD COLUMN config_version TEXT;
"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial", up_sql=_INITIAL_DDL),
    Migration(version=2, name="sessions_open_unique", up_sql=_SESSIONS_OPEN_UNIQUE_DDL),
    Migration(version=3, name="progress", up_sql=_PROGRESS_DDL),
    Migration(version=4, name="study_identity", up_sql=_STUDY_IDENTITY_DDL),
    Migration(version=5, name="s11b_config_version_history", up_sql=_S11B_CONFIG_HISTORY),
)


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
