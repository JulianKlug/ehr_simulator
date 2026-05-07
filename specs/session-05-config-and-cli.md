# Session 05 — Study config + Typer CLI + CSP

**Goal:** the simulator knows which patients, timepoints, and questions to use before any real-data pilot runs. This session adds two Pydantic-validated YAML files (`study_config.yaml`, `questions.yaml`) gated by a `schema_version: "1"` field, a Typer CLI replacing today's argparse skeleton (5 commands: `serve`, `validate-config`, `validate-adapter`, `preflight`, `preview`), tightens `EHR_SIM_DATA_ROOT` to required at the `validate-adapter` boundary, ships a Content-Security-Policy middleware on the FastAPI app, and folds two structlog-WARNING TODOs from S3/S4 (`_decode_categorical` argmax fallback, `_read_features_csv` unrecognized-source) so the new CLI is the loud signal a real-data pilot needs. `export-answers` slips to S9c per the roadmap. SQLite + `clinicians` lookup + answer capture all stay in S6+.

**Out of scope (later sessions):** SQLite + `clinicians` table + migrations runner + backup cadence (S6); Geneva AI predictions adapter (S7); real-data UI on Geneva and the validate-once-cache perf budget (S8); answer capture, question gating, CSV export (S9a/b/c); divergence view (S10); Phase-2 randomization arms (S11); v1.0 polished release (S12); DICOM rendering, FHIR layer, mobile layouts; `clinician_name` login/auth flow (UI work owned by S6 alongside the persistence layer).

---

## Context

Sessions 1–4 shipped the data path: four pandera-locked canonical shapes (S1), a thin FastAPI/HTMX UI on synthetic data (S2), a Geneva adapter (S3), and a MIMIC adapter (S4) — both real-data adapters share `_shared.py` and ship a sidecar drift CI gate. The UI today defaults to synthetic and there is no machine-readable description of which patients to walk through, in what order, at which timepoints, or what questions a clinician will face.

Without that description nothing else in the roadmap can run end-to-end. S6's SQLite layer needs a `config_hash` to compute over (the SHA256 of `study_config.yaml + questions.yaml`). S8's real-data UI needs to know whether the dataset is Geneva or MIMIC and where its CSV lives. S9a's `/answer` endpoint needs the question list to enforce "all answered before advance." S11's arm randomization keys on `(clinician_id, patient_id)` pairs that come from the patient list. The whole roadmap downstream of S5 inherits from these two YAML files; a sloppy schema here propagates.

S5 also picks up four deferred items the upstream specs explicitly handed off: schema-version field on configs (S1 D6 deferred), tighter `EHR_SIM_DATA_ROOT` contract (S3 §"Open decisions deferred"), CSP header before v1.0 release surface (TODOS.md plan-eng-review on S2), structlog WARNING when `_decode_categorical` falls back to argmax (TODOS.md plan-eng-review on S3 — `validate-adapter` is the natural emitter), and a Geneva-side defensive-issue test on `_read_features_csv` (TODOS.md plan-eng-review on S4 — same WARNING infrastructure). All four are S5 by spec; rolling them in here closes the boil-the-lake gap before the first real pilot.

The `serve` command stays runnable without a config (synthetic default unchanged). `serve --config study.yaml` wires up a config-driven `dataset_loader` closure for the FastAPI app factory. The roadmap pins S8 as the session that validates the real-data UI experience under perf load; S5 lands the plumbing so S8 is mechanical.

---

## Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | +`typer>=0.12`, +`pydantic>=2.7`, +`pyyaml>=6.0` to top-level deps; +`types-pyyaml>=6.0` to dev deps. CLI entry point string unchanged (`ehr-simulator = "ehr_simulator.cli:main"`). |
| 2  | `src/ehr_simulator/config/__init__.py` | NEW. Re-export `StudyConfig`, `Questions`, `Question`, `ResponseType`, `ConfigError`, `load_study_config`, `load_questions`, `compute_config_hash`. |
| 3  | `src/ehr_simulator/config/study.py` | NEW. `StudyConfig` Pydantic model (study_config.yaml shape). Locks `schema_version: Literal["1"]`, `dataset: Literal["synthetic","geneva","mimic"]`, optional `csv_path`/`params_dir` overrides, `patient_ids: list[str]` (deduped, ordered), `time_unit: Literal["minutes","hours"]`, `timepoints: list[float]` (sorted, ≥0). |
| 4  | `src/ehr_simulator/config/questions.py` | NEW. `Questions` collection model + `Question` discriminated union over 5 `ResponseType` primitives (`likert`, `categorical`, `multi-select`, `probability-0-100`, `free-text`). Per-type config (likert scale points, categorical/multi-select options) lives on the discriminated variants. |
| 5  | `src/ehr_simulator/config/exceptions.py` | NEW. `ConfigError` wraps Pydantic `ValidationError` to a single human-readable message that names the file + the offending field path; CLI prints the message + exits 1 on catch. |
| 6  | `src/ehr_simulator/config/loader.py` | NEW. `load_study_config(path) -> StudyConfig`, `load_questions(path) -> Questions`, `compute_config_hash(study_path, questions_path) -> str` (SHA256 of `study_path.read_bytes() + b"\\x00" + questions_path.read_bytes()`; consumed by S6's `config_hash` columns). YAML→dict via `yaml.safe_load`, then dict→model via Pydantic. Schema-version mismatch surfaces a `ConfigError` whose message names both the expected and observed version (D6 D-resolution). |
| 7  | `src/ehr_simulator/cli.py` | REWRITTEN. argparse → Typer. 5 commands: `serve`, `validate-config`, `validate-adapter`, `preflight`, `preview`. `main()` signature stays `(argv: list[str] \| None = None) -> None` for back-compat with `test_cli.py`'s monkeypatch pattern. |
| 8  | `src/ehr_simulator/cli_support.py` | NEW. Pure functions consumed by CLI commands so commands stay thin: `build_dataset_loader(study: StudyConfig) -> Callable[[], Dataset]` (returns a closure routing `dataset` enum + path overrides + env-var fallback to `load_synthetic` / `load_geneva` / `load_mimic`); `walk_preflight(study, questions) -> PreflightReport` (headlessly walks every `(patient_id, timepoint)` against the resolved dataset and aggregates issues); `render_preview(study, patient_id) -> PreviewReport` (text-summary table). The HTML-out path delegates to a TestClient wrapper around `create_app`. |
| 9  | `src/ehr_simulator/web/middleware.py` | NEW. `CSPMiddleware` (pure ASGI, mirroring `RequestContextMiddleware`'s shape). Sets `Content-Security-Policy: default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'`. **Per /plan-eng-review issue 2.1:** `script-src 'unsafe-inline'` is dropped — verified zero inline `<script>` blocks and zero `on*=` attributes across `web/templates/`. htmx's `hx-*` attributes are HTML data-attributes processed by the loaded htmx library, not script, so they don't require `'unsafe-inline'` for `script-src`. Only `style-src 'unsafe-inline'` remains, and only because plotnine emits inline `<style>` blocks inside its SVG output. The remaining allowance is documented in the module docstring as the open-source-release upper bound (revisit at v1.0 to tighten via nonce or hashed external stylesheet). |
| 10 | `src/ehr_simulator/web/app.py` | MODIFIED. `create_app` accepts a new optional `dataset_loader` argument (signature unchanged from S2 — already parameterized; the `load_synthetic` default holds when the kwarg is omitted). Wires `CSPMiddleware` into the middleware stack BELOW `RequestContextMiddleware` (request context binds first; CSP is a response-header concern). Config-bootstrap helper `app_from_study_config(study_path, questions_path, *, log_dir) -> FastAPI` consumed by `cli.py serve --config`. **Per /plan-eng-review issue 1.2:** `app_from_study_config` sets `app.state.study_timepoints = study.timepoints_minutes` so the URL ordinal `t_index` resolves to study-defined timepoints, not dataset-derived ones. The `serve` no-config path leaves `app.state.study_timepoints` unset; routes fall back to `patient_timepoints(dataset, pid)` then. |
| 10b | `src/ehr_simulator/web/routes.py` | MODIFIED. `patient_timepoint` reads `request.app.state.study_timepoints` when present (study config loaded) and uses it as the t_index lookup; falls back to `patient_timepoints(dataset, pid)` when absent (synthetic-only `serve` path). One-line getattr with default: `study_tps = getattr(request.app.state, "study_timepoints", None)`. Closes the silent study-validity bug surfaced by /plan-eng-review issue 1.2. |
| 10c | `src/ehr_simulator/web/panels.py` | MODIFIED. **Per /plan-eng-review tension B:** `slice_to_timepoint` and `patient_timepoints` currently type `dataset: SyntheticDataset`, which prevents `walk_preflight` from compiling against Geneva/MIMIC. Replace with a `DatasetLike` Protocol: `class DatasetLike(Protocol): scalar_ts: pd.DataFrame; admission: pd.DataFrame; imaging: pd.DataFrame; ai_output: pd.DataFrame`. `SyntheticDataset`, `GenevaDataset`, `MimicDataset` already satisfy the Protocol structurally. ~10 LOC + the Protocol class. |
| 11 | `src/ehr_simulator/ingestion/_shared.py` | MODIFIED. `_decode_categorical` lenient-mode argmax fallback emits `structlog.get_logger("ehr_simulator").warning(...)` with `event_kind="ingest.categorical.argmax_fallback"`, `dataset`, `patient_id`, `group_name`, `winner_label`. The existing `IngestionIssue` emission is unchanged; the WARNING is purely additive. `_read_features_csv` similarly emits `event_kind="ingest.source.unrecognized"` when adding to `attrs["unrecognized_sources"]`. (Resolves two TODOS.md items at once; both depend on S5 wiring up the WARNING infrastructure.) |
| 12 | `tests/fixtures/study/study_synthetic.yaml` | NEW. Reference happy-path study config for tests + the shipped synthetic walkthrough. |
| 13 | `tests/fixtures/study/study_geneva.yaml` | NEW. Geneva-flavored config with inline path overrides pointing at `tests/fixtures/geneva/`. |
| 14 | `tests/fixtures/study/study_mimic.yaml` | NEW. MIMIC-flavored config with inline path overrides pointing at `tests/fixtures/mimic/`. |
| 15 | `tests/fixtures/study/questions.yaml` | NEW. Reference happy-path questions file: the 4 sample questions from `example_questions.md` rendered as `categorical {Yes,No,Unknown}` plus 1 `probability-0-100`, 1 `likert` (1–5), 1 `multi-select`, 1 `free-text` to exercise all 5 primitives. |
| 16 | `tests/fixtures/study/study_broken_*.yaml` | NEW (5 files). Negative-case fixtures: missing `schema_version`, wrong version (`"2"`), bad `time_unit` (`"days"`), unknown `dataset` (`"omop"`), duplicate `patient_id`. One file per failure mode; each is read by exactly one parametrized test. |
| 17 | `tests/fixtures/study/questions_broken_*.yaml` | NEW (4 files). Categorical without `options`; likert without `scale_min`/`scale_max`; duplicate `question_id`; duplicate options within a categorical (per /plan-eng-review issue 2.3). |
| 18 | `tests/test_config.py` | NEW. ~14 tests covering Pydantic schemas, `schema_version` gating, all 5 response-type primitives, `compute_config_hash` round-trip + collision sensitivity. |
| 19 | `tests/test_cli.py` | REWRITTEN. `CliRunner`-based pattern (Typer ships its own `typer.testing.CliRunner` — different from Click's; the Typer one is exit-code-friendly). 1+ test per command (≥10 total covering happy + sad paths). `serve` keeps the monkeypatch-uvicorn pattern for back-compat. |
| 20 | `tests/test_csp.py` | NEW. 3 tests asserting CSP header on `/`, on an HTMX swap (`HX-Request` header set), and on the error path (404). |
| 21 | `tests/test_shared.py` | EXTENDED. +1 test: `_decode_categorical` argmax fallback emits the WARNING (caplog-based assertion against the structlog event dict). |
| 22 | `tests/test_geneva.py` | EXTENDED. +1 test: `_read_features_csv` defensive issue emission against the real geneva fixture (TODOS.md S4 carryover; the `WARNING` event for unrecognized sources surfaces in caplog). |
| 23 | `tests/test_app.py` (or extend `tests/test_routes.py`) | EXTENDED. +1 test: `app_from_study_config` resolves a synthetic study config to a working FastAPI app whose `/patient/synth_001/timepoint/0` returns 200. |
| 24 | `tests/conftest.py` | EXTENDED. +`study_fixture_dir` fixture pointing at `tests/fixtures/study/`. |
| 25 | `.github/workflows/ci.yml` | EXTENDED. One new step: `CLI smoke` running `uv run ehr-simulator validate-config tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml`, `validate-adapter --dataset synthetic`, and `preflight tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml`. The S3 `Data-contract drift check` and S4 `MIMIC fixture sidecar drift check` steps are unchanged. |

`README.md` and `LICENSE` are not touched in S5. README's CLI block updates are owned by `/document-release` post-ship (per CLAUDE.md skill routing).

---

## Repo layout after Session 5 (diff vs end-of-S4)

```
ehr_simulator/
├── src/ehr_simulator/
│   ├── cli.py                                # REWRITTEN (argparse → Typer)
│   ├── cli_support.py                        # NEW
│   ├── config/                               # NEW
│   │   ├── __init__.py
│   │   ├── exceptions.py
│   │   ├── loader.py
│   │   ├── questions.py
│   │   └── study.py
│   ├── ingestion/
│   │   └── _shared.py                        # MODIFIED (+structlog WARNINGs)
│   └── web/
│       ├── app.py                            # MODIFIED (+CSP wiring, +app_from_study_config, +study_timepoints binding)
│       ├── middleware.py                     # NEW (CSPMiddleware, script-src 'self'-only)
│       ├── panels.py                         # MODIFIED (+DatasetLike Protocol; widen slice_to_timepoint signature)
│       └── routes.py                         # MODIFIED (read app.state.study_timepoints when set)
├── tests/
│   ├── conftest.py                           # MODIFIED (+study_fixture_dir)
│   ├── fixtures/
│   │   └── study/                            # NEW
│   │       ├── questions.yaml
│   │       ├── questions_broken_categorical_no_options.yaml
│   │       ├── questions_broken_duplicate_id.yaml
│   │       ├── questions_broken_duplicate_options.yaml
│   │       ├── questions_broken_likert_no_scale.yaml
│   │       ├── study_broken_bad_time_unit.yaml
│   │       ├── study_broken_duplicate_patient.yaml
│   │       ├── study_broken_missing_schema_version.yaml
│   │       ├── study_broken_unknown_dataset.yaml
│   │       ├── study_broken_wrong_schema_version.yaml
│   │       ├── study_geneva.yaml
│   │       ├── study_mimic.yaml
│   │       └── study_synthetic.yaml
│   ├── test_app.py                           # NEW (or test_routes.py extension)
│   ├── test_cli.py                           # REWRITTEN
│   ├── test_config.py                        # NEW
│   ├── test_csp.py                           # NEW
│   ├── test_geneva.py                        # MODIFIED (+1 defensive-issue test)
│   └── test_shared.py                        # MODIFIED (+1 argmax-warning test)
└── .github/workflows/ci.yml                  # MODIFIED (+CLI smoke step)
```

---

## 1. `pyproject.toml` deltas

```toml
[project]
dependencies = [
    "pandas>=2.2",
    "pandera[pandas]>=0.20",
    "fastapi>=0.115",
    "uvicorn[standard]>=0.32",
    "jinja2>=3.1",
    "plotnine>=0.13",
    "structlog>=24.4",
    "typer>=0.12",        # NEW
    "pydantic>=2.7",      # NEW
    "pyyaml>=6.0",        # NEW
]

[dependency-groups]
dev = [
    # ... existing entries unchanged ...
    "types-pyyaml>=6.0",  # NEW (pyyaml has no inline stubs)
]
```

The `pytest.ini_options` `addopts = "-n auto --strict-markers -m 'not e2e and not real_data'"` is unchanged. No new pytest markers.

---

## 2. `study_config.yaml` schema (the canonical shape)

Reference happy-path file (`tests/fixtures/study/study_synthetic.yaml`):

```yaml
schema_version: "1"
dataset: synthetic
patient_ids: [synth_001, synth_002, synth_003]
time_unit: minutes
timepoints: [0, 60, 180]
```

Geneva variant with inline path overrides (`tests/fixtures/study/study_geneva.yaml`):

```yaml
schema_version: "1"
dataset: geneva
csv_path: tests/fixtures/geneva/geneva_sample.csv
params_dir: tests/fixtures/geneva
patient_ids: [g_fixture_001, g_fixture_002]
time_unit: hours
timepoints: [0, 1, 24, 48]
```

Field contract (`StudyConfig` Pydantic model in `src/ehr_simulator/config/study.py`):

| Field | Type | Constraint |
|---|---|---|
| `schema_version` | `Literal["1"]` | exact match; mismatch → `ConfigError` |
| `dataset` | `Literal["synthetic","geneva","mimic"]` | enum; routes to the right loader |
| `csv_path` | `Path \| None` | optional; required iff `dataset != "synthetic"` AND `EHR_SIM_DATA_ROOT` is unset |
| `params_dir` | `Path \| None` | same rule as `csv_path`; both must be set or both unset |
| `patient_ids` | `list[str]` | non-empty; deduped (duplicates raise); preserves order |
| `time_unit` | `Literal["minutes","hours"]` | enum |
| `timepoints` | `list[float]` | non-empty; all `≥0`; sorted ascending; deduped |

Pydantic validators enforce:
- `csv_path` and `params_dir` are either both set or both unset (`@model_validator(mode="after")`).
- When `dataset == "synthetic"`, `csv_path`/`params_dir` are forbidden (raise on presence).
- `patient_ids` no duplicates: `if len(set(v)) != len(v): raise ValueError(...)`.
- `timepoints` sorted ascending, no duplicates, all `≥0`.

**Relative path resolution (per /plan-eng-review issue 2.2):** when `csv_path` or `params_dir` are relative, `loader.load_study_config(path)` resolves them against `path.parent`, not `Path.cwd()`. Convention matches Docker Compose, ESLint, Prettier, and every config-driven tool a researcher has likely used. Implementation: a `@model_validator(mode="after")` wired by `loader.load_study_config(path)` — the validator receives the YAML's parent dir via `model_validate(data, context={"yaml_dir": path.parent})`. Tested by test #11.5 (a study config in a `tmp_path` whose `csv_path: data/foo.csv` resolves to `tmp_path/data/foo.csv`).

`StudyConfig` exposes a derived property `timepoints_minutes: list[float]` that converts via `[t * 60.0 if time_unit == "hours" else t for t in timepoints]`. Downstream code (`walk_preflight`, S8 routes) uses this property and never the raw `timepoints` list, decoupling URL/storage/log formats from the user's chosen unit. Reuses S2's existing decoupling per `session-02-thin-ui-synthetic.md` §6 ("the URL ordinal `t_index` is decoupled from the time unit; S5 will resolve `minutes` vs `hours` from the study config").

`schema_version: "1"` is a string literal, not an int. Strings versionize cleanly under YAML round-tripping (no `1` vs `"1"` ambiguity) and SemVer-friendly going forward. Locks D6 (S1 deferred decision).

---

## 3. `questions.yaml` schema

Reference happy-path file (`tests/fixtures/study/questions.yaml`):

```yaml
schema_version: "1"
questions:
  - question_id: deterioration_6h
    prompt: "Will the patient have a neurological deterioration in the next 6 hours?"
    response_type: categorical
    options: [Yes, No, Unknown]

  - question_id: survives_hospital
    prompt: "Will the patient survive the hospital stay?"
    response_type: categorical
    options: [Yes, No, Unknown]

  - question_id: good_outcome_3mo
    prompt: "Will the patient have a good neurological outcome at 3 months?"
    response_type: probability-0-100

  - question_id: dead_6mo
    prompt: "Will the patient be dead at 6 months?"
    response_type: categorical
    options: [Yes, No, Unknown]

  - question_id: confidence
    prompt: "How confident are you in your assessment?"
    response_type: likert
    scale_min: 1
    scale_max: 5
    scale_min_label: "Not at all"
    scale_max_label: "Very"

  - question_id: contributing_factors
    prompt: "Which factors contributed most to your decision?"
    response_type: multi-select
    options: [Imaging, Vitals, Labs, Medical history, AI output]

  - question_id: free_notes
    prompt: "Any additional reasoning?"
    response_type: free-text
```

Field contract (`Questions` collection + `Question` discriminated union in `src/ehr_simulator/config/questions.py`):

```python
ResponseType = Literal[
    "likert", "categorical", "multi-select",
    "probability-0-100", "free-text",
]

class _QuestionBase(BaseModel):
    question_id: str  # non-empty, [a-z0-9_]+
    prompt: str       # non-empty

class LikertQuestion(_QuestionBase):
    response_type: Literal["likert"]
    scale_min: int
    scale_max: int
    scale_min_label: str | None = None
    scale_max_label: str | None = None

class CategoricalQuestion(_QuestionBase):
    response_type: Literal["categorical"]
    options: list[str]  # ≥2, deduped

class MultiSelectQuestion(_QuestionBase):
    response_type: Literal["multi-select"]
    options: list[str]  # ≥2, deduped

class ProbabilityQuestion(_QuestionBase):
    response_type: Literal["probability-0-100"]

class FreeTextQuestion(_QuestionBase):
    response_type: Literal["free-text"]

Question = Annotated[
    LikertQuestion | CategoricalQuestion | MultiSelectQuestion
    | ProbabilityQuestion | FreeTextQuestion,
    Field(discriminator="response_type"),
]

class Questions(BaseModel):
    schema_version: Literal["1"]
    questions: list[Question]  # non-empty, question_id deduped
```

Pydantic discriminated unions surface "I expected `likert | categorical | …` but got `gut-feeling`" as a single error message at the right field path. Validators enforce:
- `Questions.questions` non-empty.
- `question_id` unique across the list (collection-level validator).
- `question_id` matches `^[a-z0-9_]+$` (used as CSV column suffix in S9c; tightening here avoids SQL-injection / cell-injection paths later).
- `LikertQuestion.scale_min < scale_max`.
- `CategoricalQuestion.options` and `MultiSelectQuestion.options`: non-empty, ≥2 entries, **no duplicates (raises)** — per /plan-eng-review issue 2.3, mirror the `patient_ids` rule. Silent dedupe could drop both halves of a typo (e.g., `Yes / yes / No` collapsing to `Yes / No`) and surface as a different error downstream; raise loud and early instead. Tested by `questions_broken_duplicate_options.yaml` fixture + a test in test_config.py.

S9-foresight fields like `required: bool` or `prompt_context: str` are **not** added in S5. The schema is additive-friendly (Pydantic ignores unknown fields by default in v2, but we set `model_config = ConfigDict(extra="forbid")` so future field additions surface as breaking changes routed through `schema_version` bump). YAGNI for S9 plumbing the schema can't yet test.

---

## 4. `compute_config_hash` (for S6's `config_hash` columns)

**Resolved per /plan-eng-review tension A.** The earlier draft hashed raw file bytes — Codex outside-voice surfaced that LF/CRLF + trailing-newline differences across editors fragment the hash for *semantically identical* configs. Pilot data committed under "the same study definition" by two researchers on different OSes would carry different `config_hash` values, fragmenting downstream analyses in S6/S9c/S11. The bytewise primitive answered the wrong question.

S5 hashes the **canonicalized parsed model** instead:

```python
def compute_config_hash(study_path: Path, questions_path: Path) -> str:
    """SHA256 of the canonicalized parsed configs, hex-encoded.

    Computed as ``sha256(study.model_dump_json(by_alias=True) || 0x00 ||
    questions.model_dump_json(by_alias=True))``. Pydantic v2's
    ``model_dump_json`` produces a deterministic JSON serialization with sorted
    keys, normalised value formatting, and stable booleans/numbers — immune to
    YAML whitespace, key ordering, and editor/OS line-ending differences.

    The invariant is **"same study definition means same hash"**, not "same file
    bytes." Two researchers re-saving the same study config through different
    editors (LF vs CRLF, trailing newline, key reordering) get the same hash.
    A semantic edit (different patient_ids, different timepoint, different
    question prompt) produces a different hash. Consumed by S6 columns:
    ``answers.config_hash``, ``events.config_hash``,
    ``arm_assignments.config_hash``.
    """
```

Tested in S5 by:

- `test_compute_config_hash_stable_across_invocations` — same input → same output.
- `test_compute_config_hash_unchanged_for_whitespace_edits` — adding a trailing newline OR converting LF→CRLF on either YAML file does NOT change the hash (the inverse of the prior spec). This is the load-bearing semantic invariant.
- `test_compute_config_hash_unchanged_for_key_reorder` — reordering top-level keys (`schema_version`, `dataset`, `patient_ids`, …) does NOT change the hash.
- `test_compute_config_hash_changes_on_semantic_edit` — adding a patient_id, changing a question prompt, or bumping `time_unit` DOES change the hash.

Consumed in S6.

---

## 5. CLI command surface (Typer)

```
$ uv run ehr-simulator --help
Usage: ehr-simulator [OPTIONS] COMMAND [ARGS]...

Commands:
  serve              Run the FastAPI server via uvicorn.
  validate-config    Validate study_config.yaml + questions.yaml shape.
  validate-adapter   Resolve a study_config.yaml's dataset and try to load it.
  preflight          Walk every (patient_id, timepoint) headlessly to surface issues before a pilot.
  preview            Render a single patient's per-timepoint summary (text + optional HTML).
```

### 5.1 `serve`

Back-compat surface: `ehr-simulator serve [--host HOST] [--port PORT] [--reload]` works exactly like S2. New: `--config STUDY_PATH --questions QUESTIONS_PATH` resolves a study config and wires the appropriate `dataset_loader` into the FastAPI app factory. When `--config` is omitted, the synthetic loader default holds (no behavior change for existing users).

```
$ uv run ehr-simulator serve --config study.yaml --questions questions.yaml
```

Implementation: when `--config` is set, call `app_from_study_config(study_path, questions_path, log_dir=Path("logs"))` (which routes to `build_dataset_loader(study)` from `cli_support.py` AND binds `app.state.study_timepoints = study.timepoints_minutes` per /plan-eng-review issue 1.2) and `uvicorn.run(app, host=..., port=..., reload=...)`. When `--config` is omitted, keep today's `uvicorn.run("ehr_simulator.web.app:app", ...)` string-form invocation (preserves `--reload` watcher behavior); `app.state.study_timepoints` stays unset and routes fall back to `patient_timepoints(dataset, pid)`.

`--reload` + `--config` warns and disables reload (`--reload` only works with the string-form import path). The warning prints to stderr; `--reload` without `--config` continues to work.

### 5.2 `validate-config`

```
$ uv run ehr-simulator validate-config STUDY_PATH QUESTIONS_PATH
```

Calls `load_study_config(study_path)` and `load_questions(questions_path)`. On `ConfigError`, prints the human-readable message naming the file + the offending field path, exits 1. On success, prints `OK: <study_path> (<n_patients> patients, <n_timepoints> timepoints), <questions_path> (<n_questions> questions, schema_version=1)` and exits 0. The exit-code contract is what makes `validate-config` CI-loop-friendly.

### 5.3 `validate-adapter`

```
$ uv run ehr-simulator validate-adapter STUDY_PATH
```

Reads the study config, builds the dataset_loader, runs the loader **once** with `strict=False` (per the GenevaDataset/MimicDataset issue-collection contract) so all issues surface, prints a formatted report:

```
Dataset:    geneva
csv_path:   /mnt/data1/klug/.../preprocessed_features_30012026_154047.csv
params_dir: /mnt/.../logs_30012026_154047/
SCALAR_TS:  19,734,201 rows
ADMISSION:  3,127 rows × ~31 fields/patient
IMAGING:    0 rows (empty by design)
AI_OUTPUT:  0 rows (S7 deferred)
Issues:     2 (run with --strict to fail on first issue)
  geneva: variable foo missing from normalisation_parameters (patient g_001, row 4321)
  geneva: orphan registry variable: bar (patient g_002, row 8112)
```

`--strict` flips to `strict=True` and fails on the first `AdapterError`. The `Issues` line is the natural emitter for the structlog WARNINGs added to `_decode_categorical` and `_read_features_csv`: those WARNINGs surface as bullet points alongside the existing `IngestionIssue` entries, with `event_kind` distinguishing them.

`EHR_SIM_DATA_ROOT` tightening: when `dataset != "synthetic"` AND the study config has no inline `csv_path`/`params_dir`, `validate-adapter` exits 1 with this remediation: `study_config.yaml must specify csv_path and params_dir for non-synthetic datasets. EHR_SIM_DATA_ROOT (optional) restricts paths to a sandbox directory but does not discover files.` (Closes the S3 deferred decision; refined per /plan-eng-review.)

### 5.4 `preflight`

```
$ uv run ehr-simulator preflight STUDY_PATH QUESTIONS_PATH
```

The pilot-smoke command. Behavior:

1. `validate-config` (re-uses the same loader; fails fast on shape errors).
2. `validate-adapter` (resolves loader; fails fast on adapter errors).
3. **For every `(patient_id, t_minutes)` in `study.patient_ids × study.timepoints_minutes`:** filter the loaded dataset's `scalar_ts` and `admission` to that patient + that timepoint slice (the same slicer S2's `slice_to_timepoint` uses; reused, not duplicated). Emit one of:
   - `OK` — at least one row in `scalar_ts` AND `admission` is non-empty for that patient.
   - `WARN: patient X has no scalar_ts data at t=N` — surfaces the empty-expected vs empty-unexpected ambiguity from S2's panel-state taxonomy. Does NOT fail; the simulator handles empty-expected gracefully.
   - `FAIL: patient X not found in dataset` — fatal; the patient_id in study_config does not exist in the resolved dataset.
4. Exit 0 if no FAILs; 1 otherwise.

The headless walk is the mitigation against "first failure mode is a clinician staring at a broken UI mid-session." Per ROADMAP §"Session 5 — Study config + CLI [NEXT]": *"`preflight <study_config.yaml>` walks every patient/timepoint headlessly before a pilot — catches missing data, schema drift, empty timepoints. Without this, the first failure mode is a clinician staring at a broken UI mid-session."*

`preflight` does NOT boot uvicorn or render templates. It exercises the data path only; UI rendering is `preview`'s job.

### 5.5 `preview`

```
$ uv run ehr-simulator preview STUDY_PATH --patient PATIENT_ID [--questions QUESTIONS_PATH] [--html-out DIR]
```

Default behavior — text summary printed to stdout:

```
Patient: synth_001 (dataset=synthetic)
t=0    vitals=5/5  labs=4/4  admission=ok    ai=1 model
t=60   vitals=5/5  labs=0/4  admission=cached ai=1 model   [WARN: no labs at t=60]
t=180  vitals=5/5  labs=4/4  admission=cached ai=1 model
```

With `--html-out DIR` (Decision OPT-IN, optional), additionally writes the rendered HTMX panel HTML for each `(patient_id, timepoint)` to `<DIR>/<patient_id>_t<timepoint_index>.html` using a `TestClient(create_app(...))` wrapper. `--html-out` is for design review and bug reproduction (e.g. attach to a GitHub issue); requires no live server. The implementation reuses `app_from_study_config` from `web/app.py` (no duplicate plumbing), routes to `/patient/{patient_id}/timepoint/{timepoint_index}`, captures `response.text`, writes to disk. Because `app_from_study_config` sets `app.state.study_timepoints` (per issue 1.2), the URL `t_index` resolves to **study** timepoints (not dataset-derived ones), so the filename `<pid>_t<idx>.html` is unambiguous.

`--html-out` does NOT chase static assets or images. The dumped HTML references `/static/...` URLs that are valid only when the server is running; the rendered HTML is for skim-review of structure, not pixel-perfect UI. Documented in the command's Typer `--help` text.

`preview` does not require `questions.yaml`; questions are not rendered into the panel HTML (S9 work). The `--questions` flag is accepted for forward-compat but currently ignored except for an INFO log line so the user knows the file path was registered.

---

## 6. `EHR_SIM_DATA_ROOT` tightening (S3 deferred → S5 lock)

S3 §6 explicitly punted: *"`EHR_SIM_DATA_ROOT` … When unset, the guard is advisory and the adapter trusts caller-provided paths. **S5's `validate-adapter` CLI tightens this to required** by always setting the env var before invoking adapters."*

S5 implementation (resolved per /plan-eng-review issue 1.1, refinement F):

- `cli_support.build_dataset_loader(study)` resolves paths in this order:
  1. If `dataset == "synthetic"` → no path resolution (`load_synthetic` takes no path arguments).
  2. Else if `study.csv_path` and `study.params_dir` are set on the YAML → use them directly (still passed through `_path_traversal_guard` against `EHR_SIM_DATA_ROOT` if set).
  3. Else → raise `ConfigError` with the remediation message: `"study_config.yaml must specify csv_path and params_dir for non-synthetic datasets. EHR_SIM_DATA_ROOT (optional) restricts paths to a sandbox directory but does not discover files."`
- **`EHR_SIM_DATA_ROOT` is the chroot/path-traversal sandbox, not a file-discovery knob.** The env var continues to behave exactly as it does today at `geneva.py:144` / `mimic.py:155` — it scopes `_path_traversal_guard` to a directory. Codex outside-voice surfaced that any "look for short-form filenames under the root" rule (the prior draft of this spec proposed `<root>/preprocessed_features.csv` + `<root>/logs/`) collides with the real Geneva CSV `preprocessed_features_30012026_154047.csv` and the real `logs_30012026_154047/` dir; no such canonical short form exists on disk. Dropped.
- `validate-adapter`, `preflight`, `preview`, `serve --config` all go through `build_dataset_loader`, so the tightening lands once and applies everywhere.
- The adapters themselves (`load_geneva`, `load_mimic`) **still treat `EHR_SIM_DATA_ROOT` as advisory** when called outside the CLI (pytest fixtures, ad-hoc scripts, S6+ programmatic use). The hard requirement lives at the CLI boundary, not at the adapter API. Documented in `mimic.py` and `geneva.py` module docstrings (one-line update in S5 commit 2).

Tested by 3 CLI tests covering (synthetic + no overrides), (non-synthetic + inline overrides), (non-synthetic + no overrides → exit 1 with the remediation message) — see test inventory.

---

## 7. CSP middleware (TODOS.md plan-eng-review on S2 → S5 lock)

`src/ehr_simulator/web/middleware.py`:

```python
"""Content-Security-Policy ASGI middleware.

Mirrors the bare-ASGI shape of :class:`RequestContextMiddleware` so contextvars
remain visible — see ``web/app.py`` module docstring for the rationale. Locks
the CSP for v1.0 open-source release surface (per TODOS.md plan-eng-review on
session-02-thin-ui-synthetic.md). Only ``style-src 'unsafe-inline'`` is
permissive: plotnine emits inline ``<style>`` blocks inside its SVG output.
``script-src`` is ``'self'``-only (verified: zero inline ``<script>`` blocks
and zero ``on*=`` event handlers across ``web/templates/``; htmx's ``hx-*``
attributes are HTML data-attributes processed by the loaded htmx library, not
script, and do not require ``'unsafe-inline'``). Further tighten ``style-src``
via hashed external stylesheet at SVG-render time as a v1.0-prep TODO (see
TODOS.md "Measure inline-SVG payload size at S8 scale ...").
"""

_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; "
    "connect-src 'self'"
)

class CSPMiddleware:
    def __init__(self, app): self.app = app
    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"content-security-policy", _CSP_HEADER_VALUE.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)
```

Wired into `web/app.py` AFTER `RequestContextMiddleware` (FastAPI executes middlewares in reverse-add order; the request-context middleware needs to wrap the route handler closest, the CSP middleware sits outside it as a pure response-header concern):

```python
app.add_middleware(RequestContextMiddleware)  # S2, unchanged
app.add_middleware(CSPMiddleware)             # S5, NEW
```

Locked by 1 parametrized test in `tests/test_csp.py` (per /plan-eng-review tension D, replaces the originally-planned 3 discrete tests; see §9). The remaining `'unsafe-inline'` allowance on `style-src` is the smallest viable for plotnine SVG today; the v1.0 hardening path (hashed external stylesheet) is documented in the module docstring so it does not get lost.

---

## 8. Structlog WARNING for `_decode_categorical` argmax fallback (TODOS.md S3 → S5 lock)

The S3 `_decode_categorical` lenient-mode fallback already emits an `IngestionIssue` when ambiguous (multiple ≥0.5 values in one one-hot group). S5 adds a structlog WARNING beside the `IngestionIssue` so `validate-adapter` and `preflight` can surface it at the CLI boundary:

```python
# inside _decode_categorical, lenient branch, after argmax pick:
log = structlog.get_logger("ehr_simulator")
log.warning(
    "categorical decode fell back to argmax",
    event_kind="ingest.categorical.argmax_fallback",
    dataset=dataset,
    patient_id=patient_id,
    group_name=group.group_name,
    winner_label=winner_label,
    candidate_count=int((group_rows["value"] >= 0.5).sum()),
)
```

Same pattern for `_read_features_csv` unrecognized-source detection:

```python
log.warning(
    "unrecognized source value",
    event_kind="ingest.source.unrecognized",
    dataset=dataset,
    source_value=src,
)
```

Both WARNINGs are additive — the `IngestionIssue` records still flow into `dataset.issues` unchanged. The two TODOS.md items close together because they share infrastructure (one WARNING per anomaly class, both routed through `structlog.get_logger("ehr_simulator")`).

The Geneva-side defensive-issue test (TODOS.md S4 carryover) lands in `tests/test_geneva.py`: it constructs a frame with one `notes` row (mimicking MIMIC vocab drift) against the geneva fixture and asserts (a) the row does not survive `_read_features_csv`, (b) the structlog WARNING fires, (c) the `attrs["unrecognized_sources"]` list contains the issue.

---

## 9. Test inventory (target ≥8 from ROADMAP; final count after /plan-eng-review = ~22 new test functions + 2 carryover edits, identical coverage to the original 27 because §9.A and §9.D parametrize cases that were originally listed as discrete tests)

Numbered to match commits in §11. **Test structure resolved per /plan-eng-review tension D**: negatives that exercise the same Pydantic validator with different fixtures are collapsed via `@pytest.mark.parametrize(..., ids=[...])` so failure output reads `test_study_config_rejects[bad_time_unit]`. CSP header tests are also parametrized (one test, three route shapes). Coverage is identical; test count drops; pytest IDs stay grep-friendly.

### `tests/test_config.py` (Pydantic schemas + loader + hash)

#### Schema-version + loader plumbing (3)

1. **`test_load_study_config_happy_path`** — load `tests/fixtures/study/study_synthetic.yaml`; assert `study.dataset == "synthetic"`, `study.patient_ids == ["synth_001", "synth_002", "synth_003"]`, `study.timepoints_minutes == [0.0, 60.0, 180.0]`.
2. **`test_load_study_config_rejects_missing_schema_version`** — `study_broken_missing_schema_version.yaml` → `ConfigError`; message contains `schema_version` and the file basename.
3. **`test_load_study_config_rejects_wrong_schema_version`** — `study_broken_wrong_schema_version.yaml` (with `schema_version: "2"`) → `ConfigError`; message names both `"1"` (expected) and `"2"` (observed).

#### `StudyConfig` field validation — parametrized (1 parametrized test, 5 cases)

4. **`test_study_config_rejects[ids=...]`** — `@pytest.mark.parametrize("fixture,expected_field_in_message", [...], ids=["unknown_dataset", "bad_time_unit", "duplicate_patient_ids", "unpaired_path_overrides", "synthetic_with_csv_path_forbidden"])`. One assertion shape: `ConfigError` raised, message names the offending field. Replaces the originally-discrete tests #4-#7. Per /plan-eng-review tension D.

5. **`test_study_config_timepoints_minutes_property`** — `time_unit: hours, timepoints: [0, 1, 24]` → `timepoints_minutes == [0.0, 60.0, 1440.0]`. (Different shape, kept discrete.)

#### `StudyConfig` extra="forbid" + path resolution (added by /plan-eng-review)

6. **`test_study_config_extra_forbid_rejects_unknown_keys`** — YAML with a top-level `unknown_field: 1` → `ConfigError` naming the rejected key. Locks the §3 claim that future field additions are routed through `schema_version` bumps. Per gap 3.5.
7. **`test_load_study_config_resolves_relative_paths_against_yaml_dir`** — write a study config under `tmp_path/study.yaml` with `csv_path: data/foo.csv` + `params_dir: data/`; create `tmp_path/data/foo.csv`; assert the loaded `study.csv_path == tmp_path / "data" / "foo.csv"` (resolved). Locks /plan-eng-review issue 2.2.

#### `Questions` schema (parametrized for negatives, discrete for happy path)

8. **`test_load_questions_happy_path_all_5_primitives`** — load fixture; assert 7 questions present, each parses to the right discriminated-union variant (`LikertQuestion`, `CategoricalQuestion`, `MultiSelectQuestion`, `ProbabilityQuestion`, `FreeTextQuestion`).
9. **`test_questions_rejects[ids=...]`** — `@pytest.mark.parametrize` over `["categorical_no_options", "likert_no_scale", "duplicate_question_id", "duplicate_options", "unknown_response_type", "question_id_with_uppercase", "question_id_with_space", "likert_scale_min_eq_max"]`. Includes:
   - The 4 original broken-fixture cases.
   - `duplicate_options` per /plan-eng-review issue 2.3 (new fixture `questions_broken_duplicate_options.yaml`).
   - `unknown_response_type` (inline YAML with `response_type: gut-feeling`).
   - `question_id_with_uppercase` and `question_id_with_space` per /plan-eng-review gap 3.3 (regex `^[a-z0-9_]+$`).
   - `likert_scale_min_eq_max` per /plan-eng-review gap 3.4 (validator `scale_min < scale_max`).
   Replaces the originally-discrete tests #10-#13.
10. **`test_questions_extra_forbid_rejects_unknown_keys`** — same gap 3.5 as #6 but for `Questions` model. One discrete test (different model).

#### `compute_config_hash` — parametrized for invariance (1 parametrized + 1 discrete)

11. **`test_compute_config_hash_invariant_under[ids=...]`** — `@pytest.mark.parametrize` over `["whitespace_trailing_newline", "whitespace_lf_to_crlf", "yaml_key_reorder"]`. For each: write two YAMLs that differ only in the listed dimension, assert `compute_config_hash(a, q) == compute_config_hash(b, q)`. Locks /plan-eng-review tension A — the canonical-model hash semantics.
12. **`test_compute_config_hash_changes_on[ids=...]`** — `@pytest.mark.parametrize` over `["added_patient_id", "different_timepoint", "edited_question_prompt", "different_dataset"]`. For each: assert hash changes. Locks the inverse: semantic edits DO change the hash.

### `tests/test_cli.py` (Typer `CliRunner`)

13. **`test_cli_serve_invokes_uvicorn_default`** — carryover from S2's monkeypatch test, ported to the Typer entry point. Same assertions: app string, host, port, reload.
14. **`test_cli_serve_with_config_routes_to_app_factory`** — `serve --config tests/fixtures/study/study_synthetic.yaml --questions tests/fixtures/study/questions.yaml`; monkeypatch `uvicorn.run`; assert it received a FastAPI app instance (not the import-string form). Boots the app via the in-test path.
15. **`test_cli_serve_reload_with_config_warns_and_disables`** — `serve --reload --config X --questions Y`; monkeypatch `uvicorn.run`; assert (a) `uvicorn.run` is called with `reload=False`, (b) stderr contains `"--reload disabled when --config is set"`. Per /plan-eng-review gap 3.2.
16. **`test_cli_validate_config_happy_path_exits_0`** — runs against the synthetic + questions fixtures; exit 0; stdout contains the OK message + counts.
17. **`test_cli_validate_config_bad_shape_exits_1`** — runs against `study_broken_missing_schema_version.yaml`; exit 1; stderr names the offending field.
18. **`test_cli_validate_adapter_synthetic_exits_0`** — runs against `study_synthetic.yaml`; exit 0; stdout contains row counts for all four canonical frames.
19. **`test_cli_validate_adapter_geneva_with_inline_paths`** — runs against `study_geneva.yaml` (paths point at `tests/fixtures/geneva/`); exit 0; stdout contains the dataset name + non-zero `SCALAR_TS` count.
20. **`test_cli_validate_adapter_non_synthetic_no_overrides_exits_1`** — Geneva config WITHOUT inline `csv_path`/`params_dir`; exit 1; stderr remediation message names `csv_path`/`params_dir` and clarifies that `EHR_SIM_DATA_ROOT` does not discover files. Locks /plan-eng-review issue 1.1 + refinement F.
21. **`test_cli_preflight_happy_path_exits_0`** — synthetic study + questions; exit 0; stdout contains one OK line per `(patient, timepoint)` (3×3 = 9 lines).
22. **`test_cli_preflight_warns_on_empty_timepoint`** — patient exists but has no `scalar_ts` rows at one of the study timepoints; exit 0 (WARN, not FAIL); stdout contains `WARN: patient X has no scalar_ts data at t=N`. Per /plan-eng-review gap 3.1.
23. **`test_cli_preflight_unknown_patient_exits_1`** — study config lists `synth_999` (not in dataset); exit 1; stderr `FAIL: patient synth_999 not found in dataset`.
24. **`test_cli_preview_text_summary_and_html_out`** — runs `preview study_synthetic.yaml --patient synth_001`; asserts text summary lines for all 3 timepoints. Then runs again with `--html-out tmp_path / "preview"`; asserts 3 HTML files written; each is non-empty and contains `<svg` (plotnine output) and the patient_id.

### `tests/test_csp.py` (1 parametrized, 3 cases)

25. **`test_csp_header_present[ids=...]`** — `@pytest.mark.parametrize("path,headers,expected_status", [("/", {}, 200), ("/patient/synth_001/timepoint/0", {"HX-Request": "true"}, 200), ("/no-such-route", {}, 404)], ids=["root", "htmx_swap", "error_404"])`. One assertion shape: response has the `Content-Security-Policy` header matching the locked value (exact-string comparison) AND the `script-src` directive does NOT contain `'unsafe-inline'` (per /plan-eng-review issue 2.1). Replaces the originally-discrete tests #25-#27.

### `tests/test_shared.py` (+1 carryover)

26. **`test_decode_categorical_argmax_fallback_emits_warning`** — in lenient mode with a one-hot group having 2 values ≥0.5, assert (a) `IngestionIssue` is emitted (S3 behavior, unchanged), (b) a structlog WARNING with `event_kind="ingest.categorical.argmax_fallback"` is captured. **API: use `structlog.testing.capture_logs()` (NOT pytest's `caplog`)** — structlog events do not flow through stdlib logging unless configured with `wrap_for_formatter`/`ProcessorFormatter`/stdlib handler chain. Per /plan-eng-review note in §3.

### `tests/test_geneva.py` (+1 carryover from S4 TODO)

27. **`test_read_features_csv_emits_issue_for_unrecognized_source_geneva_fixture`** — write a one-row variant of the Geneva fixture CSV with `source = "notes"` (a MIMIC vocab leak); load via `_read_features_csv` against Geneva's `_NON_IMPUTED_SOURCES` known-vocab; assert (a) the row does not appear in survivors, (b) the WARNING fires (capture via `structlog.testing.capture_logs()`), (c) `attrs["unrecognized_sources"]` contains the `IngestionIssue`. Mirror of `test_shared.py` test #7 from S4 but exercising the real Geneva fixture (TODOS.md S4 carryover).

### `tests/test_panels.py` or `tests/test_app.py` (DatasetLike Protocol — added by /plan-eng-review tension B)

28. **`test_dataset_like_protocol_accepts_all_three_dataset_classes`** — assert `isinstance(load_synthetic(), DatasetLike)`, plus the same for `load_geneva(...)` against the Geneva fixture and `load_mimic(...)` against the MIMIC fixture. Uses `runtime_checkable` on the Protocol. Locks the typing generalization that lets `walk_preflight` accept any adapter dataset.

### `tests/test_app.py` (app factory bridge)

29. **`test_app_from_study_config_synthetic_renders_synth_001`** — call `app_from_study_config(study_synthetic_yaml, questions_yaml, log_dir=tmp_log_dir)`; build a `TestClient`; assert `GET /patient/synth_001/timepoint/0` returns 200 with the patient summary card. End-to-end S5 plumbing test.
30. **`test_app_from_study_config_t_index_resolves_to_study_timepoints`** — REGRESSION (per /plan-eng-review issue 1.2). Build a synthetic dataset whose `t_minutes` distinct values are `{0, 60, 120, 180, 240}`, then load a study config declaring `timepoints: [0, 180]`. Assert `app.state.study_timepoints == [0.0, 180.0]` AND `GET /patient/synth_001/timepoint/1` slices to `t_minutes <= 180.0` (NOT to dataset's t=60.0 which would be the dataset-derived ordinal 1). Locks the silent study-validity bug. The `serve` no-config path keeps falling back to `patient_timepoints(dataset, pid)` (same regression test, separate case).

**Total: ~22 new test functions + 2 carryover edits (parametrized cases push the *case count* to ~38, identical coverage to the original 27-discrete plan plus the gaps surfaced by /plan-eng-review).** ROADMAP bar (≥8) cleared by 4×. Project total after S5: **110 + ~22 = ~132 test functions** (case count 110 + 38 = 148; pytest counts cases). Excludes `e2e` and `real_data` markers (`uv run pytest -m e2e` and `uv run pytest -m real_data` respectively).

---

## 10. CI changes (`.github/workflows/ci.yml`)

Add **after** the existing `MIMIC fixture sidecar drift check` step:

```yaml
- name: CLI smoke
  run: |
    uv run ehr-simulator validate-config \
      tests/fixtures/study/study_synthetic.yaml \
      tests/fixtures/study/questions.yaml
    uv run ehr-simulator validate-adapter \
      tests/fixtures/study/study_synthetic.yaml
    uv run ehr-simulator preflight \
      tests/fixtures/study/study_synthetic.yaml \
      tests/fixtures/study/questions.yaml
    uv run ehr-simulator preview \
      tests/fixtures/study/study_synthetic.yaml \
      --patient synth_001 \
      --html-out /tmp/preview_smoke
    test -s /tmp/preview_smoke/synth_001_t0.html
