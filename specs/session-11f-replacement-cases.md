# Session 11f — Replacement case scheduling

## Goal

Allow a study to issue a replacement after an activated case becomes incomplete, without deleting or rewriting the original case.

Replacement selection must use a patient never previously activated for that clinician and preserve randomisation balance and sequence as closely as possible.

S11a through S11e are assumed complete.

## Core invariants

1. The original incomplete case remains permanent.
2. A replacement is a new measured case with its own assignment and lifecycle.
3. A clinician never receives the same patient twice.
4. Only unactivated patients may become replacements.
5. Existing schedule rows are never rewritten.
6. Replacement planning is deterministic and auditable.
7. Replacement activation still occurs only through explicit Start case.
8. The maximum activated case limit always applies.
9. Replacement does not change the ITT arm of the original case.
10. Replacement linkage is preserved in both directions.

## Study configuration

Extend `case_lifecycle` with:

```
replacement_cases_enabled: true
```

Default:

```
false
```

This setting participates in `config_hash` **only when true**: serialization omits it when false, so every S11e snapshot carrying `case_lifecycle` re-renders byte-for-byte and its hash does not move (the S11e boot gate re-hashes stored snapshots and would otherwise refuse).

No replacement is planned when disabled. The flag is read from the **incomplete case's pinned** configuration.

No study specific replacement count or patient count is hard coded.

## Replacement trigger

A replacement may be planned only when:

- an activated case reaches lifecycle state `incomplete`
- replacements are enabled under that case's configuration
- at least one eligible unused scheduled patient remains

Clinician limits are **not** checked at planning. They stay where S11e enforces them: inside Start case's activation lock, against the active configuration. A plan that cannot be activated at the limit is harmless; the index shows `LIMIT_REACHED` first.

Completion of a case never creates a replacement.

A replacement may itself later become incomplete and may receive its own replacement under the same rules.

## Eligible patient pool

A candidate replacement must:

