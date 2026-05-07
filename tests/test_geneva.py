"""Unit + integration + E2E + regression tests for the Geneva adapter.

Inline DataFrames cover the helper-level contracts (#1–#7e). The
``geneva_fixture_dir`` fixture supplies a 2-patient slice of the real CSV
for the integration / E2E tests (#8–#12, #14). The 1.5 GB real-CSV smoke
test lives in ``test_geneva_real.py`` behind the ``real_data`` marker.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator.ingestion import (
    AdapterError,
    CanonicalShape,
    GenevaDataset,
    load_geneva,
    validate,
)
from ehr_simulator.ingestion import geneva as geneva_module
from ehr_simulator.ingestion.geneva import (
    CategoricalGroup,
    _decode_categorical,
    _drop_imputed,
    _inverse_normalize,
    _load_categorical_encoding,
    _load_normalisation_params,
    _load_units,
    _path_traversal_guard,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEX_GROUP = CategoricalGroup(
    group_name="Sex",
    baseline="Female",
    other_labels=("Male",),
    one_hot_columns=("sex_male",),
)

_IAT_GROUP = CategoricalGroup(
    group_name="categorical_IAT",
    baseline="271-540min",
    other_labels=("no_IAT", ">540min", "<270min"),
    one_hot_columns=(
        "categorical_iat_no_iat",
        "categorical_iat_>540min",
        "categorical_iat_<270min",
    ),
)


def _row(label: str, value: float) -> dict[str, object]:
    return {"sample_label": label, "value": value}


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_drop_imputed_drops_six_of_eight_known_sources() -> None:
    sources = [
        "EHR",
        "EHR_locf_imputed",
        "EHR_pop_imputed",
        "EHR_pop_imputed_locf_imputed",
        "stroke_registry",
        "stroke_registry_locf_imputed",
        "stroke_registry_pop_imputed",
        "stroke_registry_pop_imputed_locf_imputed",
    ]
    frame = pd.DataFrame({"source": sources, "value": list(range(len(sources)))})
    out = _drop_imputed(frame)
    assert sorted(out["source"].tolist()) == ["EHR", "stroke_registry"]


def test_inverse_normalize_round_trip() -> None:
    cases = [(73.6, 73.6, 14.5), (1.09, 1.09, 0.27), (0.0, 22.1, 4.3)]
    for x, mean, std in cases:
        z = (x - mean) / std
        assert math.isclose(_inverse_normalize(z, mean, std), x, abs_tol=1e-9)
    assert math.isnan(_inverse_normalize(float("nan"), 0.0, 1.0))


def test_decode_categorical_threshold_below_returns_baseline() -> None:
    rows = pd.DataFrame([_row("sex_male", 0.4)])
    decoded, issue = _decode_categorical(
        rows, _SEX_GROUP, strict=True, patient_id="p1", dataset="geneva"
    )
    assert decoded == "Female"
    assert issue is None


def test_decode_categorical_threshold_above_returns_match() -> None:
    rows_sex = pd.DataFrame([_row("sex_male", 0.7)])
    decoded, issue = _decode_categorical(
        rows_sex, _SEX_GROUP, strict=True, patient_id="p1", dataset="geneva"
    )
    assert decoded == "Male"
    assert issue is None

    rows_iat = pd.DataFrame(
        [
            _row("categorical_iat_no_iat", 0.0),
            _row("categorical_iat_>540min", 0.9),
            _row("categorical_iat_<270min", 0.1),
        ]
    )
    decoded, issue = _decode_categorical(
        rows_iat, _IAT_GROUP, strict=True, patient_id="p1", dataset="geneva"
    )
    assert decoded == ">540min"
    assert issue is None


def test_load_units_covers_expected_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    _load_units.cache_clear()
    units = _load_units()
    geneva_labels = {
        "FIO2",
        "creatinine",
        "max_heart_rate",
        "median_systolic_blood_pressure",
        "weight",
    }
    assert isinstance(units, dict)
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in units.items())
    assert geneva_labels.issubset(units.keys())
    assert sum(1 for k in units if k in _ALL_GENEVA_SAMPLE_LABELS) >= 30
    again = _load_units()
    assert again is units


_ALL_GENEVA_SAMPLE_LABELS: set[str] = {
    "ALAT",
    "ASAT",
    "FIO2",
    "Glasgow Coma Scale",
    "INR",
    "LDL cholesterol calcule",
    "PTT",
    "age",
    "anticoagulants_yes",
    "antihypert._drugs_pre-stroke_yes",
    "antiplatelet_drugs_yes",
    "bilirubine totale",
    "calcium corrige",
    "categorical_iat_<270min",
    "categorical_iat_>540min",
    "categorical_iat_no_iat",
    "categorical_ivt_91-270min",
    "categorical_ivt_<90min",
    "categorical_ivt_>540min",
    "categorical_ivt_no_ivt",
    "categorical_onset_to_admission_time_541-1440min",
    "categorical_onset_to_admission_time_<270min",
    "categorical_onset_to_admission_time_>1440min",
    "categorical_onset_to_admission_time_intra_hospital",
    "categorical_onset_to_admission_time_onset_unknown",
    "cbf_lt_20",
    "cbf_lt_30",
    "cbf_lt_34",
    "cbf_lt_38",
    "cbv_lt_34",
    "cbv_lt_38",
    "cbv_lt_42",
    "chlore",
    "cholesterol HDL",
    "cholesterol total",
    "creatinine",
    "erythrocytes",
    "fibrinogene",
    "glucose",
    "hematocrite",
    "hemoglobine",
    "hemoglobine glyquee",
    "hypoperfusion_with_mismatch",
    "hypoperfusion_without_mismatch",
    "lactate",
    "leucocytes",
    "lipid_lowering_drugs_pre-stroke_yes",
    "max_NIHSS",
    "max_diastolic_blood_pressure",
    "max_heart_rate",
    "max_mean_blood_pressure",
    "max_oxygen_saturation",
    "max_respiratory_rate",
    "max_systolic_blood_pressure",
    "medhist_atrial_fibr._yes",
    "medhist_cerebrovascular_event_true",
    "medhist_chd_yes",
    "medhist_diabetes_yes",
    "medhist_hyperlipidemia_yes",
    "medhist_hypertension_yes",
    "medhist_pad_yes",
    "medhist_smoking_yes",
    "median_NIHSS",
    "median_diastolic_blood_pressure",
    "median_heart_rate",
    "median_mean_blood_pressure",
    "median_oxygen_saturation",
    "median_respiratory_rate",
    "median_systolic_blood_pressure",
    "min_NIHSS",
    "min_diastolic_blood_pressure",
    "min_heart_rate",
    "min_mean_blood_pressure",
    "min_oxygen_saturation",
    "min_respiratory_rate",
    "min_systolic_blood_pressure",
    "neutrophiles-nb abs",
    "phosphates",
    "potassium",
    "prestroke_disability_(rankin)_1.0",
    "prestroke_disability_(rankin)_2.0",
    "prestroke_disability_(rankin)_3.0",
    "prestroke_disability_(rankin)_4.0",
    "prestroke_disability_(rankin)_5.0",
    "proBNP",
    "proteine C-reactive",
    "referral_in-hospital_event",
    "referral_other_hospital",
    "referral_self_referral_or_gp",
    "sex_male",
    "sodium",
    "temperature",
    "thrombocytes",
    "tmax_gt_10",
    "tmax_gt_4",
    "tmax_gt_6",
    "tmax_gt_8",
    "triglycerides",
    "uree",
    "vascular_occlusion",
    "vascular_stenosis_over_50p",
    "wake_up_stroke_true",
    "weight",
}


def test_load_categorical_encoding_covers_all_19_groups(geneva_fixture_dir: Path) -> None:
    sample_labels = set(
        pd.read_csv(geneva_fixture_dir / "geneva_sample.csv", dtype={"sample_label": str})[
            "sample_label"
        ].unique()
    )
    groups = _load_categorical_encoding(
        geneva_fixture_dir / "categorical_variable_encoding.csv",
        sample_labels,
        dataset="geneva",
    )
    assert len(groups) == 19
    for group in groups.values():
        for col in group.one_hot_columns:
            assert col in sample_labels

    stripped = sample_labels - {"sex_male"}
    with pytest.raises(AdapterError) as exc:
        _load_categorical_encoding(
            geneva_fixture_dir / "categorical_variable_encoding.csv",
            stripped,
            dataset="geneva",
        )
    assert "sex_male" in str(exc.value)


def test_decode_categorical_edge_cases() -> None:
    rows_eq = pd.DataFrame([_row("sex_male", 0.5)])
    decoded, issue = _decode_categorical(
        rows_eq, _SEX_GROUP, strict=True, patient_id="p1", dataset="geneva"
    )
    assert decoded == "Male"
    assert issue is None

    rows_ambig = pd.DataFrame(
        [
            _row("categorical_iat_no_iat", 0.7),
            _row("categorical_iat_>540min", 0.8),
            _row("categorical_iat_<270min", 0.0),
        ]
    )
    with pytest.raises(AdapterError) as exc_info:
        _decode_categorical(
            rows_ambig, _IAT_GROUP, strict=True, patient_id="p_amb", dataset="geneva"
        )
    assert exc_info.value.issues[0].patient_id == "p_amb"
    assert "ambiguous" in exc_info.value.issues[0].reason

    empty = pd.DataFrame(columns=["sample_label", "value"])
    with pytest.raises(AdapterError):
        _decode_categorical(empty, _SEX_GROUP, strict=True, patient_id="p1", dataset="geneva")
    decoded, issue = _decode_categorical(
        empty, _SEX_GROUP, strict=False, patient_id="p1", dataset="geneva"
    )
    assert decoded == "Female"
    assert issue is not None
    assert "empty" in issue.reason

    decoded, issue = _decode_categorical(
        rows_ambig, _IAT_GROUP, strict=False, patient_id="p_amb", dataset="geneva"
    )
    assert decoded == ">540min"
    assert issue is not None
    assert "picked >540min from 2 candidates" in issue.reason


def test_hour_bucket_to_minutes_conversion(geneva_fixture_dir: Path) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    bucket_minutes = sorted(dataset.scalar_ts["t_minutes"].unique().tolist())
    assert bucket_minutes[0] == 0.0
    assert all(t % 60.0 == 0.0 for t in bucket_minutes)
    assert max(bucket_minutes) <= 71 * 60.0


def test_path_traversal_guard_rejects_outside_root(tmp_path: Path) -> None:
    inside = tmp_path / "sub" / "file.csv"
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.touch()

    assert _path_traversal_guard(inside, tmp_path, dataset="geneva").resolve() == inside.resolve()
    assert _path_traversal_guard(inside, None, dataset="geneva").resolve() == inside.resolve()

    other = Path("/tmp") / "ehr_traversal_test_outside"
    with pytest.raises(AdapterError) as exc:
        _path_traversal_guard(other, tmp_path, dataset="geneva")
    assert "path traversal" in exc.value.issues[0].reason
    assert str(other.resolve()) in exc.value.issues[0].reason


def test_load_units_raises_on_missing_json(monkeypatch: pytest.MonkeyPatch) -> None:
    class _MissingResource:
        def __truediv__(self, _: str) -> _MissingResource:
            return self

        def read_text(self, encoding: str = "utf-8") -> str:
            raise FileNotFoundError("missing")

    monkeypatch.setattr(
        geneva_module.importlib.resources,
        "files",
        lambda *_a, **_kw: _MissingResource(),
    )
    _load_units.cache_clear()
    try:
        with pytest.raises(AdapterError) as exc:
            _load_units()
        assert "geneva_units.json" in str(exc.value)
    finally:
        _load_units.cache_clear()


def test_load_units_raises_on_malformed_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bad = tmp_path / "geneva_units.json"
    bad.write_text("{not json", encoding="utf-8")

    class _ResourceProxy:
        def __init__(self, path: Path) -> None:
            self.path = path

        def __truediv__(self, _: str) -> _ResourceProxy:
            return self

        def read_text(self, encoding: str = "utf-8") -> str:
            return self.path.read_text(encoding=encoding)

    monkeypatch.setattr(
        geneva_module.importlib.resources,
        "files",
        lambda *_a, **_kw: _ResourceProxy(bad),
    )
    _load_units.cache_clear()
    try:
        with pytest.raises(AdapterError) as exc:
            _load_units()
        assert "not valid JSON" in str(exc.value)
    finally:
        _load_units.cache_clear()


def test_load_normalisation_params_raises_on_missing_column(tmp_path: Path) -> None:
    bad = tmp_path / "norm.csv"
    pd.DataFrame({"variable": ["age"], "original_mean": [1.0]}).to_csv(bad, index=False)
    with pytest.raises(AdapterError) as exc:
        _load_normalisation_params(bad, dataset="geneva")
    assert "original_std" in str(exc.value)


def test_load_categorical_encoding_raises_on_malformed_cell(tmp_path: Path) -> None:
    bad = tmp_path / "cat.csv"
    pd.DataFrame(
        {
            "sample_label": ["Sex"],
            "baseline_value": ["[not a python list"],
            "other_categories": ["['Male']"],
        }
    ).to_csv(bad, index=False)
    with pytest.raises(AdapterError) as exc:
        _load_categorical_encoding(bad, sample_labels={"sex_male"}, dataset="geneva")
    assert "Sex" in str(exc.value) or "row 0" in str(exc.value)


def test_inverse_normalize_passthrough_emits_issue(
    geneva_fixture_dir: Path, tmp_path: Path
) -> None:
    csv = pd.read_csv(geneva_fixture_dir / "geneva_sample.csv", dtype=str)
    fake_label = "made_up_lab_xyz"
    extra = pd.DataFrame(
        [
            {
                "relative_sample_date_hourly_cat": "0",
                "case_admission_id": "geneva_fixture_001",
                "sample_label": fake_label,
                "source": "EHR",
                "value": "0.5",
            }
        ]
    )
    augmented = pd.concat([csv, extra], ignore_index=True)
    out_csv = tmp_path / "geneva_sample.csv"
    augmented.to_csv(out_csv, index=False)
    for fname in ("normalisation_parameters.csv", "categorical_variable_encoding.csv"):
        (tmp_path / fname).write_bytes((geneva_fixture_dir / fname).read_bytes())

    dataset = load_geneva(out_csv, tmp_path, strict=False)
    row = dataset.scalar_ts[
        (dataset.scalar_ts["patient_id"] == "geneva_fixture_001")
        & (dataset.scalar_ts["variable"] == fake_label)
    ]
    assert len(row) == 1
    assert math.isclose(float(row["value"].iloc[0]), 0.5, abs_tol=1e-9)
    assert any(
        i.reason == f"variable {fake_label} missing from normalisation_parameters"
        for i in dataset.issues
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


def test_load_geneva_routes_sources_correctly(geneva_fixture_dir: Path) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    assert set(dataset.scalar_ts["source"].unique()) == {"EHR"}
    fields = set(dataset.admission["field"].unique())
    assert "Sex" in fields
    assert "age" in fields
    assert "sex_male" not in fields
    for frame in (dataset.scalar_ts, dataset.admission, dataset.imaging, dataset.ai_output):
        if "source" in frame.columns:
            assert not frame["source"].astype(str).str.contains("imputed").any()


def test_load_geneva_admission_matches_expected_sidecar(geneva_fixture_dir: Path) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    expected = json.loads(
        (geneva_fixture_dir / "geneva_fixture_expected.json").read_text(encoding="utf-8")
    )
    actual: dict[str, dict[str, str]] = {}
    for pid, group in dataset.admission.groupby("patient_id"):
        actual[str(pid)] = {str(r.field): str(r.value) for r in group.itertuples(index=False)}
    assert actual == expected


def test_load_geneva_strict_vs_lenient(geneva_fixture_dir: Path, tmp_path: Path) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=True,
    )
    assert isinstance(dataset, GenevaDataset)

    csv = pd.read_csv(geneva_fixture_dir / "geneva_sample.csv", dtype=str)
    surviving = csv.index[
        ~csv["source"].astype(str).str.contains("imputed", na=False)
        & (csv["source"].astype(str) == "EHR")
    ]
    assert len(surviving) > 0, "fixture must contain at least one non-imputed EHR row"
    csv.loc[surviving[0], "value"] = "not a number"
    out_csv = tmp_path / "geneva_sample.csv"
    csv.to_csv(out_csv, index=False)
    for fname in ("normalisation_parameters.csv", "categorical_variable_encoding.csv"):
        (tmp_path / fname).write_bytes((geneva_fixture_dir / fname).read_bytes())

    with pytest.raises(AdapterError):
        load_geneva(out_csv, tmp_path, strict=True)

    lenient = load_geneva(out_csv, tmp_path, strict=False)
    assert lenient.issues, "expected lenient mode to surface at least one issue"


def test_load_geneva_imaging_and_ai_output_empty_but_conforming(
    geneva_fixture_dir: Path,
) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    assert len(dataset.imaging) == 0
    assert len(dataset.ai_output) == 0
    validate(dataset.imaging, CanonicalShape.IMAGING, strict=True, dataset="geneva")
    validate(dataset.ai_output, CanonicalShape.AI_OUTPUT, strict=True, dataset="geneva")


def test_load_geneva_orphan_registry_variable_emits_issue(
    geneva_fixture_dir: Path,
) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    orphan_reasons = [i.reason for i in dataset.issues if i.reason.startswith("orphan ")]
    assert orphan_reasons, "expected the fixture to contain orphan registry variables"
    assert any("made_up_orphan_var" in r for r in orphan_reasons)


# ---------------------------------------------------------------------------
# E2E
# ---------------------------------------------------------------------------


def test_load_geneva_full_fixture_roundtrip(geneva_fixture_dir: Path) -> None:
    dataset = load_geneva(
        geneva_fixture_dir / "geneva_sample.csv",
        geneva_fixture_dir,
        strict=False,
    )
    assert set(dataset.scalar_ts["patient_id"].unique()) == {
        "geneva_fixture_001",
        "geneva_fixture_002",
    }
    assert set(dataset.admission["patient_id"].unique()) == {
        "geneva_fixture_001",
        "geneva_fixture_002",
    }
    assert dataset.scalar_ts["t_minutes"].between(0.0, 71 * 60.0).all()
    validate(dataset.scalar_ts, CanonicalShape.SCALAR_TS, strict=True, dataset="geneva")
    validate(dataset.admission, CanonicalShape.ADMISSION, strict=True, dataset="geneva")
    counts = dataset.admission.groupby("patient_id").size()
    assert counts.min() >= 19


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------


def test_load_geneva_raises_on_missing_required_columns(
    geneva_fixture_dir: Path, tmp_path: Path
) -> None:
    csv = pd.read_csv(geneva_fixture_dir / "geneva_sample.csv", dtype=str)
    csv = csv.drop(columns=["source"])
    out_csv = tmp_path / "geneva_sample.csv"
    csv.to_csv(out_csv, index=False)

    with pytest.raises(AdapterError) as exc:
        load_geneva(out_csv, geneva_fixture_dir, strict=True)
    assert "source" in str(exc.value)


# ---------------------------------------------------------------------------
# S5: defensive-issue test — _read_features_csv against the Geneva fixture
# ---------------------------------------------------------------------------


def test_read_features_csv_emits_issue_for_unrecognized_source_geneva_fixture(
    geneva_fixture_dir: Path, tmp_path: Path
) -> None:
    """TODOS.md S4 carryover: a MIMIC vocab leak (``source = "notes"``) into
    a Geneva CSV must surface as an :class:`IngestionIssue` AND a structlog
    WARNING — never as a silent drop. Mirror of test_shared.py #7 + #8 but
    exercises the real Geneva fixture so the integration boundary is locked.
    """
    import structlog

    from ehr_simulator.ingestion._shared import _read_features_csv

    base = pd.read_csv(geneva_fixture_dir / "geneva_sample.csv")
    leaked_row = pd.DataFrame(
        {
            "relative_sample_date_hourly_cat": [0],
            "case_admission_id": ["geneva_fixture_001"],
            "sample_label": ["age"],
            "source": ["notes"],
            "value": [42.0],
        }
    )
    polluted = pd.concat([base, leaked_row], ignore_index=True)
    polluted_csv = tmp_path / "geneva_with_leak.csv"
    polluted.to_csv(polluted_csv, index=False)

    with structlog.testing.capture_logs() as captured:
        frame, issues = _read_features_csv(
            polluted_csv,
            required_columns=(
                "relative_sample_date_hourly_cat",
                "case_admission_id",
                "sample_label",
                "source",
                "value",
            ),
            dataset="geneva",
            known_sources=("EHR", "stroke_registry"),
        )

    # (a) The leaked row is dropped.
    assert "notes" not in frame["source"].astype(str).tolist()
    # (b) The structlog WARNING fires.
    warning_events = [e for e in captured if e.get("event_kind") == "ingest.source.unrecognized"]
    assert len(warning_events) == 1
    assert warning_events[0]["dataset"] == "geneva"
    assert warning_events[0]["source_value"] == "notes"
    # (c) The IngestionIssue surfaces in the issues list.
    assert any(i.dataset == "geneva" and "notes" in i.reason for i in issues)