```

The `preview` smoke step (per /plan-eng-review task 8) catches the broken-html-out failure mode that pytest's TestClient path can't see — `uv run ehr-simulator preview ...` exercises the real entry point post-`uv sync`, and `test -s` asserts the file exists and is non-empty. If the rendered HTML is missing or empty, CI fails before a clinician hits the same path locally.

Runs on both Python 3.11 and 3.12 (matrix-inherited). Each command exits 0 on a green main; failure surfaces immediately (the same paths are exercised by pytest, but the smoke step asserts the installed entry point works post-`uv sync` — a different failure mode than module-level tests).

The S3 `Data-contract drift check`, S4 `MIMIC fixture sidecar drift check`, and the existing real-data smoke job are unchanged. The Playwright E2E job from S2 is unchanged.

---

## 11. Commit discipline (target ~6 commits, ~1.5–2 days)

| # | Commit | Files |
|---|---|---|
| 1 | `session-05 commit 1: pydantic config models + loader + fixtures` | `pyproject.toml` (+typer +pydantic +pyyaml +types-pyyaml); `src/ehr_simulator/config/{__init__.py,study.py,questions.py,exceptions.py,loader.py}`; `tests/fixtures/study/{study_synthetic,study_geneva,study_mimic,questions,study_broken_*,questions_broken_*}.yaml`; `tests/conftest.py` (+`study_fixture_dir`); `tests/test_config.py` (tests #1-#14). All other code unchanged; CLI still on argparse; tests #1-#14 green. |
| 2 | `session-05 commit 2: typer CLI + cli_support + EHR_SIM_DATA_ROOT tightening + Protocol + study_timepoints binding` | `src/ehr_simulator/cli.py` (REWRITTEN); `src/ehr_simulator/cli_support.py` (NEW); `src/ehr_simulator/web/app.py` (+`app_from_study_config`, +`app.state.study_timepoints` binding per /plan-eng-review issue 1.2); `src/ehr_simulator/web/routes.py` (read `app.state.study_timepoints` when set, fall back to `patient_timepoints(dataset, pid)`); `src/ehr_simulator/web/panels.py` (+`DatasetLike` Protocol per /plan-eng-review tension B; widen `slice_to_timepoint` and `patient_timepoints` signatures); `tests/test_cli.py` (REWRITTEN to Typer `CliRunner`; tests #13-#24 including #20 EHR_SIM_DATA_ROOT, #15 reload+config warning, #22 preflight WARN); `tests/test_panels.py` (+test #28 DatasetLike Protocol satisfies all 3 dataset classes). All previous tests stay green. |
| 3 | `session-05 commit 3: CSP middleware (script-src 'self'-only)` | `src/ehr_simulator/web/middleware.py` (NEW; per /plan-eng-review issue 2.1, no `script-src 'unsafe-inline'`); `src/ehr_simulator/web/app.py` (+`app.add_middleware(CSPMiddleware)`); `tests/test_csp.py` (test #25, parametrized 3 cases per /plan-eng-review tension D). |
| 4 | `session-05 commit 4: structlog WARNINGs in _shared.py + Geneva defensive-issue test` | `src/ehr_simulator/ingestion/_shared.py` (WARNING in `_decode_categorical` argmax fallback + WARNING in `_read_features_csv` unrecognized-source); `tests/test_shared.py` (+test #26, uses `structlog.testing.capture_logs()`); `tests/test_geneva.py` (+test #27, also uses `structlog.testing.capture_logs()`). Closes 2 TODOS.md items. |
| 5 | `session-05 commit 5: app_from_study_config end-to-end + study_timepoints regression + CI smoke step` | `tests/test_app.py` (NEW; tests #29 happy-path, #30 study-timepoints regression per /plan-eng-review issue 1.2); `.github/workflows/ci.yml` (+CLI smoke step including `preview` per task 8). |
| 6 | `session-05 commit 6: docs + final polish` | TODOS.md (strike out the 4 closed items + add the 3 new TODOs surfaced by /plan-eng-review: slice_to_timepoint perf SLA at S8, config_hash callers if v2 changes canonical JSON, CSP `style-src` further tightening at v1.0); ruff/format pass. |

If commit 2 trends past ~500 lines (likely; CLI + cli_support + Protocol + study_timepoints binding + 12 tests), split into 2a (`serve` + `validate-config` + `validate-adapter` + DatasetLike Protocol, tests #13-#20 + #28) and 2b (`preflight` + `preview` + study_timepoints binding, tests #21-#24).

---

## 12. Acceptance criteria (how you know S5 is done)

Every item is a check a reviewer can run.

- [ ] `uv sync` clean.
- [ ] `uv run pytest` green; **~22 new test functions** (parametrized cases bring case count to ~38) + **2 carryover edits**; total ~132 test functions / 148 cases across S1+S2+S3+S4+S5 (excluding `e2e` and `real_data` markers). Coverage is identical to the original 27-discrete plan; the count drop reflects parametrization per /plan-eng-review tension D, not lost coverage.
- [ ] All previous tests stay green throughout (110 from S1-S4 unchanged in semantics; the 2 modified files in S4 carryover are extensions, not rewrites; `web/panels.py` widening to `DatasetLike` Protocol is structurally compatible — `SyntheticDataset` already satisfies it).
- [ ] `uv run ehr-simulator validate-config tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml` exits 0.
- [ ] `uv run ehr-simulator validate-config tests/fixtures/study/study_broken_missing_schema_version.yaml tests/fixtures/study/questions.yaml` exits 1 with a remediation message naming `schema_version`.
- [ ] `uv run ehr-simulator validate-adapter tests/fixtures/study/study_synthetic.yaml` exits 0 with a 4-frame row count summary.
- [ ] `EHR_SIM_DATA_ROOT=/mnt/data1/klug/datasets/opsum uv run ehr-simulator validate-adapter <a-geneva-config-with-inline-paths-under-the-root>.yaml` exits 0 (env var sandboxes the inline paths via `_path_traversal_guard`; it does not discover files).
- [ ] `uv run ehr-simulator validate-adapter <a-geneva-config-without-inline-paths>.yaml` exits 1 with the remediation message naming `csv_path`/`params_dir` (non-synthetic datasets must specify inline paths).
- [ ] `uv run ehr-simulator preflight tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml` exits 0; stdout has 9 OK lines (3 patients × 3 timepoints).
- [ ] `uv run ehr-simulator preview tests/fixtures/study/study_synthetic.yaml --patient synth_001 --html-out /tmp/preview` writes 3 non-empty HTML files containing `<svg`.
- [ ] `uv run ehr-simulator serve` (no `--config`) boots the FastAPI app on synthetic data — back-compat with S2.
- [ ] `uv run ehr-simulator serve --config tests/fixtures/study/study_synthetic.yaml --questions tests/fixtures/study/questions.yaml` boots the same app via the config-driven path.
- [ ] `curl -I http://localhost:8000/` shows the `Content-Security-Policy` header on a live server.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI passes on a PR opened against `main`, including the `CLI smoke` step on both Python 3.11 and 3.12.
- [ ] `uv run pytest -m real_data` still exits 0 (S3 + S4 real-data smokes unchanged).
- [ ] `uv run pytest -m e2e` still exits 0 (S2 Playwright walk unchanged; CSP header does not break the keyboard-walk).
- [ ] All four committed `tests/fixtures/study/study_*.yaml` happy-path files load via `load_study_config(...)` without errors.
- [ ] `compute_config_hash(study_synthetic.yaml, questions.yaml)` returns the same hex digest in two consecutive invocations (consumed in S6 as `config_hash` for `answers`/`events`/`arm_assignments` columns — locked here so S6 has a contract to import).
- [ ] `compute_config_hash` returns the **same** hex digest after converting either YAML's line endings from LF to CRLF, after adding a trailing newline, or after reordering top-level YAML keys. Returns a **different** hex digest after any semantic edit (added patient_id, edited prompt, changed timepoint). Locks /plan-eng-review tension A.
- [ ] `app_from_study_config(study_synthetic_yaml, questions_yaml, log_dir=...)` sets `app.state.study_timepoints` to the study's `timepoints_minutes` list, and the resulting app's `GET /patient/<pid>/timepoint/<i>` slices to `t_minutes <= study.timepoints_minutes[i]`, NOT to the dataset's i-th distinct `t_minutes` value. Locks /plan-eng-review issue 1.2.
- [ ] `serve` without `--config` (synthetic-only path) does not set `app.state.study_timepoints`; routes fall back to `patient_timepoints(dataset, pid)` — confirms the no-config path is unchanged from S2.
- [ ] `web/panels.py::DatasetLike` is `runtime_checkable` and `isinstance(load_synthetic(), DatasetLike)` returns `True`; same for `load_geneva(...)` and `load_mimic(...)` against their fixtures. Locks /plan-eng-review tension B.
- [ ] `curl -I http://localhost:8000/` shows the `Content-Security-Policy` header AND `script-src 'self'` (no `'unsafe-inline'` on `script-src` — only on `style-src`). Locks /plan-eng-review issue 2.1.
- [ ] `uv run ehr-simulator validate-config tests/fixtures/study/study_geneva.yaml tests/fixtures/study/questions.yaml` exits 0 even when run from outside the repo root, because `csv_path` and `params_dir` resolve relative to the YAML directory. Locks /plan-eng-review issue 2.2.

