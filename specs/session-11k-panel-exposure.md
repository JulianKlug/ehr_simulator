# Session 11k — Panel exposure telemetry and viewing episodes

## Goal

Measure actual viewport based exposure to each major information panel independently from clicks or other active interaction.

The raw event stream must be sufficient to reconstruct cumulative viewing duration, viewing episodes, viewed classification, latency to first view, first/last view timestamps, and panel open counts for each clinician, patient, timepoint, tab, and visit kind.

S11a through S11j are assumed complete.

## Core invariants

1. A panel counts as exposed only while all configured qualifying conditions are true.
2. Qualifying exposure does not require recent active interaction.
3. A collapsed panel accumulates zero exposure.
4. A collapsed header does not count as panel content exposure.
5. The configured viewport threshold applies to expanded panel content, not to the surrounding layout box or header alone.
6. Exposure is cumulative within one timepoint and resets at the next timepoint.
7. Separate episodes sum within the same timepoint.
8. Durations use browser monotonic timestamps from S11j.
9. Raw events remain the source of truth; summaries are reproducible derivations.
10. Multiple panels may be viewed simultaneously. Their durations may overlap and must not be summed as total attention time.
11. Primary and revisit panel exposure remain distinguishable.
12. Panel open state alone never implies viewed status.

## Configuration

Use the telemetry fields introduced in S11j:

```
telemetry:
  panel_viewport_threshold: 0.05
  panel_viewed_threshold_seconds: 2.0
```

For the first use case:

- viewport threshold = 5 percent
- cumulative viewed threshold = 2 seconds per timepoint

The same thresholds apply to all instrumented major panels.

Do not create per panel thresholds in S11k unless a later locked study requirement explicitly needs them.

## Instrumented panels

Instrument all major clinician facing information panels present in the patient view, including at least:

- admission/summary clinical information where represented as a panel
- vitals
- labs
- imaging
- AI, only when the AI panel actually exists in the AI assigned DOM

Every instrumented panel has a stable logical panel ID.

Recommended DOM contract:

```
<section data-panel-id="vitals" ...>
  <header ...>...</header>
  <div data-panel-content ...>
     ...expanded content...
  </div>
</section>
```

The `IntersectionObserver` observes `data-panel-content`, not the header.

If a current panel is always expanded and has no collapse UI, its expanded state is treated as true for the lifetime of that render. It has zero user initiated open/close events unless a user toggle exists.

## Expanded/collapsed state

For collapsible panels:

- expose an explicit DOM expanded state, for example `aria-expanded`
- content must be hidden/non qualifying when collapsed
- open/close interaction is recorded
- panel toggle also counts as qualifying activity for S11j active time

For non collapsible panels:

- expanded is always true
- do not fabricate open/close events merely because the panel rendered

## Viewport measurement

Use `IntersectionObserver` or an equivalent standards based browser API.

The configured threshold is interpreted as the fraction of expanded content area intersecting the viewport.

At the first use case threshold:

```
intersection_ratio >= 0.05
```

qualifies the viewport condition.

A measured ratio below 0.05 does not qualify.

Use the actual floating ratio reported by the browser. Do not round 4.9 percent up to 5 percent.

## Qualifying exposure state

For a panel, qualifying exposure is true only when all are true:

```
viewport_threshold_met
AND document_visible
AND window_focused
AND panel_expanded
AND current_timepoint_render_active
```

Recent activity is deliberately not part of this expression.

A clinician may read without moving/clicking and continue accumulating panel exposure even after S11j `active_seconds` has stopped due to inactivity.

## Raw event taxonomy

Extend the closed event kinds with at least:

- `panel.viewport_enter`
- `panel.viewport_exit`
- `panel.open`
- `panel.close`
- `panel.exposure_start`
- `panel.exposure_end`

All browser emitted events use S11j:

- tab ID
- client sequence
- client monotonic milliseconds
- client wall timestamp where available
- session/patient/timepoint association validated by the server
- visit kind

### Viewport events

Payload includes:

- `panel_id`
- `t_index`
- `intersection_ratio`
- configured threshold
- `visit_kind`

`viewport_enter` means the configured threshold transitioned false -> true.

`viewport_exit` means true -> false.

