# Session 12 — Phase 2.5 publication figures, reproducible analysis and v1.0 release

Status: **DRAFT for engineering and scientific review, 2026-10-13**. No implementation or release has been performed.

Baseline: `main` at `7efd589d8c90` (2026-10-13). Roadmap: `specs/ROADMAP.md`, Session 12. This spec supersedes only the Session 12 outline, not the locked Phase 2 policy or Sessions 10 and 11.

## 1. Goal

Turn the shipped simulator into an independently reproducible open source research package. A researcher must be able to export one study from SQLite, verify that export, run a notebook and regenerate **every figure defined in this spec** without access to the original SQLite database, the live server, the clinician name keyfile or restricted clinical input files. The published divergence figure must be legible in a paper. A new adopter must be able to see a synthetic patient through `serve --demo` in approximately ten minutes.

**Release gate:** do not tag v1.0.0 until figure generation, the notebook, quickstart, documentation, privacy review and the existing test suite pass. Tagging is a separate owner action coordinated with actual preprint submission.

## 2. Current state and integration points

* S10 already implements `src/ehr_simulator/divergence.py` and `ehr-simulator divergence-view`: a **rough, descriptive**, per patient plotnine SVG, reading answers, arm assignments and timing events under a single live configuration. It includes newly visible clinical data annotations by loading an adapter.
* S11n already implements `ehr-simulator export-phase2 STUDY.yaml --pseudonym-secret SECRET --out-dir DIR`, using a consistent read snapshot and publishing a linked, pseudonymised CSV bundle with `manifest.json`. This export is valid across pinned configuration versions. Its `EXPORT_SCHEMA_VERSION` is `phase2_export_v1`.
* The exported tables are `timepoints.csv`, `answers.csv`, `panel_summaries.csv`, `behavioral_events.csv`, `randomisation_audit.csv`, `configuration_history.csv`, `configuration_counts.csv` and `clinicians.csv` (plus optional separate practice tables). `manifest.json` records file SHA256 and row counts.
* S11h conditional questions, S11l missingness/AI delivery/viewed/PP derivations, S11m privacy and S11p clinician characteristics must be preserved, not recalculated inconsistently.
* S8 has shipped the real Geneva UI. `README.md` still says real Geneva data is not wired, which is stale. `serve` currently has no `--demo` option; `pyproject.toml` is still version `0.0.1`.
* `docs/data-contract.md` is **generated** from canonical schema docstrings and already has a `--check` test/CI gate. The project uses Python 3.11/3.12, `uv`, plotnine, pytest, Ruff, MIT and GitHub Actions.

## 3. Locked decisions

1. **Authoritative analysis input:** `SQLite → export-phase2 → validated bundle → figure functions → notebook`. The notebook must not open SQLite, access a pseudonym secret or use internal DAOs. Do not build the new paper plots on S10's live-hash `divergence-view` path, which cannot correctly cover mixed pinned versions.
2. **No invented scientific estimand:** figures are descriptive. Do not introduce a composite divergence score, hypothesis tests, p values, confidence intervals or a causal AI effect estimate without a separately approved statistical analysis plan. ITT arms come from the realised activation, never from exposure/PP status.
3. **No silent pooling:** default to separate `config_version` panels/tables. The same `question_id` in two versions is not assumed to mean the same question. Report the source versions and hashes. An optional future semantic pooling mechanism is out of scope.
4. **Authoritative denominators:** use activated measured cases as the case cohort and reached primary observations as the default answer/exposure denominator. Never use unactivated schedule positions as completed cases or silently drop incomplete cases. Explicitly display numerator, denominator and missing/unknown count.
5. **Branching and missingness:** hidden conditional questions (`branch_state=hidden`, `response_status=not_applicable`) are not unanswered; rule generated answers remain distinguishable from clinician answers; `value_exported=false` is not a zero or an analyzable response. Preserve S11l missing reasons (`reached_unanswered`, `case_abandoned`, `pending`, `timepoint_never_reached`, etc.).
6. **Telemetry semantics:** wall clock `elapsed_seconds`, measured `active_seconds` and `foreground_seconds` are distinct. Blank or unknown values are **not zero**; `telemetry_status` and `pp_determinate` control interpretation. AI viewed uses the pinned S11k/S11l derivation, not a new threshold in the notebook. Show AI delivery failure and no AI leakage separately from ITT.
7. **No clinical data dependency in the paper pipeline:** S10's newly visible data annotations require canonical clinical frames that are absent from the Phase 2 export. The official export only figures therefore omit these annotations rather than manufacture them. Retain the existing S10 view for exploratory adapter backed use.
8. **Privacy by default:** the notebook does not read or generate keyfiles, print clinician names, plot free text, render raw event payloads or place raw patient/clinician identifiers in titles or figure filenames. Published real cohort outputs require manual disclosure review; synthetic examples may ship.
9. **Chart stack:** use the installed plotnine/matplotlib stack for static SVG/PDF and optional PNG. No Plotly/D3 dependency in Session 12; interactivity is not a release criterion.
10. **Keep scope bounded:** implement in three independently reviewable slices: S12a (bundle reader, summaries and synthetic fixture), S12b (figures and notebook), then S12c (quickstart, docs and release gate). If S12a or S12b needs a new scientific decision, stop and record it rather than silently extending the code.

