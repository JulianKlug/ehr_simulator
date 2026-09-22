# Session 11 Phase 2 Implementation Checklist

Purpose: engineering checklist derived from `specs/policy/phase2-gate.md`.

This file is not the scientific Phase 2 gate and must not introduce new study design decisions.

If implementation exposes a missing scientific decision, return it to the Phase 2 gate rather than selecting a value in code.

---

# 1. Recommended scope split

The Phase 2 requirements are significantly broader than the original roadmap definition of Session 11.

If implementation exceeds the normal one to two day session budget, split work rather than enlarging S11 indefinitely.

---

# 2. Study identity

- [ ] Add `study_id` to study configuration.
- [ ] Validate the study ID format.
- [ ] Associate every study database with one study ID.
- [ ] Use the study ID in the default database filename.
- [ ] Persist study identity inside the database.
- [ ] Refuse startup when configured study ID conflicts with database study ID.
- [ ] Include `study_id` in Phase 2 exports.
- [ ] Add regression tests preventing cross study database reuse.

---

# 3. Configuration version history

- [ ] Add human readable `config_version`.
- [ ] Continue generating immutable `config_hash`.
- [ ] Add persistent configuration history.
- [ ] Store activation timestamp.
- [ ] Store change description.
- [ ] Support optional change reason.
- [ ] Preserve historical configuration versions.
- [ ] Associate every newly activated case with its active configuration version.
- [ ] Do not change the configuration identity of an already activated case.
- [ ] Permit new configuration versions within the same `study_id`.
- [ ] Update export behaviour so mixed valid configuration versions are allowed.
- [ ] Continue refusing missing or inconsistent configuration identities.
- [ ] Produce a summary of cases and observations by configuration version.

---

# 4. Explicit case activation

- [ ] Introduce an explicit **Start case** action.
- [ ] Do not activate randomisation on GET.
- [ ] Do not activate randomisation from patient index navigation.
- [ ] Conceal AI versus no AI assignment before case start.
- [ ] Commit assignment atomically when Start case succeeds.
- [ ] Record activation timestamp.
- [ ] Record active configuration version.
- [ ] Record case position.
- [ ] Record allocated patient.
- [ ] Record allocated arm.
- [ ] Ensure activated assignment is immutable.
- [ ] Treat reopening as resume rather than new activation.
- [ ] Add regression test proving an accidental GET does not consume an allocation.

---

# 5. Case lifecycle

Support explicit states sufficient to distinguish:

- [ ] planned
- [ ] activated
- [ ] active
- [ ] paused
- [ ] resumed
- [ ] completed
- [ ] incomplete or abandoned

Also:

- [ ] Record pause events.
- [ ] Record resume events.
- [ ] Record interruption timeout.
- [ ] Support study configurable reconnection grace period.
- [ ] Support study configurable voluntary pause behaviour.
- [ ] Ensure interruption beyond the allowed interval leaves the original case activated and incomplete.
- [ ] Never return an activated assignment to the allocation pool.
- [ ] Preserve incomplete cases in exports.
- [ ] Preserve structured incomplete reason where known.

---

# 6. Replacement cases

- [ ] Support replacement case scheduling.
- [ ] Draw replacement only from patients not previously seen by the clinician.
- [ ] Never overwrite the original incomplete case.
- [ ] Record replacement relationship.
- [ ] Include both original and replacement in audit exports.
- [ ] Preserve AI versus no AI balance as closely as possible.
- [ ] Preserve patient level balance as closely as possible.
- [ ] Respect study specific block constraints.

---

# 7. Clinician level stopping

- [ ] Support configurable target completed cases per clinician.
- [ ] Support configurable maximum activated cases per clinician.
- [ ] Stop assigning new cases when the relevant clinician level rule is reached.
- [ ] Do not implement automatic whole study stopping.
- [ ] Permit optional planned sample targets for display only.

---

# 8. Adaptive randomisation scheduler

Replace the original independent pairwise arm assignment design.

The scheduler must support:

- [ ] study master randomisation seed
- [ ] versioned randomisation algorithm
- [ ] random patient order per clinician
- [ ] clinician level AI versus no AI balance
- [ ] patient level AI versus no AI balance
- [ ] adaptive balancing based on activated assignments
- [ ] study configurable block length
- [ ] study configurable block sequence
- [ ] balanced starting arm
- [ ] case position metadata
- [ ] block position metadata
- [ ] preceding arm metadata
- [ ] cases since previous AI exposure metadata where applicable

Existing activated assignments must never be rewritten.

Schedules already generated for other clinicians must not be rewritten merely because a later clinician enrols or withdraws.

---

# 9. Randomisation provenance