Do not emit high frequency ratio samples for every scroll frame. Transition events are sufficient, with current ratio retained for audit.

### Open/close events

Payload includes:

- `panel_id`
- `t_index`
- `visit_kind`

The event itself plus the event columns provide timestamps/identity.

Do not treat initial always expanded rendering as a user open.

### Exposure start/end events

The browser telemetry controller maintains the combined qualifying state and emits a transition when it changes.

Start payload includes:

- `panel_id`
- `t_index`
- `visit_kind`
- optional ratio at start

End payload includes:

- `panel_id`
- `t_index`
- `visit_kind`
- `end_reason`
- optional ratio at end

Supported end reasons include:

- `scroll_out`
- `tab_hidden`
- `focus_lost`
- `panel_collapsed`
- `timepoint_exit`
- `case_interruption`
- `pagehide`

The raw underlying S11j browser state and panel viewport/open transitions remain available, so exposure episodes can be independently audited.

## Episode state machine

Each panel starts non qualifying until the initial conditions are known.

When the combined qualifying predicate changes false -> true:

- emit `panel.exposure_start`
- remember the monotonic start locally

When it changes true -> false:

- emit `panel.exposure_end` with the appropriate reason

Never emit overlapping episodes for the same panel/tab/timepoint.

Scrolling out then back in produces two episodes.

Hiding and returning produces two episodes.

Blurring and refocusing produces two episodes.

Collapsing and reopening produces two episodes.

Advancing to a new timepoint ends every open episode before the old view is discarded.

## HTMX and full navigation lifecycle

The current app swaps `#patient-view` with HTMX on forward navigation.

Panel telemetry must attach/detach correctly for both:

- full document load
- HTMX replacement

On detach of an active timepoint:

1. end every open panel exposure episode at the current monotonic time
2. use end reason `timepoint_exit` or the more specific available reason
3. disconnect old `IntersectionObserver` instances
4. attach fresh state to the newly rendered timepoint

Counters are not carried across timepoints in JavaScript.

Raw events from the previous timepoint remain in SQLite and summaries add only events matching that timepoint.

## Pure summary derivation

Create a pure derivation module, for example:

`panel_exposure.py`

Input:

raw events grouped by:

`clinician × patient × timepoint × panel × tab × visit_kind`

Output at minimum:

- `qualifying_seconds`
- `viewed`
- `episode_count`
- `time_to_first_view_seconds`
- `first_view_client_ts`
- `last_view_client_ts`
- server receipt timestamps for audit where useful
- `panel_open_count`
- completeness/integrity flag if event pairing is broken

### Duration

Pair exposure start/end transitions using client monotonic timestamps.

```
qualifying_seconds = sum(end_mono - start_mono) / 1000
```

Do not use `server_ts` deltas for duration.

If an episode has no trustworthy end, do not extend it to a later server receipt time.

### Viewed classification

```
viewed = qualifying_seconds >= configured panel_viewed_threshold_seconds
```

For the first use case:

```
viewed = qualifying_seconds >= 2.0
```

Keep continuous duration even after viewed becomes true.

### Episode count

Count valid exposure start/end episodes. A zero length malformed pair does not count as positive exposure.

### Time to first view

Use client monotonic time:

```
first_exposure_start_mono - browser.timepoint_enter_mono
```

Do not use network receipt latency.

### First/last view timestamps

Where valid client wall timestamps exist, retain them as the clinician browser timestamp and retain server timestamps separately for audit.

If client wall timestamp is missing/unusable, timestamp summary may be null while monotonic duration remains valid.

### Open count

Count explicit user `panel.open` events.

Do not infer opens from viewport visibility.

## AI panel behaviour

S11g guarantees that no AI panel exists in no AI cases.

Therefore:

- AI assigned observation may produce normal AI panel events
- no AI observation should produce no AI panel events at all

Any AI panel/exposure event associated with a no AI observation is evidence of intervention leakage for S11l.

S11k records the raw fact and does not mutate the arm.

## Revisit exposure

Events carry `visit_kind` from S11i.

Primary summaries use `visit_kind=primary`.

Revisit summaries may be retained separately for exploratory analysis.

Do not add revisit panel duration to the original primary panel duration.

## Multiple tabs