---

## 13. Conventions

- `from __future__ import annotations` at the top of every new module.
- Module docstrings on every new module (`config/{study,questions,loader,exceptions}.py`, `cli_support.py`, `web/middleware.py`, `tests/test_{config,csp,app}.py`).
- Type hints on every public function. `StudyConfig` and `Questions` use Pydantic v2 idioms (`model_config = ConfigDict(extra="forbid")`).
- No comments that restate code (per repo `CLAUDE.md`). Comments only on the `'unsafe-inline'` allowance in `middleware.py` (load-bearing security note).
- Test names: `test_<subject>_<expected_behavior>`. Mirror S3/S4 naming.
- Inline `pd.DataFrame({...})` in unit tests; YAML fixtures for config tests so the human-readable wire format is the test surface.
- `typer.testing.CliRunner` (not Click's) for all CLI tests except `test_cli_serve_invokes_uvicorn_default` (carryover monkeypatch pattern preserved).
- Schema-version field is a string `"1"`, not an int — locked in YAML fixtures and Pydantic models.
- `Path` (not `str`) for every filesystem-bound argument across `cli.py`, `cli_support.py`, `loader.py`. Typer auto-converts via `pathlib.Path` parameter typing.

---

## 14. Open decisions deferred to later sessions

- **`clinician_name` login flow** — deferred to S6 (alongside the `clinicians` table). S5's `study_config.yaml` does not enumerate clinicians; identity capture is a runtime concern handled at the `/login` endpoint S6 introduces. `study_config.yaml` is per-study, not per-clinician.
- **Backup cadence (D10)** — landed in S6 per ROADMAP §"Session 6 — SQLite persistence" → "Backup hook (per TODOS.md, /plan-eng-review residual): wire nightly copy + post-session CSV export into S6 boot." S5 does not include backup scaffolding because S5 has no SQLite layer to copy.
- **CSP nonce-based hardening** — S5 ships `'unsafe-inline'` for `style-src` and `script-src` because plotnine SVG + htmx attributes need it today. Tightening to nonce + per-response random salts is a v1.0-prep task tied to the S8 inline-SVG payload measurement (TODOS.md S2 carryover). Revisit when `hx-swap-oob` per-chart streaming lands.
- **Question gating fields** (`required: bool`, `prompt_context: str`, `panel_context: str`) — deferred to S9b. Adding them in S5 with no consumer locks a contract we cannot test.
- **Phase 2 randomization in `study_config.yaml`** — deferred to S11. The schema is additive-friendly under `extra="forbid"` + `schema_version` bump; S11 will land `schema_version: "2"` (or use a dedicated `arm_config` field on v1).
- **`schema_version: "2"` migration path (per /plan-eng-review tension E).** S5 pins `schema_version: Literal["1"]` with `extra="forbid"` to make every field addition a deliberate bump. When v2 lands (S11 or later), the migration contract is:
  1. `StudyConfig` and `Questions` become discriminated unions on `schema_version` (`StudyConfigV1 | StudyConfigV2`, both with their own `Literal[...]` discriminator).
  2. `loader.load_study_config(path)` detects v1 configs and applies an in-memory upgrade — v1's required fields map directly to v2's required fields; v2-only fields are filled with documented defaults that preserve v1 behavior. The upgraded model carries `study.upgraded_from = "1"` for telemetry so S6 columns can record that a given pilot row was authored under v1 but stored under v2 semantics.
  3. If an automatic upgrade is impossible (e.g., a breaking semantic change), `load_study_config` emits a `ConfigError` whose remediation message points at a future `ehr-simulator migrate-config <study.yaml>` CLI command (S11+).
  4. Pilot data committed under v1 remains valid forever via `study.upgraded_from`. Researchers do not need to re-author existing `study.yaml` files when v2 ships.

  This paragraph is the contract S11 inherits. S5 ships zero migration code (there is no v2 yet); the load-bearing decision is the discriminated-union shape on `schema_version`, which is already what Pydantic v2's `Field(discriminator=...)` would express.
- **`export-answers` CLI command** — deferred to S9c per ROADMAP. The CSV-injection guard + pseudonymization + multi-select pipe-encoding land in S9c, not here.
- **`Question.options` ordering for randomized presentation** — deferred to S9. S5 preserves insertion order from the YAML; S9 may add an `options_shuffle: bool` flag.
- **Multilingual question prompts (i18n)** — punted per `TODOS.md` "Full i18n" item.

---

## 15. What Session 5 does NOT lock

- SQLite schema, migrations runner, `clinicians`/`sessions`/`answers`/`events`/`arm_assignments`/`ingestion_issues`/`schema_migrations` tables — all S6.
- Backup cadence — S6.
- AI predictions adapter for either dataset — S7 (Geneva only) / never (MIMIC, no upstream pkl).
- Real-data UI experience under perf load (5 panels × 24+ timepoints on Geneva) — S8.
- Answer capture, question gating, CSV export — S9a/b/c.
- Divergence view — S10.
- Phase-2 arm randomization — S11.
- Polished v1.0 release notebook + README quickstart — S12.
- DICOM rendering — punted (TODOS.md).
- Mobile/tablet layouts — punted (TODOS.md).
- Any tightening of CSP beyond `'unsafe-inline'` for inline SVG + htmx (revisit at S8 with payload measurement).

---

## 16. What already exists (carried into S5)

- **`src/ehr_simulator/cli.py`** — argparse skeleton with one `serve` subcommand. **Will be rewritten to Typer in commit 2.** The `main(argv=None) -> None` signature is preserved so `tests/test_cli.py`'s monkeypatch idiom carries over for `serve`.
- **`src/ehr_simulator/web/app.py`** — `create_app(*, log_dir, dataset_loader=load_synthetic) -> FastAPI`. The factory is already parameterized per S2 D1; S5 adds `app_from_study_config(...)` as a sibling constructor that reuses the factory unchanged.
- **`src/ehr_simulator/web/routes.py`** — patient/timepoint slicing routes. **Modified in S5 (per /plan-eng-review issue 1.2):** one-line addition reads `app.state.study_timepoints` when set and uses it for the t_index lookup; falls back to `patient_timepoints(dataset, pid)` otherwise. `--html-out` reuses the modified handlers via `TestClient`.
- **`src/ehr_simulator/web/panels.py` + `slice_to_timepoint`** — S2's panel-state slicer. **Modified in S5 (per /plan-eng-review tension B):** `dataset` parameter retypes from `SyntheticDataset` to a `DatasetLike` Protocol so `walk_preflight` can compile against Geneva/MIMIC. Behavior unchanged — the three adapter classes already satisfy the Protocol structurally. Reused by `walk_preflight` and `render_preview` so the panel-state taxonomy is computed in one place across CLI + web.
- **`src/ehr_simulator/logging.py`** — structlog pipeline + 8 mandatory ContextVars (`request_id`, `clinician_id`, `patient_id`, `timepoint`, `timepoint_index`, `event_kind`, `chrome`, `arm`). Reused by the new `_decode_categorical`/`_read_features_csv` WARNINGs (no new fields). The CLI commands bind a `request_id` via `new_request_id()` + `bind_request_context(event_kind=f"cli.{command_name}", clinician_id=None)` so CLI logs join the same JSONL stream as web logs.
- **`src/ehr_simulator/ingestion/_shared.py`** — `_decode_categorical`, `_read_features_csv`, `_path_traversal_guard` all unchanged in semantics; S5 adds two structlog WARNINGs alongside the existing `IngestionIssue` emissions.
- **`src/ehr_simulator/ingestion/{geneva,mimic}.py`** — adapter signatures unchanged. The adapter API still treats `EHR_SIM_DATA_ROOT` as advisory; tightening lives at the CLI boundary in `cli_support.build_dataset_loader`.
- **`tests/conftest.py`** — `dataset`, `tmp_log_dir`, `client`, `geneva_fixture_dir`, `mimic_fixture_dir` fixtures all reused; +`study_fixture_dir` is the only addition.
- **`pyproject.toml` `[tool.pytest.ini_options]`** — `addopts` + `markers` unchanged.
- **`scripts/gen_data_contract.py` + `docs/data-contract.md`** — both unchanged in S5; `canonical.py` is untouched.
- **`.github/workflows/ci.yml`** — extended with the `CLI smoke` step. The S3 `Data-contract drift check` and S4 `MIMIC fixture sidecar drift check` steps still run, still pass.
- **`specs/session-{01,02,03,04}-*.md`** — structural template for this spec; S2 deferred items (CSP), S3 deferred items (`EHR_SIM_DATA_ROOT` tightening, `_decode_categorical` WARNING), S4 deferred items (Geneva `_read_features_csv` defensive-issue test) all close in S5 per the cross-references above.

---

## 17. Verification (end-to-end)

Run after the implementation lands to confirm the spec matches reality:

1. **Pytest end-to-end:**
   ```
   uv run pytest                # 139 green
   uv run pytest -m e2e         # S2 Playwright walk green; CSP header doesn't break it
   uv run pytest -m real_data   # S3+S4 real-data smokes green
   ```

2. **CLI smoke (the same commands CI runs):**
   ```
   uv run ehr-simulator validate-config tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml
   uv run ehr-simulator validate-adapter tests/fixtures/study/study_synthetic.yaml
   uv run ehr-simulator preflight tests/fixtures/study/study_synthetic.yaml tests/fixtures/study/questions.yaml
   uv run ehr-simulator preview tests/fixtures/study/study_synthetic.yaml --patient synth_001 --html-out /tmp/s5_preview
   ls /tmp/s5_preview     # 3 .html files
   ```

3. **`EHR_SIM_DATA_ROOT` tightening sanity check:**
   ```
   # Geneva config WITHOUT inline paths → exit 1 (env var does not discover files).
   uv run ehr-simulator validate-adapter <a-geneva-config-without-inline-paths>.yaml
   #   → exit 1, remediation message names csv_path/params_dir.

   # Geneva config WITH inline paths under the root → env var sandboxes them via _path_traversal_guard.
   EHR_SIM_DATA_ROOT=/mnt/data1/klug/datasets/opsum \
     uv run ehr-simulator validate-adapter <a-geneva-config-with-inline-paths>.yaml
   #   → exit 0 (against the real Geneva CSV).
   ```

4. **CSP header on live server:**
   ```
   uv run ehr-simulator serve &
   curl -sI http://localhost:8000/ | grep -i content-security-policy
   #   → exactly one CSP header line matching the locked value
   curl -sI http://localhost:8000/no-such-route | grep -i content-security-policy
   #   → CSP also present on 404
   kill %1
   ```

5. **Structlog WARNING surfaces in `validate-adapter` output:**
   ```
   uv run ehr-simulator validate-adapter \
     <a-geneva-config-pointing-at-a-fixture-with-an-ambiguous-categorical-row>.yaml
   #   → stdout reports the IngestionIssue + the WARNING; both visible.
   ```

6. **`compute_config_hash` round-trip:**
   ```
   uv run python -c "
   from pathlib import Path
   from ehr_simulator.config import compute_config_hash
   h1 = compute_config_hash(Path('tests/fixtures/study/study_synthetic.yaml'),
                            Path('tests/fixtures/study/questions.yaml'))
   h2 = compute_config_hash(Path('tests/fixtures/study/study_synthetic.yaml'),
                            Path('tests/fixtures/study/questions.yaml'))
   assert h1 == h2 and len(h1) == 64
   print(h1)
   "
   ```

If all six verification steps pass on a fresh clone post-`uv sync`, S5 is shipped.

---

## 18. Review-driven decisions log

`/plan-eng-review` ran on 2026-05-07 and surfaced 14 issues across architecture, code quality, tests, and an outside-voice second opinion (Codex auth was unavailable; fell back to a Claude adversarial subagent per skill protocol). Every recommendation was accepted with the "complete" option, consistent with the boil-the-lake pattern across this project (see `MEMORY.md` user feedback). All 14 spec edits are folded into the body of this document.

### User-resolved decisions PRIOR to /plan-eng-review (3, 2026-05-07)

| ID | Section | Decision |
|---|---|---|
| U1 | §2 (study_config.yaml) | **Hybrid dataset selector.** Required `dataset` enum + optional inline `csv_path`/`params_dir` overrides. CLI's `validate-adapter` resolves paths via env vars when overrides are absent. Rejected: enum-only (too rigid for multi-pilot deployments), inline-paths-only (loses symbolic dataset name in logs/UI labels). |
| U2 | §7 (CSP) | **CSP middleware lands in S5, not S6.** S5 already touches `web/app.py` and the open-source v1.0 release surface should not be one more deferred TODO. Rejected: punt-to-S6 (couples the CSP test to SQLite session, violating one-concern-per-session). |
| U3 | §5.5 (preview) | **Text summary + optional `--html-out` hybrid.** Default text summary keeps the terminal feedback loop tight; `--html-out` is opt-in for design-review and bug-repro. Rejected: text-only (loses visual verification), html-only (loses quick-glance summary). |

### /plan-eng-review decisions (14, 2026-05-07)

| ID | Section(s) | Decision |
|---|---|---|
| 1.1 | §6, §12 | **Drop `EHR_SIM_DATA_ROOT` short-form path discovery.** Earlier draft proposed `<root>/preprocessed_features.csv` + `<root>/logs/` short forms; verified against `.EXAMPLE_DATA_PATHS` that the real Geneva files are date-stamped (`preprocessed_features_30012026_154047.csv`, `logs_30012026_154047/`) and no canonical short form exists. Now: when `dataset != "synthetic"`, study YAML MUST set `csv_path` + `params_dir`; env var stays as the chroot guard at `_path_traversal_guard`. |
| 1.2 | §1 (deliverables row 10/10b), §5.1, §10, §12 | **Bind `study.timepoints_minutes` into `app.state.study_timepoints` in S5.** `app_from_study_config` sets it; `routes.py::patient_timepoint` reads it when present. Closes the silent study-validity bug where the URL ordinal `t_index` would resolve to dataset-derived timepoints (24-72 on Geneva) instead of study-defined ones (3-12 on a pilot study). +1 regression test #30. |
| 1.3 | §5.5 | **Folded into 1.2.** `preview --html-out`'s `<pid>_t<idx>.html` filenames become unambiguous because `app.state.study_timepoints` makes the t_index → t_minutes mapping deterministic against the study config. One-line note added. |
| 2.1 | §1 (deliverable row 9), §7, §12 | **Drop `script-src 'unsafe-inline'` from CSP.** Verified zero `<script>` blocks and zero `on*=` event handlers across `web/templates/`. htmx's `hx-*` attributes are HTML data-attributes processed by the loaded htmx library (external), not script — they don't require `'unsafe-inline'`. Only `style-src 'unsafe-inline'` remains (plotnine inline `<style>` inside SVG). Tightens v1.0 release surface. |
| 2.2 | §2 | **Resolve relative `csv_path`/`params_dir` against `study_path.parent`, not `Path.cwd()`.** Convention matches Docker Compose, ESLint, Prettier. Lets researchers put `study.yaml` next to its data dir without CWD discipline. +1 test #7. |
| 2.3 | §3 | **`Question.options` duplicates raise (not silent dedupe).** Mirror the `patient_ids` rule. Silent dedupe could collapse a typo (`Yes / yes / No`) and surface as a different error downstream. +1 broken fixture + 1 parametrized test case. |
| 3.1 | §9 | **Add walk_preflight WARN-path test.** Spec defined the `WARN: patient X has no scalar_ts data at t=N` state but no test pinned it. Test #22. |
| 3.2 | §9 | **Add `serve --reload --config X` warns + disables test.** Spec promised the warning at §5.1 but no test pinned the stderr message. Test #15. |
| 3.3 | §9 | **Add `question_id` regex `^[a-z0-9_]+$` rejection tests.** Spec claimed the constraint at §3 (cell-injection guard for S9c) but no test pinned it. Folded into the `test_questions_rejects` parametrized test (cases `question_id_with_uppercase`, `question_id_with_space`). |
| 3.4 | §9 | **Add `LikertQuestion.scale_min < scale_max` test.** Spec claimed the validator at §3 but no test pinned it. Folded into the same parametrized test (case `likert_scale_min_eq_max`). |
| 3.5 | §9 | **Add `extra="forbid"` rejection tests on both `StudyConfig` and `Questions`.** Spec claimed the schema-versioning policy at §3 but no test pinned that unknown top-level fields raise. Tests #6 and #10 (one per model — different validators, kept discrete). |
| structlog API | §9 (tests #26, #27) | **Pin `structlog.testing.capture_logs()`, NOT pytest `caplog`.** Spec mentioned both inconsistently. structlog events do not flow through stdlib logging unless explicitly chained; `caplog` would silently miss them. |
| CI smoke | §10 | **Add `preview --html-out` to the CI smoke step.** Catches the broken-html-out failure mode that pytest's TestClient path can't see. |
| Tension A | §4, §9 (#11, #12), §12 | **`compute_config_hash` hashes the canonicalized parsed model, not raw bytes.** Earlier draft framed the bytewise hash as a "research-replication invariant"; outside voice surfaced that LF/CRLF + trailing-newline differences across editors fragment the hash for *semantically identical* configs. New invariant: "same study definition means same hash" via `model.model_dump_json(by_alias=True)` over Pydantic v2's deterministic JSON serialization. S6/S9c/S11 inherit a more useful contract. |
| Tension B | §1 (deliverable row 10c), §10, §12 | **Generalize `slice_to_timepoint` typing via `DatasetLike` Protocol.** `web/panels.py:21` currently types `dataset: SyntheticDataset`, which prevents `walk_preflight` from compiling against Geneva/MIMIC. New: `DatasetLike(Protocol)` with the four canonical-frame attrs; the three adapter classes satisfy structurally. +1 test #28 with `runtime_checkable`. |
| Tension C | (no spec change) | **Keep `serve --config` in S5.** Outside voice argued cut it as dead plumbing until S8; user kept it consistent with the boil-the-lake pattern. `preview --html-out` already uses `app_from_study_config` internally so most helper code stays. |
| Tension D | §9 | **Parametrize negative-fixture tests and CSP route tests.** ~22 test functions / ~38 cases instead of 27 discrete tests. Identical coverage; pytest IDs (`test_study_config_rejects[bad_time_unit]`) stay grep-friendly. |
| Tension E | §14 | **Document the `schema_version: "2"` migration story.** `Literal["1"]` with no Union or migrator means every pre-S11 config breaks the day v2 ships. Added paragraph sketches discriminated-union shape, `study.upgraded_from = "1"` telemetry, and the future `migrate-config` CLI exit hatch. S5 ships zero migration code; the contract S11 inherits is documented now. |
| Refinement F | §6 | **Tighten `EHR_SIM_DATA_ROOT` remediation message.** Refines decision 1.1: env var is the chroot guard ONLY (never names it as a path-discovery knob the user can turn). Final message: "study_config.yaml must specify csv_path and params_dir for non-synthetic datasets. EHR_SIM_DATA_ROOT (optional) restricts paths to a sandbox directory but does not discover files." |

### TODOS.md items closed in S5 (4)

- Schema-version field on `study_config.yaml` / `questions.yaml` (S1 D6 deferred → S5 §2/§3).
- Tighter `EHR_SIM_DATA_ROOT` contract (S3 §"Open decisions deferred" → S5 §6, refined per F).
- Add a Content-Security-Policy header in S5 or S6 (TODOS.md plan-eng-review on S2 → S5 §7, with `script-src 'unsafe-inline'` dropped per 2.1).
- Emit a structlog WARNING when `_decode_categorical` falls back to argmax in lenient mode (TODOS.md plan-eng-review on S3 → S5 §8); same WARNING infrastructure also closes the Geneva-side defensive-issue test for `_read_features_csv` (TODOS.md plan-eng-review on S4 → S5 §8 + test #27).

### TODOs added (3, by /plan-eng-review)

To be appended to TODOS.md in commit 6:

1. **S8: SLA test on Geneva preflight wall time.** With `slice_to_timepoint` generalized via `DatasetLike` Protocol (per tension B), preflight can now compile against Geneva real data. S8 should add a `@pytest.mark.real_data` smoke asserting preflight on a 30-patient × 12-timepoint Geneva pilot subset stays under N seconds. Naive O(P×T×|scalar_ts|) is bounded for pilot subsets but worth measuring before the first real-data session. Depends on: S5 shipped, S8 starting.
2. **S11: migrate config_hash callers if v2 changes the canonical JSON shape.** Per tension E, when `schema_version: "2"` lands, pilot data committed under v1 carries `config_hash` rows whose canonical JSON may need a remap. The S11 spec must document whether v1's `config_hash` values stay valid across the upgrade or whether a `config_hash_v2` column gets added. Depends on: S11 spec authoring.
3. **v1.0 release: tighten CSP `style-src 'unsafe-inline'` via hashed external stylesheet.** S5 dropped `script-src 'unsafe-inline'` (per 2.1); `style-src 'unsafe-inline'` remains because plotnine emits inline `<style>` blocks inside its SVG output. The hardening path is to extract those styles into a hashed external stylesheet at SVG-render time, dropping the last `'unsafe-inline'` allowance. Depends on: v1.0 release prep + S8 inline-SVG payload measurement (existing TODO).
