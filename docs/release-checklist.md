# Release checklist

Work through this list before every tagged release. Steps marked **owner** are manual decisions that tests cannot make. Never tag from a commit that CI has not tested.

## 1. Automated gates (CI on the release commit)

- [ ] **`test` job, Python 3.11 and 3.12:**
  - `uv sync --locked --group analysis`
  - `ruff check` and `ruff format --check`
  - `pytest -m 'not e2e and not real_data'` (unit, integration, slow)
  - `pytest -m notebook`
  - the drift checks: data contract, Geneva, MIMIC, Geneva AI, analysis bundle
  - the CLI, DB, export and Phase 2 export smokes
- [ ] **`e2e` job:** Playwright walks.
- [ ] **`quickstart` job:**
  - a fresh clone of the tested commit runs `uv sync --locked` → `serve --demo` → sign in → `synth_001` (`scripts/quickstart_smoke.sh`)
  - `scripts/check_dist.sh`

## 2. Local gates (release checkout)

- [ ] `bash scripts/check_dist.sh` **in your own checkout**. Local pilot configs (`configs/.gitignore`) and other untracked files exist only there, and the guard fails if any reaches the sdist or wheel.
- [ ] `git status --ignored` shows no file you intend to ship. `git ls-files` shows no database, secret, keyfile, export, real record or real-data figure.
- [ ] Optional, needs access: `uv run pytest -m real_data` with the Geneva and MIMIC CSVs and the Geneva AI artifact (`.EXAMPLE_DATA_PATHS`).
- [ ] `uv run python scripts/gen_data_contract.py --check`. Regenerate with the script; never hand edit `docs/data-contract.md`.

## 3. Dependencies and licence

- [ ] `uv lock --check` passes, and the lockfile is committed.
- [ ] `LICENSE` is MIT with the correct copyright holder and year. `pyproject.toml` declares `license = "MIT"`, and the wheel carries `dist-info/licenses/LICENSE`.
- [ ] Runtime dependencies have MIT-compatible licences. Notebook tooling stays in the opt-in `analysis` group.

## 4. Documentation

- [ ] The `README.md` quickstart matches `scripts/quickstart_smoke.sh`.
- [ ] The *Running a Phase 2 study* section matches the CLI (`--help` of each command).
- [ ] `docs/reference-data.md`, `docs/analysis.md` and `docs/analysis-contract.md` match the code.
- [ ] `CHANGELOG.md` has an entry for the release.
- [ ] `CLAUDE.md` and `specs/ROADMAP.md` are current.
- [ ] `TODOS.md`: no open item is release blocking, and each deferred item states why.

## 5. Data handling and disclosure (owner)

- [ ] No real Geneva or MIMIC record, derived per-patient row or screenshot is in the repository, the wheel, the sdist, the release notes or preprint material.
- [ ] No cohort aggregate is published (owner decision 2026-10-13): fixture normalisation parameters stay fabricated (`tests/test_shared.py` pins them), and no table, figure or note carries real cohort statistics.
- [x] Git history: commits before the S12c fixture change carry real Geneva cohort means/stds in `tests/fixtures/{geneva,mimic}/*normalisation_parameters.csv`, public on GitHub since session 3. **Accepted (owner, 2026-10-13):** these summary statistics already appear in previous publications; no history rewrite.
- [x] Git history also carries other real-data facts later redacted from the working tree (S12c): four real Geneva case ids, cohort and test-split sizes, the outcome event count, the prediction range, the SHAP base value, imaging-data patient counts. **Accepted exposure (owner, 2026-10-13):** no history rewrite.
- [x] Working tree: no real-data value (ids, counts, prevalences, statistics, model outputs) in code, tests, docs or specs. Local paths and extraction timestamps are allowed. **Swept 2026-10-14 (current tree, sdist, wheel) and redacted; governance kept as is (owner, 2026-10-19).**
- [ ] No Geneva sample is published. Only change this with explicit written sharing permission (decision 2026-10-13: none exists).
- [ ] Demo screenshots and published figures come from synthetic data. Any figure from a real cohort has passed manual disclosure review (small cells).
- [ ] The example Phase 2 questions and profile lists are labelled placeholders. Replacing them with the real study instrument is the owner's next step (2026-10-19).

## 6. Scientific acceptance (owner, spec §11)

- [x] Analysis meanings confirmed (owner, 2026-10-19): activated measured cases as the cohort; reached timepoints as the answer/exposure denominator; unknown never zero or false; hidden never missing, and only at reached timepoints (an unreached conditional question is missing by reach status); rule answers kept distinct; withheld free text is answered (free text is not used currently); unknown AI delivery makes failure unknown; ITT arms; configuration versions never pooled.
- [x] The figures are printed at one column width and in greyscale. Check categories, medians, units, n/denominators, missingness, one-arm strips and captions against the synthetic reference bundle. **Greyscale accepted (owner, 2026-10-19); v1 and v2 must not share a figure: one file per configuration version.**
- [x] A synthetic study is exported with `export-phase2`, and the notebook runs on the bundle alone, without the database. **Verified 2026-10-14: tables byte-identical to the library, database, secret and keyfile unreadable.**
- [ ] A colleague with a fresh checkout reaches `synth_001` from the README quickstart. Owner's next step (2026-10-19).
- [ ] Descriptive-only claims are reviewed. No inferential statistic or estimand appears without an approved analysis plan.
- [ ] Decide whether the release ships the provisional figure set or waits for the analysis plan's figures. **Owner (2026-10-19):** the five figures are confirmatory / supplementary, not the main message; main-message figures come from the approved analysis plan in a follow-on session.
- [x] Figure height: resolved by the S12c re-layout and one file per configuration version; all figures fit 9.5 in with the reference bundle. Figure 1 crowds beyond about 4 options and figure 3 grows with the question count: re-check with the real instrument.

## 7. Version and tag (owner, after all of the above and the preprint submission)

1. Set `version` in `pyproject.toml` (e.g. `1.0.0`), run `uv lock`, and move the `CHANGELOG.md` entry from *Unreleased* to the version and date.
2. Commit, push, and wait for CI to pass on that commit.
3. `git tag -a v1.0.0 -m "v1.0.0"` on that exact commit, then `git show v1.0.0` to verify it.
4. Push the tag and publish release notes that match `CHANGELOG.md`.

No test or CI job creates a tag.
