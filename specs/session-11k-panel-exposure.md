# Session 11k — Panel exposure telemetry and viewing episodes

## Goal

Measure viewport based exposure to each major information panel independently from clicks or other interaction.

The raw events must reconstruct cumulative qualifying duration, episodes, viewed classification, latency to first view, first/last view timestamps and open counts per clinician, patient, timepoint, panel, tab and visit kind.

S11a through S11j are assumed complete.

## Review changes (2026-09-27)

Revised against the code at `7cf9305` and the revised S11j:

1. **Episodes are derived, not reported.** The draft had the browser emit `panel.exposure_start/end` and the server re-derive them. Two sources of the same fact can disagree. The browser now reports primitives only (mount, viewport ratio, open/close); the pure derivation reconstructs episodes and their end reasons from them plus S11j state and render boundaries. "Raw events are the source of truth" becomes literal, and every threshold test is a Python test.
2. **The server applies the threshold.** Viewport events carry the browser's floating `intersection_ratio`; qualification is `ratio >= panel_viewport_threshold` from the case pinned config. The client uses the same threshold only to choose `IntersectionObserver` callback points.
3. **Collapse = the epic chrome tab.** No panel has its own collapse control. In `epic` chrome (the study default) only the selected tabpanel is expanded; a user tab change is `panel.close` of the old panel plus `panel.open` of the new one. `dense` chrome panels are always expanded and never emit open/close. Initial selection, including the sessionStorage restore after a swap, is state, not a user open.
4. **Panel set fixed** to the five `section[data-panel]` panels: `admission`, `vitals`, `labs`, `imaging`, `ai`. The summary card header and the questions drawer are not information panels. The selector must be `section[data-panel]`: the vitals `<figure>` also carries `data-panel`.
5. **`panel.mount`** records each panel's presence, initial expansion and render state at attach. It is also the AI DOM delivery evidence S11l needs, so S11l adds no browser "mounted" event.
6. **Incomplete ≠ not viewed** carried into the summary: a lower bound at or above the viewed threshold is `viewed=true` even when the tail is truncated; below it, `viewed` is `None`. PR #7 review: a `gapped` stream is no lower bound (the lost event may be the one that ended exposure), so it never proves `viewed` — `viewed` and seconds are `None`.

## Core invariants

1. A panel is exposed only while all qualifying conditions hold.
2. Exposure does not require recent activity.
3. A collapsed panel accumulates zero exposure; its header never counts.
4. The threshold applies to the panel's content element, not its header or section box.
5. Exposure is cumulative within one timepoint; separate episodes sum; the next timepoint starts from zero.
6. Durations use S11j per render monotonic time.
7. Raw events are the source of truth; summaries are reproducible pure derivations.
8. Simultaneous panel durations overlap and are never summed into attention time.
9. Primary and revisit exposure stay separate.
10. Open state alone never implies viewed.

## Configuration

S11j `telemetry.panel_viewport_threshold` (first use case 0.05) and `telemetry.panel_viewed_threshold_seconds` (2.0), same for every panel. No per panel thresholds.

## DOM contract

Each panel template wraps everything after its `<header>` in one content element:

```html
<section class="panel ..." data-panel="vitals" data-state="loading">
  <header>Vitals</header>
  <div class="panel-content" data-panel-content>…</div>
</section>
```

The observer targets `[data-panel-content]`. `#patient-view` carries `data-viewport-threshold` when telemetry is enabled.

Expanded: in `epic`, the enclosing `[role=tabpanel]` has no `hidden` attribute; in `dense`, always.

`keyboard.js` `activateTab` dispatches `ehrsim:tabchange` (`detail: {from, to, user}`); only `user: true` (a click) becomes open/close events.

A no AI render contains no AI section (S11g), so it instruments nothing for AI.

## Raw event taxonomy

Added to `EventKind`, posted through the S11j endpoint (render bound, server stamped context).

| kind | payload |
|---|---|
| `panel.mount` | `panel_id`, `expanded: bool`, `collapsible: bool`, `state` (the panel `data-state`) |
| `panel.viewport` | `panel_id`, `intersection_ratio: 0..1` |
| `panel.open` / `panel.close` | `panel_id` |

- `panel_id` ∈ `admission|vitals|labs|imaging|ai`; `state` ∈ the S2 panel states plus S11g `unavailable`.
- `panel.mount` once per panel right after `browser.timepoint_enter`.
- `panel.viewport` on every observer callback; thresholds `[0, threshold]` plus `1.0`, so events fire only on crossings (no per frame samples). The first callback after `observe()` gives the initial ratio.
- A hidden tabpanel reports ratio 0 through the observer; the preceding `panel.close` lets the derivation attribute the end to collapse.
- An AI panel event on a no AI render is accepted and stored: it is S11l leakage evidence.

## Qualifying predicate (derivation)

For a panel within one render, at every point of the render interval:

```
qualifying = mounted
         AND expanded
         AND last_ratio >= panel_viewport_threshold
         AND visible AND focused          (S11j state)
         AND inside [enter, exit]
```

