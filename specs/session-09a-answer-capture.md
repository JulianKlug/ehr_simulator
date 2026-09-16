# Session 09a — Answer capture

**Goal:** a clinician walking a patient's timepoints can answer the study questions, and every answer lands durably in SQLite the moment it is given. S9a renders a **questions pane** on the patient view (both chromes) driven by the parsed `questions.yaml`, adds one write route `POST /patient/{pid}/timepoint/{t_index}/answer` that validates the value against the question's response type and upserts into `answers` (unique cell from S6), auto-saves on every `change` via HTMX (no Save button), pre-fills saved answers when a timepoint is revisited, emits one `events` row per save/clear with browser-side `client_ts` / `client_seq`, lazily bootstraps the `sessions` + `arm_assignments` rows on first patient contact (the S6 §7.3 deferral), binds the `arm` structlog ContextVar for the first time, and closes the S6 TODO by locking `events.kind` to a `Literal` taxonomy.

**Out of scope (later sessions):** `/advance` + all-answered gating + `sessions.ended_at` (S9b); `export-answers` CLI + CSV-injection guard + pseudonymization keyfile (S9c); randomized arms + AI-panel visibility by arm (S11); divergence view (S10); Geneva AI predictions adapter (S7); real-data UI (S8); question-level `required: bool` / conditional questions (not in `questions.yaml` v1); per-question timing analytics beyond the `answer.*` event rows; undo history of answers (last write wins by design); offline queueing of answers (local-only deployment, server always reachable).

> **Spec history:** drafted 2026-09-16. Reviewed by `/plan-eng-review` on 2026-09-16 (run in a spawned Opus sub-agent with auto-decide): **24 review-fixes applied** (R1–R24), annotated inline and tabled in §18.

---

## 1. Context

S6 shipped the persistence layer: seven tables, `answers.upsert` with the `ux_answers_cell` unique constraint, `events.append`, `sessions.start_or_resume`, `arm_assignments.assign_or_lookup`, the `/login` cookie flow and the `known_clinicians` cache. Every DAO the study needs exists and is tested, but **no route writes an answer**. `app_from_study_config` parses `questions.yaml` and throws the model away (`app.py:201`: *"the parsed model isn't wired up until S9"*). The patient view has five data panels and no place for the clinician to respond. The `arm` ContextVar in `logging.py` has never been bound. `sessions` and `arm_assignments` have zero producers outside tests.

