# Session 11h — Conditional question engine and first use case question set

## Goal

Turn the flat question list into a deterministic, server authoritative, branch aware question engine, and ship the first use case question set, without hard coding its question ids into gating or answer capture.

S11a through S11g are assumed complete.

## Review changes (2026-09-26)

1. **No JS mirror.** The original spec asked JavaScript to mirror server state *and* forbade duplicating rules in JS. Now the `/answer` response carries server rendered out of band swaps of every question whose state changed. No condition logic runs in the browser.
2. **One model, two schema versions.** `Questions.schema_version` accepts `"1" | "2"`. v2 only fields are refused under v1 and omitted from serialization when unset, so v1 snapshots re-render byte for byte. No parallel class hierarchy.
3. **Generic stale answer rule** replaces the per question description (it produces the same first use case behaviour) and drops the undefined "unless configured" escape hatch.
4. **Likert per point labels** (`scale_labels`, v2 only): the gate's confidence scale names all five points; the current model has only end labels.
5. **Tighter condition typing:** the source must be an earlier categorical question and `equals` one of its options; `auto_value` targets are categorical, likert or probability.
6. **Numbering by CSS counter**, so hidden questions leave no gap and nothing needs renumbering after a swap.

## Core invariants

1. Branching is configuration driven.
2. The server alone decides visibility, editability, required state and derived answers.
3. The advance gate considers only questions required under the current branch.
4. A hidden question keeps no stored answer.
5. A rule derived answer is distinguishable from a clinician answer.
6. A controlling answer and all dependent writes commit atomically.
7. Refresh, resume and restart rebuild the same branch from stored answers and the case pinned questions.
8. v1 snapshots stay readable and byte stable.
9. An activated case keeps the question configuration it started with.
10. No browser supplied branch state is trusted.

## Schema

```yaml
schema_version: "2"
questions:
  - question_id: deterioration_6h
    response_type: categorical
    options: [Yes, No]
  - question_id: primary_cause
    response_type: categorical
    options: [...]
    display_if: {question_id: deterioration_6h, equals: "Yes"}
  - question_id: death_3mo
    response_type: categorical
    options: [Yes, No]
    auto_value:
      when: {question_id: good_outcome_3mo, equals: "Yes"}
      value: "No"
```

New optional per question fields (v2 only; omitted from serialization when null):

- `display_if: Condition | null`
- `auto_value: {when: Condition, value: str} | null`
- `scale_labels: list[str] | null` (likert only; exactly `scale_max - scale_min + 1` non blank labels)

`Condition = {question_id, equals}`. Validation refuses:

- self, forward or unknown references
- a non categorical source question
- an `equals` value not among the source's options
- `auto_value` on a multi-select or free-text question
- an `auto_value.value` that `serialize_answer` rejects for the target
- any v2 field under `schema_version: "1"`

YAML booleans in `equals`/`value` are coerced like options (`Yes`/`No`).

`load_questions` reports a schema version mismatch against `"1"` or `"2"`.

## Evaluation

Pure module `question_branching.py`:

```python
class QuestionState(StrEnum):
    HIDDEN = "hidden"
    EDITABLE = "editable"
    DERIVED = "derived"

@dataclass(frozen=True)
class EvaluatedQuestion:
    question: Question
    state: QuestionState
    required_now: bool
    value: str | list[str] | None
    source: AnswerSource | None  # clinician | rule

def evaluate(questions: Questions, stored: Mapping[str, StoredAnswer]) -> EvaluatedSet
```

Single pass in file order, against the *effective* values computed so far:

1. `display_if` false → `HIDDEN`, no value.
2. else `auto_value.when` true → `DERIVED`, value = rule value.
3. else → `EDITABLE`, value = the stored clinician answer, if any.

A condition on a hidden or unanswered source is false. `required_now = state == EDITABLE and question.required`.

The pane mode (`open` / `locked`) stays with the S9b frontier and is orthogonal: a locked pane shows the same evaluated states, read only.

Gating (`completeness`, `required_count`, advance payload counts), the pane renderer and answer capture all use `evaluate`. A derived value satisfies nothing because it is never required; a hidden question never blocks.

## Persistence (migration 10)

`answers` gains, through `Migration.add_columns`:

```
answer_source TEXT NOT NULL DEFAULT 'clinician' CHECK (answer_source IN ('clinician','rule'))
derived_from_question_id TEXT
```

`post_sql` adds insert/update triggers refusing `rule` without `derived_from_question_id` and `clinician` with one. Existing rows become `clinician` / NULL.

The DAO gains `commit=False` on `upsert` and `delete_one`, the source columns, and returns the source from `fetch_for_cell` / `fetch_all`.

## Answer submission

One `POST /answer` runs, in one transaction:

1. The existing clinician, case, lifecycle, frontier and provenance checks.
2. Load the stored answers of the cell and evaluate the **current** branch.
3. Refuse (409, badge fragment, nothing written) when the target question is `HIDDEN` or `DERIVED`.
4. Validate and serialize the submitted value (blank = clear).
5. Evaluate the branch **after** the change and reconcile every other question:
   - `HIDDEN` with a stored row → delete it (`reason: branch_invalidated`);
   - `DERIVED` → upsert the rule row unless an identical one exists; this overwrites a clinician row (`reason: auto_value`);
   - `EDITABLE` with a stored `rule` row → delete it (`reason: branch_invalidated`). An older clinician answer is never restored.
