# Session 11n — Linked Phase 2 research exports

## Goal

Produce one reproducible, pseudonymised Phase 2 export bundle that preserves configuration provenance, planned and realised randomisation, timepoint level behavioural outcomes, panel exposure, PP variables, missing response provenance, and raw research telemetry without forcing all information into one wide answers CSV.

S11a through S11m are assumed complete.

The Phase 2 exporter must accept multiple valid configuration versions inside one study. It must interpret each case and observation under its own pinned configuration snapshot rather than under whichever YAML happens to be active at export time.

## Core invariants

1. One export is a read consistent snapshot of one bound `study_id`.
2. Multiple known configuration versions in one study are valid.
3. Every exported research row is attributable to a known `(config_version, config_hash)` pair.
4. Missing, unknown, or internally inconsistent configuration provenance refuses the entire bundle.
5. No historical row is reinterpreted under the current active configuration.
6. Activated incomplete cases remain visible.
7. ITT arm is the immutable realised assignment and is never replaced by PP status.
8. AI delivery, AI viewed, intervention failure/leakage, telemetry status, and PP are derived from S11l source data rather than mutable export-only guesses.
9. Raw events remain source data; panel/time/PP summaries must be reproducible from source events plus pinned configuration.
10. Missing telemetry is exported as missing/indeterminate, never as zero.
11. Hidden conditional questions are not exported as missing responses.
12. Practice data are excluded from routine measured research files by default.
13. Free text values are exported only when the case pinned policy explicitly allows routine export.
14. The clinician name keyfile remains a separate explicit operator output and is never placed into the routine Phase 2 bundle.
15. Export generation performs no database writes.
16. No final output directory is left half written after a validation failure.

## Existing behaviour being replaced

The current S9c `export-answers` path:

- creates one wide CSV
- is interpreted against the supplied live study/questions configuration
- requires one live `config_hash`
- refuses mixed configuration generations
- carries only the original timing fields and answer columns

That behaviour is appropriate for the earlier single generation workflow but is not a valid Phase 2 research export contract.

S11n adds a separate Phase 2 bundle builder. The legacy exporter remains available for pre Phase 2 compatibility, but it must not be presented as the Phase 2 research export.

## CLI contract

Add:

```
ehr-simulator export-phase2 STUDY_CONFIG \
    [--db-path PATH] \
    [--out-dir DIR] \
    [--keyfile PATH] \
    [--include-practice] \
    [--force]
```

`STUDY_CONFIG` is used to identify the study and resolve the default database path. The exporter does not require a current questions file because historical question/config snapshots come from `configuration_history`.

Default output directory:

```
<db parent>/exports/phase2_<study_id>_<UTC>/
```

The command must:

1. load/validate the supplied study config sufficiently to obtain its `study_id` and database path
2. open the DB read only
3. require current DB schema
4. require DB study identity to match the supplied `study_id`
5. build the complete export from one explicit read transaction
6. validate every dataset and join before final output is published
7. write into a sibling staging directory
8. atomically publish the staging directory by rename after all files succeed

If `--out-dir` already exists, refuse unless `--force` is supplied.

Under `--force`, build the new bundle completely first. Only then remove/replace the existing destination so a failure cannot leave a partially updated bundle.

No `--only-complete` option exists for the Phase 2 bundle. Incomplete activated cases are part of the research record and must remain visible.

## Relationship to `export-answers`

Preserve `export-answers` for non Phase 2/legacy workflows.

When a database contains Phase 2 randomised cases or multiple configuration versions, `export-answers` must refuse with an operator message directing the user to `export-phase2` rather than silently producing a partial or misinterpreted single generation file.

Existing keyfile generation helpers may be reused by both commands.

## Read snapshot and provenance registry

Create a Phase 2 export module, for example:

`src/ehr_simulator/export_phase2.py`

The builder receives one read only SQLite connection and opens one explicit:

```
BEGIN
...
ROLLBACK
```

snapshot, following the existing S9c pattern.

Load once inside that snapshot:

- `study_identity`
- all `configuration_history`
- measured `arm_assignments`
- measured `sessions`
- measured `progress`
- measured `answers`
- `case_lifecycle`
- `randomisation_schedules`
- `randomisation_schedule_items`
- `case_replacements`
- research behavioural events
- any S11j/S11k/S11l rows/events required by derivation

Build a registry keyed by `config_version`.

For every referenced version/hash:

- version must exist
- hash must equal the registered hash
- history row must belong to the same study
- stored study/questions snapshots must parse successfully

