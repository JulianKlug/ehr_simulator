# Session 11g — AI/no AI intervention delivery, artifact provenance and temporal preflight

## Goal

Deliver the randomised intervention exactly as assigned while preserving enough configuration provenance to identify the frozen AI artifact and presentation used for every activated case.

For AI assigned cases, the clinician sees the configured AI intervention for the current timepoint. For no AI cases, the AI intervention does not exist in the clinician facing DOM.

S11a through S11f are assumed complete.

## Core invariants

1. The activated `arm_assignment` remains the only source of truth for AI versus no AI assignment.
2. An AI assigned case may render AI content; a no AI case must not render any AI panel, placeholder, unavailable message, hidden AI container, AI heading, or intervention revealing gap.
3. Non AI clinical functionality must remain equivalent between arms.
4. Every activated case remains pinned to the S11b configuration version active at case activation.
5. AI intervention identity is part of that configuration snapshot and therefore cannot change retroactively for an already activated case.
6. The AI prediction artifact used by a measured study must be frozen and hash identifiable.
7. AI output shown at timepoint `t` must correspond to `t` and must not depend on information after `t`.
8. Clinician facing clinical data must not directly reveal or encode the configured reference outcome.
9. Preflight must fail closed on a missing, mismatching, temporally invalid, or outcome leaking measured intervention.
10. S11g does not change an assigned arm because an intervention is unavailable. Runtime failures are classified in S11l.

## Study configuration

Extend `StudyConfig` with an optional intervention block. The block is omitted from canonical serialization when absent so historical S11a to S11f snapshots continue to round trip byte for byte.

Recommended shape:

```
ai_intervention:
  model_system_version: "stroke_model_release_2026_09"
  prediction_artifact_sha256: "<64 lowercase hex>"
  explanation_artifact_sha256: null
  presentation_version: "ai_panel_v1"
  intervention_build_id: "ehrsim_ai_2026_09_1"
  prohibited_clinician_fields:
    - "<study specific reference outcome field>"
```

The exact values are study inputs. Do not hard code first use case artifact identities into platform code.

Validation requirements:

- SHA256 fields are exactly 64 lowercase hexadecimal characters.
- `model_system_version`, `presentation_version`, and `intervention_build_id` are non blank bounded strings.
- `prohibited_clinician_fields` contains unique non blank field identifiers.
- `explanation_artifact_sha256` may be null when no separate explanation artifact exists.
- Phase 2 randomisation may include AI assignments only when `ai_intervention` is configured.

Any edit to this block changes `config_hash` and therefore requires a new S11b configuration activation before it can govern newly activated cases.

## Artifact identity at ingestion

The dataset adapter must expose provenance for the AI artifact it loaded.

Add an adapter level value equivalent to:

```
AIArtifactProvenance(
    prediction_sha256: str,
    explanation_sha256: str | None,
    model_system_version: str,
)
```

The provenance describes the actual loaded artifact, not merely the expected configuration value.

For file backed prediction artifacts, hash the source artifact bytes with SHA256 before transforming them into the canonical `ai_output` frame.

For synthetic data, use one deterministic canonical artifact representation and document it in the synthetic adapter so the same synthetic prediction set always produces the same identity.

Do not hash a rendered HTML response as the prediction artifact identity.

## Case pinned intervention identity

`resolve_case_configuration()` already resolves the S11b snapshot under which a case was activated. Extend `CaseConfiguration` so downstream rendering can read the intervention block from the pinned study snapshot.

This means:

- an already activated v1 case continues to use v1 AI identity and presentation settings after v2 is activated
- a new case activated under v2 uses v2
- the currently active server configuration must never silently replace a case's pinned intervention identity

No separate mutable "current model" global is allowed in the measured rendering path.

## Arm aware panel rendering

The current rendering pipeline calls `slice_to_timepoint()` and renders all panel types, including `_panel_ai.html`, for every case. S11g changes that contract.

The panel renderer must receive the measured case context, including the immutable arm.

### AI assigned case

When `ctx.arm == "ai"`:

- include the AI panel in the normal panel ordering
- use the intervention configuration from the case pinned snapshot
- display only AI output applicable to the current configured timepoint
- do not display future AI rows
- do not silently substitute an earlier or later prediction when the expected current timepoint prediction is missing
- attach stable non sensitive DOM metadata needed by later telemetry, for example `data-panel="ai"` and `data-intervention="ai"`

