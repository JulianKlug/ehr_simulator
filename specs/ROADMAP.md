# Implementation Roadmap

Written 2026-05-05, after Session 1 shipped. Revised 2026-05-05 after `/plan-eng-review`.
Defines the order of session specs to write and execute. Each session is a 1-2 day unit with its own `session-NN-name.md` spec written immediately before implementation, structured like `session-01-data-contract.md`.

## Why this order

Thin UI on synthetic in week 1, then Geneva, then MIMIC, then real-data UI. Four reasons:

1. **Chrome A/B in week 1 is a design-doc commitment.** The design doc's premise P1 validation plan and Phase 1α success criteria require the first neurologist session within ~week 1 of build. Pushing UI to week 5+ violates that. Synthetic-data-on-thin-UI is enough to answer "dense layout vs Epic-style chrome" — the question doesn't need real Geneva density, it needs a real neurologist clicking through.
2. **Geneva is the home dataset.** It's the primary deployment target at the Geneva stroke unit. Building its adapter early means the contract gets validated against real production data before downstream sessions land.
3. **MIMIC becomes a generalization check, not a gamble.** Once Geneva works, MIMIC's adapter becomes "does the contract survive a second dataset?" rather than "will this even work?" Pushed-back risk is cheaper risk.
4. **Real-data UI deepens what synthetic can't show.** Synthetic answers chrome questions; real Geneva answers information-density and interaction questions. Both layers ship.

## Working agreement

Each session's spec gets written immediately before its implementation, not all at once up front. The roadmap below pins ordering and scope; per-session specs flesh out file lists, schemas, **test inventories**, and acceptance criteria with full context.

**Test budget convention (from /plan-eng-review):** every per-session spec ships a numbered test inventory broken down by category (unit / integration / regression / E2E) with a target count. S1's spec is the reference shape. Smoke-only sessions are not acceptable.

If a session's scope balloons mid-implementation, stop and split. The Session 1 ratio (1 spec ≈ 1 day, ~4 commits) is the target.

After each session ships: `/review`, then `/ship`, then `/land-and-deploy`.

## Sessions

### Session 1 — Data contract + repo scaffolding [SHIPPED]

Status: shipped 2026-04-21 (commits `dbf0b9d` .. `afd73f6` + `8d49d17` post-review fixes).
Spec: `session-01-data-contract.md`.
Locked: 4 pandera schemas, synthetic adapter, 15 tests, CI on Python 3.11/3.12.

### Session 2 — Thin UI on synthetic + structlog [NEXT]

Spec: `session-02-thin-ui-synthetic.md`.

Goal: a clinician can open `localhost:8000`, pick `synth_001`, scrub through three timepoints, and see vitals + admission + AI panels rendered against `load_synthetic()`. No real data, no answer capture, no SQLite.

Locks in:
- FastAPI single-route MVP (`/patient/<id>/timepoint/<t>`) returning an HTMX partial.
- **Plotnine server-side rendering** of timeline charts (returns SVG, swapped via HTMX). Zero client-side chart JS.
- `structlog` + `logs/<date>.jsonl` pipeline. Mandatory fields: `request_id`, `clinician_id` (None until S5), `patient_id`, `timepoint`, `event_kind`. Logger boots at app boot, per-request context bound via middleware.
- Five UI states per panel: `loading`, `empty-expected`, `empty-unexpected`, `partial`, `error`.
- Timepoint Summary Card always visible above the tab set.
- `[` `]` keyboard shortcuts for timepoint navigation.
- `theme.css` override hook.
- Accessibility baseline: keyboard map, WCAG AA contrast, visible focus rings, a11y-data-table fallback per chart.

**Acceptance:** chrome A/B validation session with the first embedded neurologist runs against this build (dense layout vs Epic-style chrome on synth_001). Findings recorded.

**Test inventory:** ≥6 tests. ≥2 unit (structlog mandatory fields, plotnine renderer returns SVG). ≥2 integration (panel HTMX swap, keyboard shortcuts). ≥1 [→E2E] (one synthetic patient end-to-end render). ≥1 a11y assertion.

### Session 3 — Geneva adapter

Goal: `load_geneva(csv_path, params_path)` returns a dataset that conforms to all four canonical schemas, exercising every adapter responsibility documented in Session 1.

