"""Deterministic Geneva fixture builder.

Slices the real Geneva CSV down to 2 patients, replaces the case-admission
ids with anonymous fixture ids, and copies the params CSVs verbatim. Run
once during S3 implementation; rerun only on upstream schema changes.

The two patients are picked deterministically: the patient with the most
``EHR`` rows + the patient with the most ``stroke_registry`` rows (sorted
by id as a tiebreaker; second-most as a fallback if both metrics resolve to
the same id). Values are NOT altered — they are already z-scored against a
population so they carry no PHI by construction.

Run:
    uv run python tests/fixtures/build_geneva_fixture.py [csv_path] [params_dir]

Both arguments default to the constants at the top of this file. Outputs:
- ``tests/fixtures/geneva_sample.csv`` (anonymised 2-patient slice)
- ``tests/fixtures/normalisation_parameters.csv`` (verbatim copy)
- ``tests/fixtures/categorical_variable_encoding.csv`` (verbatim copy)
- ``tests/fixtures/geneva_fixture_expected.json`` (sidecar — output of
  ``load_geneva`` over the just-written fixture, for exact-match tests)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pandas as pd

DEFAULT_CSV = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "gsu_Extraction_20220815_prepro_30012026_154047/"
    "preprocessed_features_30012026_154047.csv"
)
DEFAULT_PARAMS_DIR = Path(
    "/mnt/data1/klug/datasets/opsum/short_term_outcomes/with_imaging/"
    "gsu_Extraction_20220815_prepro_30012026_154047/logs_30012026_154047"
)
FIXTURE_DIR = Path(__file__).resolve().parent
FIXTURE_IDS = ("geneva_fixture_001", "geneva_fixture_002")


def _pick_patients(csv_path: Path) -> tuple[str, str]:
    df = pd.read_csv(
        csv_path, usecols=["case_admission_id", "source"], dtype={"case_admission_id": str}
    )
    df = df[~df["source"].astype(str).str.contains("imputed", na=False)]
    ehr = df[df["source"] == "EHR"].groupby("case_admission_id").size()
    reg = df[df["source"] == "stroke_registry"].groupby("case_admission_id").size()

    ehr_sorted = ehr.sort_values(ascending=False, kind="stable")
    ehr_sorted = ehr_sorted.sort_index().sort_values(ascending=False, kind="stable")
    reg_sorted = reg.sort_values(ascending=False, kind="stable")
    reg_sorted = reg_sorted.sort_index().sort_values(ascending=False, kind="stable")

    pid_a = str(ehr_sorted.index[0])
    pid_b = str(reg_sorted.index[0])
    if pid_b == pid_a and len(reg_sorted) > 1:
        pid_b = str(reg_sorted.index[1])
    return pid_a, pid_b


def _slice_and_anonymise(
    csv_path: Path,
    pids: tuple[str, str],
    out_path: Path,
) -> None:
    df = pd.read_csv(csv_path, dtype={"case_admission_id": str, "sample_label": str, "source": str})
    keep = df["case_admission_id"].isin(pids)
    df = df[keep].copy()
    rename_map = {pids[0]: FIXTURE_IDS[0], pids[1]: FIXTURE_IDS[1]}
    df["case_admission_id"] = df["case_admission_id"].map(rename_map)
    df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed")])
    df.to_csv(out_path, index=False)


def _write_sidecar(fixture_dir: Path) -> None:
    from ehr_simulator.ingestion import load_geneva

    dataset = load_geneva(
        fixture_dir / "geneva_sample.csv",
        fixture_dir,
        strict=False,
    )
    expected: dict[str, dict[str, str]] = {}
    for pid, group in dataset.admission.groupby("patient_id", sort=True):
        expected[str(pid)] = {
            str(row.field): str(row.value) for row in group.itertuples(index=False)
        }
    with (fixture_dir / "geneva_fixture_expected.json").open("w", encoding="utf-8") as f:
        json.dump(expected, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")


def main(argv: list[str]) -> int:
    csv_path = Path(argv[1]) if len(argv) > 1 else DEFAULT_CSV
    params_dir = Path(argv[2]) if len(argv) > 2 else DEFAULT_PARAMS_DIR

    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1
    if not params_dir.is_dir():
        print(f"params dir not found: {params_dir}", file=sys.stderr)
        return 1

    pid_a, pid_b = _pick_patients(csv_path)
    print(
        f"picked patients: {pid_a} → {FIXTURE_IDS[0]}, {pid_b} → {FIXTURE_IDS[1]}",
        file=sys.stderr,
    )

    _slice_and_anonymise(csv_path, (pid_a, pid_b), FIXTURE_DIR / "geneva_sample.csv")
    shutil.copyfile(
        params_dir / "normalisation_parameters.csv",
        FIXTURE_DIR / "normalisation_parameters.csv",
    )
    shutil.copyfile(
        params_dir / "categorical_variable_encoding.csv",
        FIXTURE_DIR / "categorical_variable_encoding.csv",
    )
    _write_sidecar(FIXTURE_DIR)
    print(f"wrote fixture files → {FIXTURE_DIR}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