## 4. Export bundle reader contract (S12a)

Add `src/ehr_simulator/analysis/bundle.py` exposing a read only `load_phase2_bundle(path) -> ValidatedStudyBundle` and a purpose specific `AnalysisError`.

Required validation, before writing any output:

1. Require `manifest.json`, `export_schema_version == "phase2_export_v1"`, all eight required CSVs and precisely the declared per file SHA256 and data row counts; reject missing files, unreadable CSV and any declared file outside the required set and the optional `practice_timepoints.csv`/`practice_answers.csv`. Declared practice files are digest and row count checked like any other, then ignored (item 6).
2. Validate required headers against a **frozen** analysis side copy of the `phase2_export_v1` headers in `analysis/schema.py`, pinned to `export_phase2.headers` by a lockstep test (the same pattern as the duplicated `study_id` pattern): an exporter header change must fail that test, never silently change the analysis contract; refuse duplicate key rows and inconsistent joins across the study, version/hash, case and timepoint keys. Do not import the web layer.
3. Require a single manifest `study_id`, registered versions in `configuration_history.csv`, and matching `config_hash` on all joined records; check `configuration_history.csv` versions are parseable without assuming the active YAML.
4. Use `(study_id, clinician_id, patient_id)` as the internal activated case key and `(case key, t_index)` as the timepoint key. `config_version`/`config_hash` are attributes pinned per case, not key parts: refuse a case whose rows carry more than one version or hash. Preserve `timepoint_minutes` and the pinned version for plotted x coordinates.
5. Read numeric values strictly; preserve nullable booleans and status strings. Refuse impossible values and duplicate rows rather than averaging them. Do not interpret `""` or `"unknown"` as false or zero. Exported cells pass through the S9c `guard_cell` formula guard (a leading `'` before `= + - @` and their full width variants; plain signed numbers unguarded): compare categorical/multi select values against the guarded form of each historical option, never strip a leading `'` blindly. Multi select `response_value` is the pipe delimited option list in historical `questions_json` order; split it and refuse any option absent from that version's question.
6. Treat `practice_*` files as excluded from all measured results, even if present. A valid zero case bundle yields an explicit `no activated cases` result, not invented data.
7. No writes to the input bundle. Render to a separate output directory with a refuse by default overwrite policy; validate first, then stage and publish output atomically where practical.

Document the `phase2_export_v1` input contract and the report schema separately from the generated canonical data contract. A future incompatible export schema must refuse loudly rather than yield plausible but wrong graphics.

## 5. Figure and descriptive analysis contract (S12b)

Implement pure transformations and plot construction in `src/ehr_simulator/analysis/summary.py` and `figures.py`; the notebook must only orchestrate these reusable functions. Output SVG and PDF for all figures, plus machine readable `summary.csv` / `denominators.csv` and `analysis_manifest.json` containing input manifest digest, software version, exact configuration versions, figure inventory and descriptive analysis settings.

