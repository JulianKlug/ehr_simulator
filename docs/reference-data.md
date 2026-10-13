# Reference data

What data ships with this repository, what does not, and how to obtain the rest.

**No real patient record, cohort aggregate, Geneva sample or MIMIC record is distributed here.** All shipped data is synthetic or fabricated.

## Built-in synthetic patients

`ingestion/synthetic.py` generates three deterministic patients: `synth_001`, `synth_002` and `synth_003`.

- They cover all four canonical shapes (`docs/data-contract.md`), including AI output.
- The values are physiologically plausible noise, not clinical cases.
- `synth_003` carries `*imputed*` rows so you can watch the adapter filter them.
- These are what `serve --demo` and `serve` without a config show.

## Checked-in fixtures (`tests/fixtures/`)

Each builder is deterministic. Its `--check` mode runs in CI and fails if the committed files drift.

| Directory | Content | Builder |
| --- | --- | --- |
| `geneva/` | Two synthetic patients in the Geneva preprocessed-features CSV shape, with the encoding and normalisation tables | `build_geneva_fixture.py` |
| `mimic/` | Two synthetic patients in the MIMIC-III preprocessed-features CSV shape, with the encoding and normalisation tables | `build_mimic_fixture.py` |
| `geneva_ai/` | Fabricated Geneva AI artifacts: predictions pickle, SHAP arrays, split file, patient id sidecar, model file | `build_geneva_ai_fixture.py` |
| `analysis_bundle/` | `walked/` and `empty/` Phase 2 export bundles, produced by the shipped exporter from a synthetic study | `build_analysis_bundle.py` |
| `study/`, `db/` | Synthetic study configs and questions; the expected migration-1 schema | – |

The patient rows in the `geneva/` and `mimic/` fixtures are hand-made constants. `categorical_variable_encoding.csv` is schema only (category labels, alphabetical). The means and stds in `*normalisation_parameters.csv` are fabricated (row i: 10·(i+1), i+1); `tests/test_shared.py` fails if real values return. Real normalisation parameters stay with the data owner.

To try the Geneva and MIMIC adapters without real data, run them on the fixtures:

```bash
uv run ehr-simulator validate-adapter tests/fixtures/study/study_geneva.yaml
uv run ehr-simulator validate-adapter tests/fixtures/study/study_mimic.yaml
```

The output reports the `made_up_orphan_var` issue on purpose: the fixture exercises the orphan-variable path.

## MIMIC-III

MIMIC-III is credentialed-access data. Obtain it only from PhysioNet: <https://physionet.org/content/mimiciii/>.

Access requires:

1. a PhysioNet account and credentialing;
2. completion of the required human-subjects training;
3. signature of the data use agreement.

Never commit MIMIC records, derived per-patient rows or screenshots of them to this repository. The `mimic` adapter expects the preprocessed-features CSV shape documented by `tests/fixtures/mimic/` and `docs/data-contract.md`.

## Geneva stroke data and AI artifacts

The Geneva stroke registry data and the Geneva AI model artifacts are **not included**, and no Geneva sample is published. Access is governed by the data owners and their ethics approvals.

Researchers with access configure a `dataset: geneva` study as follows:

- `csv_path` points at the local data.
- A `geneva_ai` block points at the local artifacts.
- The setup steps are under *Running a Phase 2 study* in `README.md`.

## Where local data goes

- Local pilot configs live in `configs/`. Everything there except `example_*.yaml` is git ignored, because those configs can name real paths and patient ids.
- Databases (`data/`, `*.db`), exports (`exports/`), analysis output (`analysis_out/`) and pseudonym secrets (`keys/`, `*.secret`) are also git ignored.
- The source distribution is built from an allowlist (`src/`, `README.md`, `LICENSE`). Run `scripts/check_dist.sh` before you publish a build.
