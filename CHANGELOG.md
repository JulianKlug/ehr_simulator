# Changelog

## Unreleased — 1.0.0

The first public release. Version and tag are set by the owner after release acceptance (`docs/release-checklist.md`).

### Simulator
- Replays patient timeseries at configured timepoints relative to first contact, revealing data up to the current timepoint only.
- Six panels (admission, vitals, neuro/support, labs, imaging, AI) in two layouts (`epic`, `dense`), plotnine SVG charts with accessible table fallbacks.
- Adapters: synthetic (built in), Geneva stroke registry CSV with Geneva AI predictions and SHAP explanations, MIMIC-III CSV. No real data is bundled (`docs/reference-data.md`).
- `serve --demo`: config-free synthetic demonstration, bannered on every page.

### Studies
- Per-timepoint questions (likert, probability, categorical, multi-select, free text) with conditional display and rule-derived answers. Every required answer gates the advance, and earlier timepoints are frozen.
- One database per study, with append-only, versioned configuration history. Every case is pinned to its version.
- Phase 2:
  - adaptive block randomisation with an explicit Start case
  - case lifecycle with timeouts and replacement cases
  - arm-dependent AI delivery
  - practice cases
  - browser telemetry and panel exposure
  - single-tab enforcement
  - clinician characteristics
- `export-phase2`: linked, pseudonymised research bundle with a manifest of digests and row counts. `export-answers`: single-configuration CSV.

### Analysis
- `ehr_simulator.analysis`: read-only bundle validator, descriptive per-version summary tables, five supplementary descriptive figures (SVG and PDF, one file per config version), atomic report publication.
- `notebooks/phase2_analysis.ipynb`: reproduces every figure and table from the bundle alone (opt-in `analysis` dependency group).

### Release hygiene
- The sdist is built from an allowlist. `scripts/check_dist.sh` refuses untracked files in either artifact.
- CI runs the README quickstart from a fresh clone.
