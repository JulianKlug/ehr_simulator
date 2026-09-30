# Session 11n — Linked Phase 2 research exports

## Goal

Produce one reproducible, pseudonymised Phase 2 export bundle that preserves configuration provenance, planned and realised randomisation, timepoint level behavioural outcomes, panel exposure, PP variables, missing response provenance and raw research telemetry, without forcing everything into one wide answers CSV.

S11a through S11m are complete. The exporter accepts several valid configuration versions inside one study and interprets every case under its own pinned snapshot, never under the YAML active at export time.

## Review revisions (2026-09-28)

Changes against the first draft, from the implemented S11j–m code:

1. **No `intervention.*` events.** AI delivery, failure and leakage come from S11l (`timepoint.render` `ai` payload + AI `panel.mount`). The event family is dropped.
2. **`behavioral_events.csv` must include `timepoint.render` and a `render_id` column.** Renders are what bind browser rows to an observation; without them no panel summary is reproducible from the file. `case.*` and `session.*` events are included too (pause/resume/reconnect episodes are behavioural and cheap to carry).
3. **S11l integrity problems are exported, not refused.** S11l's invariant 7 promises that one bad row never blocks the export. Answer arm drift, AI on a no AI case and similar S11l warnings go into an `integrity_warnings` column. Refusal is reserved for provenance and linkage failures the bundle cannot represent (unknown/mismatched config, schedule/assignment disagreement, unparseable snapshot or answer, S10 `TimingError`).
4. **Status vocabularies are the implemented ones.** `telemetry_status` ∈ `complete|incomplete|gapped|missing|multi_tab|invalid` (+ `not_configured` when the pinned config has no `telemetry`); answer states use S11h `editable|derived|hidden` and S11l `answered|not_applicable|missing`.
5. **Panel rows drop `first/last_view_server_ts`.** A browser row's `server_ts` is its batch arrival time, not a view time. `mounted` is added.
6. **Legacy `export-answers` is not blocked on Phase 2 databases.** It already refuses mixed configuration generations (hash drift), and S11e/S11i tests rely on its single generation Phase 2 behaviour (incomplete cases, overdue warning, free text policy). Its drift refusal now names `export-phase2`.
7. **The runtime "panel summary reproducible" check is dropped** (the exporter derives the summaries from the same rows, so the check is tautological); reproducibility is a test.
8. `--keyfile` is a file path, as in `export-answers`.
9. **Events of pairs that are not activated measured cases are out of scope** (practice, pre Phase 2 walks, clinician level rows) and excluded, not refused; only an event of a measured case with unattributable provenance refuses.
10. **Replacement chains need two id columns.** A replacement case can itself end incomplete and be replaced, so `randomisation_audit.csv` carries `replacement_id` (this item replaces another) and `replaced_by_replacement_id` (this case was replaced).

## Review revisions (2026-09-30, PR review)

11. **Replacement plans are checked against the item they name.** The foreign key covers only `(replacement_schedule_id, replacement_case_position)`; the exporter additionally refuses a plan whose schedule belongs to another clinician, whose item patient ≠ `replacement_patient_id`, or whose item arm ≠ the plan's `planned_arm`.
12. **`tab_conflict_detected` in `timepoints.csv`** counts primary renders only (S11m revision 14); `panel_summaries.csv` keeps the per `visit_kind` flag.

## Core invariants

1. One export is a read consistent snapshot of one bound `study_id`.
2. Several known configuration versions in one study are valid.
3. Every exported research row names one known `(config_version, config_hash)`.
4. Missing, unknown or inconsistent configuration provenance refuses the whole bundle.
5. No row is reinterpreted under the active configuration.
6. Activated incomplete cases remain visible.
7. The ITT arm is the immutable `arm_assignments.arm`; PP never replaces it.
8. AI delivery, viewing, failure, leakage, telemetry status and PP come from S11l; foreground/active time from S11j; panel exposure from S11k. The exporter derives nothing of its own.
9. Raw events stay the source; every summary is reproducible from the exported events plus the exported configuration snapshots.
10. Missing telemetry is blank with an explicit status, never zero.
11. Hidden conditional questions are not missing responses.
12. Practice data stay out of measured files.
13. Free text values are exported only under a case pinned `routine_export: include_explicit`.
14. The clinician name keyfile is a separate explicit output, never inside the bundle.
15. Export performs no database write.
16. No final output directory is left half written.

## Existing behaviour kept

`export-answers` (S9c) stays the single generation wide CSV. Under hash drift it refuses as today, with the message now ending `use export-phase2 for mixed configuration Phase 2 databases`.

## CLI contract

```
ehr-simulator export-phase2 STUDY_CONFIG \
    [--db-path PATH] [--out-dir DIR] [--keyfile FILE] [--include-practice] [--force]
```

`STUDY_CONFIG` identifies the study and resolves the DB path; historical study/question snapshots come from `configuration_history`, so no questions file is needed.

1. load `STUDY_CONFIG` (for `study_id` and `db_path` only)
2. DB missing → exit 1; open read only (`AccessMode.READ_ONLY`)
3. `assert_schema_current`; `study_identity.require(conn, study_id)`
4. build the whole bundle in memory from one `BEGIN … ROLLBACK` snapshot
5. validate every dataset and join
6. write into a sibling staging directory `.<name>.staging-<random>`, `manifest.json` last
7. publish by `rename`

Default out dir: `<db parent>/exports/phase2_<study_id>_<UTC>/`. An existing `--out-dir` refuses unless `--force`; under `--force` the old directory is renamed aside only after staging succeeded, the new one renamed in, then the old one removed. Any failure leaves the previous bundle untouched and removes the staging directory.

`export-phase2` warns (stderr, exit 0) when open cases are past their grace, as `export-answers` does. There is no `--only-complete`.

Exit 0 on success, 1 on any refusal (process exit contract pinned by a subprocess test).

## Module layout

```
cli.py ─► export_phase2.py            build + validate (pure over a snapshot)
             ├─ study_variables_reader.load_case_inputs   (S11l inputs)
             ├─ study_variables / behavioral_timing / panel_exposure (pure)
             ├─ timing (S10 elapsed)
             └─ db.* read DAOs
          export_bundle.py            CSV rendering, manifest, staged directory publish
          export.py                   guard_cell, encode helpers, keyfile writer (reused)
```

`export_phase2` never imports `web`.

## Snapshot and provenance registry

Inside one snapshot, load `study_identity`, all `configuration_history`, measured `arm_assignments` (`phase2_randomized`), `case_lifecycle`, schedules + items, `case_replacements`, measured `answers`/`progress`/`sessions`, practice cases, and the research events.

Registry keyed by `config_version`; for every version referenced by a schedule, assignment, session, progress row or answer:

- version exists in `configuration_history`
- hash equals the registered hash
- history row's `study_id` equals the bound study
- stored study and questions snapshots parse (`parse_study_snapshot`, `parse_questions_snapshot`)

Never fall back to the active config.

## Format

- export schema version `phase2_export_v1`
- UTF 8, one header row, RFC 4180 quoting, `\n` line ends
- `export.guard_cell` on every user/config supplied text cell (answer values, question ids, free text, descriptions, JSON snapshots)
- canonical JSON: sorted keys, compact separators
- booleans `true`/`false`; numeric zero `0`/`0.0`, never blank; blank = missing/not applicable, always next to a status column that says which
- floats: `repr(float)`; timestamps: ISO 8601 UTC as stored
- case identity: `study_id + clinician_id + patient_id` (no new `case_id`)
- every timepoint file carries `t_index` and `timepoint_minutes` of the pinned config

## Bundle files

```
timepoints.csv  answers.csv  panel_summaries.csv  behavioral_events.csv
randomisation_audit.csv  configuration_history.csv  configuration_counts.csv
manifest.json
```

### `timepoints.csv`

One row per configured timepoint of every activated measured case (unreached ones included).

```
study_id clinician_id patient_id t_index timepoint_minutes config_version config_hash
arm arm_source schedule_id case_position activated_at
lifecycle_state case_completed_at case_incomplete_at incomplete_reason
timepoint_reached timepoint_started_at timepoint_ended_at elapsed_seconds
telemetry_status foreground_seconds active_seconds tab_conflict_detected
ai_delivered ai_viewed ai_viewing_status ai_qualifying_seconds ai_episode_count
intervention_failure intervention_failure_reasons intervention_leakage
pp_compliant pp_determinate integrity_warnings
```

