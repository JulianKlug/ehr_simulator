# Session 08 — Real data UI on Geneva

## Goal

Make the existing patient UI clinically complete and usable with real Geneva data.

After S8, a clinician can open a Geneva study, walk the configured timepoints of a real patient, and see all study relevant Geneva variables intentionally organised into clinical panels, together with the Geneva AI prediction when the active case permits AI exposure.

The central contract is:

`Geneva adapter output → time bounded PatientSlice → clinically grouped read only presentation`

No clinician facing surface may reveal a value from a timepoint later than the currently resolved study timepoint.

S8 is a **clinical data presentation session**. It does not introduce new EHR writes. The answer, gating, timing, randomisation, telemetry, and case lifecycle machinery already shipped in later sessions remains unchanged.

---

## Context and compatibility with shipped sessions

The original roadmap predates substantial implementation work. Several items originally assigned to S8 already exist on `main`.

### Already implemented and therefore not S8 work

1. **Geneva dataset selection**

   `build_dataset_loader()` already routes `dataset: geneva` through `load_geneva()`.

   Study patient ids, practice patient ids, and patients belonging to older pinned configurations are filtered before the loaded dataset is retained.

2. **Validate once, cache once**

   `web/app.py` already loads the dataset exactly once during the FastAPI lifespan and stores it in:

   `app.state.dataset`

   Requests read the cached frames. They do not reload or revalidate the Geneva CSV.

3. **Central time locality choke point**

   `web/panels.py::slice_to_timepoint()` already performs the canonical:

   `t_minutes <= current_t`

   filtering for scalar, imaging, and AI frames before renderers receive data.

4. **Study defined timepoints**

   `app_from_study_config()` already stores `study.timepoints_minutes` in `app.state.study_timepoints`, and the route resolves `t_index` against those configured timepoints rather than Geneva's complete 72 hour timeline.

5. **Geneva AI ingestion**

   S7 already loads Geneva prediction and SHAP artifacts into canonical `AI_OUTPUT`, with provenance checks and exact timepoint matching for measured AI cases.

6. **AI versus no AI intervention behaviour**

   S11g already decides whether the AI panel is absent, rendered from the exact current timepoint, or shown as unavailable.

7. **Question gating and study lifecycle**

   S9 to S11 already own answers, progress, locked historical panes, randomisation, timing, telemetry, practice cases, and case provenance.

S8 must preserve all of these contracts.

---

## What remains for S8

The remaining gap is not primarily loading Geneva. It is **presenting Geneva correctly**.

The synthetic UI currently recognises only:

`hr, sbp, dbp, rr, spo2, temp`

as vitals, and:

`hgb, na, cr, glucose, wbc, plt`

as laboratory variables.

Geneva contains substantially more clinically relevant information. Much of it already reaches `scalar_ts` or `admission`, but is silently excluded from the clinician facing panels.

S8 closes that gap.

---

# 1. Geneva clinical variable contract

Every variable in the agreed Geneva study inventory must have an explicit clinician facing disposition.

The allowed dispositions are:

1. `vitals`
2. `neurological_support`
3. `labs`
4. `admission_baseline`
5. `admission_treatment`
6. `imaging`
7. `ai`
8. `intentionally_hidden`

For the variables listed for this study, `intentionally_hidden` must be empty unless an explicit study validity or privacy decision is added to this spec.

A variable must never disappear merely because its raw Geneva name is not part of an old synthetic era allowlist.

## 1.1 Vitals and bedside measurements

Render:

| Clinical variable | Geneva source form | UI representation |
|---|---|---|
| Heart rate | `min/median/max_heart_rate` | min to max range + median line |
| Mean blood pressure | `min/median/max_mean_blood_pressure` | min to max range + median line |
| Diastolic BP | `min/median/max_diastolic_blood_pressure` | min to max range + median line |
| Systolic BP | `min/median/max_systolic_blood_pressure` | min to max range + median line |
| Oxygen saturation | `min/median/max_oxygen_saturation` | min to max range + median line |
| Respiratory rate | `min/median/max_respiratory_rate` | min to max range + median line |
| Temperature | `temperature` | timeline |

Placed elsewhere, each in exactly one location:

1. FiO2 → `Neurological / Support` (§2)
2. Glucose → Labs (§3)
3. Weight → admission `Demographics` (§4.1). Geneva weight is a `stroke_registry` field (fixture `geneva_fixture_expected.json`, adapter docstring), so it lands in `ADMISSION`, not `SCALAR_TS`.

The display grouping is a UI decision and does not need to reproduce the source document's category boundaries exactly.

### Aggregate rule

Geneva's hourly aggregate triplets are one clinical measurement family, not three separate variables.

Canonical aliases should therefore distinguish:

`hr_min`, `hr`, `hr_max`

rather than displaying `min_heart_rate`, `median_heart_rate`, and `max_heart_rate` as three unrelated charts.

Apply the same convention to BP, oxygen saturation, respiratory rate, and NIHSS.

If only the median exists, render the median line normally.

If median plus only one bound exists, render the line and available bound without fabricating the missing value.

Never interpolate a missing min, median, or max value.

## 1.2 Canonical scalar ids

The adapter (`_VARIABLE_RENAMES`) owns the source → canonical mapping. The web layer sees canonical ids only.