S9a is the first session that turns the simulator from a viewer into an instrument. The study's primary endpoint — *did AI assistance change clinician X's answer to question Y for patient Z at timepoint T* — needs exactly one row per `(clinician_id, patient_id, timepoint, question_id)` cell, tagged with `arm` and `config_hash`. S6 locked the constraint; S9a is the first real producer, so the double-submit regression (S6 test #12) is re-asserted here **at the route level**, where the network retry actually happens.

Design principle carried from S2: the server owns state, HTMX moves fragments, JavaScript stays thin and CSP-clean (`script-src 'self'`, zero inline handlers). The questions pane follows the same shape as the panels: a Jinja partial rendered server-side inside `#patient-view`, swapped wholesale on timepoint navigation, so pre-fill is a render concern and not a client-state concern.

S9b (gating) builds directly on this: "all questions answered" is `len(saved_answers) == len(questions)` for the cell, and the advance CTA lives in the pane S9a ships. S9c (export) reads `answers.value` as the canonical serialized string defined in §6 — the serialization contract is fixed here so the wide-pivot export does not have to guess.

---

## 2. Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | No new deps. stdlib `json` + `datetime`, FastAPI `Form`, Jinja, vendored htmx 2.0.4. |
| 2  | `src/ehr_simulator/db/events.py` | MODIFIED. Adds `EventKind = Literal["clinician.login", "clinician.logout", "session.start", "answer.upsert", "answer.clear"]` and `EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))`. `append(..., kind: EventKind, ...)` raises `ValueError(f"unknown event kind {kind!r}")` **before** touching the DB when `kind not in EVENT_KINDS`. Closes the S6 TODO (*"lock down `events.kind` taxonomy"*). **[review-fix R1]** The guard breaks two existing S6 tests that pass the placeholder `kind="panel.swap"` (`tests/test_db.py:396`, `:495` — `panel.swap` is a structlog `event_kind`, never an `events` row). Commit 1 rewrites both call sites to `kind="session.start"`; no production caller changes. |
| 3  | `src/ehr_simulator/db/answers.py` | MODIFIED. Two read/delete siblings next to `upsert`: `fetch_for_cell(conn, *, clinician_id, patient_id, timepoint) -> dict[str, tuple[str, str]]` (`question_id → (value, config_hash)`, the hash carried for **[review-fix R23]**; **[review-fix R12]** served by the implicit index behind `ux_answers_cell`, whose `(clinician_id, patient_id, timepoint)` prefix matches the WHERE clause — *not* `ix_answers_patient_clinician`, which only covers two of the three columns) and `delete_one(conn, *, clinician_id, patient_id, timepoint, question_id, app_state=None) -> int` (rowcount; increments `write_counter` when `> 0`). |
| 3b | `src/ehr_simulator/db/migrations.py` | MODIFIED. **[review-fix R18]** Migration 2 (`sessions_open_unique`): partial unique index `ux_sessions_open ON sessions (clinician_id, patient_id) WHERE ended_at IS NULL`. See §3. |
| 4  | `src/ehr_simulator/db/sessions.py` | MODIFIED. Adds `find_open(conn, clinician_id, patient_id) -> str | None` — the SELECT half of `start_or_resume`, exposed so the service layer can tell "resumed" from "created" without changing the S6 `start_or_resume` signature (test #9 keeps passing). `start_or_resume` delegates to it. |
| 5  | `src/ehr_simulator/web/study_session.py` | NEW. `@dataclass(frozen=True) SessionContext(session_id: str, arm: str, config_hash: str)` + `bootstrap_session(conn, app_state, *, clinician_id, patient_id) -> SessionContext`. Idempotent per `(clinician, patient)`: `assign_or_lookup` → `find_open` → (`start_or_resume` + one `session.start` event **only when a new session row was created**). Reads `config_hash` from `app_state.config_hash`. S9b extends this module with the close path (`ended_at`). |
| 6  | `src/ehr_simulator/web/answer_capture.py` | NEW. The service layer between the route and the DAOs (per repo layering rule: controllers never call DB directly for new code). `class AnswerValidationError(ValueError)`; `serialize_answer(question: Question, raw_values: list[str]) -> str | None` (None ⇒ *clear*; raises on invalid); `deserialize_answer(question, value: str) -> str | list[str]` (multi-select JSON → list, for pre-fill); `saved_answers(conn, *, clinician_id, patient_id, t_minutes, questions, config_hash) -> dict[str, str | list[str]]` (**[review-fix R23]**: `fetch_for_cell` also selects `config_hash`; `saved_answers` compares each row against the live hash and emits one `answer.config_hash.drift` WARNING per render when they differ — see §8.5 — then drops the column from its return); `record_answer(conn, app_state, *, ctx: SessionContext, clinician_id, patient_id, t_minutes, question, raw_values, client_ts, client_seq) -> AnswerOutcome` where `AnswerOutcome = Literal["saved", "cleared"]`; `normalize_client_ts(raw: str | None) -> str | None` (ISO-8601 → SQLite-parseable UTC `YYYY-MM-DD HH:MM:SS.ffffff`, see §8.3); **[review-fix R2]** `normalize_client_seq(raw: str | None) -> int | None` (the seq twin of `normalize_client_ts`: `int()` inside try/except, and out-of-range values → `None` so an oversized integer cannot raise at INSERT time). Constants: `FREE_TEXT_MAX_CHARS = 4000`, `PROBABILITY_MIN = 0`, `PROBABILITY_MAX = 100`, `CLIENT_SEQ_MIN = 0`, `CLIENT_SEQ_MAX = 2**63 - 1` (SQLite INTEGER ceiling), `FREE_TEXT_AUTOSAVE_DELAY_MS = 1500` (**[review-fix R5]**, rendered into the template). |
| 7  | `src/ehr_simulator/web/app.py` | MODIFIED. `create_app` initializes `app.state.study = None`, `app.state.questions = None`, `app.state.config_hash = None` (no-config synthetic mode). `app_from_study_config` keeps the parsed `questions = load_questions(...)`, sets `app.state.study = study`, `app.state.questions = questions`, `app.state.config_hash = compute_config_hash_from_models(study, questions)`. Removes the *"isn't wired up until S9"* comment. |
| 8  | `src/ehr_simulator/web/routes.py` | MODIFIED. (a) Extracts the patient/t_index resolution block of `patient_timepoint` into `_resolve_timepoint(request, patient_id, t_index) -> tuple[ResolvedTimepoint | None, Response | None]` (same `(value, error_response)` shape as `_require_clinician`) so GET and POST share it byte-for-byte. (b) GET `patient_timepoint` grows: session bootstrap when `app.state.study is not None`, `update_request_context(arm=ctx.arm)`, pre-fill via `saved_answers`, renders `_questions_pane.html` into `_patient_view.html`. (c) NEW `POST /patient/{patient_id}/timepoint/{t_index}/answer` — see §5. |
| 9  | `src/ehr_simulator/web/templates/_patient_view.html` | MODIFIED. Wraps `chrome_html` and the new `questions_html` in a two-column layout container (`.patient-layout`); pane hidden entirely (no markup) when `questions_html` is empty. |
| 10 | `src/ehr_simulator/web/templates/_questions_pane.html` | NEW. `<aside id="questions-pane" aria-label="Questions for this timepoint">` with one `<form class="question" data-question-id=... hx-post=... hx-trigger=… hx-target="find .answer-status" hx-swap="innerHTML" hx-sync="this:queue last">` per question, a `<fieldset><legend>{prompt}</legend>` per form, per-type inputs (§7), and a `<span id="answer-status-{qid}" class="answer-status" role="status" aria-live="polite">` initially rendered by `_answer_status.html` with `state=saved` when a pre-filled value exists, `state=blank` otherwise. **[review-fix R4]** `hx-target` uses htmx's `find` extended selector rather than `#answer-status-{qid}`: `config/questions.py:31` allows `question_id` to start with a digit (`^[a-z0-9_]+$`), and `#3rd_question` is not a valid CSS id selector — `querySelector` would throw. The `id=` stays for tests and for S9b. |
| 11 | `src/ehr_simulator/web/templates/_answer_status.html` | NEW. The 200/422 response fragment and the initial-render badge: `<span class="answer-status is-{{ state }}" data-state="{{ state }}">…</span>` with copy `Saved ✓` / `Cleared` / `{{ error }}` / empty. |
| 12 | `src/ehr_simulator/web/static/answers.js` | NEW. (a) `htmx:configRequest` listener: for requests issued by `form.question`, sets `evt.detail.parameters.client_ts = new Date().toISOString()` and `.client_seq = nextSeq()` (per-tab monotonic counter persisted in `sessionStorage["ehrsim:client-seq"]` so full reloads keep it monotonic). Verified against the vendored build: htmx 2.0.4 wraps `parameters` in a `Proxy` whose `set` trap forwards to the underlying `FormData`, so plain property assignment is the correct spelling. (b) `htmx:beforeSwap` listener: for `form.question` requests with `xhr.status >= 400`, sets `shouldSwap = true` (htmx 2 skips 4xx swaps by default). **[review-fix R6]** the force-swap is conditional on the body actually being our fragment (`xhr.responseText` contains `data-state=`); a 5xx whose body is FastAPI's default error page is *not* swapped — the listener writes a locally built `<span class="answer-status is-error" data-state="error">Save failed — retry</span>` into the badge instead. The same fallback runs on `htmx:sendError` (network down). Zero inline JS; loaded with `defer` from `base.html`. |
| 12b | `src/ehr_simulator/web/static/keyboard.js` | MODIFIED. **[review-fix R19]** `isEditable` gains a `type` check so radios/checkboxes stop killing `[`/`]`/`?`. See §7. |
| 13 | `src/ehr_simulator/web/templates/base.html` | MODIFIED. `<script src="/static/answers.js" defer>` after `keyboard.js`. |
| 14 | `src/ehr_simulator/web/static/theme.css` | MODIFIED. `.patient-layout` grid (chrome `1fr` + pane `minmax(20rem, 26rem)` sticky at `≥1200px`; stacked below), `.question` fieldset styling, `.answer-status.is-saved / .is-cleared / .is-error` colors using existing tokens (`--color-focus-ring`, `--color-muted`, `--color-error`). |
| 15 | `src/ehr_simulator/cli.py` | MODIFIED (one string). `preview --questions` help text `"Optional questions.yaml; reserved for S9."` → `"Optional questions.yaml; renders the questions pane in --html-out."`. |
| 16 | `configs/example_questions.yaml` | MODIFIED (header comment only). Drop the *"not yet wired into the UI"* sentence. |
| 17 | `tests/conftest.py` | EXTENDED. `study_client` fixture: `app_from_study_config(study_fixture_dir/"study_synthetic.yaml", study_fixture_dir/"questions.yaml", log_dir=tmp_log_dir, db_path=tmp_db_path, backup_dir=tmp_backup_dir)` with the same pre-seeded clinician + cookie as `client`. Also `study_clinician_id` (the seeded id) for assertions. |
| 18 | `tests/test_answer_capture.py` | NEW. Service-layer unit tests (§9 #1–#10c). |
| 19 | `tests/test_study_session.py` | NEW. Bootstrap idempotency + resume-after-end (§9 #11–#12). |
| 20 | `tests/test_db.py` | EXTENDED. +6 (§9 #13–#16c). |
| 21 | `tests/test_answer_routes.py` | NEW. Route tests (§9 #17–#32d). |
| 22 | `tests/test_a11y.py` | EXTENDED. +1 (§9 #33). |
| 23 | `tests/test_csp.py` | EXTENDED. +1 (§9 #34). |
| 24 | `tests/test_app.py` | EXTENDED. +1 (§9 #35). |
| 25 | `tests/e2e/conftest.py` | EXTENDED. `live_study_server` session fixture: same subprocess pattern as `live_server` but with `--config … --questions …`, own port, own tmp DB. **[review-fix R7]** both YAML paths are **absolute** (`Path(__file__).parents[1] / "fixtures" / "study" / …`): `live_server` sets `cwd=str(work_dir)` (a `tmp_path_factory` dir, `tests/e2e/conftest.py:57`), so repo-relative argv would resolve against the tmp dir and the subprocess would exit 1 before the readiness probe. |
| 26 | `tests/e2e/test_answer_walk.py` | NEW. Playwright walk (§9 #36–#36c). |
| 27 | `.github/workflows/ci.yml` | EXTENDED. `CLI smoke` step grows one assertion: `grep -q 'id="questions-pane"' /tmp/preview_smoke/synth_001_t0.html` (the `preview --html-out` run already passes `--questions`). |
| 28 | `TODOS.md` | MODIFIED. Strike the S6 `events.kind` TODO (closed). Add the new items from §14. |
| 29 | `specs/ROADMAP.md` | MODIFIED (one line). **[review-fix R8]** §14 promised the S9a route-shape deviation would be recorded in the roadmap, but the file was in neither the deliverables nor a commit. Session 9a's `POST /answer` bullet gains a pointer to §5.1 of this spec. |

`README.md`, `CLAUDE.md` "Current state" are owned by `/document-release` post-ship (CLAUDE.md already lags at "Sessions 1-4 shipped").

---

## 3. Repo layout after Session 9a (diff vs end-of-S6)

```
ehr_simulator/
├── configs/example_questions.yaml               # MODIFIED (comment)
├── src/ehr_simulator/
│   ├── cli.py                                   # MODIFIED (help string)
│   ├── db/
│   │   ├── answers.py                           # MODIFIED (+fetch_for_cell, +delete_one)
│   │   ├── events.py                            # MODIFIED (+EventKind, +EVENT_KINDS guard)
│   │   ├── migrations.py                        # MODIFIED (migration 2: ux_sessions_open, R18)
│   │   └── sessions.py                          # MODIFIED (+find_open)
│   └── web/
│       ├── answer_capture.py                    # NEW (service layer)
│       ├── study_session.py                     # NEW (session bootstrap)
│       ├── app.py                               # MODIFIED (+study/questions/config_hash state)
│       ├── routes.py                            # MODIFIED (+_resolve_timepoint, +POST answer, GET grows)
│       ├── static/
│       │   ├── answers.js                       # NEW
│       │   ├── keyboard.js                      # MODIFIED (isEditable type check, R19)
│       │   └── theme.css                        # MODIFIED
│       └── templates/
│           ├── _answer_status.html              # NEW
│           ├── _patient_view.html               # MODIFIED
│           ├── _questions_pane.html             # NEW
│           └── base.html                        # MODIFIED (+script tag)
├── tests/
│   ├── conftest.py                              # MODIFIED (+study_client)
│   ├── e2e/
│   │   ├── conftest.py                          # MODIFIED (+live_study_server)
│   │   └── test_answer_walk.py                  # NEW
│   ├── test_a11y.py                             # MODIFIED (+1)
│   ├── test_answer_capture.py                   # NEW
│   ├── test_answer_routes.py                    # NEW
│   ├── test_app.py                              # MODIFIED (+1)
│   ├── test_csp.py                              # MODIFIED (+1)
│   ├── test_db.py                               # MODIFIED (+4)
│   └── test_study_session.py                    # NEW
├── .github/workflows/ci.yml                     # MODIFIED
├── specs/ROADMAP.md                             # MODIFIED (route-shape pointer, R8)
└── TODOS.md                                     # MODIFIED
```

**[review-fix R18] One migration after all** — version 2, index-only, no column changes:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS ux_sessions_open
    ON sessions (clinician_id, patient_id) WHERE ended_at IS NULL;
```

`sessions.start_or_resume` is a check-then-insert (`db/sessions.py:19-44`) and S9a makes it a per-request call path for the first time. §12 asserts "exactly one open row per (clinician, patient)" — the draft asserted it without enforcing it. In practice the race is unreachable (every handler is `async def`, so `bootstrap_session`, which contains no `await`, runs atomically on the event loop — see **[review-fix R11]**), but the invariant is what `find_open`'s `ORDER BY started_at DESC LIMIT 1` quietly depends on and what S10's event joins assume; a partial unique index costs ~8 lines and makes it structural instead of reviewer discipline. Migration 2 fails loudly on any DB that already holds duplicate open rows, which is the correct outcome and cannot happen today (no pilot DB predates S9a). Deliverable: `src/ehr_simulator/db/migrations.py` MODIFIED, in commit 1.

Otherwise no schema change. S6's `answers` / `events` / `sessions` / `arm_assignments` DDL already carries every column S9a writes (`client_ts`, `client_seq` were reserved for S9a in S6 §4).

---

## 4. Data flow

```
 GET /patient/{pid}/timepoint/{t}                POST /patient/{pid}/timepoint/{t}/answer
        │                                                 │  form: question_id, value[, value…],
        ▼                                                 │        client_ts, client_seq
 _require_clinician ──miss──▶ 303 / HX-Redirect           ▼
        │ hit                                     _require_clinician ──miss──▶ 303 / HX-Redirect
        ▼                                                 │ hit
 _resolve_timepoint ──bad──▶ 404 html                     ▼
        │ ok (timepoints, t_minutes)              app.state.questions is None ──▶ 409 status fragment
        ▼                                                 │
 app.state.study set? ──no──▶ render, no pane     _resolve_timepoint ──bad──▶ 404 status fragment
        │ yes                                             │ ok
        ▼                                                 ▼
 bootstrap_session(conn, state, cid, pid)        question_id ∈ questions? ──no──▶ 422 status fragment
   ├─ arm_assignments.assign_or_lookup                    │ yes
   ├─ sessions.find_open                                  ▼
   └─ (new) start_or_resume + events "session.start"      bootstrap_session (idempotent, same as GET)
        │ SessionContext(session_id, arm, config_hash)    │
        ▼                                                 ▼
 update_request_context(arm=ctx.arm)              serialize_answer(question, raw_values)
        │                                            ├─ AnswerValidationError ──▶ 422 status fragment
        ▼                                            ├─ None  ──▶ answers.delete_one + events "answer.clear"
 saved_answers(conn, cid, pid, t_minutes, qs)        └─ str   ──▶ answers.upsert   + events "answer.upsert"
        │                                                 │            (arm, config_hash from ctx;
        ▼                                                 │             client_ts normalized, client_seq)
 render _questions_pane.html (pre-filled)                 ▼
   inside _patient_view.html                       200 _answer_status.html (saved | cleared)
```

Layering (new code only; S6's direct `clinicians`/`events` calls in `/login` are left untouched):

```
 routes.py (controller)  ──▶  web/answer_capture.py, web/study_session.py (service)  ──▶  db/* (DAO)  ──▶  sqlite3
```

---

## 5. Route contracts

### 5.1 `POST /patient/{patient_id}/timepoint/{t_index}/answer`

Form-encoded (`application/x-www-form-urlencoded`, what HTMX sends for a `<form>`):

| Field | Cardinality | Notes |
|---|---|---|
| `question_id` | exactly 1 | hidden input; must exist in `app.state.questions` |
| `value` | 0..n | radios/number/textarea send 0 or 1; multi-select checkboxes send 0..n |
| `client_ts` | 0..1 | ISO-8601 from `answers.js`; absent on curl/tests → NULL |
| `client_seq` | 0..1 | int from `answers.js`; absent/garbage/out-of-range → NULL via `normalize_client_seq` (**[review-fix R2]**) |

Read with `form = await request.form(); raw_values = form.getlist("value")` — FastAPI's `Form(...)` does not model repeated keys cleanly, and the GET/POST symmetry is easier to read with the raw form. (`starlette.datastructures.FormData.getlist` verified present on the pinned Starlette.)

**[review-fix R3]** `question_id` is read with `form.get("question_id")` — first value wins if a malformed client sends it twice, and an absent or blank field is its own 422 branch rather than falling through to the unknown-question message with a `None` in it.

**[review-fix R11]** The handler is `async def`, like every other route in `routes.py`. This is load-bearing, not style: FastAPI runs `async def` handlers on the event loop (serialized) and `def` handlers on the threadpool. The app owns a single shared `sqlite3.Connection` (`db/connection.py`, `check_same_thread=False`), so a `def` handler would put concurrent writes on that connection and void the S6 §6.3 single-connection caveat.

Responses — **always** the `_answer_status.html` fragment (so the HTMX badge target gets meaningful HTML for every branch):

| Condition | Status | `data-state` | Copy |
|---|---|---|---|
| saved | 200 | `saved` | `Saved ✓` |
| cleared (empty submission) | 200 | `cleared` | `Cleared` |
| invalid value / unknown option / out of range / too long | 422 | `error` | the `AnswerValidationError` message |
| unknown `question_id` | 422 | `error` | `Unknown question '<id>'` |
| missing/blank `question_id` (**[review-fix R3]**) | 422 | `error` | `Missing question_id` |
| no cookie / unknown clinician | 303 or 200+`HX-Redirect` | — | (S6 `_require_clinician`, unchanged) |
| `app.state.questions is None` | 409 | `error` | `No questions configured (start with --config/--questions)` |
| patient not in study / unknown / `t_index` out of range | 404 | `error` | same message strings as the GET route |

**[review-fix R16]** Every message above reaches the browser as a Jinja variable inside `_answer_status.html`, never as an f-string HTML body. `Jinja2Templates` autoescapes `.html` (verified: `env.autoescape` is `select_autoescape`), so a `patient_id` or option string carrying markup is escaped. The S2 GET 404 bodies at `routes.py:199-223` *do* interpolate `patient_id` into raw HTML with f-strings; S9a does not touch them (out of scope) and the S5 CSP (`script-src 'self'`) defangs the reflected case — a TODO is filed in §14.

Why nested under the patient/timepoint URL rather than the roadmap's flat `POST /answer`: the route reuses `_require_clinician` + `_resolve_timepoint` verbatim, the server (not the browser) maps `t_index → t_minutes`, and the URL is self-describing in JSONL logs (`path` already carries `patient_id` + `t_index`). The roadmap's `/answer` was a placeholder name, not a contract; §14 records the deviation.

### 5.2 `GET /patient/{patient_id}/timepoint/{t_index}` (growth)

After the existing slice + panel rendering:

```python
questions_html = ""
if request.app.state.study is not None:
    ctx = bootstrap_session(db, request.app.state, clinician_id=clinician_id, patient_id=patient_id)
    update_request_context(arm=ctx.arm)
    prefill = saved_answers(db, clinician_id=clinician_id, patient_id=patient_id,
                            t_minutes=t_minutes, questions=request.app.state.questions,
                            config_hash=ctx.config_hash)                 # [review-fix R23]
    questions_html = templates.get_template("_questions_pane.html").render(
        request=request, patient_id=patient_id, t_index=t_index,
        questions=request.app.state.questions.questions, prefill=prefill,
        # [review-fix R15] validator constants, never literals in the markup
        free_text_max_chars=FREE_TEXT_MAX_CHARS,
        free_text_autosave_delay_ms=FREE_TEXT_AUTOSAVE_DELAY_MS,
        probability_min=PROBABILITY_MIN, probability_max=PROBABILITY_MAX,
    )
```

`questions_html` is passed into `_patient_view.html`. The HTMX partial branch (`is_htmx`) and the full-document branch both include it — the pane is inside `#patient-view`, so `[`/`]` navigation swaps it along with the panels and pre-fill is automatic.

### 5.3 `_resolve_timepoint`

**[review-fix R20]** The draft returned `tuple[ResolvedTimepoint | None, Response | None]` — the same shape as `_require_clinician` — and simultaneously promised "the 404 HTML bodies stay identical" (GET's `<div class="error-flash">`, `routes.py:199-223`) *and* that the POST's 404 is an `_answer_status.html` fragment with `data-state="error"` (§5.1, test #26). One helper cannot return both. It returns the error **message**, and each caller renders it in its own shape:

```python
@dataclass(frozen=True)
class ResolvedTimepoint:
    timepoints: tuple[float, ...]
    t_minutes: float

def _resolve_timepoint(request, patient_id, t_index) -> tuple[ResolvedTimepoint | None, str | None]:
    """Study-membership → dataset-membership → t_index range.

    Returns ``(resolved, None)`` or ``(None, message)``. The message strings are
    byte-identical to today's; GET wraps them in the ``error-flash`` div, POST
    renders them through ``_answer_status.html``.
    """
```

Pure extraction otherwise: the three guard conditions and the `timepoints` derivation move here unchanged, and GET keeps a one-line `return HTMLResponse(_error_flash(msg), status_code=404)` so its bodies stay byte-identical (S2 tests assert the `'unknown' not found` substring and the 404 status). The GET route body shrinks by ~20 lines; the POST route reuses the helper.

---

## 6. Value contract (`answers.value` serialization)

`answers.value` is `TEXT NOT NULL`. S9a fixes the canonical string per response type; S9c's export and S10's divergence view consume it verbatim.

| `response_type` | Input control | Accepted raw | Stored `value` | Rejected (422) |
|---|---|---|---|---|
| `categorical` | radios | exactly one option string | the option, verbatim | not in `options`; >1 value |
| `multi-select` | checkboxes | 0..n option strings | JSON array **in `options` order**, e.g. `["Imaging","Labs"]` | any value not in `options`; duplicates |
| `likert` | radios `scale_min..scale_max` | one int-parsable string | `str(int)` | non-int; outside `[scale_min, scale_max]`; >1 value |
| `probability-0-100` | `<input type=number min=0 max=100 step=1>` | one int-parsable string | `str(int)` | non-int (incl. `50.5`); outside `[0, 100]`; >1 value |
| `free-text` | `<textarea maxlength=FREE_TEXT_MAX_CHARS>` | one string | `.strip()`-ed text | `len > FREE_TEXT_MAX_CHARS`; >1 value |

**[review-fix R14]** Ordering: `.strip()` runs **before** the length check, so `" " * 10 + "x" * 4000` is accepted and stored as 4000 chars. Also: a browser submits `""` for an `<input type=number>` whose content it cannot parse, so typing letters into a probability field arrives as an empty submission and clears the cell. That is deliberate (the field *is* empty) and visible — the badge reads `Cleared`, not a silent no-op.

**Empty submission ⇒ clear.** Uniform rule across all five types: if, after stripping, there are no non-empty values (`[]`, `[""]`, `["   "]`), `serialize_answer` returns `None`, the route calls `answers.delete_one`, emits `answer.clear`, and returns `Cleared`. This keeps S9b's gating honest — an unchecked multi-select or a wiped textarea is *not* an answer. Radios cannot be un-selected in a browser, so categorical/likert only hit this branch via curl.

**[review-fix R10]** Consequence for S9b, stated here because S9a fixes the rule: since "answered" means "a row exists" and an empty multi-select stores no row, a multi-select whose empty state is a *legitimate* answer becomes unanswerable and the clinician can never advance. `questions.yaml` v1 has no `required` flag to opt out of. The authoring rule is therefore: **a multi-select must ship an explicit opt-out option** (`None of these`) whenever "nothing applies" is a real answer. Recorded as an S9b blocker in §14.

Multi-select canonical order = `options` order from `questions.yaml`, not click order, so equal selections always produce equal strings (byte-equality is what S9c's export and any `GROUP BY value` will see).

Whitespace: values are `.strip()`-ed before validation for every type (an option `"Yes"` submitted as `"Yes "` still matches; the stored value is the canonical option string).

---

## 7. Questions pane UI

```
┌──────────────────────────────────────────────────┬────────────────────────────────┐
│ summary card (nav, pid, age/sex, t = …)          │                                │
├──────────────────────────────────────────────────┤ <aside id="questions-pane">    │
│ chrome (epic tabs | dense grid)                  │  Questions · t = 60 min        │
│   Admission · Vitals · Labs · Imaging · AI       │ ┌────────────────────────────┐ │
│                                                  │ │ 1. Will the patient have a │ │
│                                                  │ │    neurological …?         │ │
│                                                  │ │  ( ) Yes (•) No ( ) Unknown│ │
│                                                  │ │                   Saved ✓  │ │
│                                                  │ ├────────────────────────────┤ │
│                                                  │ │ 3. Probability of good …   │ │
│                                                  │ │   [ 65 ] %                 │ │
│                                                  │ │                   Saved ✓  │ │
│                                                  │ ├────────────────────────────┤ │
│                                                  │ │ 7. Any additional …        │ │
│                                                  │ │   [textarea            ]   │ │
│                                                  │ │                            │ │
│                                                  │ └────────────────────────────┘ │
└──────────────────────────────────────────────────┴────────────────────────────────┘
      ≥1200px: two columns, pane sticky (top: 0.5rem, max-height: 100vh, overflow-y: auto)
      <1200px: pane stacks below the chrome
```

Markup per question (`_questions_pane.html`, spec not literal):

```html
<form class="question" data-question-id="{{ q.question_id }}" data-response-type="{{ q.response_type }}"
      hx-post="/patient/{{ patient_id }}/timepoint/{{ t_index }}/answer"
      hx-trigger="{{ 'change, submit, input changed delay:%dms'|format(free_text_autosave_delay_ms)
                     if q.response_type == 'free-text' else 'change, submit' }}"
      hx-target="find .answer-status" hx-swap="innerHTML"
      hx-sync="this:queue last">
  <input type="hidden" name="question_id" value="{{ q.question_id }}">
  <fieldset>
    <legend><span class="q-index">{{ loop.index }}.</span> {{ q.prompt }}</legend>
    {# categorical / likert: one <label><input type="radio" name="value" value=…></label> per option/point #}
    {# multi-select: <label><input type="checkbox" name="value" value=…></label> per option #}
    {# probability: <label>… <input type="number" name="value" min="{{ probability_min }}" max="{{ probability_max }}" step="1" inputmode="numeric"> %</label> #}
    {# free-text: <label>… <textarea name="value" rows="3" maxlength="{{ free_text_max_chars }}"></textarea></label> #}
  </fieldset>
  <span id="answer-status-{{ q.question_id }}" class="answer-status …" role="status" aria-live="polite">…</span>
</form>
```

- **Why one `<form>` per question:** HTMX submits every named input inside the issuing form, so a multi-select's `n` checked boxes arrive as `n` `value` fields with no client-side assembly. `hx-trigger="change"` fires on radio/checkbox toggle, on number commit, and on textarea blur-after-edit (the DOM `change` semantics for `<textarea>`), which is exactly the roadmap's "auto-save on blur" without a separate trigger per type.
- **[review-fix R5] Free-text also autosaves while typing.** `change` alone means a clinician who types a note and then closes the tab, or whose browser crashes, loses it — blur never fires. Free-text forms therefore add `input changed delay:{{ FREE_TEXT_AUTOSAVE_DELAY_MS }}ms`, so a 1.5 s typing pause commits. The debounce is scoped to free-text only: on radio/checkbox forms `input` and `change` both fire and would double-post. `hx-sync="this:queue last"` collapses whatever overlap remains.
- **[review-fix R21] `submit` is in every trigger list, because Enter would otherwise blow the page away.** Setting `hx-trigger` on a `<form>` replaces htmx's default `submit` trigger, so htmx stops intercepting native submission. None of the question forms has a submit button and none has more than one field that blocks implicit submission, so per the HTML implicit-submission rule pressing Enter (most likely in the probability field, where typing `65` and hitting Enter is reflex) fires a native GET to the current URL with no `action` — a full reload that drops the `chrome=` query and any un-blurred textarea. Listing `submit` puts htmx back in charge; htmx cancels the native event for a `submit` trigger on a form.
- **[review-fix R15]** `FREE_TEXT_MAX_CHARS` / `PROBABILITY_MIN` / `PROBABILITY_MAX` / `FREE_TEXT_AUTOSAVE_DELAY_MS` reach the template through the render context (§13), never as literals in the markup.
- **`hx-sync="this:queue last"`:** rapid toggles on one question queue behind the in-flight request and only the final state is sent afterwards. Verified against the vendored htmx 2.0.4: the `last` branch clears `queuedRequests` and pushes a closure that **re-serializes the form at send time**, so the queued request carries the final DOM state, not the state at queue time. Ordering across *different* questions is irrelevant (different cells).
- **Navigating mid-save is safe.** Clicking a nav control blurs the textarea first (`mousedown` → `blur` → `click`), so the POST is already in flight when the swap replaces `#patient-view`. htmx 2.0.4's element cleanup (`beforeCleanupElement`) clears timeouts and listeners but does **not** abort in-flight XHRs — it aborts only on an explicit `htmx:abort` event — so the answer still lands. The only casualty is the badge, whose target is gone; htmx logs a target error and moves on.
- **Likert labels:** `scale_min_label` / `scale_max_label` rendered under the first/last radio when present.
- **Pre-fill:** `prefill[q.question_id]` drives `checked` (radios/checkboxes), `value=` (number), textarea body. The badge renders `state=saved` when a pre-filled value exists (so a revisit shows what is already recorded), `state=blank` otherwise.
- **[review-fix R19] Keyboard shortcuts break the moment the pane ships.** `keyboard.js:16-21` returns `true` for *any* `<input>` regardless of `type`, so after the clinician clicks a radio — the single most common action in the new pane — focus sits on that radio and `[` / `]` / `?` go dead until they click elsewhere. The draft asserted the opposite here and in §16. `isEditable` gains a type check: only text-entry controls block shortcuts (`text`, `search`, `url`, `tel`, `email`, `password`, `number`, `date`/`time`/`datetime-local`/`month`/`week`), while `radio`, `checkbox`, `button`, `submit`, `reset`, `file`, `range`, `color` do not. `<textarea>`, `<select>` and `contenteditable` keep blocking. Constant `_TEXT_ENTRY_INPUT_TYPES` at the top of `keyboard.js`; e2e test #36 (`click radio → press ]`) is the regression that would have caught it.
- **No Save / Submit button.** The roadmap and plan.md want auto-save; a button would invite "did I save?" confusion. S9b adds the single **Next timepoint** CTA to the pane.
- **a11y:** every control is wrapped in a `<label>`; `fieldset/legend` groups the prompt; the badge is `role="status" aria-live="polite"`; the aside carries `aria-label`.

---

## 8. Service layer

### 8.1 `web/study_session.py`

```python
def bootstrap_session(conn, app_state, *, clinician_id: str, patient_id: str) -> SessionContext:
    config_hash = app_state.config_hash                   # str; caller guarantees study mode
    arm, _source = arm_assignments.assign_or_lookup(conn, clinician_id, patient_id, config_hash=config_hash)
    session_id = sessions.find_open(conn, clinician_id, patient_id)
    if session_id is None:
        session_id = sessions.start_or_resume(conn, clinician_id, patient_id, arm=arm, config_hash=config_hash)
        events.append(conn, session_id=session_id, clinician_id=clinician_id, patient_id=patient_id,
                      timepoint=None, kind="session.start", payload={"arm": arm}, app_state=app_state)
    return SessionContext(session_id=session_id, arm=arm, config_hash=config_hash)
```

Called from both GET and POST; two SELECTs per request in the steady state. `session.start` fires exactly once per open session (test #11). A session that S9b later closes (`ended_at` set) yields a fresh row + a fresh event on the next visit (test #12).

**[review-fix R9] Bootstrapping on GET writes, and that trips the S6 backup gate.** `events.append` increments `app_state.write_counter` (`db/events.py`), and `web/app.py:142` gates the shutdown `create_backup` on `write_counter > 0`. After S9a, merely opening a patient page produces a `session.start` event, so the gate trips on read-only browsing. Two consequences, both accepted:

1. The gate was already loose — `/login` passes `app_state` on its own event (`routes.py:124`), so any login trips it. Keeping `app_state` on `session.start` is the consistent call, and a `sessions` row *is* study data (first-contact timestamp) worth backing up.
2. `cli_support.render_html_previews` drives the real GET route, so `preview --html-out` now leaves a `<out_dir>/_preview_backups/*.db` snapshot where it previously logged `db.backup.skipped`. Harmless (the preview DB is a scratch file in the same dir) but named here and in §12 so nobody debugs it as a leak.

Bootstrap is deliberately **not** POST-only: `arm` must bind to the log ContextVar on every page render (S11 hides the AI panel by arm, and the `arm` field on the `page.render` line is how the pilot's JSONL is joined), and that needs `assign_or_lookup`, which is itself the write.

### 8.2 `web/answer_capture.py::record_answer`

```python
def record_answer(conn, app_state, *, ctx, clinician_id, patient_id, t_minutes, question,
                  raw_values, client_ts, client_seq) -> AnswerOutcome:
    value = serialize_answer(question, raw_values)        # raises AnswerValidationError
    cell = dict(clinician_id=clinician_id, patient_id=patient_id, timepoint=t_minutes,
                question_id=question.question_id)
    if value is None:
        deleted = answers.delete_one(conn, **cell, app_state=app_state)
        outcome, payload = "cleared", {"deleted": bool(deleted)}
    else:
        answers.upsert(conn, **cell, value=value, arm=ctx.arm, config_hash=ctx.config_hash, app_state=app_state)
        outcome, payload = "saved", {"value_chars": len(value)}
    events.append(conn, session_id=ctx.session_id, clinician_id=clinician_id, patient_id=patient_id,
                  timepoint=t_minutes, kind=f"answer.{'clear' if value is None else 'upsert'}",
                  payload={"question_id": question.question_id, "response_type": question.response_type,
                           **payload},
                  client_ts=normalize_client_ts(client_ts),
                  client_seq=normalize_client_seq(client_seq),   # [review-fix R2]
                  app_state=app_state)
    return outcome
```

The event payload carries **no raw value** — the value lives in `answers`; duplicating free-text into `events.payload_json` would double the pseudonymization surface for S9c's keyfile policy. `value_chars` is enough for behavioral analyses (edit length over time).

### 8.3 `normalize_client_ts` — the `PARSE_DECLTYPES` footgun

`events.client_ts` is declared `TIMESTAMP` and S6 opens connections with `detect_types=PARSE_DECLTYPES`. Python's default `timestamp` converter only parses `YYYY-MM-DD HH:MM:SS[.ffffff]`. Storing the browser's `2026-09-16T12:34:56.789Z` verbatim would make **every later `SELECT … FROM events` raise `ValueError`** on that row. So:

```python
def normalize_client_ts(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        get_logger().warning("client_ts unparseable", event_kind="answer.client_ts.invalid", raw=raw)
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S.%f")
```

A bad clock field never rejects an answer (NULL + WARNING).

**[review-fix R2]** `client_seq` gets the same treatment as an explicit, tested function rather than an inline `try: int(...)` in the route:

```python
def normalize_client_seq(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        seq = int(raw)
    except ValueError:
        get_logger().warning("client_seq unparseable", event_kind="answer.client_seq.invalid", raw=raw)
        return None
    if not CLIENT_SEQ_MIN <= seq <= CLIENT_SEQ_MAX:      # SQLite INTEGER is 8 bytes
        get_logger().warning("client_seq out of range", event_kind="answer.client_seq.invalid", raw=raw)
        return None
    return seq
```

The range clamp is not paranoia: a client posting `client_seq=9` * 30 would otherwise reach `events.append` as a Python int too large for SQLite and raise `OverflowError` *after* the answer row was already written, leaving the answer saved and the event missing.

### 8.4 `events.kind` taxonomy

```python
EventKind = Literal["clinician.login", "clinician.logout", "session.start", "answer.upsert", "answer.clear"]
EVENT_KINDS: frozenset[str] = frozenset(get_args(EventKind))
```

`append` checks membership first and raises `ValueError` — a typo in a future producer fails the first test that exercises it instead of silently forking the taxonomy. S9b appends `"advance.ok"`, `"advance.blocked"`, `"session.end"` to the same alias.

`get_args(EventKind)` at import time is safe under `from __future__ import annotations`: the future import only defers *annotations*, and `EventKind = Literal[...]` is an ordinary module-level assignment, so the alias is a live `typing` object when `EVENT_KINDS` is built.

**[review-fix R1] The guard is a breaking change for two existing tests.** `tests/test_db.py:396` (`test_events_append_returns_autoincrement_id`) and `:495` (the FK-violation test) both pass `kind="panel.swap"` as a throwaway string. `panel.swap` is a structlog `event_kind` (`web/app.py:240`), never an `events` row, so it does **not** join the taxonomy; commit 1 rewrites both to `kind="session.start"`. This is the only S1–S6 behavior change in S9a and §12 says so.

### 8.5 `config_hash` drift on pre-fill **[review-fix R23]**

`answers.upsert` overwrites `config_hash` on conflict (`db/answers.py:41-44`), and `saved_answers` keys only on `(clinician_id, patient_id, timepoint)`. So if `questions.yaml` is edited mid-pilot and the server restarts, answers recorded under the old config still pre-fill the pane, and the clinician's next save silently relabels them with the new hash — erasing exactly the provenance the column exists to preserve. The column stops separating config generations at the moment it matters.

S9a does not suppress the pre-fill (the answer to an unchanged question is still that clinician's answer, and dropping it would look like data loss). It makes the drift **loud**:

```python
stale = {qid for qid, (_v, row_hash) in rows.items() if row_hash != config_hash}
if stale:
    get_logger().warning(
        "pre-filled answers were recorded under a different config",
        event_kind="answer.config_hash.drift",
        question_ids=sorted(stale), row_config_hash=..., live_config_hash=config_hash,
    )
```

One WARNING per render, not per row. Mid-pilot config edits are already a pre-registration violation (design-doc D8); this is the detector that makes one visible in `logs/current.jsonl` instead of in the analysis six weeks later. S9c's export inherits the same check as a TODO (§14).

---

## 9. Test inventory (ROADMAP bar ≥5; final = 46 new test functions after review)

Numbered to match commits in §11. Parametrize same-validator-different-input cases per the S5 convention.

### `tests/test_answer_capture.py` (12)

1. **`test_serialize_categorical[ok|unknown|two_values]`** — `"No"` → `"No"`; `"Maybe"` → `AnswerValidationError`; `["Yes","No"]` → error.
2. **`test_serialize_likert[min|max|below|above|non_int]`** — `"1"`,`"5"` ok; `"0"`,`"6"`,`"x"` error (fixture likert is 1..5).
3. **`test_serialize_probability[0|100|101|neg|float|text]`** — `"0"`,`"100"` ok; `"101"`,`"-1"`,`"50.5"`,`"abc"` error.
4. **`test_serialize_multi_select_canonical_order`** — `["Labs","Imaging"]` → `'["Imaging","Labs"]'`; `["Imaging","Imaging"]` → error (duplicates); `["Nope"]` → error.
5. **`test_serialize_free_text_strips_and_caps`** — `"  hello "` → `"hello"`; `"x" * 4001` → error; `"x" * 4000` ok.
6. **`test_serialize_empty_means_clear[type×{[],[""],["  "]}]`** — returns `None` for all five types.
7. **`test_record_answer_upserts_and_emits_event`** (`db` fixture + seeded clinician + `bootstrap_session`) — `answers` row has `value`, `arm="no_ai"`, `config_hash`; one `events` row `kind="answer.upsert"`, `session_id == ctx.session_id`, `client_ts == "2026-09-16 12:34:56.789000"` given `"2026-09-16T12:34:56.789Z"`, `client_seq == 7`, payload has `question_id`/`response_type`/`value_chars` and **no `value` key**.
8. **`test_record_answer_clear_deletes_row_and_emits_event`** — save then clear → 0 rows; `answer.clear` event `payload.deleted == true`; clearing again → still 0 rows, second event `deleted == false`.
9. **`test_saved_answers_deserializes_per_type`** — multi-select comes back as `list[str]`, others as `str`; unknown `question_id` rows in the DB (from an older questions.yaml) are ignored.
10. **`test_normalize_client_ts[z|offset|naive|garbage|none]`** — `Z` → UTC; `+02:00` → shifted to UTC; naive kept; garbage → `None` + WARNING captured via `structlog.testing.capture_logs`; `None` → `None`.
10b. **`test_normalize_client_seq[int|zero|garbage|empty|none|overflow|negative]`** **[review-fix R2]** — `"7"` → 7; `"0"` → 0; `"abc"`/`""`/`None` → `None` + WARNING; `"9"*30` → `None` (would raise `OverflowError` at INSERT); `"-1"` → `None`.
10c. **`test_saved_answers_warns_on_config_hash_drift`** **[review-fix R23]** — upsert a row with `config_hash="old"`, call `saved_answers(config_hash="new")` → value still returned, exactly one `answer.config_hash.drift` WARNING naming the question_id; same hash → no WARNING.

### `tests/test_study_session.py` (2)

11. **`test_bootstrap_session_idempotent`** — first call: 1 `sessions` row, 1 `arm_assignments` row, 1 `session.start` event; second call returns identical `SessionContext`, counts unchanged.
12. **`test_bootstrap_session_after_ended_creates_new`** — set `ended_at` manually → third call returns a new `session_id`, 2 `session.start` events total, `arm_assignments` still 1 row (S6 lock).

### `tests/test_db.py` (+6)

13. **`test_events_append_rejects_unknown_kind`** — `append(kind="panel.swap")` raises `ValueError`; `events` row count unchanged.
14. **`test_answers_fetch_for_cell_returns_mapping`** — two upserts on different `question_id` → `{"q1": "...", "q2": "..."}`; other timepoint/clinician rows excluded.
15. **`test_answers_delete_one_rowcount_and_write_counter`** — returns 1 then 0; `write_counter` increments only on the 1.
16. **`test_sessions_find_open`** — `None` on empty; id after `start_or_resume`; `None` after `ended_at` set.
16b. **`test_migration_2_rejects_second_open_session`** **[review-fix R18]** — a raw `INSERT INTO sessions` for a `(clinician, patient)` pair that already has an open row raises `sqlite3.IntegrityError`; the same insert succeeds after the first row's `ended_at` is set. Also asserts `apply_migrations` on a v1 DB returns `[2]` and is idempotent on a second call.
16c. **`test_answers_fetch_for_cell_returns_config_hash`** **[review-fix R23]** — the mapping value is `(value, config_hash)`, not a bare string.

### `tests/test_answer_routes.py` (20)

17. **`test_post_answer_saves_row_with_arm_and_config_hash`** — `study_client.post(".../timepoint/1/answer", data={"question_id":"deterioration_6h","value":"No"})` → 200, `data-state="saved"`; row `timepoint == 60.0`, `arm == "no_ai"`, `config_hash == app.state.config_hash`.
18. **`test_post_answer_twice_same_cell_one_row`** — **REGRESSION** (S6 #12 at route level): `Yes` then `No` → 1 row, value `No`.
19. **`test_post_answer_multi_select_json_encoded`** — `data=[("question_id","contributing_factors"),("value","Labs"),("value","Imaging")]` → value `'["Imaging","Labs"]'`.
20. **`test_post_answer_invalid_value_422_no_row[bad_option|likert_9|prob_101|free_text_too_long]`** — 422, `data-state="error"`, message text present, 0 rows.
21. **`test_post_answer_unknown_question_422`** — `question_id="nope"` → 422, `Unknown question 'nope'`.
21b. **`test_post_answer_missing_question_id_422`** **[review-fix R3]** — POST with only `value=No` → 422, `Missing question_id`, 0 rows; a POST sending `question_id` twice uses the first and saves one row.
22. **`test_post_answer_empty_clears_and_returns_cleared`** — save free-text, then post `value=""` → 200 `data-state="cleared"`, 0 rows, `answer.clear` event.
23. **`test_post_answer_emits_event_with_client_fields`** — pass `client_ts`/`client_seq` → event row `session_id IS NOT NULL`, `patient_id`, `timepoint == 60.0`, normalized `client_ts`, `client_seq`.
24. **`test_post_answer_no_cookie_redirects[plain|htmx]`** — `anonymous_client` → 303 → `/login`; with `HX-Request: true` → 200 + `HX-Redirect`.
25. **`test_post_answer_no_questions_configured_409`** — plain `client` (no study) → 409 fragment; 0 rows.
26. **`test_post_answer_bad_target_404[patient_not_in_study|unknown_patient|t_index_out_of_range]`** — 404 fragment; 0 rows; no `sessions` row created.
27. **`test_post_answer_bootstraps_session_when_get_skipped`** — POST without a prior GET → `sessions` + `arm_assignments` rows exist; `session.start` event once.
28. **`test_get_patient_renders_questions_pane`** — 7 `form.question` (fixture count), each `hx-post` equals the nested URL for that `t_index`, `data-response-type` matches, `#questions-pane` present.
29. **`test_get_patient_prefills_saved_answers`** — upsert radio/number/checkbox/textarea cells directly, GET → `checked` on the right radio + checkboxes, `value="65"` on the number input, textarea body, badges `data-state="saved"` for those four and `blank` for the rest.
30. **`test_get_patient_bootstraps_session_and_binds_arm`** — GET → 1 `sessions` row, 1 `arm_assignments` row; `capture_logs()` shows the `page.render` request line with `arm == "no_ai"`; second GET → no new rows/events.
31. **`test_get_patient_without_study_has_no_pane`** — plain `client` → 200, no `#questions-pane`, no `sessions` row.
32. **`test_get_patient_htmx_partial_includes_pane`** — `HX-Request: true` → response has no `<html>` and does contain `#questions-pane` (pane travels with the swap).
32b. **`test_get_patient_prefill_does_not_leak_other_timepoints`** — **REGRESSION**, **[review-fix R13]**. The S2/S8 "data ≤ t only" invariant extended to the clinician's own answers: upsert every question at `t_index=2`, GET `t_index=0` → zero `checked` attributes, zero non-empty `value=`/textarea bodies inside `#questions-pane`, every badge `data-state="blank"`. A `fetch_for_cell` that forgets the `timepoint` predicate would show a clinician their future answers and silently contaminate the study.
32c. **`test_questions_pane_hx_trigger_per_response_type`** **[review-fix R5, R21]** — every `form.question` carries `submit` in `hx-trigger`; the `free-text` form additionally carries `input changed delay:1500ms`; the other four carry exactly `change, submit`; `hx-target` is `find .answer-status` on all seven (**[review-fix R4]**).
32d. **`test_post_answer_404_body_is_status_fragment_not_error_flash`** **[review-fix R20]** — the POST 404 body has `data-state="error"` and no `class="error-flash"`, while the GET 404 for the same bad target still has `error-flash` and the byte-identical S2 message.

### `tests/test_a11y.py` (+1)

33. **`test_every_question_control_has_label_and_legend`** — BeautifulSoup over `study_client` GET: every `input`/`textarea` inside `#questions-pane` (except `type=hidden`) is wrapped by a `<label>` or referenced by `label[for]`; every `form.question` has exactly one `fieldset > legend`; each badge has `role="status"`.

### `tests/test_csp.py` (+1)

34. **`test_questions_pane_is_csp_clean_and_answers_js_served`** — pane HTML has no `on*=` attributes and no inline `<script>`; `GET /static/answers.js` → 200 with `text/javascript`.

### `tests/test_app.py` (+1)

35. **`test_app_from_study_config_sets_questions_and_config_hash`** — `app.state.questions` is a `Questions` with 7 entries; `app.state.config_hash == compute_config_hash(study_path, questions_path)`; `create_app()` leaves all three `None`.

### E2E (3)

36. **`tests/e2e/test_answer_walk.py::test_answer_autosave_and_prefill`** (`live_study_server`) — login → `/patient/synth_001/timepoint/0?chrome=epic` → click radio `No` on `deterioration_6h` inside `page.expect_request(".../timepoint/0/answer")` → badge shows `Saved ✓` → **[review-fix R19]** press `]` **immediately, with the radio still focused** → the view advances to `t_index=1` (this is the regression for the `isEditable` bug; the draft's flow clicked away first and would have passed over it) → press `[` back → radio `No` still checked → type in `free_notes` textarea, `Tab` out → badge `Saved ✓` → `page.reload()` → still pre-filled.
36b. **`test_free_text_autosaves_without_blur`** **[review-fix R5]** — type into `free_notes`, do **not** blur, `page.wait_for_timeout(2000)` → badge reads `Saved ✓` and the row exists.
36c. **`test_enter_in_probability_field_does_not_reload`** **[review-fix R21]** — focus the `good_outcome_3mo` number input, type `65`, press `Enter` → no navigation (`page.url` unchanged, `#questions-pane` still present), badge `Saved ✓`.

**Total: 46 new test functions** (test_answer_capture 12, test_study_session 2, test_db +6, test_answer_routes 20, test_a11y +1, test_csp +1, test_app +1, e2e 3); ~75 cases after parametrization. ROADMAP bar (≥5) cleared 8×. Project baseline is 196 test functions today, so S9a lands at ~240.

---

## 10. CI changes (`.github/workflows/ci.yml`)

`CLI smoke` step, after the existing `test -s /tmp/preview_smoke/synth_001_t0.html`:

```yaml
          grep -q 'id="questions-pane"' /tmp/preview_smoke/synth_001_t0.html
```

`preview --html-out` already boots `app_from_study_config` with `--questions` and a seeded "Dr. Preview" cookie, so the rendered file now contains the pane — a cheap installed-entry-point check that the template wiring survived packaging. The `e2e` job picks up test #36 automatically via `-m e2e`.

---

## 11. Commit discipline (target 6 commits, ~1.5 days)

| # | Commit | Files |
|---|---|---|
| 1 | `session-09a commit 1: EventKind taxonomy + open-session unique index + answers fetch/delete + sessions.find_open` | `db/events.py`, `db/answers.py`, `db/sessions.py`, `db/migrations.py` (migration 2, **R18**), `tests/test_db.py` (#13–#16c, **plus the two `kind="panel.swap"` call-site rewrites at :396 / :495, R1**), `TODOS.md` (strike the events.kind TODO). |
| 2 | `session-09a commit 2: answer_capture + study_session service modules` | `web/answer_capture.py`, `web/study_session.py`, `tests/test_answer_capture.py` (#1–#10), `tests/test_study_session.py` (#11–#12). |
| 3 | `session-09a commit 3: wire questions/config_hash into app.state + _resolve_timepoint + POST answer route` | `web/app.py`, `web/routes.py`, `tests/conftest.py` (+`study_client`), `tests/test_app.py` (#35), `templates/_questions_pane.html` + `templates/_answer_status.html` + `templates/_patient_view.html` (unstyled, functional markup), `tests/test_answer_routes.py` (#17–#32). |
| 4 | `session-09a commit 4: answers.js + keyboard isEditable fix + layout CSS + a11y/CSP tests` | `templates/base.html`, `static/answers.js`, `static/keyboard.js` (**R19**), `static/theme.css`, `tests/test_a11y.py` (#33), `tests/test_csp.py` (#34). |
| 5 | `session-09a commit 5: e2e answer walk + CI smoke grep + doc strings` | `tests/e2e/conftest.py` (+`live_study_server`), `tests/e2e/test_answer_walk.py` (#36–#36c), `.github/workflows/ci.yml`, `cli.py` (help string), `configs/example_questions.yaml` (comment), `specs/ROADMAP.md` (**R8**), `TODOS.md` (new items from §14). |
| 6 | `session-09a commit 6: ruff/format pass` | whole tree. |

Commit 3 lands the pane markup unstyled so every route test (#17–#32) is meaningful in a green tree; commit 4 adds the client script, layout CSS and the a11y/CSP locks.

---

## 12. Acceptance criteria

- [ ] `uv sync` clean (no new deps).
- [ ] `uv run pytest` green; 46 new test functions. **[review-fix R1]** No S1–S6 *production* semantics change; the only edits to existing tests are the two placeholder `kind="panel.swap"` strings in `tests/test_db.py` (:396, :495). The `client` fixture is untouched; `study_client` is additive.
- [ ] **[review-fix R18]** `apply_migrations` on a v1 DB applies migration 2 and is idempotent; a second open `sessions` row for the same pair raises `IntegrityError`.
- [ ] **[review-fix R19]** Click a radio in the pane, then press `]` without clicking away — the timepoint advances (e2e #36).
- [ ] **[review-fix R21]** Type `65` in the probability field and press Enter — the page does not reload and the answer saves.
- [ ] `uv run ehr-simulator serve --config tests/fixtures/study/study_synthetic.yaml --questions tests/fixtures/study/questions.yaml --db-path /tmp/s9a.db` boots; after `/login`, `/patient/synth_001/timepoint/0` shows the 7-question pane to the right of the chrome at ≥1200px and below it when narrower.
- [ ] Selecting a radio shows `Saved ✓` within one round-trip; `sqlite3 /tmp/s9a.db "SELECT question_id, value, arm, timepoint FROM answers"` shows the row with `arm=no_ai`, `timepoint=0.0`.
- [ ] Selecting a different radio for the same question updates the row in place (`SELECT COUNT(*) FROM answers` unchanged). **REGRESSION** carried from S6 #12.
- [ ] Ticking two multi-select boxes stores `["Imaging","Labs"]` regardless of click order.
- [ ] Wiping the free-text and tabbing out shows `Cleared`; the row is gone; an `answer.clear` event exists.
- [ ] `curl -X POST -d "question_id=confidence&value=9" --cookie "$COOKIE" …/timepoint/0/answer` → 422 with the error fragment; no row.
- [ ] `curl -X POST -d "question_id=nope&value=1" …` → 422 `Unknown question 'nope'`.
- [ ] Same POST against a bare `serve` (no config) → 409.
- [ ] Same POST without cookie → 303 → `/login`; with `HX-Request: true` → 200 + `HX-Redirect: /login`.
- [ ] `SELECT kind, count(*) FROM events GROUP BY kind` after one walk shows `clinician.login`, `session.start` (once per patient visited), `answer.upsert`, `answer.clear`; `client_ts` values are `YYYY-MM-DD HH:MM:SS.ffffff` and `SELECT * FROM events` does not raise under `PARSE_DECLTYPES`.
- [ ] `sessions` has exactly one open row per `(clinician, patient)` visited; `arm_assignments` one row per pair.
- [ ] JSONL `logs/current.jsonl` request lines for patient routes carry `"arm": "no_ai"` (first ever non-null `arm`).
- [ ] Navigating `]` then `[` re-renders the pane with the saved values pre-filled and `Saved ✓` badges; a full reload does the same.
- [ ] `events.append(kind="typo")` raises `ValueError` (test #13).
- [ ] Response headers still carry the S5 CSP; browser console shows no CSP violations while answering (no inline JS introduced).
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean.
- [ ] CI green on 3.11 + 3.12 including the new `grep` in `CLI smoke`; `uv run pytest -m e2e` green including the answer walk.
- [ ] `uv run pytest -m real_data` still green (untouched adapters).
- [ ] **[review-fix R9]** `ehr-simulator preview … --html-out /tmp/preview_smoke` exits 0 and now also writes `/tmp/preview_smoke/_preview_backups/*.db` — expected, not a leak (the GET route bootstraps a session, which trips the S6 write-counter backup gate).

---

## 13. Conventions

- `from __future__ import annotations`, module docstrings, type hints on every public function — as S6.
- New Python modules are pure functions + frozen dataclasses; no classes with behavior. Constants (`FREE_TEXT_MAX_CHARS`, `PROBABILITY_MIN/MAX`, `CLIENT_SEQ_MIN/MAX`, `FREE_TEXT_AUTOSAVE_DELAY_MS`, `EVENT_KINDS`) at module top; no magic numbers in validators or templates — every one of them reaches `_questions_pane.html` through the render context (**[review-fix R15]**), never as a literal in the markup. Same rule in `keyboard.js` (`_TEXT_ENTRY_INPUT_TYPES`) and `answers.js`.
- Routes stay thin: parse form → resolve → call service → render fragment. New route code never imports `sqlite3` or DAO modules directly (`answer_capture` / `study_session` do). S6's existing `/login` DAO calls are left as-is (out of scope; do not touch).
- Early returns over nesting in the POST handler: each failure branch `return _status(...)` immediately.
- **[review-fix R11]** Both patient routes stay `async def`. This is the concurrency contract for the shared `sqlite3.Connection`, not a style choice — see §5.1.
- Template ids: `questions-pane`, `answer-status-{question_id}`; CSS classes `question`, `answer-status`, `is-saved|is-cleared|is-error|is-blank`, `patient-layout`. `data-*` attributes carry machine-readable state (`data-state`, `data-response-type`, `data-question-id`) so tests and S9b's JS never parse copy text.
- JS: IIFE + `"use strict"` like `keyboard.js`; `sessionStorage` access wrapped in try/catch; no globals leaked.
- Event payloads never contain the raw answer value.
- Tests: `test_<subject>_<expected_behavior>`; BeautifulSoup for HTML assertions (already a dev dep); `structlog.testing.capture_logs` for log assertions.

---

## 14. Open decisions deferred to later sessions / TODOs to file

- **`/advance` + gating + `ended_at`** — S9b. S9a leaves every session open. S9b computes completeness as `set(saved_answers) == {q.question_id for q in questions if q.required}` — see the `required` prerequisite below, and the multi-select opt-out rule in §6 (**[review-fix R10]**): a multi-select whose empty state is a legitimate answer must ship an explicit `None of these` option, because an empty submission deletes the row and gating reads a missing row as unanswered.
- **Advance CTA placement** — the pane's footer, added by S9b. S9a ships no placeholder slot (YAGNI).
- **Design review of the pane** — the two-column layout, sticky behavior, and badge styling are functional defaults. File a TODO: *run `/plan-design-review` on the questions pane before the next clinician session* (ties into the existing DESIGN.md TODO).
- **Route shape deviation** — roadmap said `POST /answer`; S9a ships the nested URL (§5.1 rationale). Update `specs/ROADMAP.md` S9a line in commit 5 with a one-line pointer to this spec.
- **`required` per question — promoted to a hard S9b prerequisite [review-fix R22].** The draft deferred this on "revival criterion: a study designer asks for optional questions". The dependency actually runs the other way: S9b's gate is `set(saved_answers) == {q.question_id for q in questions}`, which makes **every** question mandatory — including `free_notes` ("Any additional reasoning?"), which no clinician should be forced to fill to advance. S9b cannot ship a usable gate without `required: bool = True` on `_QuestionBase`. Two consequences for sequencing:
  - Adding a defaulted field is backward compatible for YAML parsing (`extra="forbid"` rejects unknown keys, not new known ones), so **no `schema_version` bump** is needed.
  - It **does** change `config_hash`: `compute_config_hash_from_models` hashes `questions.model_dump_json()` (`config/loader.py:129`), so every hash shifts. That is free today — S9a is the first producer of `answers` rows and no pilot data exists — and expensive after the first real session. **It must land before the first pilot answer is recorded.** S9a deliberately leaves it to S9b's first commit (S9b is the session whose acceptance needs it, and S9b lands before any pilot), but if S9a and S9b are not shipped back to back, pull it forward into S9a.
- **Conditional questions** — not in `questions.yaml` v1. Revival criterion: a study designer asks for branching. Would need a `schema_version` bump.
- **S11 prerequisite: arm assignment locks on a GET [review-fix R24].** `bootstrap_session` calls `assign_or_lookup` from the GET handler, and `arm_assignments` rows are never rewritten by design (`db/arm_assignments.py:22-25`). Harmless in S9a (the phase-1 stub always returns `no_ai`), but under S11's randomization a misclick from the patient index permanently burns that clinician-patient cell, and nothing in the plan can un-assign it. S11 must pick one: defer assignment to the first POST (and read `arm` optimistically on GET), or ship an admin un-assign path. File as a TODO alongside the existing S11 `seed` TODO. Depends on: S11 spec authoring.
- **S9c inherits the `config_hash` drift check [review-fix R23].** §8.5 logs a WARNING when pre-filled answers carry a different `config_hash` than the running config. The export has the same exposure: a wide pivot silently mixes config generations. S9c should emit a per-generation column or refuse to export a mixed set. Depends on: S9c spec authoring.
- **Reflected markup in the S2 GET 404 bodies [review-fix R16].** `routes.py:199-223` interpolates `patient_id` into HTML with f-strings and no escaping. Defanged today by the S5 CSP (`script-src 'self'`) and irrelevant on a localhost pilot, but it is a latent reflected-HTML hole that a v1.0 open-source release should not carry. S9a does not touch those lines (unrelated-code rule); fix at v1.0 prep by routing them through a template like the POST branch does. Depends on: v1.0 prep.
- **Answer edit history** — last write wins (`ts_recorded` bumps). The `answer.upsert` event stream is the audit trail (one row per save, with `value_chars`). If analyses need per-edit values, add a `payload.value` opt-in flag gated on the S9c pseudonymization policy. TODO.
- **Server-side `client_seq` gap detection** — `client_seq` is stored, not validated. A gap analysis (dropped requests) is an S10 query, not a runtime check. **[review-fix R17]** The counter lives in `sessionStorage`, which is per-tab: two tabs on the same patient produce two independent sequences that collide. S10's gap query must therefore treat `client_seq` as ordered *within a tab*, not globally, and nothing today records a tab id. If S10 needs a global order, add a per-tab uuid to the event payload then — no column change required.
- **`preview --html-out` pre-fill** — renders an empty pane (fresh DB). Fine for pilot review. **[review-fix R9]** It now also emits `<out_dir>/_preview_backups/*.db`, because the GET route bootstraps a session and that trips the S6 write-counter backup gate (§8.1). Expected; asserted in §12.
- **`events.kind` extension procedure** — append to `EventKind`, and the `ValueError` guard forces the producer's test to be written. Documented in the `events.py` docstring.
- **Python 3.12+ `sqlite3` default-converter deprecation** — `PARSE_DECLTYPES` timestamp converters are deprecated in 3.12 (removed in 3.14). S6 chose them; S9a's `normalize_client_ts` keeps rows parseable. File a TODO to replace with explicit `register_converter` or plain TEXT columns before the Python floor moves. Depends on: v1.0 prep.

---

## 15. What Session 9a does NOT lock

- Gating, advance, session close — S9b.
- CSV export shape beyond the `answers.value` string contract in §6 — S9c.
- Arm randomization and AI-panel hiding — S11.
- Pane visual design — `/plan-design-review` follow-up.
- Multi-clinician concurrency — single shared connection, pilot scale (S6 §6.3 caveat stands). Migration 2 (**R18**) now enforces the one-open-session invariant in the schema, but it does not make the connection concurrent.
- Real-data UI — S8.

Considered during the review and explicitly **not** taken into S9a:

- **One form per timepoint instead of per-question autosave.** The outside voice argued this removes `client_seq`, `hx-sync`, per-question badges, the 4xx swap handling and the clear-vs-empty semantics in one stroke. Rejected: the ROADMAP commits S9a to "auto-save on blur emits an `event` row", and per-question timing is the behavioral signal the study is built to collect — a single end-of-timepoint submit erases the order and latency of individual answers, which is exactly the AI-influence evidence. Recorded as cross-model tension in the review report.
- **Reordering S9c (export) before the pane.** Also from the outside voice: nothing is extractable from the DB until S9c, so export is the step that de-risks "are we recording the right thing". Rejected as a scope call, not an engineering one: `sqlite3` is a perfectly good read path during S9a/S9b bring-up, and §17's live walk uses it. Raise with `/plan-ceo-review` if the sequencing matters.
- **`required: bool` on `_QuestionBase`.** Deferred to S9b's first commit, with the config-hash deadline spelled out in §14 (**R22**).
- **Un-assigning an arm.** S11's problem (**R24**).

---

## 16. What already exists (carried into S9a)

- **`db/answers.upsert`** — the write path; S9a's first real caller. Unchanged.
- **`db/events.append`** — grows the `EventKind` guard; `client_ts`/`client_seq` columns already exist. `payload_json` canonicalization unchanged.
- **`db/sessions.start_or_resume`**, **`db/arm_assignments.assign_or_lookup`** — first non-test callers. Signatures unchanged.
- **`config/questions.py`** — the discriminated union the pane and the serializer switch on (`response_type`). Unchanged.
- **`config/loader.compute_config_hash_from_models`** — now called once at `app_from_study_config` time and cached on `app.state.config_hash`.
- **`web/routes._require_clinician`** — reused verbatim by the POST route.
- **`logging.update_request_context(arm=…)`** — the `arm` ContextVar finally gets a producer.
- **`keyboard.js::isEditable`** — prevents `[`/`]` while typing an answer, but **[review-fix R19]** it currently treats every `<input>` as editable, so it also kills the shortcuts after a radio click. S9a narrows it to text-entry types (§7); this is the one carried-in file S9a modifies rather than reuses.
- **`tests/conftest.py::client`, `anonymous_client`, `_seed_clinician`** — reused; `study_client` composes them with `app_from_study_config`.
- **`tests/e2e/conftest.py::live_server`** — pattern duplicated for `live_study_server` (same readiness probe on `/login`).
- **`cli_support.render_html_for_preview`** — already seeds a clinician cookie and passes `--questions`; the pane appears in previews with no code change. (The draft called it `render_html_previews`; the real symbol is `render_html_for_preview`.) Side effect noted in §8.1 / §12 (**[review-fix R9]**).
- **CSP middleware** — `script-src 'self'`; `answers.js` is a same-origin file, no policy change.

---

## 17. Verification (end-to-end)

1. **Pytest:**
   ```
   uv run pytest                 # green, +46 functions
   uv run pytest -m e2e          # S2 + S6 + S9a walks green
   uv run pytest -m real_data    # unchanged
   ```

2. **Live walk:**
   ```
   rm -f /tmp/s9a.db*
   uv run ehr-simulator serve --config tests/fixtures/study/study_synthetic.yaml \
       --questions tests/fixtures/study/questions.yaml --db-path /tmp/s9a.db &
   curl -s -o /dev/null -X POST -d "clinician_name=Dr. Nine" http://localhost:8000/login
   COOKIE="ehrsim_clinician_id=$(sqlite3 /tmp/s9a.db 'SELECT clinician_id FROM clinicians LIMIT 1')"
   BASE=http://localhost:8000/patient/synth_001/timepoint/1
   curl -s --cookie "$COOKIE" $BASE | grep -c 'class="question"'            # → 7
   curl -si --cookie "$COOKIE" -X POST -d "question_id=deterioration_6h&value=No" $BASE/answer | head -1   # 200
   curl -si --cookie "$COOKIE" -X POST -d "question_id=deterioration_6h&value=Yes" $BASE/answer | head -1  # 200
   sqlite3 /tmp/s9a.db "SELECT count(*), value FROM answers"                # → 1|Yes
   curl -si --cookie "$COOKIE" -X POST -d "question_id=contributing_factors&value=Labs&value=Imaging" $BASE/answer | head -1
   sqlite3 /tmp/s9a.db "SELECT value FROM answers WHERE question_id='contributing_factors'"   # → ["Imaging","Labs"]
   curl -si --cookie "$COOKIE" -X POST -d "question_id=confidence&value=9" $BASE/answer | head -1   # 422
   curl -si --cookie "$COOKIE" -X POST -d "question_id=free_notes&value=" $BASE/answer | grep data-state   # cleared
   sqlite3 /tmp/s9a.db "SELECT kind, count(*) FROM events GROUP BY kind"
   #   clinician.login|1  session.start|1  answer.upsert|3  answer.clear|1
   sqlite3 /tmp/s9a.db "SELECT count(*) FROM sessions WHERE ended_at IS NULL"   # → 1
   grep '"arm": "no_ai"' logs/current.jsonl | head -1                            # non-empty
   kill %1
   ```

3. **PARSE_DECLTYPES sanity** (the §8.3 footgun):
   ```
   uv run python -c "
   from pathlib import Path
   from ehr_simulator.db import connect
   rows = connect(Path('/tmp/s9a.db')).execute('SELECT client_ts FROM events').fetchall()
   print('OK', len(rows))
   "
   ```

If all three pass on a fresh clone post-`uv sync`, S9a is shipped.

---

## 18. Review history

Filled by `/plan-eng-review` (2026-09-16). Each accepted fix is annotated `[review-fix R<N>]` inline in the spec body; this table cross-references them.

| Review-fix | Original design | Resolved design |
|---|---|---|
| R1 | `EventKind` guard added; §12 claims no S1–S6 semantics change. | `kind="panel.swap"` at `tests/test_db.py:396` / `:495` would raise. Commit 1 rewrites both to `session.start`; §12 states the exception. |
| R2 | "`client_seq` follows the same policy: `int()` or NULL" — no function, no test. | `normalize_client_seq` in `answer_capture`, range-clamped to `CLIENT_SEQ_MIN..CLIENT_SEQ_MAX` so an oversized int cannot `OverflowError` after the answer row is already written. Test #10b. |
| R3 | `question_id` "exactly 1"; no behavior for 0 or 2. | Read with `form.get` (first wins); absent/blank is its own 422 `Missing question_id`. Test #21b. |
| R4 | `hx-target="#answer-status-{qid}"`. | `hx-target="find .answer-status"`. `question_id` may start with a digit (`questions.py:31`), and `#3rd_q` is not a valid CSS selector. |
| R5 | Free-text saves on `change` (blur) only. | Free-text forms add `input changed delay:1500ms` (`FREE_TEXT_AUTOSAVE_DELAY_MS`); a closed tab loses ≤1.5 s of typing, not the note. Scoped to free-text so radios don't double-post. Tests #32c, #36b. |
| R6 | `answers.js` force-swaps any `status >= 400` into the badge. | Force-swap only when the body carries `data-state`; otherwise (5xx page, `htmx:sendError`, timeout) write a local `Save failed — retry` badge. A silent stale badge is the worst failure mode for a capture tool. |
| R7 | `live_study_server` passes `tests/fixtures/...` relative paths. | Absolute paths — the fixture runs the subprocess with `cwd` set to a `tmp_path_factory` dir (`tests/e2e/conftest.py:57`). |
| R8 | §14 promises a `specs/ROADMAP.md` edit; the file is in no deliverable or commit. | Added as deliverable #29 and to commit 5. |
| R9 | GET-side bootstrap treated as side-effect-free. | Documented: the `session.start` event increments `write_counter`, so browsing trips the S6 shutdown backup gate and `preview --html-out` now writes `_preview_backups/`. Kept (consistent with `/login`, and a session row is study data); asserted in §12. |
| R10 | "Empty ⇒ clear" stated without its gating consequence. | §6 states that an empty multi-select deletes the row and S9b reads that as unanswered, so a multi-select whose empty state is a real answer must ship an explicit `None of these` option. Flagged as an S9b blocker. |
| R11 | Handler concurrency unstated. | §5.1/§13 mandate `async def` on both patient routes: the shared `sqlite3.Connection` is only safe because `async def` serializes on the event loop. Also what makes R18's race unreachable. |
| R12 | `fetch_for_cell` "served by `ix_answers_patient_clinician`". | Served by the implicit index behind `ux_answers_cell`, whose `(clinician_id, patient_id, timepoint)` prefix matches the WHERE clause. |
| R13 | No route-level check that pre-fill respects the timepoint. | Test #32b: answers saved at `t_index=2` must not appear at `t_index=0`. The S2/S8 "data ≤ t" invariant extended to the clinician's own answers. |
| R14 | Strip-vs-cap ordering and invalid-number behavior unstated. | §6: strip precedes the length check; a browser sends `""` for an unparseable `type=number`, so letters in a probability field clear the cell and show `Cleared` (visible, not silent). |
| R15 | `maxlength="4000"` hard-coded in the §7 markup, contradicting §13. | All validator constants reach the template through the render context; §5.2 shows the call. |
| R16 | POST reuses "the same message strings as the GET route". | Messages render through `_answer_status.html` (Jinja autoescape verified), never an f-string body. The S2 GET bodies keep their raw f-strings (out of scope) with a v1.0 TODO in §14. |
| R17 | `client_seq` described as a monotonic counter. | Monotonic **per tab** — `sessionStorage` is per-tab, so two tabs collide. S10's gap analysis must not assume a global order. |
| R18 | "No schema migration"; §12 asserts one open session per pair. | Migration 2 adds the partial unique index `ux_sessions_open … WHERE ended_at IS NULL`, making the asserted invariant structural. Test #16b. |
| R19 | "`keyboard.js::isEditable` already prevents `[`/`]` while typing an answer." | False for the new pane: `keyboard.js:16-21` blocks on *any* `<input>`, so shortcuts die after a radio click. `isEditable` narrows to text-entry types; e2e #36 presses `]` with the radio still focused. |
| R20 | `_resolve_timepoint` returns `(value, Response)` and promises both identical GET 404 bodies and `_answer_status.html` POST 404 bodies. | Returns `(value, message)`; each caller renders its own shape. |
| R21 | `hx-trigger="change"` on forms with no submit button. | `submit` added to every trigger list — otherwise Enter in the probability field fires a native GET to the current URL and reloads the page, dropping `chrome=` and any un-blurred textarea. Test #36c. |
| R22 | `required` deferred on "a study designer asks for optional questions". | Promoted to a hard S9b prerequisite: S9b's gate would make `free_notes` mandatory. No `schema_version` bump needed, but it shifts every `config_hash`, so it must land before the first pilot answer. |
| R23 | `saved_answers` ignores `config_hash`; `upsert` silently relabels rows. | `fetch_for_cell` returns `(value, config_hash)`; `saved_answers` emits one `answer.config_hash.drift` WARNING per render on mismatch (§8.5). Tests #10c, #16c. |
| R24 | Arm assignment on GET treated as harmless. | Harmless in S9a (stub arm), but under S11 a misclick permanently locks the cell with no un-assign path. Recorded as an S11 prerequisite in §14. |

---

## Spec destination

`specs/session-09a-answer-capture.md` (matches the `session-NN-name.md` convention). The pre-review draft is overwritten in place by the review pass.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 (fresh) | — | last run 2026-04-21, stale |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 24 issues, 0 critical gaps, all 24 folded into the spec (R1–R24) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 (fresh) | — | last run 2026-05-06, stale; pane design deferred per §14 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 (fresh) | — | last run 2026-04-21, stale |
| Outside Voice | `/plan-eng-review` (auto) | Cross-model challenge | 1 | ISSUES_FOUND | Codex CLI returned 401 on token refresh; ran the Claude-subagent fallback. 12 findings, 7 folded (R7, R10, R18, R19, R20, R21, R22/R23/R24), 2 verified-false, 2 surfaced as tension |

- **CODEX:** not available — `codex exec` failed auth (`Failed to refresh token: 401 Unauthorized`) after 5 retries. Outside voice ran as a Claude subagent with fresh context.
- **CROSS-MODEL:** high overlap on the mechanical defects (the `live_study_server` relative-path bug and the multi-select "none" trap were found independently by both). The subagent added four this review missed and verified in code: `keyboard.js::isEditable` killing `[`/`]` after a radio click (R19), `_resolve_timepoint` being unable to serve both 404 body shapes (R20), implicit form submission on Enter (R21), and `required` being a hard S9b prerequisite rather than a nice-to-have (R22). Two of its findings were checked against the vendored htmx 2.0.4 build and do **not** hold: in-flight answer POSTs survive a navigation swap (htmx's `beforeCleanupElement` clears listeners and timeouts but never aborts the XHR, so only the badge is lost, not the answer), and the `sessions` check-then-insert race is unreachable while every handler is `async def` — R18 still adds the unique index so the invariant does not depend on that. Its two strategic challenges are the open items below.
- **VERDICT:** CLEARED — 24 fixes applied. The 2 strategic decisions were answered by the owner on 2026-09-16 (both kept as recommended); see below.

**RESOLVED DECISIONS (owner, 2026-09-16):**
- **Per-question autosave vs one form per timepoint.** The outside voice argues a single end-of-timepoint form deletes `client_seq`, `hx-sync`, per-question badges, the 4xx swap handling and the clear-vs-empty semantics in one stroke, for a local single-user tool whose worst-case loss is one timepoint of re-entry. This review kept per-question autosave (§15) because the ROADMAP commits S9a to "auto-save on blur emits an `event` row" and per-answer order and latency are the AI-influence signal the study exists to measure. **Owner decision: keep per-question autosave.**
- **Whether S9c (`export-answers`) should precede the pane.** The outside voice notes nothing is extractable from the DB until S9c, so export is the step that de-risks "are we recording the right thing". This review treated it as a ROADMAP sequencing call rather than an engineering one — `sqlite3` is an adequate read path during bring-up and §17's live walk uses it. **Owner decision: keep the S9a → S9b → S9c order.**

NO UNRESOLVED DECISIONS
