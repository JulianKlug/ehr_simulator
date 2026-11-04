# EHR simulator

A browser-based simulated electronic health record. It replays a real patient's
timeline at a few discrete moments in time, optionally shows AI input, and asks a short set of questions at each timepoint.


## Quickstart (about ten minutes)

Requires [`git`](https://git-scm.com/) and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/JulianKlug/ehr_simulator.git
cd ehr_simulator
uv sync --locked
uv run ehr-simulator serve --demo
# open http://127.0.0.1:8000; sign in; open synth_001
```


`--demo` is the config free synthetic mode. It needs no data, study config or
secret, and it never creates a study case. 

The demo writes `./data/ehr_simulator.db`, its backups
`./data/backups/ehr_simulator_*.db` and `./logs/` under the directory you
started it from. To reset it, stop the server and delete the demo files only.
Study databases, their backups under `data/backups/<study_id>/` and the shared
`logs/` stay:

```bash
rm -f data/ehr_simulator.db data/ehr_simulator.db-wal data/ehr_simulator.db-shm data/backups/ehr_simulator_*.db
```

Running a study is a separate path: see *Running a Phase 2 study* below.

---

## Running it

Requires [`uv`](https://docs.astral.sh/uv/) on the machine.

```bash
uv sync --locked
uv run ehr-simulator serve \
  --config configs/example_config.yaml \
  --questions configs/example_questions.yaml
```

---

## Running a Phase 2 study

A study config with a `randomisation` block (see
`configs/example_phase2_config.yaml`) runs Phase 2: clinicians press
**Start case** and get their next patient and arm from a stored schedule.
Operator sequence:

```bash
uv run ehr-simulator validate-config STUDY.yaml QUESTIONS.yaml
uv run ehr-simulator activate-config STUDY.yaml QUESTIONS.yaml --version v1 --description "…"
uv run ehr-simulator serve --config STUDY.yaml --questions QUESTIONS.yaml
uv run ehr-simulator expire-cases STUDY.yaml      # cases whose clinician never came back
uv run ehr-simulator backup --db-path data/study_<study_id>.db
uv run ehr-simulator export-phase2 STUDY.yaml --pseudonym-secret keys/pseudonym.secret --out-dir BUNDLE  # the research bundle
```

**Analysis.** The analysis reads only the exported bundle. It never reads the
database, the secret or the keyfile. Copy only `BUNDLE/` into the analysis
environment, then:

```bash
uv sync --locked --group analysis
# absolute paths: the notebook kernel may not start in your shell's directory
EHR_SIM_PHASE2_BUNDLE="$PWD/BUNDLE" EHR_SIM_ANALYSIS_OUT="$PWD/analysis_out/phase2" \
  uv run jupyter execute notebooks/phase2_analysis.ipynb   # or open it in Jupyter
```

**Geneva AI.** A `dataset: geneva` study delivers real AI output through a
`geneva_ai` block (`predictions_path`, `patient_ids_path`, `model_path`,
`model_id`, `explanations_dir` — optional, required with `randomisation`;
relative to the YAML). Before
`activate-config`:

```bash
# once: the artifacts carry no patient ids; export the test split order
uv run python scripts/export_geneva_test_ids.py SPLIT.pth test_predictions.pkl test_patient_ids.csv
uv run ehr-simulator validate-adapter STUDY.yaml   # prints the loaded AI provenance
```

Copy the three printed values into `ai_intervention`
(`prediction_artifact_sha256`, `explanation_artifact_sha256` (omit when no
`explanations_dir`), `model_system_version`); its `model_id` must equal
`geneva_ai.model_id`. Every Phase 2 `patient_ids` entry must be in the model
test subset (preflight FAILs a missing AI row). The `geneva_ai` paths are part
of the configuration hash: moving the files needs a new `--version`. Only point
it at the trusted local artifact — the files are pickles.



---

## The six panels

| Panel | What's in it |
|---|---|
| **Admission** | static facts, grouped: demographics, prestroke medication, medical history, stroke context, then treatment / presentation; anything unregistered under *Other* |
| **Vitals** | BP (SBP + DBP), mean BP, HR, RR, SpO₂, temp — one timeline per variable; Geneva hourly min–max as a bar per bucket around the median line |
| **Neuro / Support** | NIHSS (with min–max bars), GCS, FiO₂ |
| **Labs** | every lab measured so far, one row per lab, one column per observation time |
| **Imaging** | perfusion volumes (Tmax, CBF, CBV), imaging / vascular findings, and report entries when a dataset has them |
| **AI** | the precomputed model output; for Geneva the predicted probability and its top contributors |

The simulator only reveals data **up to and including the current timepoint**.

Each panel can be in one of these states, shown as a small italicized note
when relevant:

- **Loading** — data is present and rendered. The default for a healthy panel.
- **Empty** — nothing of this kind up to this timepoint: `No labs data up to
  this timepoint.` The panel never says whether more will come.
- **Partial** — what is shown is internally incomplete (e.g. an hourly max
  without its median). Sparse sampling is normal and never partial.
- **Error** — the panel itself failed to render. The other panels are
  unaffected; the page does not crash.

An empty panel is not a bug: either that patient has no data of that kind, or
none has been recorded yet.

