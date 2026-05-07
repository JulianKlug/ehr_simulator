# Session 04 — MIMIC-III adapter

**Goal:** `load_mimic(csv_path, params_dir)` returns a `MimicDataset` whose four frames pass `validate(..., strict=True)` against the canonical schemas locked in S1, against the real MIMIC-III preprocessed-features CSV referenced in `.EXAMPLE_DATA_PATHS`. S4 is the contract-generalization gauntlet: it proves that the data contract S1 wrote and S3 stress-tested against Geneva survives a second real dataset that differs in source vocabulary, normalization-params filename, patient count, and units availability. The byproduct is `_shared.py` — the lifted home for every helper whose signature S3 declared but S4 confirms is dataset-agnostic.

**Out of scope (later sessions):** AI predictions for MIMIC (no upstream `test_predictions.pkl` exists; deferred), CLI / `validate-adapter` / `preflight` / `study_config.yaml` (S5), SQLite (S6), Geneva AI predictions (S7), real-data UI on Geneva (S8 — still uses Geneva, not MIMIC), answer capture / question gating / CSV export (S9a-c), divergence view (S10), arm randomization (S11), DICOM rendering, FHIR layer.

---

## Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | No new top-level deps. No new dev deps (MIMIC has no upstream xlsx units source; `openpyxl` from S3 stays unused by S4). |
| 2  | `src/ehr_simulator/ingestion/_shared.py` | NEW. Hosts **12 lifted helpers** (7 leaf + 5 orchestrator) + `CategoricalGroup` dataclass, with one-arg `dataset` keyword parameterization on every helper that previously hardcoded `_DATASET_NAME = "geneva"`. The orchestrator helpers (`_read_features_csv`, `_build_scalar_ts`, `_apply_scalar_ts_inverse_normalize`, `_build_admission`, `_validate_and_collect`) carry `dataset` + `units` + `registry_source` + `required_columns` parameterization so the adapter body shrinks to a thin wrapper. |
| 3  | `src/ehr_simulator/ingestion/geneva.py` | MODIFIED. Imports all 12 lifted helpers from `_shared`. Keeps `_load_units` (Geneva-specific) and the public `load_geneva()` wrapper that calls into `_shared` with Geneva-specific args (registry_source="stroke_registry", units=_load_units(), filenames). All 26 S3 tests stay green throughout. |
| 4  | `src/ehr_simulator/ingestion/mimic.py` | NEW. `load_mimic` adapter + module docstring with routing table. Thin wrapper over `_shared.*` + `canonical.empty_frame`. No units handling (`unit=None` on every SCALAR_TS row via `units=None` arg). |
| 5  | `src/ehr_simulator/ingestion/__init__.py` | MODIFIED. Re-export `MimicDataset`, `load_mimic`. |
| 6  | `tests/fixtures/geneva/...` | MOVED. S3's fixture files relocate from `tests/fixtures/` into `tests/fixtures/geneva/` (mechanical refactor; the conftest fixture path updates to match). Keeps Geneva and MIMIC fixture files in disjoint subdirectories so identically-named CSVs do not collide. |
| 7  | `tests/fixtures/mimic/build_mimic_fixture.py` | NEW. Deterministic builder reading the real MIMIC CSV; outputs the four fixture files below. Supports `--check` mode that runs the build logic in-memory, diffs against the on-disk sidecar JSON, and exits non-zero on drift (CI step in §9). Mirror of `gen_data_contract.py --check` from S3. |
| 8  | `tests/fixtures/mimic/mimic_sample.csv` | NEW. 2-patient slice of the real CSV; not anonymized (z-scored values carry no PHI; patient IDs pseudonymized). |
| 9  | `tests/fixtures/mimic/reference_population_normalisation_parameters.csv` | NEW. Verbatim copy of upstream `logs_16022026_095909/reference_population_normalisation_parameters.csv` (68 rows; no PHI). |
| 10 | `tests/fixtures/mimic/categorical_variable_encoding.csv` | NEW. Verbatim copy of upstream `logs_16022026_095909/categorical_variable_encoding.csv` (20 rows; no PHI). |
| 11 | `tests/fixtures/mimic/mimic_fixture_expected.json` | NEW. Sidecar ADMISSION expected-output JSON for the exact-match assertion in test #9. |
| 12 | `tests/test_shared.py` | NEW. Lift-equivalence tests + ROADMAP-mandated parity regression (function-identity + behavioral cross-vocabulary parity sub-test) + 2 tests for FNF-wrapping in lifted loaders + 1 test for unrecognized-source defensive issue emission in `_read_features_csv`. |
| 13 | `tests/test_mimic.py` | NEW. Unit + integration + E2E + regression tests for the adapter. |
| 14 | `tests/test_mimic_real.py` | NEW. Real-CSV smoke test marked `@pytest.mark.real_data` (skipped by default). |
| 15 | `tests/conftest.py` | MODIFIED. Add `mimic_fixture_dir`. Update `geneva_fixture_dir` path to `tests/fixtures/geneva/`. |

`.github/workflows/ci.yml` gets **one new step** (`MIMIC fixture sidecar drift check` — see §9, post-OV.1). The existing `Data-contract drift check` step from S3 still runs and still passes (`canonical.py` is untouched in S4). The `real_data` pytest marker is already registered in S3.

---

## Repo layout after Session 4 (diff vs end-of-S3)

```
ehr_simulator/
├── src/ehr_simulator/ingestion/
│   ├── __init__.py                                    # MODIFIED (+MimicDataset, +load_mimic)
│   ├── _shared.py                                     # NEW
│   ├── canonical.py                                   # unchanged
│   ├── exceptions.py                                  # unchanged
│   ├── data/                                          # unchanged
│   │   └── geneva_units.json                          # unchanged
│   ├── geneva.py                                      # MODIFIED (imports from _shared)
│   ├── mimic.py                                       # NEW
│   └── synthetic.py                                   # unchanged
└── tests/
    ├── conftest.py                                    # MODIFIED (+mimic_fixture_dir; geneva_fixture_dir path update)
    ├── fixtures/
    │   ├── geneva/                                    # NEW (mechanical move from tests/fixtures/)
    │   │   ├── build_geneva_fixture.py                # MOVED
    │   │   ├── geneva_sample.csv                      # MOVED
    │   │   ├── normalisation_parameters.csv           # MOVED
    │   │   ├── categorical_variable_encoding.csv      # MOVED
    │   │   └── geneva_fixture_expected.json           # MOVED
    │   └── mimic/                                     # NEW
    │       ├── build_mimic_fixture.py                 # NEW
    │       ├── mimic_sample.csv                       # NEW
    │       ├── reference_population_normalisation_parameters.csv  # NEW
    │       ├── categorical_variable_encoding.csv      # NEW
    │       └── mimic_fixture_expected.json            # NEW
    ├── test_data_contract.py                          # unchanged
    ├── test_geneva.py                                 # unchanged (imports still resolve via geneva.py → _shared.py)
    ├── test_geneva_real.py                            # unchanged
    ├── test_mimic.py                                  # NEW
    ├── test_mimic_real.py                             # NEW
    └── test_shared.py                                 # NEW
```

---

## 1. Data inputs (verbatim from `.EXAMPLE_DATA_PATHS`)

The adapter accepts paths to:

- **MIMIC preprocessed features CSV:** `/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/mimic_prepro_16022026_095909/preprocessed_features_16022026_095909.csv`
- **Normalization + categorical encoding directory:** `/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/mimic_prepro_16022026_095909/logs_16022026_095909/`

There is no upstream xlsx units source for MIMIC. The adapter ships with `unit=None` on every SCALAR_TS row.

Real-CSV facts the spec is built on (verified via direct inspection 2026-05-06):

