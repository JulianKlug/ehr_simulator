# Session 11o — Phase 2 integration, regression gate and documentation reconciliation

## Goal

Prove that Sessions 11a through 11n operate as one coherent Phase 2 system and reconcile the repository documentation with the implemented design.

No major scientific or platform feature originates in S11o.

S11o is the final integration/regression gate. When an integration scenario exposes a defect, fix the owning implementation while preserving its locked specification. Do not solve an integration failure by inventing a new study policy.

## Review revisions (2026-09-28)

Changes against the first draft, from the implemented S11a–S11n code:

1. **Failure and leakage are generated through public boundaries, no test hooks.** S11l has no `intervention.*` events. Scenario E uses an AI render reported `shown` whose complete telemetry carries no AI `panel.mount` (`display_failure`, posted through `/telemetry/events`). The `missing_artifact` path (boot refuses a mismatched artifact, so it cannot be staged end to end on the synthetic reference) stays covered by the S11g/S11l unit tests. Scenario F posts an AI `panel.mount` for a no AI render through `/telemetry/events` (leakage).
2. **Status name** is `multi_tab` (S11m revision 1).
3. **Multi tab e2e** uses two Playwright pages in one browser context: shared cookie, separate `sessionStorage`, so two tab ids.
4. **Legacy export on the mixed database** is refused by the existing hash drift rule; the assertion is that refusal plus the `export-phase2` hint (S11n revision 6).
5. **An open case blocks Start case** (S11d/S11e: active or paused). Scenario D therefore resumes case A under the v2 server and finishes it before case B starts; A stays pinned to v1 throughout.
6. **Harmless v2 change** for Scenario D: `case_lifecycle.study_target_completed_cases` (display only, S11e) — it moves `config_hash` without a new scientific parameter.
7. **Test files** trimmed to three: `tests/test_phase2_integration.py` (TestClient + CLI, scenarios A–G, I–K), `tests/e2e/test_phase2_walk.py` (AI and no AI browser walks, S11h branch), `tests/e2e/test_multitab_walk.py` (scenario H; shared with S11m test 46).

## Core invariants

1. S11o introduces no new scientific design decision.
2. Randomisation, lifecycle, intervention, telemetry, PP, privacy, and export thresholds remain exactly those already locked in the Phase 2 gate and S11a through S11n specs.
3. Integration tests exercise public/service boundaries rather than reimplementing subsystem logic in test code.
4. One end to end case remains traceable from planned schedule through activation, clinician interaction, lifecycle outcome, telemetry, and export.
5. Mixed configuration history remains valid and old cases stay pinned.
6. Failures/leakage never rewrite ITT assignment.
7. Incomplete cases are preserved and replacements never erase originals.
8. Multi tab conflicts are prevented or auditable and never silently double count exposure.
9. Routine exports remain pseudonymised and linked.
10. Phase 1/non study compatibility covered by existing regression tests remains intact.
11. Every implementation checklist item is marked complete only when a corresponding implementation/test exists.
12. Outdated roadmap text describing Session 11 as independent pairwise coin flip randomisation is removed or explicitly superseded.

## Existing behaviour being replaced

Before S11o, each Session 11 component has subsystem tests and local acceptance criteria, but no single repository level contract proves that the complete path works across all components.

S11o adds that cross component proof and updates documentation that still describes earlier architecture.

## In scope

### Integration fixture

Create one small deterministic Phase 2 synthetic study fixture suitable for service and browser integration tests.

The fixture must exercise existing configuration capabilities rather than add new ones. It should include:

- stable `study_id`
- explicit configuration version activation in tests
- deterministic randomisation seed and block sequence
- enough measured patients to exercise AI and no AI cases plus one replacement
- at least two timepoints
- S11e lifecycle with replacement enabled
- frozen synthetic AI intervention provenance matching S11g
- explicit clinician facing prohibited fields declaration
- S11h first use case conditional questions
- S11i read only backward navigation
- feedback all false
- practice disabled for the primary integration study
- free text disabled or excluded from routine export
- S11j telemetry thresholds including 60 second inactivity, 5 percent panel viewport threshold, 2 second viewed threshold

Use a second configuration version that changes only `case_lifecycle.study_target_completed_cases` (display only), preserving `study_id` and dataset. No new scientific parameter is invented to force a hash change.

### Scenario A — normal AI case

Exercise:

```
schedule
→ Start case
→ AI arm exact frozen intervention
→ answer branch
→ browser/foreground/panel telemetry
→ AI viewed threshold reached
→ completion
→ Phase 2 export
```

Assert:

- planned arm and realised arm are AI
- case activation pins the expected version/hash
- AI DOM exists and no wrong timepoint/model row is shown
- AI mounted/delivery evidence exists
- cumulative primary AI exposure reaches the configured threshold
- `ai_viewed=true`
- PP compliant for the AI observation when all conditions are met
- timing/panel summaries match raw events
- exported timepoint/answer/panel/randomisation rows join on stable identifiers

### Scenario B — normal no AI case

Exercise the same lifecycle on a scheduled no AI item.

Assert:

- no AI panel/tab/placeholder/payload leaks into clinician HTML
- immutable assignment remains no AI
- no leakage event is present
- no AI PP is compliant when AI remains unavailable as intended
- non AI clinical functionality/questions remain equivalent
- export records no fabricated AI panel summary

### Scenario C — incomplete case and replacement

Exercise:

```
activation
→ interruption/pause path
→ timeout or explicit operator abandonment
→ original case incomplete
→ deterministic replacement plan
→ Start case activates replacement
→ replacement completes
→ export
```

Assert:

- original case remains in `arm_assignments`, lifecycle, balance, and audit output
- original lifecycle reason is preserved
- replacement patient has never previously been activated for that clinician
- replacement link is reconstructable both directions
- replacement activates only through Start case
- both original and replacement count in later realised allocation state
- incomplete original remains in the Phase 2 bundle

### Scenario D — configuration update while a case exists

Exercise:

```
activate v1
→ start case A
→ activate v2
→ resume case A under the v2 server and finish it
→ start case B
→ export mixed versions
```

Assert:

- case A keeps v1 study/questions/behaviour/intervention provenance
- case B receives v2
- active pointer changes only through explicit activation
- mixed version Phase 2 export succeeds
- row level version/hash values are correct
- configuration counts agree with row level observations
- legacy `export-answers` refuses the mixed database (hash drift) and names `export-phase2`

### Scenario E — AI intervention failure

Create AI assigned observations where the S11l failure path triggers: a `shown` render whose complete telemetry has no AI `panel.mount` (`display_failure`).

Assert:

- assignment remains AI for ITT
- structured failure reason is retained
- `ai_delivered=false` when the failure prevents delivery
- PP is non compliant under the S11l rule
- no substitution to no AI occurs
- failure is represented in timepoint/raw event export

Do not mutate production artifact identity rules merely to make this test easy.

### Scenario F — no AI leakage

Post an AI `panel.mount` for a no AI render through `/telemetry/events` (the intake accepts any closed panel id; S11l reads it as leakage).

Assert:

- assignment remains no AI
- leakage is explicit
- PP is non compliant
- randomisation audit remains unchanged
- raw event/timepoint export carries the leakage fact

### Scenario G — conditional questions

Exercise both first use case branch directions and a branch change after a saved dependent answer.

Assert:

- deterioration Yes exposes cause
- changing to No hides/ungates cause and handles stale value under S11h rules
- good outcome Yes derives death No
- good outcome No permits/requires explicit death response according to the configured branch
- hidden question never blocks advance
- export distinguishes hidden/not applicable, rule generated, answered, and genuinely missing states
- refresh/resume preserves branch state

### Scenario H — multi tab conflict

Using the browser/e2e harness, open the same active measured case in two tab contexts.

Assert:

- first tab obtains ownership
- second live tab cannot become a measured writer
- second tab answer/advance/primary telemetry is blocked
- `tab.conflict` is auditable
- valid owner telemetry is not double counted
- when accepted telemetry from two tabs is deliberately constructed in a lower level fixture, derived status is `multi_tab` and durations are not summed

### Scenario I — backup identity and restore smoke

After the study has collected data:

- invoke the normal backup path
- assert destination is study isolated and names study/schema/timestamp
- open the backup read only
- require the same `study_id`
- require the same schema migration version
- run a read only Phase 2 export build from the backup and compare row counts/key provenance with the source snapshot taken before backup

Do not include the clinician keyfile in backup.

### Scenario J — privacy and keyfile separation

Assert across the complete workflow:

- new behavioural events contain no `name_normalized`
- routine Phase 2 bundle contains no clinician name mapping
- optional keyfile is produced only when explicitly requested
- keyfile is physically outside the bundle
- excluded free text cannot appear in behavioural/panel/manifest output

### Scenario K — legacy regression

Run representative existing Phase 1/non study flows:

- synthetic patient rendering
- `phase1_stub` intervention behaviour
- legacy answer/gating/timing export tests
- backup for an unbound/non study DB

S11o does not remove earlier behaviour merely because Phase 2 now exists.

## Integration test structure

Prefer a small number of readable scenario files over one giant test.

Files:

```
tests/test_phase2_integration.py
tests/e2e/test_phase2_walk.py
tests/e2e/test_multitab_walk.py
```

Reuse existing test helpers/fixtures for:

- config activation
- deterministic scheduler creation
- Start case
- lifecycle clock injection
- browser/e2e navigation
- raw event insertion only where a failure/leakage state cannot be generated naturally through the public browser flow

Tests must not duplicate scheduler, exposure, PP, or branch derivation algorithms.

## Complete Session 11 regression inventory

The final CI gate must execute:

- all existing tests from S11a through S11n
- all new S11o integration tests
- browser/e2e tests required for telemetry, conditional questions, and multi tab behaviour
- schema snapshot/migration tests
- CLI tests for activation, lifecycle operator commands, backup, and Phase 2 export

A failure in an earlier numbered Session 11 test blocks S11o acceptance.

Do not delete or weaken an earlier test merely because a later integration test overlaps it.

## Database/schema changes

None are planned.

If an integration test reveals that a schema change is genuinely required, that change belongs to the owning subsystem spec/implementation and must be reviewed there. S11o must not add an unreviewed catch all migration.

## Configuration changes

None are planned.

S11o may update example configuration files to contain the already specified S11a through S11m fields and first use case values.

No new config key or new study threshold originates here.

## Public API/route changes

None are planned.

S11o may fix defects in existing routes/CLI commands uncovered by integration, but must not add a new clinician workflow unless an earlier specification already requires it.

## Failure behaviour

Any of the following blocks Phase 2 completion:

- an integration scenario cannot be reproduced deterministically
- one subsystem requires current global config instead of pinned case config
- a no AI path exposes AI content
- intervention failure/leakage changes ITT assignment
- incomplete case/replacement history is lost
- multi tab exposure is silently summed
- mixed known config versions fail Phase 2 export
- unknown/missing config provenance is silently accepted
- routine export/keyfile privacy boundary is violated
- backup cannot be attributed to study/schema/time
- documentation describes behaviour inconsistent with tested implementation

The fix must preserve the Phase 2 gate decision record unless the gate itself is separately amended outside S11o.

## Documentation reconciliation

Update at least:

- `specs/ROADMAP.md`
- `specs/session11_roadmap.md`
- `specs/session-11-implementation-checklist.md`
- `README.md`
- `CLAUDE.md` or the repository's current architecture summary
- `configs/example_phase2_config.yaml`
- `configs/example_phase2_questions.yaml`
- CLI help text/docstrings for commands changed in S11a through S11n

### `specs/ROADMAP.md`

Replace/supersede text that describes Session 11 as a simple independent AI versus no AI pairwise assignment.

Document the implemented Phase 2 architecture at a high level:

- study identity and explicit config versions
- planned adaptive schedule versus realised Start case activation
- lifecycle/replacements
- AI/no AI delivery and conditional questions
- browser/panel telemetry and PP derivation
- privacy/multi tab protection
- linked Phase 2 export bundle

### Session 11 implementation checklist

Tick an item only when:

- implementation exists
- its numbered tests pass
- the integration scenarios do not contradict it

Add a brief note/link to the owning S11x spec where useful.

### README/operator documentation

Document the operational sequence, for example:

```
validate-config
activate-config
serve
expire-cases when required
backup
export-phase2
```

Explain:

- one database per study
- configuration activation is explicit
- Start case is the allocation boundary
- backups are study isolated
- Phase 2 uses linked exports
- keyfile is optional/separate
- mixed known configuration versions are supported by `export-phase2`

Do not expose study secrets or real clinician/patient data in examples.

### Example configuration/question files

Ensure examples use only already locked first use case choices.

The examples must pass:

- `validate-config`
- preflight on the synthetic reference fixture where applicable
- activation/boot tests

Do not add placeholder values that look like final scientific decisions when the gate still marks them study specific.

## Required tests

### Normal intervention paths

1. End to end AI case completes and exports with AI assignment/delivery/viewed/PP provenance.
2. End to end no AI case completes with no AI DOM surface and exports PP compliant when no leakage occurs.
3. AI and no AI cases share the same non intervention clinical/question functionality.
4. Raw events reproduce exported primary timing/panel summaries for the AI case.

### Lifecycle/replacement

5. Incomplete activated case remains in DB and export.
6. Replacement is planned deterministically without altering the original.
7. Replacement activates only through Start case.
8. Replacement never reuses a clinician's activated patient.
9. Original plus replacement remain reconstructable in randomisation audit.

### Configuration history

10. Case A started on v1 remains v1 after v2 activation.
11. Case B started after v2 uses v2.
12. Mixed version Phase 2 export succeeds.
13. Configuration counts match the integrated cases/timepoints.
14. Current active configuration never rewrites older case provenance.

### Integrity failures

15. AI delivery failure retains AI ITT arm and exports failure/non compliant PP.
16. No AI leakage retains no AI ITT arm and exports leakage/non compliant PP.
17. Deliberate unknown/mismatched config provenance makes export fail rather than repair.

### Conditional questions and missingness

18. Deterioration branch changes correctly after an earlier saved dependent answer.
19. Good outcome/death derivation behaves as specified.
20. Hidden question is not exported as missing.
21. Reached unanswered and unreached timepoint remain distinguishable in the final bundle.

### Multi tab

22. Second browser tab cannot save an answer for a live owned measured case.
23. Second browser tab cannot advance the case.
24. Second browser tab cannot contribute accepted primary telemetry while another live tab owns it.
25. Conflict is auditable.
26. Valid owner telemetry is not double counted.
27. Deliberate unresolved multi tab source data derives conflict/indeterminate rather than a summed duration.

### Privacy and backup

28. Integrated new events contain no `name_normalized` payload.
29. Routine Phase 2 export contains no clinician name mapping.
30. Explicit keyfile remains separate and protected.
31. Study backup path/content preserve study ID, schema version, and timestamp identity.
32. Restored/read only backup can build the same provenance/count snapshot.

### Documentation/config examples

33. Example Phase 2 config/questions pass `validate-config`.
34. Example Phase 2 config passes preflight on its supported synthetic fixture.
35. CLI `--help` includes the final Phase 2 commands/options and no obsolete wording.
36. Repository search finds no unsuperseded statement that Session 11 randomisation is independent pairwise coin flipping.
37. Session 11 implementation checklist has no unchecked item whose owning implementation/test is complete, and no checked item without evidence.

### Full regression

38. All S11a through S11n numbered tests pass.
39. Existing Phase 1/non study e2e smoke remains green.
40. Full CI passes from a clean checkout.

## Out of scope

S11o does not implement:

- a new randomisation algorithm
- a new replacement rule
- a new scientific endpoint or analysis model
- power/sample size decisions
- new telemetry thresholds
- new PP rules
- a new clinician identity scheme
- new retention policy
- automated statistical reporting
- deployment scaling/multi worker redesign beyond the already specified database concurrency contracts

## Final acceptance

Phase 2 implementation is complete when the integrated simulator can demonstrably:

1. identify and isolate one study
2. preserve immutable configuration history
3. generate reproducible constrained randomisation
4. activate assignment only through explicit Start case
5. preserve incomplete activated cases
6. schedule non duplicating replacements
7. deliver AI and no AI correctly
8. preserve intervention failure and leakage without changing ITT
9. record elapsed, foreground, active, and panel exposure timing
10. derive observation level AI viewed status
11. support the conditional first use case questions
12. preserve observation level ITT and PP variables
13. prevent or audit conflicting measured browser tabs
14. keep clinician names out of routine behavioural/research outputs
15. produce study identifiable backups
16. produce linked Phase 2 research exports across multiple known configuration versions
17. reconstruct planned and realised allocation including replacements
18. preserve configuration provenance for every research observation
19. keep practice/free text/keyfile data within their defined boundaries
20. preserve earlier supported non Phase 2 behaviour
21. avoid silently introducing unresolved study design decisions

S11o is accepted only when the complete regression suite and CI are green and the documentation describes the same behaviour that the tests prove.

## Checklist items closed

S11o closes:

- 35 Test requirements
- 37 Definition of done

It also verifies and reconciles every earlier Session 11 checklist section before Phase 2 is marked complete.