For measured Phase 2 rendering, the AI panel should use the row or rows whose `t_minutes` equal the current configured timepoint. The current cumulative `t_minutes <= t` AI slice may remain useful for non study preview utilities, but it must not cause a previous timepoint prediction to masquerade as the current intervention.

### No AI assigned case

When `ctx.arm == "no_ai"`:

- do not call the AI panel template
- do not emit an empty `<section>` for AI
- do not emit `data-panel="ai"`
- do not show "AI unavailable"
- do not reserve a grid cell or fixed height for the missing panel where avoidable
- do not expose AI output in page source, hidden attributes, comments, JSON blobs, accessibility text, or client side state

The non AI interface must use the same clinical data, question pane, navigation rules, lifecycle controls, and visual chrome as the AI arm except for the intervention itself and layout reflow caused by the panel not existing.

## Rendering boundary

Keep the S2/S8 data locality rule: renderers receive only time sliced clinical frames.

For the measured AI panel, add a small intervention selection layer after `slice_to_timepoint()` that selects the prediction applicable to the current configured timepoint from the already time bounded slice.

Do not give `_panel_ai.html` access to the unsliced dataset.

## Temporal validity preflight

Extend the existing `preflight` path rather than creating a second unrelated validation command.

Preflight must walk every configured patient and timepoint and perform the existing structural checks plus the following Phase 2 checks.

### 1. Frozen artifact identity

For a study with `ai_intervention`:

- loaded prediction artifact provenance exists
- loaded prediction SHA256 equals configured `prediction_artifact_sha256`
- explanation hash matches when configured
- loaded model/system version equals the configured version where the adapter exposes it

A mismatch is a hard preflight failure.

### 2. AI timepoint correspondence

For each configured patient and timepoint that can be assigned AI:

- the expected AI prediction exists for that patient and exact timepoint
- no row used for that intervention has `t_minutes > current timepoint`
- duplicate ambiguous intervention rows for one patient/timepoint are refused unless the adapter contract explicitly defines their roles

Do not silently fall back to a previous prediction.

### 3. Post timepoint clinical information

Reuse the same slicing and clinician facing field inventory as runtime rendering.

For every patient/timepoint, prove that time varying clinical rows exposed to renderers satisfy:

```
t_minutes <= current configured timepoint
```

The check must cover scalar, imaging, and any future time varying panel sources.

### 4. Direct outcome leakage

A study must identify clinician facing fields that are prohibited because they directly encode, reveal, or derive from the reference outcome.

Preflight must build the actual clinician facing field inventory and fail if any configured prohibited field is exposed by:

- admission summary data
- scalar panels
- imaging metadata or report fields where applicable
- AI payload keys that directly reveal the reference outcome rather than the permitted prediction
- any future clinician facing panel source

Do not attempt to guess scientific leakage from field names alone. The prohibited field list is study specific and must be explicit.

### 5. Render smoke

Headlessly render both intervention conditions over representative configured cells:

- AI context contains exactly one intended AI panel where data exists
- no AI context contains no AI panel marker or intervention text
- clinical panels remain renderable in both arms

This is additional to, not a replacement for, the structural checks above.

## Clinician facing field inventory

Introduce a pure helper that describes which source fields a renderer may expose. Preflight and runtime rendering should consume the same definitions.

Do not maintain one leakage list in CLI code and a different implicit list in templates.

Where templates transform a source field into a display label, the underlying source field identity must remain available to preflight.

## Intervention provenance retained by existing S11b records

The intervention identity is stored in the immutable configuration snapshot in `configuration_history`.

Because every measured case already carries:

- `config_version`
- `config_hash`
- immutable arm assignment

later exports can join a case to:

- prediction artifact hash
- explanation artifact hash where applicable
- model/system version
- presentation version
- intervention build identifier

S11g therefore does not need a second mutable intervention registry.

If an implementation adds a convenience table, it must be append only or immutable and must never become a competing source of truth with the configuration snapshot.

## Successful render evidence

S11g may add a server event such as:

`intervention.ai.render_prepared`

only after the AI panel HTML has been successfully produced for an AI assigned observation.

Payload may contain stable identifiers such as:

