# Session 11l — AI viewed, intervention integrity, PP status and missing response provenance

## Goal

Turn the intervention configuration, immutable randomised arm, successful/failed delivery evidence, and S11j/S11k telemetry into observation level research variables without changing intention to treat assignment.

Also derive structured missing response provenance without manufacturing clinician answers.

S11a through S11k are assumed complete.

## Core invariants

1. ITT arm is always the immutable activated assignment from S11d/S11f.
2. No runtime failure, non viewing, leakage, or PP rule may rewrite that arm.
3. AI viewed is defined at `clinician × patient × timepoint` level.
4. AI viewed is based on cumulative qualifying AI panel exposure, not clicks.
5. The first use case threshold is exactly two cumulative seconds under the S11k qualifying exposure definition.
6. PP compliance is observation specific, not only case specific.
7. One case may contain both PP compliant and PP non compliant timepoints.
8. AI delivery failure remains an AI ITT observation.
9. Accidental AI exposure remains a no AI ITT observation.
10. Missing answers remain missing. S11l never imputes a response.
11. Raw events remain authoritative; derived classifications are reproducible pure outputs.
12. Telemetry integrity/missingness must be distinguishable from a confidently measured zero exposure where possible.

## Observation identity

All S11l derivation functions operate at:

`study_id × clinician_id × patient_id × timepoint`

and retain:

- case/session identity
- `config_version`
- `config_hash`
- immutable assigned arm
- visit kind for telemetry inputs

The authoritative PP measure uses the primary visit only.

## Intervention event taxonomy

Add structured event kinds for intervention integrity.

Minimum set:

- `intervention.ai.mounted`
- `intervention.ai.render_failure`
- `intervention.ai.missing_artifact`
- `intervention.ai.display_failure`
- `intervention.ai.leakage`
- `intervention.integrity_failure`

Use existing `events` columns for clinician/session/patient/timepoint plus S11j tab/monotonic fields where browser originated.

Payloads contain categorical reason/identifier metadata only.

Do not include:

- clinician name
- prediction probability values
- raw AI explanation text
- raw clinical values
- answer values

## Successful AI delivery evidence

S11g may record `intervention.ai.render_prepared` when the server successfully prepares the AI HTML.

S11l adds browser acknowledgement:

`intervention.ai.mounted`

Emit it when the AI panel is present and successfully initialised in the browser DOM for an AI assigned observation.

The server validates the observation assignment before persisting it.

A browser cannot turn a no AI assignment into AI by posting this event. If a mounted event arrives for a no AI assignment, persist an integrity/leakage fact and retain the no AI arm.

## AI actually delivered

Derive `ai_delivered` per observation.

For AI assigned observations:

`ai_delivered = true` only when there is trustworthy evidence that the configured AI intervention reached the browser for that observation, normally:

- correct case pinned intervention/configuration
- successful server preparation where recorded
- browser `intervention.ai.mounted`
- no delivery preventing missing artifact/render/display failure for that observation

For no AI observations:

`ai_delivered` should normally be false by design.

If AI is accidentally mounted/exposed in a no AI observation, retain `ai_delivered=true` as a factual exposure signal plus `intervention_leakage=true`; ITT arm remains no AI.

Do not infer successful delivery solely from assignment.

## Structured intervention failure

Derive:

```
intervention_failure: bool
intervention_failure_reason: categorical/list
```

Reasons include at least:

- `render_failure`
- `missing_artifact`
- `display_failure`
- `other_integrity_failure`

If a failure prevents the AI intervention from reaching an AI assigned observation:

- assignment stays AI
- `ai_delivered=false`
- observation cannot be AI PP compliant

A technical failure that also prevents meaningful case completion may interact with S11e incomplete/replacement policy, but it still does not change the original arm.

## Intervention leakage

Derive:

```
intervention_leakage: bool
```

A no AI observation has leakage if any trustworthy evidence shows AI information became clinician facing, including:

- `intervention.ai.mounted`
- AI panel viewport/exposure events
- explicit `intervention.ai.leakage`
- other integrity event proving AI content was exposed

Do not label an AI assigned observation "leakage" merely because AI was shown as intended.

A leakage event preserves the no AI ITT arm and makes that observation non compliant for PP.

## AI viewed derivation

Use S11k primary visit AI panel summary.

For every AI assigned observation:

```
ai_qualifying_seconds = cumulative primary AI panel qualifying exposure
ai_viewed = ai_qualifying_seconds >= panel_viewed_threshold_seconds
```

For the first use case:

```
ai_viewed = ai_qualifying_seconds >= 2.0
```

Examples:

- 1.99 -> false
- 2.00 -> true
- 5.5 -> true

