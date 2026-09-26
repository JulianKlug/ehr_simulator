# Session 11h — Conditional question engine and first use case question set

## Goal

Extend the current flat question system into a deterministic, server authoritative branch aware question engine while preserving configuration provenance and answer auditability.

Implement the first use case question logic without hard coding those particular question IDs into the gating or answer capture services.

S11a through S11g are assumed complete.

## Core invariants

1. Question branching is configuration driven.
2. The server, not JavaScript, is authoritative for question visibility, editability, required state, and derived answers.
3. The advance gate considers only questions required under the current evaluated branch.
4. A hidden conditional question cannot retain a stale clinician response unless its configuration explicitly defines that behaviour.
5. A system derived answer is distinguishable from a clinician entered answer.
6. Changing a controlling answer updates dependent question state atomically with the controlling answer write.
7. Refresh and resume reconstruct the same branch from persisted answers and the case pinned question configuration.
8. Historical question configuration snapshots remain readable and byte stable.
9. An already activated case continues using the question configuration version under which it started.
10. No browser supplied branch state is trusted without server re evaluation.

## Questions schema generation

Conditional semantics materially change the question model. Introduce a new questions schema generation rather than silently changing the meaning of schema version 1.

Recommended approach:

- keep schema version 1 parseable for historical S11b snapshots
- add schema version 2 for conditional questions
- expose a common `QuestionsLike` interface to downstream rendering/gating code
- never auto rewrite a stored version 1 snapshot as version 2

`render_questions_snapshot()` and `parse_questions_snapshot()` must continue to round trip historical version 1 bytes exactly.

New first use case configuration uses:

```
schema_version: "2"
```

## Condition model

Use a deliberately small declarative condition language. Do not execute arbitrary Python, JavaScript, Jinja, SQL, or user supplied expressions.

For the first implementation, support equality against a single earlier question:

```
when:
  question_id: deterioration_6h
  equals: "Yes"
```

A condition evaluates against the persisted/effective answer mapping for the current clinician, patient, and timepoint.

### Ordering restriction

A condition may reference only a question that appears earlier in `questions.yaml`.

This provides:

- deterministic single pass evaluation
- no dependency cycles
- simple validation
- stable client rendering order

Configuration validation refuses forward references, self references, unknown question IDs, and incompatible comparison values.

## Question state model

Each question evaluates to one of these effective states:

- `hidden`
- `editable_optional`
- `editable_required`
- `derived`
- `locked` for historical/revisit panes handled by the existing frontier policy

Add pure evaluation code equivalent to:

```
evaluate_questions(questions, saved_answers) -> EvaluatedQuestionSet
```

Each evaluated question should expose at least:

- configured question
- visible yes/no
- editable yes/no
- required_now yes/no
- effective value, if any
- value source: clinician, rule, or none

The gating service, template renderer, and answer capture service must use this shared evaluator.

Do not duplicate condition rules independently in `gating.py`, templates, and JavaScript.

## Configuration fields

Recommended schema version 2 additions on each question:

```
display_if: null | Condition
auto_value: null | AutoValueRule
```

where:

```
AutoValueRule:
  when: Condition
  value: <valid encoded value for this response type>
```

Existing `required: true|false` remains the base requirement.

Effective required state is:

```
visible AND editable AND configured required
```

A derived question is never waiting for clinician input.

The implementation may later generalise the condition language, but S11h must not introduce unbounded expression evaluation.

## First use case question configuration

The Phase 2 first use case question file must contain only the following measured questions.

### 1. Neurological deterioration within six hours

```
question_id: deterioration_6h
response_type: categorical
options: [Yes, No]
required: true
```

There is no Unknown option.

### 2. Confidence

```
question_id: confidence
response_type: likert
scale_min: 1
scale_max: 5
required: true
```

Labels:

1. Not at all confident
2. Slightly confident
3. Moderately confident
4. Confident
5. Very confident

### 3. Primary cause of deterioration

Categorical and required only when:

```
deterioration_6h == Yes
```

The option list remains study specific configuration. Do not encode a cause taxonomy in platform code.

### 4. Good neurological outcome at three months

```
question_id: good_outcome_3mo
response_type: categorical
options: [Yes, No]
required: true
```