Never fall back to the active config when provenance is missing.

## Export format

Phase 2 export schema version:

```
phase2_export_v1
```

CSV files are UTF 8 with one header row and RFC 4180 compatible quoting.

Use the existing spreadsheet formula guard for user/config supplied text fields.

Canonical JSON fields use sorted keys and compact separators.

Empty CSV cell means missing/not applicable only where the corresponding status field makes its meaning explicit. Numeric zero is written as `0`/`0.0`, never as blank.

Booleans use lowercase:

```
true
false
```

Stable case identity is the existing composite:

```
study_id + clinician_id + patient_id
```

Do not add a new persisted `case_id` in S11n. `session_id` is included where the source is session specific.

Every timepoint file includes both `t_index` and `timepoint_minutes` from the case pinned configuration.

## Required bundle files

The routine measured bundle contains:

```
timepoints.csv
answers.csv
panel_summaries.csv
behavioral_events.csv
randomisation_audit.csv
configuration_history.csv
configuration_counts.csv
manifest.json
```

### `timepoints.csv`

One row per configured timepoint for every activated measured case, including timepoints never reached before an incomplete case ended.

Minimum columns:

```
study_id
clinician_id
patient_id
t_index
timepoint_minutes
config_version
config_hash
arm
arm_source
schedule_id
case_position
activated_at
lifecycle_state
case_completed_at
case_incomplete_at
incomplete_reason
timepoint_reached
timepoint_started_at
timepoint_ended_at
elapsed_seconds
foreground_seconds
active_seconds
telemetry_status
tab_conflict_detected
ai_delivered
ai_viewed
ai_qualifying_seconds
intervention_failure
intervention_failure_reasons
intervention_leakage
pp_compliant
```

Rules:

- `arm` comes from immutable `arm_assignments`
- `config_version/hash` are activation provenance, not schedule generation provenance
- all configured timepoints are emitted for an activated case
- `timepoint_reached=false` keeps unreached later observations distinguishable from missing telemetry
- `elapsed_seconds` uses S10 derivation
- `foreground_seconds` and `active_seconds` use S11j primary visit derivation
- AI fields and PP use S11l primary observation derivation
- missing/indeterminate telemetry leaves duration/view fields blank and supplies `telemetry_status`
- `intervention_failure_reasons` is a stable pipe joined categorical list in sorted/documented order

### `answers.csv`

Long format, one row for every expected question cell under the case pinned questions/configuration and effective S11h branch.

Minimum columns:

```
study_id
clinician_id
patient_id
t_index
timepoint_minutes
config_version
config_hash
question_id
branch_status
response_present
response_value
value_exported
answer_source
derived_from_question_id
missing_response_reason
ts_recorded
```

`branch_status` values:

- `required_or_visible`
- `hidden_not_applicable`

Rules:

- a hidden question is emitted as `hidden_not_applicable`, `response_present=false`, and no missing reason
- clinician and rule generated answers remain distinguishable through `answer_source`
- rule generated values are real present responses
- missing reasons come from S11l and are never statistical imputations
- multi select values use the existing stable pipe encoding in configured option order
- scalar/text values use the existing strict answer codec

Free text handling is evaluated under the case pinned `study_behaviour.free_text` policy:

- `routine_export=include_explicit` -> export the value with formula guard
- otherwise -> keep the row and response presence/provenance, but leave `response_value` blank and set `value_exported=false`

This preserves missingness/branch audit without leaking free text content.

### `panel_summaries.csv`

One row per derived panel/timepoint/visit summary for measured cases.

Minimum columns:

```
study_id
clinician_id
patient_id
t_index
timepoint_minutes
config_version
config_hash
panel_id
visit_kind
qualifying_seconds
viewed
episode_count
time_to_first_view_seconds
first_view_client_ts
last_view_client_ts
first_view_server_ts
last_view_server_ts
panel_open_count
telemetry_status
tab_conflict_detected
```

Rules:

- derivation uses S11k source events and pinned thresholds
- primary and revisit rows stay distinguishable
- primary rows are the authoritative source for PP related AI viewed status
- complete observed zero exposure is written as `0.0` and `viewed=false`
- missing/indeterminate telemetry is blank with an explicit status
- simultaneous panel durations may overlap and are never summed into a total attention measure
- no AI cases do not fabricate an AI panel summary for a panel that did not exist

### `behavioral_events.csv`

Raw research behavioural/audit source events needed to reproduce timing, exposure, and intervention integrity.

Include research event families such as:

- `answer.upsert`, `answer.clear`
- `advance.ok`, `advance.blocked`
- `timepoint.enter`, `timepoint.exit`, `timepoint.revisit`
- S11j `browser.*`
- S11k `panel.*`
- S11l `intervention.*`
- S11m `tab.*`

Exclude purely operational identity/login events and operator maintenance events from this routine research file, including:

- `clinician.login`
- `clinician.logout`
- `progress.reset`

Case lifecycle/randomisation provenance is represented in the dedicated audit file, so `case.*`, `session.*`, and `practice.*` events need not be duplicated here unless required by one of the pure derivations.

Minimum columns:

```
study_id
event_id
session_id
clinician_id
patient_id
timepoint
t_index
config_version
config_hash
tab_id
visit_kind
kind
client_ts
server_ts
client_seq
client_mono_ms
payload_json
```

Configuration provenance is resolved through the event session/case context. An included patient scoped event whose configuration identity cannot be established is an export integrity failure.

Payload rules:

- retain only the event's documented categorical/non identifying research payload
- never export `name_normalized`
- never export free text response content
- never export raw answer values
- never export raw clinical values or raw AI prediction/explanation values
- historical login payloads containing `name_normalized` are excluded with their operational event family rather than copied into the research bundle

### `randomisation_audit.csv`

One row per planned `randomisation_schedule_item`, whether activated or not.

Minimum columns:

```
study_id
clinician_id
schedule_id
generation_config_version
generation_config_hash
generated_at
algorithm_version
master_seed
derived_seed_hex
allocation_state_json
starting_ai_count
starting_no_ai_count
starting_arm
block_length
block_sequence_json
case_position
patient_id
planned_arm
block_number
position_in_block
preceding_block_arm
planned_cases_since_ai
assignment_seed
activated
activated_at
activation_config_version
activation_config_hash
realised_arm
arm_source
lifecycle_state
completed_at
incomplete_at
incomplete_reason
replacement_id
replaces_patient_id
replaced_by_patient_id
replacement_generated_at
replacement_activated_at
```

Rules:

- planned fields come from immutable schedule rows
- realised fields come from `arm_assignments` and lifecycle rows
- planned but unactivated item has `activated=false` and blank realised fields
- generation configuration and activation configuration are separate columns and may differ
- replacement linkage is visible in both directions
- incomplete original cases remain present even after a replacement activates
- the stored `allocation_state_json`, seed material, algorithm version, starting arm/counts, and block metadata must be sufficient to reproduce the scheduler inputs defined by S11c

A mismatch between planned arm and a realised `phase2_randomized` assignment is an integrity failure, not an export correction.

### `configuration_history.csv`

One row per registered configuration version, activation order.

Minimum columns:

```
study_id
config_version
config_hash
activated_at
change_description
change_reason
study_json
questions_json
```

The canonical stored snapshots are included so the bundle can reproduce historical question/config interpretation without the original operator files.

Do not export filesystem only source paths that S11b intentionally excludes from stored snapshots.

### `configuration_counts.csv`

One row per configuration version.

Minimum columns:

```
study_id
config_version
config_hash
scheduled_items_generated
activated_cases
completed_cases
incomplete_cases
expected_timepoints
reached_primary_timepoints
answer_rows_present
```

Counting rules:

- schedule item count is grouped by schedule generation provenance
- case/lifecycle/timepoint/answer counts are grouped by activation configuration provenance
- practice rows are not included in measured counts
- counts are recomputed from the same read snapshot as the exported data

These counts are an audit summary and do not replace the row level files.

### `manifest.json`

Write last inside the staging directory.

Minimum shape:

```
{
  "export_schema_version": "phase2_export_v1",
  "study_id": "...",
  "generated_at_utc": "...",
  "source_schema_version": 13,
  "included_config_versions": ["..."],
  "practice_included": false,
  "files": {
    "timepoints.csv": {"rows": 0, "sha256": "..."},
    "answers.csv": {"rows": 0, "sha256": "..."}
  }
}
```

List every produced file with row count and SHA256.

Do not include clinician names or keyfile path in the manifest.

## Practice/QA export

S11i explicitly deferred practice/QA export handling to S11n.

Default `export-phase2` excludes `observation_mode=practice` everywhere.

With `--include-practice`, add separate files:

```
practice_timepoints.csv
practice_answers.csv
```

They must never be merged into measured `timepoints.csv` or `answers.csv`.

Each practice row includes:

- `study_id`
- `clinician_id`
- `patient_id`
- `config_version`
- `config_hash`
- fixed practice `arm`
- timing/answer fields available from the practice session/progress/answer rows
- `observation_mode=practice`

Do not calculate measured randomisation audit or measured PP status for practice rows.

Free text policy still applies under the practice case pinned configuration.

The manifest records `practice_included=true` when these files are present.

## Keyfile behaviour

`--keyfile PATH` is still explicit opt in.

The keyfile:

- contains only clinician IDs present in the produced bundle
- uses the existing `clinician_id,name_normalized` format
- is written with mode `0600` where supported
- must resolve to a path outside `--out-dir`
- is not listed as a routine research file in `manifest.json`

If the operator points `--keyfile` inside the bundle directory, refuse and explain that the identifying mapping must remain separate.

## Integrity validation

Refuse the entire bundle before publication when any of the following occurs:

- DB study identity differs from supplied study
- missing `config_version` or `config_hash` on a row that requires provenance
- unknown config version
- known version with wrong hash
- config history row from another study
- stored config/question snapshot no longer parses
- assignment/session/progress/answer provenance disagreement
- answer arm differs from immutable assignment
- randomised realised arm differs from planned schedule item
- schedule/replacement link points to a missing row
- lifecycle row without realised assignment
- included event cannot be attributed to its measured case/configuration
- timepoint not present in the pinned configuration
- unknown question under the pinned questions snapshot
- invalid stored answer encoding
- negative derived timing/exposure
- impossible S11l intervention/PP combination
- panel summary cannot be reproduced from its raw source events under the pinned threshold

Do not repair these states during export.

## Ordering

Use deterministic ordering:

- `configuration_history.csv`: activation time, config version
- `configuration_counts.csv`: activation time, config version
- `randomisation_audit.csv`: clinician ID, schedule ID, case position
- `timepoints.csv`: clinician ID, activation/case position, t index
- `answers.csv`: clinician ID, activation/case position, t index, pinned question order
- `panel_summaries.csv`: clinician ID, activation/case position, t index, visit kind, panel ID
- `behavioral_events.csv`: event ID
- practice files: clinician ID, practice start time/patient, t index/question order

The same database snapshot should produce byte stable CSV content apart from manifest/export generation timestamp when no source data changed.

## Database/schema changes

None are required for S11n if S11a through S11m persistence contracts are complete.

Do not add materialised export summary tables merely to make export easier.

Derived values should remain reproducible from immutable rows/events/config snapshots.

## Configuration changes

None.

Export schema version is an exporter contract, not a study `config_hash` field.

## Public API/route changes

No clinician facing routes.

Add CLI command `export-phase2`.

Legacy `export-answers` remains for pre Phase 2 compatibility but refuses use where it would misrepresent a Phase 2/mixed version database.

## Failure behaviour

- validation/provenance failure -> exit 1, no final output directory
- output exists without `--force` -> exit 1 before publication
- staging file write failure -> clean staging best effort, leave prior final output untouched
- keyfile failure -> no claim that the full requested export succeeded; do not silently omit it
- overdue lifecycle cases -> do not silently relabel; they export in their persisted current state and operator tooling may still warn to run `expire-cases`
- missing telemetry -> export explicit status/nulls rather than substitute timing

## Files expected to change

- new `src/ehr_simulator/export_phase2.py`
- small reusable CSV/atomic directory writer helpers if appropriate
- `src/ehr_simulator/cli.py`
- read helpers in `db/config_history.py`, `db/randomisation.py`, `db/replacements.py`, `db/case_lifecycle.py` where existing APIs are insufficient
- S11j timing derivation module
- S11k `panel_exposure.py`
- S11l `study_variables.py`
- `src/ehr_simulator/export.py` only for legacy command guard/shared helpers
- tests, README/operator docs, `CLAUDE.md`, checklist

## Required tests

### Snapshot and provenance

1. Export of one valid configuration version succeeds.
2. Two valid configuration versions in one study succeed.
3. Each case/timepoint row retains its activation version/hash.
4. Schedule generation version remains distinct from later activation version where applicable.
5. Missing config version refuses the bundle.
6. Unknown config version refuses the bundle.
7. Known version/wrong hash refuses the bundle.
8. Stored snapshot parse failure refuses the bundle.
9. Foreign study identity refuses before files are published.
10. Export reads one coherent SQLite snapshot even if another connection commits after the snapshot begins.

### Timepoints and answers