- `arm`, `schedule_id`, `case_position`, `activated_at`, `config_*` from `arm_assignments` (activation provenance)
- `timepoint_reached` = S11l `reached`
- `timepoint_started_at/ended_at/elapsed_seconds` = S10 `timing.derive_timepoint_timings`
- `telemetry_status/foreground_seconds/active_seconds` = S11j primary observation (`visit_kind = primary`); no primary render → `missing`; pinned config without `telemetry` → `not_configured`, blank seconds
- AI, failure, leakage, PP, warnings = S11l `ObservationVariables`; `tab_conflict_detected` = S11m
- `intervention_failure_reasons`, `integrity_warnings`: pipe joined, sorted
- a `None` S11l value is blank

### `answers.csv`

Long format: one row per `(case, t_index, question)` of the pinned questions snapshot, every configured timepoint.

```
study_id clinician_id patient_id t_index timepoint_minutes config_version config_hash
question_id response_type branch_state required_now response_status response_value
value_exported answer_source derived_from_question_id missing_reason ts_recorded
```

- `branch_state` = S11h `editable|derived|hidden` evaluated on the stored clinician answers of that timepoint; `required_now` = S11h
- `response_status`, `missing_reason` = S11l `ResponseProvenance`
- `response_value`: the stored value decoded by `answer_codec.decode_stored_answer` (multi select pipe encoded in option order); an undecodable value refuses the bundle
- `free_text` values are written only when the case pinned `study_behaviour.free_text.routine_export` is `include_explicit`; otherwise `response_value` is blank and `value_exported=false` (presence and provenance stay)
- rows for question ids unknown to the pinned snapshot refuse the bundle

### `panel_summaries.csv`

One row per S11k summary of every observation with at least one render, for every `PANEL_IDS` panel, except the AI panel on a no AI case unless it has AI panel events (leakage).

```
study_id clinician_id patient_id t_index timepoint_minutes config_version config_hash
visit_kind panel_id mounted telemetry_status qualifying_seconds viewed episode_count
panel_open_count time_to_first_view_seconds first_view_client_ts last_view_client_ts
tab_conflict_detected
```

Primary and revisit rows are distinct; complete zero exposure is `0.0` / `viewed=false`; indeterminate is blank with its status; panels are never summed.

### `behavioral_events.csv`

Every measured event of kinds `answer.*`, `advance.*`, `timepoint.*` (render, enter, exit, revisit), `browser.*`, `panel.*`, `tab.*`, `case.*`, `session.*`, ordered by `event_id`. Excluded: `clinician.*`, `progress.reset`, `practice.*`, and every event of a practice case.

```
study_id event_id session_id clinician_id patient_id timepoint t_index visit_kind
config_version config_hash render_id tab_id kind client_ts server_ts client_seq
client_mono_ms payload_json
```

- `config_*` = the case's activation provenance; an event naming a session of another case refuses the bundle; events of pairs that are not activated measured cases are excluded
- `t_index` from the pinned timepoints; `visit_kind` from the event's render (blank when none)
- `payload_json` is the stored canonical payload. Stored payloads are categorical by construction (S11j/S11k closed models; `answer.*` carries `value_chars`, never a value); the exporter additionally refuses the bundle if any exported payload contains `name_normalized`

### `randomisation_audit.csv`

One row per `randomisation_schedule_items` row, activated or not.

```
study_id clinician_id schedule_id generation_config_version generation_config_hash
generated_at algorithm_version master_seed derived_seed_hex allocation_state_json
starting_ai_count starting_no_ai_count starting_arm block_length block_sequence_json
case_position patient_id planned_arm block_number position_in_block
preceding_block_arm planned_cases_since_ai assignment_seed
activated activated_at activation_config_version activation_config_hash realised_arm
lifecycle_state completed_at incomplete_at incomplete_reason
replacement_id replaces_patient_id replacement_generated_at replacement_activated_at
replaced_by_replacement_id replaced_by_patient_id
```

- planned fields from the schedule rows; realised fields from `arm_assignments` + `case_lifecycle`
- unactivated → `activated=false`, realised fields blank
- `replacement_id`, `replaces_patient_id`, `replacement_*_at` on the replacement item's row; `replaced_by_replacement_id`, `replaced_by_patient_id` on the original's row
- a realised assignment whose `(schedule_id, case_position)` item is missing, whose patient differs, or whose arm ≠ `planned_arm` refuses the bundle; so does a replacement pointing to a missing item or assignment, a replacement whose item belongs to another clinician's schedule, names another patient than `replacement_patient_id` or another arm than the plan's `planned_arm`, and a lifecycle row without an assignment
- the exported generation inputs regenerate the schedule (`randomisation.generate_schedule`) — a test, not a runtime step