Viewing does not require recent qualifying activity.

Reset is naturally enforced by grouping on timepoint.

Retain continuous duration even when the binary threshold is reached.

### No AI observations

`ai_viewed` should normally be false/not applicable.

Do not encode accidental AI exposure as ordinary compliant `ai_viewed` for a no AI observation. Preserve leakage and any actual AI exposure duration separately.

Recommended export semantics later:

- `ai_viewed` nullable/not applicable for no AI arm
- `ai_exposure_seconds` may still expose accidental duration when leakage occurred

S11n decides final column representation.

## Telemetry completeness

Do not equate missing telemetry with `ai_viewed=false` when the event stream is known incomplete.

Derive a telemetry status such as:

- `complete`
- `incomplete`
- `multi_tab_conflict`
- `not_applicable`

For an AI assigned observation with indeterminate AI panel telemetry:

- preserve ITT AI
- `ai_viewed` may be null/indeterminate rather than false
- PP compliance becomes indeterminate/non compliant according to the locked export/analysis rule

For this implementation, use conservative PP derivation:

```
indeterminate telemetry -> pp_compliant = false
```

while retaining the separate telemetry status so downstream analysis can distinguish "measured not viewed" from "could not establish viewing".

Do not manufacture zero seconds.

## Per protocol classification

Derive:

```
pp_compliant: bool
```

per observation.

### AI assigned

PP compliant only when:

- `ai_delivered == true`
- `ai_viewed == true`

A delivery preventing intervention failure therefore makes PP false through `ai_delivered=false`.

### No AI assigned

PP compliant only when:

- AI remained unavailable as intended
- `intervention_leakage == false`

Equivalent operational rule:

```
not ai_delivered AND not intervention_leakage
```

where accidental AI delivery makes no AI PP false.

### ITT independence

Never update:

- `arm_assignments.arm`
- schedule item planned arm
- activation provenance

based on PP outcome.

PP is a derived analysis variable only.

## Case level summaries

Support derived convenience summaries without replacing the timepoint level authoritative variables.

Examples:

- AI viewed at least once during case
- number of AI assigned timepoints viewed
- total primary AI qualifying seconds
- number of AI viewing episodes
- all observations PP compliant yes/no
- count of intervention failures/leakages

These are secondary derived summaries.

The timepoint observation remains the source for PP classification.

## Missing response provenance

S11l does not write substitute answers.

For every expected question cell in the case pinned question configuration, derive:

- `response_present`
- `timepoint_reached`
- `missing_response_reason` when absent

Recommended missing reasons:

- `technical_failure`
- `case_abandoned`
- `reached_unanswered`
- `timepoint_never_reached`
- `unknown`

### Timepoint reached

A timepoint is reached when trustworthy primary presentation evidence exists, using the server S10 `timepoint.enter` and/or validated S11j primary `browser.timepoint_enter`.

Do not infer "reached" from the mere existence of a configured timepoint.

### Technical failure

If a structured technical/intervention integrity failure is explicitly associated with the observation and plausibly prevented response capture, classify missing reason `technical_failure`.

Do not use this label for every AI render warning that did not prevent answering.

### Case abandoned

If the case ends S11e `incomplete` without a more specific technical failure explaining the missing response, missing later/unfinished responses may be classified `case_abandoned`.

Retain the original structured lifecycle incomplete reason separately:

- reconnection timeout
- pause timeout
- operator abandoned
- any later technical incomplete reason

### Reached but unanswered

If the primary timepoint was reached, the question was required/visible in the effective S11h branch, and no answer exists at termination/export snapshot:

```
missing_response_reason = reached_unanswered
```

### Timepoint never reached

If the timepoint has no primary presentation evidence and the case ended before it:

```
missing_response_reason = timepoint_never_reached
```

### Hidden/non applicable conditional question

A question hidden by the S11h branch is not a missing response.

Represent it as not applicable/branch hidden in later exports, not as unanswered.

A rule generated answer is present, with `answer_source=rule`, not missing.

## Missing response precedence

Use one documented deterministic precedence. Recommended:

1. branch hidden/not applicable -> not missing
2. answer present -> answered
3. explicit response preventing technical failure -> `technical_failure`
4. primary timepoint reached -> `reached_unanswered`
5. case terminal incomplete before the timepoint -> `case_abandoned`
6. otherwise no reach evidence -> `timepoint_never_reached`
7. inconsistent/unclassifiable data -> `unknown` plus integrity warning

This keeps "reached but unanswered" more informative than the generic case abandonment label when both are true.

## Pure derivation module

Create a module such as:

`study_variables.py`

It must not import FastAPI/web rendering code.