| Canonical id(s) | Geneva source |
|---|---|
| `hr_min`, `hr`, `hr_max` | `min/median/max_heart_rate` |
| `map_min`, `map`, `map_max` | `min/median/max_mean_blood_pressure` |
| `sbp_min`, `sbp`, `sbp_max` | `min/median/max_systolic_blood_pressure` |
| `dbp_min`, `dbp`, `dbp_max` | `min/median/max_diastolic_blood_pressure` |
| `spo2_min`, `spo2`, `spo2_max` | `min/median/max_oxygen_saturation` |
| `rr_min`, `rr`, `rr_max` | `min/median/max_respiratory_rate` |
| `nihss_min`, `nihss`, `nihss_max` | `min/median/max_NIHSS` |
| `temp` | `temperature` (unchanged) |
| `gcs` | `Glasgow Coma Scale` |
| `fio2` | `FIO2` |
| labs | §3 table |
| imaging derived | unchanged raw ids (§5.1, already snake case) |

The median keeps the bare id so existing `hr`/`sbp`/`dbp`/`rr`/`spo2` consumers are unchanged.

## 1.3 Real data source verification

The committed fixture contains only two EHR variables (`creatinine`, `max_heart_rate`). The inventory above is taken from the real label list pinned in `tests/test_geneva.py::_ALL_GENEVA_SAMPLE_LABELS`, which does not record each label's `source`.

Commit 1 adds a `real_data` test asserting, on the real CSV, that:

1. every §1.2 / §3 / §5.1 source label arrives in `SCALAR_TS`;
2. `weight` arrives in `ADMISSION`;
3. the four binary imaging variables (`hypoperfusion_*`, `vascular_*`), which have no normalisation parameters and no categorical encoding, arrive in `SCALAR_TS` with values in `{0, 1}`.

If any assertion fails, stop and amend this spec before building panels on it.

---

# 2. Neurological and respiratory support panel

Add a sixth clinical panel:

**Neurological / Support**

This panel contains:

1. NIHSS
2. Glasgow Coma Scale
3. FiO2

NIHSS uses its Geneva min, median, and max values as range bars (§11), tolerating missing bounds.

GCS and FiO2 render as standard timelines.

FiO2 lives here, not in vitals, to keep the vitals figure from becoming overloaded. Moving it later is a separate decision.

New template:

`web/templates/_panel_neuro_support.html`

New panel name:

`neuro_support`

## 2.1 Panel id is a cross-cutting vocabulary change

The panel id set is closed in several shipped layers. Adding `neuro_support` must extend every one in the same commit, otherwise the S11k browser posts a `panel.*` event that `extra="forbid"` validation refuses with 422, and the **whole batch** (including AI panel exposure) is lost.

| Layer | Change |
|---|---|
| `web/panels.py` | `PanelName`, `panel_states`, `panel_errors` |
| `web/telemetry.py` | `PanelId` Literal |
| `panel_exposure.py` | `PANEL_IDS` (also drives `export_phase2` `panel_summaries.csv`) |
| `web/templates/_chrome_epic.html` | `Neuro / Support` tab after `Vitals` |
| `web/templates/_chrome_dense.html` | section after vitals |
| `tests/e2e/` | walks that enumerate tabs or panels |

Semantics of telemetry, exposure and export do not change; only the vocabulary grows. Renders recorded before S8 simply have no `neuro_support` rows.

---

# 3. Laboratory panel

The laboratory table must support the complete Geneva laboratory set rather than the current six variable subset.

Canonical display ids:

| Display id | Geneva variable |
|---|---|
| `hct` | `hematocrite` |
| `hgb` | `hemoglobine` |
| `hba1c` | `hemoglobine glyquee` |
| `cr` | `creatinine` |
| `k` | `potassium` |
| `na` | `sodium` |
| `cl` | `chlore` |
| `urea` | `uree` |
| `calcium` | `calcium corrige` |
| `phosphate` | `phosphates` |
| `alt` | `ALAT` |
| `ast` | `ASAT` |
| `bilirubin` | `bilirubine totale` |
| `triglycerides` | `triglycerides` |
| `hdl` | `cholesterol HDL` |
| `ldl` | `LDL cholesterol calcule` |
| `cholesterol` | `cholesterol total` |
| `wbc` | `leucocytes` |
| `plt` | `thrombocytes` |
| `rbc` | `erythrocytes` |
| `neutrophils` | `neutrophiles-nb abs` |
| `inr` | `INR` |
| `ptt` | `PTT` |
| `fibrinogen` | `fibrinogene` |
| `probnp` | `proBNP` |
| `crp` | `proteine C-reactive` |
| `lactate` | `lactate` |
| `glucose` | `glucose` |

`LAB_VAR_SET` grows accordingly.

The UI remains tabular. Do not add one plot per laboratory variable.

### Table behaviour

Rows follow the table order above (registry `display order`), not alphabetical. Only labs with at least one value at or before `t` get a row.

Columns are the distinct observation times (`t_minutes`) of any lab row at or before `t`, ascending, as today. They are **not** the configured study timepoints: binning observations into study intervals would change the displayed values.

Up to 72 hourly columns can exist, so the table sits in a horizontal `overflow-x: auto` container with a sticky first (label) column. The column at the current `t` (when present) carries `aria-current="time"`.

Cells with no measurement at a column display `—`.

Sparse laboratory sampling is normal and never produces a warning. Panel state rules are in §10.

---

# 4. Baseline and treatment presentation

The current admission panel renders an unstructured list of `field/value` pairs.

For Geneva, retain the canonical `ADMISSION` frame unchanged but group its fields for display.

## 4.1 Patient baseline

Sections:

### Demographics

1. Age
2. Sex
3. Weight

### Prestroke medication

1. Anticoagulants
2. Antihypertensive drugs
3. Antiplatelet drugs
4. Lipid lowering drugs

### Medical history

