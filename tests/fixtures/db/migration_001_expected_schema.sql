-- Frozen expected schema after ALL migrations (001 "initial", 002
-- "sessions_open_unique", 003 "progress", 004 "study_identity"); the
-- filename predates 002. The drift-check test in tests/test_db.py reads
-- sqlite_master.sql (the exact DDL text SQLite stored) sorted by name,
-- joins with ";\n\n", and asserts it equals the contents of this file.
-- Updating this fixture is a deliberate review step — it must move in
-- lockstep with any DDL change in src/ehr_simulator/db/migrations.py
-- (any ``_*_DDL`` string).

CREATE TABLE answers (
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

CREATE TABLE arm_assignments (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    arm           TEXT NOT NULL,
    arm_source    TEXT NOT NULL,
    seed          INTEGER,
    assigned_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_hash   TEXT NOT NULL,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE clinicians (
    clinician_id     TEXT PRIMARY KEY,
    name_normalized  TEXT NOT NULL UNIQUE,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE events (
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

CREATE TABLE ingestion_issues (
    boot_id      TEXT NOT NULL,
    dataset      TEXT NOT NULL,
    patient_id   TEXT,
    row_idx      INTEGER,
    reason       TEXT NOT NULL,
    loaded_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX ix_answers_patient_clinician ON answers (patient_id, clinician_id);

CREATE INDEX ix_arm_clinician_patient
    ON arm_assignments (clinician_id, patient_id);

CREATE INDEX ix_events_patient_timepoint ON events (patient_id, timepoint);

CREATE INDEX ix_events_session_id        ON events (session_id);

CREATE INDEX ix_ingestion_issues_boot_id ON ingestion_issues (boot_id);

CREATE TABLE progress (
    clinician_id      TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id        TEXT NOT NULL,
    unlocked_t_index  INTEGER NOT NULL DEFAULT 0,
    completed_at      TIMESTAMP,
    config_hash       TEXT NOT NULL,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE schema_migrations ( version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at      TIMESTAMP,
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL
);

CREATE TABLE study_identity (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    study_id    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX ux_sessions_open
    ON sessions (clinician_id, patient_id) WHERE ended_at IS NULL;