### `configuration_history.csv`

```
study_id config_version config_hash activated_at change_description change_reason study_json questions_json
```

Activation order. Snapshots as stored (they never contain filesystem paths).

### `configuration_counts.csv`

```
study_id config_version config_hash scheduled_items_generated activated_cases
completed_cases incomplete_cases open_cases expected_timepoints reached_primary_timepoints
answer_rows_present
```

Schedule items grouped by generation version; everything else by activation version; practice never counted; computed from the same row sets the files are written from.

### `manifest.json`

Written last.

```
{
  "export_schema_version": "phase2_export_v1",
  "study_id": "...",
  "generated_at_utc": "...",
  "source_schema_version": 13,
  "included_config_versions": ["v1", "v2"],
  "practice_included": false,
  "files": {"timepoints.csv": {"rows": 0, "sha256": "..."}, ...}
}
```

Every bundle file except the manifest, with data row count and SHA256. No clinician name, no keyfile path.

## Practice

Default: practice excluded everywhere. `--include-practice` adds

```
practice_timepoints.csv  practice_answers.csv
```

- `practice_timepoints.csv`: `study_id clinician_id patient_id t_index timepoint_minutes config_version config_hash observation_mode arm practice_started_at practice_completed_at timepoint_started_at timepoint_ended_at elapsed_seconds telemetry_status foreground_seconds active_seconds`
- `practice_answers.csv`: the `answers.csv` columns plus `observation_mode`, with `response_status` = `answered|not_applicable|missing` and a blank `missing_reason` (no lifecycle exists)
- never merged into measured files, audit, counts or PP; free text policy of the practice case's pinned config applies; `manifest.practice_included = true`

## Keyfile

`--keyfile FILE` (explicit opt in): clinician ids present in the bundle only, `clinician_id,name_normalized`, mode `0600` via `export.write_keyfile`, refused when it resolves inside `--out-dir`, not listed in the manifest. A keyfile failure makes the command exit 1 after the bundle is published, stating that the bundle was written and the keyfile was not.

## Integrity refusals

Before anything is published, refuse on:

- study identity mismatch
- missing `config_version` on an assignment, session, progress or answer row of a measured Phase 2 case
- unknown version, wrong hash, foreign study history row, unparseable snapshot
- session/progress/answer version or hash ≠ its case's assignment
- realised vs planned schedule disagreement, dangling or inconsistent replacement (clinician, patient or arm ≠ its schedule item), lifecycle row without assignment
- event of a measured case that cannot be attributed to its case/configuration
- answer at a timepoint or with a question unknown to the pinned snapshot
- undecodable stored answer
- S10 `TimingError`
- `name_normalized` in an exported payload

Nothing is repaired.

## Ordering

- `configuration_history.csv`, `configuration_counts.csv`: `activated_at`, `config_version`
- `randomisation_audit.csv`: `clinician_id`, `schedule_id`, `case_position`
- `timepoints.csv`, `answers.csv`, `panel_summaries.csv`: `clinician_id`, `activated_at`, `patient_id`, `t_index`, then question order / `visit_kind`, `panel_id`
- `behavioral_events.csv`: `event_id`
- practice files: `clinician_id`, `started_at`, `patient_id`, `t_index`, question order

An unchanged DB yields byte identical CSVs; only `manifest.generated_at_utc` differs.

## Database/schema changes

None. Read helpers may be added to DAOs.

## Configuration changes

None.

## Public API/route changes

CLI `export-phase2`. No routes.

## Failure behaviour

- validation/provenance failure → exit 1, no output directory
- existing destination without `--force` → exit 1 before building
- staging write failure → staging removed, prior bundle untouched, exit 1
- keyfile failure → exit 1 with the bundle path named
- overdue open cases → exported in their persisted state + warning

## Files expected to change

New `export_phase2.py`, `export_bundle.py`; `cli.py`; `export.py` (drift message); `db/telemetry.py`, `db/events.py`, `db/randomisation.py`, `db/replacements.py`, `db/case_lifecycle.py`, `db/sessions.py` read helpers; tests; `README.md`, `CLAUDE.md`, checklist.

## Required tests

### Snapshot and provenance

