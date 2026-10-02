# Session 07 — Geneva AI predictions adapter

## Goal

Load the precomputed Geneva test subset AI predictions and their per timepoint SHAP explanations into the canonical `AI_OUTPUT` frame, integrate that frame into `GenevaDataset`, and expose artifact provenance compatible with the AI intervention integrity checks shipped in S11g.

After S7, a Geneva dataset loaded through the normal study configuration path can provide real AI output to S8 and to the existing Phase 2 intervention machinery.

The central contract is:

`Geneva source artifacts → deterministic adapter → canonical AI_OUTPUT + AIArtifactProvenance`

S7 consumes model output. It does not run model inference.

## Context and compatibility with shipped sessions

The original roadmap described:

`load_geneva_ai_predictions(pkl_path, shap_dir) -> pd.DataFrame`

That remains the conceptual parser boundary, but the repository has evolved since the roadmap was written.

S11g introduced `AIArtifactProvenance` and requires measured studies to verify:

1. prediction artifact SHA256
2. explanation artifact SHA256
3. model system version

`GenevaDataset` already contains:

`ai_provenance: AIArtifactProvenance | None = None`

and study boot plus preflight already fail closed when configured AI provenance does not match the loaded dataset.

Therefore S7 must populate both `GenevaDataset.ai_output` and `GenevaDataset.ai_provenance`. Loading only a DataFrame would leave real Geneva Phase 2 unusable.

The canonical `AI_OUTPUT` schema remains unchanged:

| Column | Type | Contract |
|---|---|---|
| `patient_id` | string | non empty |
| `t_minutes` | float | non negative |
| `model_id` | string | non empty |
| `output_json` | string | valid JSON |

Uniqueness remains `(patient_id, t_minutes, model_id)`.

## Non goals

S7 does not:

- change `AI_OUTPUT_SCHEMA`
- change AI versus no AI arm behaviour
- change S11g intervention selection
- render Geneva AI output in a new UI
- implement S8 real data UI work
- run or retrain an AI model
- load ground truth outcome files at runtime (`y_true` inside `test_predictions.pkl` is unpickled with the tuple but never used; only the one off export script reads the test split, and only to cross check order)
- add AI predictions for MIMIC
- substitute predictions between timepoints
- infer missing SHAP explanations
- normalise patient identifiers heuristically to force a match
- expose future predictions before their corresponding timepoint

## 1. Source exploration (done 2026-10-01)

Run read only against `.EXAMPLE_DATA_PATHS`. Model directory:
`/mnt/data1/klug/output/opsum/short_term_outcomes/end_with_imaging/best_xgb_final_model/`.

Summary: **neither artifact carries patient ids, timestep indexes or model identity.** The rows are positional. Ids come from the test split (Explore C), identity from the model file (Explore D).

### Explore A: `test_predictions.pkl`

| Item | Observed |
|---|---|
| Format | standard pickle, protocol 4, 460 751 bytes |
| Globals | `numpy.dtype`, `numpy.ndarray`, `numpy.core.multiarray._reconstruct` only |
| Top level | `tuple` of length 2 |
| `[0]` | `ndarray (38376,) float64`, values `{0.0, 1.0}`: **ground truth `y_true`** |
| `[1]` | `ndarray (38376,) float32`, range 0.00264–0.853: **predicted probability `y_prob`** |
| NaN / inf | none |
| Patient id | not present |
| Timestep | not present; implied by position |
| Layout | patient major: row `r` → test patient `r // 72`, timestep `r % 72` |
| Patients × timesteps | 533 × 72 = 38 376 |
| Timesteps | 0..71, hourly |
| `model_id` / version | not present |

Layout evidence: `sigmoid(sum(SHAP row))` equals `y_prob` within 3.6e-7 under patient major order and differs by up to 0.84 under timestep major order.

Label semantics (observation only, not used by S7): `y_true[t] = 1` when an early neurological deterioration event falls in hours `(t, t+6]`, so `y_prob` at `t` is a 6 hour risk.

### Explore B: `shap_explanations_over_time/`

