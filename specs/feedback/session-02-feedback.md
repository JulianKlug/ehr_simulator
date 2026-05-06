# Feedback on session 02
## Round 01
- epic view is better because tabs for different components
- vitals should all have the same x axis, with different y axes
- raw values of vitals should be accessible/visible to the user (hoover + add tabular view underneath)
- labs should be in tabular form
- navigation between timepoints and patients is not straightforward
- admission tab should be the first tab

## Round 02
- vitals plot is not readable at all
- vitals plot does not plot anything when only one timestep is present
- change of timestep should not change tab (example: when timestep is changed, one should not be switched from the labs to the admission tab)

## Round 03
- vitals: all blood pressure data should be grouped together (both in graph and in tabular, and have a similar color)
- vitals: let some vitals share the same plot with separate y scales (same x axis should be maintained for all plots)
-- blood pressure, heart rate, resp rate should be on the same plot
-- spo2 and temperature should be on the same plot (below)
- vitals: the x-axis should use more space on the page

## Round 03 — design decisions (from /plan-design-review on 2026-05-06)

The clinician feedback above is **what**. This section is **how** — the design decisions
the implementer needs before touching code. Approved mockup: variant D (HTML-stitched
groups), at `~/.gstack/projects/JulianKlug-ehr_simulator/designs/round-03-vitals-upper-20260506/variant-D-stitched.png`.

### Layout

- **Two figures, both using HTML-stitched groups:**
  - **Upper figure** (hemodynamics): `BP` panel → `HR` panel → `RR` panel, top to bottom.
  - **Lower figure** (oxygenation/metabolic): `SpO₂` panel → `Temp` panel, top to bottom.
- Each panel is a separate plotnine SVG. Group labels render in HTML/CSS (extends the
  existing FINDING-007 pattern from variable-level to group-level — bypasses the
  plotnine facet strip-text bug).
- All panels in a figure share the **same x-axis range** (pinned across SVGs the way
  the current per-variable code pins `x_range`). Time axis labels render only on the
  bottom-most panel; upper panels suppress x-tick labels but keep the gridlines.
- **Drop the per-row label column** (currently 5.5rem CSS grid column in
  `.vitals-row`). Panel labels move INSIDE the chart (matplotlib y-axis title or
  HTML `<figcaption>` above each SVG). Charts span the full panel width.
- Default chart width: matplotlib `figsize=(10.0, ...)`. No 12in bump unless feedback
  comes back wanting more.
- **Remove top and right spines** on every panel. Flat axes (only bottom + left lines).
  No box around panels, no rounded card corners on the chart container.

### BP grouping (the headline change)

- **Chart side:** SBP and DBP overlay on a single shared y-scale (mmHg). Same hue,
  different lightness:
  - SBP: `#1f6feb` (existing — saturated blue, solid line, circle markers).
  - DBP: bump from `#2c7be5` → `#7aa7ef` (lighter blue, solid line, circle markers).
  - No legend chrome on the BP panel — SBP is always the higher line, DBP the lower;
    the shared "BP (mmHg)" y-axis title plus position is enough.
- **Table side:** Add `<colgroup>` + a thin "BP" superhead row above the column headers.
  SBP and DBP cells get a 1px subtle background tint to mark the column group. Other
  vitals (HR, RR, SpO₂, Temp) stay flat single-column headers.
- **Column order in the values table:** `t | SBP | DBP | HR | RR | SpO₂ | Temp` — matches
  the panel reading order top-to-bottom across both figures.

### RR (resp rate)

- **Add `rr` to the data model:** synthetic dataset only. Append `("rr", "breaths/min", 12.0, 20.0)`
  to `_VITALS` in `synthetic.py`; add `"rr"` to `_VITAL_VARS` in `panels.py`; add
  `"rr": "#8e44ad"` (purple) to `VITALS_COLORS` in `charts.py`. **No canonical schema
  change** — `scalar_ts` already accepts arbitrary `variable` names; pandera contract
  unaffected. (`_VITAL_VARS` is panel-routing config, not a contract.)
