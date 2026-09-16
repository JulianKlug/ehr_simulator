# Session 09b — Question gating + `/advance`

**Goal:** a clinician cannot see a later timepoint before answering the current one, and cannot change an answer after moving on. S9b adds a per-`(clinician, patient)` **progress frontier** (`progress` table, migration 3), a single forward path `POST /patient/{pid}/timepoint/{t_index}/advance` that verifies completeness server-side, unlocks the next timepoint and — on the last one — closes the session, a **GET gate** that redirects any request beyond the frontier back to it, a **locked (read-only) pane** for every timepoint behind the frontier, an **advance CTA** in the pane footer whose label carries the remaining-count and updates out-of-band on every save, a blocked-click path that scrolls to the first unanswered question and records an `advance.blocked` event, a `required: bool` flag per question (the S9a R22 prerequisite), progress markers on the patient index, and an admin `ehr-simulator reset-progress` CLI as the recovery path for a mis-advance (owner decision, see the review report).

**Out of scope (later sessions):** `export-answers` CLI + CSV-injection guard + pseudonymization keyfile (S9c); randomized arms + AI-panel visibility by arm (S11); divergence view (S10); Geneva AI predictions adapter (S7); real-data UI (S8); per-question timers beyond the `advance.*` event rows; conditional questions; a confirm dialog before advancing (friction; the CTA label is explicit).

> **Spec history:** drafted 2026-09-16; reviewed by `/plan-eng-review` on 2026-09-16 — **29 review-fixes** applied inline and cross-referenced in §18 (R18–R29 from the outside-voice pass). The three premise-level decisions were answered by the owner on 2026-09-16 (GET gate + frozen answers kept; `reset-progress` CLI pulled into scope; `HX-Push-Url` on every partial kept) — see `RESOLVED DECISIONS` at the end of the review report.

---

## 1. Context

S9a turned the viewer into an instrument: every answer lands in `answers` the moment it is given, one row per `(clinician_id, patient_id, timepoint, question_id)`. But nothing constrains *order*. A clinician can press `]` with six questions blank, read the t=180 min labs, press `[`, and answer t=60 with knowledge they should not have. `README.md` says so in as many words (*"You can still move to the next timepoint with questions unanswered"*). For the study's primary endpoint — *what did clinician X believe about patient Z at time T* — that is a validity hole of the same class as the S8 "data ≤ t only" invariant, just one level up: **answers ≤ t must be given while data ≤ t is all the clinician has seen.**

`plan.md` is explicit: *"questions from a timepoint have to be answered before moving on to next timepoint"* and *"go to next timepoint (questions have to be answered first)"*. The ROADMAP's S9b bullets add the mechanics: `/advance` with an optimistic expected timepoint, server-side enforcement, disabled-unless-complete CTA, click-when-disabled scrolls + emits an event, remaining-count in the label.

Two S9a review-fixes are S9b's entry conditions. **R22:** the gate makes every question mandatory unless `required: bool` exists — `free_notes` ("Any additional reasoning?") must not block anyone — and adding the field shifts every `config_hash`, so it lands in commit 1, before any pilot answer exists. **R10:** an empty multi-select stores no row and the gate reads no row as unanswered, so a multi-select whose empty state is a legitimate answer must ship a `None of these` option; `configs/example_questions.yaml` documents the rule.

