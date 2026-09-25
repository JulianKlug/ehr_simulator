# Session 11e — Case lifecycle, reconnection and clinician stopping

## Goal

Add explicit measured case lifecycle state after S11d activation.

The simulator must distinguish active, paused, resumed, completed and incomplete cases, enforce study configured reconnection rules, and stop issuing new cases when clinician level limits are reached.

S11a through S11d are assumed complete.

## Core invariants

1. Once activated, a case never returns to the allocation pool.
2. Timeout or abandonment never deletes the assignment.
3. A completed case cannot become active again.
4. A timed out incomplete case cannot resume.
5. Short technical interruptions may resume within the configured grace period.
6. Voluntary pause is allowed only when configured.
7. Lifecycle transitions are auditable.
8. New case activation stops at the configured clinician limits.
9. There is no automatic whole study stopping.
10. Lifecycle state never changes ITT arm or configuration provenance.
11. A clinician holds at most one **open** case (state `active` or `paused`). A paused case still blocks Start case.

## Study configuration

Keep study schema version `"2"`.

Add optional:

```
case_lifecycle:
  reconnection_grace_seconds: 300
  voluntary_pause_enabled: false
  voluntary_pause_grace_seconds: null
  target_completed_cases_per_clinician: 20
  max_activated_cases_per_clinician: 24
  study_target_completed_cases: null     # informational only, never gates
```

Existing v2 historical snapshots without `case_lifecycle` must still parse.

### Validation

- `reconnection_grace_seconds >= MIN_RECONNECTION_GRACE_SECONDS` (see Heartbeat)
- `voluntary_pause_grace_seconds >= 1` when provided
- `voluntary_pause_grace_seconds` provided while `voluntary_pause_enabled` is false → rejected (ambiguous policy)
- `target_completed_cases_per_clinician >= 1`
- `max_activated_cases_per_clinician >= target_completed_cases_per_clinician`
- `study_target_completed_cases >= 1` when provided
- `extra="forbid"`, strict ints/bools, matching `RandomisationConfig`

No first use values are hard coded.

### Hash stability

These fields participate in `config_hash` **only when the section is present**. Serialization omits `case_lifecycle` when absent — the same wrap-serializer pattern as `randomisation` (`config/study.py` `_omit_absent_randomisation`) — so every pre-S11e snapshot and hash is byte-identical.

Fields are serialized exactly as supplied. The pause grace default is resolved at use, not stored: an unserialized helper `effective_pause_grace_seconds` returns `voluntary_pause_grace_seconds`, falling back to `reconnection_grace_seconds` when pause is enabled and the grace is null.

### Absent section

When a case's pinned configuration has no `case_lifecycle`:

- lifecycle rows are still created and tracked (state is needed for open-case detection)
- no reconnection timeout is evaluated
- voluntary pause is disabled
- no stopping limit applies to Start case when the **active** configuration has no section

`validate-config` prints a warning when `randomisation` is present without `case_lifecycle`.

## Lifecycle model

Planned state remains represented by an unactivated S11c schedule item.

Activation remains represented by the S11d `arm_assignments.activated_at`.

Add persistent lifecycle state for activated cases.

Current states:

```
active
paused
completed
incomplete
```

`resumed` is an auditable transition into `active` (`case.resumed` event), not a long lived current state.

Together with schedule and activation records this is sufficient to distinguish:

```
planned
activated
active
paused
resumed
completed
incomplete
```

## Clock

All lifecycle timestamps and timeout decisions use one injected clock, never SQLite `CURRENT_TIMESTAMP`, so tests can advance time.

- `create_app(..., clock=...)`: a callable returning an aware UTC `datetime`; default `datetime.now(UTC)`; stored on `app.state.clock`.
- The lifecycle service takes `now` as a parameter; it never reads the clock itself.
- Stored format matches existing columns: UTC `YYYY-MM-DD HH:MM:SS`.

## Database migration

Add migration 8.

Create:

