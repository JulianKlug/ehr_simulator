# Session 11c — Adaptive randomisation scheduler and planned provenance

## Goal

Add a deterministic, auditable Phase 2 scheduler that generates one planned patient and arm sequence for each clinician.

The scheduler must:

* balance AI versus no AI within each clinician
* improve patient level arm balance using activated allocation state
* randomise patient order within those constraints
* support study configured blocks
* balance starting arm across clinicians
* preserve all inputs needed to reproduce the schedule
* never rewrite an existing clinician schedule

S11a and S11b are assumed complete.

S11c creates **planned schedules only**. Explicit Start case and conversion of a planned item into an activated `arm_assignment` belong to S11d.

## Current branch baseline

On `session11b`:

* `StudyConfig` is schema version `"2"`
* configuration history and immutable snapshots exist
* `arm_assignments` carries `config_version` and `config_hash`
* `arm_assignments.assign_or_lookup()` still uses `phase1_stub`
* randomized rows are not yet created
* `arm_assignments.seed` remains nullable
* current routes still lock an assignment during session bootstrap

Do not remove or partially replace this temporary flow in S11c.

## Core invariants

1. One clinician has at most one initial randomisation schedule.
2. A saved schedule is immutable.
3. Existing schedules are never recalculated when later clinicians enrol or withdraw.
4. Adaptation uses **activated allocations only**, never merely planned schedules.
5. Patient level balancing may influence patient order.
6. Every patient appears at most once in one clinician schedule.
7. The full schedule is reproducible from persisted inputs.
8. No Python process randomness, `hash()`, UUID randomness or global RNG state may affect allocation.
9. S11c does not activate an allocation.
10. S11c does not create `phase2_randomized` rows in `arm_assignments`.

## Study configuration

Keep study schema version `"2"`.

Add optional:

```
randomisation:
  master_seed: 123456
  block_length: 2
  block_sequence: [start, other]
```

Existing v2 historical snapshots without `randomisation` must remain valid.

Schedule generation requires `randomisation` to be present.

### `RandomisationConfig`

Add a strict model with:

```
master_seed: int
block_length: int
block_sequence: list[Literal["start", "other"]]
```

Validation:

* `master_seed` must be between `0` and `2^63 - 1`
* `block_length >= 1`
* `block_sequence` must be nonempty
* it must contain both `start` and `other`
* one complete sequence cycle must contain equal numbers of `start` and `other`

`start` means the clinician's selected starting arm.

`other` means the opposite arm.

Each sequence entry represents one block containing `block_length` consecutive cases.

The sequence repeats until every configured patient has a schedule position.

Example:

```
block_length: 2
block_sequence: [start, other]
starting_arm: ai
```

produces the planned arm pattern:

```
ai, ai, no_ai, no_ai, ai, ai, ...
```

The exact block settings remain study specific. Do not introduce first use defaults.

Because `randomisation` is part of `StudyConfig`, it naturally participates in the existing `config_hash`.

## Algorithm version

Add a code constant:

```
RANDOMISATION_ALGORITHM_VERSION = "adaptive_block_v1"
```

Every schedule stores this value.

Changing allocation behaviour in a later implementation requires a new algorithm version.

Do not silently change behaviour while retaining `"adaptive_block_v1"`.

## Activated allocation state

S11c introduces the scheduler input model:

```
ActivatedAllocationState
```

containing, for every patient in the active configuration:

```
patient_id
ai_count
no_ai_count
```

Counts represent **activated measured cases only**.

Planned schedules must not contribute.

S11c does not yet derive this state from the current database because explicit activation does not exist until S11d.

The scheduler API accepts the state explicitly. S11d will build it from realised assignments.

Persist the exact canonical allocation state used for every generated schedule.

## Deterministic seed derivation

Do not use Python `hash()` or ambient `random`.

Derive a schedule key using HMAC SHA256.

Master key:

```
str(master_seed).encode("utf-8")
```

Canonical schedule context contains:

```
study_id
clinician_id
config_version
config_hash
algorithm_version
activated_allocation_state
starting_arm_counts
```

