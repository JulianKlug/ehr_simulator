# Session 11i — Study behaviour policy switches

## Goal

Put the remaining clinician facing study behaviour behind explicit, case pinned configuration: backward navigation, performance feedback, practice cases and free text.

S11a through S11h are assumed complete.

## Review changes (2026-09-26)

1. **Feedback refuses `true`.** No feedback provider or ground truth model exists, and the spec's own non goals exclude one. Accepting `true` would promise a surface that cannot render. The block records the explicit all-false policy in the snapshot; `true` is a validation error naming the missing capability.
2. **Revisit defined by pane mode:** `primary` ⇔ the editable frontier; anything else viewable (behind the frontier, or a completed case) is a `revisit`.
3. **`prohibit` reuses the S9b gate redirect** (303 / `HX-Redirect` to the frontier) instead of a new 409 shape. A completed case stays viewable at its last timepoint only.
4. **Practice made concrete:** routing, case resolution, lifecycle (not tracked by S11e), completion inside the final advance transaction, and a cross version rule. The gate forbids reusing a practice patient as a measured patient, which a per configuration disjointness check alone would miss.
5. **Practice requires Phase 2** (`randomisation` present), and a practice arm of `ai` requires `ai_intervention`.
6. **One cross model validator** for study + questions rules (free text), called by `validate-config`, `activate-config`, boot and preflight.

## Core invariants

1. Measured behaviour follows the case pinned configuration; a new version affects only cases started afterwards.
2. Backward navigation never makes a historical answer editable.
3. Revisits never write or overwrite S10 `timepoint.enter` / `timepoint.exit`.
4. Practice observations are explicitly marked and never count toward measured randomisation balance, ITT, PP, replacement or clinician stopping.
5. Free text is off for any study that declares `study_behaviour`, unless enabled explicitly.
6. No policy can be switched by a clinician through query parameters or browser state.

## Configuration

Optional `StudyConfig.study_behaviour`, omitted from serialization when absent:

```yaml
study_behaviour:
  backward_navigation: allow_readonly   # or prohibit; required, no default
  feedback:                             # optional; every flag defaults to and must be false
    show_ground_truth: false
    show_correctness: false
    show_running_score: false
    show_ai_correctness: false
  practice:                             # optional; default disabled
    enabled: false
    patient_ids: []
    arm: no_ai                          # ai | no_ai
  free_text:                            # optional; default disabled
    enabled: false
    routine_export: exclude             # exclude | include_explicit
```

Validation:

- `backward_navigation` is required when the block is present: the first use case must choose explicitly (gate §26.9).
- Any feedback flag `true` → error "performance feedback is not implemented".
- `practice.enabled: true` requires non empty unique `patient_ids`, disjoint from `patient_ids`, and a `randomisation` block; `arm: ai` requires `ai_intervention`.
- `practice.enabled: false` requires an empty `patient_ids`.

Without `study_behaviour` (legacy): read only backward navigation, no practice, free text allowed and exported as today.

## Backward navigation

`gating.is_viewable(frontier, t_index, policy)`:

| policy | viewable |
|---|---|
| `allow_readonly` (and legacy) | `t_index <= unlocked_t_index` |
| `prohibit` | `t_index == unlocked_t_index` |

A non viewable GET takes the existing gate redirect to the frontier, before slicing, session bootstrap or any write. Answer and advance already refuse non frontier timepoints (409 / 412) and stay unchanged.

Under `prohibit` the summary card omits the Prev button; `keyboard.js` `[` finds no button and does nothing.

### Revisit marking

- `_patient_view.html` carries `data-visit-kind="primary|revisit"`: `primary` ⇔ `pane_mode == "open"`.
- After a successful revisit render the GET appends one `timepoint.revisit` event, payload `{"t_index": n}`, where it would otherwise have recorded `timepoint.enter`. An occurrence marker, not a timing interval.
- S11j/S11k later label telemetry with this flag.

## Feedback

No feedback surface exists. The block is validated and pinned; a regression test asserts that measured pages render no answer correctness, score or reference outcome text.

## Practice

### Persistence (migration 11)

- `observation_mode TEXT NOT NULL DEFAULT 'measured' CHECK (observation_mode IN ('measured','practice'))` on `sessions`, `progress`, `answers` (via `add_columns`; existing rows become `measured`).
- New table:

```sql
CREATE TABLE practice_cases (
    clinician_id   TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id     TEXT NOT NULL,
    arm            TEXT NOT NULL CHECK (arm IN ('ai','no_ai')),
    config_version TEXT NOT NULL,
    config_hash    TEXT NOT NULL,
    started_at     TIMESTAMP NOT NULL,
    completed_at   TIMESTAMP,
    PRIMARY KEY (clinician_id, patient_id)
);
```

  plus triggers refusing DELETE, and UPDATE of anything but a NULL `completed_at`.
- `db/practice.py` is the sole writer. Session, progress and answer DAOs write `observation_mode` on insert only and refuse a write whose mode differs from the stored row, like the S11b provenance guard.

### Case resolution

