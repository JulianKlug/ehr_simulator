# Session 11d — Explicit case start and allocation activation

## Goal

Move Phase 2 randomisation from implicit patient access to an explicit **Start case** action.

A planned schedule item becomes a realised assignment only when Start case succeeds.

S11a, S11b and S11c are assumed complete.

## Core invariants

1. GET requests never activate randomisation.
2. Opening the index never consumes a schedule item.
3. The arm is concealed before Start case.
4. Start case activates exactly one planned item.
5. Activation is atomic and idempotent.
6. The activated patient, arm, seed and case position never change.
7. A clinician never activates the same patient twice.
8. A repeated Start request resumes the already open case instead of consuming another item.
9. Case configuration is the active configuration at activation time.
10. Planned schedule provenance remains separate from activation provenance.
11. A clinician has at most one open Phase 2 case.

## Current baseline

After S11c:

- each clinician may have one immutable randomisation schedule
- schedule items contain planned patient, arm, position and assignment seed
- `arm_assignments` still uses `phase1_stub`
- current study GET/POST paths may create an assignment through session bootstrap
- explicit activation does not yet exist
- `create_or_fetch_schedule` owns its own `BEGIN IMMEDIATE` transaction and takes the activated allocation state as an explicit argument

S11d replaces that temporary activation boundary.

## Definitions

### Phase 2 study mode

The server is in **Phase 2 study mode** when it runs in study mode (`--config` + `--questions`) and the **active** configuration (`app.state`) has a `randomisation` block (`study.randomisation is not None`).

Study mode without `randomisation` keeps the Phase 1 behaviour unchanged: GET bootstrap through `assign_or_lookup()` with `phase1_stub`, no Start case action. Non-study mode is unchanged.

In Phase 2 study mode:

- no new `phase1_stub` row is ever created
- an existing assignment of any `arm_source` (including a legacy `phase1_stub` row created under an earlier configuration) still resumes under its pinned historical configuration (S11b)

### Open case

A clinician's **open case** is a `phase2_randomized` assignment whose `progress.completed_at` is NULL (an absent progress row counts as not completed).

The open case is derived from assignment + progress, never from `sessions.ended_at`: sessions close only on the final advance, so a session-based rule would add nothing and could drift from progress.

### Case position

`case_position` is the **nominal schedule position** of the activated item (`randomisation_schedule_items.case_position`).

It is not the ordinal of activated cases. Once S11f replacements exist the two may diverge; the realised ordinal is derivable by ordering a clinician's assignments by `activated_at`. S11f records replacement positions separately (`replacement_case_position`).

## Database migration

Add migration 7.

Extend `arm_assignments` with (via `Migration.add_columns`, retry-safe):

```
schedule_id    TEXT
case_position  INTEGER
activated_at   TIMESTAMP
```

Add:

```
CREATE UNIQUE INDEX ux_arm_schedule_position
ON arm_assignments(schedule_id, case_position)
WHERE schedule_id IS NOT NULL;
```

`activated_at` is kept distinct from the existing `assigned_at`: `assigned_at` is set (by default) on every row including `phase1_stub`, whereas a non-NULL `activated_at` marks an explicit Phase 2 activation and is the column S11e/S11f reference. The Phase 2 insert writes both from one timestamp value, so they are equal.

### Schema-level enforcement

SQLite `ALTER TABLE` cannot add a CHECK constraint, so migration 7 adds triggers:

```
CREATE TRIGGER trg_arm_phase2_complete
BEFORE INSERT ON arm_assignments
WHEN NEW.arm_source = 'phase2_randomized'
 AND (NEW.schedule_id IS NULL OR NEW.case_position IS NULL
      OR NEW.activated_at IS NULL OR NEW.seed IS NULL
      OR NEW.config_version IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignment requires activation provenance');
END;

CREATE TRIGGER trg_arm_phase2_immutable
BEFORE UPDATE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;

CREATE TRIGGER trg_arm_phase2_no_delete
BEFORE DELETE ON arm_assignments
WHEN OLD.arm_source = 'phase2_randomized'
BEGIN
    SELECT RAISE(ABORT, 'phase2_randomized assignments are immutable');
END;
```

