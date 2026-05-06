# Session 02 — Thin UI on synthetic + structlog

**Goal:** a fresh clone can `uv sync && uv run ehr-simulator serve`, browse to `localhost:8000/patient/synth_001/timepoint/0`, walk the three timepoints with `[`/`]`, and see vitals / labs / admission / imaging / AI panels rendered against `load_synthetic()`. The build is the artifact reviewed in the first embedded-neurologist chrome A/B session (dense vs Epic-style on `synth_001`).

**Out of scope (later sessions):** answer capture (S9a), question gating (S9b), CSV export (S9c), SQLite persistence (S6), MIMIC adapter (S4), Geneva adapter + AI predictions (S3, S7), Pydantic-validated `study_config.yaml` / `questions.yaml` + Typer CLI (S5), login / clinician identification (S5), arm randomization + AI-visible flag (S11), divergence view (S10), DICOM rendering, Tauri packaging.

---

## Deliverables

| # | Path | Purpose |
|---|---|---|
| 1 | `pyproject.toml` | Add web + logging deps; register `ehr-simulator` script |
| 2 | `.gitignore` | Add `logs/` |
| 3 | `src/ehr_simulator/logging.py` | structlog boot + per-request middleware |
| 4 | `src/ehr_simulator/cli.py` | `argparse` shim that runs uvicorn (Typer arrives S5) |
| 5 | `src/ehr_simulator/web/__init__.py` | Web subpackage marker |
| 6 | `src/ehr_simulator/web/app.py` | FastAPI app factory, lifespan, middleware, mounts |
| 7 | `src/ehr_simulator/web/routes.py` | `GET /`, `GET /patient/{id}/timepoint/{t}` |
| 8 | `src/ehr_simulator/web/panels.py` | `slice_to_timepoint`, panel-state detection, panel renderers |
| 9 | `src/ehr_simulator/web/charts.py` | `render_timeline_svg` (plotnine → inline SVG) |
| 10 | `src/ehr_simulator/web/templates/base.html` | `<head>`, htmx + keyboard.js + theme.css links, swap target wrapper |
| 11 | `src/ehr_simulator/web/templates/index.html` | Patient list + chrome links |
| 12 | `src/ehr_simulator/web/templates/_chrome_dense.html` | Multi-panel info-dense layout |
| 13 | `src/ehr_simulator/web/templates/_chrome_epic.html` | Tabbed Epic-style layout |
| 14 | `src/ehr_simulator/web/templates/_summary_card.html` | Always-visible patient/timepoint header |
| 15 | `src/ehr_simulator/web/templates/_panel_vitals.html` | Vitals panel (chart + a11y table) |
| 16 | `src/ehr_simulator/web/templates/_panel_labs.html` | Labs panel |
| 17 | `src/ehr_simulator/web/templates/_panel_admission.html` | Admission panel |
| 18 | `src/ehr_simulator/web/templates/_panel_imaging.html` | Imaging panel |
| 19 | `src/ehr_simulator/web/templates/_panel_ai.html` | AI panel (renders `output_json`) |
| 20 | `src/ehr_simulator/web/static/htmx.min.js` | Vendored htmx.org release (pinned) |
| 21 | `src/ehr_simulator/web/static/keyboard.js` | `[`/`]`/`?` shortcuts, focus-aware |
| 22 | `src/ehr_simulator/web/static/theme.css` | Empty by default; AA-contrast tokens; adopters override |
| 23 | `tests/conftest.py` | Shared `TestClient` + `dataset` + `tmp_log_dir` fixtures |
| 24 | `tests/test_logging.py` | structlog mandatory-fields + daily-rollover assertions |
| 25 | `tests/test_charts.py` | plotnine renderer returns SVG + edge cases |
| 26 | `tests/test_panels.py` | `slice_to_timepoint`, panel-state detection |
| 27 | `tests/test_routes.py` | Index, full-page, HTMX partial, chrome A/B, data-leak regression, fixture-state acceptance, error containment, boundaries |
| 28 | `tests/test_a11y.py` | Every chart has an a11y-fallback table |
| 29 | `tests/test_cli.py` | argparse `serve` subcommand invokes uvicorn with right kwargs |
| 30 | `tests/test_static_assets.py` | sha256 of vendored htmx.min.js matches the pinned constant |
| 31 | `tests/e2e/__init__.py`, `tests/e2e/test_synthetic_walk.py` | Playwright walk via `[`/`]` |
| 32 | `.github/workflows/ci.yml` | Add `playwright install --with-deps chromium` step + `pytest -m e2e` job |

---

## Repo layout after Session 2

```
ehr_simulator/
├── .github/workflows/ci.yml         # +playwright install, +e2e step
├── .gitignore                       # +logs/
├── .python-version
├── pyproject.toml                   # +deps, +scripts entry
├── uv.lock
├── logs/                            # gitignored, created at boot
├── src/
│   └── ehr_simulator/
│       ├── __init__.py
│       ├── cli.py                   # NEW
│       ├── logging.py               # NEW
│       ├── ingestion/               # unchanged from S1
│       │   ├── __init__.py
│       │   ├── canonical.py
│       │   ├── exceptions.py
│       │   └── synthetic.py
│       └── web/                     # NEW subpackage
│           ├── __init__.py
│           ├── app.py
│           ├── routes.py
│           ├── panels.py
│           ├── charts.py
│           ├── static/
│           │   ├── htmx.min.js
│           │   ├── keyboard.js
│           │   └── theme.css
│           └── templates/
│               ├── base.html
│               ├── index.html
│               ├── _chrome_dense.html
│               ├── _chrome_epic.html
│               ├── _summary_card.html
│               ├── _panel_vitals.html
│               ├── _panel_labs.html
│               ├── _panel_admission.html
│               ├── _panel_imaging.html
│               └── _panel_ai.html
└── tests/
    ├── __init__.py
    ├── conftest.py                  # NEW
    ├── test_canonical.py            # unchanged
    ├── test_synthetic.py            # unchanged
    ├── test_logging.py              # NEW
    ├── test_charts.py               # NEW
    ├── test_panels.py               # NEW
    ├── test_routes.py               # NEW
    ├── test_a11y.py                 # NEW
    └── e2e/
        ├── __init__.py
        └── test_synthetic_walk.py   # NEW (@pytest.mark.e2e)
```

