# Session 11m — Privacy, multi tab protection and backup identity

## Goal

Harden Phase 2 behavioural data collection around clinician identity, simultaneous browser tabs, and study specific backups without changing the scientific study design.

Session 11m closes three gaps left after S11a through S11l:

- new behavioural records use the pseudonymous `clinician_id` and never duplicate the normalised clinician name
- one measured case never silently behaves as if two browser tabs were one authoritative view
- every study backup is attributable to its study, schema generation and creation time

S11a through S11l are complete. S11j provides `events.tab_id` / `render_id` / `client_mono_ms` (migration 12) and the render bound telemetry intake; S11k derives per tab panel exposure; S11j/S11k/S11l already report an observation with more than one contributing tab as `telemetry_status = multi_tab` (no seconds, no viewed).

## Review revisions (2026-09-28)

The first draft was written before S11j–l landed. Changes against the implemented code:

1. **Status name.** The implemented conflict status is `multi_tab` (`behavioral_timing.TelemetryStatus.MULTI_TAB`); `multi_tab_conflict` is dropped.
2. **No `intervention.*` events exist.** S11l derives delivery from the `timepoint.render` `ai` payload plus the AI `panel.mount` inside a telemetry batch. "Mounted acknowledgements" are therefore covered by the telemetry guard.
3. **Migration 13** (next free number).
4. **`tab.*` events must not read as browser telemetry.** `db.telemetry.load_telemetry_rows` selects `tab_id IS NOT NULL`; a `tab.*` row carrying `tab_id` would enter the S11j timeline and flip observations to `multi_tab`. The reader now selects the closed browser/panel kind set.
5. **The lease binds a render, not only a session.** A duplicated browser tab copies `sessionStorage` and so shares the tab id; an in-tab navigation sends the old page's release beacon, which can land after the new page's claim. Leases hold `(tab_id, render_id)`; writes carry both; a release only removes the lease of the render that sends it.
6. **One heartbeat.** Lease refresh rides on the existing S11e `POST /case/{pid}/heartbeat` instead of a second `/tab/heartbeat` timer.
7. **No release on blur/hidden.** Releasing on blur hands ownership to any other tab the moment the clinician glances at another window, and makes the owner lose its own case. A hidden owner keeps its lease while its heartbeat lands; the TTL covers vanished tabs; `pagehide` and logout release. Pause, completion and incomplete need no release call: a lease whose session is no longer the case's open session, or whose case is not `active`, is void.
8. **Telemetry acceptance = the render was granted a claim**, not "currently holds the lease". Otherwise the owner's own `pagehide` exit beacon can be refused after its release beacon and the owner's observation turns `incomplete`. `telemetry.js` holds a render's events until its claim resolves (an early flush would otherwise be refused and reported as a gap) and drops the events of a refused render.
9. **Guard scope.** Only a render of a measured `phase2_randomized` case whose pinned configuration carries `telemetry` is guarded: tab ids exist only there, and Phase 2 preflight already requires `telemetry`. The render payload records `tab_guard: true`, so the intake knows which renders need a claim; unguarded renders keep S11j behaviour (late events after completion, legacy data).
10. **Server restart is transparent.** Startup clears leases; a live tab's next heartbeat or write finds no lease and acquires it under the normal claim rules instead of being refused forever.
11. **`tab_conflict_detected`** is derived from `tab.conflict` events whose `render_id` belongs to the observation.
12. **Refused renders leave the derivation.** A refused tab's render has a `timepoint.render` row but no accepted telemetry; S11j would count it as an unobserved render and turn the owner's observation `incomplete`. The S11l reader drops renders that have a `tab.conflict` and no `tab.claimed`.
13. **Backup collisions get a suffix, not a refusal.** Implementation showed two shutdown backups within one second (a restart); refusing lost the second backup.

## Review revisions (2026-09-30, PR review)

14. **The conflict flag is visit specific.** `ObservationVariables.tab_conflict_detected` (the primary `timepoints.csv` row) counts only conflicts on **primary** renders; a refused revisit of the same timepoint no longer flags the primary observation. Panel rows keep their own `(t_index, visit_kind)` flag.
15. **Locked controls are really disabled.** While pending or refused the guard sets `disabled` on the answer fieldsets, `#advance-btn` and the pause button (request cancellation stays as a second line), and on `granted` re-enables only the controls it disabled itself, so a server-locked timepoint stays locked.
16. **No schema version, no backup — bound or not.** An unmigrated SQLite file is refused instead of receiving a legacy backup.

## Core invariants