| Item | Observed |
|---|---|
| Layout | flat directory, exactly 2 regular files, no symlinks, 167 MB |
| `shap_feature_names.pkl` | `list[str]`, 1134 unique names, no globals |
| `tree_explainer_shap_values_over_ts.pkl` | `list` of 72 `ndarray (533, 1135) float32`; same three numpy globals |
| List index | timestep (0..71) |
| Axis 0 | test patient, same order as predictions |
| Columns 0..1133 | per feature contributions in `shap_feature_names` order, log odds |
| Column 1134 | bias / expected value, constant `-0.6404414` over all rows |
| NaN / inf | none |
| Third party package | none (numpy only; no `shap.Explanation` objects) |
| Read + hash + unpickle | 0.46 s |

Each row describes one timestep's feature vector. Feature names are all backward looking (`<x>`, `avg_`/`min_`/`max_`/`std_` over time, `diff_`, `lag2_`, `lag3_`, `rolling_mean_`/`rolling_std_`/`rolling_trend_`, `timestep_idx`), so the explanation at `t` uses bins `0..t` only. §2.2 is satisfied structurally; no slicing is needed.

### Explore C: identifier compatibility

- Clinical CSV: `case_admission_id`, string `<digits>_<digits>` (e.g. `900001_0001`), 2657 patients; canonical `patient_id` = this string (`geneva.py`).
- Predictions and SHAP: no ids.
- The patient order exists only in the test split `test_data_early_neurological_deterioration_ts0.8_rs42_ns5.pth` (Geneva dataset dir). It is a torch zip whose `data.pkl` holds `(X, outcome_events)`:
  - `X`: `ndarray (533, 72, 103, 4) object`, fields `[case_admission_id, timestep, sample_label, value]`; the id is constant per patient, the timestep equals the axis index
  - `outcome_events`: pandas DataFrame of ground truth events
  - pickled with pandas < 2 (needs a `pandas.core.indexes.numeric.Int64Index` shim); torch is not needed
- Order check: `X[:, 0, 0, 0]` gives 533 unique ids; the 98 patients with an event in `outcome_events` are exactly the 98 patients with any `y_true = 1`, 0 mismatches.
- All 533 ids match `^\d+_\d+$` and all are in the clinical CSV.

**Pinned mapping:** identity. The `i`th id of the sidecar (§4.1) is the canonical `patient_id` of test patient `i`. No conversion.

The `.pth` is never read at runtime. `scripts/export_geneva_test_ids.py` (one off, operator run) exports the order into the sidecar `test_patient_ids.csv`:

- reads the `.pth` with `zipfile` + a restricted unpickler (numpy, pandas, the `Int64Index → pandas.Index` shim; nothing else)
- checks the id is constant per patient and the timestep field equals `0..71`
- cross checks the order against `test_predictions.pkl`: the patients with an outcome event equal the patients with any `y_true = 1`; else refuses
- writes UTF 8 CSV, header `case_admission_id`, one id per row in test order, atomically
- prints no ids, values or outcomes

Sidecar contract: header exactly `case_admission_id`; each id matches `^\d+_\d+$`; ids unique; row count = `len(y_prob) / 72` = SHAP axis 0.

### Explore D: model identity

Neither artifact embeds a model name or version. Shipped alongside: `final_model_config.json` (hyperparameters, `timestamp: 20260215_162922`) and `xgb_final_model.model` (254 591 bytes).

- `model_system_version` = lowercase hex SHA256 of the raw bytes of `xgb_final_model.model` (§5.3).
- `model_id` = explicit `geneva_ai.model_id` (operator chosen label, e.g. `opsum_end_xgb`).

## 2. Canonical payload contract

Each prediction row becomes exactly one canonical `AI_OUTPUT` row:

| Column | Value |
|---|---|
| `patient_id` | `sidecar[r // 72]` |
| `t_minutes` | `(r % 72) * 60.0` (§7.1) |
| `model_id` | `geneva_ai.model_id` |
| `output_json` | payload below |

Payload with explanations:

```json
{"explanation":{"base_value":-0.6404414,"contributions":{"ALAT":0.0012,"...":0.0}},"probability":0.0731}
```

Without an explanations source the `explanation` key is omitted (not `null`), so payloads with and without SHAP are distinguishable and byte stable.