---

## 1. `pyproject.toml` additions

Append to `[project] dependencies`:

```toml
"fastapi>=0.115",
"uvicorn[standard]>=0.32",
"jinja2>=3.1",
"plotnine>=0.13",
"structlog>=24.4",
```

Append to `[dependency-groups] dev`:

```toml
"httpx>=0.27",
"pytest-playwright>=0.5",
"beautifulsoup4>=4.12",
"freezegun>=1.5",
```

New `[project.scripts]`:

```toml
[project.scripts]
ehr-simulator = "ehr_simulator.cli:main"
```

New `[tool.pytest.ini_options]` markers (extend the existing block):

```toml
markers = [
    "e2e: end-to-end Playwright tests (require chromium installed)",
]
```

`addopts` becomes `"-n auto --strict-markers -m 'not e2e'"`. The E2E suite runs explicitly via `uv run pytest -m e2e`.

Also add to ruff lint excludes if needed for Playwright fixture style: nothing today; revisit only if ruff complains.

**HTMX is vendored**, not a Python dep. Pin a specific htmx.org release (`htmx.org@2.0.4` at the time of writing); commit `htmx.min.js` directly. Document the version in a comment at the top of `keyboard.js` so a future upgrade is greppable. **The pin is enforced by sha256:** `tests/test_static_assets.py` asserts `hashlib.sha256(static/htmx.min.js).hexdigest() == EXPECTED_HTMX_SHA256`. The constant is updated only when the version is intentionally upgraded; silent file swaps fail CI. (Decision **D15**.)

---

## 2. `.gitignore`

Append:

```
logs/
.pytest_cache/
```

---

## 3. `src/ehr_simulator/logging.py`

```python
from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

import structlog

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_clinician_id_var: ContextVar[str | None] = ContextVar("clinician_id", default=None)
_patient_id_var: ContextVar[str | None] = ContextVar("patient_id", default=None)
_timepoint_var: ContextVar[float | None] = ContextVar("timepoint", default=None)
_timepoint_index_var: ContextVar[int | None] = ContextVar("timepoint_index", default=None)
_event_kind_var: ContextVar[str | None] = ContextVar("event_kind", default=None)
_chrome_var: ContextVar[str | None] = ContextVar("chrome", default=None)
_arm_var: ContextVar[str | None] = ContextVar("arm", default=None)


def setup_logging(log_dir: Path) -> structlog.stdlib.BoundLogger:
    """Boot structlog. Idempotent. Writes JSONL to ``<log_dir>/<date>.jsonl`` + stderr.

    ``log_dir`` is required (no default) so tests pass ``tmp_path`` and the production
    entry point passes ``Path("logs")``. The factory pattern in ``web/app.py`` wires
    the production path; conftest.py overrides it. (Decision **D1**.)
    """
    ...


def bind_request_context(*, request_id: str, patient_id: str | None = None,
                         timepoint: float | None = None,
                         timepoint_index: int | None = None,
                         event_kind: str | None = None,
                         chrome: str | None = None,
                         arm: str | None = None) -> None:
    """Bind per-request fields. ``clinician_id`` stays None until S5; ``arm`` stays None
    until S11. ``chrome`` is bound from the route's query param. (Decision **D4**.)"""
    ...


def new_request_id() -> str:
    return uuid.uuid4().hex
```

**Mandatory bound fields on every record (8 keys):** `request_id`, `clinician_id`, `patient_id`, `timepoint`, `timepoint_index`, `event_kind`, `chrome`, `arm`. Missing fields are emitted as `null`, never absent — keeps the JSONL schema stable for downstream tooling. (Decisions **D4**, **D13**.)

- `timepoint` is bound to `t_minutes` (the real clinical time, study-config-agnostic). `timepoint_index` is the URL ordinal, audit-only. Analysts join on `timepoint`. (Decision **D13**.)
- `chrome` is bound from the route handler's `chrome=dense|epic` query param. The `arm` field is reserved for S11; defaults to `null` in S2.
- `event_kind` follows this rule: when the request has the `HX-Request` header, the per-request log line uses `event_kind="panel.swap"`; otherwise it uses `"page.render"`. On unhandled exception in the handler, the middleware overrides to `"page.error"` regardless of the header. (Decision **D3**.)

**Sinks.** One processor chain, one renderer (`structlog.processors.JSONRenderer`), two stdlib handlers wired into the `structlog`-bound logger:

1. `TimedRotatingFileHandler(filename=str(log_dir / "current.jsonl"), when="midnight", utc=True, backupCount=0)` — rolls at UTC midnight, names rotated files `current.jsonl.YYYY-MM-DD`. The `suffix="%Y-%m-%d"` field is set explicitly so the date format is locked. (Decision **D2**.)
2. `StreamHandler(sys.stderr)` for live debug.

`setup_logging` is idempotent: calling it twice removes prior handlers before reattaching.

---

## 4. `src/ehr_simulator/cli.py`

```python
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="ehr-simulator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if args.cmd == "serve":
        uvicorn.run(
            "ehr_simulator.web.app:app",
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
```

Typer + `validate-config` + `preflight` + `preview` arrive in S5. Today: one subcommand, one job — boot the server.

**Test (D7).** `tests/test_cli.py` monkeypatches `uvicorn.run` to a list-of-calls collector and calls `main(["serve", "--port", "8123"])`; asserts one call with the expected `app`, `host`, `port`, `reload` kwargs. Locks the entry point now so S5's Typer swap can be verified by extending this pattern.

---

## 5. `src/ehr_simulator/web/app.py`

**Parameterized factory** (Decision **D1**):

```python
def create_app(
    *,
    log_dir: Path = Path("logs"),
    dataset_loader: Callable[[], SyntheticDataset] = load_synthetic,
) -> FastAPI:
    ...

app = create_app()  # production entry; uvicorn imports this
```

