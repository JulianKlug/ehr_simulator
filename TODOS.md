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

- **Wire backup cadence into S6 SQLite boot.** D10 is design-doc-load-bearing from Phase 1 ship onward; the single Phase-2 policy gate cannot retroactively cover Phase-1 sessions. Reassigned from S5 → S6 per S5 spec §14: "S5 does not include backup scaffolding because S5 has no SQLite layer to copy." S6 spec must include nightly copy + post-session CSV-export hook. Depends on: S6.
- **Re-evaluate chart-library choice for the divergence view at S6.5.** Plotnine is locked for everyday timeline panels (S6) but may be wrong for the divergence view specifically — the divergence view is the open-source adoption hook and benefits from interactive scrubbing. Candidates at S6.5 spec time: plotnine + JS scrubber overlay, or a small D3 island just for that figure. Depends on: S6 shipped, plotnine in production.

## From plan-design-review on session-02 round-03 feedback (2026-05-06)

- **Add DESIGN.md.** Vitals colors, BP-grouping contract, line-style rules currently live as a Python dict (`VITALS_COLORS` in `web/charts.py`) plus inline docstrings. Round-03 added two more variables and tightened the SBP/DBP shade contract — the next design ask that touches non-vitals will hit the same gap. Run `/design-consultation` to formalize. Revival criterion: any session that introduces new visual primitives outside the vitals/labs panels (e.g., divergence view, AI-output panel restyling). Depends on: nothing — can run anytime.

## From plan-eng-review on session-02-thin-ui-synthetic.md (2026-05-05)

- **Measure inline-SVG payload size at S8 scale before the chrome-A/B-vs-Geneva session.** Plotnine SVGs are ~10–30KB each with embedded styles; five panels per swap could push 200KB+ at Geneva density (24 timepoints). If measured, the swap shape (full-panel-swap vs per-chart streaming via `hx-swap-oob`) can be tuned before the second neurologist session. Synthetic data is too small to surface this. Revival criterion: when S8 starts and Geneva real frames are available. Depends on: S3/S7 shipped.
- **Evaluate need for a 6th panel state `stale` (data was present, then changed) once Geneva real data lands.** S2 locks the 5-state taxonomy `loading | empty-expected | empty-unexpected | partial | error` (Decision D5). Outside-voice review flagged that real Geneva data may include lab-correction cases not covered. Revival criterion: when S8 starts. If the case is real, extend the taxonomy + the panel-state detection rules in `slice_to_timepoint`. If not, document as out-of-scope and close.
- ~~**Add a Content-Security-Policy header in S5 or S6 covering inline SVG and htmx attributes.**~~ **CLOSED in S5 commit 3.** Per /plan-eng-review issue 2.1 on the S5 spec, `script-src` is `'self'`-only (zero inline `<script>` and zero `on*=` event handlers across `web/templates/`, verified). Only `style-src 'unsafe-inline'` remains for plotnine's inline `<style>` blocks inside SVG. Tighter `style-src` via hashed external stylesheet is a v1.0-prep follow-up tied to the S8 inline-SVG payload measurement.

## From plan-eng-review on session-04-mimic-adapter.md (2026-05-07)

- ~~**Add a Geneva-side integration test asserting `_read_features_csv` defensive issue-emission catches unrecognized source values in the real Geneva fixture.**~~ **CLOSED in S5 commit 4** as `test_geneva.py::test_read_features_csv_emits_issue_for_unrecognized_source_geneva_fixture`, layered on top of the S5 structlog WARNING infrastructure.

## From plan-eng-review on session-03-geneva-adapter.md (2026-05-06)

- **Vectorize `_decode_categorical` via `groupby + idxmax` if S8 smoke measures >30s on the real Geneva CSV.** S3 ships a per-(patient, group) loop that filters pandas frames inline (~50K outer iterations × ~50-row scans = ~25s estimated on the 19.7M-row real CSV). Spec §15 explicitly defers performance to S8; S3's codified `@pytest.mark.real_data` smoke test (added by /plan-eng-review issue 1.3) will measure the actual hit. Replacement: groupby `(patient_id, group_name)` + `idxmax` to compute per-group winners in a single vectorized pass, then iterate the result table (~25 LOC, ~25× speedup). Revival criterion: smoke test wall time >30s OR S8 UI loading feels slow. Depends on: S3 shipped, smoke run on real data, S8 starting.
- ~~**Emit a structlog WARNING event when `_decode_categorical` falls back to argmax in lenient mode.**~~ **CLOSED in S5 commit 4.** `_shared.py` now imports structlog and emits `event_kind=ingest.categorical.argmax_fallback` (alongside the existing `IngestionIssue`) on every argmax fallback; same WARNING infrastructure also closes the `_read_features_csv` unrecognized-source TODO above (`event_kind=ingest.source.unrecognized`).