6. Write the submitted clinician answer (`reason: user_change`).
7. Append one `answer.upsert` / `answer.clear` event per write, payload `{question_id, response_type, source, reason, ...}`, never a raw value.
8. `conn.commit()`; any failure → `conn.rollback()`, nothing persists. `write_counter` is bumped once after the commit.

A clear of the controlling question re-evaluates the same way.

## Rendering

- `_question.html` renders one question inside a stable slot `<div class="question-slot" id="q-slot-{qid}" data-q-state="...">`. A `HIDDEN` slot is empty.
- `DERIVED` renders the value checked, the fieldset disabled, and a note "Set automatically from an earlier answer"; disabled inputs never submit.
- The `/answer` 200 response appends, next to the badge and the out of band CTA, an `hx-swap-oob` replacement of every *other* slot whose evaluated state or value changed. The submitted question is never re-swapped, so focus and typing survive.
- Question numbers come from a CSS counter over visible `.question` forms.
- No new JavaScript; a Playwright walk (`tests/e2e/test_branching_walk.py`) pins the swaps in a real browser.

## First use case questions

New fixture `configs/example_phase2_questions.yaml` (`schema_version: "2"`), prompts from the Phase 2 gate §19, nothing else:

| id | type | rule |
|---|---|---|
| `deterioration_6h` | categorical `[Yes, No]` | required |
| `confidence` | likert 1..5, `scale_labels` = Not at all / Slightly / Moderately / Confident / Very confident | required |
| `primary_cause` | categorical, placeholder options | `display_if deterioration_6h == Yes` |
| `good_outcome_3mo` | categorical `[Yes, No]`, prompt names mRS 0 to 2 | required |
| `death_3mo` | categorical `[Yes, No]` | `auto_value No when good_outcome_3mo == Yes` |

The cause options are marked in the file as placeholders: the final categories are an open gate item (§26.11). `configs/example_phase2_config.yaml` documents this file; the Phase 1 `example_questions.yaml` stays as is.

## Export and figures

The S9c export decodes rule rows like any stored value; the source columns are exported in S11n (the DB already holds them). `divergence-view` accepts v2 questions unchanged.

## Files expected to change

`config/questions.py`, `config/loader.py`, new `question_branching.py`, `answer_codec.py` (if needed), `db/migrations.py`, `db/answers.py`, `web/answer_capture.py`, `web/gating.py`, `web/routes.py`, `web/templates/_questions_pane.html`, new `_question.html`, `static/theme.css`, the new fixture, tests, docs.

## Required tests

### Schema

1. A historical v1 snapshot parses and re-renders byte identically.
2. A valid v2 conditional config loads.
3. Unknown condition source rejected.
4. Self reference rejected.
5. Forward reference rejected.
6. Non categorical source rejected.
7. `equals` outside the source options rejected.
8. `auto_value.value` invalid for the target rejected; `auto_value` on free-text rejected.
9. v2 fields under `schema_version: "1"` rejected.
10. `scale_labels` of the wrong length rejected.
11. Changing a condition changes `config_hash`.

### Evaluation (pure)

12. Deterioration Yes → cause `EDITABLE` and required.
13. Deterioration No or unanswered → cause `HIDDEN`, not required.
14. Good outcome Yes → death `DERIVED` = No.
15. Good outcome No → death `EDITABLE` and required.
16. A condition on a hidden source is false.

### Service and routes

17. Yes → cause saved → No deletes the cause row atomically, with an `answer.clear` event carrying `reason: branch_invalidated` and no value.
18. Hidden cause does not block advance.
19. Direct POST to the hidden cause → 409, nothing written.
20. Good outcome Yes writes `death_3mo = No` with `answer_source = rule`, `derived_from_question_id = good_outcome_3mo`.
21. The rule death satisfies the gate.
22. Direct POST to the derived death → 409.
23. Yes → No deletes the rule death; death then blocks until answered.
24. A clinician death answer overwritten by the rule is not restored after Yes → No.
25. Clearing the controlling answer re-evaluates dependents.
26. The answer response carries out of band slot swaps for changed questions only.
27. The migration triggers refuse inconsistent source rows.

### Atomicity

28. A failing dependent delete rolls back the controlling write.
29. A failing rule upsert rolls back the controlling write.
30. A failing event append rolls back all writes.
31. `write_counter` is bumped once, after the commit.

### Persistence

32. Branch state survives refresh (GET renders the same states).
33. Branch state survives HTMX timepoint navigation and back.
34. Branch state survives S11e pause and resume.
35. v1 and v2 pinned cases coexist in one database.

### First use case fixture

36. It holds exactly the five questions in order.
37. Deterioration options are exactly Yes and No.
38. Confidence is 1..5 with five labels.
39. Primary cause is categorical with `display_if`.
40. Good outcome and death are Yes/No; death has the `auto_value` rule.
41. No hospital survival, six month death, contributing factors or free text.

### Regression

42. v1 unconditional gating unchanged.
43. All earlier tests green; CI green.

## Explicit non goals

Boolean expression languages, cross timepoint or clinical data conditions, scoring or feedback, practice mode, telemetry, panel exposure, PP classification, Phase 2 exports.

## Acceptance

The server evaluates configured branches deterministically; stale hidden answers are removed in the same transaction; rule answers are provenance marked; gating follows the current branch; refresh and resume rebuild the same state; the first use case fixture holds exactly its five questions; v1 snapshots stay byte stable; the full suite passes.