Locks in:
- Source-column routing on real data (`EHR` → SCALAR_TS, `stroke_registry` → ADMISSION, `*_imputed` → drop via substring match). Imaging-derived scalars (`cbf_lt_30`, `tmax_gt_6`) route through `EHR` → SCALAR_TS; the canonical `IMAGING` shape stays empty for Geneva by design.
- Hour-bucket → minutes conversion (`t_minutes = hour_cat * 60.0`).
- Inverse normalization via `logs_30012026_154047/normalisation_parameters.csv`.
- Categorical-variable handling: one-hots and binaries are z-scored too; resolved via `categorical_variable_encoding.csv` (threshold ≥0.5 or re-expand the group). The 0.5-edge case is a [GAP] critical failure mode — see acceptance.
- `EHR_SIM_DATA_ROOT` env var + path-traversal guard (Session 1 deferred D35).
- Empty IMAGING and AI_OUTPUT frames.
- **`scripts/gen_data_contract.py`** that reads `canonical.py` docstrings and writes `docs/data-contract.md`. CI step `python scripts/gen_data_contract.py --check` fails if the on-disk file drifts (autoplan D50).

Helpers declared at module scope inside `geneva.py` (will be lifted to `_shared.py` in S4 only if MIMIC genuinely needs the same shape):
- `_drop_imputed(frame)` — substring drop on `source`.
- `_inverse_normalize(z, mean, std)` — continuous inversion.
- `_decode_categorical(value, encoding_map)` — threshold ≥0.5 + group re-expansion.
- `_path_traversal_guard(path, root)` — `Path.resolve().is_relative_to(root)`.

**Acceptance:** adapter loads the real CSV at the path in `.EXAMPLE_DATA_PATHS`, every frame passes `validate(strict=True)`, end-to-end test on a checked-in fixture row sample. `docs/data-contract.md` regenerates without drift.

**Test inventory:** ≥11 tests. 5+ unit (`_drop_imputed`, `_inverse_normalize`, `_decode_categorical` including the 0.5-edge case, hour-bucket → minutes, `_path_traversal_guard`). 3+ integration (source-routing branches, strict vs lenient on real-shaped data, empty IMAGING/AI_OUTPUT frame assertions). 2+ E2E (full Geneva fixture round-trip; `docs/data-contract.md` drift CI check). 1+ regression (`AdapterError` on missing required cols).

**Fixture strategy:** `tests/fixtures/build_geneva_fixture.py` reads the real CSV, samples N rows per source category, replaces `value`/identifiers with random-but-realistic fakes, writes `tests/fixtures/geneva_sample.csv`. Both committed. Re-run when source schema changes.

### Session 4 — MIMIC-III adapter

Goal: prove the canonical contract generalizes. MIMIC mirrors Geneva's structure; `notes` replaces `stroke_registry`, source vocabulary differs, normalization params live at a different path. Everything else should compose.

Refactor: lift the four helpers declared in S3 to `_shared.py` only if MIMIC's signatures match exactly. If signatures diverge, fork and document why. The list is a hypothesis, not a contract.

**Acceptance:** same shape as Session 3 against the MIMIC CSV path. Both adapters test green in parallel.

**Test inventory:** ≥10 tests. Mirror of Session 3 + 1 regression test asserting that lifted `_shared.py` helpers produce identical output for both datasets given equivalent inputs.

### Session 5 — Study config + CLI

Goal: nothing runs end-to-end against real data until the simulator knows which patients, timepoints, and questions to use. This session adds Pydantic-validated YAML configs and a Typer CLI surface.

Scope:
- `study_config.yaml`: patient ids (ordered), timepoints (relative to t=0), time unit (minutes/hours), dataset selector.
- `questions.yaml`: question id, prompt text, response type. 5 canonical question primitives: `likert`, `categorical`, `multi-select`, `probability-0-100`, `free-text`.
- Schema-version field on both: `schema_version: "1"` (Session 1 deferred D6).
- CLI commands: `serve`, `validate-config`, `validate-adapter`, `preflight`, `preview`. `export-answers` slips to S9c.
- `preflight <study_config.yaml>` walks every patient/timepoint headlessly before a pilot — catches missing data, schema drift, empty timepoints. Without this, the first failure mode is a clinician staring at a broken UI mid-session.

**Test inventory:** ≥8 tests. 1 CliRunner test per command (5 commands → 5 tests). ≥2 unit (Pydantic config rejects bad shape; `schema_version` mismatch raises). ≥1 [→E2E] (`preflight` walks a synthetic patient set end-to-end, fails on a deliberately broken config).

### Session 6 — SQLite persistence + full schema

Goal: durable response store + full data model from the design doc.

Scope (reconciled to design-doc names — note: not `responses`, not `clinician_name`, not `ai_visible_flag`):
- `clinicians(clinician_id, name_normalized, first_seen_at)` — clinician name lookup case-folded + trimmed; FK target.
- `sessions(session_id, clinician_id, patient_id, started_at, ended_at, arm, config_hash)` — multiple rows per (clinician, patient) for resume.
- `arm_assignments(clinician_id, patient_id, arm, arm_source, seed, assigned_at, config_hash)` — `arm_source ∈ {phase1_stub, phase2_randomized}`.
- `answers(clinician_id, patient_id, timepoint, question_id, value, arm, config_hash, ts_recorded)` — long format, **unique on `(clinician_id, patient_id, timepoint, question_id)` for upsert on double-submit**.
- `events(event_id, session_id, clinician_id, patient_id, timepoint, kind, payload_json, client_ts, server_ts, client_seq)` — append-only behavioral signals.
- `ingestion_issues(dataset, patient_id, row_idx, reason, loaded_at)` — tiered AdapterError aggregation (Session 1 D27).
- `schema_migrations(version, applied_at)` + 30-line runner at app startup.
- `config_hash` columns on `answers` and `events` — SHA256 of `study_config.yaml + questions.yaml`.