1. Atrial fibrillation
2. Coronary disease
3. Diabetes
4. Previous cerebrovascular event
5. Hyperlipidaemia
6. Hypertension
7. Smoking
8. Peripheral artery disease

### Stroke context

1. Wake up stroke
2. Prestroke disability, mRS

Raw registry names remain valid canonical `ADMISSION.field` values. Presentation labels belong in a display registry rather than in Jinja conditionals.

Registry keys are the exact Geneva field names, e.g. `Antihypert. drugs pre-stroke`, `MedHist CHD`, `Prestroke disability (Rankin)`, `categorical_IVT`, `wake_up_stroke`, `age`, `weight`.

### Other

Any `ADMISSION.field` absent from the registry (synthetic, MIMIC, or a new Geneva field) renders in a final **Other** section, alphabetical, under its raw name. Nothing in `ADMISSION` is hidden, so `exposed_field_ids()` keeps its "every admission field" rule.

## 4.2 Treatment and referral

Add a clearly separated **Treatment / presentation** subsection inside the admission panel:

1. IAT timing
2. IVT timing
3. Onset to admission timing
4. Referral

No inference is made from these categorical values.

The decoded adapter value is displayed as supplied.

---

# 5. Imaging panel

Geneva intentionally has an empty canonical `IMAGING` frame.

This does **not** mean Geneva has no imaging information.

The study uses imaging derived scalar variables in `scalar_ts`.

The existing Geneva UI must therefore stop presenting an empty canonical `IMAGING` frame as equivalent to "no imaging information."

## 5.1 Imaging derived scalar variables

Render:

### Perfusion volumes

1. Tmax > 4 s
2. Tmax > 6 s
3. Tmax > 8 s
4. Tmax > 10 s
5. CBF < 20%
6. CBF < 30%
7. CBF < 34%
8. CBF < 38%
9. CBV < 34%
10. CBV < 38%
11. CBV < 42%

### Imaging / vascular findings

1. Hypoperfusion with mismatch
2. Hypoperfusion without mismatch
3. Vascular occlusion
4. Vascular stenosis > 50%

Corresponding Geneva variables (real label list in `tests/test_geneva.py`; **not** in the committed fixture, see §1.3):

`tmax_gt_4`, `tmax_gt_6`, `tmax_gt_8`, `tmax_gt_10`

`cbf_lt_20`, `cbf_lt_30`, `cbf_lt_34`, `cbf_lt_38`

`cbv_lt_34`, `cbv_lt_38`, `cbv_lt_42`

`hypoperfusion_with_mismatch`

`hypoperfusion_without_mismatch`

`vascular_occlusion`

`vascular_stenosis_over_50p`

## 5.2 Rendering

Continuous perfusion volumes render as a compact timepoint table.

Boolean vascular findings render as labelled Yes / No values.

If canonical imaging reports are also present for a future dataset, the existing report rendering remains available below the derived values.

For Geneva, an empty `IMAGING` frame must not force the entire imaging panel into `empty-expected` when imaging derived scalar data exist.

Panel state calculation therefore considers both:

`PatientSlice.imaging`

and the Geneva imaging derived scalar subset in:

`PatientSlice.scalar_ts`

---

# 6. Geneva AI panel

S7 changed the real AI payload contract.

The current synthetic panel expects:

`prob_deterioration_6h`

and:

`prob_mrs_0_2_90d`

The Geneva S7 payload instead contains:

`probability`

and optionally:

`explanation.base_value`

`explanation.contributions`

A Geneva explanation may contain more than one thousand SHAP contributions.

The existing generic dictionary renderer is therefore not acceptable for real Geneva data.

## 6.1 Probability

The Geneva template displays:

**Predicted probability**

formatted as a percentage to one decimal place.

The underlying full precision value remains unchanged in `AI_OUTPUT`.

Also display the `model_id` in secondary text.

Do not infer or hard code an outcome description. Instead, `ai_intervention` gains an optional `outcome_label` (non-empty string, e.g. `"poor functional outcome at 3 months"`), omitted from serialization when absent so existing snapshots and hashes do not move.

1. Present → heading **Predicted probability of {outcome_label}**.
2. Absent → **Predicted probability**, and preflight WARNs for a Phase 2 study.
3. Read from the case's **pinned** snapshot, like the rest of `ai_intervention`.
4. Legacy (non-measured) rendering has no pinned intervention and shows **Predicted probability**.

## 6.2 Explanation

If an explanation exists, show:

**Top model contributors**

Select deterministically:

1. five largest positive SHAP contributions
2. five most negative SHAP contributions

Sort each group by absolute magnitude descending, then feature name ascending as the tie breaker.

Rules:

1. Positive means `> 0`, negative `< 0`. Zero contributions are never shown.
2. A group with fewer than five non-zero entries shows what exists; it is not padded.
3. Ranking runs over **every** contribution in the row. No feature (including `timestep_idx`) is excluded, so the display never diverges from the model's own ranking.
4. Selection is one pure function shared by the renderer and preflight (§15), e.g. `web/ai_payload.py::top_contributors(payload) -> Contributors`.

### Contributor labels

SHAP feature names are model features, not canonical variables (fixture: `avg_heart_rate`, `lag2_glucose`, `timestep_idx`, `ALAT`).

