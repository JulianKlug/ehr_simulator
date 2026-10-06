"""S8 clinical display registry: coverage, order, sections, SHAP labels."""

from __future__ import annotations

import pytest

from ehr_simulator.ingestion.canonical import (
    AGGREGATE_BOUND_VAR_SET,
    IMAGING_DERIVED_VAR_SET,
    IMAGING_FINDING_FIELDS,
    LAB_VAR_SET,
    NEURO_SUPPORT_VAR_SET,
    VITAL_VAR_SET,
)
from ehr_simulator.web.clinical_fields import (
    ADMISSION_FIELDS,
    SCALAR_FIELDS,
    DisplayGroup,
    SummaryFact,
    admission_sections,
    contributor_label,
    imaging_findings,
    ordered_ids,
    scalar_field,
    summary_fact,
)
from tests.test_geneva import _ALL_GENEVA_SAMPLE_LABELS

#: §1.2 / §3 / §5.1 SCALAR_TS source labels (``source == "EHR"`` on the real CSV).
GENEVA_EHR_LABELS = frozenset(
    {
        *(
            f"{agg}_{v}"
            for agg in ("min", "median", "max")
            for v in (
                "heart_rate",
                "mean_blood_pressure",
                "systolic_blood_pressure",
                "diastolic_blood_pressure",
                "oxygen_saturation",
                "respiratory_rate",
                "NIHSS",
            )
        ),
        "temperature",
        "Glasgow Coma Scale",
        "FIO2",
        "hematocrite",
        "hemoglobine",
        "hemoglobine glyquee",
        "creatinine",
        "potassium",
        "sodium",
        "chlore",
        "uree",
        "calcium corrige",
        "phosphates",
        "ALAT",
        "ASAT",
        "bilirubine totale",
        "triglycerides",
        "cholesterol HDL",
        "LDL cholesterol calcule",
        "cholesterol total",
        "leucocytes",
        "thrombocytes",
        "erythrocytes",
        "neutrophiles-nb abs",
        "INR",
        "PTT",
        "fibrinogene",
        "proBNP",
        "proteine C-reactive",
        "lactate",
        "glucose",
        *IMAGING_DERIVED_VAR_SET,
    }
)

LAB_ORDER = (
    "hct", "hgb", "hba1c", "cr", "k", "na", "cl", "urea", "calcium", "phosphate",
    "alt", "ast", "bilirubin", "triglycerides", "hdl", "ldl", "cholesterol", "wbc",
    "plt", "rbc", "neutrophils", "inr", "ptt", "fibrinogen", "probnp", "crp",
    "lactate", "glucose",
)  # fmt: skip


def test_registry_covers_every_geneva_scalar_variable() -> None:
    sources = {f.source for f in SCALAR_FIELDS}
    assert sources >= GENEVA_EHR_LABELS, sorted(GENEVA_EHR_LABELS - sources)


def test_every_inventory_label_has_a_disposition() -> None:
    """Every real Geneva label is a registered scalar or resolves as a feature."""
    sources = {f.source for f in SCALAR_FIELDS}
    unplaced = sorted(
        label
        for label in _ALL_GENEVA_SAMPLE_LABELS
        if label not in sources and contributor_label(label) is None
    )
    assert unplaced == []


def test_no_study_variable_is_hidden() -> None:
    groups = {f.group for f in SCALAR_FIELDS} | {f.group for f in ADMISSION_FIELDS}
    assert DisplayGroup.INTENTIONALLY_HIDDEN not in groups


def test_each_scalar_id_is_registered_once_in_its_panel_set() -> None:
    ids = [f.canonical_id for f in SCALAR_FIELDS]
    assert len(ids) == len(set(ids))
    panel_sets = {
        DisplayGroup.VITALS: VITAL_VAR_SET,
        DisplayGroup.NEURO_SUPPORT: NEURO_SUPPORT_VAR_SET,
        DisplayGroup.LABS: LAB_VAR_SET,
        DisplayGroup.IMAGING: IMAGING_DERIVED_VAR_SET,
    }
    for group, var_set in panel_sets.items():
        assert set(ordered_ids(group)) == var_set, group