For every generated schedule retain:

- [ ] `study_id`
- [ ] clinician ID
- [ ] schedule generation timestamp
- [ ] master seed or derived seed information
- [ ] algorithm version
- [ ] allocation state used during generation
- [ ] configuration version used during generation
- [ ] planned patient order
- [ ] planned arm
- [ ] starting arm
- [ ] block information where applicable
- [ ] overall case position

For every realised assignment retain:

- [ ] activated yes or no
- [ ] activation timestamp
- [ ] configuration version at activation
- [ ] completion state
- [ ] replacement relationship if applicable

---

# 10. AI condition delivery

- [ ] Show AI panel only in AI assigned cases.
- [ ] Remove the AI panel entirely in no AI cases.
- [ ] Do not render an AI placeholder in no AI cases.
- [ ] Do not render an AI unavailable message.
- [ ] Do not leave intervention revealing empty space where avoidable.
- [ ] Preserve identical non AI functionality across arms.
- [ ] Associate displayed AI output with frozen artifact identity.
- [ ] Preserve AI prediction artifact hash.
- [ ] Preserve explanation artifact hash where applicable.
- [ ] Preserve model/system version.
- [ ] Preserve template/presentation version.
- [ ] Preserve intervention build identifier.

---

# 11. Temporal validity checks

- [ ] Add study preflight checks preventing direct outcome leakage.
- [ ] Verify no clinician facing variable directly encodes the reference outcome.
- [ ] Verify no post timepoint clinical data is exposed.
- [ ] Verify precomputed AI output corresponds to the appropriate timepoint.
- [ ] Preserve evidence of the frozen AI artifact used by the study.

---

# 12. Conditional questions

The question system must support branching.

Required first use case logic:

- [ ] `deterioration_6h = Yes` displays primary cause question.
- [ ] `deterioration_6h = No` hides and ungates primary cause question.
- [ ] `mRS 0 to 2 at 3 months = Yes` automatically sets death at 3 months to No.
- [ ] `mRS 0 to 2 = No` permits explicit death at 3 months response.
- [ ] Conditional question state survives refresh/resume.
- [ ] Gating uses only questions currently required under the active branch.
- [ ] Hidden conditional questions cannot leave stale responses unless explicitly defined.
- [ ] Add regression coverage for branch changes after an answer has already been saved.

---

# 13. First use case questions

Support configuration of:

- [ ] deterioration within next six hours, Yes or No
- [ ] confidence, five point Likert scale
- [ ] primary cause of deterioration, categorical
- [ ] mRS 0 to 2 at three months, Yes or No
- [ ] death at three months, Yes or No

The first study must not silently inherit the existing example questions for:

- [ ] hospital survival
- [ ] six month death
- [ ] contributing factors
- [ ] free notes

Update example or study specific question configuration accordingly.

---

# 14. Browser visibility and focus telemetry

Add client side telemetry sufficient to identify:

- [ ] `document.visibilityState`
- [ ] window focus
- [ ] window blur
- [ ] browser/tab identifier

Use a monotonic browser clock for duration measurement.

- [ ] Use `performance.now()` or equivalent for local durations.
- [ ] Retain server timestamps for ordering and audit.
- [ ] Do not include network latency in viewport exposure duration.

---

# 15. Foreground time

Derive per clinician, patient, and timepoint:

`foreground_seconds`

Accumulate only while:

- [ ] document visible
- [ ] browser focused

Retain existing:

`elapsed_seconds`

Do not replace wall clock timing with foreground timing.

---

# 16. Active time

Derive:

`active_seconds`

Requirements:

- [ ] configurable inactivity threshold
- [ ] first use case default of 60 seconds
- [ ] activity resumes after qualifying interaction
- [ ] passive mouse movement does not reset inactivity

Qualifying activity should include:

- [ ] click
- [ ] touch interaction
- [ ] scroll
- [ ] keyboard input
- [ ] answer modification
- [ ] panel open or close
- [ ] timepoint navigation

---

# 17. Panel telemetry

Instrument all major information panels.

For each panel capture sufficient raw information to derive:

- [ ] cumulative viewing duration
- [ ] viewed yes or no
- [ ] viewing episode count
- [ ] time to first view
- [ ] first view timestamp
- [ ] last view timestamp
- [ ] panel open count

Qualifying panel exposure requires:

- [ ] viewport threshold met
- [ ] document visible
- [ ] browser focused
- [ ] expanded content visible

Collapsed header visibility must not count.

---

# 18. Panel viewing configuration

Support a study wide panel viewport threshold.

For the first use case:

- [ ] viewport threshold = 5 percent
- [ ] cumulative viewed threshold = 2 seconds