Serialize using sorted JSON keys and compact separators.

Then:

```
derived_seed_hex =
HMAC-SHA256(master_seed, canonical_context).hexdigest()
```

Persist `derived_seed_hex`.

All deterministic tie breaking uses HMAC SHA256 derived from this key.

This makes schedules reproducible independently of Python RNG implementation details.

## Starting arm selection

Starting arm balance uses previously **generated schedules**, not activated cases.

Count existing schedules with:

```
starting_arm = ai
starting_arm = no_ai
```

Then:

* fewer AI starts → choose `ai`
* fewer no AI starts → choose `no_ai`
* equal counts → choose deterministically using the schedule key

Persist the starting arm counts used during generation.

Never alter an older schedule to restore starting arm balance.

## Planned arm sequence

Expand `block_sequence` over the configured patient count.

Map:

```
start → starting_arm
other → opposite(starting_arm)
```

Each block contains exactly `block_length` positions.

Stop expansion once the number of positions equals the number of configured patients.

For each position derive:

* `case_position`, one based
* `planned_arm`
* `block_number`, one based
* `position_in_block`, one based
* `preceding_block_arm`, nullable for the first block

The arm sequence is fixed before patient selection.

## Adaptive patient ordering

Patient selection uses the activated allocation state captured when the schedule is generated.

For each schedule position, evaluate every remaining patient.

For a candidate patient and the position's planned arm, calculate the projected absolute imbalance:

```
abs(projected_ai_count - projected_no_ai_count)
```

where the planned arm contributes one additional assignment.

Select the candidate with the **lowest projected imbalance**.

When multiple patients have the same score, choose using deterministic HMAC ranking based on:

```
derived_seed
case_position
patient_id
```

This provides constrained randomisation while preferentially correcting patient level imbalance.

Remove the selected patient from the remaining pool and continue.

Never use a planned assignment from another clinician when calculating the patient balance score.

## Planned exposure metadata

For every schedule position also store:

`planned_cases_since_ai`

Definition:

* before any preceding AI position → `NULL`
* current planned arm is `ai` → `0`
* otherwise → number of consecutive preceding no AI positions since the most recent planned AI position

This is planned schedule metadata only. Realised exposure variables belong to later sessions.

## Assignment seed

Derive one 63 bit integer for each schedule item using HMAC over:

```
derived_seed
case_position
patient_id
planned_arm
```

Store it as:

`assignment_seed`

S11d must copy this value into `arm_assignments.seed` when that planned allocation is activated.

This closes the existing unresolved `phase2_randomized` seed contract without creating the assignment in S11c.

## Database migration

Add migration 6.

Create:

```
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
```

`schedule_id` must be deterministic.

Define it as the SHA256 hex digest of the same canonical generation context used for seed derivation.

Do not add activation, completion or replacement columns in S11c.

Those belong to realised assignment lifecycle work.

## Scheduler module

Add:

`src/ehr_simulator/randomisation.py`

Required public API:

```
generate_schedule(
    *,
    study,
    config_version,
    config_hash,
    clinician_id,
    allocation_state,
    starting_arm_counts,
) -> GeneratedSchedule
```

This function must be pure.

No database access.

Same inputs must produce byte for byte equivalent schedule output.

## Schedule DAO

Add:

`src/ehr_simulator/db/randomisation.py`

Required operations:

```
fetch_for_clinician(
    conn,
    study_id,
    clinician_id
) -> StoredSchedule | None

insert_schedule(
    conn,
    schedule
) -> StoredSchedule

list_schedules(
    conn,
    study_id
) -> tuple[StoredSchedule, ...]
```

`insert_schedule()` writes the schedule and all items atomically.

If a schedule already exists for the clinician:

* identical schedule → return it unchanged
* different schedule → raise a randomisation integrity error

Never overwrite it.

## Schedule service

Add a small service operation equivalent to:

```
create_or_fetch_schedule(
    conn,
    *,
    study,
    config_version,
    config_hash,
    clinician_id,
    allocation_state,
) -> StoredSchedule
```

Behaviour:

1. return an existing clinician schedule unchanged if present
2. require `study.randomisation`
3. verify the configuration version/hash through S11b history
4. verify that version/hash is currently active when creating a new schedule
5. calculate starting arm counts from stored schedules
6. generate the schedule
7. persist it atomically
8. return it

No route should call this service in S11c.

S11d will decide when clinician schedule creation enters the user workflow.

## Configuration changes

A schedule retains:

* generation `config_version`
* generation `config_hash`
* randomisation settings used at generation

Activating a later configuration must not rewrite an existing schedule.

S11c does not decide how a schedule generated under an older configuration interacts with a later case activation. The audit data required to make that distinction are preserved.

## Existing `arm_assignments`

Do not change current activation behaviour in this session.

Specifically:

* keep `phase1_stub`
* keep current `assign_or_lookup()` call sites working
* do not produce `phase2_randomized`
* do not consume schedule items
* do not mark schedule items activated
* do not expose planned arm through the UI

S11d replaces the temporary assignment boundary with explicit Start case.

## Files expected to change

* `src/ehr_simulator/config/study.py`
* `src/ehr_simulator/randomisation.py`
* `src/ehr_simulator/db/randomisation.py`
* `src/ehr_simulator/db/migrations.py`
* `src/ehr_simulator/db/__init__.py`
* database schema fixture
* study configuration fixtures
* tests for configuration, scheduler and persistence
* relevant configuration documentation

Do not modify patient routes or templates unless required only for regression compatibility.

## Required tests

### Configuration

1. Existing v2 config without `randomisation` still parses.
2. Valid randomisation config parses and changes `config_hash`.
3. Invalid seed, block length or sequence is rejected.
4. Schedule generation refuses a config without randomisation settings.

### Determinism

5. Identical inputs produce identical schedule and schedule ID.
6. Different clinician IDs produce independently randomised schedules.
7. Different master seeds change the schedule.
8. Different allocation state participates in derivation.
9. Python global RNG state has no effect.

### Balance and scheduling

10. Even feasible schedules preserve configured clinician arm balance.
11. Patient selection prefers the candidate that reduces patient level imbalance.
12. Tie candidates are resolved reproducibly.
13. Every configured patient appears exactly once.
14. Block pattern, block number and position metadata are correct.
15. Starting arm chooses the less represented starting arm.
16. Equal starting counts use deterministic tie breaking.
17. Planned cases since AI metadata is correct.

### Persistence and provenance

18. Schedule and items persist atomically.
19. Stored record contains seed, algorithm, configuration and allocation state provenance.
20. Recreating an existing identical clinician schedule is idempotent.
21. Attempting to replace an existing clinician schedule is refused.
22. Generating a later clinician schedule never changes previous schedules.
23. `assignment_seed` fits SQLite signed integer range and is reproducible.
24. Study identity/configuration provenance validation remains enforced.

### Regression

25. Existing `arm_assignments` still use `phase1_stub`.
26. S11c creates no `phase2_randomized` assignment.
27. Existing S11a/S11b, answer, gating, timing and CI tests remain green.

## Explicit non goals

S11c does not implement:

* explicit Start case
* GET randomisation removal
* allocation concealment UI
* activation timestamp
* realised randomised `arm_assignments`
* case lifecycle
* replacements
* clinician stopping rules
* AI panel visibility
* randomisation research export files

It does not calculate adaptive state from planned schedules.

It does not rewrite previously generated schedules.

## Acceptance

S11c is complete when:

1. studies may define a master seed and block structure
2. the scheduler produces deterministic constrained schedules
3. clinician arm balance follows the configured block design
4. patient ordering preferentially corrects activated patient level imbalance
5. starting arm is balanced across generated clinician schedules
6. patient order is deterministic and separately generated per clinician
7. every schedule stores sufficient inputs to reproduce it
8. existing schedules are immutable
9. per item assignment seeds are available for later activation
10. no schedule is consumed and no Phase 2 randomised assignment is activated
11. the full test suite and CI pass

S11d is responsible for explicit Start case, allocation concealment, atomic schedule consumption, and creation of the realised immutable `phase2_randomized` assignment.