Prompt identifies good outcome as mRS 0 to 2.

### 5. Death at three months

```
question_id: death_3mo
response_type: categorical
options: [Yes, No]
required: true
```

When:

```
good_outcome_3mo == Yes
```

the effective answer becomes `No` through a rule generated answer.

When:

```
good_outcome_3mo == No
```

the question requires an explicit clinician response.

The production cause options are supplied by the study configuration and are not decided in this platform spec.

## Questions removed from the first use case

The first use case configuration must not silently inherit the old example questions for:

- hospital survival
- death at six months
- contributing factor multi select
- free notes
- the old probability formulation of good outcome
- any Unknown option on the primary deterioration question

The generic Phase 1 example may remain as a separate demonstration fixture if useful. The Phase 2 first use case fixture must be explicit and separate.

## Answer provenance migration

Add the next schema migration, expected migration 10 after S11f.

Extend `answers` with:

```
answer_source TEXT NOT NULL DEFAULT 'clinician'
    CHECK (answer_source IN ('clinician', 'rule'))
derived_from_question_id TEXT
```

Existing rows backfill as:

```
answer_source = 'clinician'
derived_from_question_id = NULL
```

Rules:

- clinician submitted answer -> `answer_source='clinician'`
- automatic answer -> `answer_source='rule'`
- rule answer records its controlling question in `derived_from_question_id`
- a clinician may not directly POST an answer to a question currently in derived state

Update answer dataclasses and fetch APIs accordingly.

## Atomic branch updates

The current answer DAO commits each upsert/delete independently. S11h needs one service transaction when a controlling answer changes dependent state.

Add `commit=False` support or equivalent internal transaction support to the answer write primitives.

One answer submission must perform, atomically:

1. validate clinician/case/frontier/provenance
2. load current persisted answers
3. validate the submitted answer against the configured question
4. simulate the new controlling value
5. evaluate all dependent states
6. write the submitted clinician answer
7. clear stale hidden dependent answers where required
8. create/update/delete rule generated answers as required
9. append the corresponding audit events
10. commit once

On any failure, none of the above writes persist.

## Stale hidden answer policy

S11h locks the following first use case behaviour.

### Deterioration cause

If the clinician changes:

```
deterioration_6h: Yes -> No
```

then any persisted `primary_cause` answer is deleted in the same transaction.

It must not:

- remain hidden but persisted
- count toward exports as the current branch answer
- block or satisfy the advance gate

Append an `answer.clear` event with a reason identifying branch invalidation, not the raw answer value.

### Death at three months

If:

```
good_outcome_3mo = Yes
```

persist:

```
death_3mo = No
answer_source = rule
```

If the clinician later changes:

```
good_outcome_3mo: Yes -> No
```

clear the rule generated death answer. The clinician must then answer `death_3mo` explicitly.

Do not restore an older hidden clinician death answer automatically.

This prevents stale branch state from silently reappearing.

## Server side write protection

`POST /answer` must refuse:

- hidden question
- derived question
- unknown question
- question outside the case pinned configuration
- question at a locked timepoint

The client showing or hiding an element is never sufficient authorisation.

## Gating changes

Replace the current static completeness rule:

```
q.required and q.question_id not in saved
```

with the shared evaluated branch.

A timepoint is complete when every evaluated question with:

```
required_now == true
```

has a valid effective answer.

Rule generated answers count as effective answers for the gate.

Hidden questions never block.

`required_count()` and the advance CTA counts must use the active branch, not the entire static question list.

## Client behaviour

Add a small external JavaScript module, for example `conditional_questions.js`, loaded under the existing CSP.

Responsibilities:

- mirror server evaluated state immediately after a local answer change for responsive UX
- hide/show dependent question containers
- make derived controls read only
- update displayed derived values
- trigger/consume the normal autosave response
- re render from server state after any HTMX response

The browser implementation is a UX mirror only. Refreshing the page must produce the same state from persisted server data without relying on browser memory.

Do not put executable condition expressions in HTML.

Safe data attributes may carry question IDs and already validated condition metadata where needed.

## Refresh and resume

On every render:

1. load the case pinned `Questions` snapshot
2. load persisted answers for the current cell
3. evaluate the branch server side
4. render only the effective state

This makes conditional state survive:

- autosave
- full refresh
- HTMX timepoint swap
- reconnect/resume inside S11e grace
- server restart

## Configuration provenance

Changing any of the following changes the questions snapshot and `config_hash`:

- condition target
- condition comparison value
- automatic answer rule
- question wording
- option list
- required flag

An already activated case continues using its original conditional logic.

## Events

Reuse existing `answer.upsert` and `answer.clear` events.

Add non sensitive payload metadata where useful:

```
source: clinician | rule
reason: user_change | branch_invalidated | auto_value
```

Do not put raw free text or raw answer values into event payloads.

## Files expected to change

- `src/ehr_simulator/config/questions.py`
- `src/ehr_simulator/config/loader.py`
- `src/ehr_simulator/config/snapshot.py`
- `src/ehr_simulator/answer_codec.py`
- `src/ehr_simulator/db/migrations.py`
- `src/ehr_simulator/db/answers.py`
- `src/ehr_simulator/web/answer_capture.py`
- `src/ehr_simulator/web/gating.py`
- `src/ehr_simulator/web/routes.py`
- `src/ehr_simulator/web/templates/_questions_pane.html`
- new pure conditional evaluation module
- new external conditional question JS
- first use case Phase 2 questions fixture/config
- schema fixture, tests, and documentation

## Required tests

### Schema and validation

1. Historical schema v1 questions snapshot still parses and re renders byte identically.
2. Valid schema v2 conditional config loads.
3. Unknown condition source question is rejected.
4. Self reference is rejected.
5. Forward reference is rejected.
6. Condition value not valid for the source question is rejected.
7. Auto value not valid for the target response type is rejected.
8. Changing conditional logic changes `config_hash`.

### Deterioration branch

9. `deterioration_6h=Yes` displays primary cause.
10. Primary cause is required when deterioration is Yes.
11. `deterioration_6h=No` hides primary cause.
12. Hidden primary cause does not block advance.
13. Yes -> saved cause -> No clears the cause row atomically.
14. Clearing a stale cause emits an audit event without the raw answer value.
15. Direct POST to hidden cause is refused.

### Good outcome/death branch

16. `good_outcome_3mo=Yes` creates `death_3mo=No` as `answer_source=rule`.
17. Rule generated death satisfies the gate.
18. Direct POST to the derived death question is refused.
19. `good_outcome_3mo=No` exposes editable death response.
20. Yes -> No clears the rule generated death value.
21. After Yes -> No, death blocks until the clinician explicitly answers it.
22. An old pre branch clinician death answer is not silently restored.

### Atomicity

23. Failure while clearing a dependent answer rolls back the controlling answer write.
24. Failure while writing a derived answer rolls back the controlling answer write.
25. Failure while appending audit events rolls back all branch state writes.
26. Successful branch update increments the write counter only after the outer commit.

### Persistence and resume

27. Conditional state survives refresh.
28. Conditional state survives HTMX timepoint navigation and return where policy permits.
29. Conditional state survives S11e reconnect/resume.
30. Case pinned v1 and v2 question configurations can coexist in one study database.

### First use case configuration

31. Primary deterioration options are exactly Yes and No.
32. Confidence is a five point Likert scale.
33. Primary cause is configured as categorical.
34. Good outcome is Yes/No.
35. Death at three months is Yes/No.
36. Hospital survival is absent.
37. Six month death is absent.
38. Contributing factors are absent.
39. Free notes are absent.

### Regression

40. Existing unconditional v1 question gating remains correct for historical/Phase 1 configurations.
41. All S11a through S11g tests remain green.
42. Full CI remains green.

## Explicit non goals

S11h does not implement:

- arbitrary Boolean expression languages
- cross timepoint conditions
- conditions based directly on clinical data
- scoring or correctness feedback
- practice mode
- browser telemetry
- panel exposure
- PP classification
- final Phase 2 exports

## Acceptance

S11h is complete when the server can deterministically evaluate configured question branches, stale hidden answers are handled transactionally, automatic answers are explicitly provenance marked, gating follows only the current active branch, refresh/resume reproduce the same state, the first use case contains only its five intended questions, historical v1 question snapshots remain readable, and the complete test suite passes.