- **CSV columns:** `relative_sample_date_hourly_cat`, `case_admission_id`, `sample_label`, `source`, `value` — **no unnamed-index column** (vs Geneva has one). 1,831,752 data rows. 247 unique `case_admission_id`s. 103 unique `sample_label`s (same count as Geneva). Hour buckets 0–71 (same window as Geneva).
- **All 8 observed `source` values:** `EHR`, `EHR_locf_imputed`, `EHR_pop_imputed`, `EHR_pop_imputed_locf_imputed`, `notes`, `notes_locf_imputed`, `missing_pop_imputed`, `missing_pop_imputed_locf_imputed`. **Substring match on `"imputed"` drops 6 of 8** (the two non-imputed survivors are `EHR` and `notes`). `notes` is the registry-equivalent — replaces Geneva's `stroke_registry`.
- **`notes` rows are already one-hot expanded in `sample_label`** (`sex_male`, `medhist_*_yes`, `categorical_iat_*`, `prestroke_disability_(rankin)_*.0`, …) — same convention as Geneva. 35 distinct labels emitted under `source = "notes"`.
- **`notes` rows repeat across the 72 hour buckets per patient.** Admission is static — adapter takes the `t=0` slice only.
- **`reference_population_normalisation_parameters.csv` columns:** `variable, original_mean, original_std`. 68 rows. **Filename differs** from Geneva's `normalisation_parameters.csv`; the adapter hardcodes the MIMIC-specific filename when joining `params_dir`.
- **`categorical_variable_encoding.csv` columns:** `sample_label, baseline_value, other_categories` (Python-list-as-string). 20 rows (vs Geneva's 19).
- **Imaging-derived scalars** (`cbf_lt_30`, `tmax_gt_6`, `hypoperfusion_with_mismatch`, etc.) live in `EHR` rows and route through SCALAR_TS, exactly like Geneva. The IMAGING canonical shape stays empty for MIMIC by design.

---

## 2. `pyproject.toml` deltas

None. No new top-level deps; no new dev deps. The lifted helpers reuse what is already in the project (`pandas>=2.2`, `pandera[pandas]>=0.20`, stdlib `ast` and `os`).

---

## 3. `src/ehr_simulator/ingestion/_shared.py` — module skeleton

```python
"""Adapter helpers shared between Geneva (S3) and MIMIC (S4) and any future adapter
whose source CSV follows the OPSUM preprocessed-features layout.

Every helper here was first authored as a module-scope helper in :mod:`ehr_simulator.ingestion.geneva`
during S3 and lifted here in S4 once MIMIC confirmed the signatures match. The
roadmap bar is "signatures match exactly"; helpers that previously hardcoded
``_DATASET_NAME = "geneva"`` for error messages now accept ``dataset`` as a
keyword-only argument so call sites are explicit.

The :class:`CategoricalGroup` dataclass is defined here, not in any single
adapter, because both Geneva and MIMIC categorical encoding files share the
same shape (``sample_label, baseline_value, other_categories``) and the same
one-hot expansion convention.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue
```

### `CategoricalGroup` dataclass

Lifted verbatim from S3 §3 with no signature change:

```python
@dataclass(frozen=True)
class CategoricalGroup:
    group_name: str
    baseline: str
    other_labels: tuple[str, ...]
    one_hot_columns: tuple[str, ...]
```

### Lifted helpers (12 total: 7 leaf + 5 orchestrator)

#### Leaf helpers (7)

| Function | Signature | Lift status |
|---|---|---|
| `_drop_imputed` | `(frame: pd.DataFrame) -> pd.DataFrame` | **Cut-and-paste from S3.** Pure substring filter; no parameterization needed. |
| `_inverse_normalize` | `(z: float \| pd.Series, mean: float, std: float) -> float \| pd.Series` | **Cut-and-paste from S3.** Pure math. |
| `_one_hot_column_name` | `(group_name: str, label: str) -> str` | **Cut-and-paste from S3.** Pure naming convention; same for both datasets (verified against MIMIC's 20 categorical groups during S4 fixture build). |
| `_path_traversal_guard` | `(path: Path, root: Path \| None, *, dataset: str) -> Path` | **Lifted with `dataset` kwarg.** Body unchanged; `IngestionIssue.dataset` now reflects the kwarg. |
| `_load_normalisation_params` | `(path: Path, *, dataset: str) -> dict[str, tuple[float, float]]` | **Lifted with `dataset` kwarg + FNF wrapping.** Caller passes the full path (Geneva: `normalisation_parameters.csv`; MIMIC: `reference_population_normalisation_parameters.csv`). `dataset` flows into `AdapterError` messages. **Wraps `FileNotFoundError` as `AdapterError(message="[{dataset}] {path.name} not found at {path}", issues=[...])`** so a missing file surfaces a clean error instead of a raw pandas trace. Tested by test_shared #5. |
| `_load_categorical_encoding` | `(path: Path, sample_labels: set[str], *, dataset: str) -> dict[str, CategoricalGroup]` | **Lifted with `dataset` kwarg + FNF wrapping.** Same CSV schema for both datasets. The naming-convention self-check (S3 post-review-1.4) re-runs against MIMIC's 20 groups and is locked by test #3 in `test_mimic.py`. **Wraps `FileNotFoundError` identically to `_load_normalisation_params`.** Tested by test_shared #6. |
| `_decode_categorical` | `(rows_for_group: pd.DataFrame, group: CategoricalGroup, *, strict: bool, patient_id: str, dataset: str) -> tuple[str, IngestionIssue \| None]` | **Lifted with `dataset` kwarg.** ≥0.5 threshold + group re-expansion; tier-by-tier behavior carried verbatim from S3 §3 (strict raises, lenient picks argmax + emits issue, empty-rows behavior). The `dataset` kwarg flows into every emitted `IngestionIssue`. |

#### Orchestrator helpers (5, lifted from `geneva.py:177-641`)

| Function | Signature | Lift status |
|---|---|---|
| `_read_features_csv` | `(csv_path: Path, *, required_columns: tuple[str, ...], dataset: str, chunksize: int = 500_000) -> pd.DataFrame` | **Renamed from `_read_geneva_csv` + parameterized.** Header-only required-column check, then chunked read with per-chunk `_drop_imputed` filter, then concat. The `value` column is intentionally NOT in the dtype dict so pandera's `coerce=True` can drop a malformed-value row in lenient mode. **Defensive-issue emission for unrecognized source values** is added to the lifted version: any chunk row whose `source` does not appear in the dataset's known vocabulary surfaces an `IngestionIssue(dataset, reason="unrecognized source value: {src}")` so a future Geneva CSV with `notes` rows (or any new vocab drift) does not silently drop. Tested by test_shared #7. |
| `_build_scalar_ts` | `(ehr_rows: pd.DataFrame, norm_params: dict[str, tuple[float, float]], *, units: dict[str, str] \| None = None, dataset: str) -> tuple[pd.DataFrame, list[IngestionIssue]]` | **Lifted with `units` Optional parameterization.** `units=None` means "every output row gets `unit=None`" (MIMIC's case); `units={...}` means "look up unit per variable, fall back to None when missing" (Geneva's case). Builds the SCALAR_TS frame with raw z-scored values + collects missing-from-norm-params issues. Inverse-normalization is intentionally deferred to `_apply_scalar_ts_inverse_normalize` so pandera's `coerce=True` can catch malformed strings before normalization eats them. |
| `_apply_scalar_ts_inverse_normalize` | `(frame: pd.DataFrame, norm_params: dict[str, tuple[float, float]]) -> pd.DataFrame` | **Cut-and-paste from S3.** Per-variable mask + multiply. Runs AFTER pandera validate. **Depends on the canonical SCALAR_TS schema having no range check on `value`** (`canonical.py:88` — nullable, no checks); a future schema change adding e.g. `value >= 0` would break both adapters. Documented in the helper docstring. |
| `_build_admission` | `(registry_rows: pd.DataFrame, norm_params: dict[str, tuple[float, float]], cat_groups: dict[str, CategoricalGroup], *, strict: bool, dataset: str) -> tuple[pd.DataFrame, list[IngestionIssue]]` | **Lifted with `dataset` kwarg.** Filters registry rows to `t_minutes == 0.0` (the canonical anchor for both Geneva and MIMIC — verified by Explore A: `notes` rows repeat across all 72 hour buckets per patient). Decodes categorical groups via `_decode_categorical`, inverse-normalizes continuous registry vars, str-coerces with `str(round(raw, 2))`. Emits `IngestionIssue` for orphan registry variables. The semantic lock (`t=0` slice + str-coerce convention) transfers to MIMIC unchanged. |
| `_validate_and_collect` | `(frame: pd.DataFrame, shape: CanonicalShape, *, strict: bool, issues: list[IngestionIssue], dataset: str) -> pd.DataFrame` | **Cut-and-paste from S3 + `dataset` kwarg.** Thin wrapper around `validate(...)` that collects `frame.attrs["adapter_error"].issues` into the dataset's issues list when in lenient mode. |

### Helpers that do NOT lift

- `_load_units()` stays in `geneva.py`. MIMIC has no units source, so MIMIC's SCALAR_TS construction passes `units=None` to the lifted `_build_scalar_ts`.
- `empty_frame(shape)` already lives in `canonical.py` as of S3 (post-review-2.1); both adapters consume it from there.

### Geneva refactor (in commit 1)

`geneva.py` becomes a thin wrapper:

```python
from ehr_simulator.ingestion._shared import (
    CategoricalGroup,
    _apply_scalar_ts_inverse_normalize,
    _build_admission,
    _build_scalar_ts,
    _decode_categorical,
    _drop_imputed,
    _inverse_normalize,
    _load_categorical_encoding,
    _load_normalisation_params,
    _one_hot_column_name,
    _path_traversal_guard,
    _read_features_csv,
    _validate_and_collect,
)
```

`load_geneva()` shrinks to the public-API wrapper that resolves `EHR_SIM_DATA_ROOT`, loads norm_params + cat_groups + units, calls `_shared._read_features_csv(..., dataset="geneva", required_columns=_REQUIRED_COLUMNS)`, dispatches into `_shared._build_scalar_ts(..., units=_load_units(), dataset="geneva")` and `_shared._build_admission(..., dataset="geneva")`, then validates via `_shared._validate_and_collect(..., dataset="geneva")`. `_load_units` stays defined in `geneva.py`. All 26 S3 tests stay green throughout commit 1 (the parity test in commit 1 confirms equivalence before MIMIC depends on the lifted helpers).

---

## 4. `src/ehr_simulator/ingestion/mimic.py` — module skeleton

```python
"""MIMIC-III preprocessed-features adapter.

Loads the MIMIC stroke-cohort CSV at the path configured in
``.EXAMPLE_DATA_PATHS`` into the four canonical in-memory shapes from
:mod:`ehr_simulator.ingestion.canonical`. MIMIC mirrors Geneva's preprocessed
layout one-for-one with three deliberate differences:

==============================  ==========================================
Difference                      Handling
==============================  ==========================================
no unnamed-index column         ``pd.read_csv`` is called WITHOUT
                                ``index_col=0`` (Geneva has one; MIMIC
                                does not)
``notes`` replaces              ``stroke_registry`` is never matched;
``stroke_registry``             ``frame[frame.source == "notes"]`` routes
                                to ``ADMISSION``
no units source                 every SCALAR_TS row ships ``unit=None``;
                                ``_load_units`` is not imported
==============================  ==========================================

Routing rules (encoded in ``load_mimic``):

==============================  ==========================================
``source`` value                Action
==============================  ==========================================
contains ``"imputed"``          drop before validation (substring match,
                                via :func:`_shared._drop_imputed`)
``"EHR"`` (exact)               route to ``SCALAR_TS``; inverse-normalize via
                                ``reference_population_normalisation_parameters.csv``
                                when ``variable`` is in the params (passthrough
                                + issue when not, per S3 post-review-3.2);
                                ``unit = None``;
                                ``t_minutes = relative_sample_date_hourly_cat * 60.0``
``"notes"`` (exact)             route to ``ADMISSION``; take the ``t=0``
                                slice only; for one-hot categorical groups
                                listed in ``categorical_variable_encoding.csv``,
                                apply the ≥0.5 thresholding via
                                :func:`_shared._decode_categorical`; for
                                continuous registry vars, inverse-normalize
                                and str-coerce
==============================  ==========================================

``IMAGING`` and ``AI_OUTPUT`` are returned as empty-but-conforming DataFrames
via :func:`canonical.empty_frame`. MIMIC imaging-derived scalars
(``cbf_lt_30``, ``tmax_gt_6``, ``hypoperfusion_with_mismatch``, …) live in
``EHR`` rows and route through ``SCALAR_TS`` — same as Geneva. AI predictions
for MIMIC are not in scope for any current session (no upstream pkl exists).

The ``EHR_SIM_DATA_ROOT`` environment variable, when set, scopes both
``csv_path`` and ``params_dir`` via :func:`_shared._path_traversal_guard`.
Empty-string handling matches Geneva's S3 contract (post-review-1.5):
``os.environ.get("EHR_SIM_DATA_ROOT") or None`` is used, so an explicitly-
empty value is treated identically to unset.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ehr_simulator.ingestion._shared import (
    _apply_scalar_ts_inverse_normalize,
    _build_admission,
    _build_scalar_ts,
    _load_categorical_encoding,
    _load_normalisation_params,
    _path_traversal_guard,
    _read_features_csv,
    _validate_and_collect,
)
from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue

_DATASET_NAME = "mimic"
_NORM_PARAMS_FILENAME = "reference_population_normalisation_parameters.csv"
_CATEGORICAL_ENCODING_FILENAME = "categorical_variable_encoding.csv"
_REQUIRED_COLUMNS = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)
_NON_IMPUTED_SOURCES = ("EHR", "notes")
_REGISTRY_SOURCE = "notes"
```

### Public API

```python
@dataclass
class MimicDataset:
    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame
    issues: list[IngestionIssue] = field(default_factory=list)


def load_mimic(
    csv_path: Path,
    params_dir: Path,
    *,
    strict: bool = True,
) -> MimicDataset:
    """Load the MIMIC preprocessed-features CSV into the four canonical shapes.

    ``params_dir`` must contain ``reference_population_normalisation_parameters.csv``
    and ``categorical_variable_encoding.csv``. Both ``csv_path`` and
    ``params_dir`` are validated via :func:`_shared._path_traversal_guard`
    against ``EHR_SIM_DATA_ROOT`` if set.

    ``strict=True`` (default): every frame is validated with pandera's eager
    mode; first violation raises :class:`AdapterError`.

    ``strict=False``: lenient validation collects offending rows into
    ``MimicDataset.issues`` and returns the surviving rows. Imaging /
    AI_OUTPUT frames are empty either way. Every SCALAR_TS row ships
    ``unit=None`` (MIMIC has no upstream units source).
    """
```

The body mirrors `load_geneva` exactly except for the three differences enumerated in the module docstring (no `index_col=0`, `"notes"` registry source, `unit=None` in SCALAR_TS rows). Helpers carry `dataset="mimic"` on every call.

---

## 5. Routing logic (the core of `load_mimic`)

After Decision A1 (orchestrators lifted to `_shared.py`), `load_mimic`'s body is a thin wrapper that supplies MIMIC-specific args to the lifted orchestrators. The two-step inverse-normalize pattern (build raw, validate, then apply inverse) carries over verbatim from Geneva (`geneva.py:503-560`) — it's load-bearing because pandera's `coerce=True` must see raw strings to drop malformed rows in lenient mode.

Order of operations:

1. **Path guard.** Resolve `EHR_SIM_DATA_ROOT` via `root_str = os.environ.get("EHR_SIM_DATA_ROOT") or None`. Call `_path_traversal_guard(csv_path, root, dataset="mimic")` and `_path_traversal_guard(params_dir, root, dataset="mimic")`.
2. **Read features CSV.** Call `frame = _read_features_csv(csv_path, required_columns=_REQUIRED_COLUMNS, dataset="mimic")`. The lifted helper handles: header-only required-column check (raises `AdapterError` on missing column listing the gap), chunked read with `chunksize=500_000` and `usecols=_REQUIRED_COLUMNS`, dtype map for `case_admission_id`/`sample_label`/`source` as `str` (`value` intentionally absent so pandera's `coerce=True` can drop malformed rows), per-chunk `_drop_imputed` filter, and concat. **No `index_col=0`** is passed because MIMIC's CSV has no unnamed-index column (the lifted helper detects this from the header check; Geneva passes `index_col=0` via a kwarg). Defensive: any chunk row whose `source` is not in the dataset's known vocabulary surfaces `IngestionIssue(reason="unrecognized source value: {src}")` collected on `MimicDataset.issues` (locks the silent-drop failure mode for both adapters per outside-voice finding #9).
3. **Load params.** `norm_params = _load_normalisation_params(params_dir / _NORM_PARAMS_FILENAME, dataset="mimic")`; `cat_groups = _load_categorical_encoding(params_dir / _CATEGORICAL_ENCODING_FILENAME, sample_labels=set(frame["sample_label"].unique().tolist()), dataset="mimic")`. Both raise `AdapterError` (FNF-wrapped) if the file is missing.
4. **Time + ID coercion.** `frame = frame.copy(); frame["t_minutes"] = frame["relative_sample_date_hourly_cat"].astype(float) * 60.0; frame = frame.rename(columns={"case_admission_id": "patient_id"})`.
5. **Source split.** `ehr_rows = frame[frame.source == "EHR"]` and `registry_rows = frame[frame.source == _REGISTRY_SOURCE]` (where `_REGISTRY_SOURCE = "notes"` for MIMIC; Geneva passes `"stroke_registry"`).
6. **SCALAR_TS build (raw values, defer normalize).** `scalar_ts, scalar_issues = _build_scalar_ts(ehr_rows, norm_params, units=None, dataset="mimic")`. The lifted helper: builds the SCALAR_TS frame with the raw z-scored `value` column (NOT yet inverse-normalized), sets `source = "EHR"` and `unit = None` for every row (because `units=None`), and appends `IngestionIssue(reason="variable {x} missing from reference_population_normalisation_parameters")` for any sample_label not in `norm_params`.
7. **ADMISSION build.** `admission, admission_issues = _build_admission(registry_rows, norm_params, cat_groups, strict=strict, dataset="mimic")`. The lifted helper filters to `t_minutes == 0.0`, decodes categorical groups via `_decode_categorical(..., dataset="mimic")`, inverse-normalizes continuous registry vars and `str(round(raw, 2))`-coerces, emits `IngestionIssue(reason="orphan registry variable: {sample_label}")` for orphans.
8. **Empty IMAGING / AI_OUTPUT.** `imaging = empty_frame(CanonicalShape.IMAGING)`; `ai_output = empty_frame(CanonicalShape.AI_OUTPUT)`. Reused from `canonical.py`.
9. **Validate every frame.** Each call: `frame = _validate_and_collect(frame, shape, strict=strict, issues=issues, dataset="mimic")`. The lifted helper wraps `validate(...)`, raising `AdapterError` in strict mode and accumulating `frame.attrs["adapter_error"].issues` into the dataset's issues list in lenient mode.
10. **Apply inverse-normalize to validated SCALAR_TS.** `scalar_ts = _apply_scalar_ts_inverse_normalize(scalar_ts, norm_params)`. **This step runs AFTER `_validate_and_collect` so pandera's `coerce=True` could already have dropped a malformed row in lenient mode.** This is the load-bearing two-step pattern.
11. Return `MimicDataset(scalar_ts, admission, imaging, ai_output, issues)`.

---

## 6. `EHR_SIM_DATA_ROOT` env-var contract

Identical to S3 §6, now via the lifted `_shared._path_traversal_guard`. Empty-string handling per S3 post-review-1.5 carries over. Test #6 in `test_mimic.py` covers the all-four-states matrix (unset, empty-string, inside-root, outside-root) for `dataset="mimic"`.

---

## 7. Fixture strategy

`tests/fixtures/mimic/build_mimic_fixture.py` is run **once** during S4 implementation, then again only on upstream schema changes. Behavior mirrors `build_geneva_fixture.py` (S3 §8):

1. Reads `/mnt/data1/klug/datasets/opsum/.../preprocessed_features_16022026_095909.csv` (path argv-configurable; defaults to the constant path for reproducibility).
2. Picks 2 patient ids deterministically: the patient with the most `EHR` rows + the patient with the most `notes` rows (sorted by id as a tiebreaker). The two ids may coincide; if so, fall back to the second-most for the second slot.
3. Filters the CSV to those 2 patients only. Keeps all `(t, sample_label, source)` rows for each — no value sampling, no row dropping. Result is small enough to commit (~5K rows × 5 columns ≈ 200 KB).
4. **Replaces the two `case_admission_id`s with `mimic_fixture_001` / `mimic_fixture_002`** so the committed CSV has no PHI link to the source data.
5. **Does NOT alter any `value` cells.** The values are already z-scored against a reference population; they carry no PHI by construction. The audit trail for "no anonymization needed" lives in this spec section so reviewers do not re-litigate it.
6. Writes `tests/fixtures/mimic/mimic_sample.csv` (no index column).
7. Copies `logs_16022026_095909/reference_population_normalisation_parameters.csv` → `tests/fixtures/mimic/reference_population_normalisation_parameters.csv` byte-for-byte.
8. Copies `logs_16022026_095909/categorical_variable_encoding.csv` → `tests/fixtures/mimic/categorical_variable_encoding.csv` byte-for-byte.
9. **Sidecar expected-admission JSON:** the builder runs `load_mimic()` against the just-written fixture and serializes the resulting ADMISSION frame to `tests/fixtures/mimic/mimic_fixture_expected.json` as `{"mimic_fixture_001": {field: value, ...}, "mimic_fixture_002": {...}}`. Test #9 asserts exact-match against this JSON.

10. **`--check` mode (post-outside-voice).** The builder accepts a `--check` flag that runs steps 1-9 in-memory without writing any files, then diffs the in-memory sidecar against the on-disk `mimic_fixture_expected.json` and exits 0 on match, 1 with a unified-diff snippet to stderr on drift. CI runs this after `pytest` (§9). Mirror of `gen_data_contract.py --check` from S3. This makes silent re-baselines visible: any drift in `load_mimic()` output between fixture regenerations becomes a CI failure that requires the developer to commit the regenerated JSON (visible in PR diff). **Without `--check`**, the bootstrap loop where `build_mimic_fixture.py` calls `load_mimic()` to generate the very JSON that test #9 then asserts against would mean a regression in `load_mimic` could silently re-baseline the sidecar at fixture-refresh time; the `--check` mode + the 3-5 hand-curated anchor assertions in test #9 (see §8) close this gap together.

The builder is deterministic (no RNG; tie-breaks by sort). Builder script is committed; the four output files (CSV ×3 + sidecar JSON) are committed. Re-running the builder regenerates byte-identical files unless upstream schema changes; `--check` enforces this in CI.

`tests/conftest.py` adds:

```python
@pytest.fixture
def mimic_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "mimic"
```

and updates `geneva_fixture_dir` to `Path(__file__).parent / "fixtures" / "geneva"`.

---

## 8. Test inventory (target ≥10 from ROADMAP; final count = 22 new tests)

Numbered to match commits in §12 below.

### `tests/test_shared.py` (7 — including the mandated parity regression)

1. **`test_shared_drop_imputed_handles_geneva_and_mimic_source_vocabularies`** — parametrized over Geneva (`EHR_locf_imputed`, `stroke_registry_pop_imputed`, …) and MIMIC (`notes_locf_imputed`, `missing_pop_imputed`, …) source values; assert substring match on `"imputed"` drops them all in one pass. Locks the lift behavior against both source vocabularies.

2. **`test_shared_inverse_normalize_pure_math`** — round-trip `_inverse_normalize(_normalize(x), m, s) ≈ x` on real `(mean, std)` pairs pulled from BOTH `tests/fixtures/geneva/normalisation_parameters.csv` AND `tests/fixtures/mimic/reference_population_normalisation_parameters.csv`. Locks the helper math against both real parameter sets.

3. **`test_shared_path_traversal_guard_dataset_param_in_issue`** — call `_path_traversal_guard(/tmp/foo, /data, dataset="geneva")` and `_path_traversal_guard(/tmp/foo, /data, dataset="mimic")`; assert the resulting `IngestionIssue.dataset` field reflects the kwarg in each case.

4. **`test_shared_helpers_produce_identical_output_for_equivalent_inputs`** — **ROADMAP-MANDATED PARITY REGRESSION (layered).**
   - **Sub-assertion (a) — function identity:** `from ehr_simulator.ingestion.geneva import _drop_imputed as f1; from ehr_simulator.ingestion.mimic import _drop_imputed as f2; assert f1 is f2`. Same for every helper exported by `_shared.py.__all__` (12 helpers post-A1). Catches accidental re-fork.
   - **Sub-assertion (b) — behavioral parity on synthetic cross-vocabulary inputs:** construct a synthetic frame containing rows from BOTH Geneva (`stroke_registry_pop_imputed`) and MIMIC (`notes_locf_imputed`, `missing_pop_imputed`) source vocabularies; run through `_shared._drop_imputed` and assert the same row count survives regardless of caller; same pattern for `_inverse_normalize` (same z + (m,s) → same float output to machine epsilon); same for `_decode_categorical` on a synthetic two-class group with one ≥0.5 row (assert same `(decoded_label, None)` tuple regardless of `dataset` kwarg). Locks the cross-vocabulary contract behaviorally, not just by import identity.

5. **`test_shared_load_normalisation_params_wraps_fnf_as_adapter_error`** (post-outside-voice 3.A) — pass a non-existent path to `_load_normalisation_params(missing_path, dataset="mimic")`; assert `AdapterError` raised with message containing `mimic`, the file basename, and the missing path. Sub-assert: `exc.issues[0].dataset == "mimic"` and `exc.issues[0].reason` mentions "not found".

6. **`test_shared_load_categorical_encoding_wraps_fnf_as_adapter_error`** (post-outside-voice 3.A) — same pattern as #5 but for `_load_categorical_encoding(missing_path, sample_labels=set(), dataset="geneva")`. Asserts dataset name flows correctly across both adapters.

7. **`test_shared_read_features_csv_emits_issue_for_unrecognized_source`** (post-outside-voice 3.B, finding #9) — write a tiny tmp CSV containing one row with `source = "unknown_vocab_v2"` (not in either Geneva's or MIMIC's known source vocabularies). Call `_read_features_csv(path, required_columns=_REQUIRED_COLUMNS, dataset="geneva", known_sources=("EHR", "stroke_registry"))`. Assert: (a) the row does not appear in the returned frame's non-imputed survivors; (b) the returned frame has an attached `attrs["unrecognized_sources"]` list (or equivalent) containing `IngestionIssue(dataset="geneva", reason="unrecognized source value: unknown_vocab_v2")`. Locks the silent-drop failure mode for both adapters: if upstream Geneva CSV ever adds a `notes` row (mirroring MIMIC), it surfaces as an issue, not silent loss.

### `tests/test_mimic.py` (14)

#### Unit (7)

1. **`test_mimic_routes_eight_known_sources_to_two_non_imputed`** — inline frame with one row per of the 8 observed MIMIC source values; assert `_drop_imputed` returns exactly 2 rows whose `source ∈ {"EHR", "notes"}`. Locks the MIMIC source vocabulary against schema drift.

2. **`test_hour_bucket_to_minutes_conversion_mimic`** — fed inline rows with `relative_sample_date_hourly_cat ∈ {0, 1, 71}`; assert `t_minutes` in the SCALAR_TS output equals `{0.0, 60.0, 4260.0}`. Verifies the conversion lives at the right step (after `_drop_imputed`, before validate) for MIMIC.

3. **`test_load_categorical_encoding_covers_all_20_groups_mimic`** — load MIMIC fixture; pass the fixture's `sample_label` set to `_load_categorical_encoding(..., dataset="mimic")`; assert (a) returns 20 `CategoricalGroup` entries; (b) every `one_hot_columns` entry across all 20 groups appears in the input `sample_label` set (no orphans); (c) calling with a deliberately-stripped sample_label set raises `AdapterError` listing the missing columns. Locks the deterministic naming-convention bridge for all 20 MIMIC groups (mirror of S3 test #4c for Geneva's 19).

4. **`test_load_normalisation_params_raises_on_missing_column_mimic`** — write a CSV missing `original_std` to `tmp_path`; call `_load_normalisation_params(path, dataset="mimic")`; assert `AdapterError` whose message mentions both `original_std` and `dataset="mimic"`.

5. **`test_decode_categorical_mimic_categorical_iat_three_class`** — pick a multi-class categorical from MIMIC's encoding (e.g. `categorical_iat`, baseline `no_iat`, others `["<270min", "271-540min", ">540min"]`); construct inline `rows_for_group` with exactly one ≥0.5; assert `_decode_categorical(..., dataset="mimic")` returns the matching label and no issue. Verifies the lifted helper handles MIMIC's actual categorical structure end-to-end.

6. **`test_path_traversal_guard_rejects_outside_root_mimic`** — root=`/data`, path=`/tmp/foo`, `dataset="mimic"` → `AdapterError` with `IngestionIssue.dataset == "mimic"`. Sub-asserts: root=None passthrough; root=`/data`, path=`/data/sub/file.csv` returns resolved path; `EHR_SIM_DATA_ROOT=""` empty-string treated as unset.

7. **`test_inverse_normalize_passthrough_emits_issue_mimic`** — fixture variant containing one EHR row with a `sample_label` not present in `reference_population_normalisation_parameters.csv`; assert (a) the row's `value` in the output SCALAR_TS equals the input z-score (untouched); (b) `dataset.issues` contains an `IngestionIssue` with `dataset="mimic"` and `reason` matching `r"variable .* missing from reference_population_normalisation_parameters"`.

#### Integration (5)

8. **`test_load_mimic_routes_sources_correctly`** — uses `mimic_fixture_dir`. Load. Assert:
   - `scalar_ts["source"].unique() == {"EHR"}` (all SCALAR_TS rows came from non-imputed EHR).
   - `admission["field"].unique()` contains decoded categorical names (e.g. `"Sex"`, `"Referral"`) and continuous names (`"age"`); does not contain raw one-hot names like `"sex_male"`.
   - No row in any frame has `source` containing `"imputed"`.

9. **`test_load_mimic_admission_matches_expected_sidecar_plus_anchors`** — **layered (post-outside-voice 3.C).**
   - **Sub-assertion (a) — sidecar exact-match:** load fixture; load `tests/fixtures/mimic/mimic_fixture_expected.json`; for each `(patient_id, field, value)` tuple in the expected JSON, assert an exact-match row exists in `dataset.admission`. Both directions: every expected row exists, no unexpected rows. Mirror of S3 test #9 (sidecar-driven, post-review-3.3).
   - **Sub-assertion (b) — 3-5 hand-curated anchor assertions:** independently of the sidecar, the spec inlines 3-5 `(patient_id, field, value)` assertions derived by reading raw upstream MIMIC CSV at fixture-build time. Examples (concrete values locked when the implementer reads the real CSV): `('mimic_fixture_001', 'Sex', 'Male')` (because the raw CSV has `sex_male=1.0` for that patient at t=0); `('mimic_fixture_001', 'medhist_hypertension', 'yes')`; `('mimic_fixture_002', 'Referral', 'Other hospital')` etc. These anchors are independent of `load_mimic()` so they catch regressions even if the sidecar gets re-baselined. Picked at S4 implementation time from raw rows the implementer manually inspects; recorded with a comment explaining the source-row lookup.
   - The `--check` CI gate (§7 step 10 + §9) catches sidecar drift in CI; anchors catch the high-value semantic regressions.

10. **`test_load_mimic_strict_vs_lenient`** — using fixture in `strict=True` passes. Construct a corrupted variant (one row with `value="not a number"`, written to a `tmp_path` CSV). With `value` NOT in the read_csv dtype dict, `pd.read_csv` succeeds; pandera's `coerce=True` raises in strict mode → `AdapterError`. With `strict=False`, pandera's lazy mode drops the offending row and `dataset.issues` is non-empty.

11. **`test_load_mimic_imaging_and_ai_output_empty_but_conforming`** — using fixture, assert `len(dataset.imaging) == 0`, `len(dataset.ai_output) == 0`, both pass `validate(frame, shape, strict=True)` when re-validated explicitly. Both frames are constructed via the schema-derived `empty_frame()` helper from `canonical.py`. Locks the empty-frame contract for downstream sessions.

12. **`test_load_mimic_orphan_registry_variable_emits_issue`** — fixture variant with one `notes` row whose `sample_label` is neither categorical nor in `reference_population_normalisation_parameters.csv`; assert (a) that row does not appear in `dataset.admission`; (b) `dataset.issues` contains an `IngestionIssue` with `dataset="mimic"` and `reason="orphan registry variable: {sample_label}"`.

#### E2E (1)

13. **`test_load_mimic_full_fixture_roundtrip`** — load fixture; assert:
    - `set(scalar_ts.patient_id.unique()) == {"mimic_fixture_001", "mimic_fixture_002"}`.
    - `set(admission.patient_id.unique()) == {"mimic_fixture_001", "mimic_fixture_002"}`.
    - All `t_minutes` in `scalar_ts` ∈ `[0.0, 71*60.0]`.
    - Every SCALAR_TS row has `unit is None` (locked redundantly with test #15 — kept here to keep the E2E self-contained).
    - All four frames pass `validate(..., strict=True)` again (idempotent).
    - At least N ADMISSION fields per patient, where N is computed from the fixture (~31 fields = 20 categoricals + 11 continuous registry vars; concrete number written into the test once the fixture lands).

#### Regression (2, including the no-units lock)

14. **`test_load_mimic_raises_on_missing_required_columns`** — write a CSV missing the `source` column to `tmp_path`; call `load_mimic` with `params_dir=mimic_fixture_dir`. Assert `AdapterError` whose message mentions `source` (the missing column) AND `dataset="mimic"`. Locks the schema-drift failure mode so an upstream schema change surfaces loudly, not silently.

15. **`test_load_mimic_scalar_ts_unit_is_none_for_all_rows`** — load fixture; assert every SCALAR_TS row has `unit is None`. Locks the no-units-source contract; if a future session adds an auto-load-units path for MIMIC it must update this test deliberately, not silently flip behavior.

### `tests/test_mimic_real.py` (1, opt-in)

16. **`test_load_mimic_real_csv_smoke`** — marked `@pytest.mark.real_data`; **skipped by default** in CI and `pytest -n auto`; runnable via `uv run pytest -m real_data`. Loads `/mnt/data1/klug/datasets/opsum/.../preprocessed_features_16022026_095909.csv` + `.../logs_16022026_095909/` via `load_mimic(strict=False)`. Asserts:
    - load completes within **90 s** wall time (post-eng-review T3; MIMIC is 10× smaller than Geneva but chunked-read fixed cost + pandera validation dominate at low row counts. 90 s gives headroom for slower CI runners and cold filesystem reads on `/mnt`).
    - `dataset.scalar_ts` has rows for "most patients" — concrete count locked when the implementer measures whether all 247 patients have both EHR and notes rows; if some patients lack EHR rows, the assertion is `len(set(scalar_ts.patient_id.unique())) >= N` where N is the measured count, NOT a hardcoded 247.
    - `dataset.admission` has all 247 patients (notes rows are guaranteed per Explore A; verified at fixture-build time).
    - `dataset.issues` is reportable on test failure for debugging.

The `real_data` marker is already registered in `pyproject.toml` from S3.

**Total new tests: 22** (7 in `test_shared.py` + 14 in `test_mimic.py` + 1 in `test_mimic_real.py`). Roadmap bar (≥10) cleared. Project total after S4: **64 + 22 = 86 tests** (15 S1 + 23 S2 + 26 S3 + 22 S4).

---

## 9. CI changes (`.github/workflows/ci.yml`)

Add **after** the existing `Data-contract drift check` step (post-outside-voice 5):

```yaml
- name: MIMIC fixture sidecar drift check
  run: uv run python tests/fixtures/mimic/build_mimic_fixture.py --check
```

Runs on both Python 3.11 and 3.12 (matrix-inherited). Failure remediation, documented in the script's `--help`: `uv run python tests/fixtures/mimic/build_mimic_fixture.py` (without `--check`) to regenerate, then commit the regenerated `tests/fixtures/mimic/mimic_fixture_expected.json`.

The `Data-contract drift check` step from S3 still runs and still passes (`canonical.py` is unchanged in S4). The `real_data` marker is already registered in S3, so `tests/test_mimic_real.py` is skipped by default in CI. The Playwright E2E job from S2 is unchanged.

---

## 10. Acceptance criteria (how you know S4 is done)

Every item is a check a reviewer can run.

- [ ] `uv sync` clean.
- [ ] `uv run pytest` green; **22 new tests**; total **86 tests** across S1+S2+S3+S4 (excluding the `e2e` and `real_data` markers which run via `uv run pytest -m e2e` and `uv run pytest -m real_data` respectively).
- [ ] All 26 S3 tests stay green throughout commit 1's `_shared.py` extraction of all 12 helpers (`uv run pytest tests/test_geneva.py` green at every commit).
- [ ] `uv run python tests/fixtures/mimic/build_mimic_fixture.py --check` exits 0 on `main` (sidecar drift CI gate).
- [ ] Test #4 in `test_shared.py` passes both sub-assertions: function identity (`f1 is f2`) for every helper exported by `_shared.__all__`, AND behavioral parity on synthetic cross-vocabulary inputs.
- [ ] `uv run python -c "from ehr_simulator.ingestion import load_mimic; from pathlib import Path; ds = load_mimic(Path('tests/fixtures/mimic/mimic_sample.csv'), Path('tests/fixtures/mimic')); print(len(ds.scalar_ts), len(ds.admission))"` exits 0 with non-zero counts on both frames.
- [ ] `uv run pytest -m real_data` exits 0 (MIMIC + Geneva real-CSV smokes both pass).
- [ ] `EHR_SIM_DATA_ROOT=/mnt/data1/klug/datasets/opsum uv run python -c "from ehr_simulator.ingestion import load_mimic; from pathlib import Path; load_mimic(Path('/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/mimic_prepro_16022026_095909/preprocessed_features_16022026_095909.csv'), Path('/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/mimic_prepro_16022026_095909/logs_16022026_095909'))"` exits 0.
- [ ] `EHR_SIM_DATA_ROOT=/tmp uv run python -c "..."` (same call as above) exits non-zero with `AdapterError` mentioning `path traversal`.
- [ ] `EHR_SIM_DATA_ROOT="" uv run python -c "..."` (empty-string sentinel) exits 0.
- [ ] `uv run python scripts/gen_data_contract.py --check` clean (`canonical.py` unchanged in S4).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI passes on a PR opened against `main`, including the `Data-contract drift check` step.
- [ ] `tests/fixtures/mimic/mimic_sample.csv`, `reference_population_normalisation_parameters.csv`, `categorical_variable_encoding.csv`, `mimic_fixture_expected.json` are all committed.
- [ ] `tests/fixtures/geneva/` contains the four moved fixture files and `build_geneva_fixture.py`; the conftest fixture path is updated.
- [ ] The function-identity sub-assertion in test #4 (`tests/test_shared.py`) passes: `geneva._drop_imputed is mimic._drop_imputed` (and same for every helper in `_shared.__all__` — 12 helpers post-A1).

---

## 11. Conventions

- `from __future__ import annotations` at the top of every new module.
- Module docstrings on every new module (`_shared.py`, `mimic.py`, `build_mimic_fixture.py`, `test_mimic.py`, `test_shared.py`).
- Type hints on every public function.
- `ast.literal_eval` for parsing the Python-list strings in `categorical_variable_encoding.csv`. **Never `eval`.**
- No comments that restate code (per repo `CLAUDE.md`). Helper docstrings document WHY (especially the `dataset` kwarg parameterization rationale).
- Test names: `test_<subject>_<expected_behavior>`. Mirror S3 unit tests but tag with `_mimic` suffix where the test is dataset-specific (e.g. `test_path_traversal_guard_rejects_outside_root_mimic`).
- Inline `pd.DataFrame({...})` in unit tests; the fixture is reserved for integration / E2E.
- The `dataset` kwarg is keyword-only on every lifted helper that accepts it, so call sites are explicit at the import-site level.
- The no-units contract for MIMIC is documented in `mimic.py`'s module docstring AND `MimicDataset`'s docstring AND test #15 — three places, one truth.

---

## 12. Commit discipline (target ~5 commits, ~1.5 days)

Adheres to the S3 ratio (1 spec ≈ 1-2 days, ~5 commits). If any commit balloons past ~400 lines, split.

| # | Commit | Files |
|---|---|---|
| 1 | `session-04: extract _shared.py (12 helpers) + Geneva refactor + parity + FNF wrapping` | `_shared.py` (all 12 lifted helpers — 7 leaf + 5 orchestrator — with `dataset`/`units`/`required_columns` kwargs; FNF wrapping in `_load_normalisation_params` + `_load_categorical_encoding`; defensive issue emission in `_read_features_csv` for unrecognized sources; `CategoricalGroup` dataclass); `geneva.py` (shrinks to thin wrapper that imports all 12 helpers and supplies Geneva-specific args; `_load_units` stays); `tests/test_shared.py` (tests #1-#7: 4 lift-equivalence + #4 layered parity + #5/#6 FNF + #7 unrecognized-source); `tests/fixtures/geneva/` reshuffle (4 files moved + `build_geneva_fixture.py` moved); `tests/conftest.py` (`geneva_fixture_dir` path update). **All 26 S3 tests stay green throughout this commit.** |
| 2 | `session-04: mimic.py adapter + MIMIC fixture + sidecar --check mode` | `mimic.py` (thin wrapper); `tests/fixtures/mimic/build_mimic_fixture.py` (with `--check` flag); `tests/fixtures/mimic/mimic_sample.csv`; `tests/fixtures/mimic/reference_population_normalisation_parameters.csv`; `tests/fixtures/mimic/categorical_variable_encoding.csv`; `tests/fixtures/mimic/mimic_fixture_expected.json`; `tests/conftest.py` (+`mimic_fixture_dir`); `tests/test_mimic.py` (integration tests #8-#12 including #9's anchor sub-assertions, E2E #13, regression #14). |
| 3 | `session-04: MIMIC unit tests + no-units regression lock` | `tests/test_mimic.py` (unit tests #1-#7 + regression #15). |
| 4 | `session-04: EHR_SIM_DATA_ROOT smoke + real-data marker + sidecar drift CI gate` | `tests/test_mimic_real.py` (test #16, `@pytest.mark.real_data`, 90s budget); `.github/workflows/ci.yml` (add `MIMIC fixture sidecar drift check` step running `build_mimic_fixture.py --check`). |
| 5 | `session-04: ingestion __init__ re-exports + final polish` | `src/ehr_simulator/ingestion/__init__.py` (+`MimicDataset`, +`load_mimic`); final ruff/format pass. |

Splitting the `_shared.py` extraction + Geneva refactor into commit 1 keeps the parity test landing before MIMIC depends on the lifted helpers — bisect-friendly: any future regression that breaks Geneva-only or MIMIC-only behavior is isolatable to its own commit. Commit 1 size risk: the orchestrator lift (post-A1) makes commit 1 larger than originally scoped (~12 helpers + Geneva refactor + 7 shared tests + fixture move). If commit 1 trends past ~600 lines, split into 1a (`_shared.py` extraction + Geneva refactor + leaf-helper parity tests #1-#4) and 1b (FNF wrapping + unrecognized-source defense + tests #5-#7).

---

## 13. Open decisions deferred to later sessions

- **MIMIC AI predictions** — no upstream `test_predictions.pkl` exists for MIMIC. Deferred to whichever session first needs cross-dataset AI panel rendering. Until then, MIMIC's `AI_OUTPUT` stays empty by design.
- **MIMIC real-data UI integration** — S8 still uses Geneva. Surfacing MIMIC in the UI requires a dataset selector (likely lands with the CLI in S5 or with the SQLite layer in S6). Punted.
- **Performance on the 1.83M-row real CSV** — S4 trusts pandas + chunked-read defaults. Test #16's 90s budget is the only measurement (post-T3). If S8 surfaces MIMIC and load time matters, revisit.
- **MIMIC display-formatting policy** — decimal places, locale, French→English translation: same punt as S3, lands in S8.
- **Refactoring `_DATASET_NAME` constants out of the per-adapter modules** — `mimic.py` and `geneva.py` each define `_DATASET_NAME` for use in error messages outside the lifted helpers (e.g. the required-column check). This is intentionally a per-module constant, not a `_shared.py` enum. Revisit only if a third adapter adds enough duplication to justify it.
- **Memoizing `_load_normalisation_params` / `_load_categorical_encoding`** — premature; called once per `load_mimic` invocation and the files are <2 KB each. Revisit only if profiling shows otherwise.

---

## 14. What Session 4 does NOT lock

- AI predictions for MIMIC (no upstream pkl).
- MIMIC real-data UI integration (S8 stays on Geneva).
- Display-formatting policy for MIMIC (S8).
- Performance budgets on the real 1.83M-row CSV beyond the 90s smoke wall-time check.
- Units lookup for MIMIC (no source exists; `unit=None` is the locked behavior).
- Tighter `EHR_SIM_DATA_ROOT` contract — S5 (`validate-adapter` CLI tightens both adapters to required env-var simultaneously).
- Backup cadence (D10) — S6.

---

## 15. What already exists (carried into S4)

These shipped in S1/S2/S3 and are reused by S4 unchanged. Listed so review-driven changes do not accidentally duplicate.

- **`src/ehr_simulator/ingestion/canonical.py`** — 4 pandera schemas + `validate(frame, shape, *, strict, dataset)` + `empty_frame(shape)` (S3 post-review-2.1). All reused. No edits.
- **`src/ehr_simulator/ingestion/exceptions.py`** — `AdapterError` + `IngestionIssue`. Reused. The lifted helpers in `_shared.py` raise both with `dataset` flowing in via the kwarg.
- **`src/ehr_simulator/ingestion/synthetic.py`** — pattern reference for how an adapter calls `validate(..., strict=True)` per shape. Untouched.
- **`src/ehr_simulator/ingestion/__init__.py`** — public surface; S4 extends it with `MimicDataset` + `load_mimic`.
- **`src/ehr_simulator/ingestion/data/geneva_units.json`** — Geneva-specific; not consumed by MIMIC.
- **`scripts/gen_data_contract.py`** + **`docs/data-contract.md`** — both unchanged. The drift CI step still runs and still passes.
- **`pyproject.toml`** — no edits. `openpyxl` (dev) from S3 stays unused by S4.
- **`.github/workflows/ci.yml`** — extended with one new step in §9 (`MIMIC fixture sidecar drift check` running `build_mimic_fixture.py --check`). The S3 `Data-contract drift check` step is unchanged. `real_data` marker registration carries over.
- **`tests/conftest.py`** — extended (`mimic_fixture_dir` added; `geneva_fixture_dir` path updated to point at `tests/fixtures/geneva/`).
- **`tests/test_geneva.py`** — unchanged. All 26 tests stay green throughout commit 1.
- **`specs/session-03-geneva-adapter.md`** — structural template; review-driven decisions log §17 carries forward to S4 via the lifted helpers (1.2, 1.5, 2.1, 2.2, 3.1, 3.2 inherit; 1.4 re-applies via test #3 in `test_mimic.py`).

---

## 16. What this spec depends on from the data-shape evidence

The lift verdict in §3 — and specifically the call to lift `_decode_categorical`, `_load_normalisation_params`, and `_load_categorical_encoding` despite their S3-era surface coupling to Geneva — rests on three facts about the real MIMIC files verified on 2026-05-06:

1. MIMIC's `categorical_variable_encoding.csv` has the same 3-column shape (`sample_label, baseline_value, other_categories`) and the same Python-list-as-string format as Geneva's. Validated by direct file inspection.
2. MIMIC's `notes` rows are one-hot expanded with the same naming convention as Geneva's `stroke_registry` rows (`sex_male`, `medhist_*_yes`, `categorical_*_*`). Validated by sampling one patient's `notes` rows.
3. MIMIC's `reference_population_normalisation_parameters.csv` has the same 3-column shape (`variable, original_mean, original_std`) as Geneva's `normalisation_parameters.csv`. Validated by direct file inspection.

If any of these three facts changes upstream, the parity regression in `tests/test_shared.py` test #4 will catch it; the implementer should NOT silently re-fork.

---

## 17. Review-driven decisions log

`/plan-eng-review` ran against this spec on 2026-05-07. Six review-section issues + one cross-model tension surfaced; all resolved as concrete spec patches (listed below). Outside-voice fell back to a Claude subagent (Codex auth failed) and surfaced 9 additional findings; 7 were folded as mechanical spec edits (covered below); 2 were substantive cross-model tensions resolved via AskUserQuestion.

### Resolved decisions (8)

| ID | Section | Decision |
|---|---|---|
| 1.A1 | §3 helper inventory | Lift ALL 5 orchestrator helpers (`_read_features_csv` renamed from `_read_geneva_csv`, `_build_scalar_ts`, `_apply_scalar_ts_inverse_normalize`, `_build_admission`, `_validate_and_collect`) to `_shared.py` with `dataset` + `units` (Optional, default None) + `required_columns` kwarg parameterization. Total lift: 12 helpers + `CategoricalGroup` (vs originally proposed 7). Both adapters become thin wrappers. |
| 2.D1 | §5 routing language | Rewrite step-by-step routing to match Geneva's actual two-step pattern: `_build_scalar_ts` constructs frame with raw z-scored values, pandera validates, THEN `_apply_scalar_ts_inverse_normalize` runs. Documented as load-bearing because pandera's `coerce=True` must see raw strings to drop malformed rows in lenient mode. |
| 3.T1 | §3 + §8 | `_load_normalisation_params` and `_load_categorical_encoding` wrap `FileNotFoundError` as `AdapterError` with `IngestionIssue` carrying `dataset` + missing-path. Two new tests in `test_shared.py` (#5, #6) lock the wrapping. |
| 3.T2 | §8 test #4 | Parity regression #4 becomes layered: function identity (`f1 is f2` for every helper in `_shared.__all__`) + behavioral parity sub-test on synthetic cross-vocabulary inputs (rows from BOTH Geneva and MIMIC source vocabs flow through same helpers, assert same output). |
| 3.C1 | §7 step 9 + §8 test #9 | Test #9 grows 3-5 hand-curated anchor assertions read from raw upstream MIMIC CSV (independent of `load_mimic()`). Anchors catch high-value semantic regressions even if sidecar gets re-baselined. |
| 4.T3 | §8 test #16 | Wall-time budget bumped from 60s → 90s. MIMIC is 10× smaller than Geneva but chunked-read fixed cost + pandera validation dominate; 30s buffer over expected ~30-45s avoids CI flake on cold `/mnt` reads. |
| OV.1 | §7 step 10 + §9 + §12 | `build_mimic_fixture.py --check` mode added (mirror of `gen_data_contract.py --check`). CI step added after pytest. Closes the bootstrap-loop gap that anchor assertions alone don't fully fix. |
| OV.2 | scope | Hold the line on S4 scope. Outside voice argued ~580 spec lines + 22 tests is over-engineered for ~80 LOC of MIMIC delta. Roadmap-pinned scope + boil-the-lake project pattern + parity-test value for S7 Geneva AI inheritance justify the depth. |

### Outside-voice findings folded as mechanical spec edits (7)

| Finding | Resolution |
|---|---|
| Stale "7 helpers" inventory in §3 | §3 now enumerates 12 helpers (7 leaf + 5 orchestrator). |
| `_read_geneva_csv` rename never specified | Renamed to `_read_features_csv` in `_shared.py`; `required_columns` kwarg parameterization. |
| `_build_admission` semantic lock unstated | §3 helper table now documents the `t=0` slice + `str(round(raw, 2))` convention as transferring to MIMIC unchanged (verified by Explore A: `notes` rows repeat across all 72 hour buckets). |
| `_build_scalar_ts` units positional dict | Signature updated to `units: dict[str, str] \| None = None`; MIMIC passes `units=None`, Geneva passes `units=_load_units()`. |
| `_apply_scalar_ts_inverse_normalize` post-validate semantics | §3 helper table now documents the dependence on canonical SCALAR_TS schema having no range check on `value` (per `canonical.py:88`); future schema additions adding e.g. `value >= 0` would break both adapters. |
| §10 acceptance contradicts §3 helper inventory | Acceptance now references "every helper in `_shared.__all__` (12 helpers post-A1)". |
| 60s wall-time bumped to 90s but spec body still said 60s | All references to wall-time updated to 90s in §8 test #16 + §10 acceptance criteria. |

### Outside-voice finding #9 folded as new test (1)

`test_shared.py` test #7 added: `_read_features_csv` defensively emits `IngestionIssue(reason="unrecognized source value: ...")` for any source value not in the dataset's known vocabulary. Locks the silent-drop failure mode for both adapters.

### Test count delta

- Spec originally proposed 19 tests.
- Resolutions add 3 new tests (`test_shared.py` #5, #6, #7) + rewrite #4 (function identity → layered) + rewrite #9 (sidecar → sidecar + anchors).
- **Post-eng-review S4 total: 22 tests** (7 in `test_shared.py` + 14 in `test_mimic.py` + 1 in `test_mimic_real.py`).
- **Post-S4 project total: 86 tests** (15 S1 + 23 S2 + 26 S3 + 22 S4).

### TODOs added (1)

- **Add a Geneva-side integration test asserting the lifted `_read_features_csv` defensive issue-emission catches unrecognized source values in real Geneva fixture** — currently only test_shared.py #7 covers the synthetic case. If S5 wires up the structlog WARNING (per existing TODO from S3), the same WARNING would fire here. Revival criterion: when S5 lands. Depends on: S5.

### GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (non-UI session, optional) |
| Codex Review | `/codex review` | Independent 2nd opinion | 1 | AUTH_FAILED | Codex 401; fell back to Claude subagent |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 6 issues, 0 critical gaps, 0 unresolved |
| Outside Voice | Claude subagent fallback | Independent challenge | 1 | CLEAR | 9 findings: 7 mechanical + 2 cross-model tensions, all resolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not applicable (no UI in S4) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **CROSS-MODEL:** Outside voice and eng review agreed on most findings; 2 cross-model tensions (sidecar bootstrap loop fully fixed vs half-fixed; S4 scope hold-the-line vs trim) resolved via AskUserQuestion in favor of completeness (sidecar `--check` + hold-the-line).
- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement
