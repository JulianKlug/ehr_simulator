# Session 11m — Privacy, multi tab protection and backup identity

## Goal

Harden Phase 2 behavioural data collection around clinician identity, simultaneous browser tabs, and study specific backups without changing the scientific study design.

Session 11m closes three implementation gaps that remain after S11a through S11l:

- new behavioural records must use pseudonymous clinician identity without duplicating the normalised clinician name
- one measured case must not silently behave as if two browser tabs were one authoritative view
- every study backup must remain attributable to its study, database schema generation, and creation time

S11a through S11l are assumed complete. In particular, S11j provides `tab_id` and browser monotonic telemetry, S11k derives per tab panel exposure, and S11l leaves multi tab observations explicitly unresolved for S11m.

This specification is drafted against repository `main` after S11g through S11i were merged. The expected S11j migration adds `events.tab_id` and `events.client_mono_ms`; use the next free migration number at implementation time rather than renumbering existing migrations.

## Core invariants

1. `clinician_id` remains the routine research identifier for clinicians.
2. `clinicians.name_normalized` remains operational identity data and is not copied into new behavioural event payloads.
3. Historical events are not rewritten solely to remove old `name_normalized` payloads.
4. The optional clinician name keyfile remains an explicit operator action and remains separate from routine research outputs.
5. At most one browser tab may own the primary measured view of one active clinician/patient case at a time.
6. A second tab can never silently save answers, advance, keep the lifecycle alive, or contribute authoritative primary telemetry as if it were the owning tab.
7. A detected tab conflict is auditable even when browser behaviour makes perfect prevention impossible.
8. Tab conflict handling never changes randomised arm, case configuration provenance, lifecycle history, or ITT assignment.
9. Revisit telemetry remains distinguishable from the primary measured view and never takes ownership away from the active primary view.
10. Practice observations are not made part of the measured multi tab rule in S11m.
11. Study mode backups are isolated by `study_id` and carry schema version and UTC creation time in their identity.
12. A backup operation never silently relabels a database as another study.
13. Retention and deletion remain operator/study policy. S11m adds no automatic pruning.

## Existing behaviour being replaced

### Behavioural identity

The current login route appends:

```
kind = "clinician.login"
payload = {"name_normalized": ...}
```

although the event row already contains `clinician_id`.

S11m removes that duplication for new writes. The `clinicians` table remains unchanged because the normalised name is still needed for login lookup and explicit keyfile generation.

### Multiple tabs

S11j makes separate tabs observable but deliberately does not choose a conflict policy. S11k and S11l therefore derive per tab results first and mark an observation conflicted/indeterminate when more than one tab contributes in a way that cannot safely be combined.

S11m adds the ownership rule that prevents new Phase 2 primary writes and authoritative telemetry from multiple tabs.

### Backups

The current backup helper writes files such as:

```
ehr_simulator_20260927T140000Z.db
```

into one backup directory. The backup itself contains the database tables, but the filename and directory do not make study identity or schema generation explicit.

S11m makes study backup identity explicit while preserving the existing online SQLite backup mechanism.

## In scope

### Clinician identity privacy

For new events:

- `clinician_id` is carried in the existing event column
- `payload_json` must not contain `name_normalized`
- browser telemetry must not contain typed names, answer values, free text, raw clinical values, or browser fingerprint attributes

Change successful login event payload to `{}` unless a later categorical non identifying field is genuinely required.

Add a defensive event payload validation rule that refuses `name_normalized` anywhere in a newly appended payload. The check should recurse through dict/list payload structures so a future producer cannot reintroduce the field under a nested object.

Do not add a migration that rewrites historical event JSON.

The existing clinician table remains:

```
clinicians(clinician_id, name_normalized, ...)
```

The existing keyfile contract remains:

```
clinician_id,name_normalized
```

with POSIX mode `0600` where supported.

### Multi tab ownership model

Add a server authoritative current lease for the active measured case.

The lease is an operational concurrency primitive, not the research audit record. Audit history is stored in append only events.

Expected table:

```
CREATE TABLE case_tab_leases (
    clinician_id  TEXT NOT NULL,
    patient_id    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    tab_id        TEXT NOT NULL,
    claimed_at    TIMESTAMP NOT NULL,
    last_seen_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (clinician_id, patient_id),
    FOREIGN KEY (clinician_id, patient_id)
        REFERENCES arm_assignments(clinician_id, patient_id)
);
```

This is expected to be the next migration after the S11j event column migration. If another migration lands first, use the next free number.

`case_tab_leases` is intentionally mutable/current state:

- same tab may refresh `last_seen_at`
- release removes the row
- a stale lease may be replaced atomically
- startup may clear all rows because a lease from a prior server process cannot be assumed live

Research auditability comes from events, not from retaining old lease rows.

### Lease timing

Use a platform constant rather than a study configuration field:

```
TAB_LEASE_HEARTBEAT_SECONDS = 15
TAB_LEASE_TTL_SECONDS = 45
```

The TTL is an operational safety timeout, not an exposure definition and does not enter `config_hash`.

A focused/visible primary measured page refreshes its lease at the heartbeat cadence.

On `visibilitychange`, blur, `pagehide`, pause, completion, incomplete transition, or logout, the browser/server should release the lease best effort where the transition is known.

If a browser disappears without releasing, another tab may claim only after the existing lease is stale under server time.

### Lease service

Add a small service/DAO boundary, for example:

```
claim_case_tab(...)
refresh_case_tab(...)
release_case_tab(...)
require_case_tab_owner(...)
```

All ownership decisions use the database row under `BEGIN IMMEDIATE` so two claim requests cannot both win.

Claim behaviour:

1. resolve the authenticated clinician from the existing cookie
2. require a measured Phase 2 case for that clinician/patient
3. require the case lifecycle to be active
4. require the supplied `session_id` to be the current open session for the case
5. no existing lease -> insert and grant
6. existing lease for same `tab_id` and same session -> refresh and grant idempotently
7. existing live lease for another tab -> refuse with conflict
8. existing stale lease -> record expiry, replace it, and grant

A browser supplied clinician ID, arm, configuration version, or session for another case is never trusted.

### Public routes

Add measured case routes equivalent to:

```
POST /case/{patient_id}/tab/claim
POST /case/{patient_id}/tab/heartbeat
POST /case/{patient_id}/tab/release
```

Request body contains only the S11j `tab_id` and, where useful, the current session identifier already rendered by the server.

Responses:

- claim granted or idempotent same tab claim: `204`
- live other tab owns the case: `409`
- stale/terminal/wrong case or session: `409`
- unauthenticated: existing login redirect/auth failure behaviour
- non Phase 2 or practice route: the guard is not applied by this session

The conflict response body must not reveal the randomised arm or the other tab's browser details.

### Client guard

Add a small client controller, for example `static/case_tab_guard.js`.

For a primary measured view:

1. page controls that can mutate measured state start disabled/pending
2. the controller obtains the stable S11j tab ID
3. it claims the case lease
4. only after a successful claim are answer autosave, advance, lifecycle heartbeat, pause, and authoritative primary telemetry enabled
5. a `409` renders a blocking conflict state and leaves those controls disabled
6. visibility/focus return may revalidate ownership before resuming writes

A second already rendered tab may briefly contain clinical HTML before its asynchronous claim is refused. S11m therefore does not claim perfect prevention of duplicate display. That unavoidable race must be audited and the second tab must not become an authoritative writer or telemetry source.

### Protected measured writes

For primary measured Phase 2 cases, require ownership for:

- answer upsert/clear
- advance
- lifecycle heartbeat
- pause
- primary browser telemetry batches
- primary panel exposure events
- browser intervention mounted acknowledgements

Resume creates or reopens the server session first; the resumed page must then claim a tab before ordinary measured writes continue.

Start case itself is not tab owned because no case page lease exists before activation. The redirected first case page claims ownership.

Revisit GETs remain read only. Revisit telemetry may be retained with `visit_kind=revisit`, but it must never satisfy the primary owner requirement or alter primary timing/PP derivation.

### Event taxonomy

Add at least:

- `tab.claimed`
- `tab.released`
- `tab.conflict`
- `tab.lease_expired`

Use the S11j `tab_id` event column for the claimant/current tab.

Payloads are categorical only. Suggested fields:

```
tab.claimed:       {"reason": "initial|reclaim"}
tab.released:      {"reason": "hidden|blur|pagehide|pause|complete|incomplete|logout|explicit"}
tab.conflict:      {"reason": "live_other_tab"}
tab.lease_expired: {"reason": "ttl"}
```

Do not include clinician name, answer values, AI values, clinical values, or user agent/fingerprint data.

