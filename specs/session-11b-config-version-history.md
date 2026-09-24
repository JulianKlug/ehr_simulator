# Session 11b — Configuration version history and immutable case provenance

## Goal

Add persistent configuration version history to a study.

Each activated configuration has:

* `config_version`
* `config_hash`
* activation timestamp
* change description
* optional reason
* immutable study and question snapshots

Every case is permanently pinned to the configuration under which it started.

Activating a later configuration affects new cases only.

S11a is assumed complete.

## Core invariants

1. `study_id` identifies the study.
2. `config_version` identifies one explicit configuration activation.
3. `config_hash` identifies the exact semantic configuration.
4. Configuration history is append only.
5. An existing case never changes `config_version` or `config_hash`.
6. Historical cases must continue using their historical questions, patient list and timepoints.
7. Existing `config_hash` values are never rewritten.
8. Configuration changes are never activated implicitly by application boot.

## Locked decisions

| Topic                       | Decision                                                            |
| --------------------------- | ------------------------------------------------------------------- |
| Study schema                | Remains version `"2"`.                                              |
| Questions schema            | Remains version `"1"`.                                              |
| `config_version`            | Stored in the database, not in `study_config.yaml`.                 |
| Format                      | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`                                 |
| Activation                  | Explicit CLI command.                                               |
| Description                 | Required.                                                           |
| Reason                      | Optional.                                                           |
| Same hash under new version | Allowed.                                                            |
| Reusing a version label     | Forbidden except an exact idempotent repeat of the same activation. |
| Dataset                     | `study.dataset` may not change within one `study_id`.               |
| Historical config           | Store canonical study and question JSON in SQLite.                  |
| Active config               | Exactly one version is active.                                      |
| Mixed version exports       | Deferred to S11n.                                                   |

## Database migration

Add migration 5.

Create:

```
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

CREATE TABLE active_configuration (
    singleton       INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_version  TEXT NOT NULL REFERENCES configuration_history(config_version)
);
```

Add nullable `config_version TEXT` to:

* `arm_assignments`
* `sessions`
* `progress`
* `answers`

The columns are nullable only for migration compatibility.

New Phase 2 rows must always contain `config_version`.

Do not make `config_hash` unique.

Update the schema snapshot fixture.

## Configuration snapshots

Persist enough information to reconstruct historical study behaviour.

`study_json` contains the parsed `StudyConfig` excluding only:

* `csv_path`
* `params_dir`
* `db_path`

`questions_json` contains the complete parsed questions model.

Snapshots are immutable.

Loading a stored snapshot must revalidate it through the existing Pydantic models.

Invalid stored snapshots are database integrity errors.

## Configuration history DAO

Add:

`src/ehr_simulator/db/config_history.py`

Required API:

```
fetch_active(conn) -> ConfigHistoryRow | None

fetch_version(
    conn,
    config_version
) -> ConfigHistoryRow | None

list_all(conn) -> tuple[ConfigHistoryRow, ...]

activate(
    conn,
    *,
    study_id,
    config_version,
    config_hash,
    description,
    reason,
    study,
    questions,
) -> ConfigHistoryRow

require_known(
    conn,
    config_version,
    config_hash
) -> ConfigHistoryRow
```

Only this module may write `configuration_history` or `active_configuration`.

`require_known()` must reject:

* unknown version
* known version with the wrong hash

## Explicit configuration activation

Add CLI command:

```
ehr-simulator activate-config STUDY_CONFIG QUESTIONS \
    --version VERSION \
    --description TEXT \
    [--reason TEXT] \
    [--db-path PATH]
