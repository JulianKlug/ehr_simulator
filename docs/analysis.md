# Phase 2 analysis methods

This document covers how the reproducible Phase 2 figures and tables are computed, and how to read them. The input and output contract (manifest checks, typed cells, table columns, report layout) lives in [`analysis-contract.md`](analysis-contract.md).

## Pipeline

```
study SQLite ──export-phase2 (secret)──► bundle/ ──load_phase2_bundle──► ValidatedStudyBundle
                                                                            │
                                     build_summaries ◄──────────────────────┘
                                            │
                     figures.FIGURES ◄──────┤
                                            ▼
                         publish_report ──► report/{figures,tables,denominators.csv,analysis_manifest.json}
```

- **Input.** The analysis reads only the exported bundle. It never reads the database, the pseudonym secret, the keyfile or clinical source data.
- **Notebook.** `notebooks/phase2_analysis.ipynb` runs these library functions. It computes nothing itself.

## Status: descriptive only

Every number is a count, proportion, median, minimum or maximum. **There is no hypothesis test, p value, confidence interval, effect estimate or composite divergence score.**

A difference between arms in a figure is a description of this sample. It is not an estimate of the effect of AI assistance. Inferential analyses need a separately approved statistical analysis plan. A plan figure is added as one more builder in `figures.FIGURES` over the existing summaries.

The five figures are a **provisional** reference set. The stable contract is the bundle reader, the summary tables and `denominators.csv`.

## Populations and denominators

| Population | Definition | Source |
| --- | --- | --- |
| Activated cases | Measured `phase2_randomized` cases that were started (`timepoints.csv` rows). Planned schedule items that were never activated are counted separately and are never cases. | `timepoints.csv`, `randomisation_audit.csv` |
| Observations | One per configured timepoint of an activated case | `timepoints.csv` |
| Reached observations | `timepoint_reached` true: behind the frontier, completed, or the frontier with an S10 enter | `timepoints.csv` |
| Applicable responses | Reached, and the question was not hidden by a branch (`response_status != not_applicable`) | `answers.csv` |

- **ITT arm.** The arm is the realised activation's `arm`. Exposure, viewing and PP status never regroup a case.
- **Practice.** Practice cases are excluded, even when the bundle carries `practice_*.csv`.
- **Numerators and denominators.** Each table cell states its numerator, its denominator and its missing/unknown count. `denominators.csv` lists them all in one long table.

## No pooling across versions

Every table and figure strip is keyed by `config_version` (with its `config_hash` in `analysis_manifest.json`). The same `question_id` in two versions is not assumed to be the same question, so a question absent from a version shows `not in this version`. Pooling versions needs a semantic mapping that this release does not provide.

## Measures and their source columns

| Measure | Columns | Rule |
| --- | --- | --- |
| Case flow | `randomisation_audit.csv`: `planned_arm`, `generation_config_version`, `activated`, `realised_arm`, `replacement_id`, `replaced_by_replacement_id`; `timepoints.csv`: `lifecycle_state` | Planned beside realised. A replacement is counted once, on the original case's row: planned, activated or pending. |
| Response provenance | `answers.csv`: `response_status`, `missing_reason`, `answer_source`, `value_exported` | Hidden (`not_applicable`) is not missing. Rule answers (`answer_source = rule`) are kept apart from clinician answers. Missing answers are broken down by S11l reason: `reached_unanswered`, `case_abandoned`, `pending`, `timepoint_never_reached`, `unknown`. |
| Numeric answers | `answers.csv`: `response_value` (read as `value_number`), `response_type` ∈ {likert, probability} | n, median, min, max over answered values; individual values are plotted |
| Option answers | `answers.csv`: `response_value` (read as `value_options`, guarded form matched against the historical options), `response_type` ∈ {categorical, multi-select} | Each option's share among answers, in the historical option order. Multi-select shares are independent and need not sum to 1. No ordinal scale is invented. |
| Free text | – | Never plotted or summarised beyond its response status |
| Attention | `timepoints.csv`: `elapsed_seconds` (S10 wall clock), `foreground_seconds`, `active_seconds`, `telemetry_status` | Three separate measures. Foreground and active time count only when `telemetry_status = complete`. Each other status is counted as excluded. |
| Intervention | `timepoints.csv`: `ai_delivered`, `ai_viewed`, `intervention_failure`, `intervention_failure_reasons`, `intervention_leakage`, `pp_compliant`, `pp_determinate` | Over reached observations. On the no AI arm, AI viewing is `not applicable`, not false. Unknown is a category of its own. An AI arm observation with unknown delivery has an unknown failure, not "no failure" (owner decision, S12c review; the export still writes `false`). |

