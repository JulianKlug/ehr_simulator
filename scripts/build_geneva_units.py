"""One-shot bootstrap: upstream OPSUM xlsx → committed `geneva_units.json`.

Reads ``possible_ranges_for_variables.xlsx`` from the OPSUM repo
(https://github.com/JulianKlug/OPSUM/blob/main/preprocessing/geneva_stroke_unit_preprocessing/possible_ranges_for_variables.xlsx),
extracts the ``variable_label`` and ``units`` columns, applies a manual
expansion table for vital-sign summary statistics (Geneva CSV emits
``max_heart_rate`` / ``min_heart_rate`` / ``median_heart_rate`` from a single
``pulse`` row in the xlsx), and writes
``src/ehr_simulator/ingestion/data/geneva_units.json``.

The xlsx schema (locked 2026-05-06): 28 rows, 6 columns
(``Variable with original units``, ``variable_label``, ``units``, ``Min``,
``Max``, ``Remarks``). Of these, ~12 ``variable_label`` values match a
Geneva CSV ``sample_label`` exactly; the remaining ~6 are summary-stat
roots that the expansion table fans out into ~18 prefix-derived keys.

Usage:
    uv run python scripts/build_geneva_units.py <xlsx_path>

Idempotent given the same xlsx input. Re-run whenever upstream xlsx
refreshes, then commit the regenerated JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "src" / "ehr_simulator" / "ingestion" / "data" / "geneva_units.json"

# Geneva CSV emits per-hour-bucket summary statistics for vital signs from a
# single root variable upstream. The xlsx has one row per root; the Geneva CSV
# has three rows per root (``max_*``, ``min_*``, ``median_*``). This table
# fans out the unit assignment so every Geneva sample_label that has a known
# unit gets one. Verified by hand against the 103 Geneva sample_labels on
# 2026-05-06.
# Two upstream xlsx rows have ``units = NaN`` even though the
# ``Variable with original units`` column makes the unit unambiguous.
# Apply manual overrides; documented here so the source of truth stays in
# the upstream xlsx and these are limited to clear data-entry gaps.
_UNITS_OVERRIDE: dict[str, str] = {
    "mean": "mmHg",
    "temperature": "degC",
}

_VITAL_SUMMARY_EXPANSION: dict[str, tuple[str, ...]] = {
    "pulse": ("max_heart_rate", "min_heart_rate", "median_heart_rate"),
    "mean": (
        "max_mean_blood_pressure",
        "min_mean_blood_pressure",
        "median_mean_blood_pressure",
    ),
    "dia": (
        "max_diastolic_blood_pressure",
        "min_diastolic_blood_pressure",
        "median_diastolic_blood_pressure",
    ),
    "sys": (
        "max_systolic_blood_pressure",
        "min_systolic_blood_pressure",
        "median_systolic_blood_pressure",
    ),
    "spo2": (
        "max_oxygen_saturation",
        "min_oxygen_saturation",
        "median_oxygen_saturation",
    ),
    "fr": (
        "max_respiratory_rate",
        "min_respiratory_rate",
        "median_respiratory_rate",
    ),
}


def build_units(xlsx_path: Path) -> dict[str, str]:
    df = pd.read_excel(xlsx_path, engine="openpyxl")
    missing = {"variable_label", "units"} - set(df.columns)
    if missing:
        raise SystemExit(f"xlsx schema drift: missing columns {missing}. Found: {list(df.columns)}")

    units: dict[str, str] = {}
    for _, row in df.iterrows():
        label = row["variable_label"]
        unit = row["units"]
        if pd.isna(label):
            continue
        label = str(label).strip()
        if not label:
            continue
        if pd.isna(unit) and label in _UNITS_OVERRIDE:
            unit = _UNITS_OVERRIDE[label]
        elif pd.isna(unit):
            continue
        else:
            unit = str(unit).strip()
            if not unit:
                continue
        units[label] = unit
        if label in _VITAL_SUMMARY_EXPANSION:
            for derived in _VITAL_SUMMARY_EXPANSION[label]:
                units[derived] = unit

    return dict(sorted(units.items()))


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <xlsx_path>", file=sys.stderr)
        return 2
    xlsx_path = Path(argv[1]).expanduser().resolve()
    if not xlsx_path.is_file():
        print(f"xlsx not found: {xlsx_path}", file=sys.stderr)
        return 1

    units = build_units(xlsx_path)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(units, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")

    print(f"wrote {len(units)} entries → {OUTPUT_PATH}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
