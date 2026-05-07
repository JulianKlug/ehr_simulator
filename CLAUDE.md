# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Sessions 1-4 shipped. Python 3.11 package at `src/ehr_simulator/` with:

- **Data contract** (`ingestion/canonical.py`): four pandera schemas locking the canonical in-memory shapes (`SCALAR_TS`, `ADMISSION`, `IMAGING`, `AI_OUTPUT`).
- **Adapters** (`ingestion/`): `synthetic` (reference), `geneva` (real Geneva CSV), `mimic` (MIMIC-III CSV). Geneva + MIMIC share `_shared.py` (CSV reader, normalisation params, sidecar contract validation, categorical decoding, panel builders). `__init__.py` re-exports `load_geneva`, `GenevaDataset`, `load_mimic`, `MimicDataset`.
- **Sidecar drift gate**: `_shared.parse_normalisation_sidecar(..., check=True)` validates the columns/order/numeric content of the normalisation CSV against a frozen JSON expectation. CI runs both adapters' real-data sidecar smoke and fails on drift.
- **FastAPI + HTMX UI** (`web/`): `app.py` factory + lifespan, `routes.py` (timepoint slicing + per-patient routes), `panels.py` (5-state taxonomy: loading / empty-expected / empty-unexpected / partial / error), `charts.py` (plotnine SVG renderer with a11y fallback table), Jinja templates and static assets. CLI entrypoint `ehr-simulator serve` boots the server.
- **Logging** (`logging.py`): structlog JSONL pipeline rolling at UTC midnight to `./logs/current.jsonl`.
- **Tests**: 110 tests across 13 files (canonical, synthetic, geneva, mimic, shared, charts, panels, routes, cli, logging, a11y, static_assets, data_contract). 4 additional `@pytest.mark.real_data` tests run only in CI's real-data smoke job.

Still not shipped: SQLite, login/clinician identity, question-answering with per-timepoint gating, CSV export, AI-on/AI-off randomization, MIMIC/Geneva real-data wired into the UI. Those land in Sessions 5-11 per `specs/ROADMAP.md`.

Commands:

- `uv sync` — install dependencies (generates `uv.lock` on first run).
- `uv run pytest` — run the test suite (110 tests, parallelized via `pytest-xdist`; real-data tests deselected by default).
- `uv run pytest -m real_data` — run the real-data smoke suite (requires Geneva + MIMIC CSVs at `.EXAMPLE_DATA_PATHS`).
- `uv run ruff check .` — lint.
- `uv run ruff format .` — format.
- `uv run ehr-simulator serve` — boot the FastAPI server at http://localhost:8000.

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, `ruff check`, `ruff format --check`, `pytest` on Python 3.11 and 3.12, plus a real-data smoke job that exercises both adapters and the sidecar drift gate.

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
