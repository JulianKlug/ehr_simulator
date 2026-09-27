# Session 11g — AI/no AI intervention delivery, artifact provenance and temporal preflight

## Goal

Deliver the randomised intervention exactly as assigned and preserve enough configuration provenance to identify the frozen AI artifact and presentation used for every activated case.

An AI assigned case shows the configured AI output for the current timepoint. A no AI case has no AI intervention anywhere in the clinician facing response.

S11a through S11f are assumed complete.

## Review changes (2026-09-26)

Revised against the code at `dc32ba0`:

1. **Scope of arm aware rendering.** Only measured Phase 2 cases (`arm_source = phase2_randomized`, and S11i practice cases) render by arm. `phase1_stub` rows carry `arm = "no_ai"` as a placeholder while Phase 1 shows the AI panel; hiding it there would silently change Phase 1. Phase 1 and non study rendering stay unchanged.
2. **Row selection by `model_id`.** `ai_intervention.model_id` selects rows from the canonical `ai_output` frame. The frame is unique on `(patient_id, t_minutes, model_id)`, so "ambiguous duplicate rows" can no longer happen and that check is gone.
3. **Leakage fields moved and namespaced.** `prohibited_clinician_fields` covers all clinical data, not only AI, so it moves out of `ai_intervention` into its own block, with `source:name` identifiers.
4. **All leak surfaces listed.** Besides `_panel_ai.html`: the epic chrome `AI` tab and tabpanel, the dense grid cell, and the summary card `ai: N` count badge.
5. **Geneva/MIMIC have no AI loader** (S7 unshipped). They expose `ai_provenance = None`, so a study with `ai_intervention` on them fails preflight and boot (fail closed). S7 must hash the source bytes.
6. **Dropped** the optional `intervention.ai.render_prepared` event (S11l owns delivery evidence) and the preflight render smoke (pytest covers template behaviour; preflight stays a pure data check).
7. **Defined runtime behaviour** for a missing current row, a case pinned to a different artifact hash, and an AI case pinned to a snapshot without `ai_intervention`.

## Core invariants

1. The activated `arm_assignment` is the only source of truth for AI versus no AI.
2. A measured no AI response contains no AI panel, placeholder, unavailable message, hidden container, heading, tab, count, or payload.
3. Non AI clinical functionality is identical between arms.
4. Every activated case stays pinned to the S11b configuration version active at activation, including its intervention identity.
5. The prediction artifact used by a measured study is frozen and hash identifiable.
6. AI output shown at timepoint `t` is the row for exactly `t`; no earlier or later row substitutes.
7. Clinician facing data must not expose an explicitly prohibited outcome field.
8. Preflight and boot fail closed on a missing or mismatching artifact.
9. S11g never changes an assigned arm because an intervention is unavailable. Runtime failures are classified in S11l.

## Study configuration

Two optional `StudyConfig` blocks, each omitted from canonical serialization when absent, so S11a to S11f snapshots re-render byte for byte and keep their `config_hash`.

```yaml
ai_intervention:
  model_id: "demo_v0"                     # canonical ai_output.model_id to show
  model_system_version: "synthetic_demo_v0"
  prediction_artifact_sha256: "<64 lowercase hex>"
  explanation_artifact_sha256: null       # omitted when null
  presentation_version: "ai_panel_v1"
  intervention_build_id: "ehrsim_ai_2026_09_1"

clinician_facing:
  prohibited_fields:
    - "admission:<field>"
    - "scalar:<variable>"
    - "imaging:<column>"
    - "ai:<payload key>"
```

Values are study inputs; platform code hard codes none of them.

Validation:

- SHA256 fields: exactly 64 lowercase hex characters.
- `model_id`, `model_system_version`, `presentation_version`, `intervention_build_id`: non blank, at most 128 characters.
- `explanation_artifact_sha256` may be null; it is then omitted from serialization.
- `prohibited_fields`: unique entries matching `^(admission|scalar|imaging|ai):[^\s:]+$`. An explicit empty list is allowed: the study declares that nothing is prohibited.
- YAML loading (`load_study_config`, like the S11e balance check) refuses a `randomisation` block without `ai_intervention`. Stored snapshots are not re checked, so S11d to S11f history stays readable.

