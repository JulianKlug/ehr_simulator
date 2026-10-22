# Analysis input contract (`phase2_export_v1`), summary tables and report

Sessions 12a, 12b. Spec: `specs/session-12-phase25-release.md` §4, §5, §6.

The analysis package (`src/ehr_simulator/analysis/`) reads **only** a bundle written by `ehr-simulator export-phase2`. It never opens SQLite and never asks for a pseudonym secret or keyfile.

```
export-phase2 bundle ─► load_phase2_bundle ─► ValidatedStudyBundle ─► build_summaries
                                                                     ─► figures ─► publish_report
```

## Input: what the reader accepts

`load_phase2_bundle(path)` returns a `ValidatedStudyBundle` or raises `AnalysisError`. It writes nothing.

1. **Manifest.** `manifest.json` must hold exactly the S11n keys, with `export_schema_version == "phase2_export_v1"`. Any other schema is refused.
2. **File set.** The manifest declares the eight required CSVs, plus both practice files when `practice_included` is true. A declared file outside that set is refused, and so is any undeclared file in the directory. Each file's SHA256 and data row count must match the manifest. Practice files are verified like the others, then excluded from every measured result.
3. **Headers.** Each header must equal the frozen copy in `analysis/schema.py`. `tests/test_analysis_schema.py` pins that copy to `export_phase2/headers.py`, so an exporter change fails the test before it can move the analysis contract.
4. **Cells.** Cells are parsed strictly, by kind:
   - Booleans are `true`/`false`. Integers and floats must be finite and ≥ 0. Enums must belong to the frozen vocabularies.
   - An optional cell may be `""`, read as `<NA>` (unknown). It is never false or zero.
5. **Configurations.**
   - `configuration_history.csv` versions are unique and listed in manifest order.
   - Both snapshots must parse.
   - `config_hash` is recomputed from the snapshots and must match.
   - Every row's `(config_version, config_hash)` must be a registered pair.
6. **Keys.**

   | Key | Columns |
   | --- | --- |
   | case | `(study_id, clinician_id, patient_id)`. One version, hash, arm and lifecycle per case. |
   | timepoint | case + `t_index`. Exactly the pinned study timepoints. |
   | answer | timepoint + `question_id`. Exactly the pinned questions. |
   | panel | timepoint + `visit_kind` + `panel_id` |
   | audit | `clinician_id` + `schedule_id` + `case_position` |

   Duplicates are refused.
7. **Joins.**
   - Activated audit items and cases match one to one, with the same arm, version and lifecycle.
   - Replacement links must point both ways.
   - `configuration_counts.csv` equals the counts recomputed from the rows.
   - `clinicians.csv` lists every case and schedule clinician.
8. **Answers.**
   - Status invariants:
     - `hidden` ⇔ `not_applicable`
     - `missing` ⇔ `missing_reason`
     - `answered` ⇔ `answer_source`
     - `rule` ⇔ `derived_from_question_id`
     - an answer with no exported value is free text only
   - Values are compared in their exported form, after the S9c formula guard:
     - A categorical cell must equal the guarded form of exactly one historical option.
     - A multi-select cell is the guarded, `|`-joined option list in `questions_json` order. A leading `'` is stripped only when re-guarding reproduces the cell.
   - Likert and probability values must be integers inside their scale.
   - Free text is kept as exported and never decoded.

`ValidatedStudyBundle` holds:
- typed frames (`cases`, `timepoints`, `answers`, `panels`, `audit`, `counts`, `clinicians`)
- `versions` (`ConfigVersion`: hash, parsed `StudyConfig` and `Questions`)
- `manifest_sha256`

`answers` adds `value_number` (likert, probability) and `value_options` (a tuple of historical options). The `payload_json` of `behavioral_events.csv` is verified but not exposed.

## Output: summary tables (stable contract)

`build_summaries(bundle) -> AnalysisSummaries`. Every table follows these rules:
- **Descriptive only:** no tests, intervals or effect estimates.
- **Per version:** keyed by `config_version` and never pooled.
- **ITT arm:** grouped by the realised activation's `arm`.
- **Full grid:** pinned timepoints × pinned questions × both arms, so an empty cell is a zero count, never a missing row.