- `probability`: `y_prob[r]`
- `explanation.base_value`: SHAP column 1134 at `[r % 72][r // 72]`
- `explanation.contributions`: `{feature_names[j]: shap[r % 72][r // 72, j]}` for all 1134 features, unchanged (no top k; S8 chooses what to show)

S7 adds no outcome name or horizon string; `model_id` names the model.

### 2.1 Payload whitelist

Allowed source fields: `y_prob`, the SHAP contributions, the SHAP bias column, the feature names. Nothing else.

`y_true` (tuple index 0) is never copied into the payload, frame, logs or errors. The loader does not read or open `final_model_config.json`, `threshold_tuning_results_*.json`, `scaler.pkl`, the `.pth` split files or the outcome CSV.

### 2.2 No future information

The payload at `t` contains nothing from timesteps after `t`. With the observed structure each row is built from list element `t` only (`shap[t]`), whose features are backward looking (Explore B). The loader never reads `shap[t']` for `t' != t` when building the row for `t`.

### 2.3 Serialisation

- NumPy scalar types become native Python scalars (`float32 → float`)
- NumPy arrays become JSON arrays
- mappings are recursively converted
- string values remain strings
- booleans remain booleans
- `None` remains JSON `null`
- NaN and positive or negative infinity are rejected with `AdapterError`
- unsupported Python objects are rejected
- `json.dumps(..., sort_keys=True, separators=(",", ":"), allow_nan=False)` produces the stored string

The same input must therefore produce byte identical `output_json`.

NumPy `float32` conversion is a mandatory regression because it is explicitly called out in `ROADMAP.md`.

Size: about 40 KB per row with explanations. Only rows of retained patients are serialised (§4.2), so a 50 patient study holds about 3600 rows (~144 MB).

## 3. Prediction and SHAP correspondence

Correspondence is positional; there are no keys to match. When explanations are configured, before any row is built:

- `len(shap_values) == 72`
- every element is `float32 (N, 1135)` with `N = len(sidecar)`
- `len(feature_names) == 1134`, names unique strings
- `len(y_prob) == len(y_true) == 72 * N`
- the directory holds exactly `shap_feature_names.pkl` and `tree_explainer_shap_values_over_ts.pkl`; any other entry is drift
- **alignment check:** for every retained row, `|sigmoid(sum(shap[t][i, :])) - y_prob[72 i + t]| <= SHAP_PROBABILITY_TOLERANCE` (`1e-5`; observed max 3.6e-7)

Any failure is an `AdapterError`. The alignment check is what catches a reordered or swapped file: a positional mismatch would otherwise pass silently.

Without explanations only the prediction and sidecar length checks run; payloads carry no explanation and `explanation_sha256` is `None`.

## 4. Study configuration and loader integration

The current `build_dataset_loader(study)` is the single normal path used by `serve`, preflight, preview, and study mode. S7 must therefore wire Geneva AI through this path rather than leaving the adapter as a standalone helper.

### 4.1 Optional AI source configuration

`GenevaAIArtifactConfig` (`extra="forbid"`):

```yaml
geneva_ai:
  predictions_path: /path/to/test_predictions.pkl
  patient_ids_path: /path/to/test_patient_ids.csv
  model_path: /path/to/xgb_final_model.model
  model_id: opsum_end_xgb
  explanations_dir: /path/to/shap_explanations_over_time  # optional
```

The block is optional and serialised only when present so existing configurations and stored historical snapshots without Geneva AI remain readable. `explanations_dir` is omitted from serialisation when absent.

Rules:

- `geneva_ai` is valid only when `dataset: geneva`
- `predictions_path`, `patient_ids_path`, `model_path`, `model_id` are required; `explanations_dir` is optional
- `model_id` is non empty
- there is no `model_system_version` field: it is always derived (§5.3)
- paths resolve relative to the study YAML directory using the same convention as `csv_path` and `params_dir`
- every path is scoped through the existing `EHR_SIM_DATA_ROOT` traversal guard before access
- no `geneva_ai` block preserves the existing behaviour: empty `AI_OUTPUT` and `ai_provenance = None`

The paths in `geneva_ai` are part of the hashed study snapshot. Moving the artifacts on disk therefore requires a new configuration version. This is intended: the snapshot records exactly which files a version loaded. README states it.