**SQLite tuning at boot:** `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL`.

**Named indexes:** `events(session_id)`, `events(patient_id, timepoint)`, `answers(patient_id, clinician_id)`, `arm_assignments(clinician_id, patient_id)`.

**Backup hook (per TODOS.md, /plan-eng-review residual):** wire nightly copy + post-session CSV export into S6 boot so D10 backup cadence is active from Phase 1 ship onward, not deferred to the Phase-2 policy gate.

**Test inventory:** ≥10 tests. 7 unit (one per table CRUD, plus migrations runner forward + idempotent). ≥2 integration (WAL + synchronous PRAGMAs verified at boot; config_hash round-trip). 1 **[REGRESSION]** test: unique-constraint upsert on `answers(clinician_id, patient_id, timepoint, question_id)` — network-retry double-submit must produce 1 row, not 2. Backup-hook smoke test.

### Session 7 — Geneva AI predictions adapter

Goal: load Geneva test-subset AI output into the canonical `AI_OUTPUT` shape so the AI panel renders real content from S8 onward.

Scope:
- `load_geneva_ai_predictions(pkl_path, shap_dir) -> pd.DataFrame` conforming to `AI_OUTPUT_SCHEMA`.
- Source paths from `.EXAMPLE_DATA_PATHS`: `test_predictions.pkl` + `shap_explanations_over_time/`.
- Test-set patient-id intersection (predictions cover only the test set; non-test patients return empty AI_OUTPUT, no raise).
- SHAP-per-timepoint serialization into `output_json`. NumPy float32 must cast cleanly to JSON.

**Test inventory:** ≥6 tests. 4+ unit (pickle loader shape assertion, SHAP serialization, NumPy float32 cast, test-set patient-id intersection edge cases including the silent-empty failure mode). 1 integration (AI_OUTPUT validates strict). 1 regression (patient-id format mismatch is detected, not silent).

### Session 8 — Real-data UI on Geneva (read-only)

Goal: the thin UI from Session 2 now renders Geneva data + AI panel from S7.

Scope:
- Switch dataset selector from `synth` to `geneva`.
- Validate-once-cache strategy: load + validate Geneva on FastAPI boot, cache parsed frames in `app.state.dataset`. Per-request slicing is filtered pandas, no re-validation.
- Reveals data up to and including current t only — **the central architectural constraint**.
- Plotnine renders against real Geneva timepoint counts (24-72 per patient typical).
- Perf budget: TTI < 2s on baseline laptop with 5 panels at 24 timepoints. Measured with Chrome devtools perf trace, recorded in spec acceptance.
- AI panel renders `output_json` via a per-model template.

**Test inventory:** ≥7 tests. 3+ unit (plotnine renderer, dataset cache, AI panel template). 2+ integration (5 UI states per panel, validate-once-cache). 1 **[REGRESSION]** test: **data ≤ t only** — request `t=N` returns no rows with `t_minutes > N`. The data leak failure mode invalidates study validity; this test is non-negotiable. 1 [→E2E] (full Geneva patient walk).

**Acceptance:** clinician can navigate a real Geneva patient end-to-end on a laptop in-person.

### Session 9 — Question gating + answer capture + CSV export (split)

Originally one session; split per /plan-eng-review (working agreement: scope balloons → split).

#### Session 9a — Answer capture

Scope:
- POST `/answer` upserts to `answers` table (uses unique constraint from S6).
- Auto-save on blur emits an `event` row.
- `clinicians` lookup-or-create on first POST.
- `config_hash` captured per row.

**Test inventory:** ≥5 tests. POST upserts; auto-save-on-blur emits event; unique-constraint upsert (regression carried from S6); config_hash captured.

#### Session 9b — Question gating

Scope:
- `/advance` endpoint with optimistic `expected_timepoint` query param.
- All-questions-answered requirement enforced server-side; UI button disabled-unless-complete.
- Click-when-disabled scrolls to first unanswered + emits an event.
- Advance CTA label includes remaining-count.

**Test inventory:** ≥5 tests. `/advance` rejects mismatched `expected_timepoint`; cannot advance with unanswered question; click-when-disabled emits event; remaining-count label correctness.

#### Session 9c — CSV export

