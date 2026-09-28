# Session 11l — AI viewed, intervention integrity, PP status and missing response provenance

## Goal

Turn the immutable randomised arm, server delivery evidence and S11j/S11k telemetry into observation level research variables without touching intention to treat assignment, and give every missing response a structured reason without manufacturing answers.

S11a through S11k are assumed complete.

## Review changes (2026-09-27)

Revised against the code at `7cf9305` and the revised S11j/S11k:

1. **Delivery evidence named concretely.** S11g dropped `render_prepared`. The server already decides per render whether the AI row is shown or why not (`AIUnavailableReason`). S11l records that decision in the S11j `timepoint.render` payload (`ai`, `ai_unavailable_reason`). Browser evidence is the S11k `panel.mount` of the `ai` panel. No `intervention.ai.mounted` event.
2. **Event kinds without a producer dropped** (`intervention.ai.leakage`, `intervention.integrity_failure`, `display_failure` event). Leakage and display failure are **derived** from raw facts; nothing asserts them.
3. **Failure reasons mapped to real causes:** `missing_artifact` ← `missing_row | artifact_mismatch | not_configured`; `render_failure` ← AI panel rendered in `error` state; `display_failure` ← server shown, complete telemetry, no AI `panel.mount` in state `loading|partial`.
4. **No AI PP needs no telemetry.** A no AI render has no AI markup by construction (S11g); only positive leakage evidence breaks compliance.
5. **"Reached"** uses the authoritative gate state (`t < unlocked_t_index`, or the case completed) plus S10 `timepoint.enter` evidence for the frontier, instead of telemetry alone.
6. **Missing reasons made mutually distinct:** `case_abandoned` = the frontier of an incomplete case (reached, unanswered when it ended); `timepoint_never_reached` = timepoints after it; `reached_unanswered` = a reached, non frontier (or completed) timepoint with a visible question left blank; new `pending` for timepoints of an open case. `technical_failure` is reserved: no lifecycle reason is technical today.
7. **Integrity problems are flagged, not raised**, so one bad row never blocks S11n's export.
8. **Scope:** measured `phase2_randomized` cases only; practice and Phase 1 are excluded.
9. **No persistence, no export.** Pure derivation plus a read only loader; S11n renders columns.

## Core invariants

1. The ITT arm is the activated `arm_assignments.arm`; nothing here writes it.
2. AI viewed is defined per `clinician × patient × timepoint` from cumulative primary AI panel exposure (S11k), not from clicks.
3. First use case: viewed ⇔ `>= 2.0` s under the S11k predicate.
4. PP is per observation; one case may mix compliant and non compliant timepoints.
5. Delivery failure stays AI ITT; accidental exposure stays no AI ITT.
6. Missing answers stay missing; no path inserts an answer row.
7. Indeterminate telemetry is never a measured zero.

## Server delivery evidence

`timepoint.render` payload (S11j) gains, for every render:

```
"ai": "legacy" | "none" | "shown" | "unavailable" | "error"
"ai_unavailable_reason": "not_configured" | "artifact_mismatch" | "missing_row"   (only with "unavailable")
```

- `legacy`: Phase 1 / practice free legacy panels (not an S11l observation).
- `none`: measured no AI render, no AI markup.
- `shown`: measured AI render with the pinned row for exactly `t`.
- `unavailable`: measured AI render showing the S11g unavailable state.
- `error`: the AI panel rendered in the `error` state.

Practice renders carry their fixed arm's value; S11l ignores them.

## Observation identity

`study_id × clinician_id × patient_id × t_index`, carrying `session_ids`, `config_version`, `config_hash`, the assigned arm and `case_outcome` (`active|paused|completed|incomplete`, plus the incomplete reason).

Only primary renders feed AI delivery, exposure and PP.

## Derivations (`study_variables.py`, pure)

### Delivery

For AI assigned observations, over primary renders:

- `ai_delivered = True` when some render is `shown` **and** that render has an `ai` `panel.mount` with state `loading|partial` (the S2 normal and partial states).
- `ai_delivered = False` when every render is `unavailable|error`, or a `shown` render has `complete` telemetry and no such mount (`display_failure`).
- otherwise `None` (shown, but telemetry `missing|incomplete|multi_tab` and no mount seen).

For no AI observations `ai_delivered = True` only on leakage evidence, else `False`.

### Failure

`intervention_failure_reasons` (sorted tuple, empty = none): `missing_artifact`, `render_failure`, `display_failure`, `other_integrity_failure` (render `ai` inconsistent with the arm, e.g. `shown` on a no AI case — also leakage). `intervention_failure = bool(reasons)`.

### Leakage

A no AI observation leaks when any primary or revisit render of it has `ai` ∉ {`none`}, or any `panel.*` event with `panel_id="ai"` exists for one of its renders. AI assigned observations never leak.

### AI viewed

AI assigned only; from the S11k primary `ai` summary:

- `ai_exposure_seconds` = measured lower bound
- `ai_viewed` = the S11k `viewed` (`True|False|None`)
- `ai_viewing_status` = the S11k status

No AI: `ai_viewed = None` (not applicable); `ai_exposure_seconds` reports accidental exposure when leakage produced panel events, else `None`.