1. Commit 3 first enumerates the real `shap_feature_names.pkl` and records its naming grammar (prefixes such as `avg_`, `lagN_`, `min_`/`max_`/`median_`, and specials such as `timestep_idx`) in this spec.
2. `clinical_fields.py` resolves a feature to `<transform> <clinical label>` when the base name maps to a registry entry. Example: `lag2_glucose` → `Glucose (2 h earlier)`. `timestep_idx` → `Time since admission`.
3. An unresolved feature renders its raw name in `<code>`. It is never dropped.
4. A registry test asserts the share of resolved real feature names (`real_data`) and lists the unresolved ones in the failure message.

Labels:

`increases model output`

and:

`decreases model output`

SHAP contributions are model explanation values, not causal effects. The UI must not label them as causes.

Do not render:

1. all 1,134 contributions
2. the complete raw JSON object
3. `y_true`
4. hidden model artifacts
5. future SHAP rows

`base_value` does not need to be clinician facing in S8.

An accessible table contains the same ten displayed contributors.

## 6.3 Payload state

Replace the single synthetic `_AI_REQUIRED_KEYS` assumption with payload family validation.

Synthetic payload:

requires both existing synthetic probability fields.

Geneva S7 payload:

requires a finite `probability` in `[0, 1]`.

`explanation` is optional because S7 explicitly permits a prediction artifact without an explanations directory.

If `explanation` exists:

1. `base_value` must be numeric
2. `contributions` must be a mapping of feature name to finite numeric value

A valid probability without an explanation is a normal state, not `partial`.

Family detection, in order:

1. has `probability` → Geneva family;
2. has every synthetic key → synthetic family;
3. otherwise → `partial` (as today for missing keys).

A Geneva payload failing its checks (non-finite or out-of-range probability, malformed explanation) is `partial`, and the panel shows only fields that passed. JSON that is not an object stays `error`.

`_ai_panel_state` and `measured_ai_state` call the same validator, so legacy and measured paths agree.

## 6.4 Intervention compatibility

Do not change S11g intervention selection.

For measured AI cases, the panel receives only the configured model row at exactly the current timepoint.

For measured no AI cases, no AI panel markup exists.

For legacy mode, cumulative AI behaviour remains backward compatible for synthetic payloads (every row at or before `t`).

For Geneva payloads in legacy mode:

1. the latest row (greatest `t_minutes <= t`) renders probability + contributors;
2. earlier rows render as a compact probability history table (time, probability), no contributors.

Rendering every earlier row's explanation would multiply the bounded output by the number of rows.

---

# 7. Clinical display metadata

Add one explicit metadata module rather than spreading source labels and display names through route code and templates.

Recommended:

`src/ehr_simulator/web/clinical_fields.py`

It owns:

1. clinical display label
2. UI group
3. display order
4. formatting hint
5. source or canonical variable id
6. aggregate family where applicable
7. SHAP feature name resolution (§6.2)

Layering: `clinical_fields.py` is pure data + lookups with no `web` request or template imports, so `cli_support` (preflight) and `divergence.py` may import it.

The Geneva adapter remains responsible for source vocabulary conversion.

The web layer consumes stable canonical ids wherever possible.

Every Geneva variable listed in the study inventory must be covered by a registry test.

Unknown scalar variables remain in the canonical frame but are not silently promoted into a clinician panel. They are still visible to ingestion diagnostics and divergence tooling.

---

# 8. Accepted ranges

The accepted ranges supplied with the Geneva variable inventory are a **data contract and fixture constraint**, not a rendering clamp.

S8 must not:

1. clip a value to an accepted minimum or maximum
2. replace an out of range value with the boundary
3. silently hide an out of range adapter approved value
4. add clinical warning colours solely from these broad ingestion limits

If range enforcement is desired, it belongs at ingestion or study validation and requires a separate decision because warning clinicians about values can change study behaviour.

S8 may use the accepted ranges when building realistic UI fixtures and tests.

---

# 9. Timepoint locality

The existing `slice_to_timepoint()` choke point remains the only path from unsliced data to panel renderers.

This is the non negotiable S8 regression contract.

For a request whose resolved timepoint is `t`:

`scalar_ts.t_minutes <= t`

`imaging.t_minutes <= t`

`ai_output.t_minutes <= t`

must hold for every row accessible to any renderer.

No renderer receives the full dataset.

No template receives the full dataset.

No chart helper receives the full dataset.

No AI explanation helper reads another timepoint's row.

## 9.1 Fix `PatientSlice.timepoints`

The route already uses study defined timepoints, but `PatientSlice.timepoints` is still populated from dataset derived timepoints.

No renderer reads the field today (chart x ranges come from the sliced rows), so there is no live leak. But the dataset derived tuple spans the patient's **whole** stay, so the first renderer to use it would reveal length of stay (and so possibly death). Fix it before anything depends on it.

Change:

`slice_to_timepoint(...)`

to accept the resolved timepoint sequence explicitly.

Conceptually:

`slice_to_timepoint(dataset, patient_id, t_minutes, t_index, *, timepoints=None)`

If `timepoints` is supplied, `PatientSlice.timepoints` is exactly that tuple.

If absent, preserve the current dataset derived fallback for synthetic non study mode.

Study mode always passes `resolved.timepoints`.

After S8 there is one consistent meaning for:

`PatientSlice.timepoints`

the sequence the current UI walk is actually using.

The two other callers (`cli_support.py` preflight walk and preview) pass the study timepoints too.

## 9.2 Future existence must not be clinician facing

`slice_to_timepoint` legitimately inspects unsliced data to tell `empty-expected` (no data ever) from `empty-unexpected` (data later). Today the templates print that distinction:

- `No labs recorded for this patient.` reveals that no lab will ever be drawn;
- `No labs at this timepoint yet (data exists later).` reveals that one will be.

With sparse Geneva labs and AI that is a frequent, clinically meaningful leak.