The ratio is unknown (non qualifying) until the first `panel.viewport` of that panel. Floating ratios are compared as received; `0.049 < 0.05`.

## Episodes and end reasons

An episode starts when `qualifying` turns true and ends when it turns false. The end reason is the condition that turned false:

| cause | reason |
|---|---|
| ratio below threshold | `scroll_out` |
| visible false | `tab_hidden` |
| focused false | `focus_lost` |
| `panel.close` | `panel_collapsed` |
| exit `swap` | `timepoint_exit` |
| exit `pagehide` | `pagehide` |
| stream ends without exit | `truncated` (render `incomplete`) |

A state change that hides and blurs at once ends with `tab_hidden`. Zero length episodes are dropped. By construction one panel never has overlapping episodes within a render.

## Summary derivation (`panel_exposure.py`, pure)

Per render and panel: `qualifying_ms`, `episodes` (start/end mono, start/end `client_ts`, end reason), `open_count` (user `panel.open` events), `time_to_first_view_seconds` (first episode start − render enter), render status (S11j).

Per observation (clinician × patient × timepoint × panel × visit_kind), aggregating renders like S11j:

- `qualifying_seconds` (sum over same tab renders; measured lower bound)
- `viewed`: status `complete|incomplete` and `qualifying_ms >= threshold × 1000` → `True`; `complete` below it → `False`; `None` otherwise (`gapped`, `missing`, `multi_tab`, `invalid`, or truncated below threshold)
- `episode_count`, `panel_open_count`
- `time_to_first_view_seconds`: from the first primary render only; `None` if the first view happened in a later render
- `first_view_client_ts`, `last_view_client_ts` (first episode start, last episode end); `None` when the browser timestamp is missing
- `status`: `complete|incomplete|gapped|missing|multi_tab|invalid` (S11j aggregate); `mounted=False` when no render mounted the panel

Durations are computed in milliseconds from `client_mono_ms`, never from `server_ts`.

## Revisits, multiple tabs, failures

Revisit renders form their own summaries and never extend primary ones. More than one tab → `multi_tab`, per tab summaries kept, nothing summed. A tab holding a `gapped` or `invalid` render has per tab seconds `None`. Failed posts never become zero exposure: the S11j status carries into the summary.

## Files expected to change

- `_panel_*.html` (content wrapper), `_patient_view.html` (threshold attribute)
- `static/keyboard.js` (`ehrsim:tabchange`), `static/telemetry.js` (panel observers)
- `db/events.py` (kinds), `web/telemetry.py` (payload models)
- new `panel_exposure.py`
- tests, `CLAUDE.md`

## Required tests

### DOM

1. Every panel section has a stable `data-panel` id and one `[data-panel-content]` excluding the header.
2. No AI render has no AI section; AI render has one.
3. Telemetry disabled → no threshold attribute.

### Endpoint

4. Panel payloads validated (unknown panel id, ratio outside `0..1`, extra field → 422).
5. AI panel event on a no AI render is stored.

### Derivation

6. Ratio 0.049 does not qualify at 0.05; 0.050 does; ratios are not rounded.
7. Visible + focused + expanded + threshold starts an episode.
8. Scroll out ends with `scroll_out`; scroll back starts a new episode.
9. Hide → `tab_hidden`; return starts a new episode.
10. Blur → `focus_lost`; refocus starts a new episode.
11. Close → `panel_collapsed`; reopen starts a new episode.
12. Exit swap → `timepoint_exit`; pagehide → `pagehide`; no exit → `truncated` and incomplete.
13. Two 1 s episodes sum to 2 s → viewed.
14. 1.99 s not viewed; 2.00 s viewed; 2.01 s viewed with duration kept.
15. Next timepoint starts from zero.
16. Open without viewport qualification → zero exposure.
17. Passive reading keeps accumulating after active time stopped.
18. Hidden or unfocused periods contribute nothing.
19. Server receipt times never change durations.
20. Episode count, open count, time to first view, first/last client timestamps.
21. Dense (non collapsible) panels have open count 0.
22. Revisit exposure separate; multi tab not summed.
23. Truncated stream above threshold → viewed `True`; below → `None`; internal gap above threshold → `None`, no seconds.

### Browser (Playwright)

24. Epic: switching to Labs posts `panel.close admission` + `panel.open labs`; scrolling a dense panel out posts a viewport event with ratio below threshold.
25. Workflow continues when telemetry posts fail.

### Regression

26. All earlier tests green.

## Explicit non goals

Eye tracking, attention estimation by summing panels, occlusion detection (a panel covered by the questions drawer still counts as intersecting — the observer does not see overlays), PP classification (S11l), multi tab conflict resolution (S11m), exports (S11n).

## Acceptance

S11k is complete when every panel emits reconstructable mount, viewport and open/close primitives, the pure derivation yields episodes, end reasons and summaries on per render monotonic time under the pinned thresholds, passive reading counts, no AI renders produce no AI panel events, and the full suite passes.