- belong to the clinician's existing S11c schedule
- have no realised `arm_assignment` for that clinician
- not be the original patient
- remain valid in the current active study patient pool at replacement planning time (read from the database's active configuration snapshot, so the CLI path works without a server)
- not already be reserved by another pending (unactivated) replacement plan

Never create a second schedule containing a duplicate patient.

Never reintroduce an already seen patient after abandonment or timeout.

## Replacement selection

Do not modify the original planned schedule.

Select one still unactivated schedule item and realise it earlier than originally planned.

For each eligible candidate compute this deterministic score tuple after hypothetical activation:

```
(
    arm_mismatch,
    clinician_arm_imbalance,
    patient_arm_imbalance,
    sequence_distance,
    deterministic_tie_rank
)
```

Choose the lexicographically smallest tuple.

### Arm mismatch (study decision, 2026-09-25)

`0` when `candidate.planned_arm` equals the incomplete case's arm, else `1`.

The lost observation is replaced in kind first. ITT counts are unchanged: the incomplete case still counts as activated in the balance terms below. Without this term, an incomplete AI case (AI already leading by one) would make the rule prefer a `no_ai` replacement.

Consequence: `clinician_arm_imbalance` depends only on the candidate's planned arm, like `arm_mismatch`, so it never separates two candidates once `arm_mismatch` has. It is kept in the tuple as documentation of intent; `patient_arm_imbalance` is the first term that can break a same-arm tie.

### Clinician arm imbalance

Using all activated measured assignments for this clinician:

```
abs(projected_ai_count - projected_no_ai_count)
```

The candidate's planned arm contributes one projected activation.

### Patient arm imbalance

Using activated assignments across the study for that candidate patient:

```
abs(projected_ai_count - projected_no_ai_count)
```

Again include the candidate's planned arm.

### Sequence distance

Let `next_nominal_position` be the lowest unactivated schedule position.

```
abs(candidate.case_position - next_nominal_position)
```

This favours staying close to the original block sequence after balance requirements.

### Deterministic tie rank

Use HMAC SHA256 keyed by the clinician schedule's `derived_seed_hex` (the S11c schedule key), with its own domain-separation purpose `replacement_rank`, over:

```
original_incomplete_patient_id
candidate.case_position
candidate.patient_id
candidate.planned_arm
```

Do not use process randomness.

This selection rule is the implementation definition of preserving activated clinician balance first, patient balance second and planned sequence third.

## Database migration

Add migration 9.

Create:

```
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
```

Timestamps come from the S11e injected clock. Triggers refuse any `DELETE` and any `UPDATE` other than setting a NULL `activated_at` once.

`replacement_id` must be deterministic from:

```
study_id
clinician_id
original_patient_id
replacement_schedule_id
replacement_case_position
```

using SHA256 canonical serialization.

The original assignment is never altered.

## Replacement service

Add a service operation equivalent to:

```
plan_replacement(
    conn,
    app_state,
    *,
    clinician_id,
    original_patient_id,
) -> ReplacementPlan | None
```

Behaviour:

1. require original case state `incomplete`
2. resolve the original case configuration (its pinned snapshot)
3. require `replacement_cases_enabled` there
4. return the existing plan if one already exists
5. load eligible unactivated schedule items (active pool, unreserved)
6. build activated allocation counts
7. score candidates using the locked rule
8. persist exactly one immutable replacement plan + a `case.replacement_planned` event (payload: `replacement_id`, `replacement_patient_id`, `replacement_case_position`; never the arm)
9. return it

All under `BEGIN IMMEDIATE`, one commit.

No eligible candidate returns `None`.

Do not silently add patients outside the configured schedule.

## Lifecycle integration

When a case becomes incomplete and replacements are enabled, call `plan_replacement()` after the incomplete transition commits, in its own transaction. Call sites: the lazy timeout (`web/case_contact.py`) and `abandon-case`.

Start case Phase A also plans for every incomplete case of the clinician that has no plan yet (idempotent), so a crash between the two commits heals on the next Start.

Failure to plan because no eligible patient remains does not undo the original incomplete transition.

The index marks each incomplete case as one of:

- `incomplete · replacement pending` (unactivated plan)
- `incomplete · replaced` (activated plan)
- `incomplete · no replacement` (disabled, or no eligible candidate)

Do not hide or overwrite the original case.

## Start case integration

S11d `POST /case/start` changes priority:

1. resume the open case (S11e: `active`, or `paused` → its interstitial) if present
2. if an unactivated replacement plan exists, activate the oldest one
3. otherwise activate the next normal schedule item, skipping positions reserved by pending plans

The S11e limit check runs before either activation.

Replacement activation uses the normal S11d atomic activation path.

It copies the replacement schedule item's:

- patient
- planned arm
- assignment seed
- schedule ID
- case position

The new `arm_assignment` receives the active configuration version/hash at activation time.

In the same transaction set:

```
case_replacements.activated_at
```

The replacement then receives normal S11e lifecycle state `active`. Its `case.activated` payload adds `replacement_id`.

## Configuration compatibility

Before activating a replacement, apply the same S11d checks used for normal scheduled items.

If the active randomisation rules or patient pool make the chosen replacement invalid, refuse activation rather than silently substitute another item.

A new replacement plan may only be created from the currently valid candidate pool.

Existing activated replacement cases remain pinned to their activation configuration.

## Randomisation state

Replacement cases count as activated assignments for all later adaptive balancing.

The original incomplete case also remains counted as activated.

Neither is removed from patient or clinician balance calculations.

Planned but unactivated replacements do not affect balance.

## Audit behaviour

Preserve:

- original clinician/patient
- replacement clinician/patient
- original case lifecycle outcome
- replacement schedule ID and position
- planned replacement arm
- generation timestamp
- replacement activation timestamp

S11n will export this information. S11f only persists it.

## Files expected to change

- `src/ehr_simulator/config/study.py`
- `src/ehr_simulator/db/migrations.py`
- new replacement DAO/service module
- S11c randomisation helpers where reusable
- S11d Start case service
- S11e incomplete transition integration
- `src/ehr_simulator/web/case_contact.py`, `cli_support.py` (planning call sites)
- `src/ehr_simulator/db/events.py` (`case.replacement_planned`)
- index template
- schema fixture
- relevant tests and documentation

## Required tests

### Eligibility and linkage

1. Incomplete case creates one replacement plan when enabled.
2. No replacement is planned when disabled.
3. Completed case never creates a replacement.
4. Replacement patient has never been activated for the clinician.
5. Original case remains unchanged.
6. Replacement link records both original and replacement patients.
7. Replanning the same original is idempotent.
8. A replacement may itself receive a later replacement if it becomes incomplete.

### Selection

9. Candidate with the incomplete case's arm is preferred; then the one minimizing clinician arm imbalance.
10. Patient level imbalance resolves the next comparison.
11. Sequence distance resolves the next comparison.
12. Final ties are deterministic.
13. Same inputs reproduce the same replacement.
14. Planned schedules themselves are never modified.
15. No eligible candidate returns no plan without corrupting state.

### Activation

16. Pending replacement is activated before the next ordinary schedule item.
17. Replacement activation still requires explicit Start case.
18. Replacement receives the schedule item's planned arm and seed.
19. Replacement activation is atomic and idempotent.
20. Replacement counts toward maximum activated cases.
21. Reached maximum activated count prevents replacement activation.
22. Invalidated patient/config causes activation refusal.

### Balance and regression

23. Original incomplete case remains in activated balance counts.
24. Activated replacement also enters later balance counts.
25. Planned replacement does not affect balance before activation.
26. A clinician never receives a duplicate patient.
26a. Enabling the flag changes `config_hash`; an S11e snapshot without it hashes unchanged.
26b. A pending plan reserves its position: the next ordinary Start never takes it.
26c. A crash between incomplete and planning heals on the next Start case.
27. All S11a through S11e tests remain green.
28. Full CI remains green.

## Explicit non goals

S11f does not implement:

- new patients outside the configured schedule
- rewriting or regenerating the original schedule
- deleting incomplete cases
- automatic whole study stopping
- AI intervention delivery
- telemetry
- PP classification
- final randomisation export files

It does not treat a replacement as if the original case never existed.

## Acceptance

S11f is complete when an incomplete case can deterministically produce one auditable replacement plan from the clinician's unused scheduled patients, the original assignment remains permanent, the replacement is activated only through Start case, duplicate patients are impossible, activated balance includes both original and replacement, clinician stopping limits still apply, and the complete test suite passes.