1. One valid configuration version exports.
2. Two versions in one study export.
3. Each case/timepoint row keeps its activation version/hash.
4. Schedule generation version stays distinct from a later activation version.
5. Missing config version refuses.
6. Unknown config version refuses.
7. Known version with wrong hash refuses.
8. Unparseable stored snapshot refuses.
9. Foreign study identity refuses, nothing published.
10. The snapshot ignores a commit made by another connection after it began.

### Timepoints and answers

11. Every activated case appears, incomplete included.
12. Unreached timepoints: `timepoint_reached=false`, blank timing.
13. Complete zero exposure is `0.0`, distinct from missing telemetry (a mounted panel never on screen: `mounted=true`, `0.0`, `viewed=false`; an unreached timepoint: status `missing`, blank seconds, no panel rows).
14. S10 elapsed matches `timing`.
15. Foreground/active match `behavioral_timing`.
16. AI delivered/viewed/failure/leakage/PP match `study_variables`.
17. Reached unanswered required question → `reached_unanswered`.
18. Hidden question → `not_applicable`, `branch_state=hidden`, no reason.
19. Rule answer → `answer_source=rule`, `answered`, `branch_state=derived`.
20. Incomplete case keeps S11l reasons for later timepoints.
21. Two question schemas export in long format without union columns (v2 adds a question; v1 cases never list it, v2 cases do).
22. Undecodable stored answer refuses.
23. S11l integrity warning (answer arm drift) exports in `integrity_warnings` instead of refusing.

### Free text and privacy

24. Free text under `exclude`: row kept, value blank, `value_exported=false`.
25. Free text under `include_explicit`: value exported, cell guarded.
26. Free text never appears in events, panels, manifest or counts.
27. Bundle contains no `name_normalized`, historical login rows excluded.
28. Keyfile absent unless requested.
29. Requested keyfile is mode `0600`, outside the bundle.
30. Keyfile inside `--out-dir` refuses.

### Panels and events

31. Exported raw events re-derive every exported panel row and primary timing value.
32. Primary and revisit rows stay distinct.
33. Multi tab observation stays `multi_tab`, blank seconds.
34. A refused second tab sets `tab_conflict_detected` next to complete owner telemetry.
35. Event rows carry `render_id`, `tab_id` and configuration identity.
36. An event of a measured case without attributable provenance refuses.
37. No AI case exports no AI panel row unless it leaked.

### Randomisation audit

38. Every schedule item appears, activated or not.
39. Planned order and arm match the stored schedule.
40. Activated item shows activation time, version, hash, realised arm.
41. Planned vs realised mismatch refuses.
42. Incomplete lifecycle outcome visible.
43. Original ↔ replacement linkage in both directions.
43a. A replacement plan whose item names another patient, arm or clinician refuses the bundle.
44. Exported generation inputs regenerate the planned schedule.

### Configuration

45. History exports version, hash, activation time, description, reason, snapshots.
46. Counts match row level data per activation version.
47. Reached primary timepoint counts match `timepoints.csv`.

### Practice

48. Default export has no practice rows.
49. `--include-practice` adds only the two practice files.
50. Practice never enters measured files or counts.
51. Practice free text follows its pinned policy (a stored practice value is exported only under `include_explicit`).

### Output

52. Validation failure leaves no directory.
53. Existing destination refuses without `--force`.
54. `--force` failure leaves the previous bundle unchanged.
55. Manifest lists every file with correct row counts.
56. Manifest SHA256 match the files.
57. Manifest has no clinician name.
58. Two exports of an unchanged DB are byte identical apart from the manifest timestamp.
59. Subprocess: refusal exits 1, success 0.

### Legacy / regression

60. `export-answers` behaviour unchanged; its drift refusal names `export-phase2`.
61. All S11a–S11m tests stay green; full CI green.

## Out of scope

Statistics, imputation, further de identification, warehouse upload, Parquet, a name mapping in the bundle, materialised summary tables, new randomisation or PP definitions, retention.

## Acceptance

One command exports a read consistent, pseudonymised, linked Phase 2 bundle for a study with several valid configuration versions; every row carries the identity and provenance to join and interpret it; incomplete cases and planned vs realised randomisation are reconstructable; raw events reproduce timing and panel summaries; PP and missingness are exactly S11l's; practice, free text and keyfile obey their boundaries; provenance inconsistencies refuse instead of being repaired.

## Checklist items closed

Sections 31 (export set), 32 (common identifiers), 33 (mixed configuration export), plus the export deferrals in sections 3, 5, 9 and 21–27.