After S8:

1. Visible text and `aria-label` are identical for both empty states: `No <panel> data up to this timepoint.` (`aria-label`: `<panel> panel — empty`).
2. `data-state` keeps the precise state. It is consumed by S11k `panel.mount` telemetry and is not rendered or announced. This is an accepted residual: visible only through developer tools.
3. Every **other** clinician facing state (`partial`, rows, columns, chart ranges) is derived from data at or before `t` only (§10).
4. Applies to every panel, synthetic included.

---

# 10. Panel state semantics

Retain the existing state vocabulary:

`loading`

`empty-expected`

`empty-unexpected`

`partial`

`error`

and the S11g:

`unavailable`

AI state.

Do **not** add `stale` in S8.

The canonical Geneva snapshot contains no revision metadata from which a reliable "this value was corrected later" state can be inferred. Adding a visual stale state would therefore manufacture semantics the dataset does not provide.

Close the existing `stale` TODO with that rationale.

For sparse event driven panels such as labs and imaging, absence of a new observation at every hourly bucket is expected and does not itself mean `partial`.

## 10.1 State rules

`partial` is computed from sliced rows (`t_minutes <= t`) only. Neither "a variable is missing at the current bucket" nor "a variable appears later" makes a panel `partial` any more.

| Panel | `empty-*` | `partial` |
|---|---|---|
| vitals | no registry vital id ever / none at or before `t` | an aggregate family bucket at or before `t` has a bound without a median, or violates `min <= median <= max` |
| neuro_support | same over `nihss*`, `gcs`, `fio2` | same family rule for NIHSS; `gcs`/`fio2` never partial |
| labs | same over the §3 ids | never (sparsity is normal, values are schema validated) |
| imaging | canonical `IMAGING` rows **and** §5.1 ids both absent | canonical row with a null/empty report (unchanged); derived scalars never partial |
| admission | unchanged | unchanged |
| ai | unchanged | §6.3 payload family validation |

The vitals BP group note for a missing SBP/DBP/MAP family stays as it is.

---

# 11. Min / median / max rendering

Add a plotnine helper for aggregate series.

Recommended API:

`render_range_timeline_svg(frame, *, min_var, median_var, max_var, ...)`

Behaviour:

1. median is the primary line, with points
2. min to max is a light **per-bucket vertical range bar** (`geom_linerange`), not a filled ribbon: a ribbon visually interpolates between buckets, a bar does not
3. the median line joins two consecutive points only when they are adjacent sampling buckets (gap <= `RANGE_LINE_MAX_GAP_MINUTES = 60.0`, the Geneva hourly bucket); a longer gap breaks the line
4. a bucket with one bound draws the bar from the median to that bound only
5. a bucket with bounds but no median draws no bar and makes the panel `partial` (§10.1)
6. x axis continues to use the shared patient time window computed from sliced rows
7. the accessible fallback table contains the numeric min, median, and max values (`—` when absent)

Apply to:

1. HR
2. mean BP
3. SBP
4. DBP
5. oxygen saturation
6. respiratory rate
7. NIHSS

The existing grouped SBP / DBP visual relationship should remain. The implementation may either retain the grouped BP chart with range bars for each family or split mean BP from the SBP / DBP chart, but all three blood pressure families must remain visually distinguishable.

---

# 12. Performance

The original S8 performance requirement remains.

## 12.1 Boot

Dataset parsing and validation occur once during application startup.

There must be no adapter call or Pandera validation in a patient GET or HTMX swap.

The loader closure is called exactly once per FastAPI lifespan.

## 12.2 Request rendering

**Baseline machine** = the laptop used for the in-person clinician session. The review notes record its CPU model, core count, RAM, OS, Python version and Chrome version. Measurements on another machine count only when its specification is recorded alongside them.

Target:

**TTI < 2 seconds**

on the baseline machine (§12.2) for a Geneva patient at a representative 24 timepoint view with all available clinical panels and AI enabled.

Record:

1. server response time
2. HTML response bytes
3. total inline SVG bytes
4. browser TTI from Chrome DevTools

The measurements and machine description go into the S8 review notes.

The existing TODO concerning inline SVG payload size is closed only after this measurement.

If TTI exceeds 2 seconds, optimise within S8 before clinician use. Candidate order:

1. remove redundant chart rendering
2. cache deterministic SVGs by patient, panel, and current t within the process
3. only then consider splitting panel delivery with HTMX out of band swaps

Do not introduce client side chart JavaScript in S8.

## 12.3 Preflight SLA

Add an opt in `real_data` performance smoke for:

30 Geneva patients × 12 configured timepoints.

1. Patients: the first 30 ids of the S7 sidecar `test_patient_ids.csv`, in file order, that are present in the real CSV. Test-split patients are used so the AI panel is exercised.
2. Timepoints: `0, 60, …, 660` minutes.
3. The study config is built in the test (`tmp_path`) from `.EXAMPLE_DATA_PATHS`, with `geneva_ai` set; no real paths are committed.
4. The walk renders every panel through the same render functions the route uses (not `slice_to_timepoint` alone).

Measure the walk after the dataset is loaded.

Target:

**headless walk < 10 seconds**

on the baseline machine.

Dataset CSV loading is measured separately by the existing Geneva real data smoke and is not included in this 10 second threshold.

---

# 13. Accessibility

All new visual content has a non visual equivalent.

Required:

1. range charts have min, median, max tables
2. neuro/support timelines have fallback tables
3. imaging values are semantic tables
4. AI contributors are available as a table
5. current timepoint cells retain `aria-current="time"`
6. panel headings remain navigable
7. empty, partial, unavailable, and error messages remain text, not colour only

