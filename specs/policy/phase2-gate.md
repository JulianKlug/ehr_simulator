# Phase 2 Gate Decision Record

Status: **Phase 2 framework decisions locked**

Purpose: define the scientific, operational, behavioural telemetry, privacy, and study configuration decisions required before implementation of Phase 2 functionality.

This document intentionally does not prescribe the implementation details of Session 11. Engineering requirements derived from these decisions are tracked separately.

---

# 1. Scope

The EHRSimulator is intended to support multiple clinical research studies involving longitudinal EHR review with configurable clinician questions and optional AI assistance.

The simulator is primarily responsible for:

- Delivering the experimental condition.
- Presenting longitudinal patient information.
- Recording clinician responses.
- Recording intervention exposure.
- Recording behavioural telemetry.
- Preserving randomisation and configuration provenance.
- Producing reproducible research exports.

The simulator is **not** intended to perform statistical inference, power calculations, or final study analysis.

Statistical analysis belongs to the individual study protocol and downstream analysis workflow.

---

# 2. Phase 2 policy gate decisions

## 2.1 Registration

Formal OSF preregistration is not required for the current development stage.

Individual studies may later be preregistered when appropriate.

The simulator must therefore support different study specific outcomes, timepoints, analysis populations, randomisation structures, and intervention definitions without encoding one study permanently into the software.

## 2.2 Data handling

No dedicated IRB specific data handling document is required for the current development stage.

The current clinician identity approach may remain in use for Phase 2 subject to the privacy requirements in this document.

## 2.3 Competitive landscape

No direct competitor has been identified that combines:

- Longitudinal EHR replay.
- Configurable clinician response capture.
- Controlled AI exposure.
- Randomised experimental allocation.
- Behavioural telemetry.

A short record of reviewed systems and their differences should be retained for future documentation and publication work.

A systematic competitive review is not required for the Phase 2 gate.

---

# 3. Study identity

Every study instance must have a unique `study_id`.

Each study instance must use a separate SQLite database.

A database belonging to one `study_id` must not be reused for another study.

The database should be explicitly associated with the study identity, including through its default filename, for example:

`study_<study_id>.db`

The configured study identity must match the identity stored in the database.

`study_id` must also appear in research exports.

---

# 4. Study configuration versioning

Study configuration is not permanently frozen after data collection starts.

A study may be patched during ongoing data collection.

Every materially distinct configuration must have:

- `config_version`
- `config_hash`
- activation timestamp
- short change description
- optional reason for the change

Historical records must retain the version under which they were collected.

A configuration change does not automatically create a new `study_id`.

Any change capable of altering the intervention, clinician facing experience, allocation process, question structure, or exposure classification must create a new configuration version.

Examples include:

- AI prediction artifact.
- AI explanation artifact.
- AI model or system version.
- AI panel presentation.
- Question wording.
- Conditional question logic.
- Randomisation rules.
- Case scheduling rules.
- Viewing thresholds.
- Inactivity thresholds.
- Pause rules.
- Reconnection rules.

An already activated case remains associated with the configuration version under which it started.

A newer configuration version applies only to subsequently activated cases.

---

# 5. Participants

## 5.1 Eligibility

Eligibility criteria are study specific.

The EHRSimulator does not impose universal clinical eligibility requirements.

Every individual study must define its own eligibility criteria before recruitment.

## 5.2 First use case population

The first use case may include:

- Physicians.
- Nurses.

## 5.3 Clinician characteristics

The simulator should support collection of:

- Professional role.
- Exact years of practice in the current profession.
- Country of current clinical practice.
- Primary specialty for physicians.

Years of practice are stored as a continuous value.

For the first use case, prespecified subgroup variables are:

- Professional role.
- Years of practice.
- Primary specialty for physicians.

Country of current practice is descriptive and is not a prespecified subgroup variable.

Years of practice may additionally be grouped for descriptive reporting if the grouping is defined before analysis.

---

# 6. Experimental intervention

The core Phase 2 comparison is:

**AI access versus no AI access.**

A clinician must not review the same measured patient more than once within a study.

The no AI condition contains no AI panel, disabled AI placeholder, empty AI container, or message indicating where AI would otherwise appear.

The remainder of the clinical interface should behave consistently between conditions.

---

# 7. Randomisation framework

## 7.1 Within clinician balance

Each clinician should receive approximately 50 percent AI cases and 50 percent no AI cases.