### PP

- AI: `pp_compliant = ai_delivered is True and ai_viewed is True`.
- No AI: `pp_compliant = not intervention_leakage`.
- `pp_determinate = False` when an AI observation's `ai_delivered` or `ai_viewed` is `None` (conservative `pp_compliant=False` is kept, the flag lets analysis separate "not viewed" from "could not establish").
- Observations whose timepoint was not reached: `pp_compliant = None`.

### Case summaries

`CaseSummary`: AI viewed at least once, AI viewed timepoint count, total primary AI exposure seconds, AI episode count, all reached observations PP compliant, failure and leakage counts. Secondary only; the timepoint rows stay authoritative.

## Missing response provenance

For every question of the case pinned questions at every configured timepoint:

`timepoint_reached`:

- `t_index < unlocked_t_index` (no progress row yet = frontier 0), or the case is completed, or
- `t_index == unlocked_t_index` and an S10 `timepoint.enter` for it exists.

Branch state from the S11h evaluator over that timepoint's stored answers.

Precedence (first match wins):

1. `HIDDEN` → `status=not_applicable`
2. an answer row exists (clinician or rule) → `status=answered`, `answer_source` kept
3. a technical failure reason (reserved; none today) → `technical_failure`
4. reached, the case is `incomplete`, `t_index == unlocked_t_index` → `case_abandoned`
5. reached → `reached_unanswered`
6. the case is `active|paused` → `pending`
7. the case is `incomplete` → `timepoint_never_reached`
8. otherwise (completed but unreached, impossible) → `unknown` + integrity warning

A `DERIVED` question without its rule row is also `unknown` + warning. The lifecycle incomplete reason is always kept separately on the case.

## Integrity checks (flagged on the observation, never raised)

- an answer row whose `arm` differs from the assignment
- render `ai` inconsistent with the arm
- S11j/S11k `invalid` telemetry (non monotonic time)
- render rows whose provenance (`config_hash`) differs from the case

History is never rewritten.

## Loader

`study_variables_reader.py` (service layer, read only, no `web/` import): `load_case_variables(conn, clinician_id, patient_id) -> CaseVariables` reads the assignment, lifecycle, progress, pinned snapshot, answers, S10 enters and render bound telemetry (through `db/telemetry.py`) and calls the pure derivations. Read inside one `BEGIN … ROLLBACK` snapshot.

## Files expected to change

- `web/routes.py` / `web/timing_events.py` (render payload `ai`)
- new `study_variables.py`, `study_variables_reader.py`
- `db/telemetry.py` (loaders)
- tests, checklist, `CLAUDE.md`

## Required tests

### Delivery and failure

1. AI + `shown` + AI mount `loading` → delivered.
2. `missing_row`, `artifact_mismatch`, `not_configured` → `missing_artifact`, not delivered, arm AI.
3. AI panel `error` → `render_failure`.
4. `shown`, complete telemetry, no mount → `display_failure`; incomplete telemetry → delivered `None`.
5. A browser mount cannot make a no AI case AI (arm unchanged, leakage set).

### Leakage

6. No AI without AI evidence → no leakage, PP true.
7. AI panel event on a no AI render → leakage, PP false, arm no AI.
8. Render `ai=shown` on a no AI case → leakage + `other_integrity_failure`.

### AI viewed and PP

9. 1.99 s not viewed; 2.00 s viewed; two episodes summing to 2.00 viewed.
10. Exposure does not carry to the next timepoint.
11. Passive exposure after active time expired still counts.
12. Revisit exposure never satisfies primary viewing.
13. Duration kept with the flag.
14. Incomplete below threshold → viewed `None`, PP false, `pp_determinate` false.
15. Multi tab → indeterminate, not summed.
16. AI delivered + viewed → PP true; delivered + not viewed → false; not delivered → false.
17. One case with one compliant and one non compliant timepoint.
18. Derivation writes nothing (row counts unchanged).
19. Case summary counts.

### Missing responses

20. Clinician answer → answered; rule answer → answered, source rule.
21. Hidden question → not applicable.
22. Completed case, optional question blank → `reached_unanswered`.
23. Incomplete case: frontier → `case_abandoned`, later timepoints → `timepoint_never_reached`, earlier blank optional → `reached_unanswered`.
24. Open case, future timepoint → `pending`.
25. Frontier without S10 enter evidence → not reached.
26. Lifecycle incomplete reason retrievable on the case.

### Integrity

27. Answer arm mismatch flagged.
28. Invalid telemetry flagged.
29. Outputs carry `config_version`/`config_hash`.
30. Practice and Phase 1 cases are not observations.

### Regression

31. All earlier tests green.

## Explicit non goals

Changing ITT, imputation, statistical models, export layout (S11n), privacy/keyfile and multi tab protection (S11m), persisted PP columns.

## Acceptance

S11l is complete when every measured observation is reproducibly classified by immutable arm, delivery, cumulative primary AI exposure, viewed, failure, leakage and PP (with determinacy), failures and leakage never touch ITT, missing responses carry a distinct structured reason without any written answer, hidden questions are never missing, and the full suite passes.