Existing keyboard navigation and question drawer behaviour must not regress.

---

# 14. Study validity and existing S9 to S11 behaviour

S8 may change clinical presentation only.

It must not alter:

1. answer encoding
2. question requiredness
3. progress frontier semantics
4. GET gate behaviour
5. locked historical panes
6. session bootstrap
7. arm assignment
8. config hash behaviour
9. timing events
10. telemetry semantics (the panel id vocabulary grows by `neuro_support`, §2.1)
11. tab guard
12. case provenance
13. practice case rules

A Geneva patient page must continue to flow through the same route and case lifecycle machinery as synthetic patients.

## 14.1 Presentation is not configuration versioned

Rendering code is not part of the S11b snapshot. S8 changes what an already pinned case looks like, including its AI panel.

This is accepted because no measured Geneva data exists before S8 (S8 is what makes Geneva usable), and synthetic pilot databases are not research data.

From S8 on, the operator convention, documented in `README.md` (*Running a Phase 2 study*), is:

1. set `ai_intervention.intervention_build_id` to the deployed commit;
2. bump `ai_intervention.presentation_version` whenever AI panel rendering changes;
3. `activate-config` a new version before collecting further measured cases.

No code enforces this in S8.

---

# 15. `exposed_field_ids` and prohibited field preflight

S11g's prohibited clinician facing field gate must mirror the enlarged renderer surface.

Update `exposed_field_ids()` so it includes:

1. all displayed vital scalar ids
2. all displayed neuro/support ids
3. all displayed laboratory ids
4. all displayed imaging derived scalar ids
5. every displayed admission field
6. only the AI fields actually exposed by the S8 AI renderer

This is required for study validity.

A value cannot be newly visible in S8 while remaining invisible to the prohibited field preflight.

## 15.1 Id scheme

| Surface | Id | Example |
|---|---|---|
| scalar (vitals, neuro, labs, imaging derived, bounds included) | `scalar:<canonical id>` | `scalar:hr_max`, `scalar:tmax_gt_6` |
| admission | `admission:<raw field>` | `admission:MedHist CHD` |
| canonical imaging | `imaging:<column>` (unchanged) | `imaging:report_text` |
| AI probability | `ai:probability` | |
| AI contributor | `ai:explanation.contributions.<feature>` (raw feature name) | `ai:explanation.contributions.lag2_glucose` |

`base_value` is not shown, so `ai:explanation` and `ai:explanation.base_value` are not exposed. Synthetic payloads keep `ai:<top-level key>`.

AI ids come from one pure function used by both the renderer and preflight, e.g. `web/ai_payload.py::exposed_ai_field_ids(payload)` built on `top_contributors` (§6.2). `exposed_field_ids()` takes the decoded payload of the row actually shown instead of `ai_payload_keys`. `cli_support._cell_failures` passes it per timepoint, so a prohibited contributor fails preflight at exactly the timepoint where it would enter the top ten.

## 15.2 Pattern

`PROHIBITED_FIELD_PATTERN` today is `^(admission|scalar|imaging|ai):[^\s:]+$`, which cannot express Geneva admission fields containing spaces.

It becomes:

`^(admission|scalar|imaging|ai):[^\s:](?:[^:]*[^\s:])?$`

This allows inner spaces but forbids colons and leading or trailing whitespace. Every previously valid value stays valid, so existing configs and hashes are unaffected.

---

# 16. Divergence view compatibility

S10's `newly_visible_data` annotation must remain internally consistent after the classifier expands.

Changes:

1. newly recognised laboratory variables count as `labs`
2. newly recognised vital aggregate rows count as `vitals`
3. imaging derived scalar variables count as `imaging`, even though their canonical storage shape is `SCALAR_TS`
4. neurological/support scalar variables may remain under `other scalar` in the existing six category divergence taxonomy

Do not add a new divergence facet category solely for S8.

Aggregate bounds (`*_min`, `*_max`) are **not counted**: a Geneva bucket is one vitals observation, not three. Counting rules read the aggregate family from `clinical_fields.py`.

This preserves the existing S10 output structure while avoiding the misleading classification of perfusion variables as generic scalar data.

---

# 16a. MIMIC and synthetic impact

The variable sets live in `ingestion/canonical.py` and are dataset agnostic.

1. **Synthetic**: ids unchanged; it renders as before except the §9.2 empty state wording and the §10.1 `partial` rules.
2. **MIMIC**: has no rename map, so its vitals and labs stay unmatched as today (out of scope). It ships the same imaging derived ids (`cbf_lt_30`, `tmax_gt_6`, …, `mimic.py` docstring), so the S8 imaging panel **also renders for MIMIC**. This is accepted and covered by one test.

---

# 17. Files

## Modify

`src/ehr_simulator/ingestion/geneva.py`

Expand Geneva scalar aliases and canonicalise min / median / max names.

`src/ehr_simulator/ingestion/canonical.py`

Expand the clinical variable sets and add explicit neuro/support and imaging derived sets.

`src/ehr_simulator/web/panels.py`

Add neuro/support state handling, imaging derived state handling, truthful study timepoints, enlarged exposed field inventory, and payload aware AI state handling.

`src/ehr_simulator/web/charts.py`

Add min / median / max range bar rendering (§11).

`src/ehr_simulator/web/routes.py`

Render the new panel, expanded labs, derived imaging, grouped admission sections, and Geneva AI payload.

`src/ehr_simulator/web/templates/_panel_vitals.html`

Render aggregate range bars and expanded measurements.

