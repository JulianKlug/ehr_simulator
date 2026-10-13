# Analysis input contract (`phase2_export_v1`) and summary tables

Session 12a. Spec: `specs/session-12-phase25-release.md` §4, §5.

The analysis package (`src/ehr_simulator/analysis/`) reads **only** a bundle written by `ehr-simulator export-phase2`. It never opens SQLite and never asks for a pseudonym secret or keyfile.

```
export-phase2 bundle ─► load_phase2_bundle ─► ValidatedStudyBundle ─► build_summaries
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
| `attention` | version × t × arm | Over reached observations: S10 elapsed n / median. Foreground / active n / median come only from `telemetry_status == complete`, with counts per telemetry status. |
| `intervention` | version × t × arm | Over reached observations: AI delivered / viewed true / false / unknown (AI arm only), no AI viewing `not_applicable`, failures by reason, leakage, PP compliant / noncompliant / indeterminate. |
| `denominators` | one per measure | `summary`, `measure`, version, arm, t, question, `population`, `n`, `denominator`, `missing_unknown` |

Unknown values are never treated as zero:
- A blank duration, delivery, viewing or PP value is counted as unknown.
- Medians use known values only.
- Free text is summarised only by its response status.

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