The figure set below is **provisional** (owner decision 2026-10-13, §13): it is the reference descriptive set, not the manuscript's final figure list. The study analysis plan is being developed and arrives only after S12. Therefore:

* The stable contract S12 ships is the **bundle reader and the summary tables** (`load_phase2_bundle`, `summary.py` outputs, `denominators.csv`). Figure stems, layouts and their count are not a public contract and may change in a later release without a breaking change.
* `figures.py` exposes figures through a registry (`stem → builder over the validated bundle`); `analysis_manifest.json` lists what was generated from the registry. Adding a plan figure later is one new builder over existing summaries, never a new reader, join or derivation path.
* Plan specific estimands, models and inferential outputs are out of S12 and go into a follow on session once the plan is approved (§13).

Provisional figure set:

| Output stem | Content | Rules |
| --- | --- | --- |
| `figure_01_divergence` | AI versus no AI answers by configured timepoint, question and pinned config version | Per `response_type`: `likert` and `probability-0-100`: individual observations and median, with n. `categorical`: proportion per option and n. `multi-select`: independent per option proportions, which need not sum to one. No invented ordinal scale for nominal options. Use question order from each historical `questions_json`. `free-text`: never plotted (no facet). |
| `figure_02_case_flow` | Planned schedule items, activated cases, completed/incomplete/open states and replacement flow | Keep planned separate from realised. Replacements stay traceable without double counting original and replacement as the same case. Show denominators and arm explicitly. Chart form: stacked horizontal bars per arm and config version (planned → activated → completed / incomplete / open); replacement planned / activated / pending shown as labelled counts beside the incomplete segment, not as a flow diagram (plotnine has no Sankey). |
| `figure_03_response_provenance` | Answered, genuinely missing and not applicable responses by question, timepoint, version and arm | Distinguish clinician versus rule values; display missing reason breakdown and counts of unreached timepoints. Do not count hidden branches as missing. |
| `figure_04_attention` | Wall clock elapsed and valid foreground/active time distributions by arm and timepoint | Separate units and measures; show counts excluded by missing/gapped/invalid telemetry. Never plot an absent duration as 0. |
| `figure_05_intervention` | AI delivered/viewed, intervention failure, leakage and PP determinacy | Conditional to reached primary observations. ITT remains the grouping variable. Use separate panels or clearly labelled rates, including unknown/indeterminate counts. No AI arm has AI viewed as not applicable, not automatically false. |

For each figure, include clearly readable title, arm labels, units, x axis/timepoints, per group sample size and a caption containing the population, denominator and descriptive nature of the calculation. Provide colour independent differentiation (shapes/line styles), print legibility at one column width, accessible contrast and a consistent chart palette. Use a deterministic categorical option order from the historical snapshot. Do not infer uncertainty bands from the small synthetic example.

A supplementary **case level** divergence image may be generated by explicit patient selection for research inspection, but never batch name outputs with patient IDs or publish raw clinical values. If only one arm has observations, plot it with `no between arm comparison available`, not an implied difference. Empty questions and intervals have labelled placeholders, not missing facets.

A figure may use the Phase 2 case and question facts only. Without the clinical adapter input, `newly_visible_data` annotations are not part of the reproducible figure set. This distinction must be stated in the notebook and README.

## 6. Notebook (S12b) and synthetic reference bundle (S12a)

Add `notebooks/phase2_analysis.ipynb` with these sections: environment/input; export/manifest validation; cohort and configuration provenance; case flow; responses and divergence; timing/attention; intervention integrity and PP; output manifest and interpretation limitations.