Apply the same threshold to all instrumented panels.

Reset cumulative exposure when advancing to a new timepoint.

Do not require recent interaction for panel exposure accumulation.

---

# 19. Panel exposure episodes

Record raw transitions sufficient to reconstruct episodes.

Episode start:

- [ ] viewport threshold becomes satisfied
- [ ] document visible
- [ ] browser focused
- [ ] content expanded

Episode end when any qualifying condition becomes false.

Where useful, retain end reason:

- [ ] scroll out
- [ ] tab hidden
- [ ] focus lost
- [ ] panel collapsed
- [ ] timepoint exit
- [ ] case interruption

Raw events remain the source of truth.

---

# 20. Panel open and close events

For every collapsible panel record:

- [ ] panel opened
- [ ] panel closed
- [ ] panel ID
- [ ] timestamp
- [ ] clinician
- [ ] patient
- [ ] timepoint
- [ ] case/session identifier
- [ ] tab identifier

Do not infer panel viewed status merely from open state.

---

# 21. AI viewed classification

For every AI assigned clinician, patient, and timepoint:

- [ ] calculate cumulative qualifying AI exposure
- [ ] derive `ai_viewed`
- [ ] reset exposure on each new timepoint

For the first use case:

`ai_viewed = cumulative qualifying AI exposure >= 2 seconds`

Keep continuous duration in addition to the binary classification.

Support derived case level summaries without replacing the timepoint level source measure.

---

# 22. Per protocol variables

For each observation export enough information to derive:

- [ ] assigned arm
- [ ] AI actually delivered
- [ ] AI viewed threshold reached
- [ ] intervention leakage
- [ ] intervention failure
- [ ] PP compliant yes or no

AI assigned PP compliant:

- AI delivered
- AI viewed threshold reached

No AI PP compliant:

- AI unavailable as intended
- no AI leakage

Do not alter ITT assignment based on PP compliance.

---

# 23. Intervention failure and leakage

Add structured events for:

- [ ] AI render failure
- [ ] missing AI artifact
- [ ] AI display failure
- [ ] accidental AI exposure in no AI condition
- [ ] other intervention integrity failure

Never silently change the randomised arm because of a technical failure.

Preserve original assignment for ITT.

---

# 24. Missing response provenance

Do not impute missing answers.

Where possible preserve structured states for:

- [ ] case abandoned
- [ ] technical failure
- [ ] reached timepoint but unanswered
- [ ] timepoint never reached

Ensure these states are exportable.

---

# 25. Backward navigation configuration

Support a study setting controlling backward timepoint navigation.

When enabled:

- [ ] previous answers remain frozen
- [ ] revisits are distinguishable from first presentation
- [ ] revisit timing does not overwrite original timing
- [ ] panel exposure during revisits remains identifiable

When disabled:

- [ ] reject backward measured case navigation cleanly

---

# 26. Feedback configuration

Support study configurable performance feedback.

Default:

- [ ] no ground truth
- [ ] no correctness feedback
- [ ] no running score
- [ ] no AI correctness feedback

First use case uses this default.

---

# 27. Practice mode

Support explicit practice state for future studies.

Practice observations must be distinguishable from measured observations.

Practice data must not affect:

- [ ] ITT population
- [ ] PP population
- [ ] target completed case count
- [ ] randomisation balance

First use case:

- [ ] practice cases disabled

---

# 28. Clinician identity privacy

- [ ] Remove `name_normalized` from new behavioural event payloads.
- [ ] Keep clinician name only where operationally required.
- [ ] Continue using `clinician_id` in routine events.
- [ ] Preserve existing optional name mapping keyfile support.
- [ ] Keep keyfile separate from routine research exports.
- [ ] Retain existing keyfile filesystem protection.

Historical data do not require migration solely to remove names from old event payloads.

---

# 29. Free text

- [ ] Free text disabled by default.
- [ ] Study configuration may explicitly enable free text.
- [ ] Treat enabled free text as potentially identifying.
- [ ] Do not expose free text through telemetry summaries.
- [ ] Do not expose free text through behavioural figures.
- [ ] Make routine export behaviour explicit when free text is enabled.

---

# 30. Backup identity

Ensure backups remain attributable to:

- [ ] `study_id`
- [ ] schema/database version
- [ ] backup timestamp

Do not merge backups across studies.

Retention remains an operational policy rather than an automatic simulator rule.

---

# 31. Phase 2 export set

Do not put all Phase 2 information into one answers CSV.

Provide linked outputs for at least:

## Answers and timepoints

- [ ] clinician responses
- [ ] arm
- [ ] timing summaries
- [ ] completion status
- [ ] configuration version

## Panel summaries