Exact 50:50 allocation is preferred when mathematically possible.

## 7.2 Patient level balance

AI and no AI exposure should also be balanced across patients as closely as mathematically possible.

Independent randomisation of every clinician and patient pair without considering accumulated balance is therefore not sufficient.

## 7.3 Adaptive scheduling

When a new clinician enters the study, their schedule may be generated using assignments that have actually been activated so far.

The scheduler should aim to:

- Maintain clinician level AI versus no AI balance.
- Restore patient level balance where previous dropout created imbalance.
- Respect study specific block rules.
- Balance starting arm across clinicians.

Already activated assignments are immutable.

Previously generated schedules for other clinicians are not rewritten.

Adaptation applies only to schedules generated for future clinicians.

## 7.4 Randomisation seed

Every study has a master randomisation seed.

Clinician schedules must be reproducible from:

- `study_id`
- master randomisation seed
- clinician identity
- schedule generation context
- activated allocation state
- randomisation algorithm version

Because scheduling may be adaptive, the allocation state used during schedule generation must also be retained.

## 7.5 Patient order

Patient order is randomised separately for each clinician.

The realised order must be reproducible from the randomisation record.

## 7.6 Block structure

Block length and sequence structure are study specific.

The simulator should support consecutive runs of AI and no AI cases where required.

Block based designs may support exploratory assessment of residual behaviour after AI exposure.

Useful schedule variables include:

- overall case position
- block number
- arm
- position within block
- preceding block arm
- number of cases since the most recent AI exposure

## 7.7 Starting arm

For studies using blocks:

- starting arm is randomised
- starting arm is balanced across clinicians as closely as mathematically possible

---

# 8. Case activation and lifecycle

## 8.1 Allocation concealment

The clinician must not know the assigned arm before starting the measured case.

## 8.2 Start case boundary

The randomised clinician and patient assignment becomes active only after an explicit **Start case** action.

At case start:

- the scheduled patient is committed
- the intervention arm becomes immutable
- the activation timestamp is recorded
- the configuration version is recorded
- case timing begins

Opening the patient index or previewing navigation must not consume an assignment.

## 8.3 One sitting principle

Measured cases are intended to be completed in one sitting.

Each study may define a technical reconnection grace period.

A short interruption within this period may resume the same case.

An interruption exceeding the permitted period results in a started but incomplete case.

The original assignment remains part of the ITT record.

## 8.4 Voluntary pauses

Voluntary pausing is study specific.

Where enabled, the default permitted duration is the same as the study reconnection grace period unless otherwise configured.

Pause, resume, and timeout states must remain identifiable in the research record.

## 8.5 Incomplete activated cases

Once activated:

- the assignment remains permanent
- the case remains in the ITT record
- it is never returned to the allocation pool
- it is never erased because a replacement is issued

## 8.6 Replacement cases

A replacement may be scheduled where required by the study.

Replacement cases must come from the clinician's unused patient pool.

A clinician must never see the same patient twice.

Replacement selection should preserve the study's balance and sequence constraints as closely as possible.

A replacement matches the incomplete case's arm first; clinician balance, patient balance and sequence distance break ties.

Rationale: the replacement is drawn from the clinician's own schedule, so arm totals at stopping are nearly unchanged, while an opposite-arm replacement would break block runs (7.6).

Cost: intention to treat arm imbalance may grow by up to one case per incomplete case.

The replacement does not overwrite the original incomplete case.

## 8.7 Per clinician stopping

Each study may configure:

- target completed cases per clinician
- maximum activated cases per clinician

These determine when an individual clinician stops receiving additional cases.

## 8.8 Study stopping

The simulator must not automatically terminate the entire study.

Study termination remains an operator decision.

Optional planned sample targets may be stored and displayed for monitoring but must not trigger automatic study closure.

---

# 9. Training and practice

Each study must define participant training and familiarisation before measured data collection.

This may include:

- simulator instructions
- panel and navigation instructions
- explanation of AI output
- practice cases
- training completion criteria

When practice cases are used:

- they must be explicitly marked as practice
- they do not contribute to ITT
- they do not contribute to per protocol analyses
- they do not contribute to case targets
- they do not affect randomisation balance
- a practice patient must not later be used as a measured patient for the same clinician

The first use case will use **no practice cases**.

---

# 10. Timepoint navigation and feedback

## 10.1 Backward navigation

