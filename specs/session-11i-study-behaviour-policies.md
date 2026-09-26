# Session 11i — Study behaviour policy switches

## Goal

Move the remaining clinician facing study behaviour behind explicit configuration instead of embedding first use case assumptions in route or template code.

S11i covers backward navigation, performance feedback, practice state, and free text handling.

S11a through S11h are assumed complete.

## Core invariants

1. Measured behaviour is controlled by the case pinned configuration.
2. A configuration change affects only subsequently activated cases.
3. Backward navigation never makes a historical answer editable.
4. Revisit activity never overwrites the original measured timepoint timing.
5. Practice observations are explicitly distinguishable from measured observations.
6. Practice data never contributes to measured randomisation balance, ITT, PP, or clinician stopping counts.
7. Free text is disabled by default for Phase 2.
8. When free text is enabled it is treated as potentially identifying and is excluded from behavioural telemetry summaries/figures.
9. The default feedback policy exposes no ground truth, correctness, running score, or AI correctness.
10. None of these policies may be toggled by a clinician through query parameters or browser state.

## Study behaviour configuration

Extend `StudyConfig` with an optional block:

```
study_behaviour:
  backward_navigation: allow_readonly | prohibit

  feedback:
    show_ground_truth: false
    show_correctness: false
    show_running_score: false
    show_ai_correctness: false

  practice:
    enabled: false
    patient_ids: []
    arm: no_ai

  free_text:
    enabled: false
    routine_export: exclude
```

The block is omitted from canonical serialization when absent so historical snapshots remain byte stable.

For Phase 2 studies that use the new behaviour system, all values are part of the configuration snapshot and therefore participate in `config_hash`.

### Backward navigation default

Do not invent a first use case scientific choice that is not locked by the Phase 2 gate.

The platform supports both policies. The real first use case configuration must select one explicitly before measured data collection.

For legacy configurations without `study_behaviour`, preserve existing read only backward behaviour.

### Feedback default

All feedback flags default to false.

The first use case uses all false.

### Practice default

`enabled: false`.

The first use case uses no practice cases.

### Free text default

`enabled: false` in Phase 2 behaviour configuration.

`routine_export` supports at least:

- `exclude`
- `include_explicit`

`include_explicit` means the operator has deliberately configured routine research export inclusion. It does not make free text safe for figures or telemetry summaries.

## Backward navigation policy

### `allow_readonly`

A clinician may revisit a previously unlocked timepoint.

Requirements:

- previous answers remain disabled/frozen
- `POST /answer` continues to refuse the historical timepoint
- `POST /advance` from a historical timepoint is refused/stale
- the page is explicitly marked as a revisit in the DOM
- the server can distinguish a revisit render from the original presentation
- the S10 original `timepoint.enter`/`timepoint.exit` pair remains untouched
- later S11j/S11k telemetry can label exposure as `visit_kind=revisit`

Add a non sensitive event such as:

`timepoint.revisit`

with payload:

```
{
  "t_index": <int>
}
```

This is an occurrence marker, not a replacement timing interval.

### `prohibit`

A request for any measured `t_index < unlocked_t_index` is refused before slicing patient data.

Behaviour:

- ordinary request -> clean 409 or redirect to the current frontier with an explanatory message
- HTMX request -> whole page safe redirect or equivalent existing route convention
- no historical patient slice is rendered
- no revisit event is written
- no answer/timing state changes

The patient jumper and keyboard navigation must not offer a backward measured link when prohibited.

Server enforcement is required even if the link is absent from the UI.

## Revisit identity for later telemetry

Add stable render metadata on `_patient_view.html`, for example:

```
data-visit-kind="primary|revisit"
```

A primary render is the current editable frontier.

A revisit is a permitted render behind the frontier.

S11j and S11k use this flag so revisit foreground/panel exposure can remain identifiable without contaminating the original primary exposure interval.

## Feedback policy

Create one feedback policy object resolved from the case pinned study configuration.