| Table | Rows | Content |
| --- | --- | --- |
| `cohort` | — | `has_cases` / `no_activated_cases` |
| `case_flow` | version × arm | Planned schedule items (generation version, planned arm) beside activated / completed / incomplete / open / state unknown cases. Replacements are counted on the original (planned / activated / pending). |
| `response_provenance` | version × question × t × arm | observations, reached, reached and applicable, answered (clinician / rule), not applicable, value not exported, missing by S11l reason |
| `option_proportions` | … × answer source × option | Categorical and multi-select shares in historical option order. Multi-select shares are independent. `proportion` is `<NA>` when there are no answers. |
| `numeric_responses` | … × answer source | Likert / probability n, median, min, max (`<NA>` when n = 0). `numeric_observations` lists the points. |
| `attention` | version × t × arm | Over reached observations: S10 elapsed n / median. Foreground / active n / median come only from `telemetry_status == complete`, with counts per telemetry status. `attention_observations` lists the known durations (a blank one has no row). |
| `intervention` | version × t × arm | Over reached observations: AI delivered / viewed true / false / unknown (AI arm only), no AI viewing `not_applicable`, `intervention_failure` (true) and `intervention_failure_unknown`, failures by reason, leakage, PP compliant / noncompliant / indeterminate. |
| `denominators` | one per measure | `summary`, `measure`, version, arm, t, question, `population`, `n`, `denominator`, `missing_unknown` |

An unreached timepoint never makes a question `not_applicable`: a question the export marks hidden there (its deciding answer was never given) is counted missing by reach status, `case_abandoned` (case incomplete), `pending` (case open) or `timepoint_never_reached` (owner decision, S12c review; the export itself is unchanged).

Unknown values are never treated as zero:
- A blank duration, delivery, viewing or PP value is counted as unknown.
- **Intervention failure is unknown when delivery is unknown** (owner decision, S12c review; changes the frozen S12a contract). An AI arm observation with a blank `ai_delivered` and no recorded failure counts in `intervention_failure_unknown`, never as "no failure". A recorded failure stays true. The exporter is unchanged: it writes `intervention_failure = false` for such rows; only the analysis reads them as unknown. The `intervention_failure` denominators row (both arms) carries them as `missing_unknown`.
- Medians use known values only.
- Free text is summarised only by its response status. It has no `value_exported` denominator row: free text withheld from the routine export is answered, not missing (S12c review).

## Report: figures and published outputs (S12b)

The figures are **supplementary**, confirmatory descriptive figures (`"figure_role": "supplementary"` in the manifest). The main-message figures will come from the approved analysis plan in a follow-on session (owner decision 2026-10-19). The stable contract is the reader and the summary tables above. Figure stems, layouts and their number may change.

```
publish_report(bundle_dir, out_dir, overwrite=Overwrite.REFUSE)
    refuse: out_dir exists (REFUSE) · out_dir overlaps the bundle   ─► nothing read or written
    load_phase2_bundle + build_summaries                           ─► AnalysisError: nothing written
    <parent>/.<name>.staging-<id>/  ─► rename to <out_dir>          (REPLACE: old report aside, restored on failure)
```

`REPLACE` replaces only an empty directory or a previous report (an `analysis_manifest.json` of a known schema: `phase2_analysis_v1` or `phase2_analysis_v2`), never a symbolic link. Both modes re-check the output right before the rename, so a report published meanwhile by another run is kept.

| Path | Content |
| --- | --- |
| `figures/<stem>_<config_version>.svg`, `.pdf` | One per `figures.FIGURES` entry and config version: versions never share a figure. SVG text stays text. Neither format carries a creation date. |
| `tables/<summary>.csv` | The six S12a tables. `<NA>` is written as an empty cell, never 0. Text cells pass the S9c formula guard. |
| `denominators.csv` | The S12a `denominators` table |
| `analysis_manifest.json` | Written last. It holds: <br>• `analysis_schema_version` (`phase2_analysis_v2`; v1 had one figure file for all versions) <br>• input schema, manifest SHA256 and `study_id` <br>• software versions <br>• every `config_version` + `config_hash` <br>• `cohort` <br>• `figure_role` (`supplementary`) <br>• settings <br>• the figure inventory: `stem`, `title`, `config_version`, `files` per figure file <br>• each table's rows and SHA256. <br>There is no timestamp, so equal inputs give an equal manifest. |

