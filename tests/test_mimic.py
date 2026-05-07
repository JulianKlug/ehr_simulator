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
import math
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.ingestion import CanonicalShape, validate
from ehr_simulator.ingestion._shared import (
    CategoricalGroup,
    _decode_categorical,
    _drop_imputed,
    _load_categorical_encoding,
    _load_normalisation_params,
    _path_traversal_guard,
)
from ehr_simulator.ingestion.exceptions import AdapterError
from ehr_simulator.ingestion.mimic import MimicDataset, load_mimic

# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_mimic_routes_eight_known_sources_to_two_non_imputed() -> None:
    """Lock the MIMIC source vocabulary against schema drift.

    The 8 observed source values were verified by direct inspection of the
    real CSV on 2026-05-06. Only ``EHR`` and ``notes`` survive the
    ``_drop_imputed`` substring filter.
    """
    sources = [
        "EHR",
        "EHR_locf_imputed",
        "EHR_pop_imputed",
        "EHR_pop_imputed_locf_imputed",
        "notes",
        "notes_locf_imputed",
        "missing_pop_imputed",
        "missing_pop_imputed_locf_imputed",
    ]
    frame = pd.DataFrame({"source": sources, "value": list(range(len(sources)))})
    out = _drop_imputed(frame)
    assert sorted(out["source"].tolist()) == ["EHR", "notes"]


def test_hour_bucket_to_minutes_conversion_mimic(mimic_fixture_dir: Path) -> None:
    """``relative_sample_date_hourly_cat`` × 60 → ``t_minutes`` post-_drop_imputed.

    Loads the synthetic fixture and asserts every ``t_minutes`` is a
    multiple of 60 within the [0, 71*60] window — locks the conversion
    living between ``_drop_imputed`` and pandera validation.
    """
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    bucket_minutes = sorted(dataset.scalar_ts["t_minutes"].unique().tolist())
    assert bucket_minutes[0] == 0.0
    assert all(t % 60.0 == 0.0 for t in bucket_minutes)
    assert max(bucket_minutes) <= 71 * 60.0


def test_load_categorical_encoding_covers_all_19_groups_mimic(mimic_fixture_dir: Path) -> None:
    """The 19-group naming-convention bridge re-runs against MIMIC's encoding."""
    sample_labels = set(
        pd.read_csv(mimic_fixture_dir / "mimic_sample.csv", dtype={"sample_label": str})[
            "sample_label"
        ].unique()
    )
    groups = _load_categorical_encoding(
        mimic_fixture_dir / "categorical_variable_encoding.csv",
        sample_labels,
        dataset="mimic",
    )
    assert len(groups) == 19
    for group in groups.values():
        for col in group.one_hot_columns:
            assert col in sample_labels

    stripped = sample_labels - {"sex_male"}
    with pytest.raises(AdapterError) as exc:
        _load_categorical_encoding(
            mimic_fixture_dir / "categorical_variable_encoding.csv",
            stripped,
            dataset="mimic",
        )
    assert "sex_male" in str(exc.value)


def test_load_normalisation_params_raises_on_missing_column_mimic(tmp_path: Path) -> None:
    bad = tmp_path / "norm.csv"
    pd.DataFrame({"variable": ["age"], "original_mean": [1.0]}).to_csv(bad, index=False)
    with pytest.raises(AdapterError) as exc:
        _load_normalisation_params(bad, dataset="mimic")
    assert "original_std" in str(exc.value)
    assert "mimic" in str(exc.value)


def test_decode_categorical_mimic_categorical_iat_three_class() -> None:
    """MIMIC's three-class IAT group: baseline 271-540min, others no_IAT/<270min/>540min."""
    group = CategoricalGroup(
        group_name="categorical_IAT",
        baseline="271-540min",
        other_labels=("no_IAT", "<270min", ">540min"),
        one_hot_columns=(
            "categorical_iat_no_iat",
            "categorical_iat_<270min",
            "categorical_iat_>540min",
        ),
    )
    rows = pd.DataFrame(
        [
            {"sample_label": "categorical_iat_no_iat", "value": 0.0},
            {"sample_label": "categorical_iat_<270min", "value": 0.0},
            {"sample_label": "categorical_iat_>540min", "value": 0.9},
        ]
    )
    decoded, issue = _decode_categorical(
        rows, group, strict=True, patient_id="mimic_fixture_001", dataset="mimic"
    )
    assert decoded == ">540min"
    assert issue is None


def test_path_traversal_guard_rejects_outside_root_mimic(tmp_path: Path) -> None:
    inside = tmp_path / "sub" / "file.csv"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.touch()

    assert _path_traversal_guard(inside, tmp_path, dataset="mimic").resolve() == inside.resolve()
    assert _path_traversal_guard(inside, None, dataset="mimic").resolve() == inside.resolve()

    other = Path("/tmp") / "ehr_traversal_test_mimic_outside"
    with pytest.raises(AdapterError) as exc:
        _path_traversal_guard(other, tmp_path, dataset="mimic")
    assert exc.value.issues[0].dataset == "mimic"
    assert "path traversal" in exc.value.issues[0].reason


def test_inverse_normalize_passthrough_emits_issue_mimic(
    mimic_fixture_dir: Path, tmp_path: Path
) -> None:
    """An EHR row with sample_label not in norm_params keeps its z-score and emits an issue."""
    csv = pd.read_csv(mimic_fixture_dir / "mimic_sample.csv", dtype=str)
    fake_label = "made_up_lab_xyz"
    extra = pd.DataFrame(
        [
            {
                "relative_sample_date_hourly_cat": "0",
                "case_admission_id": "mimic_fixture_001",
                "sample_label": fake_label,
                "source": "EHR",
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
    row = dataset.scalar_ts[
        (dataset.scalar_ts["patient_id"] == "mimic_fixture_001")
        & (dataset.scalar_ts["variable"] == fake_label)
    ]
    assert len(row) == 1
    assert math.isclose(float(row["value"].iloc[0]), 0.5, abs_tol=1e-9)
    assert any(
        i.dataset == "mimic"
        and i.reason == f"variable {fake_label} missing from normalisation_parameters"
        for i in dataset.issues
    )


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


def test_load_mimic_scalar_ts_unit_is_none_for_all_rows(mimic_fixture_dir: Path) -> None:
    """Lock the no-units contract.

    MIMIC has no upstream xlsx units source. Every SCALAR_TS row ships
    ``unit=None``. If a future session adds an auto-load-units path for
    MIMIC it must update this test deliberately, not silently flip.
    """
    dataset = load_mimic(
        mimic_fixture_dir / "mimic_sample.csv",
        mimic_fixture_dir,
        strict=False,
    )
    assert dataset.scalar_ts["unit"].isna().all()