Existing legacy or `phase1_stub` rows may remain NULL in the new columns.

Update the schema-snapshot fixture.

Do not add lifecycle, pause, completion or replacement fields in S11d.

## Activated allocation state

S11c delegated this to S11d.

Add a read in `db/arm_assignments.py` equivalent to:

```
activated_arm_counts(conn) -> dict[str, tuple[int, int]]   # {patient_id: (ai, no_ai)}
```

Rules:

- counts only rows with `arm_source = 'phase2_randomized'`
- counts across all clinicians and all configuration versions (the database is bound to one study by S11a, so this is the study scope)
- `phase1_stub` and legacy rows never count
- S11d counts every activated assignment; S11e/S11f may later refine which lifecycle states count

The service builds `ActivatedAllocationState` covering exactly the active `patient_ids` (as `require_covers` demands): active patients without rows are `(0, 0)`, rows for patients outside the active pool are excluded.

### Read inside the schedule lock

The state must be read under the same `BEGIN IMMEDIATE` as schedule generation, otherwise another clinician's activation between read and lock would make the stored `allocation_state_json` stale.

Change `create_or_fetch_schedule` to take an allocation-state loader instead of a precomputed state:

```
create_or_fetch_schedule(
    conn, *, study, config_version, config_hash, clinician_id,
    load_allocation_state: Callable[[sqlite3.Connection], ActivatedAllocationState],
) -> StoredSchedule
```

The loader runs after `BEGIN IMMEDIATE` and only when a schedule must be generated. `generate_schedule` stays pure and unchanged. Update S11c tests to pass a loader (e.g. `lambda _conn: ActivatedAllocationState.empty(ids)`).

## Randomised assignment DAO

Replace the randomized creation path with an explicit operation equivalent to:

```
activate_planned_assignment(
    conn,
    *,
    clinician_id,
    schedule_item,
    config_version,
    config_hash,
    commit=True,
) -> ArmAssignment
```

It inserts:

```
arm            = schedule_item.planned_arm
arm_source     = "phase2_randomized"
seed           = schedule_item.assignment_seed
config_version = active config_version
config_hash    = active config_hash
schedule_id    = schedule_item.schedule_id
case_position  = schedule_item.case_position
activated_at   = assigned_at = one CURRENT_TIMESTAMP value
```

Existing rows are never rewritten.

The operation must refuse:

- another assignment for the same clinician/patient
- a schedule item belonging to another clinician
- an already activated schedule position with conflicting data
- unknown or inconsistent configuration provenance

An exact retry returns the existing assignment.

Keep legacy `phase1_stub` support only for non Phase 2 study mode.

## Start case service

Add a service module (e.g. `web/case_start.py`) with an operation equivalent to:

```
start_next_case(
    conn,
    app_state,
    *,
    clinician_id,
) -> StartedCase
```