`src/ehr_simulator/web/templates/_panel_labs.html`

Use display metadata and full laboratory order.

`src/ehr_simulator/web/templates/_panel_admission.html`

Group baseline and treatment fields.

`src/ehr_simulator/web/templates/_panel_imaging.html`

Render Geneva imaging derived variables as well as canonical imaging rows.

`src/ehr_simulator/web/templates/_panel_ai.html`

Render Geneva probability and bounded explanation content.

`src/ehr_simulator/web/static/theme.css`

Styles for new panel, aggregate range bars, grouped admission sections, and AI contributors.

`src/ehr_simulator/divergence.py`

Classify newly visible Geneva variables consistently.

`src/ehr_simulator/web/telemetry.py`, `src/ehr_simulator/panel_exposure.py`

Add `neuro_support` to the closed panel ids (§2.1).

`src/ehr_simulator/web/templates/_chrome_epic.html`, `_chrome_dense.html`

Place the new panel (§2.1).

`src/ehr_simulator/web/templates/_panel_*.html` (all)

Time local empty state wording (§9.2).

`src/ehr_simulator/config/study.py`

Optional `ai_intervention.outcome_label`; relaxed `PROHIBITED_FIELD_PATTERN` (§15.2).

`src/ehr_simulator/cli_support.py`

Pass study timepoints to `slice_to_timepoint`; build AI exposed ids from the shown payload (§15.1); WARN on Phase 2 without `outcome_label`.

`README.md`

Presentation versioning convention (§14.1).

`TODOS.md`

Close the `stale` state and inline SVG measurement items; update the min/median/max band item.

`tests/test_telemetry.py`, `tests/test_panel_exposure.py`, `tests/test_config.py`, `tests/test_cli.py`, `tests/test_divergence.py`, `tests/e2e/`

Lock the vocabulary, pattern, preflight and divergence changes.

`tests/test_geneva.py`

Lock the expanded alias contract.

`tests/test_panels.py`

Lock new slicing, state, timepoint, and exposed field behaviour.

`tests/test_routes.py`

Lock real shaped panel rendering.

`tests/test_intervention.py`

Lock Geneva AI rendering and prohibited field behaviour.

`tests/test_geneva_real.py`

Add opt in real data performance smoke.

## New

`src/ehr_simulator/web/clinical_fields.py`

Single display registry for clinical labels, ordering, grouping, and formatting.

`src/ehr_simulator/web/templates/_panel_neuro_support.html`

Neurological and support panel.

`src/ehr_simulator/web/ai_payload.py`

Pure AI payload family validation, `top_contributors`, `exposed_ai_field_ids` (§6, §15.1).

`tests/test_geneva_ui.py`

Focused Geneva presentation integration suite.

---

# 18. Test inventory

Target: **at least 36 S8 tests** (items 1-23 and 26-38 below), in addition to the existing suite. Items 24 and 25 are regression gates on existing tests; items 39-41 are e2e, real data or manual.

## Unit

1. Geneva aggregate aliases map min / median / max HR correctly.
2. Aggregate aliases cover BP, SpO2, RR, and NIHSS.
3. Clinical field registry covers every agreed Geneva scalar variable.
4. Lab registry contains the full laboratory set in fixed order.
5. Neuro/support registry contains NIHSS, GCS, and FiO2.
6. Imaging registry contains all Tmax, CBF, CBV, mismatch, occlusion, and stenosis variables.
7. Range renderer produces valid SVG from complete min / median / max rows.
8. Range renderer tolerates a missing min or max without inventing data.
9. Geneva AI payload selects deterministic top positive and negative contributors.
10. Geneva AI probability only payload is valid and is not marked partial.
11. `PatientSlice.timepoints` uses explicit study timepoints when supplied.
12. Contributor selection: zeros excluded, fewer than five per sign not padded, magnitude then name tie break.
13. Contributor labels: known grammar resolves (`lag2_glucose` → `Glucose (2 h earlier)`); unknown feature falls back to its raw name.
14. `PROHIBITED_FIELD_PATTERN` accepts `admission:MedHist CHD`, rejects leading/trailing whitespace and extra colons, and accepts every previously valid value.
15. `outcome_label` absent → serialized snapshot and hash unchanged; present → heading includes it.
16. `partial` rules (§10.1): bound without median and `min > median` → partial; a variable first measured after `t` → **not** partial.

## Integration

17. Geneva shaped fixture renders vitals, neuro/support, labs, admission, imaging, and AI.
18. Admission fields appear in the correct baseline / treatment sections; an unregistered field appears under Other.
19. Sparse Geneva labs render missing cells without treating normal sparsity as an error.
20. Imaging derived scalar data prevent a false "No imaging recorded" state.
21. `exposed_field_ids()` contains every field visible in the new panels, bounds and displayed contributors included, and no undisplayed contributor.
22. Dataset loader is called once across multiple patient requests.
23. Geneva AI HTML contains only bounded contributor output, not the complete SHAP dictionary.

## Regression

