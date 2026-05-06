# TODOS

Deferred items from the /autoplan review on 2026-04-21. These are OUT of scope for Phase 1 / Phase 2 / Phase 2.5 but tracked for future consideration.

## Deferred — out of Phase 1/2/2.5 scope

- **Cloud / multi-site concurrency.** Current plan is single-laptop. Revisit only if a collaborating group requests remote access (Phase 3+ territory).
- **FHIR compatibility layer.** Adapters land CSVs today. A FHIR adapter would broaden input support but adds significant complexity. Defer until an adopter asks.
- **Live AI model inference.** Explicitly out of scope in the design doc ("consumed, not produced"). Any change here would require IRB/safety reframing.
- **DICOM rendering.** Open question 6. Current plan: pre-rendered PNG + report text. Only revisit if the embedded neurologists insist DICOM is clinically essential (resolves day 1 per D5).
- **Per-institution theming beyond `theme.css` override.** One override file is enough for v1.0; deeper theming is yak-shaving until multiple adopters ask.
- **Full i18n.** English only for v1.0. Revisit if a non-English-speaking research group adopts.
- **Mobile/tablet layouts.** The study is in-person at a laptop. Mobile is not a v1 concern.

## Optional / spike-only

- **Tauri packaging spike (Phase 1α week 3, ~1 day).** Validates the "wrap-in-Tauri-later = 2 days" assumption. If it turns out to be a week, decide upfront whether to pay the tax or commit to browser-only forever. (D4)
- **uPlot fallback.** Only swap from Plotly if bundle size becomes a measured problem during pilot. (P8)

## Policy commitments (not code, but tracked)

- **Pre-registration on OSF** before Phase 2 data collection begins. (D8)
- **IRB data-handling paragraph** written before Phase 1 ships, with `clinician_name` pseudonym policy clarified. (D9)
- **SQLite backup cadence**: nightly copy to a second filesystem + CSV export after every session, from Phase 1 ship onward. (D10)
- **Competitive survey** (3-4 hours) before Phase 1α starts: catalog 6-10 closest analogues (MedAlign, HAIM, OHDSI ATLAS, Medplum, CES, etc.), document why this tool is distinct. (D3)

## From plan-eng-review (2026-05-05)

- **Wire backup cadence into S5 SQLite boot.** D10 is design-doc-load-bearing from Phase 1 ship onward; the single Phase-2 policy gate cannot retroactively cover Phase-1 sessions. S5 spec must include nightly copy + post-session CSV-export hook. Depends on: S5.
- **Re-evaluate chart-library choice for the divergence view at S6.5.** Plotnine is locked for everyday timeline panels (S6) but may be wrong for the divergence view specifically — the divergence view is the open-source adoption hook and benefits from interactive scrubbing. Candidates at S6.5 spec time: plotnine + JS scrubber overlay, or a small D3 island just for that figure. Depends on: S6 shipped, plotnine in production.

## From plan-eng-review on session-02-thin-ui-synthetic.md (2026-05-05)

- **Measure inline-SVG payload size at S8 scale before the chrome-A/B-vs-Geneva session.** Plotnine SVGs are ~10–30KB each with embedded styles; five panels per swap could push 200KB+ at Geneva density (24 timepoints). If measured, the swap shape (full-panel-swap vs per-chart streaming via `hx-swap-oob`) can be tuned before the second neurologist session. Synthetic data is too small to surface this. Revival criterion: when S8 starts and Geneva real frames are available. Depends on: S3/S7 shipped.
- **Evaluate need for a 6th panel state `stale` (data was present, then changed) once Geneva real data lands.** S2 locks the 5-state taxonomy `loading | empty-expected | empty-unexpected | partial | error` (Decision D5). Outside-voice review flagged that real Geneva data may include lab-correction cases not covered. Revival criterion: when S8 starts. If the case is real, extend the taxonomy + the panel-state detection rules in `slice_to_timepoint`. If not, document as out-of-scope and close.
- **Add a Content-Security-Policy header in S5 or S6 covering inline SVG and htmx attributes.** S2 ships HTML responses with inline SVG and `hx-*` attributes. Without a CSP, a future contributor pasting user content into a template can accidentally enable XSS. Local-only deployment makes the immediate risk low, but the open-source v1.0 release surface (per design doc) means external adopters run this in less-controlled environments. Companion to the IRB data-handling paragraph (existing TODO D9). Depends on: S5 (when the entry-point story is final).