```
CREATE TABLE case_lifecycle (
    clinician_id       TEXT NOT NULL,
    patient_id         TEXT NOT NULL,
    state              TEXT NOT NULL
        CHECK (state IN ('active', 'paused', 'completed', 'incomplete')),
    state_changed_at   TIMESTAMP NOT NULL,
    last_seen_at       TIMESTAMP NOT NULL,
    paused_at          TIMESTAMP,
    completed_at       TIMESTAMP,
    incomplete_at      TIMESTAMP,
    incomplete_reason  TEXT
        CHECK (incomplete_reason IS NULL OR incomplete_reason IN
               ('reconnection_timeout', 'pause_timeout', 'operator_abandoned')),
    PRIMARY KEY (clinician_id, patient_id),
    FOREIGN KEY (clinician_id, patient_id)
        REFERENCES arm_assignments(clinician_id, patient_id),
    CHECK (state <> 'paused' OR paused_at IS NOT NULL),
    CHECK ((state = 'completed')  = (completed_at IS NOT NULL)),
    CHECK ((state = 'incomplete') = (incomplete_at IS NOT NULL AND incomplete_reason IS NOT NULL))
);
```

No column defaults: the service writes every timestamp from the injected clock. `paused_at` may stay set on an `incomplete` row: a `pause_timeout` keeps it as evidence.

Add triggers refusing any `UPDATE` of a row whose state is `completed` or `incomplete`, and any `DELETE`, mirroring the migration 7 `phase2_randomized` triggers.

Create lifecycle rows only for `phase2_randomized` assignments with `activated_at` set. Phase 1 stub assignments get none.

### Backfill

For each such assignment present before migration:

- `progress.completed_at` set → `completed`, `completed_at = progress.completed_at`, `state_changed_at = progress.completed_at`
- otherwise → `active`, `state_changed_at = activated_at`

`last_seen_at` = the latest of `activated_at` and the pair's latest `events.server_ts`, never the migration time. A backfilled case whose real last contact is older than its pinned grace therefore times out on next contact, as it should.

No historical paused/incomplete state may be invented.

## Lifecycle layers

Follow the S11c split (`db/randomisation.py` DAO + `randomisation.py` service):

```
routes.py ─► web/case_contact.py / web/case_start.py / web/gating.py
                          │
                          ▼
             case_lifecycle.py        pure policy + transactional transitions
                          │
                          ▼
             db/case_lifecycle.py     sole writer of case_lifecycle
```

The CLI calls `case_lifecycle.py`, never the DAO directly.

### DAO (`db/case_lifecycle.py`)

Every transition is a compare-and-set: `UPDATE ... WHERE clinician_id = ? AND patient_id = ? AND state = <expected>`. A rowcount of 0 raises `CaseLifecycleError`, and nothing is written. All writes accept `commit=False` so callers can batch them into one transaction.

Operations:

```
fetch(conn, clinician_id, patient_id) -> CaseLifecycle | None
insert_active(conn, ..., now, commit)
touch(conn, ..., now, commit)                 # active only
pause(conn, ..., now, commit)                 # active -> paused
resume(conn, ..., now, commit)                # paused -> active
complete(conn, ..., now, commit)              # active -> completed
mark_incomplete(conn, ..., reason, now, from_state, commit)
counts_for_clinician(conn, clinician_id) -> LifecycleCounts
counts_by_state(conn) -> dict[clinician_id, LifecycleCounts]
```

`IncompleteReason` is an enum. Free text is never the primary reason.

### Service (`case_lifecycle.py`)

- `evaluate(case, policy, now) -> Continue | TimedOut(reason, deadline)`: pure.
  - `active`: timed out when `now - last_seen_at > reconnection_grace_seconds`; the deadline is `last_seen_at + grace`.
  - `paused`: timed out when `now - paused_at > effective_pause_grace_seconds`; the deadline is `paused_at + grace`.
  - With no `case_lifecycle` policy it always returns `Continue`.
- `enforce_timeout(conn, case, policy, now)`: inside the caller's transaction, applies a `TimedOut` decision. It marks the case incomplete, closes any open session and appends `case.incomplete`, all atomically. It then returns the decision so the caller can refuse its own action.
- `policy_for_case(...)`: the lifecycle policy from the case's **pinned** snapshot (`resolve_case_configuration`), never from the active config.

