-- Frozen expected schema after ALL migrations (001 "initial", 002
-- "sessions_open_unique", 003 "progress", 004 "study_identity", 005
-- "s11b_config_version_history", 006 "s11c_randomisation_schedules", 007
-- "s11d_case_activation", 008 "s11e_case_lifecycle", 009
-- "s11f_case_replacements"); the
-- filename predates 002. The
-- drift-check test in tests/test_db.py reads sqlite_master.sql (the exact
-- DDL text SQLite stored) sorted by name, joins with ";\n\n", and asserts
-- it equals the contents of this file.
-- Updating this fixture is a deliberate review step — it must move in
-- lockstep with any DDL change in src/ehr_simulator/db/migrations.py
-- (any ``_*_DDL`` string).

CREATE TABLE active_configuration (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_version  TEXT NOT NULL REFERENCES configuration_history(config_version)
);

CREATE TABLE answers (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    timepoint     REAL NOT NULL,
    question_id   TEXT NOT NULL,
    value         TEXT NOT NULL,
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL,
    ts_recorded   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, config_version TEXT,
    CONSTRAINT ux_answers_cell UNIQUE (clinician_id, patient_id, timepoint, question_id)
);

CREATE TABLE arm_assignments (
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    arm           TEXT NOT NULL,
    arm_source    TEXT NOT NULL,
    seed          INTEGER,
    assigned_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    config_hash   TEXT NOT NULL, config_version TEXT, schedule_id TEXT, case_position INTEGER, activated_at TIMESTAMP,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE case_lifecycle (
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

CREATE TABLE case_replacements (
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

CREATE TABLE clinicians (
    clinician_id     TEXT PRIMARY KEY,
    name_normalized  TEXT NOT NULL UNIQUE,
    first_seen_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE configuration_history (
    config_version      TEXT PRIMARY KEY,
    study_id            TEXT NOT NULL REFERENCES study_identity(study_id),
    config_hash         TEXT NOT NULL,
    activated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    change_description  TEXT NOT NULL,
    change_reason       TEXT,
    study_json          TEXT NOT NULL,
    questions_json      TEXT NOT NULL
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
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, config_version TEXT,
    PRIMARY KEY (clinician_id, patient_id)
);

CREATE TABLE randomisation_schedule_items (
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

CREATE TABLE randomisation_schedules (
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

CREATE TABLE schema_migrations ( version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP);

CREATE TABLE sessions (
    session_id    TEXT PRIMARY KEY,
    clinician_id  TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id    TEXT NOT NULL,
    started_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at      TIMESTAMP,
    arm           TEXT NOT NULL,
    config_hash   TEXT NOT NULL
, config_version TEXT);

CREATE TABLE study_identity (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    study_id    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER trg_arm_phase2_complete
BEFORE INSERT ON arm_assignments
WHEN NEW.arm_source = 'phase2_randomized'
 AND (NEW.schedule_id IS NULL OR NEW.case_position IS NULL
      OR NEW.activated_at IS NULL OR NEW.seed IS NULL
      OR NEW.config_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignment requires activation provenance');
END;

CREATE TRIGGER trg_arm_phase2_immutable
BEFORE UPDATE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;

CREATE TRIGGER trg_arm_phase2_no_delete
BEFORE DELETE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;

CREATE TRIGGER trg_case_lifecycle_no_delete
BEFORE DELETE ON case_lifecycle
BEGIN
    SELECT RAISE(ABORT, 'case lifecycle rows are permanent');
END;

CREATE TRIGGER trg_case_lifecycle_terminal
BEFORE UPDATE ON case_lifecycle
WHEN OLD.state IN ('completed', 'incomplete')
BEGIN
    SELECT RAISE(ABORT, 'completed and incomplete cases are terminal');
END;

CREATE TRIGGER trg_case_replacements_activate_once
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

CREATE TRIGGER trg_case_replacements_no_delete
BEFORE DELETE ON case_replacements
BEGIN
    SELECT RAISE(ABORT, 'replacement plans are permanent');
END;

CREATE UNIQUE INDEX ux_arm_schedule_position
    ON arm_assignments (schedule_id, case_position) WHERE schedule_id IS NOT NULL;

CREATE UNIQUE INDEX ux_sessions_open
    ON sessions (clinician_id, patient_id) WHERE ended_at IS NULL;