## From plan-eng-review on session-05-config-and-cli.md (2026-05-07)

- **S8: SLA test on Geneva preflight wall time.** With `slice_to_timepoint` generalized via the `DatasetLike` Protocol (per /plan-eng-review tension B on S5), `preflight` can now compile against Geneva real data. S8 should add a `@pytest.mark.real_data` smoke asserting preflight on a 30-patient × 12-timepoint Geneva pilot subset stays under N seconds. Naive O(P×T×|scalar_ts|) is bounded for pilot subsets but worth measuring before the first real-data session. Depends on: S5 shipped, S8 starting.
- **S11: migrate `config_hash` callers if `schema_version: "2"` changes the canonical JSON shape.** Per /plan-eng-review tension E on S5, when v2 lands, pilot data committed under v1 carries `config_hash` rows whose canonical JSON may need a remap. The S11 spec must document whether v1's `config_hash` values stay valid across the upgrade or whether a `config_hash_v2` column gets added. Depends on: S11 spec authoring.
- **CSP `style-src` further tightening at v1.0.** S5 ships `style-src 'self' 'unsafe-inline'` because plotnine emits inline `<style>` blocks inside SVG output. Tighten via hashed external stylesheet at SVG-render time as a v1.0-prep task. Tied to the S8 inline-SVG payload measurement (existing TODO above). Revival criterion: when S8 lands and the inline-SVG measurement informs whether per-chart streaming via `hx-swap-oob` is required. Depends on: S8.

## From real-data dogfooding on session-05 (2026-05-09)

Surfaced when the user pointed `serve --config` at the real Geneva CSV for the first time. S5 made the previously-silent gaps loud; these are the follow-ups.

- **S8: render Geneva min/median/max vital aggregates as a band, not just the median.** S5's tier-2 fix renames `median_heart_rate → hr`, `median_systolic_blood_pressure → sbp`, etc. so the vitals panel renders something. But Geneva ships `min_*` and `max_*` per hour bucket too — clinically meaningful (a max BP of 200 vs median 160 vs min 130 changes a stroke decision) and currently dropped on the floor. The proper fix: extend the vitals chart renderer to draw a per-variable band (min..max envelope, median line) when the dataset provides aggregates. Touches `web/charts.py::render_timeline_svg` + `web/panels.py::_VITAL_VARS` (recognize `min_*`/`max_*` as same variable for grouping). Depends on: S8 starting (real-data UI session).

- **S8: add a "neuro/support" panel for NIHSS, GCS, FIO2 (and similar stroke-clinical-essential variables).** S5's vitals filter (`hr/sbp/dbp/rr/spo2/temp`) is the synthetic-era baseline; Geneva ships clinically essential variables that don't fit any current panel: `min/median/max_NIHSS` (stroke severity score), `Glasgow Coma Scale`, `FIO2` (oxygen support level). Without somewhere to render them, they're invisible to the clinician. Two options: (a) extend the vitals panel filter to include these (cluttered); (b) add a new sixth panel "neurological / support" alongside vitals, labs, admission, imaging, ai. Option (b) cleaner; raise with clinicians at the next design review. Depends on: S8 + design input.

- **`patient_ids_file` study config field — load patient list from an external file.** A real pilot may walk 30-100 patients; inlining `case_admission_id`-shaped strings in `study.yaml` becomes unwieldy and merge-conflict-prone. Add an optional `patient_ids_file: Path | None` field to `StudyConfig` that points at a plain-text file, one patient_id per line (blank lines + `#` comments allowed). Mutually exclusive with the inline `patient_ids` list — exactly one must be set. Path resolution: same convention as `csv_path`/`params_dir` (relative to YAML dir, sandboxed by `EHR_SIM_DATA_ROOT`). Validation rules (non-empty, deduped) move from the field validator to `model_validator(mode="after")` so the file-loaded list is checked too. Cost: ~30 LOC + 3 tests + an `examples/example_patient_ids.txt` fixture. Depends on: nothing — could land any session, low-risk.