| Stem | Source tables | Shows |
| --- | --- | --- |
| `supplementary_01_divergence` | `numeric_observations`, `numeric_responses`, `option_proportions` | One panel per question: <br>• likert / probability: individual values plus the median; likert ticks are the scale's integers <br>• two option questions: the first option's share, named in the strip <br>• more options: one line per option, labelled at its right end (multi-select shares are independent) <br>• one `● / ▲ n=` count row per arm <br>• line type per arm × source (AI solid, No AI dashed, rule AI dotted, rule No AI dash-dot); rule markers hollow <br>• a line breaks where its series has no value <br>• no free text |
| `supplementary_02_case_flow` | `case_flow` | Planned items beside activated cases by lifecycle state, with replacement counts as text |
| `supplementary_03_response_provenance` | `response_provenance` | One tile grid: questions × ITT arm · timepoint. Each tile names its non-zero categories as code + count (answered clinician / rule, not applicable, missing by S11l reason); fill = share missing; an `n` row gives the case timepoints |
| `supplementary_04_attention` | `attention_observations`, `attention` | Elapsed, foreground and active seconds (median = horizontal tick), with `excl k` for reached observations that have no valid duration |
| `supplementary_05_intervention` | `intervention` | Delivered and viewed (AI arm), failure (both arms), AI leakage (No AI arm) and PP, as yes / no / unknown counts by ITT arm. Rows that would be all not applicable are omitted |

Every figure follows these rules:
- One column wide (3.5 in), at most 9.5 in tall on the reference bundle. Captions start at the figure's left edge.
- The arm is shown by colour **and** shape. Category fills are ≥ 15 L* apart, so they stay distinct in greyscale; codes on dark fills are white.
- One config version per figure file, named in the subtitle (`Config version v1`). Strips name the question, measure or outcome row only.
- The caption states the population, the denominator and "Descriptive only".
- A version without activated cases renders `No activated cases`. A bundle without any case gives one placeholder file per figure and registered version.
- Plotted text comes only from constants, config versions, question ids, option labels and counts.

`figures.case_divergence_figure(bundle, patient_id, version)` draws one explicitly selected patient's cases of one config version, one line per case (`case 1…`). It is not part of the report.

## Notebook

`notebooks/phase2_analysis.ipynb` only orchestrates the functions above. Run it after `uv sync --group analysis`.

| Variable | Default |
| --- | --- |
| `EHR_SIM_PHASE2_BUNDLE` | `../tests/fixtures/analysis_bundle/walked` |
| `EHR_SIM_ANALYSIS_OUT` | `../analysis_out/phase2` (git ignored) |
| `EHR_SIM_ANALYSIS_OVERWRITE` | `refuse` (`replace` swaps a previous report atomically) |

`uv run pytest -m notebook` executes it twice on the synthetic bundle. It checks that the tables equal the library's and that the manifest is stable. The S10 `newly_visible_data` annotations need clinical source data that the export does not carry, so they are not part of this figure set.

## Reference bundles

`tests/fixtures/analysis_bundle/{walked,empty}/` are generated by the shipped exporter from a synthetic walk. CI checks them for drift:

```bash
uv run python tests/fixtures/analysis_bundle/build_analysis_bundle.py          # regenerate
uv run python tests/fixtures/analysis_bundle/build_analysis_bundle.py --check  # CI drift gate
```

The builder pins every run-dependent value. It never writes a secret, keyfile or name into the fixture.
- **Timestamps:** SQLite timestamps on a scratch DB become `T0 + row order`.
- **Ids:** uuid ids become ordinals.
- **Manifest:** `generated_at_utc` becomes a constant.