Inputs are persisted rows/events/config snapshots.

Outputs are immutable dataclasses for:

- intervention delivery/integrity
- AI viewing
- PP status
- missing response provenance

S11n consumes these pure derivations when building linked exports.

No derived PP/AI viewed value needs to be persisted as a mutable database column in S11l unless caching is later proven necessary.

Recomputability from raw events is preferred.

## Integrity checks

Derivation must refuse or flag impossible combinations, including:

- AI panel normal telemetry in no AI arm without marking leakage
- intervention mounted event for unknown/unactivated case
- event configuration/session provenance inconsistent with the case
- negative exposure duration
- panel event referring to unknown timepoint
- answer row arm different from immutable assignment

Do not silently "fix" such records by rewriting history.

## Files expected to change

- `src/ehr_simulator/db/events.py`
- server/browser intervention acknowledgement hooks
- new pure `study_variables.py` or equivalent
- `src/ehr_simulator/panel_exposure.py` integration
- S11e lifecycle reading helpers where needed
- answer/question evaluation readers from S11h
- tests and documentation
- Session 11 checklist updates

No schema migration is required if the S11j event columns are sufficient. Add one only if a concrete persistence requirement cannot be represented by append only events/config history.

## Required tests

### AI delivered and failures

1. AI assigned + successful mounted evidence derives `ai_delivered=true`.
2. AI assigned with missing artifact derives `ai_delivered=false` and preserves AI arm.
3. AI render failure preserves AI arm.
4. AI display failure preserves AI arm.
5. Other integrity failure is structured and does not rewrite assignment.
6. Browser cannot forge a different arm through a mounted event.

### Leakage

7. No AI observation with no AI events derives no leakage.
8. AI mounted event in no AI observation derives leakage.
9. AI panel exposure event in no AI observation derives leakage.
10. Leakage preserves no AI ITT assignment.
11. Leakage makes no AI PP non compliant.

### AI viewed

12. 1.99 seconds is not viewed at the first use case threshold.
13. 2.00 seconds is viewed.
14. Separate episodes summing to 2.00 seconds are viewed.
15. Exposure at one timepoint does not carry to the next.
16. Passive reading exposure can make `ai_viewed=true` even when active time has expired.
17. Revisit AI exposure does not satisfy primary visit AI viewed.
18. Continuous qualifying duration is retained in addition to the binary flag.

### Telemetry completeness

19. Complete zero exposure is distinguishable from missing telemetry.
20. Missing telemetry does not become a fabricated 0.0 seconds.
21. Multi tab conflict remains marked and does not silently sum durations.
22. Indeterminate AI viewing conservatively produces PP non compliance while retaining telemetry status.

### PP classification

23. AI assigned + delivered + viewed -> PP true.
24. AI assigned + delivered + not viewed -> PP false.
25. AI assigned + not delivered -> PP false.
26. No AI + unavailable + no leakage -> PP true.
27. No AI + leakage -> PP false.
28. PP classification does not update arm assignment.
29. One case can contain one PP compliant and one non compliant observation.
30. Case level convenience summary does not replace the timepoint rows.

### Missing response provenance

31. Existing clinician answer -> response present, no missing reason.
32. Rule generated S11h answer -> response present, source remains rule.
33. Hidden conditional question -> not applicable, not missing.
34. Reached required question with no answer -> `reached_unanswered`.
35. Explicit response preventing technical failure -> `technical_failure`.
36. Incomplete case before later timepoint -> `case_abandoned` or the documented precedence result.
37. Never reached timepoint without stronger reason -> `timepoint_never_reached`.
38. No missing path inserts an answer row.
39. Lifecycle incomplete reason remains separately retrievable.

### Integrity/regression

40. Answer arm mismatch with assignment is refused/flagged.
41. Unknown intervention event case is refused/flagged.
42. Negative panel duration is refused/flagged.
43. All outputs retain `config_version`/`config_hash` attribution through their source context.
44. All S11a through S11k tests remain green.
45. Full CI remains green.

## Explicit non goals

S11l does not implement:

- changing ITT populations
- statistical imputation
- mixed effects models
- primary/secondary endpoint analysis
- final research CSV/file layout
- privacy/keyfile changes planned for S11m
- final multi tab protection planned for S11m
- backup identity changes planned for S11m

## Acceptance

S11l is complete when every measured observation can be reproducibly classified by immutable assigned arm, actual AI delivery, cumulative qualifying AI exposure, AI viewed threshold, intervention failure/leakage, and PP compliance; failures/leakage never rewrite ITT; missing responses remain absent while carrying structured provenance; branch hidden questions are not misclassified as missing; and the complete test suite passes.
