# Session 11j — Browser visibility, focus, active time and tab identity

## Goal

Create the browser telemetry foundation required to distinguish wall clock time, foreground time, and active time for each measured clinician, patient, and timepoint.

Durations must be based on a browser monotonic clock so network latency and server clock skew do not inflate exposure measurements.

S11a through S11i are assumed complete.

## Core invariants

1. Existing S10 `elapsed_seconds` remains wall clock time and is not redefined.
2. `foreground_seconds` accumulates only while the study document is visible and its window is focused.
3. `active_seconds` is foreground time limited by a configured inactivity threshold.
4. Duration arithmetic uses a monotonic browser clock such as `performance.now()`.
5. Server timestamps remain retained for ordering, audit, and receipt evidence, but not as the primary duration clock.
6. Passive mouse movement does not count as qualifying activity.
7. Reading may be foreground but inactive after the inactivity threshold.
8. Every telemetry event is attributable to study, clinician, case/session, patient, timepoint, configuration version, and browser tab through existing joins plus the new tab identity.
9. Telemetry is append only source data. Derived timing can be rebuilt from raw events.
10. Missing browser telemetry is represented as missing/indeterminate, not as zero seconds.
11. Simultaneous/multiple tab identity is observable; final conflict policy remains S11m.
12. Primary and revisit telemetry remain distinguishable using the S11i `visit_kind` marker.

## Telemetry configuration

Add one optional `telemetry` block to `StudyConfig` in S11j and define all thresholds needed by S11j and S11k now, so S11k does not later change the canonical config shape merely by adding default fields.

Recommended shape:

```
telemetry:
  inactivity_threshold_seconds: 60
  panel_viewport_threshold: 0.05
  panel_viewed_threshold_seconds: 2.0
```

Validation:

- inactivity threshold is finite and greater than zero
- viewport threshold is finite and `0 < threshold <= 1`
- viewed threshold is finite and greater than zero

For the first use case:

- inactivity threshold = 60 seconds
- panel viewport threshold = 0.05
- panel viewed threshold = 2.0 seconds

The panel values are stored in S11j configuration but are consumed beginning in S11k.

The entire telemetry block participates in `config_hash` when present.

Historical configurations without the block remain parseable and retain their bytes.

## Browser tab identifier

Add a browser tab identifier generated client side.

Requirements:

- high entropy random identifier, preferably UUID v4 through `crypto.randomUUID()` with a safe fallback
- persisted for the lifetime of the tab using `sessionStorage`
- reused across full page and HTMX navigation within that tab
- never derived from clinician name, patient data, IP address, user agent, or fingerprinting
- not a cross browser/device identifier

Recommended storage key:

```
ehrsim:tab-id
```

The existing `client_seq.js` counter remains per tab and should be shared by telemetry events rather than creating a second independent sequence counter.

S11m will handle conflicting simultaneous views. S11j must at least make those views detectable through distinct or detectably colliding tab identities.

## Event storage migration

Add the next schema migration, expected migration 12 after S11i.

Extend the existing `events` table with:

```
tab_id          TEXT
client_mono_ms   REAL
```

Keep existing:

- `client_ts`
- `server_ts`
- `client_seq`

Add indexes useful for derivation, for example:

```
(session_id, tab_id, client_seq)
(session_id, patient_id, timepoint, kind)
```

Do not make historical events invalid because the new columns are NULL.

Update `events.append()` so server emitted legacy events can omit the fields and browser telemetry can supply them.

Validate `client_mono_ms` as finite and non negative before insert.

## Telemetry endpoint

Add an authenticated study route such as:

`POST /telemetry/events`

The endpoint accepts a small ordered batch of validated browser events.

Each event includes:

- event kind
- tab ID
- client sequence
- client monotonic milliseconds
- optional client wall timestamp for human audit/timestamp reconstruction
- current patient/timepoint context already present in the rendered page
- visit kind: primary or revisit

The server derives/validates:

- clinician ID from the signed/known clinician cookie
- active session/case association from server state
- case pinned configuration through the existing context
- patient/timepoint legitimacy

Do not accept clinician ID or arm as trusted browser supplied authority.

Reject telemetry for an unactivated Phase 2 patient or a session/case that does not match the supplied context.