### Allowed transitions

```
activation -> active
active     -> paused      # voluntary pause
paused     -> active      # resume (case.resumed)
active     -> completed
active     -> incomplete  # reconnection_timeout | operator_abandoned
paused     -> incomplete  # pause_timeout | operator_abandoned
```

No transition leaves `completed` or `incomplete`. The DB triggers make that hold for writers outside the DAO too.

## Open case and index

Replace the S11d open-case definition in `web/case_start.py` (docstring + `find_open_case`). The rule changes from "progress not completed" to:

> **open case** = a `phase2_randomized` assignment whose lifecycle state is `active` or `paused`.

`completed` and `incomplete` cases are closed. An incomplete case never blocks Start case.

`IndexAction` gains:

```
START           no open case, limits not reached, schedule not exhausted
RESUME          open case is active     -> link to its frontier
RESUME_PAUSED   open case is paused     -> form POST /case/{pid}/resume
LIMIT_REACHED   a clinician limit is reached
EXHAUSTED       schedule exhausted (unchanged)
```

Precedence: open case first, then limits, then exhaustion.

`index_state` stays a pure read. It does not enforce timeouts; the next contact does.

Index case rows show one of these markers: `in progress` / `paused` / `complete ✓` / `incomplete`. Incomplete rows are not links. (The patient jumper keeps its S11d markers; a jump to an incomplete case redirects to the index.) `LIMIT_REACHED` shows "No further cases can be started." None of these views show the arm, seed, reason or limit values.

## Start case integration

Updated flow:

```
start_next_case
    │
    ├─ Phase 0  open case?  timed out ──► BEGIN IMMEDIATE, re-read,
    │                                         enforce_timeout, COMMIT, continue
    │                       still open ──► resume (touch only)
    │           none                  ──► continue
    ├─ fast-path limit check (read) ─ reached ──► ClinicianLimitReachedError
    ├─ Phase A  unchanged (schedule, own commit)
    └─ Phase B  BEGIN IMMEDIATE
                open case? (racing Start won)  ──► rollback, resume
                stale-server check
                limit check  (authoritative, inside the lock)
                first unactivated item ─ none ──► ScheduleExhaustedError
                schedule ↔ active config compatible
                activate assignment + insert_active lifecycle
                + session + case.activated + session.start
                COMMIT                          rollback on any failure
```

Resuming a paused case through Start case never resumes it implicitly. The 303 goes to the case page, which renders the paused view.

An exact retry that finds the case open returns it without duplicating lifecycle state.

## Heartbeat

```
POST /case/{patient_id}/heartbeat
```

`static/heartbeat.js` sends it every `HEARTBEAT_INTERVAL_SECONDS` while the editable measured case page is open. It sends one more immediately on `visibilitychange` → visible. The interval reaches the page through a template data attribute rendered from the Python constant; it is not duplicated in JS.

Constants:

- `HEARTBEAT_INTERVAL_SECONDS = 15` (`web/`)
- `MIN_RECONNECTION_GRACE_SECONDS = 180` (`config/study.py`). The floor covers Chrome's intensive background-tab throttling, which fires timers at most once per minute, with a 3× margin.
- A lockstep test pins `MIN_RECONNECTION_GRACE_SECONDS >= 3 * 60` and `HEARTBEAT_INTERVAL_SECONDS * 4 <= MIN_RECONNECTION_GRACE_SECONDS`. The config layer must not import `web/`.

The heartbeat records no answer or panel exposure data. A successful heartbeat updates only `last_seen_at`; it appends no event.

## Reconnection rule

"Meaningful contact" is:

- successful case GET (touch after a successful render, like `timepoint.enter`)
- answer save/clear
- timepoint advance
- heartbeat
- Start case / resume returning this case

Each contact runs in one transaction:

1. `evaluate` against the pinned policy.
2. `TimedOut` → `enforce_timeout`, COMMIT, then refuse the action (status table below). The refused action writes nothing.
3. `Continue` → perform the action. Once it succeeds, `touch` in its own commit: if `now - last_seen_at > RECONNECT_GAP_SECONDS` (`3 * HEARTBEAT_INTERVAL_SECONDS`), append `case.reconnected` with `{gap_seconds}` so short interruptions are auditable. A lost touch can only shorten the grace, never extend it. The final advance does not touch: it completes the case.

A reconnection keeps the existing open session; it does not create one.

### GET ordering

The S9b rule stands: nothing is written before the frontier gate, and no data is sliced for a refused request. The Phase 2 GET order is:

```
login → activated? → lifecycle evaluate (may commit incomplete, then redirect)
      → state gate → frontier gate → render → touch (+ case.reconnected)
```

Writing the timeout transition on GET is allowed. It is a lifecycle fact, not an allocation.

### Timeout result

1. state `incomplete`
2. reason `reconnection_timeout` (or `pause_timeout` when paused)
3. `incomplete_at` = detection time (`now`); `last_seen_at` / `paused_at` are left unchanged
4. any open session closed
5. assignment, progress, answers and telemetry preserved
6. `case.incomplete` payload `{reason, deadline, grace_seconds}`
7. all of the above in one transaction

## Voluntary pause

```
POST /case/{patient_id}/pause
POST /case/{patient_id}/resume
```

When the pinned policy has voluntary pause disabled (or no `case_lifecycle` section), pause returns 409 and changes nothing. The pause button renders only when pause is enabled.

Pause, in one transaction:

- `evaluate` first; timed out → the timeout path
- requires `active`
- state `paused`, `paused_at = now`
- closes the open session
- appends `case.paused`

Resume, in one transaction:

- requires `paused`
- `now - paused_at <= effective_pause_grace_seconds` → state `active`, `paused_at` cleared, `last_seen_at = now`, new session via `sessions.start_or_resume` with the pinned provenance, `case.resumed` + `session.start`
- beyond grace → `incomplete`, reason `pause_timeout`

GET of a paused case renders a paused interstitial showing only a Resume button: no panels, no questions, no data sliced. Answer and advance writes are refused. Heartbeats are refused and the page sends none.

## Completion

The winning final advance marks the lifecycle `completed` (`completed_at = now`, set once) in the same transaction as:

- progress completion
- final timing events
- session close
- `case.completed`

The final advance runs `evaluate` first. An advance arriving after the grace period makes the case incomplete, not completed.

A repeated final advance loses the progress CAS (412 stale, existing S9b path). It writes no lifecycle change and no second `case.completed`.

## Incomplete cases

Structured reasons (`IncompleteReason` enum, matching the DB CHECK):

```
reconnection_timeout
pause_timeout
operator_abandoned
```

An incomplete case:

- keeps its original assignment
- keeps its ITT arm
- keeps its config version/hash
- keeps existing answers and telemetry
- never returns to the randomisation pool
- cannot be resumed
- still counts as activated

Replacement is deferred to S11f.

## Operator CLI

### `abandon-case`

```
abandon-case STUDY_CONFIG --clinician NAME --patient PID [--db-path P]
```

- `study_identity.require` gate first (as for `reset-progress`)
- open case (`active`/`paused`) → `incomplete`, reason `operator_abandoned`, closes the open session, appends `case.incomplete`, one transaction
- unknown clinician, no lifecycle row, or state `completed`/`incomplete` → exit 1 before any write

### `case-status`

```
case-status STUDY_CONFIG [--db-path P]
```

- read-only: WAL snapshot `BEGIN…ROLLBACK`, `require` gate
- per `clinician_id` (never the name): counts by state, activated count, remaining against the active limits
- study-wide completed count against `study_target_completed_cases` when set; informational only, never gates anything

### `reset-progress`

Lifecycle aware:

- no lifecycle row (Phase 1) → unchanged
- `active` / `paused` → allowed; rewinds the frontier only; lifecycle state, provenance and arm unchanged
- `completed` / `incomplete` → exit 1 before any write (terminal states)

## Events

Add:

```
case.paused       {}
case.resumed      {paused_seconds}
case.reconnected  {gap_seconds}
case.completed    {}
case.incomplete   {reason, deadline | null, grace_seconds | null}
```