Backward navigation is study specific.

The simulator should support:

- permitting review of previous timepoints while freezing previous answers
- prohibiting backward navigation during measured cases

Where revisits are permitted, revisits must be distinguishable from the original timepoint exposure.

## 10.2 Outcome and performance feedback

Feedback is study specific and disabled by default.

For the first use case clinicians will not receive:

- ground truth during measured data collection
- correctness feedback
- running performance scores
- information about whether the AI was correct

---

# 11. Temporal validity

## 11.1 Clinician facing data

Variables that directly encode, reveal, or are derived from the reference outcome must not be visible at or before the relevant prediction timepoint.

Clinical information legitimately available by that timepoint remains visible.

This must be verified before measured data collection.

## 11.2 AI information

AI output displayed at timepoint `t` may only use information available at or before `t`.

It must not use:

- future measurements
- future notes
- reference outcome information
- features derived using future information

---

# 12. AI intervention provenance

Every study must preserve a traceable definition of the AI intervention.

Where applicable this includes:

- AI model or system version
- prediction artifact hash
- explanation artifact hash
- presentation template version
- intervention build or release identifier

The first use case uses precomputed AI predictions.

The exact prediction artifact shown to clinicians must therefore be frozen and identifiable.

---

# 13. Timing telemetry

Three distinct time measures are retained for every clinician, patient, and timepoint.

## 13.1 Wall clock time

`elapsed_seconds`

Total time from entering to leaving the timepoint.

## 13.2 Foreground time

`foreground_seconds`

Cumulative time while:

- the document is visible
- the browser window is focused

## 13.3 Active time

`active_seconds`

Foreground time excluding periods beyond a configured inactivity threshold.

The first use case uses a **60 second inactivity threshold**.

Meaningful activity includes:

- clicking
- touch interaction
- scrolling
- keyboard input
- answer modification
- panel opening or closing
- timepoint navigation

Passive mouse movement does not count as activity.

---

# 14. Panel viewing telemetry

## 14.1 General definition

Viewport based exposure is measured for all major information panels.

Qualifying panel viewing time accumulates only while:

- the expanded panel content meets the configured viewport intersection threshold
- the document is visible
- the browser window is focused

A collapsed panel accumulates no viewing time.

The panel header itself does not count as panel content exposure.

## 14.2 First use case thresholds

For the first use case:

- viewport intersection threshold is **5 percent**
- cumulative panel viewed threshold is **2 seconds per timepoint**

The same thresholds apply to all instrumented panels.

## 14.3 Cumulative exposure

Exposure is cumulative within the current timepoint.

Separate viewing episodes may be added together.

The cumulative counter resets when a new timepoint begins.

## 14.4 Viewing episodes

A viewing episode begins when all qualifying conditions become true.

It ends when any qualifying condition becomes false.

Examples that create a new episode include:

- scrolling away and returning
- switching tabs and returning
- losing and regaining browser focus
- collapsing and reopening the panel

## 14.5 Relationship to active time

Panel viewing does not require recent interaction.

A clinician may read a visible panel without clicking or scrolling and still accumulate valid panel viewing time.

## 14.6 Panel interactions

For collapsible panels, record:

- panel open
- panel close
- panel identifier
- timestamp

Opening a panel alone does not mean that it qualifies as viewed.

## 14.7 Raw and derived telemetry

Raw behavioural events remain the source of truth.

Derived panel measures may include:

- cumulative viewing duration
- viewed yes or no
- viewing episode count
- time to first view
- first viewing timestamp
- last viewing timestamp
- number of panel opens

Panel exposure times may overlap where multiple panels are visible simultaneously.

Panel durations therefore must not be summed and interpreted as total attention time.

---

# 15. Per protocol AI exposure

AI exposure is evaluated independently for every:

`clinician × patient × timepoint`

For the first use case:

**AI viewed = at least 2 cumulative seconds during which at least 5 percent of the expanded AI panel content intersects the viewport while the document is visible and the browser window is focused.**

The cumulative exposure counter resets for every new timepoint.

Case level summaries may additionally be derived.

Examples include:

- AI viewed at least once during the case
- number of AI viewed timepoints
- total AI panel viewing duration
- number of AI viewing episodes

The timepoint level measure remains authoritative for per protocol classification.

For AI assigned observations:

- the observation is per protocol compliant only when the configured AI viewing threshold is reached