Design principle carried from S2/S9a: the server owns state, HTMX moves fragments, JS stays thin and CSP-clean. Completeness is computed **once, server-side**, and every UI reflection of it (CTA label, `aria-disabled`, scroll target, the summary card's missing Next button) is rendered from that one computation — the browser never counts badges.

---

## 2. Deliverables

| #  | Path | Purpose |
|----|---|---|
| 1  | `pyproject.toml` | No new deps. |
| 2  | `src/ehr_simulator/config/questions.py` | MODIFIED. `_QuestionBase.required: StrictBool = True`. `StrictBool`, not `bool`: pydantic's lax mode would accept `required: 1` / `"yes"`; the field is a study-design switch and gets no coercion. Backward compatible for parsing (`extra="forbid"` rejects unknown keys, not new known ones) → **no `schema_version` bump**. Shifts every `config_hash` (`compute_config_hash_from_models` dumps the model) — acceptable only because no pilot answer exists yet (S9a §14). |
| 3  | `tests/fixtures/study/questions.yaml`, `configs/example_questions.yaml` | MODIFIED. `free_notes` gains `required: false`. Example file's header gains the two authoring rules: *optional questions need `required: false`*; *a multi-select whose empty state is a real answer must ship an explicit `None of these` option* (S9a R10). **[review-fix R22]** — and both files must actually **obey** the second rule, which the draft did not: `contributing_factors` ("Which factors contributed most to your decision?") is a multi-select with `options: [Imaging, Vitals, Labs, Medical history, AI output]` (`tests/fixtures/study/questions.yaml:30-33`, `configs/example_questions.yaml`) and no opt-out. Under the gate it is `required` by default, so a clinician who thinks *nothing* drove the call cannot advance without naming a factor — fabricated data, at every timepoint, on the study's own reference config. Add `None of these` to the option list in **both** files (the fixture's option count is asserted only through `test_get_patient_prefills_saved_answers`'s checked-set, which is unaffected) and lock the rule with test #3c. |
| 4  | `src/ehr_simulator/db/migrations.py` | MODIFIED. Migration 3 (`progress`): the per-pair frontier table. See §3. |
| 5  | `tests/fixtures/db/migration_001_expected_schema.sql` | MODIFIED. Schema snapshot grows the `progress` DDL (test `test_post_migration_schema_matches_fixture` compares `sqlite_master` byte-for-byte). |
| 6  | `src/ehr_simulator/db/progress.py` | NEW. DAO. `@dataclass(frozen=True) Progress(clinician_id, patient_id, unlocked_t_index: int, completed_at: datetime | None, config_hash: str)`; `fetch(conn, *, clinician_id, patient_id) -> Progress | None`; `unlock(conn, *, clinician_id, patient_id, from_t_index, to_t_index, config_hash, app_state=None) -> bool` (**[review-fix R29]** compare-and-set on the observed frontier; returns `False` when the row already moved) (upsert, **monotonic**: `MAX(progress.unlocked_t_index, excluded.unlocked_t_index)`; **[review-fix R11]** `config_hash` is written on INSERT only and never overwritten on conflict — the row records the hash the walk *started* under, which is what the §8.2 drift WARNING compares against); `mark_complete(conn, *, clinician_id, patient_id, unlocked_t_index, config_hash, app_state=None) -> None` (upsert; `completed_at = COALESCE(progress.completed_at, CURRENT_TIMESTAMP)` so it is set once); `list_for_clinician(conn, clinician_id) -> dict[str, Progress]` (index page). Every write bumps `app_state.write_counter`. A missing row *is* the "not started" state — GET never writes a progress row (unlike `session.start`), so browsing a fresh patient stays a pure read on this table. |
| 7  | `src/ehr_simulator/db/sessions.py` | MODIFIED. `close(conn, session_id) -> int` — `UPDATE sessions SET ended_at = CURRENT_TIMESTAMP WHERE session_id = ? AND ended_at IS NULL`; returns rowcount (0 on a second call). Frees the pair under `ux_sessions_open`, so the next visit opens a fresh session (S9a test #12 already locks that). **[review-fix R12]** also `find_latest(conn, clinician_id, patient_id) -> str | None` (most recent by `started_at`, open **or** closed) — `bootstrap_session` reuses it on a completed patient instead of opening a session nothing can ever close. |
| 8  | `src/ehr_simulator/db/events.py` | MODIFIED. `EventKind` += `"advance.ok"`, `"advance.blocked"`, `"session.end"` (the three S9a §8.4 reserved). |
| 9  | `src/ehr_simulator/db/__init__.py` | MODIFIED. Re-export `progress`. |
| 10 | `src/ehr_simulator/web/study_session.py` | MODIFIED. NEW `@dataclass(frozen=True) Frontier(unlocked_t_index: int, completed: bool)` + `read_frontier(conn, app_state, *, clinician_id, patient_id) -> Frontier` — the **read-only** half, split out so the GET gate can decide without writing anything (**[review-fix R21]**). `SessionContext` += `frontier: Frontier`; `bootstrap_session(..., frontier)` takes it rather than re-reading. The clamp guard reads `getattr(app_state, "study_timepoints", None)` and skips when absent (**[review-fix R19]** — `tests/test_study_session.py:11-13`'s `_AppState` stub defines only `write_counter` and `config_hash`, so the draft's unconditional `len(app_state.study_timepoints)` raises `AttributeError` in both existing `bootstrap_session` tests; the stub also gains the attribute). `read_frontier` reads `progress.fetch` **before** the session lookup (**[review-fix R12]**: when `completed_at IS NOT NULL` and no session is open, reuse `sessions.find_latest` instead of inserting — the final advance closed the session and no later advance can close a new one, so every read-only revisit would otherwise leak an un-closeable `sessions` row plus a `session.start` event into S10's dwell-time query; falls back to `start_or_resume` when the pair has no session row at all). Missing progress row → `(0, False)`. Two guards, both WARNING-only: `unlocked_t_index >= len(app_state.study_timepoints)` → clamp to the last index + `progress.clamped` (a study whose timepoint list shrank mid-pilot must not 500); `row.config_hash != live` → `progress.config_hash.drift` (same pattern as S9a §8.5). |
| 11 | `src/ehr_simulator/web/gating.py` | NEW. The service layer for everything S9b decides: `completeness(questions, saved) -> Completeness`; `pane_mode(frontier, t_index) -> PaneMode`; `is_viewable(frontier, t_index) -> bool` (**[review-fix R21]** — they take a `Frontier`, not a `SessionContext`: the gate must be decidable before any session row exists); `advance(conn, app_state, *, ctx, clinician_id, patient_id, t_index, timepoints, questions, client_ts, client_seq) -> AdvanceResult`; **[review-fix R7]** `progress_overview(conn, *, clinician_id, patient_ids, timepoint_count) -> dict[str, PatientProgress]` — the index page and the summary card's patient jumper both need a per-patient frontier, and §13 forbids `routes.py` from importing `progress` directly, so the read lives here. See §8. |
| 12 | `src/ehr_simulator/web/routes.py` | MODIFIED. (a) `_htmx_aware_redirect(request, url) -> Response` extracted from `_require_clinician` (pure extraction; the login redirect keeps its bytes). (b) GET `patient_timepoint` restructured: resolve → **bind request context** (**[review-fix R10]**) → **bootstrap + gate before any slicing** → render; the render body moves to `_render_patient_view(...)` so `/advance` reuses it byte-for-byte; HTMX partial responses gain `HX-Push-Url`, except history-restore requests, which get the full document (**[review-fix R6]**). (c) POST `/answer`: refuses when `pane_mode != "open"` (409 `Timepoint locked`); 200 responses append the advance CTA **out-of-band**. **[review-fix R18]** `tests/test_answer_routes.py` posts to `t_index=1` by default (`:22`), so this line invalidates ~12 existing tests — see §12. (d) NEW `POST /patient/{pid}/timepoint/{t_index}/advance` — re-renders through `_render_patient_view` with a **post-write** context (**[review-fix R8]**: `dataclasses.replace(ctx, unlocked_t_index=result.unlocked_t_index, completed=…)`; the `ctx` from `bootstrap_session` still holds the pre-advance frontier, so reusing it verbatim would render the newly unlocked timepoint as *locked*). (e) `/` index: progress markers + resume links in study mode. (g) **[review-fix R24]** non-HTMX (`HX-Request` absent) requests to `/advance` answer with POST-redirect-GET 303s instead of fragments — the CTA is a real `<form action method=post>` and must not dead-end at 405 when htmx fails to load. (f) `_render_summary` takes `timepoint_count` + `resume_t_index` (jumper links resume at each patient's frontier; Next omitted at the frontier). **[review-fix R14]** `resume_t_index` is **total** over `all_patient_ids` — Jinja's default `Undefined` renders a missing key as the empty string, which would emit `/patient/x/timepoint/?chrome=epic` and 404. See §5. |
| 13 | `src/ehr_simulator/web/app.py` | MODIFIED (3 lines). `app_from_study_config` logs `questions.none_required` WARNING when no question is `required` — the gate is vacuous and the researcher should know. |
| 14 | `src/ehr_simulator/web/templates/_questions_pane.html` | MODIFIED. Takes `mode ∈ {open, locked}`, `completed`, `unlocked_t_index`, `timepoint_count`, `remaining`, `is_last`, `chrome`. `locked`: `<fieldset disabled>` on every question, no `hx-*` attributes on the forms (nothing can fire), a `role="note"` lock note, footer with a resume link (or the "Patient complete" note + `← All patients`). `open`: unchanged forms + `{% include "_advance_cta.html" %}` in a `<footer class="pane-footer">`. |
| 15 | `src/ehr_simulator/web/templates/_advance_cta.html` | NEW. `<form id="advance-form" class="advance-cta [is-blocked]" data-remaining=N [data-first-unanswered=qid] [hx-swap-oob="true"] method="post" action="/patient/{pid}/timepoint/{t}/advance?chrome=c" hx-post=… hx-target="#patient-view" hx-swap="outerHTML" hx-sync="this:drop">` (**[review-fix R24]** — `action`+`method` so a submit without htmx hits the real route; the draft's `hx-post`-only form would POST to the current URL, which has no POST handler, and hand the clinician a blank 405. §7's resume link already bothers to carry a real `href`; the one *forward* control must too) + `<button type="submit" id="advance-btn" [aria-disabled="true" aria-describedby="advance-hint"]>` + hint `<p id="advance-hint">`. Rendered three ways: inline in the open pane, OOB inside `/answer` 200 responses, and as the `/advance` 409 body. See §7. |
| 16 | `src/ehr_simulator/web/templates/_summary_card.html` | MODIFIED. `total` comes from `timepoint_count` (study timepoints) instead of `patient_slice.timepoints|length` (dataset timepoints — identical on synthetic, wrong on Geneva; see §14). **[review-fix R4]** the template uses the dataset-derived length **twice** — `{% set total = … %}` on line 1 *and* the `summary-time` span (`_summary_card.html:46`, `timepoint {{ … + 1 }} of {{ patient_slice.timepoints|length }}`); both route through `total`. Next button rendered only when `show_next` (false at the frontier in study mode: the pane CTA is the one forward path). Patient-jumper links go to `/timepoint/{{ resume_t_index.get(pid, 0) }}` (**[review-fix R14]** — `.get`, not `[]`). |
| 17 | `src/ehr_simulator/web/templates/_patient_view.html` | MODIFIED. `data-t-count="{{ timepoint_count }}"` (same fix as #16; `keyboard.js` reads it for the boundary flash). |
| 18 | `src/ehr_simulator/web/templates/index.html` | MODIFIED. In study mode each row shows a progress marker (`not started` / `in progress · t k/N` / `complete ✓`) and its links resume at the frontier. Non-study rows unchanged. |
| 19 | `src/ehr_simulator/web/static/advance.js` | NEW. (a) `htmx:beforeSwap` for requests issued by `#advance-form`: force the swap on 409 (body carries `id="advance-form"`) and 412 (body carries `id="patient-view"`); anything else ≥400 writes a local `Could not advance — retry` hint. (b) `htmx:afterSwap`, **scoped to 409 responses whose `e.detail.requestConfig.elt` is `#advance-form`** (**[review-fix R3]**): when the swapped-in `#advance-form` carries `data-first-unanswered`, scroll that question into view (`prefers-reduced-motion` → `auto`), focus its first control, flash `.is-highlighted` for `HIGHLIGHT_MS`. The scoping is load-bearing, not tidiness: htmx 2.0.4 pushes OOB-swapped nodes into the **shared** `settleInfo.elts` and fires `htmx:afterSwap` on every element in it, so an unscoped handler would scroll + steal focus on *every* `/answer` 200 that still has unanswered questions — including the 1.5 s free-text autosave, which would yank the caret out of the textarea mid-sentence and break `tests/e2e/test_answer_walk.py::test_free_text_autosaves_without_blur:72`. (c) `htmx:sendError` → same local hint. (d) **[review-fix R17]** `htmx:configRequest` stamps `client_ts`/`client_seq` on `#advance-form` requests using the same per-tab counter `answers.js` owns (exported through a shared `ehrsim` namespace or duplicated constant — `answers.js`'s listener matches `form.question` only and must stay that way). Zero inline JS. |
| 20 | `src/ehr_simulator/web/static/keyboard.js` | MODIFIED (one branch). `navigate(+1)`: if `#advance-btn` exists, `.click()` it and return — at the frontier `]` *is* the advance CTA (blocked or not); otherwise the S2 `hx-get` path is unchanged. **[review-fix R5]** the branch goes **first**, above the `next >= state.tCount` boundary check (`keyboard.js:67`): on the last timepoint `next == tCount`, so an advance branch placed after it would flash `Already at last timepoint` and make `Finish patient ✓` unreachable from the keyboard. |
| 21 | `src/ehr_simulator/web/templates/base.html` | MODIFIED. `<script src="/static/advance.js" defer>` after `answers.js`. **[review-fix R6]** `<body hx-history="false">`: `HX-Push-Url` switches htmx's history machinery on, and its default behavior is to snapshot the rendered body — charts, labs, admission facts — into `localStorage` on every push. `hx-history="false"` keeps the URL push and the `replaceState`, skips the cache write, and routes every back-button restore through the server (see #12b). |
| 22 | `src/ehr_simulator/web/static/theme.css` | MODIFIED. `.pane-footer`, `.advance-cta` + `.advance-cta.is-blocked` (**[review-fix R27]** — §7's markup puts `is-blocked` on the *form*, not the button; the draft's #22 said `.advance-btn` + `.is-blocked`), `.advance-btn` (muted under a blocked parent, `cursor: not-allowed`, still focusable), `.advance-hint`, `.pane-lock-note`, `.question.is-highlighted` (outline pulse), `fieldset[disabled]` opacity, `.progress-marker` variants on the index. Existing tokens only. |
| 23 | `src/ehr_simulator/cli_support.py` | MODIFIED. `render_html_for_preview` unlocks step-wise: before rendering `t_index=idx` it calls `progress.unlock(app.state.db, …, to_t_index=idx, …)` for the preview clinician, so every `--html-out` file shows the **open** pane the clinician would see. **[review-fix R23]** the draft's justification ("without it, `t1`/`t2` would be 303s and the CLI test would fail on `raise_for_status`") is false: `TestClient` is constructed with `follow_redirects=True` (`cli_support.py:295`), so an ungated preview silently writes the **t=0 body** into `synth_001_t1.html` and `_t2.html` — `raise_for_status()` passes, and §10's `grep 'id="advance-form"'` passes too, because the t=0 body has a CTA. The guard only exists if the preview client passes `follow_redirects=False` (a gate redirect then fails loudly) **and** the assertions key on `data-t-index="{idx}"`, not on the pane's presence. Same layer that already seeds the clinician row. **[review-fix R13]** delete the redundant second id computation at `cli_support.py:285` (`hashlib.sha256(b"dr. preview")…`) and use `lookup_or_create`'s return value: the two agree today, but the preview's unlock now has to write a `progress` row for *the same* id the cookie carries, and a silent divergence would turn every `--html-out` file into a redirect body. |
| 24 | `tests/conftest.py` | EXTENDED. `answer_all_required(client, patient_id, t_index)` helper: POSTs one valid value per `required` question, **derived from the running `app.state.questions`** rather than a hard-coded id/value list (**[review-fix R15]** — a hard-coded list silently under-answers the moment `tests/fixtures/study/questions.yaml` gains a question, and every advance test would flip to `blocked` with a confusing diff; a per-`response_type` value picker plus an assertion that the helper covered exactly the required set makes the drift loud); `seed_progress(db_path, clinician_id, patient_id, unlocked_t_index, completed=False)` helper for gate tests that must not go through `/advance`. |
| 25 | `tests/test_config.py` | EXTENDED (+4 functions, +1 param case). |
| 26 | `tests/test_db.py` | EXTENDED (+9, **4 adapted** — the schema snapshot plus three hard-coded migration-version assertions, **[review-fix R1]**). |
| 27 | `tests/test_gating.py` | NEW (9). |
| 28 | `tests/test_study_session.py` | EXTENDED (+3, 2 adapted — `_AppState` gains `study_timepoints`, **[review-fix R19]**). |
| 29 | `tests/test_answer_routes.py` | EXTENDED (+5, **module re-pointed to `T_INDEX = 0`; ~12 existing tests adapted** — **[review-fix R18]**, see §12). |
| 30 | `tests/test_advance_routes.py` | NEW (21). |
| 31 | `tests/test_app.py` | MODIFIED (+1, 1 adapted — **[review-fix R2]** the adaptation must pass `follow_redirects=False`; `TestClient` follows redirects by default, so the gate would make the existing assertions pass while silently testing t=0. §12. |
| 32 | `tests/test_a11y.py`, `tests/test_csp.py` | EXTENDED (+1 each). |
| 33 | `tests/test_cli.py` | MODIFIED (preview assertion grows). |
| 34 | `tests/e2e/test_answer_walk.py` | MODIFIED (1 adapted) + NEW `tests/e2e/test_gated_walk.py` (2). |
| 35 | `.github/workflows/ci.yml` | EXTENDED. `CLI smoke`: `grep -q 'data-t-index="2"' …/synth_001_t2.html` **and** `grep -q 'id="advance-form"' …/synth_001_t2.html` (**[review-fix R23]** — the second grep alone cannot fail). |
| 36 | `TODOS.md` | MODIFIED. Strike the two S9b prerequisites (closed). Add §14 items. |
| 37 | `specs/ROADMAP.md` | MODIFIED (S9b block). Route-shape pointer (§5.1) and the "GET gate + frozen answers" scope growth, one line each. |
| 38 | `src/ehr_simulator/cli.py`, `src/ehr_simulator/cli_support.py`, `src/ehr_simulator/db/progress.py`, `src/ehr_simulator/db/answers.py`, `src/ehr_simulator/db/events.py` | **Owner decision (b): shipped, not deferred.** NEW `ehr-simulator reset-progress STUDY_CONFIG --clinician NAME --patient PID [--to-t-index N=0] [--db-path P]`. `cli_support.reset_progress(conn, *, clinician_name, patient_id, to_t_index, timepoints) -> ResetReport(previous_unlocked, deleted_answers)`: resolves the name the way `/login` does (no row → exit 1), `progress.reset` (sets `unlocked_t_index = N`, `completed_at = NULL`; rowcount 0 when the pair has no row → exit 1), `answers.delete_after(min_timepoint_exclusive = timepoints[N])` (answers **at** N survive and pre-fill the re-opened pane), one `progress.reset` event (`EventKind` += `"progress.reset"`, payload `{from_t_index, to_t_index, deleted_answers}`, `session_id=None`). `--to-t-index` beyond the study's last index → exit 1. The study config is required because index → minutes needs `timepoints_minutes`. |

`README.md` and `CLAUDE.md` "Current state" are owned by `/document-release` post-ship (README's *"You can still move to the next timepoint…"* paragraph and the two "not in this build" bullets become false).

---

## 3. Repo layout after Session 9b (diff vs end-of-S9a)

```
ehr_simulator/
├── configs/example_questions.yaml               # MODIFIED (required: false + authoring rules)
├── src/ehr_simulator/
│   ├── cli_support.py                           # MODIFIED (preview: step-wise unlock)
│   ├── config/questions.py                      # MODIFIED (+required)
│   ├── db/
│   │   ├── __init__.py                          # MODIFIED (+progress)
│   │   ├── events.py                            # MODIFIED (+3 kinds)
│   │   ├── migrations.py                        # MODIFIED (migration 3: progress)
│   │   ├── progress.py                          # NEW (DAO)
│   │   └── sessions.py                          # MODIFIED (+close)
│   └── web/
│       ├── app.py                               # MODIFIED (none_required warning)
│       ├── gating.py                            # NEW (service)
│       ├── routes.py                            # MODIFIED (gate, /advance, OOB CTA, index)
│       ├── study_session.py                     # MODIFIED (+unlocked_t_index, +completed)
│       ├── static/
│       │   ├── advance.js                       # NEW
│       │   ├── keyboard.js                      # MODIFIED (] → #advance-btn)
│       │   └── theme.css                        # MODIFIED
│       └── templates/
│           ├── _advance_cta.html                # NEW
│           ├── _patient_view.html               # MODIFIED (timepoint_count)
│           ├── _questions_pane.html             # MODIFIED (mode, footer)
│           ├── _summary_card.html               # MODIFIED (show_next, resume links, total)
│           ├── base.html                        # MODIFIED (+script)
│           └── index.html                       # MODIFIED (progress)
├── tests/
│   ├── conftest.py                              # MODIFIED (+2 helpers)
│   ├── e2e/test_answer_walk.py                  # MODIFIED (] now hits /advance)
│   ├── e2e/test_gated_walk.py                   # NEW
│   ├── fixtures/db/migration_001_expected_schema.sql   # MODIFIED
│   ├── fixtures/study/questions.yaml            # MODIFIED (free_notes required: false)
│   ├── test_advance_routes.py                   # NEW
│   ├── test_gating.py                           # NEW
│   └── test_{a11y,answer_routes,app,cli,config,csp,db,study_session}.py   # MODIFIED
├── .github/workflows/ci.yml                     # MODIFIED
├── specs/ROADMAP.md                             # MODIFIED
└── TODOS.md                                     # MODIFIED
```

**Migration 3 — `progress`:**

```sql
CREATE TABLE IF NOT EXISTS progress (
    clinician_id      TEXT NOT NULL REFERENCES clinicians(clinician_id),
    patient_id        TEXT NOT NULL,
    unlocked_t_index  INTEGER NOT NULL DEFAULT 0,   -- highest t_index the clinician may view
    completed_at      TIMESTAMP,                    -- set once by the final advance
    config_hash       TEXT NOT NULL,
    updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (clinician_id, patient_id)
);
```

Why a table and not a column on `sessions`: progress spans sessions. The final advance closes the session (`ended_at`), and S9a's `bootstrap_session` opens a fresh one on the next visit — a `sessions.unlocked_t_index` would reset to 0 exactly when the clinician comes back to review a finished patient. `arm_assignments` is per pair too, but mixing walk state into the randomization record is the wrong join for S10/S11. The table is tiny (one row per pair, ≤ patients × clinicians) and the PK serves every query.

Why `unlocked_t_index` (URL ordinal) and not minutes: the gate compares against `t_index` in the path, `HX-Push-Url` and every redirect carry `t_index`, and `study.timepoints` is validated sorted-unique so the ordinal is stable within one `config_hash`. The row carries `config_hash`; drift is detected at read time (deliverable #10). Mid-pilot timepoint edits are a pre-registration violation (design-doc D8) — the guard's job is *not to 500*, not to reconcile.

**DAO SQL (spec, not literal):**

```sql
-- unlock: compare-and-set on the frontier the caller observed (review-fix R29).
-- Monotonic by construction: to_t_index is always from_t_index + 1.
UPDATE progress
   SET unlocked_t_index = :to, updated_at = CURRENT_TIMESTAMP
 WHERE clinician_id = :cid AND patient_id = :pid
   AND unlocked_t_index = :from;                       -- ← the guard
-- rowcount 0 with :from == 0 means "no row yet", not "someone raced me":
INSERT OR IGNORE INTO progress (clinician_id, patient_id, unlocked_t_index, config_hash)
VALUES (:cid, :pid, :to, :hash);                       -- config_hash written here ONLY (review-fix R11)

-- mark_complete: set-once
INSERT INTO progress (clinician_id, patient_id, unlocked_t_index, completed_at, config_hash)
VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)
ON CONFLICT(clinician_id, patient_id) DO UPDATE SET
    completed_at = COALESCE(progress.completed_at, CURRENT_TIMESTAMP),
    updated_at   = CURRENT_TIMESTAMP;
```

`MAX` is the two-argument scalar form. `completed_at` is read back as `datetime` under `PARSE_DECLTYPES` (S9a §8.3) — `CURRENT_TIMESTAMP` writes `YYYY-MM-DD HH:MM:SS`, which the default converter parses with `microseconds = 0`. Both statements, the two-arg `MAX`, `COALESCE(progress.completed_at, …)` against the existing row, the FK on `clinician_id` and the PK rejection were executed against SQLite 3.37 during review and behave as written.

**[review-fix R29] — the frontier guard is in SQL, not in the event loop.** The draft's `MAX(…)` upsert is monotonic but not idempotent-safe: it happily accepts *any* forward jump, so correctness rested entirely on "no `await` runs between the progress read and the progress write". That property is invisible, untested, and one refactor away from silently vanishing — and R17 now puts an `await request.form()` in the same handler (hoisted above `bootstrap_session`, but the next person may not know why). `UPDATE … WHERE unlocked_t_index = :from` makes a double-advance impossible at the storage layer whatever the handler does: the second writer's guard does not match, `unlock` returns `False`, and `gating.advance` degrades it to `stale` — the same outcome the `t_index != unlocked` check produces, reached without depending on scheduler behavior. Verified against SQLite 3.37 (`UPDATE` rowcount 1 → 0 → 1 → 0 across a replayed advance). Test #31 keeps its meaning and #6c adds the DAO-level case.

**[review-fix R11] — why `unlock` leaves `config_hash` alone.** The draft wrote `config_hash = excluded.config_hash` on conflict. That is the live hash, so after the very first advance the row's hash always equals the running config and §8.2's `progress.config_hash.drift` WARNING can never fire again — the drift detector would silently disarm itself one advance into the pilot. The row now records the hash the walk *started* under, which is the thing a mid-pilot config edit has to be compared against. Test #6b.

---

## 4. Data flow

```
 GET /patient/{pid}/timepoint/{t}?chrome=c            POST …/timepoint/{t}/advance?chrome=c
        │                                                    │
 _require_clinician ──miss──▶ 303 / HX-Redirect              _require_clinician ──miss──▶ 303 / HX-Redirect
        │                                                    │
 _resolve_timepoint ──bad──▶ 404 error-flash                 study is None ──▶ 409 error-flash
        │                                                    │
 study is None? ──yes──▶ render (no gate, no pane)           _resolve_timepoint ──bad──▶ 404 error-flash
        │ no                                                 │
 read_frontier  (PURE READ — review-fix R21)                 bootstrap_session ─▶ ctx
        │                                                    │
 t > frontier.unlocked? ─yes─▶ WARNING gate.redirect         gating.advance(t, timepoints, questions)
        │ no                303 / HX-Redirect → frontier        │
        │                   (no session, no arm, no slice)      │
 bootstrap_session ─▶ ctx(frontier)                             │
        │                                                       │
        ▼                  (NO slicing happened)                ├─ t != unlocked or completed
 _render_patient_view                                           │        └─▶ "stale": 412 + frontier view + HX-Push-Url
   slice_to_timepoint (data ≤ t)                                ├─ remaining ≠ ∅
   panels · summary(show_next, resume links) · chrome           │        └─▶ events advance.blocked
   pane(mode = open | locked, remaining, is_last)               │            409 + _advance_cta.html
        │                                                       │            HX-Retarget #advance-form · HX-Reswap outerHTML
   htmx? ─▶ 200 partial + HX-Push-Url                           ├─ t == last
   restore? ─▶ 200 full document (review-fix R6)                 │
   else  ─▶ 200 base.html                                       │        └─▶ progress.mark_complete · sessions.close
                                                                │            events advance.ok(final) + session.end
 POST …/timepoint/{t}/answer                                    │            "finished": HX ─▶ 200 + HX-Redirect /
        │ (S9a preamble unchanged)                              │                        plain ─▶ 303 /
 bootstrap_session ─▶ ctx                                       └─ else
        │                                                                └─▶ progress.unlock(t+1) · events advance.ok
 pane_mode(ctx.frontier, t) != open ─▶ 409 "Timepoint locked"                        "advanced": 200 + view(t+1) + HX-Push-Url
        │ open
 record_answer (S9a)
        │
 200 badge + OOB _advance_cta.html (remaining recomputed)
```

Layering (new code only):

```
 routes.py ──▶ web/gating.py, web/study_session.py, web/answer_capture.py ──▶ db/progress, db/sessions, db/events, db/answers ──▶ sqlite3
```

`gating.py` imports `answer_capture.saved_answers` (same layer) — completeness is defined on the same mapping the pane pre-fills from, so the two can never disagree about what "answered" means.

---

## 5. Route contracts

### 5.1 `POST /patient/{patient_id}/timepoint/{t_index}/advance?chrome=`

`t_index` **is** the ROADMAP's "optimistic `expected_timepoint`": the timepoint the client believes it is leaving. The server compares it to `ctx.frontier.unlocked_t_index`; a mismatch is a stale client (second tab, double-click, back-button) and is answered with the truth, never with a write. Nested under the patient URL for the S9a §5.1 reasons (shared preamble, self-describing logs); the ROADMAP's flat `/advance` was a placeholder, recorded in §14.

`chrome` query param so the rendered view and `HX-Push-Url` keep the chrome variant. `async def` (S9a R11 — the shared `sqlite3.Connection` is only safe because handlers serialize on the event loop; this is what makes the progress check-then-write race-free without a transaction).

**[review-fix R17] — the body is not empty, and the `await` position is load-bearing.** `/advance` carries `client_ts` + `client_seq` like every `/answer` POST does. `events.client_ts`/`client_seq` exist and would otherwise be NULL on exactly the rows S10 reads for per-timepoint dwell time (§8.3), leaving no way to separate think time from render/network latency, and `advance.blocked` is itself a behavioral signal worth timestamping client-side. The cost is one `await request.form()`, which **must be hoisted above `bootstrap_session`** — the same order `routes.py:396` vs `:421` already uses on `/answer`. The no-transaction argument above holds only while there is no `await` between the progress read inside `bootstrap_session` and the progress write inside `gating.advance`; an `await` placed between them re-opens the check-then-write race the spec leans on. §13 states the rule so a later refactor cannot quietly break it.

| Outcome | Condition | Status | Body | Headers | Writes |
|---|---|---|---|---|---|
| **advanced** | `t == unlocked`, not completed, remaining = ∅, `t < last` | HX: 200 · plain: 303 | `#patient-view` for `t+1` (open pane — rendered with the **post-write** ctx, **[review-fix R8]**) · plain: empty | `HX-Push-Url: …/timepoint/{t+1}?chrome=c` · `Location: …/timepoint/{t+1}?chrome=c` | `progress.unlock(t → t+1)`; `events advance.ok` |
| **finished** | same, `t == last` | HX: 200 · plain: 303 | empty | `HX-Redirect: /` · `Location: /` | `progress.mark_complete`; `sessions.close`; `events advance.ok(final)`, `events session.end` |
| **blocked** | `t == unlocked`, not completed, remaining ≠ ∅ | HX: 409 · plain: 303 | `_advance_cta.html` (`data-remaining`, `data-first-unanswered`) · plain: empty | `HX-Retarget: #advance-form`, `HX-Reswap: outerHTML` · `Location: …/timepoint/{t}?chrome=c` | `events advance.blocked` |
| **stale** | `t != unlocked`, completed, **or** `progress.unlock` CAS miss (**[review-fix R29]**) | HX: 412 · plain: 303 | `#patient-view` for `unlocked` · plain: empty | `HX-Push-Url: …/timepoint/{unlocked}?chrome=c` · `Location: …/timepoint/{unlocked}?chrome=c` | none (WARNING `advance.stale`) |
| no study | `app.state.study is None` | 409 | `error-flash` `No questions configured …` | — | none |
| bad target | `_resolve_timepoint` fails | 404 | `error-flash` (S2 message strings) | — | none |
| no cookie | S6 preamble | 303 / 200+`HX-Redirect` | — | — | none |

**[review-fix R24] — plain-browser rows.** The draft defined a non-HTMX answer only for *finished*; the CTA is now a real `<form action method="post">` (#15) so every outcome needs one, and a bare 409/412 fragment rendered as a whole page is not it. Without htmx, the browser POSTs, the server writes, and the answer is POST-redirect-GET: *advanced* → the next timepoint, *blocked* → back to the current one (the freshly rendered pane carries the same remaining-count and hint the fragment would have; only the scroll-and-focus is lost, and that was always JS-only), *stale* → the frontier. Same writes, same events, same gate — one branch on `HX-Request` at the end of the handler. Tests #26c/#28b/#30b.

Status choice: **409** for *blocked* (the resource's state — unanswered required questions — conflicts with the request), **412** for *stale* (the request's precondition, `t_index == unlocked`, failed: the classic optimistic-concurrency code). The two bodies target different elements, so `advance.js` keys its force-swap on the status + a body marker, never on copy text.

Double-submit: `hx-sync="this:drop"` on the form drops clicks while one request is in flight (verified in the vendored htmx 2.0.4: `drop` returns early whenever the sync element already owns an XHR); a second request that still gets through (keyboard `]` + click in the same frame, or a second tab) arrives with `t_index` one behind the bumped frontier → **stale**, no second unlock. Test #31 is the regression.

**[review-fix R8] — where the re-rendered view's context comes from.** `SessionContext` is frozen and `bootstrap_session` ran *before* the write, so it still carries the pre-advance frontier. Re-rendering `t+1` with it makes `pane_mode(ctx.frontier, t+1)` return `locked` (`t_index != frontier.unlocked_t_index`) and the clinician lands on a read-only pane with no CTA — a dead end one click into the walk. The handler renders with `dataclasses.replace(ctx, frontier=Frontier(result.unlocked_t_index, completed))`. A second `bootstrap_session` call would also work but costs three more SELECTs and re-reads state the service already returned; test #29 asserts the open pane either way.

**[review-fix R2] — asserting a redirect in these tests.** Starlette's `TestClient` is built with `follow_redirects=True`, so `client.get(<gated url>)` transparently follows the 303 and returns the frontier's 200. Every test in §9 that asserts a 3xx status or a `Location` header passes `follow_redirects=False`; §13 makes it a convention.

### 5.2 `GET /patient/{patient_id}/timepoint/{t_index}` (growth)

New shape of the handler body, in this order:

```python
resolved, message = _resolve_timepoint(request, patient_id, t_index)
if resolved is None: return HTMLResponse(_error_flash(message), 404)

# [review-fix R10] bound BEFORE the gate: a redirected request must still be
# attributable in logs/current.jsonl, and the middleware's per-request line
# reads these contextvars after the handler returns.
update_request_context(patient_id=…, timepoint=…, timepoint_index=…, chrome=…)

ctx = None
if state.study is not None:
    # [review-fix R21] the gate decides on a pure read. bootstrap_session
    # WRITES (arm lock + sessions row + session.start event) and must not
    # run for a request we are about to bounce.
    frontier = read_frontier(state.db, state, clinician_id=…, patient_id=…)
    if not is_viewable(frontier, t_index):                  # t_index > frontier.unlocked_t_index
        log.warning("gate.redirect", event_kind="gate.redirect", requested=t_index, unlocked=frontier.unlocked_t_index)
        return _htmx_aware_redirect(request, _timepoint_url(patient_id, frontier.unlocked_t_index, chrome))
    ctx = bootstrap_session(state.db, state, clinician_id=…, patient_id=…, frontier=frontier)
    update_request_context(arm=ctx.arm)

inner = _render_patient_view(request, clinician_id=…, patient_id=…, t_index=…, chrome=…, resolved=resolved, ctx=ctx)

# [review-fix R6] a history restore is an HX request that wants a whole document.
if request.headers.get("hx-history-restore-request", "").lower() == "true":
    return templates.TemplateResponse(request, "base.html", {…, "inner": inner})
if is_htmx:
    return HTMLResponse(inner, headers={"HX-Push-Url": _timepoint_url(patient_id, t_index, chrome)})
return templates.TemplateResponse(request, "base.html", {…, "inner": inner})
```

**[review-fix R10].** The draft bound `patient_id` / `timepoint` / `chrome` *after* the study branch, so a gate redirect produced a `gate.redirect` WARNING and a middleware `request` line with no patient and no timepoint — unattributable, in the one log line §14 wants to promote to an `events` row for S10's "tried to peek" signal. `resolved` is already in hand at that point, so moving the binding up costs nothing. Test #26b.

**[review-fix R6] — `HX-Push-Url` turns htmx's history machinery on, and the route has to hold up its end.** Two consequences the draft did not account for, both verified in the vendored htmx 2.0.4:

1. **Back-button on a cache miss re-requests the URL as a full page.** htmx caches at most a handful of snapshots; on a miss it issues `GET <pushed url>` with `HX-Request: true` **and** `HX-History-Restore-Request: true`, then `innerHTML`-swaps the **whole `<body>`** with whatever comes back. Today the route answers any `HX-Request` with the bare partial, so a restore would leave the document as a naked `#patient-view`: no shortcut overlay, no `hx-history` marker, nothing below `<body>`. The branch above answers restore requests with `base.html` (and no `HX-Push-Url` — htmx is restoring, not navigating). Test #40b.
2. **Snapshots land in `localStorage`.** The cached body is the rendered chart SVGs, labs table and admission facts — patient data persisted outside SQLite, outside the backup path and outside anything the S9c pseudonymization policy covers, on a machine several clinicians share. `hx-history="false"` on `<body>` (#21) suppresses the cache write while keeping the push, which also means *every* restore goes down path 1. Asserted in test #43.

**The gate runs before `slice_to_timepoint`.** A redirect response contains no patient data — not a panel, not a summary count. This is the S8 "data ≤ t" invariant lifted to "data ≤ frontier".

**[review-fix R20] — the body assertion does not test that.** The draft locked the invariant with "test #26 asserts the body has neither `<svg` nor `id=\"patient-view\"`". Both redirect shapes — Starlette's `RedirectResponse(303)` and the S6 `Response(200, headers={"HX-Redirect": …})` — have **empty bodies by construction**, so that assertion passes whether or not `slice_to_timepoint` ran first, and §13's "any future refactor that reorders them fails test #26" is false. Test #26 installs a `monkeypatch` counter on `routes.slice_to_timepoint` and asserts it was called **zero** times; the body assertions stay as a cheap second line.

**[review-fix R21] — the gate must not write either.** `bootstrap_session` is not a read: `study_session.py:47-65` calls `arm_assignments.assign_or_lookup` (which **permanently locks** the S11 randomization cell for the pair), inserts a `sessions` row and appends a `session.start` event. Running it before `is_viewable` means a bookmark, a stale second tab or a typed URL pointing past the frontier burns the arm assignment and starts the session clock for a patient the clinician never actually opened — and §14 already flags arm-locking-on-GET as the S11 prerequisite, so the draft was adding two *new* triggers for it (the gate and the index resume links) while filing the problem as future work. The read/write split (#10) fixes it; test #26 asserts zero rows in `progress`, `sessions` **and** `arm_assignments`, and zero `session.start` events, not just the `progress` row the draft checked.

The redirect is the S6 shape (303 for browsers, 200 + `HX-Redirect` for HTMX) because the request only ever arrives from a typed URL, a stale tab, or a bookmark — the pane and keyboard never issue it (§7). A full-page load to the frontier is the right recovery for all three.

`HX-Push-Url` on partial responses is new: S2's `hx-get` buttons and `keyboard.js` never pushed history, so the address bar sat on whatever URL the page loaded with. Harmless when every timepoint was reachable; with a gate, a reload from a stale URL lands the clinician on a *locked* pane and the first thing they see is "Locked — answered before you advanced". The header is one line per response and covers the Prev button, `]`/`[`, the resume link and `/advance` uniformly.

`_render_patient_view` is a pure extraction of today's slice → panels → summary → chrome → pane → `_patient_view.html` block, plus the new render inputs: `timepoint_count = len(resolved.timepoints)`, `show_next`, `resume_t_index`, pane `mode`/`remaining`/`is_last`. Non-study behavior is byte-identical (`show_next` is the S2 `not at_last`; `resume_t_index` is all zeros; `timepoint_count` equals `len(patient_slice.timepoints)` because both derive from `patient_timepoints(dataset, pid)` without a study).

### 5.3 `POST /patient/{patient_id}/timepoint/{t_index}/answer` (growth)

After `bootstrap_session`, before `record_answer`:

| Condition | Status | `data-state` | Copy |
|---|---|---|---|
| `pane_mode(ctx.frontier, t_index) == "locked"` (t behind the frontier, t beyond it, or patient completed) | 409 | `error` | `Timepoint locked` |

The disabled fieldsets are a client courtesy; this line is the enforcement. No row is written, no event is emitted (test #22).

200 responses (`saved` / `cleared`) become **two fragments**: the S9a badge, followed by `_advance_cta.html` rendered with `oob=True` (`hx-swap-oob="true"` on the form root). htmx swaps the badge into the form's `.answer-status` as before and the CTA into `#advance-form` by id. Remaining is recomputed after the write (one `fetch_for_cell`). 4xx responses stay a bare badge — nothing changed, so nothing to refresh (test #24).

### 5.4 `GET /` (growth)

Study mode only: `progress.list_for_clinician` → per patient `{"state": "not_started" | "in_progress" | "complete", "unlocked_t_index", "timepoint_count"}`; links go to `/timepoint/{resume}` where `resume = unlocked_t_index` (a completed patient resumes at its last timepoint, read-only). Non-study: unchanged template branch.

---

## 6. Gating semantics

```
 t_index:        0        1        2        3          (timepoint_count = 4)
                 │        │        │        │
 unlocked = 2:   locked   locked   OPEN     ─ gate ─▶ 303 to t=2
                 read-only         editable  not viewable
                 no hx-*           CTA in    (slice never runs)
                 resume link ─────▶ footer

 completed:      locked   locked   locked   locked      every pane read-only, no CTA,
                                                         "Patient complete ✓ · ← All patients"
```

- **Viewable** ⇔ `t_index ≤ unlocked_t_index`.
- **Open** (editable, CTA shown) ⇔ `t_index == unlocked_t_index` **and not** `completed`.
- **Locked** ⇔ viewable and not open. Answers are shown pre-filled but every `fieldset` is `disabled`, the forms carry no `hx-post`/`hx-trigger`, and `POST /answer` refuses with 409.
- **Complete** ⇔ every question with `required: true` has a row in `answers` for the cell. Optional questions never block. Rows for question ids not in the running config are ignored (S9a `saved_answers` already drops them).
- **Remaining** = required question ids without a row, **in `questions.yaml` order** — the first one is the scroll target.
- **Advance** moves the frontier by exactly one. The last timepoint's advance sets `completed_at` and closes the session instead.

**Why answers freeze on advance.** The study measures what a clinician believed at *T* given data ≤ *T*. Letting them revise t=60 after reading t=180 is the same contamination as showing t=180 data early — only the direction differs. `plan.md`'s workflow is forward-only, and the ROADMAP's "all answered before advancing" is a commit semantics, not a hint. The cost is that a mis-advance cannot be undone by the clinician; the CTA label is unambiguous (`Next timepoint ›` only ever appears when everything required is answered, `Finish patient ✓` on the last), the blocked state is a different label and visual, and an admin unlock path is filed in §14 with a revival criterion. A confirm dialog was considered and rejected (§15).

**Why progress is stored, not derived from `answers`.** Deriving "first incomplete timepoint" from rows fails on both sides: before the explicit advance it would unlock the next timepoint the moment the last radio is clicked (skipping the `advance.ok` event and the dwell-time signal S10 wants), and it can never express "frozen" — clearing an answer at t=0 would silently re-lock t=1 under the clinician's feet. The frontier is an *act*, and acts get rows.

---

## 7. Pane UI

```
 open (t == unlocked)                          locked (t < unlocked)
 ┌────────────────────────────────┐            ┌────────────────────────────────┐
 │ Questions · t = 60 min         │            │ Questions · t = 0 min          │
 │ ┌────────────────────────────┐ │            │ ┌────────────────────────────┐ │
 │ │ 1. Will the patient …      │ │            │ │ 1. Will the patient …      │ │
 │ │  ( ) Yes (•) No ( ) Unknown│ │            │ │  ( ) Yes (•) No ( ) Unknown│ │  ← disabled
 │ │                   Saved ✓  │ │            │ │                   Saved ✓  │ │
 │ ├────────────────────────────┤ │            │ ├────────────────────────────┤ │
 │ │ 3. Probability of good …   │ │◀ scrolled  │ │ …                          │ │
 │ │   [    ] %                 │ │  + focused │ └────────────────────────────┘ │
 │ │                            │ │  on blocked│ ┃ Locked — answered before   ┃ │
 │ ├────────────────────────────┤ │  click     │ ┃ you advanced.              ┃ │
 │ │ …                          │ │            │ Go to current timepoint (2/3) →│
 │ └────────────────────────────┘ │            └────────────────────────────────┘
 │ ┌────────────────────────────┐ │
 │ │ Next timepoint · 5 unanswered │ ← aria-disabled, muted, clickable
 │ │ Answer the 5 remaining     │ │
 │ │ required questions to …    │ │
 │ └────────────────────────────┘ │
 └────────────────────────────────┘
   after the 6th required answer the OOB swap flips it to:
 │ ┌────────────────────────────┐ │        last timepoint:  ┌──────────────────┐
 │ │      Next timepoint ›      │ │                         │ Finish patient ✓ │
 │ └────────────────────────────┘ │                         └──────────────────┘
```

`_advance_cta.html` (spec, not literal):

```html
<form id="advance-form"
      class="advance-cta{% if remaining %} is-blocked{% endif %}"
      data-remaining="{{ remaining|length }}"
      {% if remaining %}data-first-unanswered="{{ remaining[0] }}"{% endif %}
      {% if oob %}hx-swap-oob="true"{% endif %}
      hx-post="/patient/{{ patient_id }}/timepoint/{{ t_index }}/advance?chrome={{ chrome }}"
      hx-target="#patient-view" hx-swap="outerHTML" hx-sync="this:drop">
  <button type="submit" id="advance-btn" class="advance-btn"
          {% if remaining %}aria-disabled="true" aria-describedby="advance-hint"{% endif %}>
    {%- if remaining %}Next timepoint · {{ remaining|length }} unanswered
    {%- elif is_last %}Finish patient ✓
    {%- else %}Next timepoint ›{% endif -%}
  </button>
  {% if remaining %}
  <p id="advance-hint" class="advance-hint">
    Answer the {{ remaining|length }} remaining required question{{ "s" if remaining|length != 1 }} to continue.
  </p>
  {% endif %}
</form>
```

- **`aria-disabled`, not `disabled`.** A `disabled` button swallows clicks, and the ROADMAP wants click-when-disabled to scroll + record an event. `aria-disabled` keeps the button focusable and clickable, announces "dimmed" to screen readers, and the `is-blocked` class carries the visual. Enter/Space on it take the same path as a click.
- **[review-fix R26] the htmx contract `advance.js` depends on, written down.** Verified in the vendored 2.0.4 bundle: `HX-Retarget` and `HX-Reswap` are applied to the response info **before** the default `shouldSwap` is computed from `responseHandling` (`[45].. → swap:false`) and **before** `htmx:beforeSwap` fires, so flipping `e.detail.shouldSwap = true` in the listener picks up the already-retargeted `detail.target`. But the event is *dispatched on the pre-retarget element* (`#patient-view`), so listeners must be delegated on `document.body` — as `answers.js` already is — and must filter on `e.detail.requestConfig.elt`, never on the node the event fired from. `htmx:afterSwap` is likewise fired for every element in the shared settle list, OOB inserts included (R3).
- **The blocked click goes to the server.** The client does not know what "complete" means (`required`, config drift, cleared rows) and must not guess; `POST /advance` is the one source of truth, and its 409 is what emits `advance.blocked` and returns the fresh remaining list. One code path, no client-side counter.
- **Scroll target.** `advance.js` reads `data-first-unanswered` from the swapped-in CTA and selects `form.question[data-question-id="…"]` (attribute selector — digit-leading ids are legal in `question_id`, S9a R4). `scrollIntoView({block: "center", behavior})` with `behavior = "auto"` under `prefers-reduced-motion: reduce`, focus the first `input:not([type=hidden]), textarea`, add `.is-highlighted` for `HIGHLIGHT_MS = 1500`. **[review-fix R3]** the handler fires only for `xhr.status === 409` on a request whose `e.detail.requestConfig.elt` is `#advance-form`. htmx 2.0.4 appends OOB-swapped nodes to the same `settleInfo.elts` list the primary swap uses and then fires `htmx:afterSwap` on every entry, so "did a fresh `#advance-form` with `data-first-unanswered` just appear?" is **also** true after every `/answer` 200 that leaves questions unanswered. Unscoped, the pane would scroll and re-focus on each keystroke-debounced free-text save — the caret leaves the textarea mid-sentence and `tests/e2e/test_answer_walk.py::test_free_text_autosaves_without_blur` (`document.activeElement.tagName == "TEXTAREA"`) fails. That existing test is the cheapest regression for this; e2e #44 asserts the intended direction.
- **Remaining-count stays live** through the OOB swap on every `/answer` 200. No JS counts badges; when the last required answer saves, the response itself carries `Next timepoint ›`.
- **`]` at the frontier is the CTA.** `keyboard.js::navigate(+1)` clicks `#advance-btn` when present — **[review-fix R5]** as the *first* statement of the function, before the `next >= state.tCount` boundary guard (`keyboard.js:67`). On the last timepoint `next == tCount`, so an advance branch placed after the guard would return with an `Already at last timepoint` flash and `Finish patient ✓` would be reachable only by mouse. A `<button type="submit">` with `aria-disabled` (not `disabled`) still dispatches `submit` on its form under `.click()`, and htmx's default trigger for a `<form>` is `submit`, so the click path and the keyboard path are the same request. Blocked → 409 → scroll. Complete → advance. On a locked timepoint there is no CTA, so `]` is the S2 `hx-get` to the (already unlocked) next timepoint. On a completed patient there is no CTA anywhere and `]` walks read-only. The summary card's Next button is **omitted at the frontier** so there is exactly one forward control; on locked timepoints it renders as before.
- **No-JS fallback (**[review-fix R24]**).** The form carries `action` + `method="post"`, so a submit without htmx is an ordinary POST to `/advance` and comes back as a 303 to the right timepoint (§5.1). The button is `type="submit"` either way; htmx intercepts the `submit` event when it is loaded and lets the native path run when it is not.
- **Locked pane.** `<fieldset disabled>` disables every descendant control in one attribute; the `<form class="question">` drops `hx-post`/`hx-trigger`/`hx-target`/`hx-sync` entirely (a disabled control is not submitted, but the form must not even try). Badges render the S9a `saved`/`blank` state so the clinician sees what they recorded. Footer: `<p class="pane-lock-note" role="note">Locked — answered before you advanced.</p>` + resume link (`hx-get` to the frontier, `hx-target="#patient-view"`, plus a real `href` for no-JS). Completed: `Patient complete ✓ — answers are locked.` + `← All patients`.
- **Resume everywhere.** Index rows and the summary-card patient jumper link to each patient's frontier, so switching patients never lands on a locked t=0 by default.
- **a11y:** CTA hint linked via `aria-describedby`; lock note is `role="note"`; the highlighted question keeps its `fieldset/legend`; focus moves *into* the first unanswered control so keyboard users are one Tab from answering.

---

## 8. Service layer

### 8.1 `web/gating.py`

```python
PaneMode = Literal["open", "locked"]
AdvanceOutcome = Literal["advanced", "finished", "blocked", "stale"]

@dataclass(frozen=True)
class Completeness:
    remaining: tuple[str, ...]          # required question_ids without a row, questions.yaml order
    @property
    def complete(self) -> bool: return not self.remaining

@dataclass(frozen=True)
class AdvanceResult:
    outcome: AdvanceOutcome
    unlocked_t_index: int               # the frontier after the call
    remaining: tuple[str, ...]          # non-empty only for "blocked"

def completeness(questions: Questions, saved: Mapping[str, object]) -> Completeness: ...
def is_viewable(f: Frontier, t_index: int) -> bool:   return t_index <= f.unlocked_t_index
def pane_mode(f: Frontier, t_index: int) -> PaneMode:  return "open" if t_index == f.unlocked_t_index and not f.completed else "locked"
```

**[review-fix R7] — the index and the patient jumper need a service function, not a DAO import.**

```python
ProgressState = Literal["not_started", "in_progress", "complete"]

@dataclass(frozen=True)
class PatientProgress:
    state: ProgressState
    unlocked_t_index: int               # == resume target; 0 when not started
    timepoint_count: int

def progress_overview(conn, *, clinician_id: str, patient_ids: Sequence[str],
                      timepoint_count: int) -> dict[str, PatientProgress]: ...
```

One `progress.list_for_clinician` read, clamped and defaulted per patient, **total over `patient_ids`** so the templates can index it without a Jinja `Undefined` (R14). Both study-mode consumers go through it: `GET /` (§5.4) and `_render_summary`'s jumper links (#16). §13 forbids `routes.py` from importing `progress`, and the draft gave it no other way to get this data — without the function the index either punches through the layer or the feature silently drops. Outside study mode it is never called and both templates keep their S2 branch. Cost: one extra PK-indexed SELECT per patient-view render at pilot scale (≤ patients × clinicians rows). Test #18b.

```python
def advance(conn, app_state, *, ctx, clinician_id, patient_id, t_index, timepoints, questions) -> AdvanceResult:
    if t_index != ctx.frontier.unlocked_t_index or ctx.frontier.completed:
        get_logger().warning("advance from a stale timepoint", event_kind="advance.stale",
                             requested=t_index, unlocked=ctx.frontier.unlocked_t_index, completed=ctx.frontier.completed)
        return AdvanceResult("stale", ctx.frontier.unlocked_t_index, ())

    t_minutes = timepoints[t_index]
    saved = saved_answers(conn, clinician_id=…, patient_id=…, t_minutes=t_minutes, questions=questions, config_hash=ctx.config_hash)
    comp = completeness(questions, saved)
    # [review-fix R9] "answered" must be answered-among-required. len(saved)
    # counts optional questions too, so a filled free_notes would emit
    # {"answered": 7, "required": 6} and every downstream ratio is wrong.
    n_required = sum(1 for q in questions.questions if q.required)
    base_payload = {"t_index": t_index,
                    "answered_required": n_required - len(comp.remaining),
                    "answered_total": len(saved),
                    "required": n_required}

    if not comp.complete:
        events.append(conn, session_id=ctx.session_id, …, timepoint=t_minutes, kind="advance.blocked",
                      payload={**base_payload, "remaining": list(comp.remaining)}, app_state=app_state)
        return AdvanceResult("blocked", ctx.frontier.unlocked_t_index, comp.remaining)

    is_last = t_index == len(timepoints) - 1
    if is_last:
        progress.mark_complete(conn, …, unlocked_t_index=t_index, config_hash=ctx.config_hash, app_state=app_state)
        sessions.close(conn, ctx.session_id)
        events.append(…, kind="advance.ok", payload={**base_payload, "to_t_index": None, "final": True})
        events.append(…, timepoint=None, kind="session.end", payload={"reason": "patient_complete"})
        return AdvanceResult("finished", t_index, ())

    # [review-fix R29] compare-and-set: a racing second request finds the row
    # already at t_index + 1, gets False, and degrades to "stale".
    if not progress.unlock(conn, …, from_t_index=t_index, to_t_index=t_index + 1,
                           config_hash=ctx.config_hash, app_state=app_state):
        get_logger().warning("advance lost the frontier race", event_kind="advance.stale", requested=t_index)
        return AdvanceResult("stale", t_index + 1, ())
    events.append(…, kind="advance.ok", payload={**base_payload, "to_t_index": t_index + 1, "final": False})
    return AdvanceResult("advanced", t_index + 1, ())
```

Every `events.append` in `advance` passes the normalized `client_ts` / `client_seq` from the request body (**[review-fix R17]**), through the same `normalize_client_ts` / `normalize_client_seq` pair `record_answer` uses — a bad clock field is NULL + WARNING, never a rejected advance.

Write order: state first (`progress`, `sessions`), events after — same discipline as S9a's `record_answer` (an event-append failure must never leave the frontier un-moved after the clinician saw success). `advance.blocked` carries the remaining ids — they are question ids from the config, not answer values, so the S9a "no raw value in payloads" rule holds. `stale` writes nothing: it is a client artifact, not clinician behavior.

### 8.2 `web/study_session.py` growth

```python
@dataclass(frozen=True)
class SessionContext:
    session_id: str
    arm: str
    config_hash: str
    unlocked_t_index: int      # NEW
    completed: bool            # NEW
```

`read_frontier` owns the progress read, the clamp and the drift WARNING (**[review-fix R21]** — it is the pure-read half the GET gate calls); `bootstrap_session` takes the resulting `Frontier` and resolves the session (**[review-fix R12]**):

```python
row = progress.fetch(conn, clinician_id=clinician_id, patient_id=patient_id)
unlocked = 0 if row is None else row.unlocked_t_index
completed = row is not None and row.completed_at is not None

timepoints = getattr(app_state, "study_timepoints", None)   # [review-fix R19] the test stub has none
last = (len(timepoints) - 1) if timepoints else unlocked
if unlocked > last:                                  # timepoint list shrank under a live DB
    get_logger().warning("progress beyond study timepoints; clamped", event_kind="progress.clamped", …)
    unlocked = last
if row is not None and row.config_hash != config_hash:
    get_logger().warning("progress recorded under a different config", event_kind="progress.config_hash.drift", …)
```

**[review-fix R12] — a completed patient must not open a session nothing can close.** The final advance sets `sessions.ended_at`, so the next GET finds no open session and S9a's `bootstrap_session` inserts a fresh one plus a `session.start` event. Nothing will ever close it: closing only happens on an advance, and a completed patient has no advance left. Every read-only revisit therefore leaks one permanently-open `sessions` row (the `ux_sessions_open` index tolerates exactly one, so the *next* revisit resumes it — but it stays open forever) and one extra `session.start` into the event stream S10 reads as "clinician began this walk". The fix:

```python
row = progress.fetch(conn, clinician_id=…, patient_id=…)
completed = row is not None and row.completed_at is not None

session_id = sessions.find_open(conn, clinician_id, patient_id)
if session_id is None and completed:
    session_id = sessions.find_latest(conn, clinician_id, patient_id)   # open or closed
if session_id is None:
    session_id = sessions.start_or_resume(...)                          # + events "session.start"
```

`find_latest` returning `None` (a seeded `progress` row with no session, which the `seed_progress` test helper produces) falls through to the S9a path, so the function is still total. Nothing is written with the reused id — a completed patient emits no events at all on GET — so re-pointing at a closed session cannot corrupt the log. Tests #9b, #20b.

Still two-to-three SELECTs per request, still no write in the steady state (the `progress` read never inserts). Every S9a test on `bootstrap_session` passes unchanged with the two new fields defaulting to `(0, False)`.

### 8.3 `events.kind` growth

```python
EventKind = Literal["clinician.login", "clinician.logout", "session.start", "session.end",
                    "answer.upsert", "answer.clear", "advance.ok", "advance.blocked"]
```

Payloads:

| kind | `timepoint` | payload | clock fields |
|---|---|---|---|
| `advance.ok` | minutes of the timepoint left | `{t_index, to_t_index | null, final, answered_required, answered_total, required}` | `client_ts`, `client_seq` |
| `advance.blocked` | minutes of the current timepoint | `{t_index, answered_required, answered_total, required, remaining: [qid, …]}` | `client_ts`, `client_seq` |
| `session.end` | `null` | `{reason: "patient_complete"}` | — |

`answered_required` / `answered_total` are **[review-fix R9]**: the draft's single `answered` key was `len(saved)`, which counts optional answers, so a clinician who filled `free_notes` produced `{"answered": 7, "required": 6}` and any "fraction answered" query built on it is wrong by a variable amount.

S10 gets per-timepoint dwell time from consecutive `advance.ok` rows (`server_ts` deltas); **[review-fix R17]** the `client_ts` / `client_seq` pair on the same rows is what lets S10 net out render and network latency, and it is the reason the columns are populated at all for advance events.

**[review-fix R25] — the "no further columns" claim needs a caveat.** `sessions.find_open` resumes an open session forever, so a clinician who closes the tab at t=1 and comes back the next morning emits no marker and the t=1→t=2 delta is eighteen hours. S9b does not try to fix this (a heuristic "resume" event is guesswork about what a gap means), but it must not hand S9c/S10 a metric that looks clean and is not: the delta is dwell time **within one continuous sitting only**, and S10 has to discard or flag deltas above a ceiling. Filed in §14.

---

## 9. Test inventory (ROADMAP bar ≥5; **57 new functions + ~24 adapted** after review)

Parametrize same-behavior-different-input cases per the S5 convention. Helpers from `tests/conftest.py`: `answer_all_required(client, pid, t)` and `seed_progress(db_path, cid, pid, unlocked, completed=False)`.

**[review-fix R15]** `answer_all_required` walks `app.state.questions`, skips `required is False`, and picks a valid value per `response_type` (`categorical`/`multi-select` → first option, `likert` → `scale_min`, `probability-0-100` → `50`, `free-text` → `"x"`), asserting afterwards that it covered exactly the required set. On today's fixture that is `deterioration_6h`, `survives_hospital`, `good_outcome_3mo`, `dead_6mo`, `confidence`, `contributing_factors`, leaving `free_notes` blank — but a seventh required question added to the fixture later must break loudly, not silently flip every advance test to `blocked`.

**[review-fix R2]** every assertion on a 3xx status or a `Location` header passes `follow_redirects=False`; Starlette's `TestClient` is constructed with `follow_redirects=True`.

### `tests/test_config.py` (+4, +1 case)

1. **`test_question_required_defaults_true`** — every fixture question has `required is True` except `free_notes` (fixture sets `false`).
2. **`test_question_required_false_parses`** — inline YAML with `required: false` on a categorical question.
3. **`test_question_required_rejects_non_bool[int|str]`** — `required: 1`, `required: "yes"` → `ConfigError` (StrictBool).
3b. `test_compute_config_hash_changes_on[required]` — new parametrize case: flipping `required` on one question changes the hash.
3c. **`test_shipped_questions_required_multiselect_has_opt_out[fixture|example]`** — **[review-fix R22]** parametrized over `tests/fixtures/study/questions.yaml` and `configs/example_questions.yaml`: every `multi-select` question with `required is True` must carry an option from `OPT_OUT_OPTIONS = {"None of these"}`. Locks the S9a R10 authoring rule against the two files the project actually ships, instead of leaving it as prose in a header comment.

### `tests/test_db.py` (+10, 4 adapted)

4. **`test_migration_3_creates_progress_and_is_idempotent`** — `apply_migrations` on a v2 DB returns `[3]`; second call `[]`; `progress` in `sqlite_master`; a second `INSERT` for the same pair raises `IntegrityError` (PK).
5. **`test_progress_fetch_none_when_absent`**.
6. **`test_progress_unlock_upserts_and_is_monotonic`** — `unlock(from=0, to=1)` inserts; `unlock(from=1, to=2)` gives 2; `write_counter` bumped per successful call.
6b. **`test_progress_unlock_preserves_original_config_hash`** — **[review-fix R11]** insert under `"h1"`, unlock again under `"h2"`: `unlocked_t_index` moves, `config_hash` stays `"h1"`. Without this the §8.2 drift WARNING disarms itself one advance into the pilot.
6c. **`test_progress_unlock_compare_and_set_rejects_stale_from_index`** — **[review-fix R29]** `unlock(from=0, to=1)` → `True`, row 1; replaying it → `False`, row still 1; `unlock(from=1, to=2)` → `True`; `unlock(from=0, to=1)` against a row at 2 → `False`, row still 2, no `write_counter` bump on the misses.
7. **`test_progress_mark_complete_sets_completed_at_once`** — `completed_at` is a `datetime` after the call; a second call leaves the same value; `unlocked_t_index` untouched.
8. **`test_progress_list_for_clinician`** — A has two patients, B one → A's dict has both keys, B's has one, unrelated rows excluded.
9. **`test_sessions_close_sets_ended_at_and_frees_pair`** — `close` returns 1; `find_open` → `None`; a new `start_or_resume` succeeds (no `IntegrityError` under `ux_sessions_open`); `close` again returns 0.
9b. **`test_sessions_find_latest_returns_most_recent_open_or_closed`** — **[review-fix R12]** `None` on an unknown pair; after `start_or_resume` returns that id; after `close` still returns it; after a second `start_or_resume` returns the newer one; another pair's rows are excluded.
10. **`test_events_kind_taxonomy_includes_s9b_kinds`** — the three new kinds are in `EVENT_KINDS` and `append` accepts each.

— adapted (**[review-fix R1]** — §12's draft named the schema snapshot as the only affected `test_db.py` test; three more assert the migration-version list literally and every one fails on migration 3):
  - **`test_post_migration_schema_matches_fixture`** (`tests/test_db.py:213`) — regenerated fixture SQL.
  - **`test_apply_migrations_forward`** (`tests/test_db.py:144`) — `assert versions == [1, 2]` at `:153` → `[1, 2, 3]`; the `expected` table set gains `progress`.
  - **`test_apply_migrations_recovers_from_partial_apply`** (`tests/test_db.py:157`) — same literal at `:184`.
  - **`test_migration_2_rejects_second_open_session`** (`tests/test_db.py:614`) — `assert apply_migrations(v1) == [2]` at `:626` → `[2, 3]`; it bootstraps a v1 DB and runs the whole forward chain.

### `tests/test_gating.py` (9 — signatures take a `Frontier`, **[review-fix R21]**)

11. **`test_completeness_ignores_optional_questions`** — six required saved, `free_notes` absent → `complete`.
12. **`test_completeness_remaining_in_yaml_order`** — save `confidence` + `deterioration_6h` only → `remaining == ("survives_hospital", "good_outcome_3mo", "dead_6mo", "contributing_factors")`.
13. **`test_pane_mode_and_viewable[open|past|future|completed]`** — table over `Frontier`: `Frontier(1, False)` → t=1 open+viewable, t=0 locked+viewable, t=2 locked+not viewable; `Frontier(2, True)` → t=2 locked+viewable.
14. **`test_advance_blocked_emits_event_and_keeps_progress`** — no answers → `"blocked"`, `remaining` = 6 ids; one `advance.blocked` event with `payload.remaining` == list; `progress.fetch` still `None`.
15. **`test_advance_ok_unlocks_next_and_emits_event`** — all required saved at t=0 → `"advanced"`, `unlocked_t_index == 1`; `progress` row `1`; `advance.ok` payload `{t_index:0, to_t_index:1, final:false, answered_required:6, answered_total:6, required:6}` (**[review-fix R9]**); session still open.
16. **`test_advance_final_completes_and_closes_session`** — ctx at last index, complete → `"finished"`; `completed_at` set; `sessions.ended_at` set; events `advance.ok(final=true)` **and** `session.end`, in that order.
17. **`test_advance_stale_when_t_index_mismatch`** — frontier `unlocked=1`, call with `t_index=0` → `"stale"`, no `progress` write, no event, `advance.stale` WARNING captured.
18. **`test_advance_after_complete_is_stale`** — `completed=True` → `"stale"` even at `t_index == unlocked`.
18b. **`test_progress_overview_states_and_resume_indices`** — **[review-fix R7]** three patients: no row, `unlocked=1`, `completed` → `not_started/0`, `in_progress/1`, `complete/2`; the mapping is **total** over the requested `patient_ids` (**[review-fix R14]** — the jumper template indexes it); a row whose `unlocked_t_index` exceeds `timepoint_count - 1` clamps.

### `tests/test_study_session.py` (+3, 2 adapted)

19. **`test_read_frontier_reads_progress`** — no row → `Frontier(0, False)`; after `progress.unlock(from=0, to=1)` → `unlocked_t_index == 1`; after `mark_complete` → `completed is True`; **no** `sessions` / `arm_assignments` row is created by the read (**[review-fix R21]**).
20. **`test_read_frontier_clamps_and_warns_on_drift`** — row `unlocked=7` under a 3-timepoint study → `2` + `progress.clamped` WARNING; row `config_hash="old"` → `progress.config_hash.drift` WARNING; live hash → neither; `app_state` without `study_timepoints` → no clamp, no crash (**[review-fix R19]**).
20b. **`test_bootstrap_session_reuses_latest_session_when_completed`** — **[review-fix R12]** bootstrap → `sessions.close` → `progress.mark_complete` → bootstrap again: the returned `session_id` is the *original* one, `COUNT(sessions) == 1`, `COUNT(events WHERE kind='session.start') == 1`. Without `mark_complete` the S9a `test_bootstrap_session_after_ended_creates_new` semantics are unchanged.

— adapted (**[review-fix R19]**): `tests/test_study_session.py:11-13`'s `_AppState` stub defines only `write_counter` and `config_hash`, so the draft's unconditional `len(app_state.study_timepoints)` raises `AttributeError` in **both** existing tests. The stub gains `study_timepoints = [0.0, 60.0, 180.0]` and `read_frontier` tolerates its absence anyway. §16's "Every S9a test on `bootstrap_session` passes unchanged" was false.

### `tests/test_answer_routes.py` (+5, ~12 adapted)

**[review-fix R18] — the module targets a timepoint the gate closes.** `tests/test_answer_routes.py:22` sets `T_INDEX = 1` and `_url()` defaults to it, so nearly every POST test aims at t=1 while a fresh study DB has `unlocked = 0`. Under §5.3 each becomes a 409 `Timepoint locked`, and the failures are confusing rather than obvious — the validation tests flip from 422 to 409 because `record_answer` runs *after* the new gate. Fix: re-point the module to `T_INDEX = 0` / `T_MINUTES = 0.0`, the frontier of a fresh DB, which preserves every assertion's meaning. Affected: `:64`, `:76`, `:85`, `:93`, `:104`, `:129`, `:152` (×4 parametrize cases), `:170`, `:276` and `:321`. `:276` is the nastiest: `_pane(study_client, T_INDEX)` gets a 303 that `TestClient` silently follows, so `assert r.status_code == 200` at `:55` passes and the test fails four lines later on `form["hx-post"] == _url(1)` — drift, not a clean failure. `:220`, `:253` and `:262` target out-of-range / unknown patients and resolve before the gate, so they are unaffected.

21. **`test_post_answer_200_includes_oob_advance_cta`** — after one valid POST the body has both `data-state="saved"` and `id="advance-form"` with `hx-swap-oob="true"` and `data-remaining="5"`; a clearing POST returns `data-remaining="6"`.
22. **`test_post_answer_locked_timepoint_409[past|future|completed]`** — `seed_progress(unlocked=1)` then POST at t=0 → 409 `Timepoint locked`, `data-state="error"`, 0 rows, no `answer.*` event; same for t=2; `seed_progress(unlocked=2, completed=True)` + POST at t=2 → 409.
23. **`test_post_answer_422_has_no_oob_cta`** — invalid value → body has `data-state="error"` and no `advance-form`.
24. **`test_get_patient_pane_open_has_advance_cta_with_remaining`** — fresh GET t=0 → `#advance-form[data-remaining="6"]`, `#advance-btn[aria-disabled="true"][aria-describedby="advance-hint"]`, label contains `6 unanswered`, `data-first-unanswered="deterioration_6h"`, `hx-sync="this:drop"`.
25. **`test_get_patient_pane_locked_renders_disabled_fieldsets`** — `seed_progress(unlocked=1)`, GET t=0 → every `fieldset` has `disabled`; no `form.question` carries `hx-post`; `.pane-lock-note[role="note"]` present; resume link `href` ends `/timepoint/1?chrome=epic`; no `#advance-form`.

### `tests/test_advance_routes.py` (21)

26. **`test_get_beyond_frontier_redirects[plain|htmx]`** — **REGRESSION** (data ≤ frontier): fresh GET t=2 → 303 with `Location: /patient/synth_001/timepoint/0?chrome=epic` (plain, `follow_redirects=False`) / 200 + `HX-Redirect` (htmx); `gate.redirect` WARNING captured.
26b. **`test_gate_beyond_frontier_writes_nothing`** — **[review-fix R20] + [review-fix R21]**, the test that actually locks the invariant: `monkeypatch` a call counter onto `routes.slice_to_timepoint`, fresh GET t=2 → counter `== 0`; `SELECT COUNT(*)` on `progress`, `sessions` **and** `arm_assignments` all `0`; no `session.start` event. The draft's "body has neither `<svg` nor `id=\"patient-view\"`" survives as a cheap second line but is vacuous alone — both redirect shapes have empty bodies by construction, so it passes whether or not slicing ran.
26c. **`test_advance_plain_browser_outcomes_are_303[advanced|blocked|stale|finished]`** — **[review-fix R24]** no `HX-Request` header: blocked → 303 to `…/timepoint/0?chrome=epic` with the `advance.blocked` event still written; advanced → 303 to t=1; stale → 303 to the frontier; finished → 303 to `/`.
26d. **`test_gate_redirect_log_line_is_attributable`** — **[review-fix R10]** the captured `gate.redirect` WARNING and the middleware's `request` line both carry `patient_id="synth_001"`, `timepoint_index=2` and `chrome`.
27. **`test_get_frontier_and_past_are_200`** — `seed_progress(unlocked=1)`: t=0 → 200 locked, t=1 → 200 open, t=2 → 303.
28. **`test_advance_blocked_409_fragment_and_event`** — fresh POST advance t=0 with `HX-Request: true` → 409, `HX-Retarget: #advance-form`, `HX-Reswap: outerHTML`, body root `id="advance-form"` with `data-remaining="6"`, `data-first-unanswered="deterioration_6h"`, no `hx-swap-oob`; exactly one `advance.blocked` event; `progress.fetch` is `None`.
28b. **`test_advance_cta_has_no_js_form_action`** — **[review-fix R24]** the rendered CTA's `action` ends `/timepoint/0/advance?chrome=epic` and `method` is `post`, so a submit without htmx cannot 405 on the GET-only patient URL.
29. **`test_advance_ok_renders_next_view_and_pushes_url`** — `answer_all_required(t=0)` → POST advance → 200, `HX-Push-Url: /patient/synth_001/timepoint/1?chrome=epic`, body `#patient-view[data-t-index="1"]` with an **open** pane (`#advance-form` present, `data-remaining="6"` — the **[review-fix R8]** post-write context; the pre-advance ctx would render it locked); `progress` row `1`; `advance.ok` event.
29b. **`test_advance_events_carry_client_ts_and_client_seq`** — **[review-fix R17]** POST advance with `client_ts=<iso>&client_seq=7` → the `advance.blocked` row has a non-NULL `client_ts` that reads back as a `datetime` and `client_seq == 7`; a garbage `client_ts` still advances and leaves NULL + an `answer.client_ts.invalid` WARNING.
30. **`test_advance_stale_412_renders_frontier_view`** — after #29's state, POST advance at t=0 → 412, body `data-t-index="1"`, `HX-Push-Url` to t=1; no new event; `progress` still `1`.
30b. **`test_advance_concurrent_frontier_move_is_stale`** — **[review-fix R29]** `answer_all_required(t=0)`, move the frontier out from under the handler (`progress.unlock(from=0, to=1)` directly), then POST advance at t=0 → stale, `unlocked_t_index == 1` (never 2), exactly one `advance.ok`. **Post-implementation correction (code review #4):** through the route this takes the *plain stale* path — `patient_advance` re-reads the frontier inside the handler, after its only `await`, so the CAS in `progress.unlock` is only reachable from a second connection. The route test stays as a stale-path check; the CAS is locked at service level by `test_gating.py::test_advance_lost_cas_race_is_stale`, which captures `ctx` before moving the frontier.
31. **`test_advance_double_submit_second_is_stale`** — **REGRESSION**: `answer_all_required(t=0)`, two sequential POSTs → 200 then 412; `unlocked_t_index == 1` (not 2); exactly one `advance.ok`.
32. **`test_advance_final_redirects_and_closes_session[plain|htmx]`** — walk t=0 → t=1 → t=2 via `answer_all_required` + advance; final advance → 303 `/` (plain) / 200 + `HX-Redirect: /` (htmx); `progress.completed_at` set; `sessions.ended_at` set; one `session.end`; subsequent GET t=2 → 200 with locked pane and `Patient complete`, and **no new `sessions` row or `session.start` event** (**[review-fix R12]**); GET t=0 → 200 locked.
33. **`test_advance_free_notes_optional_does_not_block`** — answer the six required, leave `free_notes` empty → advanced. (The S9a R22 acceptance.)
34. **`test_advance_no_study_409`** — plain `client` → 409 `error-flash`.
35. **`test_advance_no_cookie_redirects[plain|htmx]`**.
36. **`test_advance_bad_target_404[not_in_study|unknown|out_of_range]`** — `error-flash` bodies, no writes.
37. **`test_summary_card_next_hidden_at_frontier`** — fresh GET t=0 → no `.tp-next`; `seed_progress(unlocked=1)`: GET t=0 → `.tp-next[hx-get$="/timepoint/1?chrome=epic"]`; GET t=1 → no `.tp-next`; `.tp-prev` unaffected.
38. **`test_patient_jumper_and_index_resume_at_frontier`** — `seed_progress(synth_001, unlocked=1)`: GET synth_002 t=0 → jumper link for synth_001 ends `/timepoint/1`, synth_002 `/timepoint/0`; GET `/` → synth_001 row has `in progress` marker + `t 2/3` + link `/timepoint/1`, synth_002 `not started`; after `seed_progress(synth_003, unlocked=2, completed=True)` → `complete` marker, link `/timepoint/2`.
39. **`test_index_without_study_has_no_progress_markers`** — plain `client` GET `/` → no `.progress-marker`; **[review-fix R14]** the summary-card jumper `href`s are byte-identical to S2 (`/patient/{pid}/timepoint/0?chrome=…`), never `/timepoint/?chrome=…` — a missing key in `resume_t_index` renders as the empty string under Jinja's default `Undefined`.
40. **`test_timepoint_count_uses_study_timepoints`** — **REGRESSION** for the latent S5 mismatch: study `timepoints: [0, 180]` on synthetic (dataset has 3) → `data-t-count="2"`, summary `(1/2)` in **both** places the template prints a total (**[review-fix R4]** — `_summary_card.html:1` and `:46`), and `]` at t=1 would be a boundary.
40b. **`test_history_restore_request_serves_full_document`** — **[review-fix R6]** GET the frontier with `HX-Request: true` **and** `HX-History-Restore-Request: true` → 200 whose body starts `<!doctype html>`, contains `#shortcut-overlay` and `#patient-view`, and carries **no** `HX-Push-Url`; the same GET without the restore header is the bare partial **with** `HX-Push-Url`. Beyond the frontier the restore request still redirects — the gate is unconditional.

### `tests/test_db.py` / `tests/test_cli.py` — reset-progress (owner decision (b); +4)

10b. **`test_progress_reset_rewinds_and_clears_completed`** — row at `2` with `completed_at` set → `reset(to=0)` returns 1, row reads `(0, None)`; `reset` on an unknown pair returns 0.
10c. **`test_answers_delete_after_keeps_boundary`** — rows at 0/60/180 → `delete_after(min_timepoint_exclusive=60.0)` returns 1, rows at 0 and 60 remain; `write_counter` bumps only when a row went.
10d. **`test_cli_reset_progress_rewinds_walk`** (`tests/test_cli.py`) — seed clinician + `progress` at 2 + answers at all three timepoints → `reset-progress STUDY --clinician "Dr. Test" --patient synth_001 --to-t-index 1 --db-path P` exits 0; stdout names previous frontier 2 and 1 deleted answer; `progress` reads `(1, None)`; one `progress.reset` event with that payload.
10e. **`test_cli_reset_progress_errors[unknown_clinician|no_progress_row|index_out_of_range]`** — each exits 1 with the reason on stderr and writes nothing.

### `tests/test_app.py` (+1, 1 adapted)

41. **`test_app_from_study_config_warns_when_no_question_required`** — questions.yaml with every `required: false` → `questions.none_required` WARNING at factory time; fixture questions → no WARNING.

— adapted: **`test_app_from_study_config_t_index_resolves_to_study_timepoints`** (`tests/test_app.py:92`) — **[review-fix R2]**: left as drafted this test would keep passing while testing nothing. `TestClient(follow_redirects=True)` silently follows the gate's 303 to t=0, so `status_code == 200`, the second GET and `"synth_001" in response.text` all still hold and the t_index→t_minutes regression (the reason the test exists, /plan-eng-review issue 1.2) stops being exercised. The adaptation: `client.get(…/timepoint/1, follow_redirects=False)` → 303 with `Location: /patient/synth_001/timepoint/0?chrome=epic`; then `progress.unlock(from=0, to=1)` and GET t=1 → 200 with `data-t-index="1"` and `data-t-minutes="180.0"` (the original intent, restored). `app.state.study_timepoints` unchanged.

### `tests/test_a11y.py` (+1)

42. **`test_advance_cta_and_locked_pane_a11y`** — blocked: `#advance-btn[aria-disabled="true"]` and its `aria-describedby` resolves to an element whose text mentions the remaining count; locked: `.pane-lock-note[role="note"]`, every control inside a `fieldset[disabled]`, resume link has visible text; both panes still pass the S9a label/legend walk.

### `tests/test_csp.py` (+1)

43. **`test_advance_js_served_and_gated_fragments_csp_clean`** — `GET /static/advance.js` → 200 `text/javascript`; the open pane, locked pane, 409 CTA body and 412 view body have no `on*=` attributes and no inline `<script>`; **[review-fix R6]** the full document's `<body>` carries `hx-history="false"`, so rendered patient data never reaches `localStorage`.

### `tests/test_cli.py` (1 adapted)

— adapted: **`test_cli_preview_text_summary_and_html_out`** — **[review-fix R23]** each of the three `--html-out` files asserts `data-t-index="{idx}"` **first**, then `id="advance-form"` and `data-remaining="6"`. The pane assertion alone cannot fail: with `follow_redirects=True` an ungated preview writes the t=0 body (CTA included) into all three files.

### E2E (2 new, 1 adapted)

44. **`tests/e2e/test_gated_walk.py::test_blocked_click_scrolls_and_focuses_first_unanswered`** — login → t=0 → click `#advance-btn` inside `page.expect_response(…/advance)` → status 409 → `#advance-form[data-remaining="6"]` re-rendered → `document.activeElement` is inside `form[data-question-id="deterioration_6h"]` → that form has `.is-highlighted` → wait `HIGHLIGHT_MS + 200` → class gone. Then answer `deterioration_6h` → CTA reads `5 unanswered` **and focus stays on the control the clinician just used** (**[review-fix R3]** — the OOB swap must not trigger the scroll-and-focus path).
45. **`tests/e2e/test_gated_walk.py::test_gated_walk_to_completion`** — answer six required at t=0, leave `free_notes` blank → CTA text `Next timepoint ›`, no `aria-disabled` → press `]` inside `expect_response(…/advance)` → `#patient-view[data-t-index='1']`, `page.url` ends `/timepoint/1?chrome=epic` (push) → press `[` → t=0: `fieldset[disabled]` count == 7, no `#advance-form`, `.pane-lock-note` visible → click resume link → t=1 → answer all six → `]` → t=2 → answer → CTA `Finish patient ✓` → **press `]`** (**[review-fix R5]** — the last timepoint is exactly where the boundary guard would swallow the key; a mouse click would not catch it) → `page.wait_for_url(…/)` → index row synth_001 shows `complete` → `page.goto(…/timepoint/1)` → 200, locked pane (no gate redirect: everything is viewable) → `page.goto(…/timepoint/2)` → locked with `Patient complete`. Finally `page.go_back()` to exercise the htmx history restore (**[review-fix R6]**): the restored document still has `#shortcut-overlay`.

— **not adapted but now load-bearing:** `tests/e2e/test_answer_walk.py::test_free_text_autosaves_without_blur:72` asserts `document.activeElement.tagName == "TEXTAREA"` after a debounced save. With the OOB CTA riding on every 200 that assertion becomes the regression for **[review-fix R3]**: an unscoped `htmx:afterSwap` in `advance.js` fails it. §12 records it.

— adapted: **`tests/e2e/test_answer_walk.py::test_answer_autosave_and_prefill`** — after clicking radio `No`, pressing `]` with the radio still focused must now fire **`POST …/advance`** (the S9a R19 regression re-expressed: the key was not swallowed) and land a 409 with `data-remaining="5"`. The `[`-back step at `:44` is dropped, and with it the `is_checked` assertion at `:46` — **[review-fix R27]**: the draft justified dropping it with "t=0 has no previous", but that `[` is pressed at **t=1**; the real reason is that the `]` at `:42` no longer navigates. The reload/pre-fill assertions at `:54-58` stay and cover what `:46` covered.

**Total: 57 new test functions** (config 4, db 10, gating 9, study_session 3, answer_routes 5, advance_routes 21, app 1, a11y 1, csp 1, e2e 2) **+ ~24 adapted**: 4 in `test_db.py` (schema snapshot + three migration-version assertions, R1), ~12 in `test_answer_routes.py` (the `T_INDEX` re-point, R18), 2 in `test_study_session.py` (the `_AppState` stub, R19), 1 in `test_app.py` (R2), 1 in `test_cli.py` (R23), 1 e2e answer walk (R27) — plus 2 new parametrize cases. ROADMAP bar (≥5) cleared 11×.

**[review-fix R16] — corrected arithmetic.** The draft's "45 new + 4 adapted; baseline 313 → ~358 default-suite functions (e2e 6 → 8)" was wrong three ways: it folded the two e2e functions into the default suite, conflated functions with collected items, and counted 4 adapted tests where the real figure is ~24. `uv run pytest --co -q` reports **313 collected / 8 deselected** today (321 total). 55 of the 57 new functions are default-suite and 2 are `@pytest.mark.e2e`, so the floor is **368 collected / 10 deselected**; parametrized cases (`[int|str]`, `[plain|htmx]`, `[past|future|completed]`, `[open|past|future|completed]`, `[not_in_study|unknown|out_of_range]`, `[advanced|blocked|stale|finished]`, `[fixture|example]`, the new `[required]` case) push the collected number higher. Also corrected in §5.1: the double-submit regression is test **#31**, not #33.

---

## 10. CI changes (`.github/workflows/ci.yml`)

`CLI smoke`, after the S9a `grep -q 'id="questions-pane"' …_t0.html`:

```yaml
          grep -q 'data-t-index="2"' /tmp/preview_smoke/synth_001_t2.html
          grep -q 'id="advance-form"' /tmp/preview_smoke/synth_001_t2.html
```

**[review-fix R23]** — the `data-t-index` grep is the one that can fail. The draft's rationale ("a `t2` rendered from a 303 body would have no pane at all") is wrong: `render_html_for_preview` builds its `TestClient` with the default `follow_redirects=True` (`cli_support.py:295`), so an ungated preview writes the **t=0 body** into `synth_001_t2.html` — pane, CTA and all — and `raise_for_status()` never trips. The preview client also passes `follow_redirects=False` (#23) so a gate redirect fails loudly at the source. The `e2e` job picks up `test_gated_walk.py` via `-m e2e`.

---

## 11. Commit discipline (target 6 commits, **~2 days** — **[review-fix R18]**: the draft's 1.5 days budgeted no time for the ~24 adapted tests, half of them a module-wide re-point in `test_answer_routes.py`)

| # | Commit | Files |
|---|---|---|
| 1 | `session-09b commit 1: required flag, progress table + DAO, sessions.close, EventKind growth` | `config/questions.py`, both `questions.yaml`, `db/migrations.py`, `db/progress.py` (incl. `reset`), `db/answers.py` (+`delete_after`), `db/sessions.py`, `db/events.py`, `db/__init__.py`, `tests/fixtures/db/migration_001_expected_schema.sql`, `tests/test_config.py` (#1–#3c), `tests/test_db.py` (#4–#10c + the four adaptations, R1), `TODOS.md` (strike the two S9b prerequisites). |
| 2 | `session-09b commit 2: gating service + progress-aware SessionContext` | `web/gating.py`, `web/study_session.py`, `tests/test_gating.py` (#11–#18b), `tests/test_study_session.py` (#19–#20b + the `_AppState` adaptation, R19). |
| 3 | `session-09b commit 3: GET gate, /advance route, locked pane, OOB CTA, index progress` | `web/routes.py`, `web/app.py`, `templates/_questions_pane.html`, `templates/_advance_cta.html`, `templates/_summary_card.html`, `templates/_patient_view.html`, `templates/index.html`, `tests/conftest.py` (helpers), `tests/test_answer_routes.py` (#21–#25 **+ the `T_INDEX` re-point**, R18), `tests/test_advance_routes.py` (#26–#40b), `tests/test_app.py` (#41 + adaptation). |
| 4 | `session-09b commit 4: advance.js, ] delegation, pane/CTA CSS, a11y + CSP tests` | `static/advance.js`, `static/keyboard.js`, `static/theme.css`, `templates/base.html`, `tests/test_a11y.py` (#42), `tests/test_csp.py` (#43). |
| 5 | `session-09b commit 5: preview step-wise unlock, reset-progress CLI, e2e gated walk, CI grep, docs` | `cli.py` (+`reset-progress`), `cli_support.py` (preview unlock + `reset_progress`), `tests/test_cli.py` (#10d, #10e + preview adaptation), `tests/e2e/test_gated_walk.py` (#44–#45), `tests/e2e/test_answer_walk.py`, `.github/workflows/ci.yml`, `specs/ROADMAP.md`, `TODOS.md` (§14 items). |
| 6 | `session-09b commit 6: ruff/format pass` | whole tree. |

Commit 3 lands the templates functional-but-unstyled so #21–#40b are meaningful in a green tree; commit 4 adds the client script and CSS. **[review-fix R27]** commit 3 is green only because #42/#43 (the a11y and CSP tests, which assert on classes and attributes the CSS merely *styles*) live in commit 4 with `theme.css`; nothing in commit 3 asserts a computed style. Commit 5's preview change is what makes the CI smoke pass — it ships with the test that needs it.

---

## 12. Acceptance criteria

- [ ] `uv sync` clean (no new deps).
- [ ] `uv run pytest` green; 57 new test functions. **Pre-S9b behavior changes — the draft called this list "exhaustive" with four entries; it is ~24 tests (**[review-fix R1]**, **[review-fix R18]**, **[review-fix R19]**, **[review-fix R2]**, **[review-fix R23]**, **[review-fix R27]**):**
  - (a) `tests/test_answer_routes.py` — `T_INDEX = 1` at `:22` is past the frontier of a fresh DB, so ~12 tests (`:64`, `:76`, `:85`, `:93`, `:104`, `:129`, `:152`×4, `:170`, `:276`, `:321`) hit the new 409 or a silently-followed 303. Module re-pointed to `T_INDEX = 0`. **This is the single biggest cost item in the session and the draft did not see it.**
  - (b) `tests/test_db.py` — four tests: the schema snapshot plus `test_apply_migrations_forward:153`, `test_apply_migrations_recovers_from_partial_apply:184` and `test_migration_2_rejects_second_open_session:626`, each asserting the migration-version list literally.
  - (c) `tests/test_study_session.py` — both existing tests, via the `_AppState` stub that has no `study_timepoints`.
  - (d) `test_app_from_study_config_t_index_resolves_to_study_timepoints` — a direct GET to t=1 is now a 303; without `follow_redirects=False` the test would keep passing while testing nothing.
  - (e) `test_cli_preview_text_summary_and_html_out` — assertions grow and must key on `data-t-index`, not on the pane.
  - (f) e2e `test_answer_autosave_and_prefill` — `]` at the frontier hits `/advance`; e2e `test_free_text_autosaves_without_blur` is unchanged but becomes the R3 focus-theft regression.
  Non-study routes (`client` fixture) stay byte-identical: no gate, no pane, no CTA, summary card and `data-t-count` unchanged.
- [ ] `apply_migrations` on a v2 DB applies `[3]`, idempotent; `progress` PK rejects a duplicate pair.
- [ ] Fresh study DB, logged in: `GET /patient/synth_001/timepoint/2` → 303 to `/timepoint/0?chrome=epic`; `slice_to_timepoint` was **not called** (the body assertion alone is vacuous — redirect bodies are empty either way, **[review-fix R20]**) and `progress`, `sessions` and `arm_assignments` are all still empty (**[review-fix R21]** — the gate must not lock the S11 arm for a request it bounces).
- [ ] t=0 shows the CTA `Next timepoint · 6 unanswered` (muted); clicking it does **not** navigate, scrolls to and focuses question 1, and `SELECT kind FROM events WHERE kind='advance.blocked'` has one row with `payload_json` listing the six ids.
- [ ] Answering the six required questions flips the label to `Next timepoint ›` **without** a page load (OOB swap); `free_notes` stays blank.
- [ ] Clicking it (or pressing `]`) renders t=1 with an open pane; the address bar reads `/timepoint/1?chrome=epic`; `progress.unlocked_t_index == 1`; one `advance.ok` row with `to_t_index=1`.
- [ ] `[` back to t=0: every fieldset disabled, the saved radios still checked, `Locked — answered before you advanced.` shown, no CTA, summary card **has** a Next button (to t=1); `curl -X POST … /timepoint/0/answer -d "question_id=deterioration_6h&value=Yes"` → 409 `Timepoint locked`, row unchanged.
- [ ] `curl -X POST … /timepoint/0/advance` (stale) → 412, body is the t=1 view; `progress` unchanged; no new event.
- [ ] Two rapid clicks on the CTA at t=1 (complete) → exactly one `advance.ok`, frontier at 2, second response 412.
- [ ] At t=2 the label reads `Finish patient ✓`; clicking it lands on `/`; synth_001 shows `complete ✓` linking to `/timepoint/2`; `sessions.ended_at` set; one `session.end` event; `progress.completed_at` set.
- [ ] Revisiting synth_001 t=0..2 after completion: all 200, all locked, no CTA, `Patient complete ✓` note; **no** new `sessions` row and **no** new `session.start` event (**[review-fix R12]** — the final advance closed the session and nothing could ever close a replacement).
- [ ] Index row for synth_002 reads `not started` and links to t=0; the summary-card jumper on synth_002 links synth_001 to `/timepoint/2`.
- [ ] `serve` without `--config`: `]` walks all three timepoints exactly as in S2; no `progress` table writes; `/advance` → 409.
- [ ] `questions.yaml` with every `required: false` → `questions.none_required` WARNING in `logs/current.jsonl` at boot; the CTA is `Next timepoint ›` from the first render.
- [ ] `ehr-simulator preview … --html-out /tmp/p` writes `synth_001_t{0,1,2}.html`, each carrying **its own** `data-t-index` and an **open** pane with `id="advance-form"` (**[review-fix R23]**).
- [ ] `SELECT kind, count(*) FROM events GROUP BY kind` after one full walk: `clinician.login 1`, `session.start 1`, `answer.upsert 18`, `advance.blocked ≥1`, `advance.ok 3`, `session.end 1`.
- [ ] With JavaScript disabled, the CTA still advances: submitting it POSTs to `/advance` and the browser lands on the next timepoint via 303 (**[review-fix R24]**).
- [ ] `localStorage` is empty after a full walk — no htmx history snapshot of patient data (**[review-fix R6]**); the back button re-requests from the server and the restored page still has the shortcut overlay.
- [ ] Neither `contributing_factors` nor any other required multi-select can be answered only by fabricating a factor: both shipped questions files offer `None of these` (**[review-fix R22]**).
- [ ] Browser console shows no CSP violations while advancing; `prefers-reduced-motion: reduce` makes the blocked-click scroll instant.
- [ ] `uv run ruff check .` and `uv run ruff format --check .` clean; CI green on 3.11 + 3.12 including the new `grep`; `uv run pytest -m e2e` green; `uv run pytest -m real_data` untouched.

---

## 13. Conventions

- `from __future__ import annotations`, module docstrings, type hints on every public function — as S9a.
- `gating.py` and `progress.py` are pure functions + frozen dataclasses. Constants at module top: `HIGHLIGHT_MS = 1500` (`advance.js`), status codes via `fastapi.status` (`HTTP_409_CONFLICT`, `HTTP_412_PRECONDITION_FAILED`), message constant `_TIMEPOINT_LOCKED_MSG` (the failure hint is client-side copy in `advance.js`); event-kind strings only through the `EventKind` alias.
- Function parameters that switch behavior are `Literal`s or dataclasses, never booleans (`PaneMode`, `AdvanceOutcome`). The one template boolean (`oob`) is a render flag, not a Python API.
- Routes stay thin: preamble → resolve → bind context → **read frontier → gate** → bootstrap → service → render (**[review-fix R21]**: the gate sits between the pure read and the first write). `routes.py` never imports `progress` or `sessions` directly; `gating.py` / `study_session.py` do — which is why the index's progress markers go through `gating.progress_overview` (**[review-fix R7]**).
- Both patient routes and `/advance` are `async def` (S9a R11). **[review-fix R29]** — but say why correctly: `async def` guarantees nothing on its own; what serializes the check-then-write is the absence of an `await` between the progress read and the progress write, and `connect(..., check_same_thread=False)` (`connection.py:38`) exists precisely so a threadpool-dispatched `def` handler would also work. That invariant is invisible and untestable, so it is **not** what correctness rests on: `progress.unlock` compares-and-sets on the observed frontier in SQL, and a lost race degrades to `stale`. The `await request.form()` that R17 adds is hoisted above `bootstrap_session` anyway, mirroring `routes.py:396`/`:421`.
- The GET gate runs **before** `slice_to_timepoint` **and before any write**. Test #26b monkeypatches a call counter onto `slice_to_timepoint` and asserts zero, and asserts zero rows in `progress`/`sessions`/`arm_assignments`; asserting on the redirect body cannot catch a reordering (**[review-fix R20]**).
- Every test that asserts a 3xx status or a `Location` header passes `follow_redirects=False` (**[review-fix R2]**).
- Template ids: `advance-form`, `advance-btn`, `advance-hint`; classes `advance-cta`, `advance-btn`, `is-blocked`, `advance-hint`, `pane-footer`, `pane-lock-note`, `resume-link`, `is-highlighted`, `progress-marker is-not-started|is-in-progress|is-complete`. Machine-readable state in `data-*` (`data-remaining`, `data-first-unanswered`, `data-t-count`); tests and JS never parse copy.
- JS: IIFE + `"use strict"`, no globals, `matchMedia` guarded, no inline handlers.
- Event payloads carry question ids and counts, never answer values.
- Tests: `test_<subject>_<expected_behavior>`; BeautifulSoup for HTML; `structlog.testing.capture_logs` for WARNINGs; the two conftest helpers are the only way advance tests reach a complete cell or a seeded frontier.

---

## 14. Open decisions deferred to later sessions / TODOs to file

- ~~**Admin unlock / reset progress.**~~ **Pulled into S9b by owner decision (b), 2026-09-16** — deliverable #38, tests #10b–#10e. The operator-run `reset-progress` CLI is the recovery path for a frozen walk; an in-app clinician-facing undo stays out of scope.
- **`gate.redirect` as an `events` row.** S9b logs it as a structlog WARNING only. If S10 wants "tried to peek" as a behavioral signal, promote it to an `EventKind` then — it is ambiguous today (a stale tab or bookmark looks identical to intent).
- **`PatientSlice.timepoints` is dataset-derived.** `slice_to_timepoint` fills `timepoints=patient_timepoints(dataset, pid)` regardless of study mode; S9b routes the two UI consumers (`data-t-count`, summary total) through `timepoint_count = len(resolved.timepoints)` and locks it with test #40, but the dataclass field still lies on Geneva (24 dataset timepoints vs a 3-timepoint study). S8 should thread study timepoints into `slice_to_timepoint` or drop the field. Depends on: S8.
- **S9c export must know about `progress`.** A wide pivot should mark (or exclude) incomplete walks; `progress.completed_at` is the flag. Also inherits the S9a config-hash drift check. Depends on: S9c spec authoring.
- **S10 dwell time — bounded, not exact (**[review-fix R25]**).** Per-timepoint time-on-task = `server_ts` delta between consecutive `advance.ok` rows (plus `session.start` for the first), and `client_ts` on the same rows (R17) nets out render/network latency. But `sessions.find_open` resumes an open session indefinitely, so a clinician who closes the tab at t=1 and returns the next morning emits no marker and the t=1→t=2 delta is eighteen hours. S9b deliberately does not guess what a gap means; S10 must discard or flag deltas above a ceiling, or S10.5 adds a `session.resume` event kind. Do not describe the metric as clean until one of those lands.
- **S11 prerequisite (S9a R24, restated and partly discharged).** `bootstrap_session` still locks the arm on the *first viewable* GET. **[review-fix R21]** removes the two triggers S9b would otherwise have added: a gated GET past the frontier now decides on a pure read and writes nothing, so a bookmark or stale tab can no longer burn a randomization cell. What remains is the S9a exposure — opening a patient at all locks the arm — and S11 must still decide assignment-on-first-POST vs an admin un-assign path.
- **Route shape deviation.** ROADMAP said `/advance` with an `expected_timepoint` query param; S9b ships `t_index` in the path (§5.1). ROADMAP S9b block gets the pointer in commit 5.
- **Scope growth vs ROADMAP.** The ROADMAP's S9b bullets do not mention the GET gate, frozen answers, `progress` table, index progress or `HX-Push-Url`. §1 and §6 argue each is load-bearing for the invariant the session exists to enforce; the review should confirm or trim. Recorded in the ROADMAP line.
- **Confirm-before-advance dialog.** Rejected (§15). Revive only if the pilot reports mis-advances *and* the admin reset above proves insufficient.
- **`HX-Push-Url` and the Prev button.** S9b pushes from the server on every HTMX partial, which also fixes S2's Prev/`[` never updating the URL. If a clinician's browser history now fills with one entry per timepoint, consider `hx-replace-url` for backward moves. Cosmetic; observe in the next clinician session.

---

## 15. What Session 9b does NOT lock

- CSV export shape — S9c (it *does* lock `progress.completed_at` as the "walk complete" flag S9c will read).
- Arm randomization and AI-panel hiding — S11.
- Pane visual design, CTA styling, index layout — `/plan-design-review` follow-up (S9a's TODO still open).
- Multi-clinician concurrency — single shared connection; the frontier check-then-write is serialized by the event loop, not by a transaction.
- Real-data UI — S8.

Considered and explicitly **not** taken:

- **Gate only `/advance`, leave GET open.** Cheaper, and what a literal reading of the ROADMAP bullets gives. Rejected: `]`, the Next button and a typed URL would all still show t=180 before t=60 is answered; the gate would be decoration. The server-side check has to sit on the *view*.
- **Derive progress from `answers`.** No new table. Rejected in §6: it cannot express "frozen" and would unlock on the last radio click, skipping the advance act and its event.
- **Derive progress from the `advance.ok` events S9b is adding anyway (**[review-fix R28]** — the alternative §6 should have argued against, and did not).** `frontier = MAX(payload.to_t_index)` over `events WHERE kind='advance.ok'`, served by `ix_events_patient_timepoint`. It satisfies §6's own principle verbatim ("the frontier is an *act*, and acts get rows"), expresses "frozen" fine, and needs no migration 3, no DAO, no schema-fixture regeneration, no `config_hash` column and no clamp guard — §6's stated rebuttal only ever addressed deriving from `answers`, which is a different and much weaker proposal. Rejected anyway, for reasons that had to be spelled out rather than assumed: (1) `completed_at` has no `advance.ok` to hang on — the final advance emits `final: true` but "this pair is done" then becomes a payload JSON scan on every index render; (2) the gate is on the hot path of every patient GET, and a PK lookup on one row beats a `MAX` over a growing append-only log with a JSON extraction per row; (3) `events` is an audit log the S9c pseudonymization pass may rewrite or redact, and load-bearing application state must not live in something an export policy can touch; (4) the compare-and-set frontier guard (**[review-fix R29]**) has no equivalent over an append-only table. The table stays — but as a cache of an act, not as the act itself, which is why `advance.ok` still carries `to_t_index` and the two can be reconciled.
- **Client-side disabled button + client-side event POST.** Rejected: duplicates the completeness rule (`required`, drift, cleared rows) in JS; the blocked click goes to the server and the server emits the event (§7).
- **Auto-jump to the next unfinished patient on finish.** Rejected in favor of `/` with progress markers: the clinician chooses when to start the next chart, and the index doubles as the pilot's progress board. Trivial to flip later (one redirect target).
- **Confirm dialog before advancing.** Friction on every timepoint to protect against a mis-click on a button whose label only ever reads `Next timepoint ›` when complete. The admin reset (§14) is the recovery path.
- **`progress.unlocked_t_index` in minutes.** Would match `answers.timepoint`, but every gate comparison is on the URL ordinal and the mapping back needs a `.index()` that can miss under drift. Ordinal + `config_hash` + clamp is simpler and cannot 500.

---

## 16. What already exists (carried into S9b)

- **`web/study_session.bootstrap_session`** — splits into `read_frontier` (pure) + `bootstrap_session(..., frontier)` (**[review-fix R21]**); both existing tests need the `_AppState` stub extended (**[review-fix R19]**) — the draft's "every S9a test passes unchanged" was false.
- **`web/answer_capture.saved_answers`** — the completeness input; unchanged. Its `config_hash` drift WARNING keeps firing on locked panes (pre-fill still runs).
- **`db/answers.fetch_for_cell`** — one extra call per `/answer` 200 (remaining recompute) and per `/advance`. Served by the `ux_answers_cell` prefix (S9a R12).
- **`db/sessions.find_open` / `start_or_resume`** + **migration 2 `ux_sessions_open`** — `close` is their missing half; the S9a test #12 "new session after `ended_at`" is exactly the post-completion revisit.
- **`db/events.append` + `EventKind` guard** — three kinds appended; the `ValueError` guard forces #10.
- **`routes._require_clinician`, `_resolve_timepoint`, `_error_flash`, `_answer_status`** — reused. `_require_clinician` delegates its redirect to the new `_htmx_aware_redirect` (extraction, same bytes).
- **`templates/_answer_status.html`** — unchanged; the CTA rides behind it in the response body.
- **`static/answers.js`** — unchanged; its `beforeSwap` only matches `form.question`, so the `#advance-form` requests fall through to `advance.js`.
- **`static/keyboard.js::navigate`** — one early-return branch; boundary flashes and `[` unchanged.
- **`cli_support.render_html_for_preview`** — already seeds the clinician; now also walks the frontier.
- **`tests/conftest.py::study_client`, `study_clinician_id`, `tests/e2e/conftest.py::live_study_server`** — reused as-is. `live_study_server` is session-scoped and shared, but every e2e test logs in as a different clinician and `progress` is keyed per `(clinician, patient)`, so the new gated walk cannot leak a frontier into the S9a tests.
- **`tests/test_answer_routes.py`** — *not* reusable as-is: `T_INDEX = 1` (`:22`) is past the gate (**[review-fix R18]**).
- **`config/loader.compute_config_hash_from_models`** — unchanged; the `required` field enters the hash through `model_dump_json`.

---

## 17. Verification (end-to-end)

1. **Pytest:**
   ```
   uv run pytest                 # green, +45 functions
   uv run pytest -m e2e          # S2 + S6 + S9a + S9b walks green
   uv run pytest -m real_data    # unchanged
   ```

2. **Live walk:**
   ```
   rm -f /tmp/s9b.db*
   uv run ehr-simulator serve --config tests/fixtures/study/study_synthetic.yaml \
       --questions tests/fixtures/study/questions.yaml --db-path /tmp/s9b.db &
   curl -s -o /dev/null -X POST -d "clinician_name=Dr. Gate" http://localhost:8000/login
   COOKIE="ehrsim_clinician_id=$(sqlite3 /tmp/s9b.db 'SELECT clinician_id FROM clinicians LIMIT 1')"
   P=http://localhost:8000/patient/synth_001/timepoint

   curl -si --cookie "$COOKIE" $P/2 | head -1                                        # 303
   curl -si --cookie "$COOKIE" $P/2 | grep -c '<svg'                                # 0  (no data leaked)
   # [review-fix R24] plain POSTs now get POST-redirect-GET 303s; send HX-Request
   # to exercise the fragment contract the pane actually uses.
   HX='-H "HX-Request: true"'
   curl -si --cookie "$COOKIE" -H "HX-Request: true" -X POST "$P/0/advance" | head -1   # 409
   curl -s  --cookie "$COOKIE" -H "HX-Request: true" -X POST "$P/0/advance" | grep -o 'data-remaining="[0-9]*"'   # 6
   for kv in deterioration_6h=No survives_hospital=Yes good_outcome_3mo=65 dead_6mo=No confidence=3 contributing_factors=Labs; do
     curl -s --cookie "$COOKIE" -X POST -d "question_id=${kv%=*}&value=${kv#*=}" $P/0/answer | grep -o 'data-remaining="[0-9]*"'
   done                                                                              # 5 4 3 2 1 0
   curl -si --cookie "$COOKIE" -H "HX-Request: true" -X POST "$P/0/advance" | grep -i 'HX-Push-Url\|^HTTP' # 200, …/timepoint/1?chrome=epic
   curl -si --cookie "$COOKIE" -H "HX-Request: true" -X POST "$P/0/advance" | head -1                       # 412 (stale)
   curl -si --cookie "$COOKIE" -X POST "$P/0/advance" | grep -i '^HTTP\|^Location'                          # 303 → …/timepoint/1 (no-JS path)
   curl -si --cookie "$COOKIE" -X POST -d "question_id=deterioration_6h&value=Yes" $P/0/answer | head -1   # 409 (locked)
   curl -s  --cookie "$COOKIE" $P/0 | grep -c '<fieldset disabled'                  # 7
   curl -s  --cookie "$COOKIE" $P/0 | grep -o 'hx-history="false"'                  # review-fix R6
   sqlite3 /tmp/s9b.db "SELECT unlocked_t_index, completed_at FROM progress"       # 1|
   sqlite3 /tmp/s9b.db "SELECT count(*) FROM arm_assignments"                      # 1, not 2 (review-fix R21:
                                                                                   #   the first curl's gated t=2
                                                                                   #   GET locked no arm)
   # … repeat the six answers at /1 and /2, then:
   curl -si --cookie "$COOKIE" -X POST "$P/2/advance" | grep -i '^HTTP\|^Location'  # 303, /
   sqlite3 /tmp/s9b.db "SELECT unlocked_t_index, completed_at IS NOT NULL FROM progress"   # 2|1
   sqlite3 /tmp/s9b.db "SELECT count(*) FROM sessions WHERE ended_at IS NOT NULL"  # 1
   sqlite3 /tmp/s9b.db "SELECT kind, count(*) FROM events GROUP BY kind"
   #   advance.blocked|1  advance.ok|3  answer.upsert|18  clinician.login|1  session.end|1  session.start|1
   curl -s --cookie "$COOKIE" http://localhost:8000/ | grep -o 'is-complete'         # synth_001 done
   grep '"event_kind": "gate.redirect"' logs/current.jsonl | head -1                 # the first curl
   kill %1
   ```

3. **Preview walks the gate:**
   ```
   rm -rf /tmp/p && uv run ehr-simulator preview tests/fixtures/study/study_synthetic.yaml \
       --patient synth_001 --questions tests/fixtures/study/questions.yaml --html-out /tmp/p
   grep -c 'id="advance-form"' /tmp/p/synth_001_t{0,1,2}.html                        # 1 1 1
   grep -c 'data-t-index="2"'   /tmp/p/synth_001_t2.html                             # 1  (review-fix R23 —
                                                                                     #   the assertion that can fail)
   ```

If all three pass on a fresh clone post-`uv sync`, S9b is shipped.

---

## 18. Review history

Filled by `/plan-eng-review` (2026-09-16). Each accepted fix is annotated `[review-fix R<N>]` inline in the spec body; this table cross-references them. R18–R29 came from the outside voice (Codex was unauthenticated; a Claude subagent ran the pass with fresh context) and were each re-verified against the code before acceptance.

| Review-fix | Original design | Resolved design |
|---|---|---|
| R1 | §12: "Pre-S9b behavior changes, **exhaustively**" — 4 items, one of them in `test_db.py`. | Three more `test_db.py` tests assert the migration-version list literally and fail on migration 3: `test_apply_migrations_forward:153`, `test_apply_migrations_recovers_from_partial_apply:184`, `test_migration_2_rejects_second_open_session:626`. Added to §9, §11 commit 1 and §12. |
| R2 | `test_app_from_study_config_t_index_resolves_to_study_timepoints` "now asserts 303 to t=0". | `TestClient` is built with `follow_redirects=True`, so the drafted test would keep **passing** while silently testing t=0 — the worst failure mode for a regression test. Adaptation uses `follow_redirects=False`, asserts the `Location`, then unlocks and re-asserts `data-t-minutes="180.0"` at t=1. Made a §13 convention. |
| R3 | `advance.js` `htmx:afterSwap`: "when the new `#advance-form` carries `data-first-unanswered`, scroll + focus" — unscoped. | htmx 2.0.4 appends OOB-swapped nodes to the shared settle list and fires `htmx:afterSwap` on every entry, so the handler would also fire on each `/answer` 200 and yank focus out of a textarea mid-autosave (failing `e2e/test_answer_walk.py:72`). Scoped to `status === 409` on requests whose `requestConfig.elt` is `#advance-form`. |
| R4 | `_summary_card.html`'s `total` re-pointed at `timepoint_count`. | The template prints a dataset-derived total **twice** — `:1` and `:46`. Both route through `total`; test #40 asserts both. |
| R5 | `keyboard.js::navigate(+1)`: "if `#advance-btn` exists, `.click()` it and return" — position unstated. | The branch must precede the `next >= state.tCount` guard at `keyboard.js:67`; on the last timepoint `next == tCount`, so a later branch makes `Finish patient ✓` unreachable from the keyboard. e2e #45 presses `]` there instead of clicking. |
| R6 | `HX-Push-Url` on every HTMX partial GET, described as "one line per response". | It switches htmx's whole history subsystem on. (a) A cache-miss restore re-GETs with `HX-History-Restore-Request: true` and `innerHTML`-swaps the **whole body** — the route must answer that with `base.html`. (b) htmx otherwise snapshots rendered patient data into `localStorage`, outside SQLite, backups and the S9c pseudonymization policy, and a cache **hit** restores a stale open pane without consulting the gate. `hx-history="false"` on `<body>` kills the cache and forces every restore through (a). Tests #40b, #43. |
| R7 | §5.4's index reads `progress.list_for_clinician`; §13 forbids `routes.py` importing `progress`. | `gating.progress_overview(conn, *, clinician_id, patient_ids, timepoint_count) -> dict[str, PatientProgress]` in commit 2, consumed by both `GET /` and `_render_summary`. Test #18b. |
| R8 | `/advance` "advanced" → "`#patient-view` for `t+1` (open pane)"; no statement of where the render context comes from. | `SessionContext` is frozen and predates the write, so reusing it renders t+1 as **locked** — a dead end one click into the walk. Handler renders with `dataclasses.replace(ctx, frontier=…)`. |
| R9 | `advance.*` payload `{answered: len(saved), required: n_required}`. | `len(saved)` counts optional answers, so a filled `free_notes` emits `{"answered": 7, "required": 6}`. Split into `answered_required` / `answered_total`. |
| R10 | `update_request_context(patient_id=…)` after the study branch. | The `gate.redirect` WARNING and the middleware's per-request line then carry no patient and no timepoint — unattributable, in exactly the line §14 wants to promote to an event. Binding moved above the gate. Test #26d. |
| R11 | `unlock` upsert sets `config_hash = excluded.config_hash`. | That is the live hash, so the §8.2 drift WARNING disarms itself one advance into the pilot. `config_hash` is written on INSERT only; the row records the hash the walk started under. Test #6b. |
| R12 | A completed patient's revisit re-opens a session, "S9a test #12 semantics, unchanged". | Nothing can ever close that session — closing only happens on an advance, and a completed patient has none left — so every revisit leaks an un-closeable `sessions` row and an extra `session.start` into S10's dwell query. `sessions.find_latest` + a `completed` branch in `bootstrap_session`. Tests #9b, #20b, #32. |
| R13 | `cli_support.py` computes the preview clinician id twice (`:283` then `:285`). | The preview's new `progress.unlock` must target the same id the cookie carries; a future normalization change would silently turn every `--html-out` file into a redirect body. Redundant line deleted. |
| R14 | Jumper links `/timepoint/{{ resume_t_index[pid] }}`. | Jinja's default `Undefined` renders a missing key as `""` → `/patient/x/timepoint/?chrome=epic` → 404. Mapping is total over `all_patient_ids`; template uses `.get(pid, 0)`; test #39 asserts the non-study hrefs are byte-identical to S2. |
| R15 | `answer_all_required` POSTs a hard-coded list of six `(question_id, value)` pairs. | Drifts silently the day the fixture gains a question — every advance test flips to `blocked` with a confusing diff. Derived from `app.state.questions` with a per-`response_type` value picker plus a coverage assertion. |
| R16 | "45 new + 4 adapted; baseline 313 → ~358 default-suite functions." | Wrong three ways: e2e functions folded into the default suite, functions conflated with collected items, adapted count off by 20. Now 57 new (55 default + 2 e2e) + ~24 adapted; floor 368 collected / 10 deselected. Also: the double-submit regression is #31, not #33. |
| R17 | `/advance` has an empty body; no client clock fields. | `events.client_ts`/`client_seq` would be NULL on exactly the rows S10 reads for dwell time. `/advance` carries both, stamped by `advance.js`'s `configRequest`, with `await request.form()` hoisted above `bootstrap_session`. Test #29b. |
| R18 | §5.3's `/answer` gate treated as additive; `test_answer_routes.py` budgeted "+5". | `tests/test_answer_routes.py:22` sets `T_INDEX = 1` and `_url()` defaults to it, so ~12 existing tests POST past the frontier and become 409s (validation tests flip 422→409 because `record_answer` runs after the gate), and `:276` gets a silently-followed 303 that fails four lines later. Module re-pointed to `T_INDEX = 0`; §11 estimate raised to ~2 days. |
| R19 | §8.2: `last = len(app_state.study_timepoints) - 1`; §16: "Every S9a test on `bootstrap_session` passes unchanged." | `tests/test_study_session.py:11-13`'s `_AppState` stub defines only `write_counter` and `config_hash` → `AttributeError` in both existing tests. `read_frontier` uses `getattr(..., None)` and skips the clamp; the stub also gains the attribute. |
| R20 | "Test #26 asserts the body has neither `<svg` nor `id=\"patient-view\"`" — presented as the data-leak regression. | Both redirect shapes have **empty bodies by construction**, so the assertion passes whether or not slicing ran; §13's "any refactor that reorders them fails test #26" was false. Test #26b monkeypatches a call counter onto `routes.slice_to_timepoint` and asserts zero. |
| R21 | GET handler: `bootstrap_session` → `is_viewable` → redirect. | `bootstrap_session` **writes**: it locks the S11 randomization arm, inserts a session row and appends `session.start` — so a bookmark pointing past the frontier burned a randomization cell before being bounced, and S9b was adding two new triggers for the very problem §14 files as an S11 prerequisite. Split into `read_frontier` (pure) + `bootstrap_session(..., frontier)`; `is_viewable`/`pane_mode` take a `Frontier`. Test #26b asserts zero rows in `progress`, `sessions` **and** `arm_assignments`. |
| R22 | Deliverable #3 adds the "a required multi-select needs an explicit opt-out" rule to the example file's header comment. | Neither shipped questions file obeys it: `contributing_factors` is a required multi-select with no opt-out (`tests/fixtures/study/questions.yaml:30-33`, `configs/example_questions.yaml`), so the gate would force a fabricated contributing factor at every timepoint on the study's own reference config. `None of these` added to both files; test #3c enforces it. |
| R23 | "Without the step-wise unlock, `t1`/`t2` would be 303s and the CLI test would fail on `raise_for_status`"; CI greps `id="advance-form"` in `_t2.html`. | `render_html_for_preview` builds its `TestClient` with the default `follow_redirects=True` (`cli_support.py:295`), so an ungated preview writes the **t=0 body** — CTA included — into all three files and every stated guard passes. Preview client gets `follow_redirects=False`; CI and the CLI test key on `data-t-index`. |
| R24 | `_advance_cta.html` is a `<form hx-post=…>` with no `action`/`method`; §5.1 defines a plain-browser response only for *finished*. | Without htmx the submit POSTs to the current URL, which has no POST handler → blank 405, on the one **forward** control (the resume link already carries a real `href`). Form gets `action`+`method`; every outcome gets a plain-browser POST-redirect-GET 303. Tests #26c, #28b. |
| R25 | §8.3: "S10 gets per-timepoint dwell time from consecutive `advance.ok` rows with no further columns." | `sessions.find_open` resumes a session indefinitely, so a clinician returning the next morning yields an eighteen-hour delta with no marker. Claim qualified to "within one continuous sitting"; S10 must flag deltas above a ceiling. Filed in §14. |
| R26 | `advance.js` force-swaps 4xx and relies on `HX-Retarget`; ordering undocumented. | Verified in the vendored bundle and written into §7: retarget and reswap are applied **before** `shouldSwap` is computed and before `htmx:beforeSwap` fires, but the event is *dispatched on the pre-retarget element*, so listeners must be delegated on `document.body` and filter on `detail.requestConfig.elt`. |
| R27 | Assorted slips. | (a) §9 justified dropping the e2e `[` step with "t=0 has no previous"; that `[` is pressed at **t=1** (`test_answer_walk.py:44`) — the real reason is that the preceding `]` no longer navigates, and `:46`'s assertion is covered by the reload check at `:54-58`. (b) §2 #22 put `is-blocked` on `.advance-btn`; §7's markup puts it on the form. (c) §11's "commit 3 is green" holds only because #42/#43 assert classes and attributes, never computed styles. |
| R28 | §6 rejects "derive progress from `answers`" and presents that as having disposed of the no-new-table option. | The real alternative is deriving the frontier from the `advance.ok` events S9b adds anyway — which satisfies §6's own "acts get rows" principle and needs no migration. Rejected on four stated grounds (no home for `completed_at`, a `MAX` + JSON scan on the hot path, `events` being an audit log S9c may redact, and no compare-and-set equivalent), now written into §15 instead of assumed. |
| R29 | `unlock` is a `MAX(...)` upsert; correctness rests on "`async def` makes the check-then-write race-free". | `async def` guarantees nothing; the absence of an `await` between read and write does, and that invariant is invisible, untested, and one refactor (or R17's `await request.form()`) away from vanishing. `unlock` becomes `UPDATE … WHERE unlocked_t_index = :from` (compare-and-set, verified on SQLite 3.37); a lost race returns `False` and degrades to `stale`. §13 restates the concurrency reasoning correctly. Tests #6c, #30b. |

**Implementation notes (2026-09-16, post-review deviations, all cosmetic):** `seed_progress` takes the `TestClient` (it reads the cookie's clinician id and the app's `config_hash`) rather than `(db_path, clinician_id, …)`; `db/clinicians.lookup` (read-only) was added for `reset-progress` so a mistyped name cannot create a clinician; `test_gate_redirect_log_line_is_attributable` reads the JSONL sink because `capture_logs` bypasses the contextvars merge; the blocked-click e2e uses `page.click(force=True)` because Playwright treats `aria-disabled="true"` as non-actionable (a real pointer click is unaffected); one further pre-S9b test changed that the review missed — `test_cli_migrate_forward_then_idempotent` asserted `Applied migrations: [1, 2]` literally. Commit 6 (ruff pass) was a no-op: every commit was formatted before landing.

---

## Spec destination

`specs/session-09b-question-gating.md` (matches the `session-NN-name.md` convention). The pre-review draft is overwritten in place by the review pass.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 (fresh) | — | last run 2026-04-21, stale |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | ISSUES_OPEN | 29 issues, 3 critical (R18 test-suite blast radius, R21 gate-writes-before-refusing, R22 required multi-select with no opt-out), all 29 folded into the spec (R1–R29); 3 premise decisions left to the owner |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 (fresh) | — | last run 2026-05-06, stale; pane design still deferred per §14 |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 (fresh) | — | last run 2026-04-21, stale |
| Outside Voice | `/plan-eng-review` (auto) | Cross-model challenge | 1 | ISSUES_FOUND | Codex CLI returned 401 on token refresh; ran the Claude-subagent fallback. 14 findings, 12 folded (R18–R29), 1 partly corrected, 1 already covered |

- **CODEX:** not available — `codex exec` (codex-cli 0.44.0) failed auth with `Failed to refresh token: 401 Unauthorized` after 5 retries. Outside voice ran as a Claude subagent with fresh context, bounded the same way.
- **CROSS-MODEL:** high overlap on the htmx mechanics — both passes independently disassembled the vendored 2.0.4 bundle and reached the same conclusions about OOB swaps firing `htmx:afterSwap` (R3), `HX-Retarget` preceding `htmx:beforeSwap` (R26) and the history subsystem (R6). The subagent found six defects this review missed and every one was re-verified against the code before acceptance: `T_INDEX = 1` making ~12 existing answer-route tests collide with the new gate (R18, the single largest cost item in the session), the `_AppState` stub lacking `study_timepoints` (R19), the data-leak regression being vacuous because redirect bodies are empty (R20), `bootstrap_session` writing before the gate refuses (R21, the one that actually matters for study validity — it burns S11 randomization cells from a bookmark), `contributing_factors` being a required multi-select with no opt-out on both shipped configs (R22), and `TestClient`'s `follow_redirects=True` defeating the preview/CI guard (R23). One of its findings was corrected rather than folded verbatim: it reported that `htmx:beforeSwap`'s `detail.target` is the *pre*-retarget element — it is not (`detail.target` is already retargeted; the *dispatch node* is the old one), and the practical advice it drew from that, filter on `detail.requestConfig.elt`, is right either way and is now written into §7 as R26. Its strategic read — that the plan is written as additive when it is a behavioral change to the app's central route — is the reason §12's "exhaustive" list and §11's estimate both moved.
- **VERDICT:** CLEARED — 29 fixes applied inline. The three premise-level items below were answered by the owner on 2026-09-16 before commit 1: (a) keep the GET gate + frozen answers, (b) ship `reset-progress` in S9b, (c) push `HX-Push-Url` on every partial.

**RESOLVED DECISIONS (review recommendation, kept):**
- **Finish → `/` with progress markers vs auto-jump to the next unfinished patient.** Kept as drafted (§15). The index doubles as the pilot's progress board and the clinician chooses when to start the next chart; reversing it is one redirect target and one test.
- **Route shape: `t_index` in the path vs the ROADMAP's flat `/advance?expected_timepoint=`.** Kept as drafted (§5.1) — it shares the patient preamble with the other two routes and makes the logs self-describing. The ROADMAP gets the pointer in commit 5.
- **Storing the frontier in a table rather than deriving it from `advance.ok` events.** Kept, but the justification was rewritten (R28) because §6's original rebuttal answered a weaker alternative than the one that exists.

**RESOLVED DECISIONS (owner, 2026-09-16 — recommendation kept on all three):**

- **(a) The GET gate + frozen answers, or gate only `/advance`.** S9b's premise is that a clinician may not *see* a timepoint past their frontier and may not *revise* an answer once they have advanced. The ROADMAP's S9b bullets ask for neither — they ask for a disabled-unless-complete CTA and server-side enforcement on `/advance`. §1 and §6 argue the view has to be gated or the gate is decoration (`]`, the Next button and a typed URL would all still show t=180 before t=60 is answered), and that letting a clinician revise t=60 after reading t=180 is the same contamination as showing the data early. I agree with that reasoning and recommend keeping it: the study's primary endpoint is "what did clinician X believe at time T", and without both halves the endpoint is not measurable. But this is a protocol commitment, not a code preference — it makes a mis-advance unrecoverable for the clinician, it is the thing a pre-registration document has to describe, and it is worth an explicit yes from the person who owns the study design. The alternative (gate `/advance` only, leave GET and answers open) is roughly one commit smaller, removes R20/R21 entirely, and leaves the validity hole §1 opens with.
- **(b) Ship the admin `reset-progress` CLI in S9b, or defer it as §14 does.** The draft defers it with the revival criterion "the first real mis-advance report", which in a research pilot means the first lost data point — a clinician who fat-fingers `]` at t=0 has silently frozen six answers and there is no in-app path back. Because decision (a) is what creates the trap, the two should be answered together. I recommend shipping a minimal `ehr-simulator reset-progress --db-path P --clinician NAME --patient PID [--to-t-index N]` in S9b: a `progress.reset` DAO call, deletion of `answers` rows past `N`, one `progress.reset` event, roughly 60 LOC and 4 tests, and it reuses the migration and DAO that commit 1 already lands. The alternative is to ship without it and accept that the first mis-advance is repaired with `sqlite3` by hand — survivable on a single-laptop pilot the researcher operates themselves, and genuinely cheaper now.
- **(c) `HX-Push-Url` on every HTMX partial GET, or only on `/advance` and the gate redirect.** The draft pushes from every partial; R6 showed that decision carries an htmx-history contract with it (a server-side restore branch, `hx-history="false"` to keep patient data out of `localStorage`, and two more tests), and it changes the non-study S2 path too. I recommend keeping the broad version: the failure it prevents is a clinician reloading a stale URL and landing on a locked pane whose first line is "Locked — answered before you advanced", which is exactly the confusing moment a pilot cannot afford, and it also fixes S2's Prev/`[` never updating the address bar. The alternative — push only where the frontier moves — is smaller, keeps S2 byte-identical, and leaves the address bar stale on backward navigation, which is a cosmetic wrong rather than a misleading one. Either way R6's `hx-history="false"` should ship, since any push at all turns the snapshot cache on.

NO UNRESOLVED DECISIONS