`StartedCase` carries the patient ID, the resume `t_index` (the case's frontier) and whether the call `activated` or `resumed`. It never carries the arm to the route.

Start case runs in two phases.

### Phase A — schedule (its own commit)

1. if the clinician has an open case, return it (`resumed`); no write
2. verify the database active configuration matches `app.state` (else `StaleConfigurationError`)
3. fetch or create the clinician schedule via `create_or_fetch_schedule` (commits on its own when it generates)

Creating a schedule is not consumption: a schedule is planned-only and never reveals or fixes a realised arm. A failure after phase A may therefore leave a schedule but never an assignment. Schedule creation happens only on this POST, never on GET.

### Phase B — activation (one transaction)

4. `BEGIN IMMEDIATE`
5. re-check for an open case (a concurrent Start may have won); if found, roll back and return it (`resumed`)
6. re-verify the active configuration still matches `app.state`
7. verify schedule compatibility with the active configuration (see below)
8. select the first unactivated schedule item in `case_position` order; none left → `ScheduleExhaustedError`
9. verify that patient has never been activated for this clinician
10. `activate_planned_assignment(..., commit=False)`
11. `sessions.start_or_resume(..., commit=False)` with the assignment's arm and provenance
12. `events.append(case.activated, commit=False)`
13. `events.append(session.start, commit=False)`
14. one `conn.commit()`; `conn.rollback()` on any failure; `write_counter` bumped once, post-commit
15. return the activated case (`activated`, `t_index` 0)

Progress is not written: the `progress` row is still created lazily by the first `unlock`, as today.

DAO helpers used in phase B must support deferred commit (`commit=False`), matching the S10 advance transaction pattern.

## Configuration at activation

The assignment receives the **currently active**:

- `config_version`
- `config_hash`

The schedule retains its original generation version/hash separately.

A schedule generated under an older configuration may be used only if the active configuration has the same:

- randomisation settings (`master_seed`, `block_length`, `block_sequence`)
- randomisation algorithm version
- study patient pool required by the scheduled item (the selected item's patient is in the active `patient_ids`)

A change to unrelated study content does not invalidate the schedule. Patients added to the active pool after generation are not in the schedule and are not offered to that clinician.

If randomisation settings or patient eligibility for the selected item changed, Start case must refuse (`ScheduleIncompatibleError`) rather than silently regenerate, skip or reinterpret the schedule.

## Route contract

Add:

```
POST /case/start
```

It requires an authenticated clinician (unknown cookie → `/login`, as other routes).

| Outcome | Browser | HTMX |
|---|---|---|
| activated or resumed | 303 to the case's frontier timepoint | 200 + `HX-Redirect` to it |
| not Phase 2 study mode | 409 | 409 |
| stale server (`StaleConfigurationError`) | 409, restart message | 409 |
| schedule exhausted | 409, "no further cases" | 409 |
| schedule incompatible with active config | 409, operator message | 409 |
| provenance/integrity error | 500 via the existing `ConfigurationProvenanceError` handler | 500 |

Error bodies use the existing flash fragment and carry no arm, seed, patient order or schedule metadata. Every failure leaves no assignment, session or event behind (a schedule may remain, see phase A).

## Study index and patient jumper

In Phase 2 study mode:

- the index lists **only the clinician's activated cases**, with the existing progress markers and resume links
- unactivated scheduled patients are not listed and not linked, in the index or the patient jumper
- when no case is open and unactivated items remain, the index shows a **Start case** button (a form POSTing to `/case/start`)
- when a case is open, the index shows **Resume case** for it instead of Start case
- when the schedule is exhausted (every item activated, none open), the index shows "All cases completed" and no Start action
- before the clinician's schedule exists, the index shows Start case; opening the index never creates the schedule

The index and jumper must not render `ai`, `no_ai`, assignment seed, block metadata, remaining-arm counts or any other arm-revealing value.

Outside Phase 2 study mode the index and jumper are unchanged.

## Remove assignment from GET

In Phase 2 study mode:

```
GET /patient/{patient_id}/timepoint/{t_index}
```

must never create:

- `arm_assignments`
- schedules
- sessions
- progress
- randomisation consumption

For an activated case, GET may resume/render the existing case according to the current session rules.

For an unactivated patient, direct GET redirects to the study index (303, or 200 + `HX-Redirect`) before `slice_to_timepoint` runs.

The same rule applies to answer and advance POSTs: they require an already activated assignment and may not bootstrap one; an unactivated patient gets 409.

## Session bootstrap refactor

`bootstrap_session()` must no longer call `assign_or_lookup()` for an unseen pair in Phase 2 study mode.

In Phase 2 it receives an existing `ArmAssignment` or refuses.

Existing assignment provenance determines:

- arm
- config version
- config hash

A new session for an activated case must preserve those values exactly.

## Case configuration resolution

Refactor S11b case resolution so, in Phase 2 study mode:

```
existing assignment -> resolve its historical configuration
no assignment       -> not an activated case
```

Do not return the current active configuration as though an unassigned patient were already a case.

The active configuration is used only inside Start case when creating the assignment.

## Events

Add event kind:

```
case.activated
```

Payload must contain only non identifying audit metadata needed operationally:

```
schedule_id
case_position
arm
```

Do not include clinician name.

The database assignment row remains the authoritative source for allocation.

## Files expected to change

- `src/ehr_simulator/db/migrations.py`
- `src/ehr_simulator/db/arm_assignments.py`
- `src/ehr_simulator/db/sessions.py`
- `src/ehr_simulator/db/events.py`
- `src/ehr_simulator/randomisation.py` (allocation-state loader)
- `src/ehr_simulator/web/study_session.py`
- `src/ehr_simulator/web/routes.py`
- new Start case service module
- index and patient-jumper templates
- database schema fixture
- `specs/session-11-implementation-checklist.md`
- relevant tests and documentation (`CLAUDE.md` current state)

## Required tests

### Activation

1. GET does not create an assignment.
2. Index GET does not consume a schedule item or create a schedule.
3. Start case creates one `phase2_randomized` assignment.
4. Planned arm and assignment seed are copied exactly.
5. Activation stores schedule ID, case position and timestamp; `activated_at == assigned_at`.
6. Assignment receives the active configuration version/hash.
7. A second identical Start request resumes the same case.
8. Concurrent Start attempts cannot consume two positions.
9. Activated assignment is immutable (UPDATE and DELETE rejected by trigger).
10. Same patient cannot be activated twice for one clinician.
11. A `phase2_randomized` insert missing any activation field is rejected by trigger.
12. A failure injected at each phase B write (assignment, session, each event) rolls back: no assignment, session or event exists afterwards, and the next Start activates the same position.
13. A failure after phase A leaves the schedule and no assignment.

### Allocation state

14. Only `phase2_randomized` rows count; `phase1_stub` rows are ignored.
15. Counts span clinicians and configuration versions.
16. Active patients without assignments are `(0, 0)`; assignments for patients outside the active pool are excluded.
17. Clinician A's activations change clinician B's generated schedule, and B's stored `allocation_state_json` equals the counts at generation.
18. The state is read inside the schedule lock (an activation committed between loader call sites cannot be missed).

### Mode and lifecycle rules

19. Study mode without `randomisation` keeps the Phase 1 GET bootstrap and shows no Start case.
20. In Phase 2 mode no `phase1_stub` row is created; an existing legacy row still resumes.
21. Open case is derived from assignment + progress: a completed case is not open and the next Start activates the next position.
22. Start with an open case resumes it without creating a schedule, assignment, session or event.
23. Exhausted schedule: Start returns 409 and writes nothing; index shows "All cases completed".

### Concealment and routing

24. Index and jumper do not reveal the planned arm, seed or block metadata, before or after Start.
25. Index and jumper list and link only activated cases.
26. Direct GET to an unactivated patient redirects to the index (303 and HTMX variants).
27. GET to an activated patient succeeds.
28. Answer and advance routes cannot create assignments; unactivated patient → 409.
29. Existing activated case keeps its historical configuration.
30. Route status contract: each table row above, browser and HTMX variants.
31. Error bodies contain no arm-revealing values.

### Schedule/config integrity

32. Next unactivated item is selected in schedule order.
33. A schedule generated under compatible later config content remains usable.
34. Changed randomisation rules cause Start case refusal.
35. Changed patient pool that invalidates the selected item causes refusal.
36. Existing schedules are never rewritten.
37. Stale server (DB active ≠ `app.state`) refuses Start with 409 before any write.

### Regression

38. Non Phase 2 `phase1_stub` tests remain supported.
39. S11a/S11b/S11c tests remain green (S11c updated for the loader argument).
40. Full CI remains green.

## Checklist

On completion tick every item in section 4 of `specs/session-11-implementation-checklist.md`, and in section 9 the realised-assignment items "activated yes or no", "activation timestamp" and "configuration version at activation". Completion state and replacement relationship stay open (S11e, S11f).

## Explicit non goals

S11d does not implement:

- pause/resume timeout policy
- incomplete or abandoned case state
- clinician stopping rules
- replacement cases
- AI panel delivery
- telemetry
- Phase 2 research exports

It does not regenerate an existing schedule.

## Acceptance

S11d is complete when a clinician can activate one planned case only through explicit Start case, the activation is atomic and concealed beforehand, GET never consumes randomisation, the realised assignment is permanently linked to its schedule position and activation configuration, later schedules balance against realised assignments, retries cannot consume another case, and the complete test suite passes.

S11e owns case lifecycle, reconnection, pause/resume and clinician level stopping.