**Unknown is never zero.** A blank duration, delivery, viewing or PP cell is counted as unknown. Medians use known values only. An absent duration is never plotted as 0. "AI viewed" is the pinned S11k/S11l derivation recorded in the export. The analysis applies no threshold of its own.

## Reading the figures

| Figure | How to read it |
| --- | --- |
| 1 Divergence | One panel per question and version. A two option question plots the share answering the option named in the strip (the other is its complement). A question with more options draws one line per option, labelled at its right end. Lines: AI solid, No AI dashed; rule answers dotted (AI) or dash-dot (No AI), with hollow markers. A line never crosses a timepoint where its series has no value. Below the values, one row per arm gives `n` answers per timepoint. |
| 2 Case flow | Planned schedule items beside activated cases by lifecycle state. |
| 3 Response provenance | One tile per question × arm × timepoint. The codes carry the meaning, e.g. `AC2 MU1` = two clinician answers, one reached but unanswered. The fill only shows the share missing (none / some / all). Hidden (`NA`) is evaluated first, so a conditional question at an unreached timepoint can be `NA` rather than pending, abandoned or never reached. |
| 4 Attention | Points are durations, horizontal ticks medians. `● / ▲ excl k` counts reached observations without a valid duration. |
| 5 Intervention | Delivered and viewed for the AI arm, AI leakage for the no AI arm (yes = leakage occurred), failure and PP for both. Yes / no / unknown (`?`) per timepoint. |

All five fit one column (3.5 in) and one page on the reference bundle. Arms differ by colour and shape, lines by type, and category fills by lightness, so a greyscale print stays readable.

## What the figures do not show

- **Newly visible clinical data.** The export carries no clinical values, so the S10 `newly_visible_data` annotations of `ehr-simulator divergence-view` are absent from this figure set. That command stays available for exploratory, adapter-backed inspection of a single live configuration.
- **Uncertainty.** No bands or intervals are drawn.
- **Between-arm comparisons where one arm has no data.** Such a strip reads `no between arm comparison available`.

## Privacy of outputs

- **No identifiers in figures or files.** Figures, file names and plotted text never carry clinician or patient ids, names, free text or event payloads. Plotted text comes only from constants, config versions, question ids, option labels and counts.
- **Pseudonymised ids.** The bundle's `clinician_id` values are keyed pseudonyms. The bundle still keeps randomisation seeds, so treat a real bundle as re-identifiable (README, *Known residual risk*).
- **Single-patient figure.** `figures.case_divergence_figure(bundle, patient_id)` is called only on explicit selection and labels cases `case 1…`. It is never written by `publish_report`.
- **Disclosure review.** Small cells can identify. Review any figure or table from a real cohort before you share or publish it. The synthetic reference bundle (`tests/fixtures/analysis_bundle/`) needs no review.

## Reproducing a report

```bash
uv sync --locked --group analysis
EHR_SIM_PHASE2_BUNDLE="$PWD/BUNDLE" EHR_SIM_ANALYSIS_OUT="$PWD/analysis_out/phase2" \
  uv run jupyter execute notebooks/phase2_analysis.ipynb
```

- **Equal inputs, equal report.** The same bundle and settings give equal tables and an equal `analysis_manifest.json`, which has no timestamp. SVG bytes may differ across platforms.
- **Existing output.** An existing output directory is refused, unless `EHR_SIM_ANALYSIS_OVERWRITE=replace`, which swaps a previous report (or fills an empty directory) atomically. Any other directory is refused, never deleted.
