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
- ~~**SQLite backup cadence**: nightly copy to a second filesystem + CSV export after every session, from Phase 1 ship onward. (D10)~~ **CLOSED in S6**: shutdown-time backup wired into the lifespan + `ehr-simulator backup` CLI command, write-counter-gated to avoid dev-iteration clutter. CSV export remains S9c.
- **Competitive survey** (3-4 hours) before Phase 1α starts: catalog 6-10 closest analogues (MedAlign, HAIM, OHDSI ATLAS, Medplum, CES, etc.), document why this tool is distinct. (D3)

## From plan-eng-review (2026-05-05)

- ~~**Wire backup cadence into S6 SQLite boot.** D10 is design-doc-load-bearing from Phase 1 ship onward; the single Phase-2 policy gate cannot retroactively cover Phase-1 sessions. Reassigned from S5 → S6 per S5 spec §14.~~ **CLOSED in S6**: lifespan shutdown branch + `ehr-simulator backup` ship the per-session snapshot; nightly off-host copy stays an ops-policy concern (researcher-managed `cp data/backups/*.db /external`).
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

## From plan-eng-review on session-06-sqlite-persistence.md (2026-05-10)

- **S11: define how `arm_assignments.seed` is populated for `phase2_randomized` rows.** S6 ships the `phase1_stub` assigner (always returns `("no_ai", "phase1_stub")`, leaves `seed` NULL). Test #11 only locks "existing rows are never rewritten" — reproducibility of *new* randomized rows written by S11 is unsolved. Candidate seed strategies: (a) deterministic from `(study.seed, clinician_id, patient_id)` HMAC-style, (b) top-level `study.seed` field consumed at assign time, (c) per-row `uuid4()` (irreproducible but unique). The S11 spec must pick one and document the trade-off. Without a defined seed strategy, two researchers running the same Phase-2 pilot get diverging arm assignments. Depends on: S11 spec authoring.

- ~~**S9a: lock down `events.kind` taxonomy with a `Literal` type alias.**~~ **CLOSED in S9a commit 1**: `db/events.py` ships `EventKind = Literal[...]` + `EVENT_KINDS`; `append` raises `ValueError` on any other kind before touching the DB. S6 ships free-text `events.kind` (TEXT NOT NULL) with examples in the spec but no compile-time enforcement. S9a will add multiple producers (`panel.swap`, `answer.upsert`, `page.render-error`, etc.). Without a `EventKind = Literal["clinician.login", "clinician.logout", "panel.swap", ...]` type alias enforced in `events.append`, taxonomy drift is silent and analyses that filter by kind silently miss new variants. Cost at S9a: ~5 LOC + 1 mypy/runtime check. Revival criterion: when S9a's first new event-kind producer lands. Depends on: S9a.

- ~~**README.md persistence-paragraph update post-S6.**~~ **CLOSED**: `/document-release` rewrote the Privacy section (JSONL + SQLite + `data/backups/` + the three db_path overrides). Original note: lines 134-139 claimed "only JSONL logs hit disk" — wrong after S6 ships SQLite + backups. Owned by `/document-release` post-S6 ship per CLAUDE.md skill routing, but tracking here so it doesn't slip if document-release isn't run. Depends on: S6 shipped.

- **v1.0 README: backup time vs uvicorn graceful-timeout for large DBs.** S6 spec §13 documents that `sqlite3.Connection.backup()` is blocking and pilot-scale DBs (≤30 patients, ≤10 MB) finish well inside uvicorn's 5s default. For DBs >100 MB, researchers must invoke `serve --graceful-timeout 60` or risk SIGKILL mid-backup leaving a corrupt sidecar. Document this in v1.0 README's deployment section. Revival criterion: when a real-data pilot accumulates >50 MB of answers/events. Depends on: v1.0 README pass.

## From plan-eng-review on session-09a-answer-capture.md (2026-09-16)