Tests build their own app via `create_app(log_dir=tmp_path, dataset_loader=fake_loader)`. **Routes never import `app` at module scope** — they read `request.app.state.dataset` instead. This guarantees every test gets an isolated app, an isolated `logs/` directory under `tmp_path`, and a controllable dataset. (Cross-model agreement, outside voice point 1.)

Lifespan event (in order):

1. `setup_logging(log_dir)`.
2. `try: app.state.dataset = dataset_loader()` — validate-once, cache parsed frames. **Per-request slicing is filtered pandas; never re-validate.**
3. `except AdapterError as e:` log `event_kind="app.boot.failed"` with the `e.issues` list, print to stderr `"Synthetic dataset failed validation. Run `uv run pytest tests/test_synthetic.py` to see what's broken."`, raise `SystemExit(1)`. (Decision **D17**.)
4. On success, bind app-boot log record (`event_kind="app.boot"`).

Middleware stack (outermost first):

1. **Request-ID middleware**: generate `request_id`, bind to context vars, attach `X-Request-ID` response header.
2. **Logging middleware**: emit one log record per request after handler returns. Rule (Decision **D3**):
   - `request.headers.get("hx-request")` truthy → `event_kind="panel.swap"`.
   - Else → `event_kind="page.render"`.
   - On exception bubbling out of the handler → `event_kind="page.error"`, re-raise.

Mounts:

- `/static` → `src/ehr_simulator/web/static/` (StaticFiles).
- Templates loaded from `src/ehr_simulator/web/templates/` via `Jinja2Templates`.

Routes registered from `routes.py`.

---

## 6. `src/ehr_simulator/web/routes.py`

Two routes only:

```python
@router.get("/")
async def index(request: Request) -> HTMLResponse:
    """List the three synthetic patients with chrome=dense / chrome=epic links."""

@router.get("/patient/{patient_id}/timepoint/{t_index}")
async def patient_timepoint(
    request: Request,
    patient_id: str,
    t_index: int,
    chrome: Literal["dense", "epic"] = "dense",
) -> HTMLResponse:
    """Render the patient view at timepoint index t_index (0, 1, 2 for synthetic).

    HX-Request header → returns inner partial (summary card + panels).
    No HX-Request → returns full <html> page wrapping the same partial.
    Out-of-range t_index → 404 with HTML body shaped for the swap target.
    Unknown patient_id → 404 with HTML body shaped for the swap target.
    Invalid chrome (not in {"dense","epic"}) → 422 (FastAPI Literal default).
    """
```

`t_index` is the **ordinal position** in the patient's sorted timepoint list (0, 1, 2). The actual `t_minutes` is resolved from `request.app.state.dataset` per request. This decouples the URL from the time unit (a contract S5 will reuse when the study config picks `minutes` vs `hours`).

**Error body shapes (Decisions D6, D10):**

- Out-of-range `t_index` → HTTP 404, body is `<div class="error-flash" role="alert">Timepoint t_index=N out of range (valid: 0…M)</div>`. HTMX's default 4xx behavior is to not swap, so user-visible effect is: keyboard handler stays on the current view (defense in depth — the keyboard handler should already prevent OOB requests; 404 is the regression signal).
- Unknown `patient_id` → HTTP 404, body is `<div class="error-flash" role="alert">Patient '{patient_id}' not found</div>`.
- Invalid `chrome` → HTTP 422 (FastAPI's default Literal-mismatch handler); test asserts the response code only, not the body.

---

## 7. `src/ehr_simulator/web/panels.py` — slicing rule (data ≤ t only)

> **The central architectural constraint.** Even though Geneva's real-data version of this lives in S8, the rule is locked here so it cannot drift later.

**Architectural choke point (Decision D5):** `slice_to_timepoint` is the *only* function in the codebase that reads the unsliced dataset. It computes per-panel state (which requires peeking at `t > t_minutes` for `empty-unexpected` detection) and returns it alongside the sliced frames. **Renderers receive the slice plus the per-panel state label only — they have no path to future data.** The data-locality invariant becomes a structural property of the codebase, not a discipline. (Cross-model agreement, outside voice point 5.)

```python
PanelState = Literal["loading", "empty-expected", "empty-unexpected", "partial", "error"]

@dataclass(frozen=True)
class PatientSlice:
    patient_id: str
    t_minutes: float
    timepoint_index: int
    scalar_ts: pd.DataFrame   # rows where t_minutes <= current
    admission: pd.DataFrame   # patient_id filter only
    imaging: pd.DataFrame     # rows where t_minutes <= current
    ai_output: pd.DataFrame   # rows where t_minutes <= current
    panel_states: dict[str, PanelState]   # keys: vitals, labs, admission, imaging, ai
    panel_errors: dict[str, str | None]   # error_text per panel; None unless state == "error"


def slice_to_timepoint(
    dataset: SyntheticDataset,
    patient_id: str,
    t_minutes: float,
    timepoint_index: int,
) -> PatientSlice:
    """Filter every frame to (patient_id == patient_id) AND (t_minutes <= t_minutes),
    then derive panel states by inspecting the unsliced dataset (the only function
    authorized to do so).

    ADMISSION has no t_minutes column → filter only by patient_id.
    Returns a frozen dataclass; renderers consume sliced frames + state labels only.
    """
```

Implementation: `df[(df.patient_id == pid) & (df.t_minutes <= t)]` for the filtering; the state computation reads `dataset.scalar_ts[(dataset.scalar_ts.patient_id == pid) & (dataset.scalar_ts.t_minutes > t)]` (etc) to test `empty-unexpected`. The structural invariant: the sliced frames returned to the renderer never contain rows with `t_minutes > current_t`. Test #9 (the data-leak regression) is a real HTTP request — not a unit-level shortcut.

Panel-state detection rules (all five states from the roadmap):

| Panel | `loading` | `empty-expected` | `empty-unexpected` | `partial` | `error` |
|---|---|---|---|---|---|
| Vitals | (initial render before HTMX swap) | no rows up to t AND no earlier rows for this patient | rows exist at later t but not yet at current t AND ≥1 earlier timepoint had data for this variable | some variables present, others missing (`synth_002` at t=60 for labs) | uncaught exception in renderer |
| Labs | as Vitals | as Vitals | as Vitals | as Vitals (`synth_002` t=60 hits this) | as Vitals |
| Admission | n/a (always loaded with page) | patient has no ADMISSION rows | n/a (admission is static — every patient has it in synth) | n/a | renderer exception |
| Imaging | as Vitals | `synth_003` (no imaging at all) | t > 0 and earlier imaging existed but current t has none for this patient | imaging exists but report_text is null | renderer exception |
| AI | as Vitals | patient has no AI_OUTPUT rows | any earlier timepoint had AI but current does not (data integrity signal) | output_json parses but is missing expected keys | output_json parse error |

`partial` is **not** a degraded `empty-expected`; it explicitly signals "we have data but it's incomplete," which the chrome layouts render with a different visual treatment. The state-detection unit test (#4) is table-driven over these rules.

**Per-panel error containment (Decision D9).** The route handler wraps each panel renderer call in a try/except. On exception: set `panel_states[panel] = "error"`, set `panel_errors[panel] = repr(exc)`, and continue rendering the other panels. The middleware does **not** see the exception (so the per-request log line stays `event_kind="page.render"` or `"panel.swap"`, not `"page.error"`); the broken panel renders with the error visual treatment instead of taking down the whole request.

---

## 8. `src/ehr_simulator/web/charts.py` — plotnine → SVG

```python
def render_timeline_svg(
    frame: pd.DataFrame,
    variable: str,
    *,
    width: float = 4.0,
    height: float = 1.6,
) -> str:
    """Render a single-variable timeline as inline SVG.

    Uses plotnine ggplot, savefig(format='svg', bbox_inches='tight') via BytesIO.
    Returns the SVG as a UTF-8 string starting with '<?xml' or '<svg'.

    The SVG has data-variable="{variable}" on the root <svg> element so a11y
    fallback tables and CSS hooks can target it without parsing.
    """
```

**No client-side chart JS.** Charts are SVG fragments inlined into the panel templates. HTMX swaps the entire panel partial (chart + a11y table + state metadata) atomically — no per-chart streaming, no progressive loading. A single request, a single DOM swap.

**Edge-case contract (Decision D12):** the renderer is called only when the panel state is `loading | partial`. Defensive shape:

- **Single-row frame** → returns a valid SVG with a single point on the timeline (no degenerate-axis raise). Locks the t=0 happy path.
- **Empty frame** (zero rows) → returns a valid SVG with axes but no data marks. Defends against state-detection bugs cascading into the renderer.
- **Variable name not in `frame['variable'].unique()`** → raises `KeyError`. Loud failure; surfacing the bug is preferable to a silent empty SVG.

Performance note for S2: the synthetic dataset is tiny (3 patients × 3 timepoints × ~10 variables); plotnine render time is dominated by matplotlib import (~150ms cold). At S8 scale (24 timepoints × Geneva density) we'll revisit. Matplotlib is imported eagerly at app boot to take that hit once.

---

## 9. Timepoint Summary Card

`_summary_card.html` — always visible above the chrome region. Fields:

- patient id
- age (from ADMISSION)
- sex
- current `t_minutes` (resolved from `t_index`)
- count of revealed rows across all shapes (one badge per shape)
- "out of range" flash slot (populated when `[` / `]` hit a wall)

The summary card and the panels share **one HTMX swap target** (`<div id="patient-view">`), so a `[` / `]` press swaps both atomically. This is the single-source-of-truth render path; tests assert on this target.

---

## 10. Chrome A/B (`?chrome=dense` vs `?chrome=epic`)

Both chrome layouts render the **same data** through the same panel partials. They differ only in arrangement and density:

- `dense` (default): one scrollable page, all five panels visible simultaneously, compact whitespace, sans-serif at 13px. Optimized for "see everything at once."
- `epic`: tabbed interface (one panel visible at a time), 16px font, generous whitespace, header/sidebar mimicking Epic's chrome conventions. Optimized for "focus on one panel."

`routes.py` reads `?chrome=` and selects `_chrome_dense.html` vs `_chrome_epic.html`. Both extend `base.html` and import the panel partials. The panel partials are chrome-agnostic — they render their own state regardless of which chrome wraps them.

The chrome decision is **also reflected in the URL** (it's a query param, not a cookie) so the embedded-neurologist session can compare them by clicking a single link rather than fiddling with a toggle.

---

## 11. Keyboard shortcuts (`static/keyboard.js`)

```javascript
// keyboard.js — pinned htmx.org@2.0.4
// ] = next timepoint, [ = previous timepoint, ? = show shortcuts overlay
// Shortcuts ignored when focus is in an input/textarea/select/contenteditable.
```

Behavior:

- `]` triggers `htmx.ajax('GET', '/patient/{pid}/timepoint/{t_index+1}?chrome={chrome}', '#patient-view')`.
- `[` is the symmetric `t_index-1`.
- Out-of-range (e.g. `]` at the last timepoint) does **not** wrap. It does **not** make the request. It populates the summary-card flash slot with "Already at last timepoint" via a CSS class for ~2s.
- `?` opens a `<details>` block listing all shortcuts.
- All keypresses emit a `keyboard.shortcut` log event server-side via a beacon (`POST /events` is **out of scope** for S2 — for now, the server only sees the resulting GET and logs it as `event_kind="panel.swap"`. The dedicated `events` table arrives in S6).

The patient id, current `t_index`, and chrome are read from `data-` attributes on `#patient-view` so the script doesn't need to parse the URL.

---

## 12. Accessibility baseline

- **Contrast.** `theme.css` defaults pass WCAG AA: text on background ≥4.5:1, UI elements ≥3:1. Pin specific tokens (`--color-text`, `--color-bg`, `--color-border`, `--color-focus-ring`) so adopters who replace the file don't accidentally regress.
- **Focus rings.** `*:focus-visible { outline: 2px solid var(--color-focus-ring); outline-offset: 2px; }`. Not removed anywhere.
- **a11y fallback tables.** Every `<svg>` chart has a sibling `<table class="a11y-fallback">` containing the underlying `(t_minutes, value)` pairs. Visually hidden by default (`clip: rect(0 0 0 0)` pattern, not `display: none` — screen readers must reach it). Test #10 enforces presence.
- **ARIA.** Each panel has `<section aria-label="vitals panel — {state}">`. The summary card is `<header role="region" aria-label="patient summary">`.
- **Keyboard map.** The `?` overlay is the canonical reference. Also rendered in a hidden `<details>` block at the top of `base.html` so screen readers find it without keyboard interaction.

---

## 13. structlog pipeline summary

| Where | What |
|---|---|
| `setup_logging(log_dir)` at app boot | Configure structlog with one processor chain ending in `JSONRenderer`. Two handlers: `TimedRotatingFileHandler(log_dir/"current.jsonl", when="midnight", utc=True, suffix="%Y-%m-%d", backupCount=0)`, `StreamHandler(sys.stderr)`. (Decision **D2**.) |
| Request-ID middleware | `bind_request_context(request_id=new_request_id(), patient_id=..., timepoint=t_minutes, timepoint_index=t_index, event_kind=..., chrome=..., arm=None)`. Bound vars are picked up by every `logger.info(...)` in the request scope. |
| Per-event log lines | One per request (`page.render` or `panel.swap` per **D3**, `page.error` on unhandled exception), one at `app.boot`, one at `app.shutdown`, one at `app.boot.failed` if lifespan dataset load raises (**D17**). No per-row logs. |
| Rotation | One file per UTC date. `current.jsonl` is the active file; rotated files become `current.jsonl.YYYY-MM-DD`. UTC midnight cutover; long-running process rolls daily. (Decision **D2**.) |

Mandatory fields (8) are enforced by a custom processor (`_inject_context`) that reads the eight `ContextVar`s and adds them to the event dict, defaulting to `None`. This is the same processor every test in `test_logging.py` exercises.

---

## 14. Test inventory (target: ≥20 tests after review-driven additions)

This inventory expanded during `/plan-eng-review` (May 2026) — the original 11-test floor was raised to lock acceptance criteria, error containment, boundary cases, the daily-rollover contract, the htmx pin, and the lifespan failure path. Decisions referenced inline.

### Unit — logging (3)

1. **`test_setup_logging_emits_mandatory_fields`** (`tests/test_logging.py`) — bind a request context, log an info event, parse the JSONL line, assert all 8 mandatory keys (`request_id`, `clinician_id`, `patient_id`, `timepoint`, `timepoint_index`, `event_kind`, `chrome`, `arm`) are present. `clinician_id` and `arm` are `null` in S2; `chrome` is bound to `"dense"`. (Decisions **D4**, **D13**.)
2. **`test_event_kind_dispatch_on_hx_request_header`** (`tests/test_logging.py` or `test_routes.py`) — TestClient sends GET with and without `HX-Request: true`; asserts the JSONL log line for the request carries `event_kind="panel.swap"` and `"page.render"` respectively. (Decision **D3**.)
3. **`test_log_file_rolls_at_utc_midnight`** (`tests/test_logging.py`) — uses `freezegun.freeze_time("2026-05-05T23:59:55Z")` as a context manager. **Inside** the frozen block: build the app via `create_app(log_dir=tmp_path)` (handler-init must happen under the frozen clock — Decision **D14**), log a line, advance via `frozen.tick(timedelta(seconds=10))`, log a second line. Assert two files exist: `current.jsonl` and `current.jsonl.2026-05-05`. (Decisions **D2**, **D11**.)

### Unit — charts (4)

4. **`test_render_timeline_svg_returns_svg_string`** (`tests/test_charts.py`) — call `render_timeline_svg` on an inline 3-row frame, assert result starts with `<?xml` or `<svg`, contains `data-variable="hr"`, is non-empty.
5. **`test_render_timeline_svg_handles_single_row`** (`tests/test_charts.py`) — single-row frame → valid SVG (locks t=0 happy path). (Decision **D12**.)
6. **`test_render_timeline_svg_handles_empty_frame`** (`tests/test_charts.py`) — zero-row frame → valid SVG with axes only. (Decision **D12**.)
7. **`test_render_timeline_svg_raises_on_missing_variable`** (`tests/test_charts.py`) — frame without the named variable → `KeyError`. (Decision **D12**.)

### Unit — panels (2)

8. **`test_slice_to_timepoint_filters_data_above_t`** (`tests/test_panels.py`) — build inline frames at t=0, 60, 180; slice at t=60; assert no rows with `t_minutes > 60.0` survive in any shape; assert ADMISSION is filtered by patient only.
9. **`test_panel_state_detection_table`** (`tests/test_panels.py`) — table-driven over (panel, patient_id, t_minutes) → expected state. **Per Decision D5**, exercises `slice_to_timepoint` directly with an inline full dataset; asserts both the sliced frames AND the `panel_states` dict. Covers all five states for vitals + imaging + AI panels (~12 cases). Includes at least one `empty-unexpected` case per panel (proves the slicing function inspected unsliced data correctly).

### Unit — CLI (1)

10. **`test_cli_serve_invokes_uvicorn`** (`tests/test_cli.py`) — monkeypatch `uvicorn.run` to a list-of-calls collector; call `main(["serve", "--port", "8123", "--reload"])`; assert one invocation with `app="ehr_simulator.web.app:app"`, `host="127.0.0.1"`, `port=8123`, `reload=True`. (Decision **D7**.)

### Unit — static assets (1)

11. **`test_htmx_min_js_sha256_matches_pin`** (`tests/test_static_assets.py`) — `assert hashlib.sha256(static/htmx.min.js).hexdigest() == EXPECTED_HTMX_SHA256`. The constant lives next to the test; updated only on intentional upgrade. (Decision **D15**.)

### Unit — lifespan (1)

12. **`test_lifespan_boot_failure_logs_and_exits`** (`tests/test_app.py` or in `test_routes.py`) — pass a `dataset_loader` that raises `AdapterError("boom")`; assert the `app.boot.failed` log line is emitted and the lifespan raises `SystemExit(1)`. (Decision **D17**.)

### Integration — routes (8)

13. **`test_index_lists_three_synthetic_patients`** (`tests/test_routes.py`) — `TestClient` GET `/`, parse with BeautifulSoup, assert `synth_001`, `synth_002`, `synth_003` all present and each has both a `?chrome=dense` and `?chrome=epic` link.
14. **`test_patient_route_renders_full_page_on_browser_request`** (`tests/test_routes.py`) — GET `/patient/synth_001/timepoint/0` without `HX-Request`, assert response includes `<html>`, the summary card, all five panels, the htmx.min.js script tag, and the theme.css link.
15. **`test_patient_route_returns_partial_on_htmx_request`** (`tests/test_routes.py`) — same URL with `HX-Request: true`, assert the response has the summary card + panels but **no** `<html>` wrapper.
16. **`test_chrome_query_param_routes_to_correct_template`** (`tests/test_routes.py`) — GET `?chrome=dense` and `?chrome=epic` for the same patient/timepoint, assert the DOM structures differ (dense has 5 panel sections at the top level; epic has a `<nav role="tablist">`); the default (no query param) is `dense`.
17. **`test_synth_003_imaging_panel_is_empty_expected_at_t0`** (`tests/test_routes.py`) — GET `/patient/synth_003/timepoint/0`, parse, assert the imaging `<section>` has `aria-label="imaging panel — empty-expected"`. (Decision **D8**, locks the §16 acceptance criterion.)
18. **`test_synth_002_labs_panel_is_partial_at_t60`** (`tests/test_routes.py`) — GET `/patient/synth_002/timepoint/1`, parse, assert the labs `<section>` has `aria-label="labs panel — partial"`. (Decision **D8**, locks the §16 acceptance criterion.)
19. **`test_renderer_exception_contains_to_single_panel`** (`tests/test_routes.py`) — monkeypatch `render_timeline_svg` to raise `RuntimeError("boom")`; GET `/patient/synth_001/timepoint/0`. Assert: (a) status 200, (b) vitals `<section>` has `aria-label="vitals panel — error"`, (c) other panels render their normal states, (d) the per-request JSONL log line carries `event_kind="page.render"` (NOT `"page.error"` — the request didn't fail; just one panel did). (Decision **D9**.)
20. **`test_route_boundary_errors`** (`tests/test_routes.py`) — three asserts in one test (or three small tests): (a) GET `/patient/synth_001/timepoint/99` → 404 with HTML body containing `valid: 0…2`; (b) GET `/patient/unknown/timepoint/0` → 404 with HTML body containing `'unknown' not found`; (c) GET `/patient/synth_001/timepoint/0?chrome=garbage` → 422. (Decisions **D6**, **D10**.)

### Regression (1, non-negotiable)

21. **`test_data_leak_request_for_t_index_1_returns_no_rows_above_60`** (`tests/test_routes.py`) — full HTTP test, not the unit-level slice. GET `/patient/synth_001/timepoint/1` (t=60), parse the response, assert no panel content references `t_minutes` of `180`, and no a11y-fallback-table cell shows a value that only exists at t=180 in `load_synthetic()`. The data-leak failure mode invalidates the study; this is the gate.

### A11y (1)

22. **`test_a11y_fallback_table_present_for_every_chart`** (`tests/test_a11y.py`) — `TestClient` GET `/patient/synth_001/timepoint/2`, parse with BeautifulSoup, find every `<svg>` in the response, assert each has a sibling `<table class="a11y-fallback">`, and assert each table contains at least one numeric cell.

### E2E (1, marked `@pytest.mark.e2e`)

23. **`test_e2e_walk_synth_001_via_keyboard`** (`tests/e2e/test_synthetic_walk.py`) — `pytest-playwright` test. Open `/patient/synth_001/timepoint/0?chrome=dense`. Use `page.expect_request("/patient/synth_001/timepoint/1*")` as a context manager wrapping `page.keyboard.press("]")`; after the block, `page.locator("[data-t-minutes='60']").wait_for()`. Repeat for `]` → t=180 and `[` twice → t=0 (each with the same context-manager pattern). Then press `[` past t=0: assert the network request was NOT made (use `page.expect_request(...)` with a timeout that times out); assert the summary-card flash region contains "already at first timepoint". Then press `]` past the last timepoint (from t=180): assert network request NOT made and flash contains "already at last timepoint". (Decision **D16**.)

**Total: 23 tests, distributed 12 unit / 8 integration / 1 regression / 1 a11y / 1 E2E.** Up from 11 in the initial spec; the additions are review-driven (D6, D7, D8, D9, D10, D11, D12, D14, D15, D16, D17).

---

## 15. CI changes (`.github/workflows/ci.yml`)

Add **after** `uv sync --locked`:

```yaml
- name: Install Playwright browsers
  run: uv run playwright install --with-deps chromium

- name: E2E
  run: uv run pytest -m e2e
  # Runs after the regular pytest job (which has -m 'not e2e' from pyproject)
```

Cache the Playwright browser bundle:

```yaml
- name: Cache Playwright
  uses: actions/cache@v4
  with:
    path: ~/.cache/ms-playwright
    key: playwright-${{ runner.os }}-${{ hashFiles('uv.lock') }}
```

Place the cache step **before** `playwright install`. The unit/integration suite still runs as before via the existing `Test` step; `addopts` filters E2E out by default.

---

## 16. Acceptance criteria (how you know Session 2 is done)

Every item is a check a reviewer can run.

- [ ] `uv sync` clean.
- [ ] `uv run ehr-simulator serve` boots without error; visiting `localhost:8000/` shows three patient links.
- [ ] `curl -s localhost:8000/patient/synth_001/timepoint/0 | grep -q "patient-view"` exits 0.
- [ ] `curl -s -H "HX-Request: true" localhost:8000/patient/synth_001/timepoint/0 | grep -vq "<html"` exits 0 (HTMX path returns partial only).
- [ ] In a browser: open `synth_001`, press `]` twice, see t=180; press `[` twice, see t=0; press `]` once more past the last and see the flash.
- [ ] Open `synth_003` at t=0, see imaging panel in `empty-expected` state.
- [ ] Open `synth_002` at t_index=1 (t=60), see labs panel in `partial` state (vitals present, labs missing).
- [ ] `?chrome=epic` renders the tabbed layout with the same data.
- [ ] `uv run pytest` green (22 tests excluding E2E + 15 from S1 = 37 total).
- [ ] `uv run pytest -m e2e` green (1 test).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI passes on a PR opened against main, including the Playwright job.
- [ ] **`logs/current.jsonl` exists after one request and every line contains all 8 mandatory fields (decisions D2, D4, D13).**
- [ ] **Chrome A/B validation session with the first embedded neurologist runs against this build on `synth_001`. Findings recorded in `specs/session-02-validation-findings.md` (separate file written post-session). Pre-session briefing (Decision D18, mitigating outside-voice critique): explicitly tell the neurologist that the values are synthetic and not clinically valid; the session is about layout, density, and discoverability, not clinical accuracy. The findings document separates layout observations from data-realism reactions.**

---

## 17. Conventions

- Module docstrings on every new module, locking the module's contract in prose.
- `from __future__ import annotations` at the top of every module.
- Type hints on every public function.
- No comments that restate code (per repo CLAUDE.md).
- Test names: `test_<subject>_<expected_behavior>`. One assertion group per test.
- Inline `pd.DataFrame({...})` in tests; no fixtures until a pattern repeats 3+ times. The `dataset` fixture in `conftest.py` is shared by `test_routes.py`, `test_a11y.py`, `test_panels.py`, and the lifespan-failure test (≥4 uses post-review, satisfying the 3+ rule). A `tmp_log_dir` fixture (autouse for tests that build the app) wires `create_app(log_dir=tmp_path)` per D1.
- Templates: snake_case file names, `_panel_*.html` for partials, `_chrome_*.html` for layouts, no logic-heavy templates (pre-render in Python, pass strings).
- HTMX-specific attributes in lowercase (`hx-get`, `hx-target`, `hx-swap`).

---

## 18. Commit discipline (target: ~6 commits, ~2 days)

Updated post-`/plan-eng-review` to reflect added tests and the decisions sheet. If any commit balloons past ~400 lines, split.

| # | Commit | Files |
|---|---|---|
| 1 | `session-02: scaffolding + deps` | `pyproject.toml`, `.gitignore`, `web/__init__.py`, `cli.py`, `tests/test_cli.py` (D7), empty `web/templates/` + `web/static/` directories, vendored `htmx.min.js` + `tests/test_static_assets.py` (D15), `theme.css` (empty token block) |
| 2 | `session-02: structlog pipeline + lifespan failure mode` | `logging.py` (8 ContextVars per D4/D13, `TimedRotatingFileHandler` per D2), `tests/test_logging.py` (mandatory fields, event_kind dispatch, daily rollover via freezegun per D11/D14), `tests/conftest.py` (logging + tmp_log_dir fixtures) |
| 3 | `session-02: FastAPI factory + routes + slicing` | `web/app.py` (parameterized factory per D1, lifespan boot-failure path per D17, middleware event_kind rule per D3), `web/routes.py` (uses `request.app.state.dataset` only; HTML 404 bodies per D6), `web/panels.py` (slice + state computation per D5), `templates/base.html`, `templates/index.html`, `templates/_summary_card.html`, `tests/test_panels.py`, `tests/test_routes.py` (index + full-page + HTMX partial + data-leak regression + boundary tests per D10 + lifespan failure per D17) |
| 4 | `session-02: panels + plotnine + chrome A/B + a11y` | `web/charts.py` (edge-case contract per D12), `web/panels.py` (renderers + per-panel error containment per D9), all `_panel_*.html` + `_chrome_dense.html` + `_chrome_epic.html`, `tests/test_charts.py` (4 tests including edge cases per D12), `tests/test_a11y.py`, `tests/test_routes.py` (chrome A/B + fixture-state tests per D8 + renderer-error containment per D9) |
| 5 | `session-02: keyboard shortcuts + Playwright E2E + CI` | `static/keyboard.js`, `tests/e2e/__init__.py`, `tests/e2e/test_synthetic_walk.py` (E2E pattern per D16), `.github/workflows/ci.yml` |

The S1 ratio (1 spec ≈ 1-2 days, ~5 commits) is the target — this session adds one commit's worth of review-driven test scope.

---

## 19. Open decisions deferred to later sessions

- **Login / `clinician_id`** — S5 (study config + CLI). Until then, every log line carries `clinician_id: null`.
- **Answer capture (`POST /answer`)** — S9a.
- **Question gating (`/advance`)** — S9b.
- **CSV export** — S9c.
- **Arm randomization + AI-visible flag** — S11. Until then, the AI panel is always shown.
- **SQLite `events` table** — S6. Until then, behavioral events are JSONL log lines only.
- **Divergence view** — S10.
- **Tauri spike** — only if browser proves unfit during the embedded-neurologist session (per `TODOS.md`).
- **uPlot fallback** — only if plotnine bundle/render time becomes a measured problem during pilot (per `TODOS.md`).

---

## 20. What Session 2 does NOT lock

- The visual treatment of the two chromes is provisional. The embedded-neurologist session is the design review; outputs may shift the chrome templates significantly in S2.5 or S8. Lock is on the **architecture** (query-param toggle, shared panel partials, single swap target), not the pixels.
- Performance budgets. The synthetic dataset is too small to benchmark anything meaningful. S8's "TTI < 2s on baseline laptop" budget is the first real measurement.
- Per-institution theming. `theme.css` ships empty; one override file is enough for v1.0 (per `TODOS.md`).
- A 6th panel state (`stale` — data was present, then changed). Outside-voice point 11 flagged this as known-incomplete; not S2's job. Revisit at S8 if Geneva data exposes the case.
- Inline-SVG payload size. Per outside-voice point 3 / spec §20: at S8 (24 timepoints × Geneva density), the full-panel-swap-with-inline-SVG architecture may need per-chart streaming or `hx-swap-oob`. Lock is on the swap-target single-source-of-truth render path, not on payload-shape optimization.

---

## 21. What already exists (carried into S2)

These shipped in S1 and are reused by S2 unchanged. Listed so review-driven changes don't accidentally duplicate.

- **`src/ehr_simulator/ingestion/canonical.py`** — `CanonicalShape` enum + 4 `pa.DataFrameSchema` constants (`SCALAR_TS_SCHEMA`, `ADMISSION_SCHEMA`, `IMAGING_SCHEMA`, `AI_OUTPUT_SCHEMA`) + `validate(frame, shape, *, strict, dataset)`. The strict-mode `AdapterError` chain is what D17's lifespan-failure path catches.
- **`src/ehr_simulator/ingestion/synthetic.py`** — `load_synthetic(*, seed=42)` returns a `SyntheticDataset`. **The S2 acceptance criteria for partial-state and empty-expected-state demos are already wired into this fixture**: `synth_002` skips labs at `t=60.0` (synthetic.py:136-137); `_build_imaging` only emits rows for `synth_001` and `synth_002`. The dataset is the test fixture for D8's two integration tests.
- **`src/ehr_simulator/ingestion/exceptions.py`** — `AdapterError` + `IngestionIssue`. Reused for the lifespan-failure log payload (D17).
- **`tests/test_canonical.py` + `tests/test_synthetic.py`** — 15 tests; unchanged by S2.
- **`.github/workflows/ci.yml`** — already runs `uv sync --locked`, ruff, ruff format, pytest on Python 3.11 + 3.12. S2 §15 extends with Playwright steps; does not rebuild the matrix.
- **`pyproject.toml`** — already pins Python ≥3.11, declares `[tool.pytest.ini_options]` with `addopts = "-n auto"`, sets ruff line-length 100 and lint selectors. S2 extends `addopts` with `--strict-markers -m 'not e2e'` and adds the `e2e` marker.

---

## 22. Review-driven decisions log (`/plan-eng-review`, 2026-05-05)

Eighteen decisions were taken during the review. All are folded into the spec text above; this section is the audit trail.

| # | Topic | Decision | Spec section affected |
|---|---|---|---|
| D1 | App factory + test isolation | Parameterize `create_app(log_dir, dataset_loader)`; routes never import `app` at module scope (use `request.app.state`); conftest overrides per test | §3, §5, §17 |
| D2 | Log file rotation | `TimedRotatingFileHandler(when='midnight', utc=True, backupCount=0, suffix='%Y-%m-%d')` instead of `RotatingFileHandler` | §3, §13 |
| D3 | `event_kind` dispatch | Middleware: `HX-Request` header → `panel.swap`, else `page.render`; on exception → `page.error` | §5, §13 |
| D4 | Forward-compat log fields | 8 mandatory ContextVars: add `chrome` (bound from query param) and `arm` (null until S11) | §3, §13 |
| D5 | Panel-state choke point | Compute `panel_states` inside `slice_to_timepoint`; renderers receive labels only — no path to unsliced data | §7 |
| D6 | OOB t_index body shape | 404 + HTML body shaped for the swap target (not JSON) | §6 |
| D7 | CLI test | `tests/test_cli.py` with one serve-invocation test | §4, §14 |
| D8 | Acceptance fixture tests | 2 integration tests for synth_003/0 imaging empty-expected + synth_002/1 labs partial | §14 |
| D9 | Per-panel error containment | Route handler wraps each panel renderer in try/except; failed panel renders error state, others render normally; per-request log stays page.render | §7, §14 |
| D10 | Boundary tests + spec | OOB t_index, unknown patient_id, invalid chrome — three asserts; spec §6 documents all three | §6, §14 |
| D11 | Daily rollover test | freezegun-based test asserts two files after midnight cutover | §14 |
| D12 | Chart edge cases | Single-row → SVG; empty → SVG with axes only; missing variable → KeyError | §8, §14 |
| D13 | `timepoint` field semantics | JSONL `timepoint = t_minutes` (real time); add `timepoint_index` sister field for URL audit | §3, §13 |
| D14 | Rollover test sequencing | Construct `setup_logging` *inside* the `freeze_time` block; tick past midnight | §14 |
| D15 | htmx integrity | `tests/test_static_assets.py` asserts sha256(htmx.min.js) matches a pinned constant | §1, §14 |
| D16 | E2E race fix | Use `page.expect_request(...)` context manager + `page.locator(...).wait_for()`; both deterministic | §14 |
| D17 | Lifespan failure mode | Wrap `dataset_loader()` in try/except; log `app.boot.failed` + raise `SystemExit(1)` with stderr remediation message | §5, §14 |
| D18 | Chrome A/B premise | Reject outside-voice critique; keep S2 chrome A/B with explicit confound-briefing in §16 acceptance | §16 |

**Outside voice (Claude subagent):** ran after sections 1-4, 12 points; agreement on points 1, 5; new findings on 4, 6, 7, 8, 9 incorporated as D13-D17; strategic critique on point 10 surfaced as D18 and explicitly rejected per design-doc P1 commitment; remaining points 2, 3, 11, 12 deferred-by-design or self-resolving. (Codex was unavailable due to 401 auth failure; Claude subagent fallback was used.)

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 1 | issues_open (stale, 2026-04-21, prior plan) | 1 unresolved, 2 critical gaps from autoplan run; not re-run for this session |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 (this run) | clean | 18 decisions logged (0 unresolved); 0 critical gaps; 23 tests in inventory |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | issues_open (stale, 2026-04-21, prior plan) | 1 unresolved from autoplan run; not re-run for this session |
| DX Review | `/plan-devex-review` | Developer experience gaps | 1 | clean (stale, 2026-04-21, prior plan) | TTHW: 3d → 5min from autoplan run |

**CROSS-MODEL:** Outside voice (Claude subagent fallback after Codex 401) added 5 new findings (D13–D17) and one rejected strategic critique (D18). Cross-model agreement on D1 sub-decision (routes use `request.app.state` not module-global) and D5 (`slice_to_timepoint` is the choke point that authorizes unsliced-data access).
**UNRESOLVED:** 0 (this session). Stale unresolveds carry forward from the 2026-04-21 autoplan run on the higher-level design plan, not this session spec.
**VERDICT:** ENG CLEARED — ready to implement. Optional next reviews: `/plan-design-review` post-implementation against the rendered chrome A/B, before the neurologist session.