1. `clinician_id` remains the routine research identifier for clinicians.
2. `clinicians.name_normalized` remains operational identity data and is never written into a new event payload.
3. Historical events are not rewritten to remove old `name_normalized` payloads.
4. The optional clinician name keyfile remains an explicit operator action, separate from routine research outputs.
5. At most one browser tab holds the lease of one active measured clinician/patient case at a time.
6. A tab without the lease can never save answers, advance, pause, keep the lifecycle alive, or contribute accepted telemetry for a guarded render.
7. A refused claim is auditable (`tab.conflict`) even though a second tab may briefly display the page before its claim is refused.
8. Tab handling never changes the randomised arm, configuration provenance, lifecycle history or ITT assignment.
9. A revisit render claims like any other render of the case, so it never takes the lease from another live tab.
10. Practice cases are not guarded in S11m.
11. Study mode backups are isolated by `study_id` and carry schema version and UTC creation time in their path.
12. A backup never silently relabels a database as another study, and never overwrites an existing file.
13. Retention and deletion remain operator policy. S11m adds no pruning.

## Existing behaviour being replaced

- `POST /login` appends `clinician.login` with payload `{"name_normalized": ...}` although the row already carries `clinician_id`.
- Two tabs of the same case both save answers, advance, heartbeat and post telemetry; derivation reports `multi_tab` after the fact.
- `db.backup.create_backup` writes `ehr_simulator_<UTC>.db` into one flat directory, whatever the study.

## In scope

### Clinician identity privacy

- Successful login appends `clinician.login` with payload `{}`.
- `events.append` and `events.append_browser_batch` refuse (`ValueError`, before any insert) a payload containing the key `name_normalized` at any depth of nested dicts/lists.
- No migration rewrites historical event JSON.
- `clinicians(clinician_id, name_normalized, …)` and the keyfile format `clinician_id,name_normalized` (mode `0600`) are unchanged.

### Guarded renders

A render is **guarded** when all hold:

- study mode, and the case resolves to `observation_mode = measured` with an `arm_source = phase2_randomized` assignment
- the case's pinned configuration has a `telemetry` block (so the render carries a `render_id`)
- the lifecycle is `active`

A guarded render's `timepoint.render` payload gains `"tab_guard": true`; `#patient-view` gains `data-tab-claim-url` and `data-tab-release-url`. Nothing else in the payload changes.

### Lease table (migration 13)

```
CREATE TABLE case_tab_leases (
    clinician_id  TEXT NOT NULL,
    patient_id    TEXT NOT NULL,
    session_id    TEXT NOT NULL REFERENCES sessions(session_id),
    tab_id        TEXT NOT NULL,
    render_id     TEXT NOT NULL,
    claimed_at    TIMESTAMP NOT NULL,
    last_seen_at  TIMESTAMP NOT NULL,
    PRIMARY KEY (clinician_id, patient_id),
    FOREIGN KEY (clinician_id, patient_id)
        REFERENCES arm_assignments(clinician_id, patient_id)
);
```

Mutable operational state, not research record: refresh updates `last_seen_at` (and `render_id` on a same tab claim), release deletes, a stale or void lease is replaced, lifespan startup deletes every row. Audit history lives in `tab.*` events. Timestamps come from `app.state.clock`.

### Lease timing

Platform constants (`web/tab_guard.py`), not study configuration, never in `config_hash`:

```
TAB_LEASE_TTL_SECONDS = 3 * HEARTBEAT_INTERVAL_SECONDS   # 45
```

A lease is **stale** when `now - last_seen_at > TAB_LEASE_TTL_SECONDS`, and **void** when its `session_id` is not the case's open session or the lifecycle is not `active`. Stale and void leases are treated as absent by every decision below.

### Lease service

Layers: `db/tab_leases.py` (sole writer of `case_tab_leases`, pure SQL) → `web/tab_guard.py` (ownership decisions + `tab.*` events). All decisions run under `BEGIN IMMEDIATE`, one commit.

`claim(render, tab_id)` — the render must be a guarded render of this clinician/patient in the case's open session:

| lease | outcome | event |
|---|---|---|
| absent | insert, grant | `tab.claimed {reason: initial}` |
| stale | replace, grant | `tab.lease_expired {reason: ttl}` (old tab) + `tab.claimed {reason: reclaim}` |
| void | replace, grant | `tab.claimed {reason: initial}` |
| same tab, same render | refresh, grant | none |
| same tab, other render | move to this render, grant | `tab.claimed {reason: navigate}` |
| other live tab | refuse | `tab.conflict {reason: live_other_tab}` |