For no AI observations:

- the observation is per protocol compliant only when AI remains unavailable and no AI information is leaked

Per protocol status is therefore observation specific.

A single patient case may contain both compliant and non compliant observations.

---

# 16. Intervention failure and leakage

If AI is assigned but cannot be delivered:

- the original AI assignment remains unchanged for ITT
- the failure is recorded
- the failure reason is retained
- the affected observation is non compliant for PP when AI was unavailable
- the case must not silently become a no AI case

If AI information is accidentally exposed during a no AI observation:

- the original no AI assignment remains unchanged for ITT
- the leakage is recorded
- the affected observation is non compliant for PP

Replacement rules may apply when technical failure prevents meaningful case completion.

---

# 17. Missing responses

The EHRSimulator must never manufacture or statistically impute a missing clinician response.

Where possible, missing responses should retain a structured reason such as:

- case abandoned
- technical failure
- reached timepoint but unanswered
- timepoint never reached

Statistical handling of missing outcomes belongs to the study specific analysis protocol.

---

# 18. First use case scientific design

## 18.1 Current planning estimate

The current planning estimate is approximately:

- 40 patients
- 15 clinicians

These numbers remain provisional.

They must not be hard coded into the simulator.

## 18.2 Primary question

At every eligible study timepoint:

**Will the patient experience neurological deterioration within the next 6 hours?**

Responses:

- Yes
- No

There is no Unknown response.

## 18.3 Prediction window

The reference target is:

**Neurological deterioration within the next 6 hours, or until observation ends, whichever occurs first.**

A complete future six hour observation period is therefore not required.

If deterioration occurs during the available observation period, the reference outcome is positive.

If no deterioration occurs before observation ends, the reference outcome is negative.

## 18.4 Ground truth

Ground truth uses an existing deterioration label in the source dataset.

Before data collection, the study documentation must identify:

- exact source field
- coding
- interpretation

## 18.5 Primary observations

Every eligible study timepoint contributes a primary observation.

A single clinician and patient case may therefore contribute multiple primary observations.

## 18.6 Primary analysis population

The primary analysis population is intention to treat.

Observations remain classified according to their randomised arm regardless of whether AI was viewed.

## 18.7 Primary estimand

The primary treatment effect is:

**The absolute difference in the probability of a correct deterioration prediction between AI assigned and no AI assigned observations.**

The main effect measure is the absolute percentage point difference in accuracy with a 95 percent confidence interval.

## 18.8 Current analysis plan

Statistical modelling is outside the EHRSimulator implementation.

The current first use case analysis plan uses a mixed effects logistic regression including:

Fixed effects:

- AI assignment
- study timepoint
- overall case position

Random intercepts:

- clinician
- patient
- clinician and patient combination

Clinician characteristics are not automatically included in the primary adjustment set.

## 18.9 Multiplicity

The first use case has one confirmatory primary endpoint.

Secondary outcomes, subgroup analyses, per protocol analyses, and behavioural analyses are secondary or exploratory.

---

# 19. First use case question set

## 19.1 Neurological deterioration

**Will the patient experience neurological deterioration within the next 6 hours?**

Response:

- Yes
- No

Role: primary endpoint.

## 19.2 Confidence

Confidence is recorded alongside the deterioration prediction.

Five point scale:

1. Not at all confident.
2. Slightly confident.
3. Moderately confident.
4. Confident.
5. Very confident.

Role: secondary or exploratory.

## 19.3 Primary cause of deterioration

Shown only when neurological deterioration is predicted as Yes.

The clinician selects one primary cause from a study specific predefined list.

Cause accuracy is calculated only where a valid reference cause exists.

Otherwise the response remains descriptive or exploratory.

## 19.4 Good neurological outcome at three months

**Will the patient have a good neurological outcome at three months, defined as mRS 0 to 2?**

Response:

- Yes
- No

## 19.5 Death by three months

**Will the patient be dead at three months?**

Response:

- Yes
- No

If good neurological outcome at three months is Yes, death at three months is automatically No.

## 19.6 Questions not included

The first use case does not include:

- hospital survival prediction
- contributing factor multi select
- free text reasoning

---

# 20. Clinician covariates and subgroup analyses

Clinician characteristics are descriptive and secondary.

They are not required covariates in the primary model.

For the first use case the prespecified subgroup variables are:

- physician versus nurse
- exact years of practice
- physician primary specialty

