# Session 03 — Geneva adapter

**Goal:** `load_geneva(csv_path, params_dir)` returns a `GenevaDataset` whose four frames pass `validate(..., strict=True)` against the canonical schemas locked in S1, exercising every adapter responsibility documented at the bottom of `session-01-data-contract.md`. The adapter is the contract-validation gauntlet that every later real-data adapter (S4 MIMIC, S7 Geneva AI predictions) copies. Geneva is the home dataset for the Geneva stroke unit deployment; building the adapter early validates the contract against the production target before downstream sessions land.

**Out of scope (later sessions):** MIMIC adapter (S4), CLI / `validate-adapter` / `preflight` / `study_config.yaml` (S5), SQLite (S6), Geneva AI predictions (S7), real-data UI on Geneva (S8), answer capture / question gating / CSV export (S9a-c), divergence view (S10), arm randomization (S11), DICOM rendering, FHIR layer.

---

## Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | (no new top-level deps; `ast` is stdlib). Confirm `pandas>=2.2` + `pandera[pandas]>=0.20` already pinned. |
| 2  | `.gitignore` | Add `docs/data-contract.md.tmp` (drift-check scratchpad — but the generator should not write a tmp file; reserved if the implementer takes that route). |
| 3  | `src/ehr_simulator/ingestion/geneva.py` | `load_geneva` adapter + 7 module-scope helpers; reads units from the committed JSON in deliverable #6 |
| 4  | `src/ehr_simulator/ingestion/__init__.py` | Re-export `GenevaDataset`, `load_geneva` |
| 5  | `scripts/build_geneva_units.py` | One-shot converter: reads `possible_ranges_for_variables.xlsx` (path argv) and writes deliverable #6. Run once during S3 implementation; rerun only on upstream xlsx refresh |
| 6  | `src/ehr_simulator/ingestion/data/geneva_units.json` | `dict[str, str]` keyed by Geneva `sample_label` → unit string. Committed; the source of truth at runtime |
| 7  | `scripts/gen_data_contract.py` | Reads `canonical.py` docstring + pandera schemas, writes `docs/data-contract.md`; `--check` mode for CI drift gate |
| 8  | `docs/data-contract.md` | Generated; committed; CI re-runs `--check` after pytest |
| 9  | `tests/fixtures/build_geneva_fixture.py` | Deterministic builder reading the real Geneva CSV; outputs the three fixture files below |
| 10 | `tests/fixtures/geneva_sample.csv` | 2-patient slice of the real CSV; **not anonymized** (z-scored values carry no PHI) |
| 11 | `tests/fixtures/normalisation_parameters.csv` | Verbatim copy of upstream `logs_30012026_154047/normalisation_parameters.csv` (68 rows; no PHI) |
| 12 | `tests/fixtures/categorical_variable_encoding.csv` | Verbatim copy of upstream `logs_30012026_154047/categorical_variable_encoding.csv` (19 rows; no PHI) |
| 13 | `tests/test_geneva.py` | Unit + integration + E2E + regression tests for the adapter |
| 14 | `tests/test_data_contract.py` | E2E test: `gen_data_contract.py --check` against committed `docs/data-contract.md` returns 0 |
| 15 | `tests/conftest.py` | Add `geneva_fixture_dir` fixture pointing at `tests/fixtures/` |
| 16 | `.github/workflows/ci.yml` | Add `Data-contract drift check` step after the existing `Test` step |

---

## Repo layout after Session 3 (diff vs end-of-S2)

```
ehr_simulator/
├── docs/                                         # NEW
│   └── data-contract.md                          # NEW (generated, committed)
├── scripts/
│   ├── round03_vitals_mockup.py                  # unchanged
│   ├── build_geneva_units.py                     # NEW (one-shot xlsx → JSON converter)
│   └── gen_data_contract.py                      # NEW
├── src/ehr_simulator/ingestion/
│   ├── __init__.py                               # extended (+GenevaDataset, +load_geneva)
│   ├── canonical.py                              # unchanged
│   ├── data/                                     # NEW (package data)
│   │   └── geneva_units.json                     # NEW (committed; sourced from upstream xlsx)
│   ├── exceptions.py                             # unchanged
│   ├── geneva.py                                 # NEW
│   └── synthetic.py                              # unchanged
└── tests/
    ├── conftest.py                               # extended (+geneva_fixture_dir)
    ├── fixtures/                                 # NEW
    │   ├── build_geneva_fixture.py               # NEW
    │   ├── geneva_sample.csv                     # NEW (committed)
    │   ├── normalisation_parameters.csv          # NEW (committed; verbatim from upstream)
    │   └── categorical_variable_encoding.csv     # NEW (committed; verbatim from upstream)
    ├── test_data_contract.py                     # NEW
    └── test_geneva.py                            # NEW
```

---

## 1. Data inputs (verbatim from `.EXAMPLE_DATA_PATHS`)

The adapter accepts paths to:

- **Geneva preprocessed features CSV:** `/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/gsu_Extraction_20220815_prepro_30012026_154047/preprocessed_features_30012026_154047.csv`
- **Normalization + categorical encoding directory:** `/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/gsu_Extraction_20220815_prepro_30012026_154047/logs_30012026_154047/`