- Geneva and MIMIC adapters will populate RR from real data when those sessions land.

### State coverage (per-panel, both figures)

| State | Behavior |
|-------|----------|
| `loading` | All panels render axes only, no marks. Caption above figure: "Loading…" |
| `empty-expected` | Existing copy: "No vitals recorded for this patient." |
| `empty-unexpected` | Existing copy: "No vitals at this timepoint yet (data exists later)." |
| `partial` | Existing badge above figure: "Partial data at this timepoint." Per-panel behavior below. |
| `error` | Per-panel error containment (FINDING-009 unchanged). |

**Partial-state behavior on the BP-grouped panel** (new, specific to round-03):
when only one of SBP/DBP has arrived at the current timepoint, render the present
line solid + a faint dashed grey "expected band" at the missing variable's reference
range (60–95 mmHg for DBP, 110–160 mmHg for SBP — same ranges as the synthetic
generator). Annotation in the panel: "DBP missing at this timepoint." Clinically
honest: an isolated SBP read without DBP context is misleading.

For HR / RR / SpO₂ / Temp panels (single-variable), partial state behaves as today.

### A11y / responsive

- a11y fallback table: keep the existing wide-format pivot. Mirror the new BP `<colgroup>`
  in the visible pivot table.
- Each panel SVG: stamp `data-panel="vitals"` (existing) plus new `data-group="bp"|"hr"|"rr"|"spo2"|"temp"`.
- WCAG: SBP `#1f6feb` and DBP `#7aa7ef` against white both pass AA for graphical
  objects (≥3:1) at the line widths used. RR `#8e44ad` likewise.
- **Narrow viewports (<768px):** `min-width: 720px` on `.vitals-stack`; container scrolls
  horizontally on phones. Time axis stays continuous. Clinical use is desktop-first; the
  "no mobile" TODO already covers Phase 1.

### Slop rails (do NOT add during implementation)