`deadline`/`grace_seconds` are null for `operator_abandoned`.

Do not duplicate the clinician name in event payloads.

## Clinician stopping

Counts come from `case_lifecycle` for the clinician, across all configuration versions:

- completed = state `completed`
- activated = all rows (active, paused, completed, incomplete)

Using the **active** configuration's `case_lifecycle`, refuse a new Start case when either is true:

```
completed_count >= target_completed_cases_per_clinician
activated_count >= max_activated_cases_per_clinician
```

- An open case is always resumable, whatever the limits.
- The authoritative check runs inside Phase B's `BEGIN IMMEDIATE`, so two tabs cannot race past a limit. The pre-Phase-A read is a fast path only.
- Refusal: `ClinicianLimitReachedError` (a `CaseStartRefusedError`) → 409. The index shows `LIMIT_REACHED`.
- A limit never alters existing incomplete cases.
- There is no automatic whole-study close. `study_target_completed_cases` is display-only (`case-status`).

## Historical configuration

Lifecycle policy is pinned to the case activation configuration.

For an already activated case, use that case's historical:

- reconnection grace
- voluntary pause policy
- pause grace

Do not switch an active or paused case to newer lifecycle settings after a configuration activation. A case pinned to a snapshot without `case_lifecycle` never times out (see Absent section).

Stopping rules for whether a **new** case may start use the currently active configuration.

## HTTP status contract

Phase 2 only. Refusals write nothing except a committed timeout transition where noted.

| Request | Condition | Response |
|---|---|---|
| any case route | not Phase 2 / unactivated patient | existing S11d behaviour (redirect GET, 409 POST) |
| GET case | active | 200 editable (existing gates) |
| GET case | paused | 200 paused interstitial |
| GET case | completed | existing completed behaviour |
| GET case | incomplete, or timed out now | 303 / `HX-Redirect: /` |
| heartbeat | active, within grace | 204 |
| heartbeat | paused / completed / incomplete / timed out now | 409 + `HX-Redirect: /` |
| answer / advance | paused | 409 |
| answer / advance | incomplete / timed out now | 409 + `HX-Redirect: /` |
| pause | success | 303 / `HX-Redirect` to case page |
| pause | disabled / not active / timed out now | 409 |
| resume | success | 303 / `HX-Redirect` to frontier |
| resume | not paused / timed out now | 409 + `HX-Redirect: /` |
| Start case | clinician limit reached | 409 |

`CaseLifecycleError` from a CAS miss on a concurrent transition → 409, never 500.

## Exports

No export redesign (deferred to the S11 export set). Required:

- `export-answers` default output still includes incomplete cases' answered rows
- `--only-complete` excludes them (their `progress.completed_at` is null)
- `divergence-view` unaffected

## Files expected to change

- `src/ehr_simulator/config/study.py` (`CaseLifecycleConfig`, omit-when-absent, `MIN_RECONNECTION_GRACE_SECONDS`)
- `src/ehr_simulator/db/migrations.py` (migration 8 + backfill + triggers)
- new `src/ehr_simulator/db/case_lifecycle.py` (DAO)
- new `src/ehr_simulator/case_lifecycle.py` (service)
- new `src/ehr_simulator/web/case_contact.py` (per-request lifecycle gate, clock, heartbeat constants)
- `src/ehr_simulator/db/events.py`
- `src/ehr_simulator/db/exceptions.py`
- `src/ehr_simulator/web/app.py` (`clock`, error mapping)
- `src/ehr_simulator/web/case_start.py` (open case, limits, `IndexAction`)
- `src/ehr_simulator/web/gating.py`
- `src/ehr_simulator/web/routes.py`
- `src/ehr_simulator/cli.py`, `cli_support.py` (`abandon-case`, `case-status`, `reset-progress`, `validate-config` warning)
- index + questions pane templates, paused interstitial (`case_paused.html`), `static/heartbeat.js`
- schema fixture, `configs/` Phase 2 example config
- relevant tests, `CLAUDE.md`, `specs/ROADMAP.md`, checklist sections 5 and 7

## Required tests

### Configuration

