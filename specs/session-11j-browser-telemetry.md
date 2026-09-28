# Session 11j — Browser visibility, focus, active time and tab identity

## Goal

Create the browser telemetry foundation required to distinguish wall clock time, foreground time, and active time for each measured clinician, patient, and timepoint.

Durations use a browser monotonic clock so network latency and server clock skew do not inflate measurements.

S11a through S11i are assumed complete.

## Review changes (2026-09-27)

Revised against the code at `7cf9305`:

1. **Monotonic clocks are per document.** `performance.now()` restarts on every full page load (Start case, resume, reload, the lock pane's resume link), so values from two documents are not comparable. Every rendered `#patient-view` now carries a server issued **`render_id`**; one render lives in exactly one document, so monotonic arithmetic is only ever done within one render.
2. **The server owns the context.** A browser event names its `render_id` only. The server resolves session, patient, timepoint, `t_index` and `visit_kind` from its own `timepoint.render` row and refuses unknown or foreign renders. The browser never supplies patient, timepoint, visit kind, clinician or arm.
3. **New server event `timepoint.render`** (one per successful study render whose pinned config has `telemetry`: GET and the 200 advance). S10 `timepoint.enter`/`exit` and S11i `timepoint.revisit` are untouched. The 412 stale view stays write free (S9b contract), so it carries no `render_id` and no telemetry.
4. **Telemetry off unless pinned.** The block is optional; a case whose pinned snapshot lacks it renders no telemetry attributes and posts nothing. Preflight FAILs a Phase 2 study without `telemetry` (same precedent as `clinician_facing`); YAML loading does not.
5. **Exact activity throttling.** Leading plus trailing edge throttling of activity samples keeps the eligibility union exact (see Qualifying activity), instead of an unspecified "sensible rate limit".
6. **Only foreground activity counts**, and a periodic `browser.state` bounds tail loss after a crash to one period.
7. **Loss is explicit.** The client reports dropped events (`browser.gap`); retries are idempotent on `(render_id, tab_id, client_seq)`.
8. **Telemetry is not case contact.** The endpoint never touches `last_seen_at`, never runs the S11e lazy timeout, and accepts late events for completed or incomplete cases.
9. **Exports unchanged.** S11j delivers the pure derivation and its DB reader; CSV columns are S11n's.
11. **PR #7 review (2026-09-28):** a reported loss (`browser.gap`) or a lost enter is `gapped`, not `incomplete`: the lost event may be the hide/blur that ended foreground, so the measure is no lower bound and yields no seconds. Only tail truncation (no exit, nothing lost before the last event) stays a lower bound. The endpoint answers an unknown clinician with 401, not the login redirect (`fetch()` would follow it and read the login page as success), and `client_ts` must be an ISO timestamp with a UTC offset.
10. **Dropped** client exit reasons the browser cannot know (`advance`, `finish`, `redirect`, `interruption`): exits are `swap` or `pagehide`.

## Core invariants

1. S10 `elapsed_seconds` stays the server wall clock measure.
2. `foreground_seconds` accumulates only while the document is visible **and** focused.
3. `active_seconds` is foreground time inside the eligibility window (`inactivity_threshold_seconds`) of a qualifying activity that happened while foreground.
4. Duration arithmetic uses `client_mono_ms` within one render; `server_ts` is audit only.
5. Passive mouse movement is not activity.
6. Every telemetry event is attributable through its `render_id` to study, clinician, session, patient, timepoint, configuration version and visit kind, plus `tab_id`.
7. Telemetry is append only raw data; every derived value is rebuilt from it.
8. Missing telemetry is `missing`/`incomplete`/`gapped`, never zero seconds; a `gapped` stream yields no seconds at all.
9. Multiple tabs are observable; conflict policy is S11m's.
10. Primary and revisit telemetry are separate derivation groups.
11. Telemetry failure never blocks the clinical workflow.

## Telemetry configuration

Optional `StudyConfig.telemetry`, omitted from canonical serialization when absent (pre S11j snapshots and hashes unchanged). All S11j/S11k thresholds are defined now so S11k does not reshape the config.

```yaml
telemetry:
  inactivity_threshold_seconds: 60
  panel_viewport_threshold: 0.05
  panel_viewed_threshold_seconds: 2.0
```

Validation (all required, `extra="forbid"`):

- `inactivity_threshold_seconds`: finite, `> 0`
- `panel_viewport_threshold`: finite, `0 < x <= 1`
- `panel_viewed_threshold_seconds`: finite, `> 0`

First use case: 60 s, 0.05, 2.0 s (`configs/example_phase2_config.yaml`). The panel values are consumed from S11k.

Telemetry for a render is enabled iff study mode and the **case pinned** snapshot has the block.

## Browser tab identifier

- `crypto.randomUUID()`; fallback: 16 bytes from `crypto.getRandomValues` formatted as UUID v4.
- Stored in `sessionStorage` under `ehrsim:tab-id`; in memory when storage is blocked.
- Reused across HTMX swaps and full loads within the tab.
- Never derived from the clinician, patient, IP, user agent or fingerprinting.
- A "duplicate tab" copies `sessionStorage`, so two tabs can share an id. That collision stays detectable: both tabs continue the shared `client_seq`, producing the same `(tab_id, client_seq)` under different `render_id`s.

The `client_seq.js` counter is shared (one per tab sequence across answers, advance and telemetry).

## Render identity

`timepoint.render` (server event, `web/timing_events.record_render`):

- written after a successful study render when the pinned config has `telemetry`, on the GET path and on the 200 advance path (which renders the next pane);
- columns: `session_id`, `clinician_id`, `patient_id`, `timepoint` (minutes), `render_id`;
- payload: `{"t_index": int, "visit_kind": "primary"|"revisit"}` (`primary` ⇔ the pane is the editable frontier, S11i);
- `render_id` = `uuid4().hex`, rendered into `#patient-view` as `data-render-id` with `data-telemetry-url`.

The row is committed before the response body leaves the handler, so any event the page can post already has its render.

## Event storage migration

Migration 12 (`s11j_browser_telemetry`) adds nullable columns to `events` via `add_columns`:

```
tab_id          TEXT
render_id       TEXT
client_mono_ms  REAL
```

and indexes:

```
ix_events_render            (render_id, client_mono_ms)
ux_events_browser_delivery  UNIQUE (render_id, tab_id, client_seq) WHERE tab_id IS NOT NULL
```

Historical rows keep NULLs. `events.append()` gains optional `tab_id`, `render_id`, `client_mono_ms`; `client_mono_ms` must be finite and `>= 0` (`ValueError` before touching the DB).

## Telemetry endpoint

`POST /telemetry/events`, JSON body:

```json
{"tab_id": "<uuid>", "events": [
  {"kind": "browser.state", "render_id": "<hex>", "client_seq": 12,
   "client_mono_ms": 1532.4, "client_ts": "2026-09-27T10:00:00.000Z",   // ISO, with UTC offset
   "payload": {"visible": true, "focused": false, "reason": "blur"}}
]}
```

Server:

- clinician from the cookie; unknown → **401** (never the login redirect: `fetch()` follows it and would read the login page as success; the client also treats `response.redirected` as a refusal).
- body `<= 64 KiB`, `1..100` events, closed kinds and per kind payload models (`extra="forbid"`); any violation → **422**, nothing written.
- each `render_id` must exist as a `timepoint.render` of **this** clinician; otherwise → **409**, nothing written.
- each event inherits `session_id`, `patient_id`, `timepoint` from its render row.
- one transaction per batch; a duplicate `(render_id, tab_id, client_seq)` is skipped (idempotent retry); **204** on success.
- never touches lifecycle contact or `last_seen_at`; accepted whatever the case's lifecycle state.

sendBeacon on pagehide posts the same body (`Blob`, `application/json`; same origin, cookie sent).

## Browser event taxonomy

Closed kinds (added to `EventKind`); payloads carry no values beyond these.

| kind | payload |
|---|---|
| `browser.timepoint_enter` | `visible: bool`, `focused: bool` |
| `browser.state` | `visible`, `focused`, `reason: visibilitychange\|focus\|blur\|periodic` |
| `browser.activity` | `activity_kind: click\|touch\|scroll\|keyboard\|answer_change` |
| `browser.timepoint_exit` | `reason: swap\|pagehide` |
| `browser.gap` | `dropped: int >= 1` |

- `timepoint_enter` fires when telemetry attaches to a rendered `#patient-view` (DOMContentLoaded or its HTMX swap in). It is the full initial state snapshot and counts as the `timepoint_navigation` activity.
- `state` fires on every change of `(visible, focused)` and every `PERIODIC_STATE_MS` (15 000) while foreground.
- `timepoint_exit` fires before `#patient-view` is swapped out (`swap`) or on `pagehide`; the queue is flushed with `sendBeacon`/`keepalive`.
- `pageshow` with `persisted=true` reloads the page: the old render already exited, a restored page needs a new render.
- `gap` reports events the client discarded (queue overflow past `MAX_QUEUE`, or a send refused with 4xx).

Never recorded: key values, text, answer values, element text, coordinates, pointer trails.

## Qualifying activity

Click, touch (`pointerdown` of type touch/pen), scroll (`scroll` / `wheel`), keyboard (`keydown`), answer modification (`change`/`input` inside `#questions-pane`). Panel toggles are clicks. `mousemove` is not listened to.

Throttling: per render, the first activity in a `ACTIVITY_THROTTLE_MS` (1000) window is sent immediately (leading edge) and the last suppressed one at the window's end, stamped with its **own** monotonic time (trailing edge). Because the window is shorter than the inactivity threshold, the union of `[a, a + threshold]` over the sent samples equals the union over all activities.

## Derivation (`behavioral_timing.py`, pure)

Input: the raw events of one render (`timepoint.render` + its browser events), one threshold.

Sort by `(client_mono_ms, client_seq)`.

- **Interval**: `[enter, end]`, `end` = `timepoint_exit` or, when missing, the last event of the render (status `incomplete`).
- **Foreground**: intervals where the latest state has `visible and focused`, clipped to the interval.
- **Active**: foreground ∩ ⋃ `[a, a + threshold]` over activities `a` (enter included) that happened while foreground ∩ interval. Inactive gaps are never filled retroactively.
- **Status** per render: `complete` (enter + exit, no gap), `incomplete` (no exit: closed at the last event, a lower bound), `gapped` (`browser.gap` or no enter: an internal loss may have removed a stop transition, so no lower bound and no seconds), `missing` (render without any browser event), `invalid` (non monotonic, e.g. `client_mono_ms` decreasing along `client_seq` — flagged, not raised).

Observation (clinician × patient × timepoint × visit_kind) aggregate:

- renders of one tab are sequential: seconds sum, status = worst render status;
- more than one `tab_id`, or a duplicated `(tab_id, client_seq)` across renders → `multi_tab`, per tab values kept, no authoritative total;
- a `gapped`, `invalid` or `missing` render measures nothing, and a tab holding a `gapped` or `invalid` render has per tab value `None` (diagnostics never show a partial sum);
- durations are reported as measured lower bounds alongside the status, only for `complete` and `incomplete`; a missing render among reported ones makes the observation at least `incomplete`.

Primary values come from `visit_kind=primary` renders only; revisits never extend them.

`db/telemetry.py` holds the read only loaders (`fetch_renders`, `load_render_rows`, `load_telemetry_rows`); no module under `web/` is imported by the derivation.

## Privacy

No `name_normalized`, answer values, free text, clinical values or fingerprint attributes in any new payload.

## Failure semantics

A failed POST keeps events queued (bounded), retries on the next flush, and reports drops as `browser.gap`. The workflow never waits on telemetry. Derivation never substitutes elapsed time.

## Files expected to change

- `config/study.py` (`TelemetryConfig`), `cli_support.py` (preflight FAIL)
- `db/migrations.py` (migration 12), `db/events.py`, new `db/telemetry.py`
- new `behavioral_timing.py`
- `web/timing_events.py` (`record_render`), new `web/telemetry.py` (endpoint service), `web/routes.py`
- `_patient_view.html`, `base.html`, new `static/telemetry.js`
- `configs/example_phase2_config.yaml`, tests, `CLAUDE.md`

## Required tests

### Configuration

1. First use case block loads; absent block keeps the snapshot bytes and hash.
2. Zero/negative/non finite inactivity threshold refused.
3. Viewport threshold `0`, `> 1`, NaN refused; `1.0` accepted.
4. Zero/negative viewed threshold refused.
5. Present block changes `config_hash`.
6. Preflight FAILs Phase 2 without `telemetry`.

### Render identity and endpoint

7. A telemetry pinned GET writes one `timepoint.render` with `render_id`, `t_index`, `visit_kind`; the page carries the same `data-render-id`.
8. A pinned config without `telemetry` renders no attributes and writes no render row.
9. The 200 advance writes a render row for the next pane; 409/412 write none.
10. Revisit render has `visit_kind=revisit`.
11. Events inherit session/patient/timepoint from the render; payload cannot override them (unknown fields → 422).
12. Foreign clinician's `render_id` → 409, nothing written.
13. Unknown kind, bad payload, oversized batch, non finite / negative `client_mono_ms` → 422, nothing written.
14. Duplicate delivery is idempotent.
15. Telemetry does not touch `last_seen_at` and is accepted for a completed case.
16. Unactivated Phase 2 patient has no render and therefore no telemetry.

### Derivation

17. Visible + focused accumulates foreground; hidden or unfocused does not.
18. Hidden → visible yields two intervals that sum.
19. Network delay (server_ts) never changes durations.
20. Missing exit ends at the last event and marks `incomplete`.
21. Enter starts eligibility; activity before the threshold extends it.
22. After 60 s without activity, further foreground is inactive; a later click resumes from the click.
23. Scroll, keyboard, answer change resume active time.
24. Activity while hidden never creates active time, even after returning.
25. Throttled samples yield the same active seconds as the full stream.
26. Revisit renders never extend primary values.
27. Two tab ids → `multi_tab`, not summed; duplicated `(tab_id, client_seq)` → `multi_tab`.
28. Same tab reload: two renders sum.
29. `browser.gap` or a lost enter → `gapped` without seconds; render without events → `missing`.
29a. Unknown clinician → 401; `client_ts` without a UTC offset or unparseable → 422.
30. Decreasing monotonic time → `invalid`.

### Browser (Playwright)

31. Tab id valid and stable across swap and reload.
32. Initial enter snapshot, blur/focus and visibility transitions are posted.
33. Swap posts `timepoint_exit` for the old render before the new enter.
34. The workflow advances while `/telemetry/events` fails.
35. No payload contains answer values or `name_normalized`.

### Regression

36. S10 `elapsed_seconds` and enter/exit events unchanged; all earlier tests green.

## Explicit non goals

Panel exposure (S11k), AI viewed / PP (S11l), multi tab conflict resolution (S11m), export columns (S11n), fingerprinting, attention modelling.

## Acceptance

S11j is complete when every telemetry pinned render has a server issued identity, browser events reconstruct visibility, focus, activity and render boundaries on a per render monotonic clock, the derivation yields correct foreground and active seconds with explicit completeness status, elapsed time is unchanged, revisits and multiple tabs stay identifiable, and the full suite passes.
