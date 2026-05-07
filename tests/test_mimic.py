"""Unit + integration + E2E + regression tests for the MIMIC adapter.

Inline DataFrames cover the helper-level contracts; ``mimic_fixture_dir``
supplies a 2-patient slice of the real CSV for the integration / E2E
tests. The 1.83M-row real-CSV smoke test lives in ``test_mimic_real.py``
behind the ``real_data`` marker.

Anchor assertions in test #9 sub-(b) were locked at S4 implementation
time by reading raw upstream rows for the two anonymised fixture patients
(see :func:`tests/fixtures/mimic/build_mimic_fixture.py`). They are
independent of ``load_mimic`` so a regression in the adapter that also
drifts the sidecar still gets caught.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.ingestion import CanonicalShape, validate
from ehr_simulator.ingestion.exceptions import AdapterError
from ehr_simulator.ingestion.mimic import MimicDataset, load_mimic

# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_load_mimic_routes_sources_correctly(mimic_fixture_dir: Path) -> None:
    """SCALAR_TS sees only EHR rows; ADMISSION uses decoded category names."""
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    assert set(dataset.scalar_ts["source"].unique()) == {"EHR"}
    fields = set(dataset.admission["field"].unique())
    assert "Sex" in fields
    assert "Referral" in fields
    assert "age" in fields
    assert "sex_male" not in fields
    for frame in (dataset.scalar_ts, dataset.admission, dataset.imaging, dataset.ai_output):
        if "source" in frame.columns:
            assert not frame["source"].astype(str).str.contains("imputed").any()


def test_load_mimic_admission_matches_expected_sidecar_plus_anchors(
    mimic_fixture_dir: Path,
) -> None:
    """Sidecar exact-match + 5 hand-curated anchors independent of `load_mimic`."""
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    expected = json.loads(
        (mimic_fixture_dir / "mimic_fixture_expected.json").read_text(encoding="utf-8")
    )
    actual: dict[str, dict[str, str]] = {}
    for pid, group in dataset.admission.groupby("patient_id"):
        actual[str(pid)] = {str(r.field): str(r.value) for r in group.itertuples(index=False)}
    assert actual == expected

    # Anchor assertions — locked from the synthetic generator in
    # ``build_mimic_fixture.py``. Patient 1 keeps every categorical group at
    # the encoding's baseline; Patient 2 picks the first non-baseline label
    # per group. Anchors are deliberately independent of the sidecar JSON
    # so a regression that drifts the sidecar still gets caught.
    anchors = [
        ("mimic_fixture_001", "Sex", "Female"),
        ("mimic_fixture_001", "MedHist Hypertension", "no"),
        ("mimic_fixture_001", "Referral", "Emergency service (144)"),
        ("mimic_fixture_002", "Sex", "Male"),
        ("mimic_fixture_002", "categorical_IVT", "91-270min"),
        ("mimic_fixture_002", "Prestroke disability (Rankin)", "2.0"),
    ]
    admission = dataset.admission
    for patient_id, field, value in anchors:
        match = admission[(admission["patient_id"] == patient_id) & (admission["field"] == field)]
        assert len(match) == 1, f"expected one row for ({patient_id}, {field})"
        assert match["value"].iloc[0] == value, (
            f"anchor mismatch: ({patient_id}, {field}) "
            f"expected {value!r}, got {match['value'].iloc[0]!r}"
        )


def test_load_mimic_strict_vs_lenient(mimic_fixture_dir: Path, tmp_path: Path) -> None:
    """Strict mode passes on clean fixture; lenient mode surfaces issues on a corrupted variant."""
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=True,
    )
    assert isinstance(dataset, MimicDataset)

    csv = pd.read_csv(mimic_fixture_dir / "mimic_sample.csv", dtype=str)
    surviving = csv.index[
        ~csv["source"].astype(str).str.contains("imputed", na=False)
        & (csv["source"].astype(str) == "EHR")
    ]
    assert len(surviving) > 0, "fixture must contain at least one non-imputed EHR row"
    csv.loc[surviving[0], "value"] = "not a number"
    out_csv = tmp_path / "mimic_sample.csv"
    csv.to_csv(out_csv, index=False)
    for fname in (
        "reference_population_normalisation_parameters.csv",
        "categorical_variable_encoding.csv",
    ):
        (tmp_path / fname).write_bytes((mimic_fixture_dir / fname).read_bytes())

    with pytest.raises(AdapterError):
        load_mimic(out_csv, tmp_path, strict=True)

    lenient = load_mimic(out_csv, tmp_path, strict=False)
    assert lenient.issues, "expected lenient mode to surface at least one issue"


def test_load_mimic_imaging_and_ai_output_empty_but_conforming(
    mimic_fixture_dir: Path,
) -> None:
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    assert len(dataset.imaging) == 0
    assert len(dataset.ai_output) == 0
    validate(dataset.imaging, CanonicalShape.IMAGING, strict=True, dataset="mimic")
    validate(dataset.ai_output, CanonicalShape.AI_OUTPUT, strict=True, dataset="mimic")


def test_load_mimic_orphan_registry_variable_emits_issue(
    mimic_fixture_dir: Path, tmp_path: Path
) -> None:
    """Inject one notes row at t=0 with an unknown sample_label; assert orphan issue surfaces."""
    csv = pd.read_csv(mimic_fixture_dir / "mimic_sample.csv", dtype=str)
    fake_label = "made_up_registry_var_xyz"
    extra = pd.DataFrame(
        [
            {
                "relative_sample_date_hourly_cat": "0",
                "case_admission_id": "mimic_fixture_001",
                "sample_label": fake_label,
                "source": "notes",
                "value": "0.5",
            }
        ]
    )
    augmented = pd.concat([csv, extra], ignore_index=True)
    out_csv = tmp_path / "mimic_sample.csv"
    augmented.to_csv(out_csv, index=False)
    for fname in (
        "reference_population_normalisation_parameters.csv",
        "categorical_variable_encoding.csv",
    ):
        (tmp_path / fname).write_bytes((mimic_fixture_dir / fname).read_bytes())

    dataset = load_mimic(out_csv, tmp_path, strict=False)
    assert not (
        (dataset.admission["patient_id"] == "mimic_fixture_001")
        & (dataset.admission["field"] == fake_label)
    ).any()
    assert any(
        i.dataset == "mimic" and i.reason == f"orphan registry variable: {fake_label}"
        for i in dataset.issues
    )


# ---------------------------------------------------------------------------
# E2E
# ---------------------------------------------------------------------------


def test_load_mimic_full_fixture_roundtrip(mimic_fixture_dir: Path) -> None:
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    assert set(dataset.scalar_ts["patient_id"].unique()) == {
        "mimic_fixture_001",
        "mimic_fixture_002",
    }
    assert set(dataset.admission["patient_id"].unique()) == {
        "mimic_fixture_001",
        "mimic_fixture_002",
    }
    assert dataset.scalar_ts["t_minutes"].between(0.0, 71 * 60.0).all()
    assert dataset.scalar_ts["unit"].isna().all()
    validate(dataset.scalar_ts, CanonicalShape.SCALAR_TS, strict=True, dataset="mimic")
    validate(dataset.admission, CanonicalShape.ADMISSION, strict=True, dataset="mimic")
    counts = dataset.admission.groupby("patient_id").size()
    assert counts.min() >= 20


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def test_load_mimic_raises_on_missing_required_columns(
    mimic_fixture_dir: Path, tmp_path: Path
) -> None:
    csv = pd.read_csv(mimic_fixture_dir / "mimic_sample.csv", dtype=str)
    csv = csv.drop(columns=["source"])
    out_csv = tmp_path / "mimic_sample.csv"
    csv.to_csv(out_csv, index=False)

    with pytest.raises(AdapterError) as exc:
        load_mimic(out_csv, mimic_fixture_dir, strict=True)
    assert "source" in str(exc.value)
    assert "mimic" in str(exc.value)