11. Every activated measured case appears, including incomplete cases.
12. Unreached configured timepoints appear with `timepoint_reached=false` and no fabricated timing.
13. Complete zero foreground/active/exposure remains numeric zero, distinct from missing telemetry.
14. S10 elapsed time is preserved.
15. S11j foreground/active values match pure derivation.
16. S11l AI delivery/viewed/failure/leakage/PP values match pure derivation.
17. Required reached unanswered question exports `reached_unanswered`.
18. Hidden conditional question exports `hidden_not_applicable`, not missing.
19. Rule generated answer exports `answer_source=rule` and is present.
20. Incomplete case missing later response retains the S11l reason.
21. Mixed question schemas export correctly in long format without inventing union wide columns.
22. Invalid stored answer encoding refuses.

### Free text and privacy

23. Free text under `routine_export=exclude` retains response provenance but not the value.
24. Free text under `include_explicit` exports only in `answers.csv`/practice answers with cell guarding.
25. Free text never appears in `behavioral_events.csv`, panel summaries, manifest, or configuration counts.
26. Routine bundle contains no `name_normalized`.
27. Historical login event containing `name_normalized` is not copied into `behavioral_events.csv`.
28. Keyfile is absent unless requested.
29. Requested keyfile is mode `0600` and separate from the bundle.
30. Keyfile path inside `out-dir` is refused.

### Panel and behavioural telemetry

31. Panel raw events reproduce exported panel duration, viewed flag, episode count, first latency, timestamps, and open count.
32. Primary and revisit panel rows remain distinct.
33. Multi tab conflict is not silently summed.
34. A refused second tab conflict flag may coexist with otherwise complete owner telemetry.
35. Raw behavioural rows carry tab ID and configuration identity.
36. Included event with unattributable case/config provenance refuses.

### Randomisation audit

37. Every schedule item appears whether activated or not.
38. Planned patient order and arm match persisted schedule.
39. Activated item shows activation timestamp/version/hash and realised arm.
40. Planned versus realised mismatch refuses.
41. Incomplete lifecycle outcome remains visible.
42. Original and replacement rows expose reconstructable bidirectional linkage.
43. Randomisation algorithm version, seed/context, block metadata, and allocation state are exported.
44. Rebuilding the known test schedule from exported generation inputs reproduces the planned schedule.

### Configuration files/counts

45. Configuration history exports version, hash, activation timestamp, description, optional reason, and snapshots.
46. `configuration_counts.csv` activated/completed/incomplete case counts match row level data.
47. Reached primary timepoint counts match `timepoints.csv`.
48. Counts remain separated by activation configuration version.

### Practice

49. Default Phase 2 export contains no practice rows.
50. `--include-practice` creates separate practice files only.
51. Practice rows never enter measured randomisation/PP files or counts.
52. Practice free text follows its pinned export policy.

### Atomic output and manifest

53. A validation failure creates no final bundle directory.
54. Existing destination refuses without `--force`.
55. A staged write failure leaves an existing final bundle unchanged.
56. Manifest names every produced routine file and correct row count.
57. Manifest SHA256 values match the final files.
58. Manifest contains study/schema/export identity and no clinician names.
59. Same unchanged DB snapshot yields deterministic row ordering/content.

### Legacy/regression

60. Pre Phase 2 `export-answers` behaviour remains green.
61. Phase 2/mixed version use of legacy `export-answers` refuses with guidance to `export-phase2`.
62. All S11a through S11m tests remain green.
63. Full CI remains green.

## Out of scope

S11n does not implement:

- statistical analysis or effect estimation
- statistical imputation
- de identification beyond the locked pseudonymous identifier policy
- automatic upload to a data warehouse
- Parquet/Arrow as a required format
- a clinician name mapping inside the routine bundle
- mutable materialised summary tables
- changing randomisation or PP definitions
- changing retention policy

## Acceptance

S11n is complete when one command can export a read consistent, pseudonymised, linked Phase 2 bundle for a study containing multiple valid configuration versions; every row carries sufficient stable identity/provenance to join and interpret it; incomplete cases and planned versus realised randomisation remain reconstructable; raw events reproduce timing/panel summaries; PP and missingness remain faithful to S11l; practice/free text/keyfile data obey their privacy boundaries; and any provenance inconsistency refuses the bundle instead of being silently repaired.

## Checklist items closed

Primarily Session 11 implementation checklist:

- 31 Phase 2 export set
- 32 Common export identifiers
- 33 Mixed configuration export behaviour

S11n also closes the export related deferred items from study identity, configuration provenance, randomisation provenance, timing/panel telemetry, PP, intervention failure/leakage, missing responses, practice mode, and free text.