The source configuration and `ai_intervention` remain conceptually separate.

`geneva_ai` says what artifact to load.

`ai_intervention` says what frozen artifact a measured study expects to deliver. Its `model_id` must equal `geneva_ai.model_id` or every measured AI render is `MISSING_ROW` (preflight FAILs).

S11g's existing provenance comparison remains the integrity gate between them.

### 4.2 Geneva adapter

`load_geneva()` gains one optional AI input rather than duplicating AI loading in the CLI or web layer:

```python
load_geneva(
    csv_path,
    params_dir,
    *,
    strict=True,
    patient_ids=None,
    ai_source=None,
) -> GenevaDataset
```

`ai_source: GenevaAISource | None` is a frozen ingestion dataclass with the resolved paths and `model_id`.

When absent:

- current S3/S4 behaviour stays byte for byte equivalent
- `ai_output` is the canonical empty frame
- `ai_provenance` is `None`

When present:

1. normal Geneva clinical ingestion runs unchanged
2. AI source paths pass the same root guard
3. each artifact file is read into memory once
4. those bytes are hashed, then parsed from the same bytes (`Unpickler(io.BytesIO(data))`, never a second `open`), so the hash always describes what was parsed
5. structure and correspondence checks (§3) run on the complete artifact
6. rows are restricted to `patient_ids` when a patient filter is provided; only retained rows are serialised
7. `AI_OUTPUT` validates with `validate(..., CanonicalShape.AI_OUTPUT, strict=True, dataset="geneva")`
8. the validated frame is assigned to `GenevaDataset.ai_output`
9. calculated source provenance is assigned to `GenevaDataset.ai_provenance`

AI validation is strict regardless of the `strict` argument: `build_dataset_loader` passes `strict=False` for the clinical frames, and that never relaxes AI checks.

AI artifact corruption always fails closed. It is not converted into a lenient `IngestionIssue`, because serving malformed or ambiguously aligned model output is an intervention integrity failure rather than an ordinary clinical source row anomaly.

### 4.3 CLI loader

`cli_support.build_dataset_loader()` constructs the optional Geneva AI source from `study.geneva_ai` and passes it into `load_geneva`.

No AI parsing logic lives in `cli_support.py`.

The existing S11g preflight and boot checks continue to consume `dataset.ai_provenance`; they do not need a second Geneva specific provenance path.

### 4.4 Operator provenance output

Operators need the loaded digests to fill `ai_intervention.prediction_artifact_sha256`, `explanation_artifact_sha256` and `model_system_version`.

`validate-adapter` prints the loaded `ai_provenance` (three values, plus AI row and patient counts) when the dataset exposes one. It never prints payloads. README's *Running a Phase 2 study* section documents this step, and the sidecar export, before `activate-config`.

README also states that every Phase 2 `patient_ids` entry must be in the model test subset; preflight already FAILs a missing `(patient, t, model_id)` row.

## 5. Artifact provenance

S7 uses the existing:

```python
@dataclass(frozen=True)
class AIArtifactProvenance:
    prediction_sha256: str
    explanation_sha256: str | None
    model_system_version: str
```

### 5.1 Prediction digest

The predictions are positional, so which patient a value belongs to is defined by the predictions file **and** the sidecar together. `prediction_sha256` covers both:

```
entries = [
  {"role": "patient_ids", "sha256": sha256(raw sidecar bytes)},
  {"role": "predictions", "sha256": sha256(raw test_predictions.pkl bytes)},
]
prediction_sha256 = sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8"))
```

Roles are fixed constants, not file names, so renaming a file does not move the digest (the path change already moves the config hash).

Hash raw bytes before transformation. Do not hash:

- the parsed Python object
- the canonical DataFrame
- rendered HTML
- `output_json`

### 5.2 SHAP directory digest

For every regular file directly under `explanations_dir`:

1. compute SHA256 over that file's raw bytes
2. take its path relative to `explanations_dir` using POSIX separators
3. build an entry containing `path` and `sha256`

Sort entries lexicographically by relative path, serialise with `json.dumps(entries, sort_keys=True, separators=(",", ":"))`, SHA256 the UTF 8 bytes. This digest is `explanation_sha256`. Without an explanations source it is `None`.