```

Activation must:

1. load and validate both configuration files
2. compute the normal `config_hash`
3. resolve the database
4. apply migrations
5. bind or verify `study_id`
6. validate activation metadata
7. validate the dataset invariant
8. store the immutable snapshot
9. update the active configuration
10. commit atomically

Validation:

* `config_version` matches the locked format
* description is nonempty after trimming
* description maximum 500 characters
* reason maximum 1000 characters
* provided reason may not be empty after trimming

Behaviour:

| Existing state                            | Result                |
| ----------------------------------------- | --------------------- |
| new version                               | register and activate |
| same version, same hash and metadata      | no op                 |
| same version, different hash or metadata  | refuse                |
| new version, same hash as earlier version | allowed               |

A rollback must use a new `config_version`.

## Application boot

Study mode boot sequence:

`migrations → study identity → active configuration validation → normal startup`

Boot must refuse if:

* no active configuration exists
* supplied YAML hash differs from the active hash
* active history belongs to another `study_id`
* the persisted snapshot is invalid
* version/hash provenance is inconsistent

Successful boot sets:

```
app.state.config_version
app.state.config_hash
app.state.active_configuration
```

`app.state.study` and `app.state.questions` remain the supplied parsed models.

Their computed hash must equal the active configuration hash.

Bare non study mode remains unchanged.

Application boot must never automatically create a new configuration version.

## Pinned case configuration

Until S11d introduces explicit Start case, `arm_assignments` remains the case provenance anchor.

Add `config_version` to `ArmAssignment`.

For a new clinician/patient assignment:

* use the currently active `config_version`
* use the currently active `config_hash`

For an existing assignment:

* return its stored version/hash
* never replace them with the active version

Add a service operation equivalent to:

```
resolve_case_configuration(
    conn,
    app_state,
    clinician_id,
    patient_id
) -> ConfigurationSnapshot
```

Behaviour:

* existing assignment → historical snapshot for its stored version/hash
* no assignment → currently active snapshot

Study behaviour for an existing case must use the resolved historical snapshot.

This includes:

* patient membership
* timepoints
* questions
* answer validation
* gating

Do not use the current global config for those decisions once a case exists.

## Provenance on existing tables

### `arm_assignments`

Store immutable:

* `config_version`
* `config_hash`

Never overwrite either.

### `sessions`

Store both from the case configuration.

On resume, stored session version/hash must match the case assignment.

Mismatch is an integrity error.

### `progress`

Store both on initial creation.

Later unlock, completion and reset operations must not change them.

Progress provenance must match the case assignment.

### `answers`

Store both from the case configuration.

Answer upsert may update normal answer fields but must never update:

* `config_version`
* `config_hash`

If an existing answer has different provenance from the case configuration, reject the write.

`saved_answers()` must validate expected version and hash.

A mismatch is an integrity error, not a warning.

## Historical case behaviour

After activating a new configuration, an unfinished older case must continue using:

* its historical patient membership
* its historical timepoints
* its historical questions
* its historical configuration hash/version

A patient already assigned under an older version must remain accessible even if removed from the active `patient_ids`.

For the transitional S11b index:

1. show active configuration patients in configured order
2. append existing assigned patients that are absent from the active version

S11c/S11d may later replace this index behaviour.

## S11a database upgrade

S11b must support valid S11a databases.

When configuration history is empty, first activation may inspect existing provenance.

Collect distinct `config_hash` values from:

* `arm_assignments`
* `sessions`
* `progress`
* `answers`

Rules:

| Existing hashes                    | Behaviour                                   |
| ---------------------------------- | ------------------------------------------- |
| none                               | activate normally                           |
| exactly one and matches new config | activate and backfill only `config_version` |
| anything else                      | refuse                                      |

Never rewrite an existing hash.

After configuration history exists, a provenance row missing `config_version` is an integrity error.

Do not guess its version.

## Runtime stale server guard

Before creating a new case, verify that the database active version/hash still equals:

```
app.state.config_version
app.state.config_hash
```

If not, refuse creation of the new case and require restart.

Existing pinned cases may continue using their historical snapshots.

## Files expected to change

* `src/ehr_simulator/db/migrations.py`
* `src/ehr_simulator/db/config_history.py`
* `src/ehr_simulator/db/__init__.py`
* `src/ehr_simulator/db/arm_assignments.py`
* `src/ehr_simulator/db/sessions.py`
* `src/ehr_simulator/db/progress.py`
* `src/ehr_simulator/db/answers.py`
* `src/ehr_simulator/config/loader.py` or a dedicated snapshot module
* `src/ehr_simulator/web/app.py`
* `src/ehr_simulator/web/study_session.py`
* `src/ehr_simulator/web/routes.py`
* `src/ehr_simulator/web/answer_capture.py`
* `src/ehr_simulator/web/gating.py`
* `src/ehr_simulator/cli.py`
* `tests/conftest.py`
* database schema fixture
* relevant documentation

## Required tests

### Configuration history

1. First activation stores version, hash, metadata and snapshots.
2. Invalid version or activation metadata is rejected.
3. Repeating the exact activation is idempotent.
4. Reusing a version with changed hash or metadata is rejected.
5. New version with an existing hash is allowed.
6. Previous history rows remain unchanged.
7. Dataset change within one study is refused.

### Application boot

8. Matching active configuration boots.
9. Missing active configuration refuses boot.
10. Active hash mismatch refuses boot.
11. Invalid stored snapshot refuses boot.
12. Bare non study mode remains unchanged.

### Case provenance

13. New case receives active version/hash.
14. Existing case retains its version/hash after later activation.
15. New case after activation receives the new version.
16. Historical questions and timepoints remain in use.
17. Session provenance must match the case.
18. Progress provenance cannot change.
19. Answer upsert cannot change provenance.
20. Answer provenance mismatch refuses the write.
21. Removed but already assigned patient remains accessible.

### Upgrade and runtime integrity

22. Empty S11a DB activates normally.
23. One matching historical hash backfills only `config_version`.
24. Ambiguous or mismatching historical hashes refuse migration.
25. Unknown or missing configuration provenance is refused.
26. Stale server refuses creation of a new case after external activation.

All existing S11a, persistence, gating, answer capture, timing and CI tests must remain green.

## Explicit non goals

S11b does not implement:

* adaptive randomisation
* randomisation seed strategy
* explicit Start case
* case lifecycle
* replacement cases
* conditional questions
* AI intervention delivery
* telemetry
* PP classification
* Phase 2 linked exports
* backup naming

It does not add `config_version` to YAML.

It does not rewrite historical hashes.

It does not automatically activate configurations on server start.

## Acceptance

S11b is complete when:

1. one study can contain multiple immutable configuration versions
2. exactly one version is active
3. activation is explicit and auditable
4. new cases receive the active version/hash
5. existing cases retain their original version/hash
6. historical cases use their historical study and question configuration
7. sessions, progress and answers cannot mutate configuration provenance
8. S11a databases are upgraded only when the mapping is unambiguous
9. invalid or unknown provenance is refused
10. existing `config_hash` values remain untouched
11. the complete test suite and CI pass

S11n remains responsible for final mixed version Phase 2 exports and configuration version counts.