`require_owner(tab_id, render_id)` — used by every guarded write: a live lease with both ids → refresh, allow. A lease that is absent, stale or void → acquire exactly as `claim` would (restart recovery). Anything else → refuse (`TabOwnershipError`), nothing written except `tab.conflict` when the refusal is a live other tab.

`release(tab_id, render_id)` — deletes the lease only when both ids match; otherwise a no-op. Records `tab.released {reason}`. Idempotent.

`release_all(clinician_id)` — logout; `tab.released {reason: logout}` per removed lease.

Browser supplied clinician, arm, session or configuration is never trusted; session and patient come from the server's render row.

### Routes

```
POST /case/{patient_id}/tab/claim     JSON {tab_id, render_id}
POST /case/{patient_id}/tab/release   JSON {tab_id, render_id, reason}   (sendBeacon)
```

- claim granted: `204`; refused (live other tab, unknown/foreign/unguarded render, case not active): `409`; malformed body: `422`; unknown clinician: `401` (a `fetch()` must not follow the login redirect)
- release: `204` always for a known clinician (no-op on mismatch); `422` malformed; `401` unknown clinician
- refusal bodies reveal neither the arm nor the other tab

### Protected writes

For a guarded case, these requests must carry the owner identity — headers `X-Ehrsim-Tab-Id` and `X-Ehrsim-Render-Id`, or (plain form posts) form fields `ehrsim_tab_id` / `ehrsim_render_id`:

- `POST /patient/{pid}/timepoint/{t}/answer`
- `POST /patient/{pid}/timepoint/{t}/advance`
- `POST /case/{pid}/heartbeat`
- `POST /case/{pid}/pause`

`require_owner` runs after the existing lifecycle `check`, before any write or `touch`. A refusal is `409` with header `X-Ehrsim-Tab: conflict` and no state change (no answer, progress, `last_seen_at` or lifecycle write). Missing identity on a guarded case is refused the same way: omitting the headers never bypasses the guard.

On the `200` advance, the owner's lease moves to the new render inside the same request (the lease already proves ownership), so the next write from the swapped view is accepted without waiting for a claim round trip.

`POST /case/start` and `POST /case/{pid}/resume` are not guarded (no render exists yet); the redirected page claims.

### Telemetry intake

`record_batch` additionally refuses (`409`, nothing written) a batch containing an event whose render payload has `tab_guard: true` unless a `tab.claimed` event exists with the same `render_id` and `tab_id`. The check is "ever granted", not "holds the lease now", so a granted render's late exit and its `pagehide` beacon are accepted after release or completion.

`db.telemetry.load_telemetry_rows` selects only the S11j/S11k browser and panel kinds.

### Client guard (`static/case_tab_guard.js`)

For a view with `data-tab-claim-url`:

1. marks `#patient-view` `data-tab-state="pending"`; the answer fieldsets, `#advance-btn` and the pause button get `disabled` until granted (the guard marks what it disabled and re-enables only those)
2. claims with the S11j tab id and the view's `render_id`
3. `204` → `granted`, controls re-enabled
4. `409` → `refused`: renders a blocking notice ("This case is open in another tab or window. Close it, then press Retry."), keeps controls disabled; Retry, `focus` and `visibilitychange → visible` re-claim
5. adds the owner headers to every htmx request (`htmx:configRequest`) and to `heartbeat.js`; fills the pause form's hidden fields
6. any write answered `409` with `X-Ehrsim-Tab: conflict` → `refused`
7. `pagehide` → `sendBeacon` release (`reason: pagehide`)
8. `htmx:afterSwap` of `#patient-view` → state of the new render (`granted` when the advance moved the lease — the guard re-claims idempotently to confirm)

`window.ehrsim.tabState(renderId)` returns `granted|pending|refused|unguarded`. `telemetry.js` sends only events whose render is `granted` or `unguarded`, keeps `pending` ones queued, and drops `refused` ones without reporting a gap.

A second tab may show clinical HTML until its claim is refused. S11m does not claim perfect display prevention; the refusal is audited and the tab never becomes a writer or a telemetry source.

### Event taxonomy

Add to `EventKind`: `tab.claimed`, `tab.released`, `tab.conflict`, `tab.lease_expired`. `events.append` gains `tab_id`; `tab.*` rows carry `session_id`, `patient_id`, `timepoint`, `render_id` of the render concerned and `tab_id` of the tab concerned (for `tab.lease_expired`, the expired tab). Payloads are categorical only:

```
tab.claimed:       {"reason": "initial|reclaim|navigate"}
tab.released:      {"reason": "pagehide|logout"}
tab.conflict:      {"reason": "live_other_tab"}
tab.lease_expired: {"reason": "ttl"}
```

No clinician name, answer, AI or clinical value, user agent or fingerprint data.

### Final multi tab derivation rule

For each observation (clinician × patient × `t_index` × `visit_kind`):

- accepted telemetry from one tab → derive normally (S11j/S11k/S11l unchanged)
- accepted telemetry from more than one tab (legacy S11j data, or sequential owners after a stale handover) → `multi_tab`, never summed (unchanged)
- `tab_conflict_detected = true` when any `tab.conflict` event names a render of the observation's `(t_index, visit_kind)`; the owner's telemetry keeps its own status. The primary observation (`ObservationVariables`, `timepoints.csv`) counts primary renders only; a conflict on a revisit never flags it

`behavioral_timing.ObservationTiming` and `panel_exposure.PanelSummary` are unchanged; `tab_conflict_detected` is added to `study_variables.ObservationVariables` (read from `tab.conflict` rows via `db.telemetry.load_tab_render_ids`). A conflict event alone is not proof that the owner's telemetry is unusable, and it does not change PP.

### Backup identity

`db/backup.py` keeps `Connection.backup()` and gains study identity:

```
create_backup(db_path, backup_root, *, expected_study_id=None) -> Path
```

1. open the source; read `study_identity` (`study_identity.fetch`) and `MAX(version)` from `schema_migrations`
2. `expected_study_id` given and ≠ stored (or stored is `None`) → `BackupIdentityError`, nothing created
3. schema version missing → `BackupIdentityError`, whether or not the DB is study bound
4. bound DB → `<backup_root>/<study_id>/study_<study_id>_schema_<N>_<UTC>.db`; unbound DB → legacy `<backup_root>/ehr_simulator_<UTC>.db`
5. destination created exclusively (`O_EXCL`); a taken name moves to the next `_<n>` suffix (`…Z_2.db`), never overwritten — two shutdown backups inside one second (a quick restart) must both survive
7. the copy is switched to `journal_mode=DELETE`: one self-contained file, no `-wal`/`-shm` sidecars
6. copy; reopen the copy read only (`mode=ro` URI); its identity and schema version must equal the source's; else delete the copy and raise `BackupIdentityError`

Example: `data/backups/icu_ai_phase2/study_icu_ai_phase2_schema_13_20260927T140000Z.db`.

`--backup-dir` (serve and backup) is the backup root. The lifespan shutdown passes `expected_study_id=app.state.study_id` in study mode; its failure keeps the existing `db.backup.failed` logging and never writes the source. `backup` CLI reports the path and, for a bound DB, the study and schema version. Logs carry paths and ids, never clinical rows or the name mapping. No keyfile is ever copied or generated.

## Database/schema changes

- migration 13 `s11m_case_tab_leases`: `case_tab_leases`
- no change to `clinicians`, no rewrite of `events`, no backup table

## Configuration changes

None. Lease timing is a platform constant; backup identity comes from the DB.

## Public API/route changes

- new `POST /case/{pid}/tab/claim`, `POST /case/{pid}/tab/release`
- answer, advance, heartbeat, pause gain the owner guard on guarded cases
- `/telemetry/events` refuses unclaimed guarded renders
- CLI `backup` output path for a bound DB changes as above

## Failure behaviour

- payload containing `name_normalized` → `ValueError` before insert
- live second tab claim → `409` + `tab.conflict`, no ownership
- non owner or identity-less write on a guarded case → `409` + `X-Ehrsim-Tab: conflict`, nothing mutated
- unclaimed guarded telemetry → `409`, nothing written
- stale lease → expired and replaced atomically
- server restart → leases cleared; the next heartbeat/write/claim reacquires
- backup identity missing/mismatching, schema version missing, no free destination name, copy verification fails → `BackupIdentityError`, no (or a removed) destination

## Files expected to change

- `db/migrations.py`, `db/events.py`, new `db/tab_leases.py`, `db/telemetry.py`, `db/backup.py`, `db/exceptions.py`
- new `web/tab_guard.py`; `web/routes.py`, `web/telemetry.py`, `web/timing_events.py` (render payload), `web/app.py` (lease clear, shutdown backup)
- `study_variables.py`, `study_variables_reader.py`
- templates `_patient_view.html`, `_questions_pane.html`, `base.html`; static `case_tab_guard.js`, `telemetry.js`, `heartbeat.js`, `style.css`
- `cli.py`; schema fixture, tests, `CLAUDE.md`, checklist