- [ ] panel viewed
- [ ] cumulative duration
- [ ] episode count
- [ ] first view latency
- [ ] first and last view timestamps
- [ ] panel open counts

## Raw events

- [ ] behavioural source events
- [ ] tab identity
- [ ] configuration identity

## Randomisation audit

- [ ] planned schedule
- [ ] generated arm
- [ ] activated status
- [ ] activation timestamp
- [ ] lifecycle outcome
- [ ] replacement links

## Configuration history

- [ ] `config_version`
- [ ] `config_hash`
- [ ] activation timestamp
- [ ] description
- [ ] reason where present

---

# 32. Common export identifiers

Ensure linked outputs expose stable join keys including:

- [ ] `study_id`
- [ ] `clinician_id`
- [ ] `patient_id`
- [ ] timepoint
- [ ] case/session identity
- [ ] `config_version`
- [ ] `config_hash`

---

# 33. Mixed configuration export behaviour

Replace the current blanket mixed generation refusal.

- [ ] Permit multiple valid configuration versions inside one study export.
- [ ] Keep every observation attributable to one version.
- [ ] Refuse missing configuration identity.
- [ ] Refuse unknown configuration identity.
- [ ] Refuse internal inconsistencies.
- [ ] Produce counts by configuration version.

---

# 34. Multi tab protection

Add explicit browser tab identity.

- [ ] Generate per tab identifier.
- [ ] Include it in behavioural events.
- [ ] Detect simultaneous active views of the same measured case.
- [ ] Prefer preventing conflicting active case views.
- [ ] If prevention cannot be guaranteed, make the conflict auditable.

---

# 35. Test requirements

The eventual S11 specification should contain a complete numbered test inventory.

At minimum include regression coverage for:

- [ ] GET does not consume randomisation.
- [ ] Start case activates exactly once.
- [ ] Activated assignment is immutable.
- [ ] Patient is never repeated for the same clinician.
- [ ] Adaptive scheduling improves or preserves configured balance.
- [ ] Existing activated assignments survive later scheduling.
- [ ] Starting arm balancing works.
- [ ] Randomisation reproduces from stored inputs.
- [ ] Study ID mismatch refuses startup.
- [ ] Configuration change does not mutate active cases.
- [ ] Mixed valid configuration versions export successfully.
- [ ] Invalid configuration provenance still refuses export.
- [ ] No AI condition contains no AI panel.
- [ ] AI condition displays only the frozen intervention artifact.
- [ ] Conditional cause question gating works.
- [ ] Three month outcome dependency works.
- [ ] Background tab time does not count toward panel exposure.
- [ ] Unfocused browser time does not count toward panel exposure.
- [ ] Separate exposure episodes accumulate correctly.
- [ ] Exposure resets at the next timepoint.
- [ ] Collapsed panel does not accumulate exposure.
- [ ] AI viewing does not require recent interaction.
- [ ] Active time excludes inactivity beyond threshold.
- [ ] Passive mouse movement does not reset inactivity.
- [ ] Panel open does not automatically imply viewed.
- [ ] Intervention failure preserves ITT arm.
- [ ] AI leakage preserves no AI ITT arm.
- [ ] PP compliance is observation specific.
- [ ] Missing responses remain missing.
- [ ] Replacement case never repeats a patient.
- [ ] Pause/resume within configured grace period works.
- [ ] Timeout produces an incomplete activated case.
- [ ] Behavioural events contain clinician ID but not clinician name.
- [ ] Tab conflicts are prevented or recorded.

---

# 36. Items that must remain configurable

Implementation must not hard code:

- number of clinicians
- number of cases
- patient pool
- eligibility criteria
- timepoints
- block length
- block order
- reconnection grace period
- voluntary pause policy
- backward navigation policy
- deterioration reference field
- cause categories
- sample targets
- AI artifact
- statistical model
- statistical power assumptions
- retention duration

---

# 37. Definition of done

Phase 2 software implementation is complete only when the simulator can:

1. Run a study under a unique study identity.
2. Preserve configuration history.
3. Generate auditable constrained randomisation.
4. Activate assignments only on explicit case start.
5. Preserve incomplete activated cases.
6. Issue nonduplicating replacements where configured.
7. Deliver AI and no AI conditions correctly.
8. Record intervention failures and leakage.
9. Capture wall clock, foreground, active, and panel exposure telemetry.
10. Derive observation level AI viewed status.
11. Support conditional first use case questions.
12. Preserve ITT and PP relevant variables.
13. Export linked answers, telemetry, allocation, and configuration datasets.
14. Preserve configuration provenance for every research observation.
15. Avoid introducing scientific decisions that belong to the study protocol.