Years of practice should primarily be analysed continuously.

Prespecified categories may additionally be reported descriptively.

Country of current practice is not a prespecified subgroup variable.

---

# 21. Privacy and clinician identity

## 21.1 Clinician identifier

The current deterministic clinician identifier may remain for Phase 2.

It must be regarded as pseudonymous rather than anonymous.

A future change to random study specific clinician identifiers remains a privacy improvement rather than a Phase 2 prerequisite.

## 21.2 Clinician name storage

Clinician names should exist only where operationally required for identity lookup.

Behavioural event payloads must not duplicate the normalised clinician name.

New Phase 2 behavioural events should identify clinicians through `clinician_id`.

Historical records do not need to be rewritten.

## 21.3 Name mapping keyfile

The existing clinician ID to name mapping functionality remains available.

The operator may explicitly generate the keyfile when required.

Routine research exports remain pseudonymised.

The keyfile remains separate from routine research outputs.

## 21.4 Free text

Free text questions are disabled by default.

Where enabled:

- free text is considered potentially identifying
- its handling must be explicitly defined by the study
- it should not automatically appear in routine analysis exports
- it must never appear in behavioural figures or telemetry summaries

---

# 22. Retention and backups

Retention and deletion rules are study specific.

The simulator must not automatically delete research data according to one global retention period.

Each study should define retention rules for:

- SQLite database
- backups
- exports
- clinician identity data
- name mapping keyfile
- free text where applicable

Every backup must remain attributable to:

- `study_id`
- database or schema version
- creation timestamp

Backups from different studies must never be silently combined.

---

# 23. Research exports

Phase 2 should use multiple linked research outputs rather than one oversized export.

At minimum, the study should be able to produce separate linked outputs for:

- answers and timepoint outcomes
- panel exposure summaries
- raw behavioural telemetry
- randomisation schedule and activation history
- configuration version history

These outputs should remain joinable through stable identifiers such as:

- `study_id`
- `clinician_id`
- `patient_id`
- timepoint
- case or session identifier
- `config_version`
- `config_hash`

Mixed configuration versions are valid within one study.

Every row must retain the version under which it was collected.

Exports should fail only when configuration identity is missing, unknown, or internally inconsistent.

A configuration summary should allow investigators to determine how many cases and observations were collected under each version.

---

# 24. Randomisation audit record

The study must preserve enough information to reconstruct both planned and realised allocation.

The audit record should include:

- planned patient order
- planned arm
- starting arm
- block number where applicable
- position within block
- overall case position
- schedule generation timestamp
- master seed or derived randomisation information
- randomisation algorithm version
- whether the case was activated
- activation timestamp
- completion or abandonment status
- replacement relationship where applicable
- configuration version at schedule generation
- configuration version at case activation

---

# 25. Intentionally study specific parameters

The following remain configurable and are not globally fixed by Phase 2:

- clinician eligibility criteria
- final number of clinicians
- final number of patients
- patient selection procedure
- patient pool
- target completed cases per clinician
- maximum activated cases per clinician
- exact timepoint list
- block length
- block sequence structure
- reconnection grace period
- voluntary pause policy
- backward navigation policy
- exact deterioration reference field and coding
- cause of deterioration category list
- AI artifact
- training procedure
- practice case design
- retention period
- backup destination
- statistical power calculation
- final statistical analysis protocol

---

# 26. First use case parameters still requiring completion

Before measured data collection for the first use case begins, the following must still be fixed:

1. Final clinician sample target.
2. Final patient sample target.
3. Patient selection procedure.
4. Exact patient pool.
5. Exact study timepoints.
6. Final block structure.
7. Reconnection grace period.
8. Voluntary pause policy.
9. Backward navigation policy.
10. Exact deterioration reference field and coding.
11. Final cause of deterioration categories.
12. Target completed cases per clinician.
13. Maximum activated cases per clinician.
14. Frozen AI artifact identifiers and hashes.

These remaining study specific parameters do not prevent implementation of the general Phase 2 framework.

---

# 27. Gate conclusion

The Phase 2 framework decisions are sufficiently defined to proceed with software implementation.

The gate establishes the scientific and operational contract that implementation must satisfy while preserving study level configurability.

No unresolved scientific design decision should be silently chosen by the programmer.

Any study specific value not fixed in this document must be supplied through study configuration or accompanying study documentation before measured data collection begins.