* Parameters come from `EHR_SIM_PHASE2_BUNDLE` and `EHR_SIM_ANALYSIS_OUT`. Defaults point to a **committed synthetic** miniature Phase 2 export bundle and a local ignored output directory. Run headlessly through `nbclient` (or `jupyter nbconvert --execute`) in CI, on Python 3.11 and 3.12.
* The committed miniature bundle must be generated from the **shipped S11n exporter** with synthetic clinicians, both arms, multiple timepoints, a conditional question and at least one incomplete/replacement or explicitly missing state. Store no pseudonym secret/keyfile, names, real Geneva records, or restricted MIMIC material. Its manifest must validate; document fixture regeneration.
* **Deterministic regeneration.** The builder (`tests/fixtures/analysis_bundle/build_analysis_bundle.py --check`, run in CI beside the Geneva/MIMIC drift gates) drives the app with a FakeClock and a fixed test only pseudonym secret held as a constant in the builder, never written as a file into the fixture. The shipped exporter still emits run dependent values: `arm_assignments.activated_at` (SQLite `CURRENT_TIMESTAMP`), `session_id` and `render_id` (`uuid4`), event `server_ts` and the manifest's `generated_at_utc`. `--check` therefore compares a **normalised** bundle: those columns replaced by stable ordinal placeholders (consistent across tables, so joins survive), CSV SHA256 and manifest recomputed. The committed fixture is the normalised form, and its manifest must validate through `load_phase2_bundle`. Do not change the exporter or the app to make output deterministic.
* The notebook is a thin consumer of `analysis/`, not a second implementation of joins, rates or thresholds. It contains a readable method note with eligibility, missingness, descriptive summaries and limitations. No random results or hidden downloaded inputs.
* Report generation must complete from this bundle after a clean `uv sync --locked --group analysis`; add a small opt in `analysis` dependency group (`nbclient`, `nbformat`, `ipykernel`; the notebook pins kernel name `python3`, provided by `ipykernel`) and update `uv.lock`. Do not force notebook tooling on the main server runtime.
* For a real study, an operator first runs `export-phase2` with their local secret, moves **only the validated bundle** into their analysis environment and points the notebook at it. The notebook cannot recover raw identities and must not ask for their secret.

## 7. `--demo` and README quickstart (S12c)

Add `--demo` to `ehr-simulator serve` as a friendly, explicit alias for the existing **config free synthetic** FastAPI path. `--demo` requires no input data, activation, pseudonym secret or YAML. It must not create a measured Phase 2 case or change the study configuration. It may use the existing non study local SQLite persistence; document the exact location and clean reset procedure. Forbid combining `--demo` with `--config`/`--questions` (exit 2), and keep the existing `127.0.0.1` default bind. The config free path already exists: the new work is the flag, the refusal and the indicator. Show a conspicuous `synthetic demonstration` indicator in this mode so it cannot be confused with a clinical study.

The top of README must have a **tested** approximately ten minute path:

```bash
git clone https://github.com/JulianKlug/ehr_simulator.git
cd ehr_simulator
uv sync --locked
uv run ehr-simulator serve --demo
# open http://127.0.0.1:8000; sign in; open synth_001
```

Follow with a distinct research operator guide for Phase 2: validate config, activate config, serve, back up, export with secret, run the notebook and interpret provenance. Correct the stale README statement about Geneva UI; distinguish real Geneva support from the fact that non public source data is **not bundled**. Clearly label placeholder Phase 2 clinical questions and profile choices as examples, not ready for a preregistered clinical experiment.

## 8. Reference data, generated documentation and open source hygiene (S12c)