- `t_index`
- `presentation_version`
- `intervention_build_id`

Do not repeat clinician names or raw clinical values in the payload.

This event means "server prepared the intended AI intervention", not "clinician viewed AI" and not necessarily "browser received AI". S11k and S11l provide the later exposure and delivery classifications.

## Error behaviour

During measured operation:

- no AI case plus any attempted AI render is an integrity error
- AI case plus missing expected artifact/output must not silently render a no AI case
- AI case plus artifact identity mismatch must refuse measured study boot or preflight; if a runtime inconsistency still occurs, keep the AI assignment and let S11l record the failure

Do not use the current no data AI messages as a no AI control condition.

## Files expected to change

- `src/ehr_simulator/config/study.py`
- `src/ehr_simulator/config/snapshot.py` if required for canonical omission rules
- `src/ehr_simulator/cli_support.py`
- `src/ehr_simulator/cli.py`
- dataset adapter provenance types and the synthetic/Geneva AI loaders
- `src/ehr_simulator/web/panels.py`
- `src/ehr_simulator/web/routes.py`
- `src/ehr_simulator/web/study_session.py`
- `src/ehr_simulator/web/templates/_panel_ai.html`
- panel/chrome templates where layout assumptions currently reserve AI space
- `src/ehr_simulator/db/events.py` if `intervention.ai.render_prepared` is added
- `configs/example_phase2_config.yaml`
- relevant fixtures, documentation, and tests

## Required tests

### Configuration and provenance

1. Valid intervention metadata loads.
2. Malformed prediction SHA256 is rejected.
3. Malformed explanation SHA256 is rejected.
4. Blank model/system version is rejected.
5. Blank presentation version is rejected.
6. Blank intervention build identifier is rejected.
7. Duplicate prohibited field identifiers are rejected.
8. Omitting `ai_intervention` preserves historical snapshot serialization.
9. Adding or changing intervention identity changes `config_hash`.
10. A case activated under an older configuration resolves the older intervention identity after a newer version is activated.

### Arm aware rendering

11. AI assigned case contains the AI panel.
12. No AI assigned case contains no `data-panel="ai"` marker.
13. No AI HTML contains no AI placeholder.
14. No AI HTML contains no "AI unavailable" message.
15. No AI HTML contains no hidden AI payload.
16. Removing the AI panel does not remove clinical panels, questions, or lifecycle controls.
17. AI rendering uses the case's pinned arm and cannot be changed through query parameters or client input.
18. AI rendering for timepoint `t` uses the prediction for exactly `t`.
19. A previous timepoint prediction is not silently substituted when the current prediction is missing.
20. Future prediction rows are never rendered.

### Artifact preflight

21. Matching frozen prediction hash passes.
22. Prediction hash mismatch fails preflight.
23. Missing loaded artifact provenance fails preflight.
24. Configured explanation hash mismatch fails preflight.
25. Wrong model/system version fails when the adapter exposes that identity.
26. Missing expected patient/timepoint prediction fails preflight.
27. Ambiguous duplicate intervention row fails unless explicitly supported by the adapter contract.

### Temporal validity

28. A time varying clinical row after `t` is not exposed.
29. A deliberately injected future row causes the temporal preflight check to fail when it reaches the clinician facing inventory.
30. A configured prohibited admission field fails preflight.
31. A configured prohibited scalar field fails preflight.
32. A configured prohibited AI payload field fails preflight.
33. A field that is present in the raw dataset but never clinician facing does not fail merely because it exists.

### Regression

34. Phase 1/non randomised preview behaviour remains supported.
35. All S11a through S11f tests remain green.
36. Full CI remains green.

## Explicit non goals

S11g does not implement:

- conditional questions
- practice mode
- browser focus or visibility telemetry
- panel viewing duration
- AI viewed classification
- per protocol classification
- structured runtime intervention failure classification
- multi tab conflict handling
- final Phase 2 exports
- statistical analysis of AI effect

## Acceptance

S11g is complete when a measured AI assignment renders only the frozen, time appropriate AI intervention from the case pinned configuration; a no AI assignment contains no AI intervention surface at all; preflight refuses artifact mismatch, future information, wrong timepoint AI, and explicitly configured outcome leakage; intervention provenance remains reconstructable through `config_version` and `config_hash`; and the complete test suite passes.