- ~~**S9b prerequisite: `required: bool = True` on `_QuestionBase`.**~~ **CLOSED in S9b commit 1** (`required: StrictBool = True`). Original note: S9b's gate makes every question mandatory, including `free_notes`. Adding the defaulted field needs no `schema_version` bump but shifts every `config_hash` (`compute_config_hash_from_models` hashes the dumped model), so it must land before the first pilot answer is recorded. Depends on: S9b commit 1.
- ~~**S9b authoring rule: multi-select questions need an explicit opt-out option**~~ **CLOSED in S9b commit 1**: both shipped questions files carry `None of these`; `configs/example_questions.yaml` documents the rule; `tests/test_config.py` enforces it on both files. Original note: (`None of these`) whenever "nothing applies" is a real answer. An empty submission deletes the row and gating reads a missing row as unanswered. Document in the questions.yaml example when S9b ships.
- **S11 prerequisite: arm assignment locks on a GET.** `bootstrap_session` calls `assign_or_lookup` from the patient GET, and rows are never rewritten. Harmless under the phase-1 stub; under randomization a misclick from the index permanently burns that clinician-patient cell. S11 must defer assignment to the first POST or ship an un-assign path. Depends on: S11 spec authoring (alongside the `seed` TODO above).
- **S9c inherits the `config_hash` drift check.** S9a logs `answer.config_hash.drift` when pre-filled answers carry a different hash than the running config. The wide-pivot export has the same exposure: emit a per-generation column or refuse a mixed set. Depends on: S9c spec authoring.
- **Run `/plan-design-review` on the questions pane before the next clinician session.** Two-column sticky layout, badge styling and likert anchors are functional defaults. Ties into the DESIGN.md TODO above.
- **v1.0 prep: reflected markup in the S2 GET 404 bodies.** `routes._error_flash` interpolates `patient_id` into HTML without escaping. Defanged by the CSP and irrelevant on localhost, but route it through a template (as the POST branch does) before open-source release.
- **v1.0 prep: `PARSE_DECLTYPES` default converters are deprecated in Python 3.12** (removed in 3.14). `normalize_client_ts` keeps `events.client_ts` parseable today; replace with explicit `register_converter` or plain TEXT columns before the Python floor moves.
- **S10: `client_seq` is monotonic per tab only** (`sessionStorage`). Two tabs on one patient produce colliding sequences and nothing records a tab id. If a global order is needed, add a per-tab uuid to the event payload then.
- **Answer edit history.** Last write wins; the `answer.upsert` event stream (with `value_chars`, never the value) is the audit trail. If analyses need per-edit values, add a `payload.value` opt-in gated on the S9c pseudonymization policy.

## From plan-eng-review on session-09b-question-gating.md (2026-09-16)

- **`gate.redirect` as an `events` row.** S9b logs a GET past the frontier as a structlog WARNING only. If S10 wants "tried to peek" as a behavioral signal, promote it to an `EventKind`; today a stale tab or bookmark is indistinguishable from intent. Depends on: S10 spec authoring.
- **S8: `PatientSlice.timepoints` is dataset-derived.** `slice_to_timepoint` fills `timepoints=patient_timepoints(dataset, pid)` regardless of study mode. S9b routes the two UI consumers (`data-t-count`, summary total) through `timepoint_count = len(resolved.timepoints)` and locks that with `test_timepoint_count_uses_study_timepoints`, but the dataclass field still lies on Geneva (24 dataset timepoints vs a 3-timepoint study). Thread study timepoints into `slice_to_timepoint` or drop the field. Depends on: S8.
- **S9c: export must know about `progress`.** A wide pivot should mark (or exclude) incomplete walks; `progress.completed_at` is the flag. Also inherits the S9a `config_hash` drift check. Depends on: S9c spec authoring.
- **S10: dwell time is bounded, not exact.** Per-timepoint time-on-task = `server_ts` delta between consecutive `advance.ok` rows (`client_ts` on the same rows nets out latency). But `sessions.find_open` resumes a session indefinitely, so a clinician returning the next morning yields an eighteen-hour delta with no marker. S10 must flag deltas above a ceiling, or S10.5 adds a `session.resume` event kind. Depends on: S10 spec authoring.
- **S11 prerequisite (S9a R24, partly discharged).** `bootstrap_session` still locks the arm on the first *viewable* GET. S9b's gate decides on a pure read, so a bookmark past the frontier no longer burns a randomization cell; what remains is the S9a exposure (opening a patient at all locks the arm). S11 must still decide assignment-on-first-POST vs an admin un-assign path. Depends on: S11 spec authoring.
- **`HX-Push-Url` and browser history depth.** S9b pushes from the server on every HTMX partial (also fixes S2's Prev/`[` never updating the address bar). If a clinician's history fills with one entry per timepoint, consider `hx-replace-url` for backward moves. Cosmetic; observe in the next clinician session.
- **Confirm-before-advance dialog.** Rejected in S9b (friction on every timepoint; the CTA label only reads "Next timepoint ›" when complete). Revive only if the pilot reports mis-advances *and* `reset-progress` proves insufficient.
- **Run `/plan-design-review` on the questions pane + advance CTA + index progress markers** before the next clinician session (carried from S9a; S9b added three more visual primitives with functional-default styling).