The adapter also reads units from a committed JSON file (deliverable #6) that is bootstrapped — once, at S3 implementation time — from the upstream OPSUM xlsx:

- **Units source (upstream):** `https://github.com/JulianKlug/OPSUM/blob/main/preprocessing/geneva_stroke_unit_preprocessing/possible_ranges_for_variables.xlsx`. The implementer downloads this file once, runs `scripts/build_geneva_units.py <xlsx_path>`, and commits the generated `src/ehr_simulator/ingestion/data/geneva_units.json`. The xlsx itself is **not vendored**: avoiding a binary in git keeps the repo diff-friendly, and the JSON form is the source of truth at runtime. Refresh process is documented in §6.

Real-CSV facts the spec is built on (verified via direct inspection 2026-05-06):

- **CSV columns:** unnamed-index, `relative_sample_date_hourly_cat`, `case_admission_id`, `sample_label`, `source`, `value`. 19,704,311 data rows. 2,657 unique `case_admission_id`s. 103 unique `sample_label`s. Hour buckets 0–71.
- **All 8 observed `source` values:** `EHR`, `EHR_locf_imputed`, `EHR_pop_imputed`, `EHR_pop_imputed_locf_imputed`, `stroke_registry`, `stroke_registry_locf_imputed`, `stroke_registry_pop_imputed`, `stroke_registry_pop_imputed_locf_imputed`. Substring match on `"imputed"` drops 6 of 8.
- **`stroke_registry` rows are already one-hot expanded in `sample_label`** (`sex_male`, `medhist_hypertension_yes`, `categorical_iat_no_iat`, …). The adapter consults `categorical_variable_encoding.csv` to re-fold one-hot groups back to single labels.
- **`stroke_registry` rows repeat across all 72 hour buckets per patient.** Admission is static — adapter takes the `t=0` slice only.
- **`normalisation_parameters.csv` columns:** `variable, original_mean, original_std`. 68 rows.
- **`categorical_variable_encoding.csv` columns:** `sample_label, baseline_value, other_categories` (Python-list-as-string). 19 rows.

---

## 2. `pyproject.toml` deltas

No new top-level (runtime) dependencies — the adapter reads `geneva_units.json` via stdlib `json`. `ast` (for `ast.literal_eval` on the categorical-encoding strings) and `os` (for `EHR_SIM_DATA_ROOT`) are stdlib. No `python-dotenv`.

Add to `[dependency-groups] dev`:

```toml
"openpyxl>=3.1",
```

`openpyxl` is used **only** by `scripts/build_geneva_units.py` (one-shot tooling), so it lives in `dev`, not in the runtime dependency set. End users installing the wheel never pull it.

Add `tool.hatch.build.targets.wheel.force-include` (or equivalent package-data declaration) so `src/ehr_simulator/ingestion/data/geneva_units.json` ships inside the wheel:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/ehr_simulator"]

[tool.hatch.build.targets.wheel.force-include]
"src/ehr_simulator/ingestion/data/geneva_units.json" = "ehr_simulator/ingestion/data/geneva_units.json"
```

(Hatch picks JSON files inside `packages` automatically in most layouts; the `force-include` is defense against future refactors.)

---

## 3. `src/ehr_simulator/ingestion/geneva.py` — module skeleton

```python
"""Geneva preprocessed-features adapter.

Loads the Geneva stroke-unit CSV at the path configured in
``.EXAMPLE_DATA_PATHS`` into the four canonical in-memory shapes from
:mod:`ehr_simulator.ingestion.canonical`.

Routing rules (encoded in ``_route_row``):

==============================  ==========================================
``source`` value                Action
==============================  ==========================================
contains ``"imputed"``          drop before validation (substring match)
``"EHR"`` (exact)               route to ``SCALAR_TS``; inverse-normalize via
                                ``normalisation_parameters.csv`` when
                                ``variable`` is in the params; unit from
                                ``geneva_units.json`` (sourced from the upstream
                                OPSUM xlsx; see §3 for provenance);
                                ``t_minutes = relative_sample_date_hourly_cat
                                * 60.0``
``"stroke_registry"`` (exact)   route to ``ADMISSION``; take the ``t=0``
                                slice only; for one-hot categorical groups
                                listed in ``categorical_variable_encoding.csv``,
                                apply >=0.5 thresholding + group re-expansion
                                via :func:`_decode_categorical`; for continuous
                                registry vars (``age``, ``weight``,
                                ``prestroke_disability_(rankin)_*``) inverse-
                                normalize and str-coerce
==============================  ==========================================

``IMAGING`` and ``AI_OUTPUT`` are returned as empty-but-conforming
DataFrames. Geneva imaging-derived scalars (``cbf_lt_30``, ``tmax_gt_6``,
…) live in ``EHR`` rows and route through ``SCALAR_TS``; AI predictions
land in S7. Empty frames keep downstream code special-case-free.

The ``EHR_SIM_DATA_ROOT`` environment variable, when set, scopes both
``csv_path`` and ``params_dir`` via :func:`_path_traversal_guard`. When
unset, the guard is advisory and the adapter trusts caller-provided paths.
S5's ``validate-adapter`` CLI tightens this to required (deferred).
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ehr_simulator.ingestion.canonical import CanonicalShape, validate
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue

_DATASET_NAME = "geneva"
_REQUIRED_COLUMNS = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)
_NON_IMPUTED_SOURCES = ("EHR", "stroke_registry")
```

### Public API

```python
@dataclass
class GenevaDataset:
    scalar_ts: pd.DataFrame
    admission: pd.DataFrame
    imaging: pd.DataFrame
    ai_output: pd.DataFrame
    issues: list[IngestionIssue] = field(default_factory=list)


def load_geneva(
    csv_path: Path,
    params_dir: Path,
    *,
    strict: bool = True,
) -> GenevaDataset:
    """Load the Geneva preprocessed-features CSV into the four canonical shapes.

    ``params_dir`` must contain ``normalisation_parameters.csv`` and
    ``categorical_variable_encoding.csv``. Both ``csv_path`` and
    ``params_dir`` are validated via :func:`_path_traversal_guard` against
    ``EHR_SIM_DATA_ROOT`` if set.

    ``strict=True`` (default): every frame is validated with pandera's
    eager mode; first violation raises :class:`AdapterError`.

    ``strict=False``: lenient validation collects offending rows into
    ``GenevaDataset.issues`` and returns the surviving rows. Imaging /
    AI_OUTPUT frames are empty either way.
    """
```

### Helpers (declared module-scope; **NOT** lifted to `_shared.py` yet — S4 decides)

| Function | Signature | Contract |
|---|---|---|
| `_drop_imputed` | `(frame: pd.DataFrame) -> pd.DataFrame` | Returns rows whose `source` does not contain the literal substring `"imputed"`. Returns a copy with `reset_index(drop=True)`. Empty input → empty output. |
| `_inverse_normalize` | `(z: float \| pd.Series, mean: float, std: float) -> float \| pd.Series` | Returns `z * std + mean`. Vectorized. NaN propagates. |
| `_decode_categorical` | `(rows_for_group: pd.DataFrame, group: CategoricalGroup, *, strict: bool, patient_id: str) -> tuple[str, IngestionIssue \| None]` | Given all sample_labels for one categorical group at one (patient_id, t=0), apply ≥0.5 threshold + group re-expansion. Returns `(decoded_label, optional_issue)`. **Tiered behavior** (post-review-1.2): `>= 0.5` matches (numpy convention); **all <0.5** → return `(group.baseline, None)`; **exactly one ≥0.5** → return `(matching_label, None)`; **multiple ≥0.5** → strict raises `AdapterError(issues=[IngestionIssue(dataset="geneva", patient_id=patient_id, row_idx=None, reason="ambiguous categorical decode for {group.group_name}: {n} candidates >=0.5")])`; lenient picks `argmax` and returns `(argmax_label, IngestionIssue(reason="ambiguous categorical decode for {group.group_name}: picked {label} from {n} candidates"))`; **empty rows_for_group** → strict raises `AdapterError(issues=[IngestionIssue(...)])`, lenient returns `(group.baseline, IngestionIssue(reason="empty rows for categorical group {group.group_name}"))`. |
| `_path_traversal_guard` | `(path: Path, root: Path \| None) -> Path` | When `root` is set: `path.resolve().is_relative_to(root.resolve())` else raise `AdapterError(issues=[IngestionIssue(dataset="geneva", patient_id=None, row_idx=None, reason="path traversal: {path} not under EHR_SIM_DATA_ROOT={root}")])`. When `root` is `None`: passthrough (returns `path.resolve()`). Always returns a resolved `Path`. |
| `_load_normalisation_params` | `(path: Path) -> dict[str, tuple[float, float]]` | `pd.read_csv` then `{row.variable: (row.original_mean, row.original_std)}`. Raises `AdapterError` with column-name diagnostic on missing-column. |
| `_load_categorical_encoding` | `(path: Path, sample_labels: set[str]) -> dict[str, CategoricalGroup]` | Reads CSV, parses `baseline_value` and `other_categories` cells via `ast.literal_eval` (NOT `eval`). For each computed `one_hot_columns` entry, asserts membership in the input frame's `sample_labels` set. **Self-check** (post-review-1.4): if any computed one-hot column is missing from `sample_labels`, raises `AdapterError(issues=[IngestionIssue(reason="categorical naming-convention drift: {missing_columns} not in CSV sample_label")])`. Returns a dict keyed by `sample_label` (the canonical group name like `"Sex"`, `"categorical_IAT"`). |
| `_load_units` | `() -> dict[str, str]` | Loads `src/ehr_simulator/ingestion/data/geneva_units.json` from the package via `importlib.resources.files("ehr_simulator.ingestion") / "data" / "geneva_units.json"` (works inside an installed wheel). Returns the mapping `sample_label → unit_string`. Decorated with `@functools.lru_cache(maxsize=1)` (post-review-2.2) so the JSON is read once per process; tests that need fresh state call `_load_units.cache_clear()`. Raises `AdapterError` if the file is missing or not valid JSON — that condition means the wheel was built wrong, not a data error. |

### Supporting types

```python
@dataclass(frozen=True)
class CategoricalGroup:
    group_name: str           # e.g. "Sex", "categorical_IAT"
    baseline: str             # e.g. "Female", "271-540min"
    other_labels: tuple[str, ...]  # e.g. ("Male",), ("no_IAT", ">540min", "<270min")
    one_hot_columns: tuple[str, ...]  # the sample_label values in the CSV that
                                       # correspond to other_labels, in the same order
                                       # (e.g. ("sex_male",), ("categorical_iat_no_iat", ...))
```

`one_hot_columns` is computed by `_load_categorical_encoding` via a deterministic naming convention: lowercased `group_name` + `_` + lowercased label, with spaces and dots replaced by underscores. The convention is documented in the function's docstring. **Mismatches surface at load time** (post-review-1.4): `_load_categorical_encoding` takes the input frame's `sample_label` set as a parameter and asserts every computed `one_hot_columns` entry is present, raising `AdapterError` listing every missing column. Test #4c (added post-review) covers all 19 categorical groups against the fixture, locking the convention end-to-end.

### Units source: upstream xlsx → committed JSON

The Geneva CSV has no `unit` column. The UI panels (S2) render units from the `unit` field on each SCALAR_TS row. Rather than hand-curate a units dict in code, S3 sources units from the upstream OPSUM repo:

- **Upstream:** `https://github.com/JulianKlug/OPSUM/blob/main/preprocessing/geneva_stroke_unit_preprocessing/possible_ranges_for_variables.xlsx` (added to `.EXAMPLE_DATA_PATHS` 2026-05-06).
- **At runtime:** `geneva.py` reads `src/ehr_simulator/ingestion/data/geneva_units.json` via `importlib.resources`, so the JSON ships in the wheel and works on a fresh clone with no network. The JSON is `dict[str, str]` keyed by Geneva `sample_label` → unit string (e.g. `{"max_heart_rate": "bpm", "ALAT": "U/L", ...}`). Variables not in the JSON carry `unit=None` on their SCALAR_TS row.
- **Bootstrap:** `scripts/build_geneva_units.py <xlsx_path>` reads the upstream xlsx via `pandas.read_excel(..., engine="openpyxl")`, normalizes the variable-name column to match Geneva CSV `sample_label` values exactly (lowercased, spaces → underscores, accents stripped), extracts the units column, and writes `src/ehr_simulator/ingestion/data/geneva_units.json` (sorted keys, 2-space indent, trailing newline). Idempotent given the same xlsx input.
- **Coverage gaps**: variables present in the Geneva CSV but missing from the xlsx are emitted as `IngestionIssue("geneva", patient_id=None, row_idx=None, reason="unit unknown for variable={name}")` and surfaced on `GenevaDataset.issues` (collected even in strict mode — strict gates schema violations, not unit-coverage gaps). The bootstrap script also prints these gaps to stderr at conversion time so they surface during S3 implementation, not in production.

Refresh process (when upstream xlsx changes): re-download the xlsx, re-run `scripts/build_geneva_units.py`, commit the regenerated JSON. The JSON is the source of truth at runtime; the xlsx URL is provenance-only.

**The xlsx schema is not yet known to this spec** — the implementer inspects the file once during S3 implementation, locks the column names in `build_geneva_units.py`'s docstring, and adds **test #4b** (see §9) which loads the committed JSON and asserts ≥30 of the 103 Geneva sample_labels are covered (concrete number adjusted post-bootstrap once the implementer has measured actual coverage).

---

## 4. Routing logic (the core of `load_geneva`)

Order of operations:

1. Resolve `EHR_SIM_DATA_ROOT` via `root_str = os.environ.get("EHR_SIM_DATA_ROOT") or None` — empty string is treated as unset (post-review-1.5). Then `_path_traversal_guard(csv_path, root)` and `_path_traversal_guard(params_dir, root)`.
2. Read the CSV in chunks (post-review-1.3): `pd.read_csv(csv_path, usecols=list(_REQUIRED_COLUMNS), dtype={"case_admission_id": str, "sample_label": str, "source": str}, chunksize=500_000)`. **Note:** the `value` column is intentionally NOT in the dtype dict (post-review-1.1) — pandera's `coerce=True` handles numeric coercion at the validate step so `validate(strict=False)` can drop a malformed-value row instead of `read_csv` raising. The integer index column is dropped via `index_col=0`. For each chunk, apply `_drop_imputed` *inside* the chunk loop (cuts ~75% of rows before concat), then `pd.concat` the survivors. Required-column check happens on the first chunk.
3. Required-column check: `set(_REQUIRED_COLUMNS).issubset(frame.columns)` else `AdapterError("Geneva CSV missing required columns: ...")`.
4. `_drop_imputed` already applied per-chunk (step 2).
5. Compute `t_minutes = relative_sample_date_hourly_cat * 60.0`.
6. Rename `case_admission_id` → `patient_id`.
7. Split by `source`:
   - `ehr_rows = frame[frame.source == "EHR"]` → build SCALAR_TS.
   - `registry_rows = frame[frame.source == "stroke_registry"]` → build ADMISSION.
8. **SCALAR_TS build:** for each row, look up `(mean, std)` in `norm_params`; if found, `value = _inverse_normalize(value, mean, std)`. **Passthrough audit** (post-review-3.2): if a `sample_label` is NOT in `norm_params`, leave value as-is AND append `IngestionIssue(dataset="geneva", patient_id=None, row_idx=None, reason="variable {sample_label} missing from normalisation_parameters")` (collected even in strict mode — strict gates schema violations, not normalization-coverage gaps). Set `unit = _load_units().get(variable)` (falls back to `None` for variables absent from `geneva_units.json`). Set `source = "EHR"`. Drop the index column. Cast dtypes.
9. **ADMISSION build:** filter `registry_rows` to `t_minutes == 0.0` (every patient has the same registry value across all 72 buckets; t=0 is the canonical anchor). Then:
   - Group rows by patient.
   - For each known categorical group (from `_load_categorical_encoding`), find the rows whose `sample_label` is in `group.one_hot_columns`; call `_decode_categorical(rows, group, strict=strict, patient_id=pid)` → emit one `(patient_id, group.group_name, decoded_label)` row. Append the optional `IngestionIssue` (lenient ambiguity / lenient empty) to the dataset's issues list.
   - For continuous registry variables (rows whose `sample_label` matches a `normalisation_parameters.csv` entry AND is not part of any categorical group), inverse-normalize and emit `(patient_id, sample_label, str(round(raw, 2)))`.
   - Sample_labels that are neither categorical nor in norm_params (none expected; would be a schema drift) emit `IngestionIssue(reason="orphan registry variable: {sample_label}")` and are skipped.
10. **IMAGING / AI_OUTPUT build:** empty conforming frames via `empty_frame(CanonicalShape.IMAGING)` and `empty_frame(CanonicalShape.AI_OUTPUT)` — schema-derived helper in `canonical.py` (post-review-2.1; see §5).
11. Validate every frame via `validate(frame, shape, strict=strict, dataset=_DATASET_NAME)`. In lenient mode, accumulate `frame.attrs["adapter_error"].issues` onto `GenevaDataset.issues`.
12. Return `GenevaDataset(scalar_ts, admission, imaging, ai_output, issues)`.

---

## 5. Empty-but-conforming IMAGING / AI_OUTPUT

Post-review-2.1: a single schema-derived helper in `canonical.py` replaces the per-shape constructors. Both Geneva (S3) and any future adapter (MIMIC S4, etc.) call the same primitive; if the schema gains a column, the empty constructor follows automatically.

```python
# In src/ehr_simulator/ingestion/canonical.py — added next to validate()
def empty_frame(shape: CanonicalShape) -> pd.DataFrame:
    """Return an empty DataFrame conforming to the given canonical shape.

    Columns and dtypes are derived from the pandera schema, so adding a
    column to the schema flows through to every adapter's empty-frame
    construction without code changes.
    """
    schema = SCHEMAS[shape]
    return pd.DataFrame({
        col: pd.Series([], dtype=_pandera_dtype_to_pandas(spec.dtype))
        for col, spec in schema.columns.items()
    })
```

In `geneva.py`, both empty frames are constructed via:

```python
imaging = empty_frame(CanonicalShape.IMAGING)
ai_output = empty_frame(CanonicalShape.AI_OUTPUT)
```

Both pass `validate(frame, shape, strict=True)` because pandera's `coerce=True` accepts empty frames with the right column names + nullable settings. Test #11 locks this for both shapes via the shared path.

---

## 6. `EHR_SIM_DATA_ROOT` env-var contract

```
EHR_SIM_DATA_ROOT  =  /mnt/data1/klug/datasets/opsum   (example)
```

Read **once at adapter call-time** (not at module import). Behavior:

| State | csv_path / params_dir relationship | Result |
|---|---|---|
| env var unset OR empty string | any | passthrough (advisory only) |
| env var set to a non-empty path | both paths resolve under it | proceed |
| env var set to a non-empty path | either path resolves outside it | `AdapterError(issues=[IngestionIssue(reason="path traversal: {path} not under EHR_SIM_DATA_ROOT={root}")])` |

**Empty-string handling** (post-review-1.5): the caller reads the env var as `os.environ.get("EHR_SIM_DATA_ROOT") or None`, so an explicitly-set-but-empty value is treated identically to unset. Without this, `Path("")` would resolve to the current working directory and silently reject every absolute path outside CWD.

Resolution uses `Path.resolve(strict=False)` so missing files surface the underlying error, not a path-traversal false negative. The check is intentionally permissive when the env var is unset — S5's `validate-adapter` CLI tightens this to required by always setting the env var before invoking adapters. The tightening is deferred so S3 stays fixture-friendly (tests don't have to set the env var).

Test #7 covers all four states (unset, empty-string, inside-root, outside-root).

---

## 7. `scripts/gen_data_contract.py`

Generates `docs/data-contract.md` from `canonical.py`:

- **Source of truth:** the module docstring of `canonical.py` (header + per-shape paragraphs) plus pandera schema introspection for each `*_SCHEMA` (column name, dtype, nullable, checks, uniqueness).
- **Two run modes:**
  - default: write `docs/data-contract.md` (overwrite). Idempotent given `canonical.py`.
  - `--check`: render in-memory, compare to the file on disk byte-for-byte; exit 0 if equal, exit 1 with a diff snippet on stderr if not. CI uses `--check`.
- **Markdown structure** (post-review-2.3 — routing section dropped):
  1. Title + autogeneration warning (`<!-- This file is generated by scripts/gen_data_contract.py — do not edit by hand. -->`).
  2. Overview pulled from `canonical.__doc__`.
  3. One section per `CanonicalShape` (4 shapes), each with:
     - Columns table (column name | dtype | nullable | constraints).
     - Uniqueness clause (if any).
     - Schema-level checks (e.g. `output_json must be valid JSON`).
  4. Closing one-liner: `"Adapter-specific routing rules: see ehr_simulator/ingestion/{adapter}.py module docstring (currently: synthetic.py, geneva.py)."` — keeps the contract doc canonical-only and decouples it from any single adapter's docstring formatting. The original spec coupled the generator to `geneva.__doc__` parsing, which would have required updating the generator every time a routing table heading or whitespace changed.

Implementation contract:

```python
def render_contract() -> str:
    """Build the full markdown content from canonical.py docstring + pandera schema introspection only."""

def main(argv: list[str] | None = None) -> int:
    """argparse with `--check` flag. Returns 0/1 for sys.exit."""
```

The generator is a pure function plus an I/O wrapper, so test #13 can call `render_contract()` directly without subprocess overhead in unit form, and use subprocess only for the `--check` exit-code test.

---

## 8. Fixture strategy

`tests/fixtures/build_geneva_fixture.py` is run **once** during S3 implementation, then again only on upstream schema changes. Behavior:

1. Reads `/mnt/data1/klug/datasets/opsum/.../preprocessed_features_30012026_154047.csv` (path argv-configurable; defaults to the constant path for reproducibility).
2. Picks 2 patient ids deterministically: the patient with the most `EHR` rows + the patient with the most `stroke_registry` rows (sorted by id as a tiebreaker). The two ids may coincide; if so, fall back to the second-most for the second slot.
3. Filters the CSV to those 2 patients only. Keeps all `(t, sample_label, source)` rows for each — no value sampling, no row dropping. Result is small enough to commit (~5K rows × 6 columns ≈ 200 KB).
4. **Replaces the two `case_admission_id`s with `geneva_fixture_001` / `geneva_fixture_002`** so the committed CSV has no PHI link to the source data.
5. **Does NOT alter any `value` cells.** The values are already z-scored against a population; they carry no PHI by construction. The audit trail for "no anonymization needed" lives in this spec section so reviewers do not re-litigate it.
6. Writes `tests/fixtures/geneva_sample.csv` (no index column).
7. Copies `logs_30012026_154047/normalisation_parameters.csv` → `tests/fixtures/normalisation_parameters.csv` byte-for-byte.
8. Copies `logs_30012026_154047/categorical_variable_encoding.csv` → `tests/fixtures/categorical_variable_encoding.csv` byte-for-byte.
9. **Sidecar expected-admission JSON** (post-review-3.3): the builder runs `load_geneva()` against the just-written fixture and serializes the resulting ADMISSION frame to `tests/fixtures/geneva_fixture_expected.json` as `{"geneva_fixture_001": {field: value, ...}, "geneva_fixture_002": {...}}`. Test #9 asserts exact-match against this JSON. Regenerating the fixture also regenerates the sidecar; drift is human-readable in the diff.

The builder is deterministic (no RNG; tie-breaks by sort). Builder script is committed; the four output files (CSV ×3 + sidecar JSON) are committed. Re-running the builder regenerates byte-identical files unless upstream schema changes.

`tests/conftest.py` adds:

```python
@pytest.fixture
def geneva_fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"
```

---

## 9. Test inventory (target ≥11 from ROADMAP; final count = 26 after review resolutions)

Numbered to match `commits` in §13 below. Tests marked **(post-review-N.M)** were added or rewritten by `/plan-eng-review` resolutions on 2026-05-06.

### Unit (15)

1. **`test_drop_imputed_drops_six_of_eight_known_sources`** (`tests/test_geneva.py`) — inline frame with one row per of the 8 observed `source` values; assert `_drop_imputed` returns exactly 2 rows (`EHR`, `stroke_registry`). Asserts the substring contract is "contains `imputed`", not "endswith".

2. **`test_inverse_normalize_round_trip`** (`tests/test_geneva.py`) — `_inverse_normalize(_normalize(x), m, s) ≈ x` for `(x, m, s)` ∈ {(73.6, 73.6, 14.5), (1.09, 1.09, 0.27), (0.0, 22.1, 4.3)}` (pulled from real `normalisation_parameters.csv` rows). NaN input → NaN output.

3. **`test_decode_categorical_threshold_below_returns_baseline`** (`tests/test_geneva.py`) — group `Sex` (baseline=`Female`, other=`Male`, one_hot=`("sex_male",)`), single-row input with `value=0.4` → returns `("Female", None)`.

4. **`test_decode_categorical_threshold_above_returns_match`** (`tests/test_geneva.py`) — same `Sex` group with `value=0.7` → returns `("Male", None)`. Multi-category group `categorical_IAT` (3 one-hot columns), exactly one ≥0.5 → returns the matching label.

4b. **`test_load_units_covers_expected_variables`** (`tests/test_geneva.py`) — call `_load_units()`, assert (a) returns a non-empty `dict[str, str]`; (b) ≥30 of the 103 known Geneva `sample_label` values are covered (concrete number locked once the bootstrap runs and actual coverage is known); (c) every value is a non-empty string; (d) calling twice returns the same object (caching contract; uses `_load_units.cache_clear()` setup-side per post-review-2.2). Locks the JSON-source-of-truth contract so a missing/malformed `geneva_units.json` fails CI loudly.

4c. **`test_load_categorical_encoding_covers_all_19_groups`** (`tests/test_geneva.py`, **post-review-1.4**) — load fixture; pass the fixture's `sample_label` set to `_load_categorical_encoding`; assert (a) returns 19 `CategoricalGroup` entries; (b) every `one_hot_columns` entry across all groups appears in the input `sample_label` set (no orphans); (c) calling with a deliberately-stripped sample_label set raises `AdapterError` listing the missing columns. Locks the deterministic naming-convention bridge for all 19 groups, not just `sex_male`.

5. **`test_decode_categorical_edge_cases`** (`tests/test_geneva.py`) — table-driven, **expanded** sub-asserts (post-review-1.2/1.5):
   - (a) `value=0.5` exactly → matches (per the `>=` contract).
   - (b) two rows ≥0.5 in `strict=True` → raises `AdapterError`; `exc.issues[0].patient_id` equals the input `patient_id`.
   - (c) empty `rows_for_group` in `strict=True` → raises `AdapterError`; `strict=False` returns `(group.baseline, IngestionIssue(...))`.
   - (d) two rows ≥0.5 in `strict=False` → returns `(argmax_label, IngestionIssue(reason="ambiguous categorical decode for {group}: picked {label} from 2 candidates"))`.
   This test resolves the [GAP] from `ROADMAP.md` §"Session 3".

6. **`test_hour_bucket_to_minutes_conversion`** (`tests/test_geneva.py`) — fed inline rows with `relative_sample_date_hourly_cat ∈ {0, 1, 71}`, assert `t_minutes` in the SCALAR_TS output equals `{0.0, 60.0, 4260.0}`. Verifies the conversion lives at the right step (after `_drop_imputed`, before validate).

7. **`test_path_traversal_guard_rejects_outside_root`** (`tests/test_geneva.py`) — **expanded** sub-asserts (post-review-1.5):
   - root=`/data`, path=`/tmp/foo` → `AdapterError` with `IngestionIssue` whose `reason` mentions both paths.
   - root=None, any path → passthrough.
   - root=`/data`, path=`/data/sub/file.csv` → returns resolved path.
   - `EHR_SIM_DATA_ROOT=""` (empty string) — sub-assert that the caller's `os.environ.get(...) or None` resolves to None and the guard passes through.

7a. **`test_load_units_raises_on_missing_json`** (`tests/test_geneva.py`, **post-review-3.1**) — monkeypatch `importlib.resources.files` to return a non-existent path; call `_load_units.cache_clear()` then `_load_units()`; assert `AdapterError` mentions "geneva_units.json".

7b. **`test_load_units_raises_on_malformed_json`** (`tests/test_geneva.py`, **post-review-3.1**) — write `"{not json"` to a tmp path, monkeypatch the resource lookup to it; assert `AdapterError` from the json.loads layer.

7c. **`test_load_normalisation_params_raises_on_missing_column`** (`tests/test_geneva.py`, **post-review-3.1**) — write a CSV missing `original_std` to `tmp_path`; assert `AdapterError` whose message mentions `original_std`.

7d. **`test_load_categorical_encoding_raises_on_malformed_cell`** (`tests/test_geneva.py`, **post-review-3.1**) — write a CSV with `baseline_value="[not a python list"` to `tmp_path`; assert `AdapterError` from the `ast.literal_eval` layer; message mentions the row index or `sample_label`.

7e. **`test_inverse_normalize_passthrough_emits_issue`** (`tests/test_geneva.py`, **post-review-3.2**) — load fixture variant containing one EHR row with a `sample_label` not present in `normalisation_parameters.csv`; assert (a) the row's `value` in the output SCALAR_TS equals the input z-score (untouched), (b) `dataset.issues` contains an `IngestionIssue` with `reason` matching `r"variable .* missing from normalisation_parameters"`.

### Integration (5)

8. **`test_load_geneva_routes_sources_correctly`** (`tests/test_geneva.py`) — uses `geneva_fixture_dir`. Load. Assert:
   - `scalar_ts["source"].unique() == {"EHR"}` (all SCALAR_TS rows came from non-imputed EHR).
   - `admission["field"].unique()` contains decoded categorical names like `"Sex"` and continuous names like `"age"`; does not contain raw one-hot names like `"sex_male"`.
   - No row in any frame has `source` containing `"imputed"`.

9. **`test_load_geneva_admission_matches_expected_sidecar`** (`tests/test_geneva.py`, **post-review-3.3 — rewritten**) — load fixture; load `tests/fixtures/geneva_fixture_expected.json`; for each `(patient_id, field, value)` tuple in the expected JSON, assert an exact-match row exists in `dataset.admission`. Both directions: every expected row exists, no unexpected rows. Replaces the previous loose "at least one Sex row" assertion.

10. **`test_load_geneva_strict_vs_lenient`** (`tests/test_geneva.py`, **post-review-1.1 — pandera coerce path**) — using fixture in strict=True passes. Construct a corrupted variant (one row with `value="not a number"`, written to a `tmp_path` CSV). With `value` NOT in the read_csv dtype dict (per resolution 1.1), `pd.read_csv` succeeds; pandera's `coerce=True` raises in strict mode → `AdapterError`. With `strict=False`, pandera's lazy mode drops the offending row and `dataset.issues` is non-empty.

11. **`test_load_geneva_imaging_and_ai_output_empty_but_conforming`** (`tests/test_geneva.py`) — using fixture, assert `len(dataset.imaging) == 0`, `len(dataset.ai_output) == 0`, both pass `validate(frame, shape, strict=True)` when re-validated explicitly. Both frames are constructed via the schema-derived `empty_frame()` helper (post-review-2.1). Locks the empty-frame contract for downstream sessions.

11a. **`test_load_geneva_orphan_registry_variable_emits_issue`** (`tests/test_geneva.py`, **post-review-3.2**) — fixture variant with one `stroke_registry` row whose `sample_label` is neither categorical nor in `normalisation_parameters.csv`; assert (a) that row does not appear in `dataset.admission`, (b) `dataset.issues` contains an `IngestionIssue` with `reason="orphan registry variable: {sample_label}"`.

### E2E (3)

12. **`test_load_geneva_full_fixture_roundtrip`** (`tests/test_geneva.py`) — load fixture, assert:
    - `set(scalar_ts.patient_id.unique()) == {"geneva_fixture_001", "geneva_fixture_002"}`.
    - `set(admission.patient_id.unique()) == {"geneva_fixture_001", "geneva_fixture_002"}`.
    - All `t_minutes` in `scalar_ts` ∈ `[0.0, 71*60.0]`.
    - All four frames pass `validate(..., strict=True)` again (idempotent).
    - At least N ADMISSION fields per patient, where N is computed from the fixture (~30 fields = 19 categoricals + 11 continuous registry vars; concrete number written into the test once the fixture lands).

13. **`test_data_contract_md_no_drift`** (`tests/test_data_contract.py`) — invokes `python scripts/gen_data_contract.py --check` via `subprocess.run` in the project root; asserts exit code 0 and stderr is empty. If the check fails, the test prints the diff snippet to aid the implementer fixing it.

13a. **`test_load_geneva_real_csv_smoke`** (`tests/test_geneva_real.py`, **post-review-1.3**) — marked `@pytest.mark.real_data`; **skipped by default** in CI and `pytest -n auto`; runnable via `uv run pytest -m real_data`. Loads `/mnt/data1/klug/datasets/opsum/.../preprocessed_features_30012026_154047.csv` + `.../logs_30012026_154047/` via `load_geneva(strict=False)`. Asserts: (a) load completes within 120 s wall time; (b) `dataset.scalar_ts` has rows for all 2,657 patients (or whatever the implementer measures and locks); (c) `dataset.admission` has 2,657 patients; (d) `dataset.issues` is reportable (printed on test failure for debugging). Adds `real_data` marker to `[tool.pytest.ini_options]` so `--strict-markers` is satisfied.

### Regression (1, non-negotiable)

14. **`test_load_geneva_raises_on_missing_required_columns`** (`tests/test_geneva.py`) — write a CSV missing the `source` column to `tmp_path`; call `load_geneva` with `params_dir=geneva_fixture_dir`. Assert `AdapterError` whose message mentions `source` (the missing column). Locks the schema-drift failure mode so an upstream schema change surfaces loudly, not silently.

**Total new tests: 26** (15 unit / 5 integration / 3 E2E / 1 regression / +1 marker registration). With S1's 15 + S2's 23, the project is at **64 tests** post-S3.

---

## 10. CI changes (`.github/workflows/ci.yml`)

Add **after** the existing `Test` step:

```yaml
- name: Data-contract drift check
  run: uv run python scripts/gen_data_contract.py --check
```

The step runs on both Python 3.11 and 3.12 (matrix-inherited). Failure remediation, documented in the script's `--help`: `uv run python scripts/gen_data_contract.py` then commit the regenerated `docs/data-contract.md`.

The Playwright E2E job from S2 is unchanged.

---

## 11. Acceptance criteria (how you know S3 is done)

Every item is a check a reviewer can run.

- [ ] `uv sync` clean.
- [ ] `uv run pytest` green; **26 new tests**; total **64 tests** across S1+S2+S3 (excluding the `e2e` and `real_data` markers which run via `uv run pytest -m e2e` and `uv run pytest -m real_data` respectively).
- [ ] `src/ehr_simulator/ingestion/data/geneva_units.json` is committed; contents valid JSON parsing to a non-empty `dict[str, str]`.
- [ ] `uv run python scripts/gen_data_contract.py` produces `docs/data-contract.md`; `--check` re-run is clean.
- [ ] `uv run python -c "from ehr_simulator.ingestion import load_geneva; from pathlib import Path; ds = load_geneva(Path('tests/fixtures/geneva_sample.csv'), Path('tests/fixtures')); print(len(ds.scalar_ts), len(ds.admission))"` exits 0 with non-zero counts on both frames.
- [ ] `uv run pytest -m real_data` exits 0 (post-review-1.3 smoke test on the real 1.5 GB CSV; opt-in marker, skipped by default).
- [ ] `EHR_SIM_DATA_ROOT=/mnt/data1/klug/datasets/opsum uv run python -c "from ehr_simulator.ingestion import load_geneva; from pathlib import Path; load_geneva(Path('/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/gsu_Extraction_20220815_prepro_30012026_154047/preprocessed_features_30012026_154047.csv'), Path('/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/gsu_Extraction_20220815_prepro_30012026_154047/logs_30012026_154047'))"` exits 0.
- [ ] `EHR_SIM_DATA_ROOT=/tmp uv run python -c "..."` (same call as above) exits non-zero with `AdapterError` mentioning `path traversal`.
- [ ] `EHR_SIM_DATA_ROOT="" uv run python -c "..."` (empty-string sentinel) exits 0 — the empty string is treated as unset (post-review-1.5).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI passes on a PR opened against `main`, including the `Data-contract drift check` step.
- [ ] `docs/data-contract.md` is committed and matches what the generator produces.
- [ ] `tests/fixtures/geneva_sample.csv`, `normalisation_parameters.csv`, `categorical_variable_encoding.csv`, `geneva_fixture_expected.json` are all committed.

---

## 12. Conventions

- `from __future__ import annotations` at the top of every new module.
- Module docstrings on every new module (`geneva.py`, `gen_data_contract.py`, `build_geneva_fixture.py`, `test_geneva.py`, `test_data_contract.py`).
- Type hints on every public function.
- `ast.literal_eval` for parsing the Python-list strings in `categorical_variable_encoding.csv`. **Never `eval`.**
- No comments that restate code (per repo `CLAUDE.md`). Helper docstrings document WHY (especially the 0.5-edge resolution).
- Test names: `test_<subject>_<expected_behavior>`. One assertion group per test (sub-asserts in test #5 and #7 are documented as table-driven).
- Inline `pd.DataFrame({...})` in unit tests; the fixture is reserved for integration / E2E.
- The 0.5-edge contract is documented in `_decode_categorical`'s docstring AND the routing-table section of `geneva.py`'s module docstring AND test #5 — three places, one truth.
- `_decode_categorical`'s strict-vs-lenient tier (post-review-1.2) is documented in the helper's docstring AND `load_geneva`'s docstring (so callers know lenient mode is non-fatal on ambiguous decodes) AND test #5 sub-asserts (b)/(c)/(d).
- `_load_units` uses `@functools.lru_cache(maxsize=1)` (post-review-2.2). No module-level mutable global. Tests use `_load_units.cache_clear()` for isolation.
- The schema-derived `empty_frame(shape)` helper lives in `canonical.py` (post-review-2.1); both Geneva and any future adapter (MIMIC, etc.) consume it. No per-adapter empty constructors.

---

## 13. Commit discipline (target ~5 commits, ~1.5 days)

Adheres to the S1 ratio (1 spec ≈ 1-2 days, ~5 commits). If any commit balloons past ~400 lines, split.

| # | Commit | Files |
|---|---|---|
| 1 | `session-03: scaffolding + helpers + unit tests` | `geneva.py` skeleton (module docstring, dataclasses, all 7 helpers, post-review tiered `_decode_categorical` + `@lru_cache` on `_load_units`), `canonical.py` (+`empty_frame()` helper, post-review-2.1), `scripts/build_geneva_units.py`, `src/ehr_simulator/ingestion/data/geneva_units.json` (committed), `tests/test_geneva.py` (unit tests #1–#7 + #4b + #4c + #7a–#7e). |
| 2 | `session-03: load_geneva integration + Geneva fixture` | `geneva.py` (`load_geneva` body + routing logic + chunked read + per-chunk `_drop_imputed` per post-review-1.3), `tests/fixtures/build_geneva_fixture.py` (writes the sidecar `geneva_fixture_expected.json` per post-review-3.3), `tests/fixtures/geneva_sample.csv`, `tests/fixtures/normalisation_parameters.csv`, `tests/fixtures/categorical_variable_encoding.csv`, `tests/fixtures/geneva_fixture_expected.json`, `tests/conftest.py` (+`geneva_fixture_dir`), `tests/test_geneva.py` (integration tests #8, #9 (sidecar-driven), #10, #11, #11a, E2E #12, regression #14). |
| 3 | `session-03: EHR_SIM_DATA_ROOT path-traversal guard + real-data smoke` | `geneva.py` (`_path_traversal_guard` wired into `load_geneva` with empty-string sentinel per post-review-1.5), `tests/test_geneva.py` (test #7 expanded), `tests/test_geneva_real.py` (test #13a — `@pytest.mark.real_data` smoke test per post-review-1.3), `pyproject.toml` (+`real_data` marker registration). |
| 4 | `session-03: data-contract generator + drift CI gate` | `scripts/gen_data_contract.py` (canonical.py + pandera introspection only, no geneva.__doc__ parsing per post-review-2.3), `docs/data-contract.md`, `tests/test_data_contract.py` (test #13), `.github/workflows/ci.yml` (drift-check step). |
| 5 | `session-03: ingestion __init__ re-exports + acceptance polish` | `src/ehr_simulator/ingestion/__init__.py` (+`GenevaDataset`, +`load_geneva`, +`empty_frame`), final ruff/format pass. |

Splitting the path-traversal guard + real-data smoke into commit 3 keeps commit 2's diff focused on the load-path and isolates the env-var/perf contracts for review.

---

## 14. Open decisions deferred to later sessions

- **Lift helpers to `_shared.py`** — S4 (MIMIC). Decision criterion: lift only if MIMIC's helper signatures match exactly. If MIMIC's `notes` source needs different decoding logic than `stroke_registry`, fork and document why.
- **`EHR_SIM_DATA_ROOT` becoming required** — S5 (`validate-adapter` / `preflight` CLI). The CLI sets the env var before invoking adapters and tightens the guard to required.
- **Coverage gaps in `geneva_units.json`** — S8 (real-data UI). Decision criterion: if the real-data UI surfaces variables with `unit=None` and clinicians flag the gap, file an upstream PR against the OPSUM xlsx, refresh the JSON via `scripts/build_geneva_units.py`. The repo carries no hand-edited overrides; upstream is the single source.
- **`selected_variables.xlsx` ingestion** — not needed; the CSV is self-describing. Revisit only if downstream metadata (e.g. variable display names, French-to-English translation) requires it.
- **AI predictions** — S7 (`load_geneva_ai_predictions`). Until then, `AI_OUTPUT` is empty by design.
- **Display formatting** (decimal places, locale-aware decimal separators, French→English variable name translation for `bilirubine totale` etc.) — S8.
- **Memoizing `_load_normalisation_params` / `_load_categorical_encoding`** — premature; called once per `load_geneva` invocation and the files are <2 KB each. Revisit only if profiling shows otherwise.

---

## 15. What Session 3 does NOT lock

- MIMIC adapter (S4). The S4 spec decides whether helpers lift to `_shared.py`.
- AI predictions (S7). `AI_OUTPUT` stays empty in S3.
- Real-data UI integration (S8). The thin UI from S2 keeps loading `load_synthetic` until S8.
- Display-formatting policy (decimal places, French→English variable translation, units rendering rules in panels). Punted to S8 where the real-data UI surfaces what's actually needed.
- Performance budgets. The fixture is small (<5K rows); the real CSV is ~20M rows. S8 measures actual load time and decides whether to add caching, parquet conversion, or column-pruning. S3 trusts pandas defaults.
- Tighter `EHR_SIM_DATA_ROOT` contract — S5.
- Backup cadence (D10) — S6 wires it into SQLite boot.

---

## 16. What already exists (carried into S3)

These shipped in S1/S2 and are reused by S3 unchanged. Listed so review-driven changes don't accidentally duplicate.

- **`src/ehr_simulator/ingestion/canonical.py`** — 4 pandera schemas + `validate(frame, shape, *, strict, dataset)`. Reused. The module docstring is the source for `gen_data_contract.py`.
- **`src/ehr_simulator/ingestion/exceptions.py`** — `AdapterError` + `IngestionIssue`. Reused. `_decode_categorical`, `_path_traversal_guard`, and `load_geneva` all raise `AdapterError`; `IngestionIssue` carries unit-coverage gaps and lenient-mode dropped rows.
- **`src/ehr_simulator/ingestion/synthetic.py`** — pattern reference for how a real adapter calls `validate(..., strict=True)` per shape (`synthetic.py:109-112`). S3 mirrors that call shape.
- **`src/ehr_simulator/ingestion/__init__.py`** — public surface; S3 extends it.
- **`pyproject.toml`** — `pandas>=2.2`, `pandera[pandas]>=0.20` already pinned; no new top-level deps.
- **`.github/workflows/ci.yml`** — already runs `uv sync --locked`, ruff, ruff format, pytest on Python 3.11 + 3.12. S3 §10 adds one step.
- **`tests/conftest.py`** — already has the `dataset` and `tmp_log_dir` fixtures from S2; S3 adds `geneva_fixture_dir`.
- **`specs/session-01-data-contract.md`** §"What Session 1 does NOT lock" — pre-locked routing rules, inverse-normalization gotchas, categorical-encoding warning. The S3 spec quotes from this section so the audit trail stays intact.

---

## 17. Review-driven decisions log

`/plan-eng-review` ran against this spec on 2026-05-06. Twelve issues surfaced and were resolved as concrete spec patches (listed below). Outside-voice was skipped (12 issues already resolved, structural blind spots low). All resolutions are CLEAN with no critical gaps.

### Resolved decisions (12)

| ID | Section | Decision |
|---|---|---|
| 1.1 | §4 step 2 | Drop `value: float` from `pd.read_csv` dtype dict; rely on pandera coerce so `validate(strict=False)` can drop the malformed row instead of read_csv raising. |
| 1.2 | §3 helper table — `_decode_categorical` | Strict mode raises `AdapterError(issues=[IngestionIssue(patient_id=pid, ...)])`; lenient mode picks argmax and appends `IngestionIssue("ambiguous categorical decode for {group}: picked {label} from {n} candidates")`. Preserves load on real data. |
| 1.3 | §4 step 2 + §9 | Read CSV with `usecols=_REQUIRED_COLUMNS, chunksize=500_000`; `_drop_imputed` runs inside the chunk loop; concat after. Add `tests/test_geneva_real.py::test_load_geneva_real_csv_smoke` marked `@pytest.mark.real_data` (skipped by default; runnable via `uv run pytest -m real_data`). |
| 1.4 | §3 helper table — `_load_categorical_encoding` | Signature becomes `(path, sample_labels: set[str])`; raises `AdapterError` if any computed `one_hot_columns` entry isn't present in the input frame's labels. New unit test #4c covers all 19 groups against the fixture. |
| 1.5 | §4 step 1 + §6 + §3 helpers | Read env var as `os.environ.get("EHR_SIM_DATA_ROOT") or None` (empty string → unset); sub-assert added to test #7. `_decode_categorical` and `_path_traversal_guard` raise with `IngestionIssue` carrying `patient_id` / offending path. |
| 2.1 | §5 | Replace inline `_empty_imaging` / `_empty_ai_output` with a single `empty_frame(shape: CanonicalShape)` helper in `canonical.py`; both call sites consume it. Schema-derived; survives schema additions. |
| 2.2 | §3 helper table — `_load_units` | Decorate with `@functools.lru_cache(maxsize=1)`; remove `_UNITS` module global. Tests use `_load_units.cache_clear()` for isolation. |
| 2.3 | §7 | Drop the routing-rules section from `data-contract.md`. Generator reads only `canonical.py` + pandera schema introspection; one-liner at the bottom points to `ehr_simulator/ingestion/{adapter}.py` for adapter-specific routing. |
| 3.1 | §9 | Add four file-loader failure-mode tests: `_load_units` JSON missing, `_load_units` JSON malformed, `_load_normalisation_params` missing column, `_load_categorical_encoding` `ast.literal_eval` failure. Each ~5 LOC. |
| 3.2 | §3 + §9 | `_inverse_normalize` passthrough emits `IngestionIssue("variable {x} missing from normalisation_parameters")`; orphan registry variable emits `IngestionIssue("orphan registry variable: {sample_label}")`. Both tested. |
| 3.3 | §8 + §9 test #9 | Fixture builder writes `tests/fixtures/geneva_fixture_expected.json` as part of regeneration. Test #9 loads the JSON and asserts every `(patient_id, field, value)` tuple in ADMISSION matches exactly. |
| 4.1 | §15 | Performance of `_decode_categorical` (~25 s on real CSV) deferred to S8 measurement; codified smoke test from 1.3 will measure. Vectorize-when-needed entry added to TODOS.md. |

### Test count delta

- Spec ships 15 tests.
- Resolutions add 11 new tests + rewrite #9.
- **Post-S3 total: 64 tests** (15 S1 + 23 S2 + 26 S3).

### TODOs added (2)

- Vectorize `_decode_categorical` via `groupby + idxmax` if S8 smoke measures >30 s.
- Emit a structlog WARNING when `_decode_categorical` falls back to argmax in lenient mode (S5+).

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (non-UI session, optional) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | 12 issues, 0 critical gaps, 0 unresolved |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | not applicable (no UI in S3) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **UNRESOLVED:** 0
- **VERDICT:** ENG CLEARED — ready to implement