UI code asks the policy whether each feedback surface is allowed. Do not scatter first use case `False` constants through templates.

Supported switches:

- ground truth
- clinician correctness
- running performance score
- AI correctness

When a flag is false:

- the corresponding information is not present in clinician facing HTML
- it is not present in hidden DOM or serialized client state

When enabled in a test/future study, rendering must require an explicit feedback provider/source. If the configured feedback source is unavailable, fail preflight rather than silently showing incomplete or fabricated feedback.

S11i does not define a universal clinical ground truth schema. Study specific providers remain adapter/configuration responsibilities.

## Practice mode

Practice support is explicit and separate from measured Start case.

### Initial implementation boundary

To avoid collisions with the measured answer/progress keys, S11i requires configured practice patient IDs to be disjoint from measured `study.patient_ids`.

This is a platform implementation constraint for the first practice implementation. A later session may relax it by extending all analysis keys with observation mode.

Configuration validation refuses overlap.

### Practice start

When enabled, expose a separate practice entry point, for example:

`POST /practice/start`

Never overload measured `POST /case/start` with an implicit practice flag.

Practice cases:

- use only `practice.patient_ids`
- use the configured fixed practice arm
- are labelled `observation_mode='practice'`
- do not consume an S11c schedule item
- do not write `phase2_randomized` arm assignments
- do not affect replacement selection
- do not count toward target completed cases
- do not count toward maximum activated measured cases

The configured practice arm is a training presentation choice, not a randomised assignment.

### Practice persistence

Add the next schema migration, expected migration 11 after the S11h answer provenance migration.

Add:

```
observation_mode TEXT NOT NULL DEFAULT 'measured'
    CHECK (observation_mode IN ('measured', 'practice'))
```

to at least:

- `sessions`
- `progress`
- `answers`

Existing rows backfill `measured`.

Add a small `practice_cases` table keyed by clinician and practice patient to record:

- clinician ID
- patient ID
- configured practice arm
- configuration version/hash
- started timestamp
- completed timestamp where applicable

Do not store practice cases in `randomisation_schedule_items` or as `phase2_randomized` assignments.

All answer/progress/session DAOs must validate that a row cannot silently change observation mode.

### Practice lifecycle

The first practice implementation may reuse question gating and answer capture, but practice status must flow through `SessionContext`.

Practice data may be retained for QA/training review, but later Phase 2 research exports must exclude it from ITT and PP datasets by default.

The current first use case keeps practice disabled, so no practice UI is shown there.

## Clinician stopping integration

Every S11e clinician stopping count must explicitly filter to measured Phase 2 cases.

Practice sessions and practice answers must not affect:

- completed measured count
- activated measured count
- replacement eligibility
- randomisation balance

Add regression tests around the existing `index_state()` / Start case limit queries so future refactors cannot accidentally count practice.

## Free text policy

### Configuration validation

When a Phase 2 case's behaviour policy has:

```
free_text.enabled = false
```

any free text question in that case's question configuration is a configuration/preflight error.

Do not merely hide the question at runtime while leaving it configured as required.

Legacy non Phase 2 configs remain supported under their historical semantics.

### Enabled free text

When enabled:

- existing autosave length and validation protections remain
- value is treated as potentially identifying
- answer audit events continue to omit raw free text
- behavioural events must never include raw free text
- divergence/behavioural figures must not display it
- telemetry summaries must not display it

### Routine export policy

Update the current general answer export so free text handling is explicit.

For `routine_export: exclude`:

- omit free text value columns from routine answer CSV output
- preserve the underlying database row
- optionally expose a non identifying `free_text_present` indicator only if the export contract explicitly names it

For `routine_export: include_explicit`:

- include the answer value only because the study configuration explicitly opted in
- retain existing CSV injection protections
- clearly identify the column as potentially identifying in documentation

S11n may later replace the current export with the Phase 2 linked export set, but S11i must not leave the existing exporter accidentally leaking newly enabled free text.

## Configuration provenance

These behaviour settings are part of the case pinned study snapshot.

Examples:

- a case started while backward navigation is prohibited remains prohibited even after a new config allows it
- a case started with free text disabled cannot gain a free text question through a later active version
- practice policy changes apply to newly started practice cases

## Preflight additions

Extend preflight to verify:

- practice patient IDs exist in the dataset
- practice and measured patient lists are disjoint for this implementation
- configured feedback source exists whenever any feedback flag is true
- Phase 2 free text questions are absent when free text is disabled
- free text export policy is valid when free text is enabled

## Files expected to change

- `src/ehr_simulator/config/study.py`
- `src/ehr_simulator/config/loader.py`
- `src/ehr_simulator/db/migrations.py`
- session/progress/answers DAOs
- new practice persistence/service module
- `src/ehr_simulator/web/study_session.py`
- `src/ehr_simulator/web/routes.py`
- `src/ehr_simulator/web/gating.py`
- index, summary, question, and navigation templates
- keyboard/navigation JS
- `src/ehr_simulator/export.py`
- `src/ehr_simulator/divergence.py` regression coverage for free text exclusion
- `src/ehr_simulator/cli_support.py` preflight
- Phase 2 example config and tests

## Required tests

### Backward navigation

1. `allow_readonly` permits rendering a prior unlocked timepoint.
2. Revisited answers are disabled.
3. POST answer to a revisited timepoint is refused.
4. POST advance from a revisited timepoint is refused/stale.
5. Revisit render is marked `visit_kind=revisit`.
6. Revisit writes a revisit event without raw clinical/answer data.
7. Revisit does not create or overwrite S10 primary timing events.
8. `prohibit` refuses a backward GET before patient slicing.
9. Prohibited UI does not offer a backward control.
10. Manually typing a backward URL remains refused.

### Feedback

11. Default feedback policy renders no ground truth.
12. Default feedback policy renders no correctness feedback.
13. Default feedback policy renders no running score.
14. Default feedback policy renders no AI correctness feedback.
15. Enabling a feedback flag changes `config_hash`.
16. An enabled feedback surface is controlled by configuration, not a hard coded constant.
17. Missing configured feedback source fails preflight.

### Practice

18. Practice disabled shows no practice start control.
19. Practice/measured patient overlap is rejected by configuration validation.
20. Practice Start uses only a configured practice patient.
21. Practice Start does not consume a randomisation schedule item.
22. Practice Start does not create a `phase2_randomized` assignment.
23. Practice answer rows are labelled `observation_mode=practice`.
24. Practice completion does not increment measured completion count.
25. Practice start does not increment measured activated count.
26. Practice does not change patient or clinician randomisation balance.
27. Practice does not trigger replacement scheduling.
28. Existing rows migrate as `observation_mode=measured`.

### Free text

29. Phase 2 free text question is rejected when free text is disabled.
30. Free text renders and saves when explicitly enabled.
31. Behavioural/audit events contain no raw free text value.
32. Divergence/behavioural figure path does not display free text.
33. Routine export excludes free text under `routine_export=exclude`.
34. Routine export includes it only under `include_explicit`.
35. Changing free text policy changes `config_hash`.

### Case pinned policy

36. An old case keeps its old backward navigation policy after a new config activates.
37. An old case keeps its old feedback/free text policy after a new config activates.
38. New cases use the new active policy.

### Regression

39. First use case practice remains disabled.
40. First use case feedback remains fully disabled.
41. All S11a through S11h tests remain green.
42. Full CI remains green.

## Explicit non goals

S11i does not implement:

- a universal ground truth data model
- statistical performance scoring
- participant eligibility/recruitment workflow
- clinician covariate collection
- browser focus/active timing
- panel exposure
- PP classification
- multi tab conflict resolution
- final Phase 2 exports

## Acceptance

S11i is complete when backward navigation is enforced from case pinned configuration, feedback defaults to no study outcome/performance information, practice observations are explicit and provably excluded from measured allocation/counting, free text is opt in with explicit export treatment, historical cases retain their original policy, and the complete test suite passes.