The current owning tab ID need not be copied into another event's payload because it is already recoverable from accepted owner events. If implementation needs it for debugging, use only the opaque tab ID, never browser fingerprint attributes.

### Final multi tab derivation rule

S11m finalises the S11j/S11k/S11l conflict rule.

For each primary observation:

- accepted research events from one authoritative owner tab only -> derive normally
- a refused second tab claim with no accepted competing primary telemetry -> set `tab_conflict_detected=true`, but the owner's telemetry may remain `complete`
- accepted overlapping primary research telemetry from multiple tabs, an ownership gap that cannot be resolved, or legacy S11j data with multiple contributing tabs -> `telemetry_status=multi_tab_conflict`
- never sum simultaneous tabs to manufacture foreground, active, panel, or AI exposure duration

A conflict event by itself is not proof that the valid owner's measured telemetry is unusable.

S11l conservative PP behaviour still applies when telemetry status is actually `multi_tab_conflict`/indeterminate.

### Backup identity

Keep SQLite `Connection.backup()` as the copy mechanism.

Before a study mode backup:

1. open/read the source database identity
2. require exactly the expected `study_id` when the caller is study aware
3. read the current schema migration version from `schema_migrations`
4. refuse if identity is missing/mismatching or schema state is invalid
5. copy the database
6. open the copied database read only and verify the same study identity and schema version

For a study bound database, default destination becomes:

```
<backup_root>/<study_id>/study_<study_id>_schema_<schema_version>_<UTC>.db
```

Example:

```
data/backups/icu_ai_phase2/study_icu_ai_phase2_schema_13_20260927T140000Z.db
```

The internal copied `study_identity` and `schema_migrations` tables remain the authoritative identity. The path makes that identity visible operationally.

For a non study/unbound database, preserve the existing legacy backup naming/placement behaviour unless a later general migration deliberately changes it.

An explicit `--backup-dir` is treated as a backup root; study backups still go into its `<study_id>/` child directory.

Never place two studies' Phase 2 backup files in one undifferentiated study directory.

Do not copy or generate the clinician name keyfile as part of backup.

### Backup collisions and failures

A backup destination that already exists must not be silently overwritten.

If verification of the copied backup fails:

- delete the failed destination when safe
- log a failure without clinical/identity mapping data
- return/raise failure to the operator

Shutdown backup failure should retain the existing explicit application logging behaviour. It must not rewrite the source DB.

## Database/schema changes

Expected one migration after S11j:

- add `case_tab_leases`
- no change to `clinicians`
- no rewrite of historical `events`
- no backup metadata table in the live research database

S11j's expected `events.tab_id` and `events.client_mono_ms` columns are prerequisites.

## Configuration changes

None.

Multi tab lease timing is an operational constant, not a study parameter.

Backup identity is derived from the bound database and schema migrations, not from new study configuration.

## Public API/route changes

New internal clinician facing routes:

- `POST /case/{patient_id}/tab/claim`
- `POST /case/{patient_id}/tab/heartbeat`
- `POST /case/{patient_id}/tab/release`

Existing measured mutation routes gain the S11m owner guard.

CLI `backup` keeps its command name. Its output path changes for a study bound database to the study isolated path above.

## Failure behaviour

- event payload containing `name_normalized` -> reject before insert
- second live tab claim -> `409`, record `tab.conflict`, no measured write ownership
- non owner answer/advance/heartbeat/primary telemetry -> `409`, no state mutation
- stale lease -> expire and replace atomically
- server restart -> old operational leases are cleared; browser tabs must reclaim
- missing/mismatching backup study identity -> refuse, no mislabeled backup
- invalid schema version during backup -> refuse
- copied backup identity mismatch -> fail and remove invalid destination where safe

No failure path changes the case arm or configuration provenance.

## Files expected to change

- `src/ehr_simulator/db/migrations.py`
- `src/ehr_simulator/db/events.py`
- new `src/ehr_simulator/db/tab_leases.py`
- new small tab ownership service module if kept separate from the DAO
- `src/ehr_simulator/web/routes.py`
- answer/advance/lifecycle request guards
- S11j telemetry endpoint guard
- `src/ehr_simulator/web/templates/_patient_view.html`
- `src/ehr_simulator/web/static/case_tab_guard.js`
- `src/ehr_simulator/web/static/answers.js`, `advance.js`, `heartbeat.js` integration where needed
- `src/ehr_simulator/db/backup.py`
- `src/ehr_simulator/web/app.py` shutdown backup call
- `src/ehr_simulator/cli.py` backup reporting
- schema fixture, tests, `CLAUDE.md`, checklist

