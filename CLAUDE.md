# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Session 1 (data contract + repo scaffolding) shipped: Python 3.11 package at `src/ehr_simulator/` with four pandera schemas locking the canonical in-memory shapes (`SCALAR_TS`, `ADMISSION`, `IMAGING`, `AI_OUTPUT`), a synthetic reference adapter, and 15 contract tests. No FastAPI server, SQLite, HTMX, MIMIC/Geneva adapters, or UI yet — those are later sessions per `specs/`.

Commands:

- `uv sync` — install dependencies (generates `uv.lock` on first run).
- `uv run pytest` — run the test suite (15 tests, parallelized via `pytest-xdist`).
- `uv run ruff check .` — lint.
- `uv run ruff format .` — format.

CI (`.github/workflows/ci.yml`) runs `uv sync --locked`, `ruff check`, `ruff format --check`, and `pytest` on Python 3.11 and 3.12.

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