* Add `docs/reference-data.md` describing synthetic built in data, checked in Geneva/MIMIC fixture shapes and how to obtain MIMIC III from its official access controlled source. Link the official access/credential process; do not distribute MIMIC records. State that real Geneva source data and AI artifacts are not included. Include a Geneva sample path **only if explicit sharing permission exists**; otherwise refer to the safe synthetic fixture in `tests/fixtures/geneva/`.
* Regenerate `docs/data-contract.md` via `python scripts/gen_data_contract.py`; never hand edit the generated body. Require `--check`, Geneva/MIMIC sidecar drift gates and relevant adapter regression tests green.
* Add `docs/analysis.md` documenting figure methods, source table/column mapping, descriptive only status, analysis manifest, pooled cohort restrictions and privacy of outputs. Add `docs/release-checklist.md` covering dependencies, CLI, docs, data handling, licensing, CI, optional local real data smoke and disclosure review.
* Audit pre release TODOs against current code instead of blindly applying stale suggestions. For example, the deprecated SQLite converter issue is already addressed in current `db/connection.py`; README's Geneva status is not. Review error handling and CSP residuals and fix demonstrable release blocking issues or document a narrowly scoped deferred item.
* Verify MIT licence and copyright attribution, clean install/build, generated files ignored appropriately, no secrets or real records committed, published demo screenshots/figures synthetic, and a changelog/release notes entry. Set package version from `0.0.1` to `1.0.0` **only after** release acceptance; refresh the lockfile.

## 9. Expected file changes

| Path | Action |
| --- | --- |
| `specs/session-12-phase25-release.md` | New approved spec |
| `src/ehr_simulator/analysis/{__init__,schema,bundle,summary,figures}.py` | New frozen input schema, pure reader, summaries and static figure builder |
| `notebooks/phase2_analysis.ipynb` | New parameterised notebook |
| `tests/fixtures/analysis_bundle/` and fixture builder | New synthetic reproducible bundle, drift guard |
| `tests/test_analysis_schema.py` (header lockstep), `tests/test_analysis_bundle.py`, `tests/test_analysis_figures.py`, `tests/test_notebook.py` | New tests |
| `src/ehr_simulator/cli/serve.py`, UI template/CSS if needed | Add visibly synthetic `--demo` mode only |
| `tests/test_cli.py` and CI workflow | Demo, analysis execution and docs smoke gates |
| `README.md`, `docs/analysis.md`, `docs/reference-data.md`, `docs/release-checklist.md`, `CLAUDE.md`, `TODOS.md`, `specs/ROADMAP.md` | Documentation reconciliation |
| `pyproject.toml`, `uv.lock` | Analysis dependency group and final version |

**No required SQLite migration, new clinician workflow, real data upload, statistical model, new chart stack or breaking change to `export-phase2`.** Keep the legacy S10 `divergence-view` functional, clearly labelled rough/exploratory.

## 10. Numbered test inventory

Target **at least 20 newly scoped checks** (the roadmap minimum of four is a floor, not a sufficient release gate), using current `unit` / `integration` / `slow` / `e2e` conventions.

### Unit (9)

1. Reject a bundle with wrong manifest export schema version.
2. Reject missing files or mismatched file SHA256.
3. Reject mismatched manifest row counts, CSV header changes and duplicate case/timepoint/question keys.
4. Reject unknown config versions and row/hash linkage drift, while accepting valid **mixed** versions.
5. Prove planned schedule entries never count as activated measured cases, including replacement chains.
6. Prove hidden questions are not counted as missing and rule answers retain their provenance.
7. Prove numeric/categorical/multi select question denominators and option ordering are correct, including one arm/no answer cases.
8. Prove blank/unknown telemetry and PP indeterminacy are preserved, not coerced to zero/false.
9. Prove free text, raw identifiers and event payloads cannot enter figure title, filename or plotted text.

### Integration (6)

10. Build all five figures, summaries and analysis manifest from a real S11n generated synthetic bundle.
11. Run the same analysis against two pinned configuration versions; outputs remain stratified and consistent with their historical questions.
12. Produce SVG and PDF with non empty visible marks, legible legend/captions and stable named output inventory.
13. A zero case export gives a documented no data result with no fabricated rates or comparisons.
14. `serve --demo` boots in a temporary directory and renders the synthetic index and `synth_001`; conflicting study flags refuse.
15. A bad input bundle or existing output directory refuses without altering existing outputs.

### Regression (3)

16. AI display failure or leakage changes intervention/PP categories, **not** the recorded ITT arm.
17. A valid Phase 2 bundle with a missing/invalid browser telemetry stream does not turn unknown viewing or attention into a negative measurement.
18. Existing S10 `divergence-view`, S11n `export-phase2` and existing Phase 2 tests continue to pass unchanged.