## Required tests

### Clinician identity privacy

1. New login event carries `clinician_id` and payload `{}`.
2. `events.append` refuses a top level `name_normalized`.
3. `events.append` refuses a nested `name_normalized` (dict in list in dict).
4. `append_browser_batch` refuses it too, writing nothing.
5. A historical row containing `name_normalized` stays readable after migrating.
6. Login lookup still resolves through `clinicians.name_normalized`.
7. Keyfile generation still maps id → name, mode `0600`, only when requested.

### Lease persistence and concurrency

8. First claim of a guarded render creates one lease and one `tab.claimed {initial}`.
9. Same tab, same render claim is idempotent (no second event) and refreshes `last_seen_at`.
10. Same tab, new render claim moves the lease (`navigate`).
11. Two claims from different tabs: exactly one wins; the loser gets `409` and one `tab.conflict`.
12. The conflict response and event hold no arm, name, answer, AI or clinical value.
13. A stale lease is replaced: `tab.lease_expired` for the old tab then `tab.claimed {reclaim}`.
14. A lease of a closed session (pause → resume) is void: another tab claims without conflict.
15. Release by the holding render removes the lease and is idempotent.
16. Release by another tab, or by an older render of the same tab, leaves the lease.
17. Logout releases every lease of the clinician.
18. Startup clears leases without touching events.
19. Paused, completed or incomplete case cannot claim (`409`).
20. Unguarded render (no telemetry block, practice, Phase 1) cannot claim (`409`); its writes need no identity.

### Protected writes

21. Owner can save an answer.
22. Non owner answer is refused; the answer row and events are unchanged.
23. Answer without identity headers on a guarded case is refused.
24. Owner can advance; the lease moves to the new render and the next answer from it is accepted.
25. Non owner advance is refused; progress unchanged.
26. Non owner heartbeat does not move `last_seen_at`.
27. Non owner pause is refused; lifecycle stays `active`.
28. With no lease (after restart) the tab's heartbeat reacquires and succeeds.
29. Start case and resume need no lease; the redirected page must claim before writing.

### Telemetry

30. Claimed render's batch is accepted.
31. Unclaimed guarded render's batch is refused, nothing written.
32. A granted render's exit is accepted after its release and after completion.
33. Unguarded renders keep S11j intake behaviour.
34. `tab.*` rows never appear in `load_telemetry_rows`; an observation with a claim and a refused conflict stays `complete`.

### Multi tab derivation

35. One owner tab derives normally.
36. A refused second claim sets `tab_conflict_detected` without changing the owner's status or PP.
37. Accepted telemetry from two tabs stays `multi_tab`, never summed, distinct from complete zero.
37a. A tab refused only on a revisit of timepoint 0 leaves the primary timepoint 0 `tab_conflict_detected=false`.

### Backup identity

38. Study backup path is `<root>/<study_id>/study_<study_id>_schema_<N>_<UTC>.db`.
39. The copy holds the source's `study_identity` and schema version.
40. Mismatching expected study refuses; nothing created.
41. Two studies sharing a root land in separate directories.
42. Existing destination is never overwritten; a same-second backup gets the `_2` suffix.
43. Unbound DB keeps the legacy name and placement.
44. Backup never writes a keyfile.
45. Shutdown backup in study mode uses the study path.
45a. An unbound SQLite file without `schema_migrations` is refused.

### Browser

46. e2e: a second page of the same case shows the conflict notice, its answer inputs and advance button are `disabled`, the owner's answers still save, and after the owner closes and Retry the controls are enabled again.

### Regression

47. All S11a through S11l tests stay green (updated only where the login payload, render payload or backup path is asserted).
48. Full CI including e2e stays green.

## Out of scope

Random study specific clinician ids, fingerprinting, recruitment identity, retention/deletion, backup rotation or upload, encryption, the Phase 2 export layout (S11n), a PP rule for attempted conflicts, practice guard, perfect prevention of a duplicate display.

## Acceptance

New behavioural events never carry the normalised name; the name mapping stays operational and separate; exactly one tab can write to, keep alive, or report telemetry for an active guarded case; every refused tab is audited; multi tab telemetry has one deterministic interpretation; every study backup is attributable from its path and contents to its study, schema generation and creation time.

## Checklist items closed

Sections 28 (clinician identity privacy), 30 (backup identity) and 34 (multi tab protection); the multi tab semantics referenced by sections 14–24 are finalised without changing their thresholds.