S11g already handles both cases: a configured `explanation_artifact_sha256` of `None` matches a loaded `None`; a configured digest with no loaded explanations (or the reverse) is a mismatch. Adding or removing SHAP therefore requires a new configuration version.

The digest changes when file contents change or a file is added, removed or renamed (an unexpected file is refused anyway, §3). Traversal order does not affect it. Symlinks and subdirectories are rejected.

### 5.3 Model system version

`model_system_version` = lowercase hex SHA256 of the raw bytes of `geneva_ai.model_path`. The file is hashed, never loaded; xgboost is not a dependency.

It is never taken from `ai_intervention.model_system_version` or from YAML.

## 6. Patient subset semantics

The Geneva AI artifact contains only the 533 test patients. This is expected.

If a requested Geneva patient is not in the sidecar:

- return no `AI_OUTPUT` rows for that patient
- do not synthesise predictions
- do not raise merely because that patient is absent

This preserves the original S7 roadmap requirement.

Identifier incompatibility must not silently appear as an empty test subset:

- every sidecar id must match `GENEVA_CASE_ADMISSION_ID_PATTERN = ^\d+_\d+$`; else `AdapterError`
- requested ids are not format checked (the clinical adapter accepts any id); a requested id absent from the sidecar simply gets no AI rows
- when none of the requested ids is in the sidecar the load still succeeds (a study may use only non test patients); `validate-adapter` prints `AI rows: 0` and preflight FAILs any Phase 2 patient without its row

Never automatically repair an unknown mismatch.

## 7. Timepoint semantics

Canonical AI rows use minutes from first patient contact, matching the rest of the simulator.

Rules:

- `t_minutes = timestep * 60.0`, `timestep = r % 72` (an exact float)
- duplicate `(patient_id, t_minutes, model_id)` rows (a duplicated sidecar id) are rejected before canonical validation
- no interpolation, nearest neighbour matching, forward fill or backward fill

S11g selects the measured AI row at exactly the configured timepoint; configured timepoints must therefore be whole hours to receive AI rows.

### 7.1 Alignment with clinical bins (pinned)

Geneva clinical rows use `t_minutes = relative_sample_date_hourly_cat * 60.0` (`geneva.py`). Bin `h` holds the data of hour `h`, revealed at `t = 60h`.

Timestep index `i` of the predictions and SHAP is the same hourly bin index: the test split's timestep field equals `0..71` on the axis built from `relative_sample_date_hourly_cat`, and the model's features at `i` are backward looking over bins `0..i` (Explore B, C).

**Rule: index `i` → `t = 60i`.** The prediction at `t` uses bins `0..t/60` only, the same data the clinician sees at `t`.

Confirmed against the producing code (`JulianKlug/OPSUM`, `prediction/short_term_outcome_prediction/`):

- `testing/test_xgb.py::test_final_model` concatenates per patient arrays from `aggregate_and_label_timeseries`, one row per timestep `0..71` → patient major, `y_prob = predict_proba(...)[:, 1].astype('float32')`, `pickle.dump((y_test, y_prob))`.
- `timeseries_decomposition.py::aggregate_and_label_timeseries`: `y_true[t] = 1` iff an event's `relative_sample_date_hourly_cat` is in `(t, t + 6]`.
- `prediction/utils/utils.py::aggregate_features_over_time`: every feature at `t` is causal (`cumsum`, `minimum/maximum.accumulate`, `diff` with `t-1`, `lag2`/`lag3`, trailing rolling windows of 6; `timestep_idx = t / 71`).
- `testing/compute_shap_explanations_over_time.py::compute_shap_final_model`: `shap[t] = booster.predict(DMatrix(X[:, t, :]), pred_contribs=True)` on the same scaled matrix, so row sum = margin and the last column is the bias; feature names from `X_test_raw[0, 0, :, 2]`.

A wrong rule shifts AI one hour, so it keeps its own regression test (§12).

## 8. Pickle trust boundary

`test_predictions.pkl` and both SHAP files are Python pickles and therefore executable input.

The loader documentation states explicitly:

> Only load the trusted, locally supplied Geneva model artifact. Python pickle deserialisation is not safe for untrusted files.

The artifact paths are operator controlled and local. No HTTP upload or clinician supplied path reaches the pickle loader.

Defence in depth (not a claim of safety): the loader uses an `Unpickler` whose `find_class` allows only the globals observed in Explore A/B (`numpy.dtype`, `numpy.ndarray`, `numpy.core.multiarray._reconstruct`, plus `numpy._core.multiarray._reconstruct` for files re-saved under NumPy 2). Any other global is an `AdapterError`.

No new dependency: plain numpy pickles; no torch, shap or xgboost.

## 9. Error contract

AI ingestion failures use `AdapterError`.

Errors include enough context to diagnose the artifact without dumping patient level model values, ids or labels.

Required failure classes:

- predictions, sidecar, model file or (configured) explanations directory missing
- path outside `EHR_SIM_DATA_ROOT`
- pickle global outside the allowlist (§8)
- predictions not a 2 tuple of 1 D arrays of equal length, or length not `72 × N`
- `y_prob` not float, or outside `[0, 1]`
- sidecar header wrong, id not matching `^\d+_\d+$`, duplicate id, or row count ≠ `N`
- SHAP directory with missing, extra, symlinked or nested entries
- SHAP values not a list of 72 `(N, 1135)` float arrays, or feature names not 1134 unique strings
- SHAP/prediction alignment check failed (§3)
- duplicate canonical AI key
- NaN or infinite value
- non JSON serialisable payload
- empty `model_id`
- canonical `AI_OUTPUT` validation failure

Do not include complete prediction or SHAP payloads in exception messages or logs.

## 10. Files expected to change

| Path | Change |
|---|---|
| `src/ehr_simulator/ingestion/geneva_ai.py` | New. Restricted unpickler, sidecar reader, prediction + SHAP parsers, alignment check, JSON normalisation, digests |
| `src/ehr_simulator/ingestion/geneva.py` | Optional `ai_source`; populate `ai_output` and `ai_provenance` |
| `src/ehr_simulator/ingestion/__init__.py` | Export `load_geneva_ai_predictions(ai_source, *, patient_ids=None) -> GenevaAIOutput` (frame + provenance) and `GenevaAISource` |
| `src/ehr_simulator/config/study.py` | Optional `geneva_ai` block (§4.1); omitted when absent |
| `src/ehr_simulator/config/__init__.py` | Re export `GenevaAIArtifactConfig` |
| `src/ehr_simulator/cli_support.py` | Pass resolved Geneva AI source into `load_geneva` |
| `src/ehr_simulator/cli.py` | `validate-adapter` prints loaded AI provenance (§4.4) |
| `scripts/export_geneva_test_ids.py` | New. One off sidecar export from the test split `.pth` (§1 Explore C) |
| `README.md` | Phase 2 operator steps: export sidecar, read provenance, fill `ai_intervention`; test subset requirement; artifact paths are hashed |
| `tests/test_geneva_ai.py` | Unit, integration and regression coverage |
| `tests/test_geneva_ai_real.py` | `@pytest.mark.real_data` smoke against local production artifacts |
| `tests/fixtures/geneva_ai/` | Fully synthetic fixture reproducing the observed structure |
| `tests/fixtures/geneva_ai/build_geneva_ai_fixture.py` | Deterministic fixture builder |
| `CLAUDE.md` | Mark S7 shipped, document Geneva AI ingestion/provenance, update test counts |
| `specs/ROADMAP.md` | Mark S7 shipped and note S11g provenance integration |

`canonical.py` must not change.

## 11. Fixture strategy

No real Geneva patient identifier, prediction value, SHAP value, or clinical data is committed.

The builder reproduces the observed structure with fabricated values, using the real container types and `T = 72` timesteps but a small `N` and feature count `F`:

- `test_predictions.pkl`: `(y_true float64, y_prob float32)`, patient major, length `72 N`
- `test_patient_ids.csv`: `N = 3` fabricated ids matching `^\d+_\d+$` (e.g. `900001_0001`)
- a small Geneva clinical CSV of its own with those ids plus one absent patient, reusing `tests/fixtures/geneva/` normalisation and encoding files (the existing clinical fixture ids `geneva_fixture_NNN` don't match the pattern and stay untouched)
- `shap_explanations_over_time/shap_feature_names.pkl`: `F = 4` names
- `shap_explanations_over_time/tree_explainer_shap_values_over_ts.pkl`: 72 float32 `(N, F + 1)` arrays, constant bias column, with `y_prob = sigmoid(row sum)` so the alignment check passes
- `xgb_final_model.model`: a few fabricated bytes (only hashed)
- a fake test split `.pth` (zip with `<name>/data.pkl` + `<name>/version`) for the export script test
- an expected canonical CSV for exact comparison

The fixture tests structure and conversion. The marked real data test verifies that the assumptions still match the local production artifact. The parser takes `T`, `F` from the artifact and checks them for consistency; it does not hard code 1134 (only `T = 72` is the observed constant, `SOURCE_TIMESTEPS`).

## 12. Required tests

Target: at least 32 S7 specific tests (the list below).

### Unit

1. The parser accepts the fixture and returns the expected intermediate rows (patient major indexing).
2. Timestep `i` converts to exactly `60.0 * i` minutes.
3. NumPy `float32` prediction values become valid native JSON numbers.
4. SHAP contributions convert to a `{feature: value}` map plus `base_value`.
5. NaN or infinity in a prediction or explanation raises `AdapterError`.
6. Deterministic JSON serialisation produces the exact expected string independent of dictionary insertion order.
7. `prediction_sha256` equals the §5.1 digest over the raw predictions and sidecar bytes; changing either moves it.
8. SHAP directory digest is deterministic regardless of enumeration order and changes on content, rename, add or remove.
9. Wrong SHAP shapes (timestep count, patient count, column count, feature names) raise.
10. A duplicated sidecar id raises (duplicate canonical key).
11. A pickle with a global outside the allowlist raises before any object is built.

### Patient subset and identifier regressions

12. A requested Geneva patient outside the test subset receives zero AI rows without an exception.
13. Mixed requested patients retain test subset patients and omit non test patients.
14. **[REGRESSION]** a sidecar whose ids lost their format (e.g. `900001` instead of `900001_0001`) raises rather than returning a silently empty `AI_OUTPUT`.
15. A sidecar row count different from `len(y_prob) / 72` raises.

### Integration

16. The complete fixture output passes `validate(..., AI_OUTPUT, strict=True, dataset="geneva")`.
17. `load_geneva(..., ai_source=...)` returns clinical frames unchanged, a populated `ai_output`, and the expected `AIArtifactProvenance`.
18. `load_geneva()` without an AI source preserves existing behaviour: canonical empty `ai_output`, `ai_provenance is None`.
19. `build_dataset_loader()` loads Geneva AI when the study provides `geneva_ai` and leaves it absent when omitted; `geneva_ai` with `dataset != geneva` is a config error.
20. Every `geneva_ai` path outside `EHR_SIM_DATA_ROOT` is refused.

### S11g compatibility regressions

21. A fixture dataset with matching provenance passes the S11g provenance comparison and preflight.
22. A modified predictions file or sidecar moves `prediction_sha256` and fails the S11g gate.
23. A modified SHAP file moves `explanation_sha256` and fails the gate.
24. `model_system_version` equals the SHA256 of the model file and differs from an unrelated `ai_intervention.model_system_version`.

### Leakage and integrity regressions

25. **[REGRESSION]** `y_true` values (set to a recognisable sentinel pattern in the fixture) never reach `output_json`, the frame, or exception text.
26. **[REGRESSION]** the row for `t` is built from `shap[t]` only: perturbing `shap[t']` for every `t' != t` leaves the row for `t` byte identical.
27. **[REGRESSION]** timestep alignment (§7.1): hand written expected `(patient, t_minutes, probability)` triples for fixture rows `i = 0, 1, 71`.
28. **[REGRESSION]** swapping two patients' rows in the SHAP file (or in the sidecar relative to predictions) fails the alignment check.
29. Hash and parse come from the same bytes: replacing the file after it has been read leaves the reported hash matching the parsed content.
30. `load_geneva(..., strict=False, ai_source=...)` still rejects a malformed AI artifact.
31. `validate-adapter` prints the loaded provenance values and no payload content.

### Optional explanations

32. `geneva_ai` without `explanations_dir` loads: payloads have no `explanation` key, `explanation_sha256 is None`, and a matching S11g config without `explanation_artifact_sha256` passes preflight.
33. A study configuring `explanation_artifact_sha256` against a dataset loaded without SHAP fails the S11g gate (and the reverse).

### Export script

34. `export_geneva_test_ids.py` on the fake `.pth` writes the expected sidecar, and refuses when the outcome / `y_true` order cross check fails.

### Real data

35. `@pytest.mark.real_data` loads the real artifacts with a sidecar exported by the script, validates the canonical frame strictly, asserts 38 376 rows over 533 patients, checks uniqueness, the alignment check, ids ⊆ clinical CSV ids, and records AI parse + hash wall time into §14.

The default CI suite does not require private Geneva artifacts.

All pre S7 tests remain green.

## 13. Implementation order

### Commit 1: fixture

- Explore A–D are done (§1)
- fixture builder + fixture
- parser unit tests first

### Commit 2: Geneva AI parser

- `geneva_ai.py`: restricted unpickler, sidecar, predictions, SHAP, alignment check, JSON, digests
- parser tests green

### Commit 3: Geneva and configuration integration

- `geneva_ai` config block
- `load_geneva(..., ai_source=...)`
- `build_dataset_loader()` integration
- path guards
- `validate-adapter` provenance output
- integration tests

### Commit 4: export script, S11g and real data regressions

- `scripts/export_geneva_test_ids.py` + test
- provenance compatibility tests
- leakage and alignment regressions
- real data smoke + load time measurement
- documentation (CLAUDE.md, README, ROADMAP)
- full test suite

## 14. Acceptance criteria

S7 is complete when all of the following hold:

1. The real Geneva prediction artifact loads without model inference and without torch, shap or xgboost.
2. Every returned AI row passes the unchanged canonical `AI_OUTPUT_SCHEMA` in strict mode.
3. Prediction and SHAP values are represented in deterministic standard JSON; NumPy `float32` serialises correctly.
4. Every retained row passes the SHAP/prediction alignment check.
5. Legitimate Geneva patients outside the test subset receive empty AI output without an exception.
6. A sidecar id format regression cannot silently turn the artifact into an empty intersection.
7. `GenevaDataset.ai_provenance` contains the §5.1 prediction digest, the §5.2 SHAP digest (or `None`), and the model file SHA256.
8. A study loaded through `build_dataset_loader()` receives the same AI output and provenance as direct adapter use.
9. Existing S11g boot and preflight checks accept a matching Geneva artifact and refuse a mismatching one.
10. Loading Geneva without AI configuration behaves exactly as before S7.
11. No real patient data, ids or model outputs are committed.
12. No `output_json` contains `y_true` or information from timesteps after its own `t`.
13. Prediction timesteps follow §7.1 (`i → 60i`).
14. `validate-adapter` reports the loaded provenance; README documents the sidecar export and provenance steps.
15. All default tests pass and the real data smoke passes locally.
16. Measured AI load and hash time on the real artifacts: 42.3 s for all 533 patients (38 376 rows, with SHAP; dominated by JSON encoding and strict validation), 4.6 s for 50 patients (Explore B: 0.46 s for the SHAP file alone).

## 15. S8 handoff

S7 ends at a validated and provenance identified canonical dataset.

S8 may assume:

- `dataset.ai_output` contains real Geneva AI rows when configured, restricted to the 533 test patients
- timepoints are canonical whole hour minutes, `0..4260`
- `output_json` is deterministic valid JSON: `probability` (6 hour early neurological deterioration risk) and, when configured, `explanation.base_value` + `explanation.contributions` (1134 features, log odds); a missing `explanation` key means "no explanation"
- feature names are the model's engineered names (`lag2_`, `rolling_mean_`, …); S8 owns any display mapping
- each row refers to one exact patient/timepoint/model
- `dataset.ai_provenance` identifies the loaded artifacts
- S11g can select the configured model row at exactly the active timepoint

S8 owns rendering and presentation. It must not reopen or reinterpret the pickle or SHAP files.
