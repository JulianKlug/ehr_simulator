# Session 11a — Study identity and database binding

## Goal

Make study identity a first class invariant.

Every configured study has one explicit `study_id`. Every Phase 2 study database is bound to exactly one `study_id`. A database must never be silently reused, adopted, or relabelled for another study.

S11a establishes this foundation only. Randomisation, configuration version history, explicit Start case, Phase 2 exports, backup naming, and all other Phase 2 functionality remain later S11 subsessions.

## Locked decisions

| Topic                                     | Decision                                                                                                                                                                                       |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Study config schema                       | `study_config.yaml` moves from `schema_version: "1"` to `"2"` because `study_id` is a new required field. `questions.yaml` remains schema version `"1"`.                                       |
| `study_id` format                         | Required string matching `^[a-z0-9][a-z0-9_-]{0,63}$`. Lowercase only, maximum 64 characters.                                                                                                  |
| Default study DB                          | `data/study_<study_id>.db`.                                                                                                                                                                    |
| Non study DB                              | Bare `serve` without a study config retains `data/ehr_simulator.db`.                                                                                                                           |
| Explicit DB overrides                     | Existing precedence remains: CLI override → `EHR_SIM_DB_PATH` → `study.db_path` → study specific default. An override does not bypass study identity validation.                               |
| Database identity                         | Persisted in a singleton `study_identity` table.                                                                                                                                               |
| Fresh database                            | May be bound automatically to the configured study at study mode boot.                                                                                                                         |
| Existing matching database                | Opens normally.                                                                                                                                                                                |
| Existing mismatching database             | Refused before any study data are read or written.                                                                                                                                             |
| Legacy nonempty database without identity | Refused. Never infer or backfill its `study_id`.                                                                                                                                               |
| Empty migrated database without identity  | May be claimed by the configured study. `schema_migrations` rows do not make a database nonempty for this rule.                                                                                |
| `config_hash`                             | Existing column and SHA256 representation remain. No `config_hash_v2` column. Schema v2 naturally produces a new hash because `schema_version` and `study_id` are part of the canonical model. |
| Historical hashes                         | Never rewritten or remapped. They remain opaque identifiers of the configuration that generated them.                                                                                          |
| Backup identity                           | Filename and backup policy changes are deferred to S11m.                                                                                                                                       |
| Export shape                              | Adding `study_id` columns to research outputs is deferred to S11n. Study aware export commands must nevertheless verify database identity in S11a.                                             |

## Configuration contract

`StudyConfig` becomes schema version 2 and requires `study_id`.

A minimal valid study configuration is therefore conceptually:

```
schema_version: "2"
study_id: example_synthetic
dataset: synthetic
patient_ids: [synth_001, synth_002, synth_003]
time_unit: minutes
timepoints: [0, 60, 180]
```

`study_id` is operational identity, not a display label. It must be stable for the lifetime of the study.

Changing `study_id` means a different study. It must change the computed `config_hash` and resolve to a different default database.

`load_study_config()` must report a schema mismatch as expected `"2"` when given a v1 study file.

All shipped study configs, fixtures, inline test YAML, documentation examples, and CI inputs must be migrated to study schema v2. Question files are unchanged.

## Configuration hash contract

`compute_config_hash()` and `compute_config_hash_from_models()` retain their current API and SHA256 output.

`study_id` is included in the canonical study payload. The existing deployment exclusions remain exactly:

`csv_path`, `params_dir`, `db_path`

Therefore:

same study definition + same `study_id` → same hash

same definition + different `study_id` → different hash

No historical database row may have its existing `config_hash` rewritten during S11a.

There is no compatibility bridge that interprets a v1 study config as v2. Existing nonempty v1 databases remain legacy databases unless a future explicit migration procedure is specified.

## Database path resolution

Change only the final fallback in `resolve_db_path()`.

For `study is None`:

`data/ehr_simulator.db`

For a configured study without any path override:

`data/study_<study_id>.db`

All existing path precedence and traversal protections remain unchanged.

The following must continue to win over the default study path, in order:

1. explicit CLI `--db-path`
2. `EHR_SIM_DB_PATH`
3. `study.db_path`
4. `data/study_<study_id>.db`

An explicit path changes location only. It never changes which study the database belongs to.

## Database schema

Add migration 4, named `study_identity`.

Schema:

```
CREATE TABLE IF NOT EXISTS study_identity (
    singleton   INTEGER PRIMARY KEY CHECK (singleton = 1),
    study_id    TEXT NOT NULL UNIQUE,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

The singleton key is fixed to `1`. The database can therefore contain at most one study identity.

Update the checked schema fixture used by `test_post_migration_schema_matches_fixture`.

Migration 4 creates the table only. It must never guess or populate a `study_id`, because the migration command has no study configuration.

## Study identity DAO

Add `src/ehr_simulator/db/study_identity.py` and expose it through `db/__init__.py`.

It owns all database identity logic. No route, CLI command, exporter, or app boot path may implement identity checks with ad hoc SQL.

Required public behaviour:

`fetch(conn) -> str | None`

Returns the stored study ID, or `None` when the table is unbound.

`bind(conn, study_id) -> None`

Used only on writable databases when study creation is legitimate.

Behaviour:

| Database state                      | Result                     |
| ----------------------------------- | -------------------------- |
| identity matches                    | no op                      |
| identity differs                    | raise `StudyIdentityError` |
| no identity, no persistent data     | insert identity            |
| no identity, persistent data exists | raise `StudyIdentityError` |

For the empty database test, ignore `schema_migrations`, `study_identity`, and SQLite internal tables. A row in any other persistent table makes the database nonempty.

`require(conn, study_id) -> None`

Pure validation. Never writes.

Behaviour:

| Database state   | Result                     |
| ---------------- | -------------------------- |
| identity matches | success                    |
| identity differs | raise `StudyIdentityError` |
| identity missing | raise `StudyIdentityError` |

Add `StudyIdentityError(ValueError)` with operator facing messages containing both identities when a mismatch exists, but no clinical or clinician data.

Identity binding is metadata initialisation and must not increment `app.state.write_counter`.

## Application boot

`create_app()` initialises:

`app.state.study_id = None`

`app_from_study_config()` sets:

`app.state.study_id = study.study_id`

During lifespan, after `apply_migrations()` and before loading clinician state or recording ingestion issues:

1. if `app.state.study_id is None`, preserve current non study behaviour
2. otherwise call `study_identity.bind(app.state.db, app.state.study_id)`
3. only after successful binding may the database be considered ready

A mismatch or nonempty unbound legacy database must fail startup. It must not populate caches, record ingestion issues, start sessions, or perform any other application write.

The DB connection must be closed on failed startup.

`db.ready` must only be emitted after identity validation succeeds.

No configuration driven study route may become available against an unverified database.

## Study aware CLI commands

Any command that opens a database while holding a `StudyConfig` must verify the persisted identity.

Current commands affected:

| Command/path         | Required behaviour                                                                           |
| -------------------- | -------------------------------------------------------------------------------------------- |
| `serve --config ...` | writable boot uses `bind()`                                                                  |
| `export-answers`     | after schema validation, call `require()` before building the export                         |
| `divergence-view`    | after schema validation, call `require()` before reading study data                          |
| `reset-progress`     | after schema validation, call `require()` before any lookup or write                         |
| `preview --html-out` | initialise the fresh scratch DB with the study identity before seeding the preview clinician |

`validate-config`, `validate-adapter`, and ordinary `preflight` do not need a database and therefore perform no identity binding.

`migrate` remains configuration independent. It applies migration 4 but does not populate `study_identity`.

`backup` remains configuration independent in S11a.

## Preview and test database initialisation

The current HTML preview and several tests seed clinician rows before `app_from_study_config()` starts. Under the new contract, that would correctly look like a nonempty unidentified legacy database.

Those paths must therefore explicitly bind the fresh database before inserting study data.

Update `render_html_for_preview()` so its scratch DB sequence is:

`connect → apply_migrations → study_identity.bind → seed preview clinician → close → app boot`

Apply the same rule to study mode test fixtures.

Do not weaken the production legacy database refusal merely to preserve existing test setup.

Bare non study test fixtures may continue to seed databases without a study identity.

## Files expected to change

| Path                                                  | Required change                                                           |
| ----------------------------------------------------- | ------------------------------------------------------------------------- |
| `src/ehr_simulator/config/study.py`                   | schema v2, required validated `study_id`                                  |
| `src/ehr_simulator/config/loader.py`                  | expect study schema v2; hash naturally includes `study_id`                |
| `src/ehr_simulator/db/connection.py`                  | study specific default DB path                                            |
| `src/ehr_simulator/db/migrations.py`                  | migration 4                                                               |
| `src/ehr_simulator/db/study_identity.py`              | new identity DAO                                                          |
| `src/ehr_simulator/db/__init__.py`                    | expose study identity API                                                 |
| `src/ehr_simulator/web/app.py`                        | application state and boot binding                                        |
| `src/ehr_simulator/cli.py`                            | identity validation on study aware DB commands; help text for new default |
| `src/ehr_simulator/cli_support.py`                    | preview scratch DB binding                                                |
| `configs/example_config.yaml`                         | schema v2 + example `study_id`                                            |
| `tests/fixtures/study/*.yaml`                         | migrate study configs to v2 and add IDs                                   |
| `tests/fixtures/db/migration_001_expected_schema.sql` | include `study_identity`                                                  |
| `tests/conftest.py`                                   | initialise study databases before study data seeding                      |
| affected tests containing inline study YAML           | schema v2 + `study_id`                                                    |
| `README.md`, `CLAUDE.md`                              | update config and default DB documentation                                |
| `TODOS.md`                                            | close the config hash v2 TODO only                                        |

Do not close the S11 randomisation seed TODO or assignment on GET TODO. They belong to later subsessions.

## Test inventory

The following tests are required. Existing relevant tests should be extended rather than duplicated where practical.

### Configuration

1. `study_id` parses for a valid v2 config.
2. Missing `study_id` is rejected.
3. Invalid IDs are rejected, including uppercase, whitespace, path separators, more than 64 characters, and leading punctuation.
4. Study schema version 1 is rejected with an error stating expected `"2"`.
5. `questions.yaml` remains schema version 1.
6. Changing only `study_id` changes `config_hash`.
7. Existing hash invariants for whitespace, key ordering and deployment paths remain green.

### Database path

8. Study mode with no override resolves to `data/study_<study_id>.db`.
9. Bare non study mode still resolves to `data/ehr_simulator.db`.
10. Existing CLI, environment and YAML path precedence remains unchanged.

### Migration and DAO

11. Migration 4 creates exactly the specified singleton table and remains idempotent.
12. `bind()` writes an identity to an otherwise empty migrated DB.
13. Rebinding the same study is a no op.
14. Binding a different study raises without changing the stored identity.
15. Binding an unidentified database containing any persistent application row is refused.
16. `require()` succeeds only on a matching identity and performs no write.
17. The schema snapshot test includes the new table.

### Application and CLI integration

18. First study mode boot against a fresh DB stores the configured `study_id`.
19. Restarting the same study against that DB succeeds.
20. Starting another study against the same DB fails before application data are written.
21. A nonempty legacy unidentified DB is refused in study mode.
22. Bare `create_app()` remains functional with no identity row required.
23. `export-answers` refuses a mismatching or unidentified database before writing output.
24. `divergence-view` refuses a mismatching or unidentified database before writing SVG.
25. `reset-progress` refuses a mismatching or unidentified database before modifying state.
26. HTML preview successfully binds its scratch database before clinician seeding.
27. An explicit `--db-path` cannot bypass identity checking.

All existing default, E2E, CLI smoke, migration drift, export and divergence tests must remain green after fixture migration.

## Explicit non goals

S11a does not implement randomisation, a randomisation seed, scheduling, Start case, case lifecycle, configuration history, `config_version`, conditional questions, AI visibility, telemetry, PP classification, Phase 2 export datasets, or backup filename changes.

It does not alter `arm_assignments`, existing answer semantics, progress semantics, session semantics, or question behaviour.

It does not provide an operator command for claiming a nonempty legacy database. Such a command would make a scientific provenance decision and must be specified separately if ever required.

## Acceptance

S11a is complete only when all of the following are true:

A valid v2 study config carries a stable `study_id`; its default SQLite path is study specific; a fresh study database persists that ID; every subsequent study aware database access verifies it; another study cannot reuse the same database even through an explicit path override; a nonempty unidentified legacy database is never silently adopted; `study_id` participates in the existing configuration hash without rewriting historical hashes; non study synthetic mode remains unchanged; and the full test suite and CI pass.

At completion, implementation checklist Study identity items for adding, validating, associating, naming, persisting, mismatch refusal, and cross study reuse are closed. Adding `study_id` to Phase 2 research exports remains open for S11n.