### Notebook / release / E2E (4)

19. Headless notebook executes on the committed synthetic bundle on Python 3.11 and 3.12, with all expected figure files.
20. Independently compare notebook numeric summary tables with the analysis library output; rerunning with the same input/settings gives equal values and figure inventory (do not require cross platform byte identical SVG).
21. Execute README's install/`serve --demo` path in a clean checkout: CI clones the tested commit locally (`git clone file://$GITHUB_WORKSPACE` into a temp directory, not GitHub, which may not have the commit), runs `uv sync --locked`, starts `serve --demo` in the background, polls until it answers, signs in and fetches the synthetic patient page.
22. Run the generated data contract `--check`, fixture drift gates, Ruff, complete non real data pytest suite and Playwright E2E, in CI.

## 11. Manual scientific and release acceptance

1. Review the printed divergence figure at normal journal one column width and in greyscale. Check categories, medians, units, n/denominators, missingness, one arm rendering and captions against known synthetic test cases.
2. With a synthetic Phase 2 run, export from the study SQLite DB using `export-phase2`, validate its manifest and execute the notebook **without the DB**. Every required figure and summary must regenerate from that bundle alone.
3. A colleague with a fresh checkout can follow the README quickstart and reach `synth_001` in a browser, without private data or a preconfigured SQLite database.
4. All release hygiene gates are green; no restricted records, names, secret, keyfile or non reviewed real data figures are included in the repository, wheel, release notes or attached preprint material.
5. The owner reviews the final figures and their descriptive only claims for the intended manuscript. Any inferential statistics or a manuscript specific estimand require an independent approved analysis plan.
6. Only after all above criteria and actual preprint submission coordination: update to `1.0.0`, create and verify annotated Git tag `v1.0.0` against the tested commit and publish the matching release notes. Do **not** tag automatically as part of tests.

## 12. Implementation sequencing and stop conditions

**S12a — analysis contract (first review/ship):** frozen schema + lockstep test, bundle verifier, pure summaries, deterministic synthetic fixture and its drift gate; tests 1–8, 13 (summary level) and 15 (bundle refusal half). No figures.

**S12b — figures and notebook (second review/ship):** five figures, analysis manifest, output publication, notebook, `analysis` dependency group and CI execution; tests 9–12, 13 (figure level), 15 (output directory half) and 16–20. Not blocked by the analysis plan: the five figures are built as the provisional reference set (§5). Freeze only the reader, summary and denominator semantics during scientific review; figure stems stay provisional.

**S12c — adopter and release polish (third review/ship):** `--demo`, README and reference docs, contract drift, security/permission audit, CI clean checkout smoke, tests 14–15 and 21–22, owner release checklist and eventual manual tag.

If any slice exceeds the established one to two day target, split before implementation and add a short follow on spec. Do not compromise S11's provenance, privacy, randomisation or timepoint visibility safeguards to shorten the release path.

## 13. Explicit non goals and open owner decisions

**Non goals:** raw Geneva or credentialed MIMIC publication, automated preprint submission, registered trial analysis, inferential model fitting, power calculations, interactive dashboards, cloud study hosting, data ingestion redesign, DICOM/FHIR, web analytics, clinician facing UI redesign.

**Owner decision (resolved 2026-10-13):** the five figure list is provisional. A study specific statistical analysis plan is being developed and will be available only after S12 ships. S12 builds the provisional descriptive set on a stable reader/summary contract (§5); the plan's figures and any estimand are a follow on session with its own spec, consuming `analysis/` unchanged. Do not label a descriptive graphic a treatment effect estimate.

**Open owner decision before the v1.0.0 tag:** does v1.0.0 ship with the provisional figure set, or wait for the plan's figures if the preprint needs them? Either way, S12 implementation is not blocked.

**Owner decision before S12c release:** Does a shareable Geneva example exist under appropriate data permissions? If not, release only synthetic/sanitised fixtures and access instructions. Confirm the preprint submission event before creating the v1.0.0 tag.