Derive per tab panel exposure first.

Do not sum simultaneous tab exposure into one authoritative duration before S11m resolves the conflict.

If multiple tabs contribute to one observation, retain:

- per tab raw events
- per tab summaries
- a multi tab/conflict marker

## Telemetry write failures

If panel telemetry cannot be sent:

- clinical interaction continues
- no fake zero exposure is produced
- summary integrity/completeness indicates missing telemetry

Do not infer "not viewed" merely because no events arrived if the telemetry stream itself failed.

S11l must distinguish true measured non viewing from indeterminate telemetry where possible.

## Files expected to change

- panel templates to expose stable panel/content IDs
- collapse controls where applicable
- `src/ehr_simulator/db/events.py`
- new `src/ehr_simulator/panel_exposure.py`
- `src/ehr_simulator/web/static/telemetry.js` or a dedicated panel telemetry module
- `src/ehr_simulator/web/templates/base.html`
- `src/ehr_simulator/web/templates/_patient_view.html`
- panel/chrome templates
- browser/e2e tests
- derivation unit tests
- documentation

No new database table is required if the S11j `events` extension is sufficient.

## Required tests

### DOM instrumentation

1. Every major panel has a stable panel ID.
2. Observer targets expanded content rather than the header.
3. No AI panel instrumentation exists in no AI HTML.
4. AI panel instrumentation exists in AI assigned HTML.

### Viewport threshold

5. 0.049 intersection does not qualify at 0.05 threshold.
6. 0.050 qualifies.
7. Threshold crossing upward emits viewport enter.
8. Threshold crossing downward emits viewport exit.
9. Floating ratios are not rounded into qualification.

### Exposure episodes

10. Visible + focused + expanded + threshold met starts an episode.
11. Scroll out ends the episode with `scroll_out`.
12. Scroll back starts a new episode.
13. Tab hide ends the episode with `tab_hidden`.
14. Returning visible may start a new episode when other conditions remain true.
15. Focus loss ends with `focus_lost`.
16. Refocus may start a new episode.
17. Collapse ends with `panel_collapsed`.
18. Reopen may start a new episode.
19. Timepoint advance ends all open episodes.
20. Pagehide ends an episode when the event is deliverable.
21. Same panel never has overlapping episodes in one tab/timepoint.

### Cumulative derivation

22. Two one second episodes sum to two seconds.
23. 1.99 seconds is not viewed at a two second threshold.
24. 2.00 seconds is viewed.
25. 2.01 seconds remains viewed and continuous duration is retained.
26. Timepoint change resets the cumulative grouping.
27. Same panel at next timepoint starts from zero.
28. Panel open alone produces zero qualifying exposure.
29. Passive reading continues accumulating exposure even after active time becomes inactive.
30. Hidden/unfocused periods contribute zero panel duration.
31. Network latency does not affect exposure duration.
32. Unpaired/missing end does not invent duration.

### Summary measures

33. Episode count matches valid exposure episodes.
34. Time to first view is measured from client monotonic timepoint enter.
35. First/last client timestamps remain separate from server receipt timestamps.
36. Explicit open events produce the correct open count.
37. Always expanded static panel does not receive a fabricated user open count.

### Revisits and multiple tabs

38. Revisit panel events are labelled revisit.
39. Revisit duration does not extend primary duration.
40. Multiple tab summaries remain separate and are not blindly summed.

### Regression

41. Clinical workflow continues when panel telemetry POST fails.
42. Raw telemetry contains no answer/free text/clinical values.
43. All S11a through S11j tests remain green.
44. Full CI including Playwright coverage remains green.

## Explicit non goals

S11k does not implement:

- gaze tracking or eye tracking
- total attention estimation by summing panel durations
- AI PP classification
- intervention failure/leakage classification beyond recording raw facts
- final multi tab conflict resolution
- final Phase 2 exports

## Acceptance

S11k is complete when every major panel produces reconstructable viewport/open/exposure transitions, qualifying duration is based on monotonic browser time and the configured visibility/focus/expanded predicate, separate episodes accumulate correctly within a timepoint, passive reading counts, the first use case 5 percent and two second thresholds derive correctly, no AI cases generate no normal AI panel telemetry, and the complete test suite passes.