1. Existing v2 config without lifecycle settings still parses.
2. Pre-S11e snapshot serialization and `config_hash` are byte-identical.
3. Valid lifecycle configuration changes `config_hash`.
4. Invalid grace periods, grace below the floor, pause grace while disabled, or bad stopping limits are rejected.
5. Pause grace resolves to reconnection grace when enabled and omitted, and the resolved value is not serialized.
6. Lockstep: heartbeat interval vs grace floor.
7. `validate-config` warns on `randomisation` without `case_lifecycle`.

### Migration

8. Migration 8 on an S11d DB backfills `completed` / `active` with the correct timestamps; `last_seen_at` is never the migration time.
9. Phase 1 stub assignments get no lifecycle row.
10. CHECK constraints and triggers refuse an invalid state, an update of a terminal row, and a delete.

### Lifecycle

11. Start case creates `active` lifecycle state in the activation transaction; a failure rolls it back.
12. Valid active -> paused -> active transition works.
13. Invalid transitions are refused and write nothing.
14. Completed state is terminal.
15. Incomplete state is terminal.
16. Assignment/config provenance and arm never change across transitions.

### Reconnection and pause

17. Heartbeat updates `last_seen_at` and appends no event.
18. Contact within reconnection grace continues the same case and session; a gap above `RECONNECT_GAP_SECONDS` appends `case.reconnected`.
19. Contact after reconnection grace marks incomplete atomically (state, session close, event) and refuses the action.
20. A timed-out GET redirects before any data is sliced.
21. Pause is refused when disabled or when the pinned snapshot has no section.
22. Pause and resume within configured grace succeeds with a new session and `case.resumed`.
23. Resume beyond pause grace becomes `pause_timeout` incomplete.
24. Paused case: interstitial renders no data; answer/advance/heartbeat are refused.
25. Timed out case cannot accept answer or advance writes.
26. No timeout is ever evaluated for a case pinned to a config without `case_lifecycle`.

### Open case and Start case

27. An incomplete case no longer blocks Start case.
28. A paused case blocks Start case; Start redirects to the paused view without resuming.
29. Start case with a timed-out open case marks it incomplete, then activates the next case.

### Completion and stopping

30. Final advance atomically marks the case completed.
31. Repeated final advance does not duplicate completion.
32. Final advance after grace yields incomplete, not completed.
33. Incomplete case remains counted as activated.
34. Target completed count blocks another Start case.
35. Maximum activated count blocks another Start case.
36. An open case stays resumable at the limit.
37. Concurrent Start cases cannot exceed the maximum activated count.
38. Index shows `LIMIT_REACHED` and the state markers; no arm is rendered.
39. The simulator never performs automatic whole study stopping.

### CLI

40. `abandon-case` marks `operator_abandoned`; it refuses terminal or unknown cases with exit 1 and no write.
41. `case-status` is read-only and prints clinician ids, never names.
42. `reset-progress` refuses completed/incomplete Phase 2 cases and leaves lifecycle unchanged on open ones.
43. Subprocess exit-code test covers the two new commands.

### Export

44. Incomplete cases appear in the default export and are excluded by `--only-complete`.

### Regression

45. Existing S11d Start case idempotency remains intact.
46. A historical case uses its pinned lifecycle policy after a new config activation.
47. All earlier tests and CI remain green.

All time-dependent tests use the injected clock, never sleep.

## Explicit non goals

S11e does not implement:

- replacement case selection
- replacement linkage
- AI intervention delivery
- panel telemetry
- multi-tab protection (checklist 34; concurrent tabs are safe through CAS but not detected)
- PP classification
- research export redesign (lifecycle outcome columns belong to the S11 export set)

It does not automatically delete, recycle or reassign an incomplete case.

## Acceptance

S11e is complete when every realised case has auditable lifecycle state, short interruptions and voluntary pauses obey the pinned study configuration, timeout preserves the original incomplete assignment, incomplete cases no longer block new cases, completion is atomic, clinician level case limits prevent further activation, no whole study stop is automatic, and the complete test suite passes.

S11f owns replacement scheduling for incomplete cases.