## Required tests

### Clinician identity privacy

1. Successful new login event stores `clinician_id` and does not store `name_normalized` in `payload_json`.
2. `events.append()` refuses a top level `name_normalized` payload field.
3. `events.append()` refuses nested `name_normalized` payload content.
4. Existing historical event rows containing `name_normalized` remain readable and are not migrated.
5. Clinician login lookup still uses `clinicians.name_normalized`.
6. Explicit keyfile generation still returns the clinician ID/name mapping.
7. Keyfile mode remains `0600` where supported and is not produced unless requested.

### Lease persistence and concurrency

8. First active measured tab claim creates one lease.
9. Same tab/session claim is idempotent and refreshes the lease.
10. Two concurrent claim attempts cannot both become owner.
11. Live second tab claim returns `409` and writes `tab.conflict`.
12. Conflict event contains no clinician name, arm, answer, AI value, or clinical value.
13. A stale lease is expired and a new tab can claim atomically.
14. Stale replacement writes `tab.lease_expired` before/with the new ownership transition.
15. Explicit owner release removes the current lease and is idempotent.
16. Non owner release does not remove the valid owner's lease.
17. Server startup clears persisted operational leases without deleting research events.
18. Terminal/incomplete/paused case cannot hold or claim an active primary lease.

### Protected writes

19. Owner tab can save an answer.
20. Non owner tab answer POST is refused without changing the answer row.
21. Owner tab can advance.
22. Non owner tab advance is refused without changing progress.
23. Non owner lifecycle heartbeat does not extend `last_seen_at`.
24. Non owner pause request is refused.
25. Owner primary telemetry batch is accepted.
26. Non owner primary telemetry is refused and does not contribute an accepted exposure event.
27. Revisit telemetry stays labelled revisit and does not take primary ownership.
28. Start case still activates without a pre existing tab lease; redirected page must claim before writes.
29. Resume produces a page that must claim before measured writes continue.

### Multi tab derivation

30. One owner tab yields normal foreground/active/panel derivation.
31. A refused second claim sets `tab_conflict_detected=true` without automatically invalidating otherwise complete owner telemetry.
32. Accepted overlapping primary telemetry from two tabs yields `telemetry_status=multi_tab_conflict` and is never summed.
33. Multi tab conflict remains distinguishable from complete zero exposure.
34. S11l PP derivation remains conservative when telemetry is genuinely indeterminate.

### Backup identity

35. Study backup path includes `study_id`, schema version, and UTC timestamp.
36. Study backup is written under `<backup_root>/<study_id>/`.
37. Opening the copied backup shows the same `study_identity` as the source.
38. Opening the copied backup shows the same schema migration version as the source.
39. A mismatching expected study ID refuses before a backup is produced.
40. Two study databases using the same backup root are written into separate study directories and cannot collide silently.
41. Existing destination is not silently overwritten.
42. Non study backup behaviour remains backwards compatible.
43. Backup never emits a clinician name keyfile.

### Regression

44. All S11a through S11l tests remain green.
45. Full CI including browser/e2e tests remains green.

## Out of scope

S11m does not implement:

- random study specific clinician IDs
- browser/device fingerprinting
- participant recruitment identity management
- automatic retention/deletion
- backup rotation or remote backup upload
- encryption key management
- final Phase 2 research export layout
- a new PP rule for a merely attempted conflicting tab when owner telemetry remains valid
- changing practice mode semantics

## Acceptance

S11m is complete when new behavioural events no longer duplicate the normalised clinician name; the optional name mapping remains operational and separate; exactly one tab can authoritatively mutate or measure an active primary Phase 2 case; every conflict is either prevented or explicitly auditable; multi tab telemetry has one final deterministic interpretation; and every study backup can be attributed from its path and contents to the correct study, schema generation, and creation time.

## Checklist items closed

Primarily Session 11 implementation checklist sections:

- 28 Clinician identity privacy
- 30 Backup identity
- 34 Multi tab protection

S11m also finalises the multiple tab semantics referenced by sections 14 through 24 without changing their scientific thresholds.