## Browser event taxonomy

Add closed event kinds sufficient for reconstruction.

Recommended minimum:

- `browser.state`
- `browser.activity`
- `browser.timepoint_enter`
- `browser.timepoint_exit`

### `browser.state`

Payload:

```
{
  "visible": true|false,
  "focused": true|false,
  "reason": "initial|visibilitychange|focus|blur|pageshow|pagehide",
  "t_index": <int>,
  "visit_kind": "primary|revisit"
}
```

Do not rely only on delta events. Emit an initial full state snapshot when telemetry attaches to a rendered timepoint.

### `browser.activity`

Payload:

```
{
  "activity_kind": "click|touch|scroll|keyboard|answer_change|panel_toggle|timepoint_navigation",
  "t_index": <int>,
  "visit_kind": "primary|revisit"
}
```

Do not include key values, typed text, answer values, element text, clinical values, or coordinates unless separately justified later.

### `browser.timepoint_enter` / `browser.timepoint_exit`

These are client monotonic boundaries for duration derivation. They do not replace the S10 server `timepoint.enter`/`timepoint.exit` events.

They identify the monotonic interval in which the browser actually held that rendered timepoint.

Exit reason may include:

- advance
- finish
- htmx_swap
- pagehide
- redirect
- interruption

Best effort pagehide delivery may use `navigator.sendBeacon()` or `fetch(..., {keepalive:true})`.

Loss of the final event must not fabricate a duration beyond the last trustworthy monotonic transition.

## Qualifying activity

The following reset the inactivity deadline:

- click
- touch interaction
- scroll
- keyboard input
- answer modification
- panel open/close
- timepoint navigation

Passive mouse movement does not.

Do not record every keystroke as an event if that would create unnecessary volume. It is sufficient to record a generic qualifying activity transition/heartbeat with no key contents and sensible rate limiting.

Likewise scroll activity may be throttled/debounced while still accurately marking activity resumption.

## Foreground derivation

Create a pure derivation module, for example:

`behavioral_timing.py`

Input is raw events for one:

`clinician × patient × timepoint × tab × visit_kind`

Sort primarily by client monotonic time and use client sequence to break ties within the same tab.

Server timestamp remains an audit field, not the duration axis.

### State model

Foreground is true only when:

```
visible == true AND focused == true
```

The initial `browser.state` event establishes the starting state.

A foreground interval ends when:

- document becomes hidden
- window blurs
- timepoint exits
- telemetry terminates/interruption closes the trustworthy interval

`foreground_seconds` is the sum of these monotonic intervals.

## Active time derivation

At client timepoint entry, treat `timepoint_navigation` as qualifying activity.

Each qualifying activity at monotonic time `a` creates active eligibility through:

```
a + inactivity_threshold
```

Active time is the intersection of:

- foreground intervals
- the union of activity eligibility intervals
- the timepoint monotonic interval

Equivalent interpretation:

- active starts immediately on qualifying activity while foreground
- it continues until the threshold expires with no further qualifying activity
- a later qualifying activity resumes active time from that later activity forward
- inactive gaps are not retroactively filled

This definition avoids sampling based approximations.

## Existing elapsed time

Keep S10 derivation unchanged:

`elapsed_seconds = server wall clock exit - server wall clock enter`

S11j adds:

- `foreground_seconds`
- `active_seconds`

Do not rename `elapsed_seconds` to "active" or replace it in the existing export/divergence path.

## Multiple tabs

Derive per tab metrics first.

If one observation has events from more than one tab, retain all raw events and mark the observation as multi tab for S11m.

Do not simply sum per tab foreground/active durations because simultaneous tabs would double count.

Until S11m defines the final conflict rule:

- single tab observation -> authoritative derived duration
- multi tab observation -> per tab durations available, aggregate marked indeterminate/conflicted rather than silently summed

## Revisit handling

Primary and revisit intervals are separate derivation groups.

The original measured timepoint's foreground/active values are based on `visit_kind=primary`.

A later revisit may have its own exploratory telemetry, but must not overwrite or extend the original primary duration.

## Event volume and batching

Use batching for rapid activity/state events where safe.

Constraints:

- preserve client sequence for deterministic reconstruction
- flush on pagehide/timepoint exit where possible
- cap maximum batch size and payload size server side
- reject unknown event kinds/fields
- do not store raw pointer trails or raw keyboard content

## Privacy

New behavioural payloads contain only pseudonymous identifiers through existing columns/joins.

Do not include `name_normalized`.

Do not include:

- answer values
- free text
- raw clinical values
- browser fingerprint attributes

S11m performs the broader privacy clean up, but S11j events must already comply.

## Failure semantics

Telemetry failure must not block the clinician from completing a case unless the study explicitly chooses such a policy in a future session.

On client/server telemetry write failure:

- continue the clinical workflow
- log/record the telemetry failure where possible
- derived foreground/active values become incomplete/indeterminate
- never substitute elapsed time as foreground/active

## Files expected to change

- `src/ehr_simulator/config/study.py`
- `src/ehr_simulator/db/migrations.py`
- `src/ehr_simulator/db/events.py`
- new pure behavioural timing derivation module
- `src/ehr_simulator/web/routes.py`
- `_patient_view.html` telemetry context attributes
- `base.html` script includes
- new `static/telemetry.js`
- `static/client_seq.js` integration
- `static/answers.js`, `advance.js`, `pane.js` hooks for qualifying activity where needed
- test fixtures, schema snapshot, documentation

## Required tests

### Configuration

1. First use case inactivity threshold 60 seconds loads.
2. Zero/negative inactivity threshold is rejected.
3. Viewport threshold outside `(0,1]` is rejected.
4. Zero/negative viewed threshold is rejected.
5. Telemetry block changes `config_hash`.
6. Historical config without telemetry block round trips unchanged.

### Tab identity

7. New tab context gets a valid random tab ID.
8. Same tab retains its ID across timepoint navigation.
9. Same tab retains its ID across full reload.
10. Telemetry payload cannot override clinician identity.
11. Telemetry for an unactivated Phase 2 patient is refused.

### Browser state

12. Initial state snapshot records visibility and focus.
13. `visibilitychange` emits the new full state.
14. blur emits focused false.
15. focus emits focused true.
16. pagehide closes the trustworthy interval when delivered.

### Foreground duration

17. Visible+focused interval accumulates foreground time.
18. Hidden tab does not accumulate foreground time.
19. Unfocused browser does not accumulate foreground time.
20. Refocus resumes foreground accumulation.
21. Hidden then visible creates two intervals whose durations sum correctly.
22. Network delay between browser event and server insert does not affect duration.
23. Invalid/non finite monotonic timestamp is rejected.
24. Missing final exit does not invent time after the last trustworthy event.

### Active duration

25. Timepoint entry starts an activity eligibility interval.
26. Activity before the threshold extends active time.
27. After 60 seconds with no qualifying activity, further foreground time is inactive for the first use case.
28. A later click resumes active time only from the click forward.
29. Scroll resumes active time.
30. Keyboard activity resumes active time without recording the key value.
31. Answer modification resumes active time without recording the answer value.
32. Panel toggle resumes active time.
33. Mouse movement alone does not resume active time.
34. Hidden time never becomes active even if an activity event is malformed/injected there.

### Existing timing compatibility

35. S10 `elapsed_seconds` remains unchanged.
36. Foreground and active time do not overwrite S10 enter/exit events.
37. Revisit telemetry is labelled separately and does not extend primary timing.

### Multiple tabs

38. Two tab IDs for the same observation remain separately derivable.
39. Multi tab observation is marked conflicted/indeterminate rather than summed.

### Regression

40. Clinical workflow continues if telemetry endpoint is unavailable.
41. New telemetry events contain no `name_normalized` or raw answer values.
42. All S11a through S11i tests remain green.
43. Full CI including browser tests remains green.

## Explicit non goals

S11j does not implement:

- panel viewport exposure
- AI viewed classification
- PP classification
- final simultaneous tab conflict resolution
- browser fingerprinting
- statistical attention modelling
- final Phase 2 export files

## Acceptance

S11j is complete when browser events provide a reconstructable monotonic record of visibility, focus, qualifying activity, tab identity, and client timepoint boundaries; single tab observations yield correct foreground and active durations without network latency; elapsed time remains the existing S10 wall clock measure; revisits and multiple tabs remain identifiable; and the complete test suite passes.