Any edit to either block changes `config_hash` and so needs a new S11b activation.

## Artifact identity at ingestion

`DatasetLike` gains `ai_provenance: AIArtifactProvenance | None`:

```python
@dataclass(frozen=True)
class AIArtifactProvenance:
    prediction_sha256: str
    explanation_sha256: str | None
    model_system_version: str
```

It describes the artifact actually loaded, not the configured expectation.

- Synthetic: SHA256 of one canonical representation of the generated `ai_output` frame: rows sorted by `(patient_id, t_minutes, model_id)`, each row as `[patient_id, t_minutes, model_id, output_json]`, dumped with `json.dumps(..., sort_keys=True, separators=(",", ":"))`. `model_system_version = "synthetic_demo_v0"`. The representation is documented in the adapter.
- Geneva, MIMIC: `None` (no AI artifact loaded). A future file backed loader (S7) hashes the source bytes before transforming them.
- Never hash rendered HTML.

## Intervention context

A pure resolver produces the rendering context once per request:

```python
class InterventionMode(StrEnum):
    LEGACY = "legacy"  # Phase 1 / non study: every panel, cumulative AI slice
    AI = "ai"          # measured AI case
    NO_AI = "no_ai"    # measured no AI case

@dataclass(frozen=True)
class InterventionContext:
    mode: InterventionMode
    intervention: AIInterventionConfig | None  # from the case pinned snapshot
    loaded: AIArtifactProvenance | None        # from the dataset
```

- Non study mode, and study cases whose assignment is not `phase2_randomized` (or S11i practice): `LEGACY`.
- Measured case: the mode follows the stored arm; `intervention` comes from `CaseConfiguration.study.ai_intervention`, never from the active server configuration.
- No query parameter, form field or cookie can influence it.

## Arm aware rendering

`_render_panels` and the chrome/summary templates receive the `InterventionContext`.

### NO_AI

- `_render_ai` is not called.
- The epic chrome omits the `AI` tab button and its tabpanel; the dense grid omits the cell and its layout reflows.
- The summary card omits the `ai` count badge.
- No `data-panel="ai"`, no AI text in markup, attributes, comments, JSON, aria text or client state.

### AI

- The AI panel renders in the normal order, marked `data-panel="ai" data-intervention="ai"`.
- A selection step after `slice_to_timepoint()` picks, from the already time bounded slice, the single row with `t_minutes == current timepoint` and `model_id == intervention.model_id`. Exact float equality: both come from the same configured minute values.
- Found → rendered with its payload; it shows neither a cumulative history nor other models.
- Missing → the AI panel renders an `unavailable` state ("AI output unavailable for this timepoint"). It never falls back to another row and never renders as no AI.
- Pinned `prediction_artifact_sha256` ≠ loaded hash (case pinned to an older artifact) → `unavailable` state; never another artifact's output.
- Pinned snapshot without `ai_intervention` (development data from before S11g) → `unavailable` state.
- The summary card omits the `ai` count badge as in NO_AI, so the non AI chrome stays identical.

### LEGACY

Unchanged S2 to S11f behaviour.

`_panel_ai.html` never receives the unsliced dataset (S2 data locality rule).

## Boot gate

Study mode boot with an active configuration carrying `ai_intervention` refuses (`StudyStartupError`) when the loaded dataset's `ai_provenance` is missing, or its prediction hash, explanation hash or model/system version differs from the active configuration.

## Clinician facing field inventory

One pure helper in `web/panels.py`, used by preflight and matching what the renderers display:

```python
def exposed_field_ids(patient_slice: PatientSlice, ai_row_payload_keys: Iterable[str]) -> frozenset[str]
```

- `admission:<field>` for every admission row (the admission panel shows all of them).
- `scalar:<variable>` for variables in `VITAL_VAR_SET ∪ LAB_VAR_SET` present in the slice. Other scalar variables are counted but never shown, so they are not exposed.
- `imaging:modality`, `imaging:report_text` when imaging rows exist.
- `ai:<key>` for every key of the selected AI payload.

## Preflight additions

`walk_preflight` keeps its structural checks and adds FAIL rows for:

1. **Artifact identity** (study with `ai_intervention`): `ai_provenance` missing, or any hash/version differs from configuration.
2. **AI timepoint correspondence** (Phase 2 study with `ai_intervention`): for every configured patient and timepoint, exactly one row exists for `(patient, t, model_id)` and it is a JSON object.
3. **Post timepoint guard:** every time varying row in the slice satisfies `t_minutes <= t`. It asserts on the slice output, defending against a slicer regression.
4. **Outcome leakage:** a configured prohibited field is in `exposed_field_ids` for any patient and timepoint. For a Phase 2 study (`randomisation` present) with no `clinician_facing` block, preflight fails: the study must declare its prohibited fields, even as an explicit `[]`.

## Provenance through S11b records

The intervention identity lives in the immutable `configuration_history` snapshot. A case's `config_version` / `config_hash` joins to the prediction and explanation hashes, model/system version, presentation version and build id. No second intervention registry.

## Files expected to change

- `config/study.py`, `config/loader.py`, `config/__init__.py`
- `ingestion/synthetic.py`, `ingestion/geneva.py`, `ingestion/mimic.py`, a small provenance type module
- `web/panels.py`, `web/routes.py`, `web/app.py` (boot gate)
- `web/templates/_panel_ai.html`, `_chrome_epic.html`, `_chrome_dense.html`, `_summary_card.html`
- `cli_support.py`
- `configs/example_phase2_config.yaml`, Phase 2 test fixtures
- tests, `CLAUDE.md`, checklist

## Required tests

### Configuration

1. Valid `ai_intervention` and `clinician_facing` load.
2. Malformed prediction SHA256 rejected.
3. Malformed explanation SHA256 rejected.
4. Blank `model_id`, `model_system_version`, `presentation_version` or `intervention_build_id` rejected.
5. Duplicate or malformed prohibited field identifiers rejected.
6. Omitting both blocks preserves historical snapshot bytes and hash.
7. Changing intervention identity changes `config_hash`.
8. YAML with `randomisation` but no `ai_intervention` is refused; a stored snapshot without it still parses.

### Provenance

9. The synthetic provenance hash is deterministic and matches its documented canonical form.
10. Geneva and MIMIC datasets expose `ai_provenance = None`.
11. Boot refuses on prediction hash mismatch and on missing provenance.

### Arm aware rendering

12. AI case contains exactly one `data-panel="ai"` panel, with `data-intervention="ai"`.
13. No AI case response has no `data-panel="ai"`, AI tab, AI heading, `ai` count badge, AI payload key or model id, in both chromes.
14. No AI case keeps every clinical panel, the questions pane and lifecycle controls.
15. AI case renders the row for exactly `t` and no earlier row.
16. Missing current row renders `unavailable` in the AI arm and never an earlier prediction.
17. Future rows are never rendered.
18. Rows of other `model_id`s are never rendered.
19. A case pinned to an older configuration keeps its intervention identity after a newer one is activated; a pinned hash ≠ loaded hash renders `unavailable`.
20. The arm cannot be changed through query parameters.
21. Phase 1 study mode (`phase1_stub`) and non study mode still render the AI panel as before.

### Preflight

22. Matching provenance passes.
23. Prediction hash mismatch fails.
24. Missing provenance fails.
25. Explanation hash mismatch fails.
26. Model/system version mismatch fails.
27. Missing `(patient, t, model_id)` row fails.
28. A future row injected into the slice output fails the post timepoint guard.
29. Prohibited admission field exposed fails.
30. Prohibited scalar field exposed fails.
31. Prohibited AI payload key exposed fails.
32. A prohibited scalar variable that is never rendered does not fail.
33. Phase 2 study without a `clinician_facing` block fails; explicit `[]` passes.

### Regression

34. All earlier tests stay green; CI stays green.

## Explicit non goals

Conditional questions, practice mode, browser telemetry, panel exposure, AI viewed, PP classification, structured runtime failure events, multi tab handling, Phase 2 exports, a Geneva AI loader (S7).

## Acceptance

A measured AI case renders only the frozen, exact timepoint AI output from its pinned configuration; a measured no AI case has no AI surface at all; Phase 1 is unchanged; boot and preflight refuse artifact mismatch; preflight refuses wrong timepoint AI, future rows and configured outcome leakage; the full suite passes.