24. Existing answer, advance, locked pane, telemetry, and tab guard route tests remain green.
25. Synthetic vitals and labs continue to render.
26. **DATA LEAK REGRESSION:** requesting timepoint `t=N` exposes no scalar, imaging, AI, table, chart, accessibility fallback, or derived value whose `t_minutes > N`.
27. **FUTURE EXISTENCE REGRESSION:** for a panel with data only after `t`, and one with no data at all, the visible HTML and `aria-label` are identical (§9.2).
28. Measured no AI cases continue to contain no AI panel markup.
29. Measured AI uses exactly the current timepoint and never falls back to an earlier prediction.
30. Study configured timepoint count remains authoritative even when Geneva contains 72 dataset timepoints.
31. `POST /telemetry/events` accepts `panel.mount`/`viewport`/`open`/`close` for `neuro_support`; `panel_summaries.csv` carries its rows.
32. Preflight FAILs when a prohibited contributor enters the top ten at a timepoint and passes when it is outside the top ten.
33. Preflight FAILs on a prohibited admission field containing spaces.
34. Legacy Geneva AI: only the latest row renders contributors; earlier rows show probability only.
35. Divergence counts a Geneva vitals bucket once (bounds not counted) and perfusion rows as `imaging`.
36. MIMIC imaging derived scalars render in the imaging panel.
37. Epic chrome has a `Neuro / Support` tab; dense chrome has the section.
38. `real_data`: every inventory label arrives in its expected frame (§1.3).

## E2E / real data

39. Geneva fixture patient can be walked from first configured timepoint to last without a render error, epic tabs included.
40. `@pytest.mark.real_data`: 30 patient × 12 timepoint headless walk stays below the S8 SLA.
41. Manual Chrome trace confirms TTI < 2 seconds on the baseline machine.

---

# 19. Acceptance criteria

S8 is complete when all of the following are true:

1. A real Geneva study starts through the normal `serve --config` path.
2. Geneva is loaded and validated once at boot.
3. All study configured patients use study configured timepoints.
4. No clinician facing output contains future data.
5. Every variable in the agreed Geneva clinical inventory has an explicit display location.
6. Geneva min / median / max vitals are rendered as one clinical series with range bars.
7. NIHSS and GCS are visible.
8. The complete study laboratory set is available in the Labs panel.
9. Baseline variables are grouped into readable clinical sections.
10. Treatment and referral information is visible.
11. Imaging derived perfusion and vascular variables are visible despite Geneva's intentionally empty canonical `IMAGING` frame.
12. Geneva AI displays probability plus a bounded explanation rather than raw JSON.
13. No AI case behaviour is unchanged.
14. Prohibited field preflight matches what the clinician can actually see.
15. Synthetic UI remains functional.
16. No new client side charting dependency is introduced.
17. Representative Geneva TTI is below 2 seconds.
18. A clinician can navigate one real Geneva patient end to end on the baseline machine without a render failure.
19. No clinician facing text or `aria-label` distinguishes "no data ever" from "data later".
20. S11k telemetry and the Phase 2 export accept and report `neuro_support`.
21. The §1.3 real data source assertions pass.

---

# 20. Explicit decisions

**D1.** Do not rebuild dataset selection or boot caching. They already exist.

**D2.** Keep canonical Geneva imaging derived measurements in `SCALAR_TS`; presentation grouping does not require moving rows into `IMAGING`.

**D3.** Add a neurological/support panel rather than overloading the synthetic vitals layout.

**D4.** Expand Labs to the full Geneva study laboratory set.

**D5.** Render min / median / max aggregates as one measurement family.

**D6.** Accepted ranges are not UI clipping or warning thresholds.

**D7.** Do not add a `stale` panel state because the canonical dataset does not contain sufficient revision semantics.

**D8.** The Geneva AI renderer exposes one probability and at most ten SHAP contributors.

**D9.** AI explanation absence is valid because S7 allows prediction artifacts without SHAP.

**D10.** Study defined timepoints become authoritative inside `PatientSlice`, not only at route level.

**D11.** S8 changes presentation only; existing S9 to S11 study behaviour is preserved.

**D12.** A value newly visible to a clinician must also be visible to the prohibited field preflight.

**D13.** FiO2 lives in Neuro / Support, glucose in Labs, weight in admission Demographics; each variable has exactly one location.

**D14.** Clinician facing empty states are time local; the precise state survives only in `data-state` for telemetry.

**D15.** `partial` is derived from data at or before `t` only.

**D16.** Aggregate bounds render as per-bucket range bars, not ribbons; the median line breaks at gaps over one bucket.

**D17.** The outcome name comes from an optional pinned `ai_intervention.outcome_label`, never from code.

**D18.** Contributor ranking covers every feature; unresolved feature names render raw, never dropped.

**D19.** Contributors are prohibited-field addressable as `ai:explanation.contributions.<feature>`; the pattern allows inner spaces.

**D20.** Presentation is not configuration versioned in S8; operators version it through `presentation_version` and `intervention_build_id` (§14.1).

**D21.** Lab columns are observation times at or before `t`, not configured study timepoints.

---

# 21. Suggested implementation order

### Commit 1 — Geneva clinical vocabulary

Expand Geneva aliases, clinical variable sets, display registry, and coverage tests. Run the §1.3 real data source assertions first; stop on failure.

### Commit 2 — Real Geneva panels

Implement range bars, neuro/support (with the §2.1 vocabulary change in the same commit), complete labs, grouped admission, imaging derived rendering, §10.1 state rules and §9.2 empty state wording.

### Commit 3 — Geneva AI UI + study validity

Enumerate the real SHAP feature names and record the grammar here. Then implement `ai_payload.py`, `outcome_label`, the bounded SHAP presentation, the §15 id scheme and pattern, and the intervention regressions.

### Commit 4 — Timepoint and performance hardening

Make `PatientSlice.timepoints` truthful, add the data leak and future existence regressions, run real data SLA measurements, record the Chrome performance trace, and update `README.md` / `TODOS.md`.

After implementation:

`/review`

then:

`/ship` (only when the user asks; it pushes)

Deploying is out of scope (project rule: no deploy or push without the user).