Scope:
- `ehr-simulator export-answers <study_config.yaml>`: wide pivot, one row per `(patient_id, clinician_id)`, columns `{question_id}_t{timepoint}` plus `arm`.
- Multi-select answers pipe-delimited; UTF-8; header row first.
- **Cell-injection guard:** any cell whose first character is in `{=, +, -, @, \t, \r}` is prefixed with `'`. **[REGRESSION]** test required.
- Clinician-id pseudonymization: exports use `clinician_id`, raw `clinician_name` stays in an on-disk keyfile (gitignored).

**Test inventory:** ≥6 tests. Wide-pivot shape; CSV-injection guard (regression); pseudonymization; round-trip import; multi-select pipe encoding; UTF-8 header.

### Session 10 — Rough divergence view (Phase 1 dogfood)

Goal: validate the figure's information architecture before real data is collected — design doc P6 update from autoplan.

Scope:
- Plotly figure or plotnine SVG querying `events` + `answers` + `arm_assignments`.
- Synthetic + pilot data input.
- Per-patient timeline showing AI vs no-AI answer differences over time, annotated with what data became visible at each timepoint.

**Test inventory:** ≥3 tests. Query shape; figure renders against synthetic; dogfood smoke test on pilot data.

**Note (TODOS.md):** chart library may be re-evaluated for the divergence view specifically (plotnine + JS scrubber overlay vs small D3 island). Decision deferred to S10 spec authoring time.

### Phase-2 policy gate (not a code session)

Before Session 11 (randomization) ships, the following must be filed/locked:
- OSF pre-registration of primary endpoints + "AI-viewed = panel in viewport ≥3s" operational definition (D8).
- IRB data-handling paragraph + clinician_name pseudonym policy (D9).
- Competitive survey (3-4 hours): 6-10 closest analogues catalogued (D3).

Backup cadence (D10) is **not** in this gate — it lives in S6 per /plan-eng-review (TODOS.md).

### Session 11 — Phase 2 randomization

Goal: randomize AI-visible vs not per `(clinician_id, patient_id)` pair, the actual study endpoint.

Scope:
- `assign_arm(clinician_id, patient_id, seed) -> Literal["ai", "no_ai"]` — deterministic given inputs.
- `arm_source` flips from `phase1_stub` to `phase2_randomized` on first assignment.
- AI panel hidden vs visible based on assignment.
- `arm` round-trips through CSV (column added to wide-pivot export from S9c).

**Test inventory:** ≥5 tests. `assign_arm` determinism (same inputs → same output across Python sessions); `arm_source` enum boundary; AI panel visibility iff `arm == "ai"`; **[REGRESSION]** `arm` column round-trips through CSV; switching stub → randomized does not rewrite existing arm_assignments.

### Session 12 — Phase 2.5 polished divergence + v1.0 release

Goal: publication-quality divergence view + reproducible analysis notebook + open-source v1.0 release. This is a **must-have** for v1.0 per the design doc — "do not ship v1.0 without it."

Scope:
- Polished divergence figure suitable for inclusion in the paper.
- Reproducible analysis notebook generating all study figures from the SQLite export.
- README quickstart (~10-min: clone, install, run, see one synthetic patient via `--demo`).
- `docs/data-contract.md` final pass + drift CI green.
- Reference dataset pointer (MIMIC-III access instructions + Geneva sample path if shareable).
- v1.0 git tag cut in parallel with preprint submission.

**Test inventory:** ≥4 tests. Notebook runs end-to-end; all figures regenerate from SQLite; README quickstart smoke test on a fresh clone; data-contract drift CI passes.

## Deliberately deferred

These remain out of scope. Each is tracked in `TODOS.md` or in Session 1's "Open decisions deferred" section, with revival criteria stated:

- Tauri packaging spike — post-Session 2, only if browser-only proves unfit.
- DICOM rendering — only if embedded neurologists insist (D5).
- FHIR compatibility layer — only if external adopters ask.
- Cloud / multi-site concurrency — Phase 3+ territory.
- Live AI model inference — out of scope by design (consumed, not produced).
- Imaging as PNG/DICOM render pipeline — Geneva imaging-as-scalars routes through SCALAR_TS; the IMAGING canonical shape stays empty for Geneva by design. Revisit only if a true imaging source (DICOM dir, PNG renders + report text) is added.
- D3 / bokeh / altair / R-subprocess for charts — plotnine selected. Re-evaluate for the divergence view at S10 (TODOS.md).

## Parallelization notes

Two clean lanes after S1:
- **Lane A (data path):** S3 Geneva → S4 MIMIC → S7 Geneva AI predictions.
- **Lane B (UI/infra):** S2 thin UI + structlog → S5 CLI → S6 SQLite.

Lanes A and B touch disjoint module trees (`src/ingestion/` vs `src/web/` + `src/cli/` + `src/db/`) until S8, where they converge. Worktree-friendly.