- `CaseConfiguration` gains `observation_mode`. `resolve_case_configuration`: a pair in `practice_cases` resolves to its pinned version with `observation_mode = practice`.
- `bootstrap_session` takes the arm from `practice_cases`, never from `arm_assignments`.
- `_is_unactivated_phase2_patient` treats a practice pair as a case.
- S11g: a practice case renders by its fixed arm (`InterventionMode.AI` / `NO_AI`).
- Practice cases have no `case_lifecycle` row: no timeout, no pause, no heartbeat. The final advance sets `practice_cases.completed_at` inside the S10 transaction.

### Start

- `POST /practice/start` (never a flag on `/case/start`): resume the clinician's open practice case, else start the first `practice.patient_ids` entry of the **active** configuration they have not started. 409 when practice is disabled, the server is stale, or the list is exhausted.
- The index shows a separate "Practice" section when the active configuration enables practice, or when the clinician holds an open practice case (which stays resumable after a newer version disables practice); practice cases are labelled "Practice".
- No schedule item, no `arm_assignments` row, no replacement, no lifecycle count.

### Cross version rule

`config_history.activate` refuses a version whose measured `patient_ids` intersect the practice `patient_ids` of any registered version, or the reverse. Study wide, so a clinician can never meet a practice patient as a measured one.

### Exclusion

`export-answers` and `divergence-view` read measured rows only (`observation_mode = 'measured'`). S11n defines the practice/QA export.

## Free text

- Cross model validator `validate_study_questions(study, questions)` (pure, `config/`): a declared `study_behaviour` without `free_text.enabled: true` plus any free-text question is an error. Called by `validate-config`, `activate-config`, study mode boot and preflight. Stored snapshots are not re checked.
- Enabled free text keeps the existing length limits; events already omit values; divergence never plots values.
- `export-answers`: with `study_behaviour.free_text.routine_export: exclude`, free-text question columns are omitted (rows stay in the DB); `include_explicit` keeps them with the existing cell guard. Legacy configurations export as today.

## Preflight

Adds FAIL rows for practice patients missing from the dataset and for a failing `validate_study_questions`.

## Files expected to change

`config/study.py`, `config/loader.py` or a new `config/cross_validation.py`, `db/migrations.py`, `db/sessions.py`, `db/progress.py`, `db/answers.py`, new `db/practice.py`, `db/config_history.py`, `web/study_session.py`, `web/gating.py`, `web/routes.py`, `web/case_start.py` (index state), templates (`index.html`, `_summary_card.html`, `_patient_view.html`), `export.py`, `divergence.py`, `cli.py`, `cli_support.py`, `web/app.py`, example config, tests, docs.

## Required tests

### Configuration

1. `study_behaviour` absent keeps historical snapshot bytes.
2. Block without `backward_navigation` rejected.
3. Any feedback flag `true` rejected.
4. Practice/measured overlap rejected.
5. Practice enabled without `randomisation` rejected; `arm: ai` without `ai_intervention` rejected.
6. Changing any policy changes `config_hash`.

### Backward navigation

7. `allow_readonly` renders a prior timepoint with disabled answers and `data-visit-kind="revisit"`.
8. POST answer to a revisited timepoint → 409; POST advance → 412.
9. A revisit writes one `timepoint.revisit` and no `timepoint.enter` / `timepoint.exit`.
10. The frontier renders `data-visit-kind="primary"`.
11. `prohibit` redirects a backward GET to the frontier before slicing (no event, no session write).
12. `prohibit` renders no Prev button.
13. `prohibit` allows the last timepoint of a completed case and redirects earlier ones.
14. An old case keeps its policy after a new version with the other policy activates.

### Feedback

15. Measured pages render no correctness, score or ground truth text.

### Practice

16. Practice disabled → no practice section; `POST /practice/start` → 409.
17. Practice start picks the first unstarted configured practice patient and resumes an open one.
18. Practice start writes no schedule, assignment, lifecycle or replacement row.
19. Practice answers, progress and session carry `observation_mode = practice`.
20. Completing a practice case sets `completed_at` and leaves measured completed/activated counts unchanged.
21. Measured Start case works while a practice case exists; balance state unchanged.
22. A mode changing write is refused by the DAOs.
23. Existing rows migrate as `measured`.
24. Activation refuses cross version practice/measured overlap.
25. Export and divergence exclude practice rows.
26. A practice case renders by its fixed arm.

### Free text

27. Free-text question with `study_behaviour` and free text disabled rejected by `validate_study_questions`, `activate-config`, boot and preflight.
28. Free text enabled renders and saves.
29. Events carry no free-text value.
30. Export omits free-text columns under `exclude` and includes them under `include_explicit`.
31. Divergence never shows free-text values.

### Regression

32. The first use case example keeps practice disabled and all feedback false.
33. All earlier tests green; CI green.

## Explicit non goals

Ground truth model, scoring, recruitment workflow, clinician covariates, telemetry, panel exposure, PP classification, multi tab handling, Phase 2 exports.

## Acceptance

Backward navigation follows the case pinned policy; feedback is recorded and provably absent; practice cases are explicit, routed separately and excluded from every measured count and export; free text is opt in with explicit export treatment; old cases keep their policies; the full suite passes.