- No emoji in panel labels (no 💓, no 🌡️).
- No rounded card corners on the chart container.
- No legend chrome on the BP panel (or any single-color panel).
- No gradients, no decorative blobs, no drop shadows.
- No purple/violet section backgrounds (RR's purple is line-color only).

### Rendering library

Stay on **plotnine + HTML stitching**. Implementation pattern (mirrors current FINDING-007 fix):
1. For the BP panel: one plotnine call with two `geom_line` (SBP, DBP) + shared y-axis.
2. For HR / RR / SpO₂ / Temp: one plotnine call each, single line.
3. Each call returns an SVG. The Jinja template (`_panel_vitals.html`) stacks the SVGs
   inside a `<figure>` with HTML strip labels above each SVG and a shared x-range
   pinned by the renderer (same `x_range` plumbing as the current per-variable code).

The matplotlib gridspec render in the approved mockup was just a faster way to
prototype the layout — the live UI uses plotnine SVGs because labs and other panels
already do.

### Mockups

| Screen/Section | Mockup Path | Direction |
|----------------|-------------|-----------|
| Vitals upper plot (BP+HR+RR) | `~/.gstack/projects/JulianKlug-ehr_simulator/designs/round-03-vitals-upper-20260506/variant-D-stitched.png` | Approved variant D — three stacked panels, BP grouped on shared mmHg, HR + RR own panels, shared x-axis, top/right spines removed. Lower plot (SpO₂+Temp) follows same pattern. |

### NOT in scope for round-03

- **Lower plot color contract review.** SpO₂ stays `#0e8a3a`, Temp stays `#a85d00`. They're
  already on different y-scales (% vs degC), naturally distinct. No grouping needed.
- **Hover-tooltip data on chart.** Round-01 covered this with the values table; round-03
  doesn't change that.
- **Plotnine facet strip-text bug fix.** Round-03 sidesteps it by stitching SVGs in HTML.
  Fixing plotnine itself is unbounded-cost work (already burned a session on FINDING-007).
- **Mobile-first redesign.** Phase 1 is desktop-only per existing TODOS.md; horizontal
  scroll on narrow viewports is the accepted compromise.
- **DESIGN.md.** Deferred to TODOS.md — colors-as-code stays for now.
- **Real RR data from Geneva/MIMIC.** Synthetic-only for round-03; adapters will populate
  later.

### What already exists (reuse, don't reinvent)

- `render_timeline_svg` (`web/charts.py:44`) — single-variable renderer. Round-03 keeps
  it for HR / RR / SpO₂ / Temp (single-line panels). Only the BP panel needs a new
  multi-line variant.
- `VITALS_COLORS` (`web/charts.py:34`) — color map. Add `"rr"`, bump `"dbp"` shade.
- `_VITAL_VARS` (`web/panels.py:26`) — panel-routing frozenset. Add `"rr"`.
- `slice_to_timepoint` panel-state machinery — unchanged. Round-03 only changes
  rendering, not state detection.
- A11y fallback table in `_panel_vitals.html:49-56` — unchanged data shape; the visible
  pivot table changes (BP colgroup), the long-format fallback does not.
- `x_range` plumbing in `routes.py:202` — already pins the time axis across per-variable
  charts; round-03 reuses this for per-group charts.
- The existing partial-state badge above the figure (`_panel_vitals.html:14`) is the
  panel-level partial signal; the new BP-panel-internal "DBP missing" annotation is
  additive, not a replacement.

## Completion Summary

```
+====================================================================+
|         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
+====================================================================+
| System Audit         | No DESIGN.md (TODO logged); UI scope = vitals card |
| Step 0               | Initial 3/10. Mockups via real plotnine renders   |
| Pass 1  (Info Arch)  | 5/10 → 9/10                                       |
| Pass 2  (States)     | 3/10 → 9/10                                       |
| Pass 3  (Journey)    | 5/10 → 8/10                                       |
| Pass 4  (AI Slop)    | 8/10 → 9/10                                       |
| Pass 5  (Design Sys) | 3/10 → 8/10                                       |
| Pass 6  (Responsive) | 2/10 → 8/10                                       |
| Pass 7  (Decisions)  | 7 resolved, 0 deferred                            |
+--------------------------------------------------------------------+
| NOT in scope         | written (6 items)                                  |
| What already exists  | written (7 components)                             |
| TODOS.md updates     | 1 added (DESIGN.md)                                |
| Approved Mockups     | 4 generated, 1 approved (variant D)                |
| Decisions made       | 11 added to feedback file                          |
| Decisions deferred   | 0                                                  |
| Overall design score | 3/10 → 8.5/10                                      |
+====================================================================+
```

Plan is design-complete. Run `/plan-eng-review` next — round-03 introduces (a) a new
multi-line plotnine call (BP-grouped), (b) RR data-model extension, (c) `<colgroup>`
table restructure, (d) state-machine extension for partial-within-group BP rendering.
All four warrant architecture review before code.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open (mode: SELECTIVE_EXPANSION, 2 critical gaps) — stale (2026-04-21, before round-03) | 1 unresolved |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 2 | clean (PLAN, 2026-05-05, last on session-02 spec) | 18 issues, 0 critical |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | clean (FULL, 2026-05-06, this run) | score: 3/10 → 8/10, 11 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | clean — stale (2026-04-21, before round-03) | score: 4/10 → 7/10 |

- **UNRESOLVED:** 0 from this design review.
- **VERDICT:** ENG (PLAN) + DESIGN CLEARED for round-03 implementation. Recommend re-running `/plan-eng-review` before code lands — round-03 introduces a multi-line plotnine call, RR data-model extension, table colgroup restructure, and a partial-within-group state. The 2026-05-05 eng review pre-dates round-03.