def test_lab_registry_is_the_full_set_in_fixed_order() -> None:
    assert ordered_ids(DisplayGroup.LABS) == LAB_ORDER


def test_neuro_support_registry() -> None:
    assert set(ordered_ids(DisplayGroup.NEURO_SUPPORT)) == {
        "nihss",
        "nihss_min",
        "nihss_max",
        "gcs",
        "fio2",
    }
    assert scalar_field("nihss_max").family == "nihss"
    assert scalar_field("gcs").family is None


def test_imaging_registry() -> None:
    assert set(ordered_ids(DisplayGroup.IMAGING)) == {
        *(f"tmax_gt_{s}" for s in (4, 6, 8, 10)),
        *(f"cbf_lt_{p}" for p in (20, 30, 34, 38)),
        *(f"cbv_lt_{p}" for p in (34, 38, 42)),
    }
    findings = {f.field for f in ADMISSION_FIELDS if f.group is DisplayGroup.IMAGING}
    assert findings == set(IMAGING_FINDING_FIELDS)


def test_fio2_glucose_weight_have_one_location() -> None:
    assert scalar_field("fio2").group is DisplayGroup.NEURO_SUPPORT
    assert scalar_field("glucose").group is DisplayGroup.LABS
    assert [f.field for f in ADMISSION_FIELDS if f.field == "weight"] == ["weight"]
    assert "weight" not in VITAL_VAR_SET


def test_bounds_are_their_familys() -> None:
    for bound in AGGREGATE_BOUND_VAR_SET:
        field = scalar_field(bound)
        assert field is not None and field.family == bound.rsplit("_", 1)[0]


def test_admission_sections_group_and_order() -> None:
    facts = {
        "zeta_extra": "1",
        "Referral": "Self referral or GP",
        "MedHist CHD": "yes",
        "Sex": "Male",
        "age": "70.1",
        "alpha_extra": "2",
        "vascular_occlusion": "True",
    }
    sections = admission_sections(facts)
    assert [s.title for s in sections] == [
        "Demographics",
        "Medical history",
        "Treatment / presentation",
        "Other",
    ]
    assert [f.label for f in sections[0].facts] == ["Age", "Sex"]
    assert [f.field for f in sections[-1].facts] == ["alpha_extra", "zeta_extra"]
    # The imaging finding lives in the imaging panel only.
    assert all(f.field != "vascular_occlusion" for s in sections for f in s.facts)
    assert [(f.label, f.value) for f in imaging_findings(facts)] == [("Vascular occlusion", "Yes")]


def test_treatment_value_shown_as_supplied() -> None:
    sections = admission_sections({"categorical_IVT": "271-540min"})
    assert sections[0].facts[0].value == "271-540min"


@pytest.mark.parametrize(
    ("facts", "expected"), [({"Sex": "Male"}, "Male"), ({"sex": "F"}, "F"), ({}, None)]
)
def test_summary_sex_through_registry(facts: dict[str, str], expected: str | None) -> None:
    assert summary_fact(facts, SummaryFact.SEX) == expected


@pytest.mark.parametrize(
    ("feature", "label"),
    [
        ("lag2_glucose", "Glucose (2 h earlier)"),
        ("timestep_idx", "Time since admission"),
        ("ALAT", "ALT"),
        ("max_heart_rate", "Heart rate max"),
        ("max_max_heart_rate", "Heart rate max (max)"),
        ("rolling_trend_median_NIHSS", "NIHSS (rolling trend)"),
        ("avg_FIO2", "FiO₂ (mean)"),
        ("sex_male", "Sex: male"),
        ("categorical_ivt_<90min", "IVT timing: <90min"),
        ("std_medhist_chd_yes", "Coronary disease (SD)"),
        ("vascular_occlusion", "Vascular occlusion"),
    ],
)
def test_contributor_label_grammar(feature: str, label: str) -> None:
    assert contributor_label(feature) == label


@pytest.mark.parametrize("feature", ["avg_heart_rate", "made_up", "lag2_", "rolling_mean_x"])
def test_unknown_contributor_is_unresolved(feature: str) -> None:
    assert contributor_label(feature) is None
