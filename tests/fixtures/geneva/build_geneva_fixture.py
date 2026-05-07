"""Deterministic synthetic Geneva fixture builder.

Generates a 2-patient synthetic preprocessed-features CSV that exercises
every routing path in :func:`ehr_simulator.ingestion.geneva.load_geneva`
without any real patient data. Inputs are the schema files already in
the fixture directory:

* ``categorical_variable_encoding.csv`` — population-level encoding;
  defines the 19 categorical groups + their one-hot columns.
* ``normalisation_parameters.csv`` — population-level (mean, std) per
  variable.

Both inputs are aggregate / schema-defining and carry no patient data.
The generator emits patient rows whose ``sample_label`` values reference
those schema files but whose ``value`` cells are hand-crafted constants.

Run:
    uv run python tests/fixtures/geneva/build_geneva_fixture.py

Or, in ``--check`` mode (CI gate, mirrors ``gen_data_contract.py --check``):
    uv run python tests/fixtures/geneva/build_geneva_fixture.py --check

``--check`` runs the build steps in-memory without touching disk, then
diffs the in-memory sidecar against the on-disk
``geneva_fixture_expected.json``; exits 0 on match, 1 on drift with a
unified-diff snippet to stderr. The fix is to regenerate without
``--check`` and commit the regenerated JSON (the diff lands visibly in
the PR).

Outputs:
- ``tests/fixtures/geneva/geneva_sample.csv``
- ``tests/fixtures/geneva/geneva_fixture_expected.json``

Re-run on upstream schema changes to ``categorical_variable_encoding.csv``
or ``normalisation_parameters.csv`` (e.g., a new categorical group, a new
variable). The committed CSV is intentionally tiny (~80 rows) — no real
patient data, no PHI, deterministic byte-for-byte.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

FIXTURE_DIR = Path(__file__).resolve().parent
FIXTURE_IDS = ("geneva_fixture_001", "geneva_fixture_002")
DATASET = "geneva"
REGISTRY_SOURCE = "stroke_registry"
NORM_PARAMS_NAME = "normalisation_parameters.csv"
CATEGORICAL_NAME = "categorical_variable_encoding.csv"
SAMPLE_NAME = "geneva_sample.csv"
SIDECAR_NAME = "geneva_fixture_expected.json"

REGISTRY_VARS = ("age", "weight")
EHR_VARS = ("creatinine", "max_heart_rate")
EHR_TIMEPOINTS = (0, 1, 2)
ORPHAN_VAR = "made_up_orphan_var"


def _load_categorical_groups(path: Path) -> list[tuple[str, str, list[str]]]:
    """Return [(group_name, baseline, [other_labels])] for each group.

    Tolerates both Geneva-style ``[baseline]`` list literals and MIMIC-style
    bare baseline scalars; only ``other_categories`` is consistently a list.
    """
    df = pd.read_csv(path)
    groups: list[tuple[str, str, list[str]]] = []
    for _, row in df.iterrows():
        raw_baseline = str(row["baseline_value"])
        try:
            parsed = ast.literal_eval(raw_baseline)
        except (SyntaxError, ValueError):
            parsed = raw_baseline
        baseline = str(parsed[0]) if isinstance(parsed, list) else str(parsed)
        others = [str(x) for x in ast.literal_eval(str(row["other_categories"]))]
        groups.append((str(row["sample_label"]), baseline, others))
    return groups


def _one_hot_column_name(group_name: str, label: str) -> str:
    return f"{group_name}_{label}".lower().replace(" ", "_")


def _build_synthetic_rows() -> pd.DataFrame:
    cat_groups = _load_categorical_groups(FIXTURE_DIR / CATEGORICAL_NAME)
    rows: list[dict[str, object]] = []

    for pid_idx, pid in enumerate(FIXTURE_IDS):
        # Categorical one-hots at t=0. Patient 0 keeps every group at the
        # baseline (all one-hots = 0.0). Patient 1 sets the first non-
        # baseline one-hot per group to 1.0 (so the decode picks that
        # label).
        for group_name, _baseline, other_labels in cat_groups:
            for col_idx, label in enumerate(other_labels):
                col = _one_hot_column_name(group_name, label)
                val = 0.0 if pid_idx == 0 else (1.0 if col_idx == 0 else 0.0)
                rows.append(
                    {
                        "relative_sample_date_hourly_cat": 0,
                        "case_admission_id": pid,
                        "sample_label": col,
                        "source": REGISTRY_SOURCE,
                        "value": val,
                    }
                )

        # Continuous registry vars at t=0 (z-scored constants).
        for var in REGISTRY_VARS:
            z = 0.5 if pid_idx == 0 else -0.3
            rows.append(
                {
                    "relative_sample_date_hourly_cat": 0,
                    "case_admission_id": pid,
                    "sample_label": var,
                    "source": REGISTRY_SOURCE,
                    "value": z,
                }
            )

        # Orphan registry var (not in norm_params, not a categorical
        # one-hot) — exercises the orphan-issue emission.
        rows.append(
            {
                "relative_sample_date_hourly_cat": 0,
                "case_admission_id": pid,
                "sample_label": ORPHAN_VAR,
                "source": REGISTRY_SOURCE,
                "value": 0.1,
            }
        )

        # EHR rows across multiple hour buckets.
        for var in EHR_VARS:
            for t in EHR_TIMEPOINTS:
                rows.append(
                    {
                        "relative_sample_date_hourly_cat": t,
                        "case_admission_id": pid,
                        "sample_label": var,
                        "source": "EHR",
                        "value": 0.1 * (t + 1) + 0.01 * pid_idx,
                    }
                )

        # Imputed source rows (verify _drop_imputed). Should be filtered
        # before validation.
        for var in EHR_VARS:
            rows.append(
                {
                    "relative_sample_date_hourly_cat": 1,
                    "case_admission_id": pid,
                    "sample_label": var,
                    "source": "EHR_locf_imputed",
                    "value": 999.9,
                }
            )

    return pd.DataFrame(
        rows,
        columns=[
            "relative_sample_date_hourly_cat",
            "case_admission_id",
            "sample_label",
            "source",
            "value",
        ],
    )


def _build_sidecar(fixture_dir: Path) -> dict[str, dict[str, str]]:
    from ehr_simulator.ingestion.geneva import load_geneva

    dataset = load_geneva(
        fixture_dir / SAMPLE_NAME,
        fixture_dir,
        strict=False,
    )
    expected: dict[str, dict[str, str]] = {}
    for pid, group in dataset.admission.groupby("patient_id", sort=True):
        expected[str(pid)] = {
            str(row.field): str(row.value) for row in group.itertuples(index=False)
        }
    return expected


def _serialize_sidecar(expected: dict[str, dict[str, str]]) -> str:
    return json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write_fixture(fixture_dir: Path) -> None:
    df = _build_synthetic_rows()
    df.to_csv(fixture_dir / SAMPLE_NAME, index=False)
    expected = _build_sidecar(fixture_dir)
    (fixture_dir / SIDECAR_NAME).write_text(_serialize_sidecar(expected), encoding="utf-8")
    print(f"wrote {len(df)} rows → {fixture_dir / SAMPLE_NAME}", file=sys.stderr)
    print(f"wrote sidecar → {fixture_dir / SIDECAR_NAME}", file=sys.stderr)


def _check() -> int:
    on_disk = (FIXTURE_DIR / SIDECAR_NAME).read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        # Copy the schema files into the tmp dir so load_geneva finds them.
        for fname in (NORM_PARAMS_NAME, CATEGORICAL_NAME):
            (tmp_dir / fname).write_bytes((FIXTURE_DIR / fname).read_bytes())
        df = _build_synthetic_rows()
        df.to_csv(tmp_dir / SAMPLE_NAME, index=False)
        expected = _build_sidecar(tmp_dir)
        rebuilt = _serialize_sidecar(expected)

    if rebuilt == on_disk:
        return 0

    diff = difflib.unified_diff(
        on_disk.splitlines(keepends=True),
        rebuilt.splitlines(keepends=True),
        fromfile=f"on-disk: tests/fixtures/geneva/{SIDECAR_NAME}",
        tofile="rebuilt (in-memory)",
        n=3,
    )
    sys.stderr.writelines(diff)
    sys.stderr.write(
        "\nSidecar drift detected. Regenerate with:\n"
        "    uv run python tests/fixtures/geneva/build_geneva_fixture.py\n"
        f"and commit the regenerated tests/fixtures/geneva/{SIDECAR_NAME}.\n"
    )
    return 1


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="rebuild the sidecar in memory and diff against the on-disk JSON; "
        "exit non-zero on drift",
    )
    args = parser.parse_args(argv[1:])

    if args.check:
        return _check()

    _write_fixture(FIXTURE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
