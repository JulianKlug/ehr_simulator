# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Sessions 1-6, 9a and 9b shipped. Python 3.11 package at `src/ehr_simulator/` with:

- **Data contract** (`ingestion/canonical.py`): four pandera schemas locking the canonical in-memory shapes (`SCALAR_TS`, `ADMISSION`, `IMAGING`, `AI_OUTPUT`).
- **Adapters** (`ingestion/`): `synthetic` (reference), `geneva` (real Geneva CSV), `mimic` (MIMIC-III CSV). Geneva + MIMIC share `_shared.py` (CSV reader, normalisation params, sidecar contract validation, categorical decoding, panel builders). `__init__.py` re-exports `load_geneva`, `GenevaDataset`, `load_mimic`, `MimicDataset`.
- **Sidecar drift gate**: `_shared.parse_normalisation_sidecar(..., check=True)` validates the columns/order/numeric content of the normalisation CSV against a frozen JSON expectation. CI re-runs both adapters' fixture builders with `--check` and fails on drift.
- **FastAPI + HTMX UI** (`web/`): `app.py` factory + lifespan, `routes.py` (timepoint slicing + per-patient routes), `panels.py` (5-state taxonomy: loading / empty-expected / empty-unexpected / partial / error), `charts.py` (plotnine SVG renderer with a11y fallback table), Jinja templates and static assets. In study mode (`serve --config/--questions`) the patient view also renders `_questions_pane.html`.
- **SQLite** (`db/`): `connection.py` (`journal_mode=WAL`, `synchronous=NORMAL`, `foreign_keys=ON`; path resolves `--db-path` → `EHR_SIM_DB_PATH` → study YAML `db_path` → `data/ehr_simulator.db`), `migrations.py` (three migrations, applied in the lifespan), DAOs for `clinicians`, `sessions`, `arm_assignments`, `answers`, `progress`, `events`, `ingestion_issues`, `backup.py` (shutdown snapshot into `<db parent>/backups`, `--backup-dir` overrides; skipped unless the session wrote something), `cookies.py`.
- **Login** (`web/routes.py`): `/login` + `/logout`. The typed name is case-folded and SHA256-truncated to a 16-hex `clinician_id`, carried in an unsigned cookie; every clinician-facing route sends a cookie that misses `app.state.known_clinicians` to `/login` (303, or 200 + `HX-Redirect` for HTMX requests).
- **Answer capture** (`web/answer_capture.py`, `web/study_session.py`, `static/answers.js`): `POST /patient/{pid}/timepoint/{t_index}/answer` upserts one `answers` row per `(clinician, patient, timepoint, question)` and appends an `answer.upsert` / `answer.clear` event. The pane auto-saves on change, free-text after a 1.5s pause; an all-blank submission deletes the row so the gate reads it as unanswered. Outside study mode the route returns 409.
- **Question gating** (`web/gating.py`, `db/progress.py`, `static/advance.js`): migration 3 adds `progress`, one row per `(clinician, patient)` holding `unlocked_t_index` + `completed_at`; `unlock` is a compare-and-set on the observed frontier. `study_session.read_frontier` is the pure read the GET gate uses before `bootstrap_session` writes anything — a request past the frontier is redirected (303, or `HX-Redirect`) before `slice_to_timepoint` runs, so it cannot leak data or burn an S11 arm. `POST /patient/{pid}/timepoint/{t_index}/advance` is the one forward path: **200** advanced/finished (`HX-Push-Url` to `t+1`, or `HX-Redirect: /` after `mark_complete` + `sessions.close`), **409** blocked (the `_advance_cta.html` fragment retargeted onto itself, `advance.blocked` event, client scrolls to the first unanswered question), **412** stale (`t_index != unlocked_t_index`, or a CAS miss — answered with the frontier's view, never a write). Non-HTMX submits get POST-redirect-GET 303s. Timepoints behind the frontier render read-only and `POST /answer` refuses them with 409. Only `required: true` questions (`config/questions.py`, `StrictBool`, default `True`) block; a required multi-select must ship a `None of these` option. The index and patient jumper show per-patient progress markers and resume at each frontier.
- **Logging** (`logging.py`): structlog JSONL pipeline rolling at UTC midnight to `./logs/current.jsonl`.
- **Tests**: 393 tests across 24 files. Two marker suites are deselected by default: 8 Playwright tests in `tests/e2e/` (`-m e2e`) and 2 `@pytest.mark.real_data` adapter smokes that need the local Geneva + MIMIC CSVs.

Still not shipped: Geneva AI predictions (S7), Geneva real-data wired into the UI (S8), CSV export (S9c), divergence view (S10), AI-on/AI-off randomization (S11 — `arm_assignments` currently always returns the `phase1_stub` `no_ai` arm). See `specs/ROADMAP.md`.

Commands:

- `uv sync` — install dependencies (generates `uv.lock` on first run).
- `uv run pytest` — run the default suite (393 tests, parallelized via `pytest-xdist`; e2e + real-data deselected).
- `uv run pytest -m e2e` — run the Playwright walks (needs chromium installed).
- `uv run pytest -m real_data` — run the real-data smoke suite locally (requires Geneva + MIMIC CSVs at `.EXAMPLE_DATA_PATHS`).
- `uv run ruff check .` — lint.
- `uv run ruff format .` — format.
- `uv run ehr-simulator serve` — boot the FastAPI server at http://localhost:8000. Add `--config configs/example_config.yaml --questions configs/example_questions.yaml` for study mode (both flags or neither); `--db-path`/`--backup-dir` relocate persistence. Any of those four flags disables `--reload` (uvicorn needs the import-string entry point).
- `uv run ehr-simulator validate-config`, `validate-adapter`, `preflight`, `preview` — config and dataset checks; `preview --patient P --questions Q --html-out DIR` dumps the rendered per-timepoint HTML, questions pane included.
- `uv run ehr-simulator migrate [--db-path P]` — apply pending migrations and checkpoint the WAL.
- `uv run ehr-simulator backup [--db-path P] [--backup-dir D]` — snapshot the DB (defaults `data/ehr_simulator.db` → `data/backups/`).
- `uv run ehr-simulator reset-progress STUDY_CONFIG --clinician NAME --patient PID [--to-t-index N] [--db-path P]` — operator recovery for a mis-advance: rewinds the frontier to `N` (default 0), clears `completed_at`, deletes answers strictly after `N`, records one `progress.reset` event. Unknown clinician, no walk, or an index outside the study exits 1 before any write.

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest` on Python 3.11 and 3.12, then the data-contract and fixture-sidecar drift checks, a CLI smoke (`validate-config`/`validate-adapter`/`preflight`/`preview` + a `migrate`/`backup` round-trip), and a second job for the Playwright e2e suite. The `real_data` suite runs locally only — CI has no access to the CSVs.

## What's being built

A browser-based EHR simulator that replays historical patient timeseries to a clinician at discrete timepoints, optionally displays precomputed AI model output alongside, and prompts the clinician to answer questions per timepoint. The goal is to evaluate how AI assistance changes clinician assessments and decisions. See `plan.md` for the full spec; `example_questions.md` has sample prompts.

Key architectural constraints from `plan.md` that cross multiple components and are easy to miss:

- **Timepoints are relative to first patient contact (t=0)** and configured per-study in a settings file alongside the unit (minutes/hours) and the ordered list of patient ids. The simulator reveals data up to and including the current timepoint only.
- **Data ingestion must be modular** — the simulator should accept differently-formatted input sources, not be hard-coded to one schema. Example inputs listed in `.EXAMPLE_DATA_PATHS` come from MIMIC-III and the Geneva stroke dataset.
- **For the example CSVs referenced in `.EXAMPLE_DATA_PATHS`, skip rows where the `source` column contains `"imputed"`** — only non-imputed datapoints are used.
- **AI output is consumed, not produced.** The simulator displays precomputed per-timepoint model output; it does not run models. Clinical safety features are explicitly out of scope (local-only use).
- **Question gating:** all questions for a timepoint must be answered before advancing to the next timepoint. Responses are keyed by `(patient_id, clinician_name, timepoint)` and must export to CSV with one column per question.
- **Phase 2 adds randomized AI-vs-no-AI arms per clinician-patient pair**; answer records must carry a flag indicating whether AI assistance was visible. Design data models with this in mind even if Phase 1 ships first.

## Example data

The CSVs in `.EXAMPLE_DATA_PATHS` live on the local filesystem under `/mnt/data1/klug/datasets/opsum/...` and are not in the repo. Don't assume their schema without reading them first.

## gstack

Use the `/browse` skill from gstack for all web browsing. Never use `mcp__claude-in-chrome__*` tools.

Available gstack skills: `/office-hours`, `/plan-ceo-review`, `/plan-eng-review`, `/plan-design-review`, `/design-consultation`, `/design-shotgun`, `/design-html`, `/review`, `/ship`, `/land-and-deploy`, `/canary`, `/benchmark`, `/browse`, `/connect-chrome`, `/qa`, `/qa-only`, `/design-review`, `/setup-browser-cookies`, `/setup-deploy`, `/retro`, `/investigate`, `/document-release`, `/codex`, `/cso`, `/autoplan`, `/plan-devex-review`, `/devex-review`, `/careful`, `/freeze`, `/guard`, `/unfreeze`, `/gstack-upgrade`, `/learn`.

Teammates without gstack can install it with:

```
git clone --single-branch --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup
```

(Requires [bun](https://bun.sh).)

## Skill routing

When the user's request matches an available skill, ALWAYS invoke it using the Skill
tool as your FIRST action. Do NOT answer directly, do NOT use other tools first.
The skill has specialized workflows that produce better results than ad-hoc answers.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke office-hours
- Bugs, errors, "why is this broken", 500 errors → invoke investigate
- Ship, deploy, push, create PR → invoke ship
- QA, test the site, find bugs → invoke qa
- Code review, check my diff → invoke review
- Update docs after shipping → invoke document-release
- Weekly retro → invoke retro
- Design system, brand → invoke design-consultation
- Visual audit, design polish → invoke design-review
- Architecture review → invoke plan-eng-review
- Save progress, checkpoint, resume → invoke checkpoint
- Code quality, health check → invoke health
