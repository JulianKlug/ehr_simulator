"""Clinical display registry (S8): labels, panel groups and order.

Pure data + lookups: no request, template or DB import, so preflight
(``cli_support``) can use it. The adapters own source → canonical id
conversion (``ingestion/geneva.py``); this module maps canonical ids and
raw ADMISSION fields to what a clinician reads::

    SCALAR_TS.variable  ─┐                    ┌─▶ vitals / neuro_support /
    ADMISSION.field     ─┼─▶ clinical_fields ─┤   labs / imaging panels
    SHAP feature name   ─┘                    └─▶ AI contributor labels

Order inside each tuple is the display order.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ehr_simulator.ingestion.canonical import (
    AGGREGATE_BOUNDS,
    IMAGING_DERIVED_VAR_SET,
    IMAGING_FINDING_FIELDS,
    LAB_VAR_SET,
    NEURO_SUPPORT_VAR_SET,
    VITAL_VAR_SET,
)


class DisplayGroup(StrEnum):
    """Where a variable is shown. Each variable has exactly one (D13)."""

    VITALS = "vitals"
    NEURO_SUPPORT = "neuro_support"
    LABS = "labs"
    ADMISSION_BASELINE = "admission_baseline"
    ADMISSION_TREATMENT = "admission_treatment"
    IMAGING = "imaging"
    AI = "ai"
    INTENTIONALLY_HIDDEN = "intentionally_hidden"


class ValueFormat(StrEnum):
    NUMBER = "number"  # numeric table cell
    YES_NO = "yes_no"  # "True"/"False" → "Yes"/"No"
    AS_SUPPLIED = "as_supplied"  # decoded adapter value, shown verbatim


@dataclass(frozen=True)
class ScalarField:
    """One canonical SCALAR_TS id. ``family`` is the median id of its
    aggregate family (``hr`` for ``hr_min``), else ``None``."""

    canonical_id: str
    label: str
    group: DisplayGroup
    source: str  # Geneva sample_label
    family: str | None = None


def _family(median: str, label: str, source: str, group: DisplayGroup) -> tuple[ScalarField, ...]:
    """Median + bounds of one Geneva aggregate triplet, e.g. ``hr``."""
    low, high = AGGREGATE_BOUNDS[median]
    return (
        ScalarField(median, label, group, f"median_{source}", median),
        ScalarField(low, f"{label} min", group, f"min_{source}", median),
        ScalarField(high, f"{label} max", group, f"max_{source}", median),
    )


_V = DisplayGroup.VITALS
_N = DisplayGroup.NEURO_SUPPORT
_L = DisplayGroup.LABS
_I = DisplayGroup.IMAGING

SCALAR_FIELDS: tuple[ScalarField, ...] = (
    *_family("hr", "Heart rate", "heart_rate", _V),
    *_family("map", "Mean BP", "mean_blood_pressure", _V),
    *_family("sbp", "Systolic BP", "systolic_blood_pressure", _V),
    *_family("dbp", "Diastolic BP", "diastolic_blood_pressure", _V),
    *_family("spo2", "SpO₂", "oxygen_saturation", _V),
    *_family("rr", "Respiratory rate", "respiratory_rate", _V),
    ScalarField("temp", "Temperature", _V, "temperature"),
    *_family("nihss", "NIHSS", "NIHSS", _N),
    ScalarField("gcs", "GCS", _N, "Glasgow Coma Scale"),
    ScalarField("fio2", "FiO₂", _N, "FIO2"),
    ScalarField("hct", "Hematocrit", _L, "hematocrite"),
    ScalarField("hgb", "Hemoglobin", _L, "hemoglobine"),
    ScalarField("hba1c", "HbA1c", _L, "hemoglobine glyquee"),
    ScalarField("cr", "Creatinine", _L, "creatinine"),
    ScalarField("k", "Potassium", _L, "potassium"),
    ScalarField("na", "Sodium", _L, "sodium"),
    ScalarField("cl", "Chloride", _L, "chlore"),
    ScalarField("urea", "Urea", _L, "uree"),
    ScalarField("calcium", "Calcium (corrected)", _L, "calcium corrige"),
    ScalarField("phosphate", "Phosphate", _L, "phosphates"),
    ScalarField("alt", "ALT", _L, "ALAT"),
    ScalarField("ast", "AST", _L, "ASAT"),
    ScalarField("bilirubin", "Total bilirubin", _L, "bilirubine totale"),
    ScalarField("triglycerides", "Triglycerides", _L, "triglycerides"),
    ScalarField("hdl", "HDL cholesterol", _L, "cholesterol HDL"),
    ScalarField("ldl", "LDL cholesterol (calculated)", _L, "LDL cholesterol calcule"),
    ScalarField("cholesterol", "Total cholesterol", _L, "cholesterol total"),
    ScalarField("wbc", "Leukocytes", _L, "leucocytes"),
    ScalarField("plt", "Platelets", _L, "thrombocytes"),
    ScalarField("rbc", "Erythrocytes", _L, "erythrocytes"),
    ScalarField("neutrophils", "Neutrophils (abs.)", _L, "neutrophiles-nb abs"),
    ScalarField("inr", "INR", _L, "INR"),
    ScalarField("ptt", "PTT", _L, "PTT"),
    ScalarField("fibrinogen", "Fibrinogen", _L, "fibrinogene"),
    ScalarField("probnp", "proBNP", _L, "proBNP"),
    ScalarField("crp", "CRP", _L, "proteine C-reactive"),
    ScalarField("lactate", "Lactate", _L, "lactate"),
    ScalarField("glucose", "Glucose", _L, "glucose"),
    ScalarField("tmax_gt_4", "Tmax > 4 s", _I, "tmax_gt_4"),
    ScalarField("tmax_gt_6", "Tmax > 6 s", _I, "tmax_gt_6"),
    ScalarField("tmax_gt_8", "Tmax > 8 s", _I, "tmax_gt_8"),
    ScalarField("tmax_gt_10", "Tmax > 10 s", _I, "tmax_gt_10"),
    ScalarField("cbf_lt_20", "CBF < 20%", _I, "cbf_lt_20"),
    ScalarField("cbf_lt_30", "CBF < 30%", _I, "cbf_lt_30"),
    ScalarField("cbf_lt_34", "CBF < 34%", _I, "cbf_lt_34"),
    ScalarField("cbf_lt_38", "CBF < 38%", _I, "cbf_lt_38"),
    ScalarField("cbv_lt_34", "CBV < 34%", _I, "cbv_lt_34"),
    ScalarField("cbv_lt_38", "CBV < 38%", _I, "cbv_lt_38"),
    ScalarField("cbv_lt_42", "CBV < 42%", _I, "cbv_lt_42"),
)
_SCALAR_BY_ID: dict[str, ScalarField] = {f.canonical_id: f for f in SCALAR_FIELDS}

#: Canonical variable set backing each scalar panel (the ingestion sets).
_GROUP_VAR_SETS: dict[DisplayGroup, frozenset[str]] = {
    DisplayGroup.VITALS: VITAL_VAR_SET,
    DisplayGroup.NEURO_SUPPORT: NEURO_SUPPORT_VAR_SET,
    DisplayGroup.LABS: LAB_VAR_SET,
    DisplayGroup.IMAGING: IMAGING_DERIVED_VAR_SET,
}


def scalar_field(canonical_id: str) -> ScalarField | None:
    return _SCALAR_BY_ID.get(canonical_id)


def scalar_label(canonical_id: str) -> str:
    """Clinical label, or the raw id for a variable outside the registry."""
    field = _SCALAR_BY_ID.get(canonical_id)
    return field.label if field is not None else canonical_id


def ordered_ids(group: DisplayGroup) -> tuple[str, ...]:
    """Registry ids of one scalar panel, display order."""
    return tuple(f.canonical_id for f in SCALAR_FIELDS if f.group is group)


def group_var_set(group: DisplayGroup) -> frozenset[str]:
    return _GROUP_VAR_SETS[group]


def displayed_scalar_ids() -> frozenset[str]:
    """Every scalar id some panel shows (bounds included)."""
    return frozenset().union(*_GROUP_VAR_SETS.values())


# ---------------------------------------------------------------------------
# ADMISSION
# ---------------------------------------------------------------------------


class AdmissionSection(StrEnum):
    DEMOGRAPHICS = "Demographics"
    PRESTROKE_MEDICATION = "Prestroke medication"
    MEDICAL_HISTORY = "Medical history"
    STROKE_CONTEXT = "Stroke context"
    TREATMENT = "Treatment / presentation"
    IMAGING_FINDINGS = "Imaging / vascular findings"  # imaging panel, not admission


#: Unregistered admission fields render here, alphabetical, raw name.
OTHER_SECTION = "Other"


@dataclass(frozen=True)
class AdmissionField:
    field: str  # raw ADMISSION.field (exact Geneva name)
    label: str
    section: AdmissionSection
    value_format: ValueFormat = ValueFormat.AS_SUPPLIED

    @property
    def group(self) -> DisplayGroup:
        if self.section is AdmissionSection.IMAGING_FINDINGS:
            return DisplayGroup.IMAGING
        if self.section is AdmissionSection.TREATMENT:
            return DisplayGroup.ADMISSION_TREATMENT
        return DisplayGroup.ADMISSION_BASELINE


_D = AdmissionSection.DEMOGRAPHICS
_M = AdmissionSection.PRESTROKE_MEDICATION
_H = AdmissionSection.MEDICAL_HISTORY
_C = AdmissionSection.STROKE_CONTEXT
_T = AdmissionSection.TREATMENT
_F = AdmissionSection.IMAGING_FINDINGS

_FINDING_LABELS: dict[str, str] = {
    "hypoperfusion_with_mismatch": "Hypoperfusion with mismatch",
    "hypoperfusion_without_mismatch": "Hypoperfusion without mismatch",
    "vascular_occlusion": "Vascular occlusion",
    "vascular_stenosis_over_50p": "Vascular stenosis > 50%",
}

ADMISSION_FIELDS: tuple[AdmissionField, ...] = (
    AdmissionField("age", "Age", _D),
    AdmissionField("Sex", "Sex", _D),
    AdmissionField("sex", "Sex", _D),  # synthetic spelling
    AdmissionField("weight", "Weight", _D),
    AdmissionField("Anticoagulants", "Anticoagulants", _M),
    AdmissionField("Antihypert. drugs pre-stroke", "Antihypertensive drugs", _M),
    AdmissionField("Antiplatelet drugs", "Antiplatelet drugs", _M),
    AdmissionField("Lipid lowering drugs pre-stroke", "Lipid lowering drugs", _M),
    AdmissionField("MedHist Atrial Fibr.", "Atrial fibrillation", _H),
    AdmissionField("MedHist CHD", "Coronary disease", _H),
    AdmissionField("MedHist Diabetes", "Diabetes", _H),
    AdmissionField("MedHist cerebrovascular_event", "Previous cerebrovascular event", _H),
    AdmissionField("MedHist Hyperlipidemia", "Hyperlipidaemia", _H),
    AdmissionField("MedHist Hypertension", "Hypertension", _H),
    AdmissionField("MedHist Smoking", "Smoking", _H),
    AdmissionField("MedHist PAD", "Peripheral artery disease", _H),
    AdmissionField("wake_up_stroke", "Wake up stroke", _C),
    AdmissionField("Prestroke disability (Rankin)", "Prestroke disability, mRS", _C),
    AdmissionField("categorical_IAT", "IAT timing", _T),
    AdmissionField("categorical_IVT", "IVT timing", _T),
    AdmissionField("categorical_onset_to_admission_time", "Onset to admission", _T),
    AdmissionField("Referral", "Referral", _T),
    *(
        AdmissionField(name, _FINDING_LABELS[name], _F, ValueFormat.YES_NO)
        for name in IMAGING_FINDING_FIELDS
    ),
)
_ADMISSION_BY_FIELD: dict[str, AdmissionField] = {f.field: f for f in ADMISSION_FIELDS}


@dataclass(frozen=True)
class DisplayFact:
    field: str  # raw ADMISSION.field
    label: str
    value: str


@dataclass(frozen=True)
class FactSection:
    title: str
    facts: tuple[DisplayFact, ...]


def format_value(value: object, value_format: ValueFormat) -> str:
    """``YES_NO``: ``"True"`` → ``"Yes"``; anything else verbatim."""
    text = "" if value is None else str(value)
    if value_format is ValueFormat.YES_NO:
        return {"True": "Yes", "False": "No"}.get(text, text)
    return text


def admission_sections(facts: Mapping[str, object]) -> list[FactSection]:
    """Admission panel sections in display order, then ``Other``.

    Imaging findings are left out (the imaging panel shows them). Empty
    sections are dropped. Example: ``{"Sex": "Male", "x": "1"}`` →
    ``[Demographics: Sex=Male, Other: x=1]``.
    """
    sections: list[FactSection] = []
    for section in AdmissionSection:
        if section is AdmissionSection.IMAGING_FINDINGS:
            continue
        shown = tuple(
            DisplayFact(f.field, f.label, format_value(facts[f.field], f.value_format))
            for f in ADMISSION_FIELDS
            if f.section is section and f.field in facts
        )
        if shown:
            sections.append(FactSection(section.value, shown))

    other = tuple(
        DisplayFact(name, name, format_value(facts[name], ValueFormat.AS_SUPPLIED))
        for name in sorted(facts)
        if name not in _ADMISSION_BY_FIELD
    )
    if other:
        sections.append(FactSection(OTHER_SECTION, other))
    return sections


def imaging_findings(facts: Mapping[str, object]) -> tuple[DisplayFact, ...]:
    """The binary imaging findings present in ``facts``, display order."""
    return tuple(
        DisplayFact(f.field, f.label, format_value(facts[f.field], f.value_format))
        for f in ADMISSION_FIELDS
        if f.section is AdmissionSection.IMAGING_FINDINGS and f.field in facts
    )


class SummaryFact(StrEnum):
    AGE = "age"
    SEX = "sex"


#: Registry fields backing each summary card fact, first present wins.
_SUMMARY_FIELDS: dict[SummaryFact, tuple[str, ...]] = {
    SummaryFact.AGE: ("age",),
    SummaryFact.SEX: ("Sex", "sex"),
}


def summary_fact(facts: Mapping[str, object], which: SummaryFact) -> str | None:
    """Summary card value through the registry: Geneva ``Sex`` or synthetic ``sex``."""
    for name in _SUMMARY_FIELDS[which]:
        if name in facts:
            return format_value(facts[name], _ADMISSION_BY_FIELD[name].value_format)
    return None


# ---------------------------------------------------------------------------
# SHAP feature names (spec §6.2 grammar)
# ---------------------------------------------------------------------------

_TIMESTEP_FEATURE = "timestep_idx"
_TIMESTEP_LABEL = "Time since admission"

#: ``<transform>_<source label>``; ``lagN`` handled by :data:`_LAG_RE`.
_TRANSFORMS: tuple[tuple[str, str], ...] = (
    ("rolling_mean_", "rolling mean"),
    ("rolling_std_", "rolling SD"),
    ("rolling_trend_", "rolling trend"),
    ("avg_", "mean"),
    ("min_", "min"),
    ("max_", "max"),
    ("std_", "SD"),
    ("diff_", "change"),
)
_LAG_RE = re.compile(r"^lag(\d+)_(.+)$")

#: One-hot registry features: ``<prefix><category>`` → ``<label>: <category>``.
_ONE_HOT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("categorical_onset_to_admission_time_", "Onset to admission"),
    ("categorical_ivt_", "IVT timing"),
    ("categorical_iat_", "IAT timing"),
    ("prestroke_disability_(rankin)_", "Prestroke disability, mRS"),
    ("referral_", "Referral"),
    ("sex_", "Sex"),
)

#: Binary registry features named after their admission field.
_REGISTRY_FEATURES: dict[str, str] = {
    "age": "Age",
    "weight": "Weight",
    "anticoagulants_yes": "Anticoagulants",
    "antihypert._drugs_pre-stroke_yes": "Antihypertensive drugs",
    "antiplatelet_drugs_yes": "Antiplatelet drugs",
    "lipid_lowering_drugs_pre-stroke_yes": "Lipid lowering drugs",
    "medhist_atrial_fibr._yes": "Atrial fibrillation",
    "medhist_chd_yes": "Coronary disease",
    "medhist_diabetes_yes": "Diabetes",
    "medhist_cerebrovascular_event_true": "Previous cerebrovascular event",
    "medhist_hyperlipidemia_yes": "Hyperlipidaemia",
    "medhist_hypertension_yes": "Hypertension",
    "medhist_smoking_yes": "Smoking",
    "medhist_pad_yes": "Peripheral artery disease",
    "wake_up_stroke_true": "Wake up stroke",
    **_FINDING_LABELS,
}
_SCALAR_FEATURES: dict[str, str] = {f.source: f.label for f in SCALAR_FIELDS}


def _source_label(name: str) -> str | None:
    """Clinical label of a bare Geneva source label, else ``None``."""
    if name in _SCALAR_FEATURES:
        return _SCALAR_FEATURES[name]
    if name in _REGISTRY_FEATURES:
        return _REGISTRY_FEATURES[name]
    for prefix, label in _ONE_HOT_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return f"{label}: {name[len(prefix) :].replace('_', ' ')}"
    return None


def contributor_label(feature: str) -> str | None:
    """Clinical label of a SHAP feature, or ``None`` when unresolved.

    Exact source label first, else one transform stripped. Examples:
    ``lag2_glucose`` → ``Glucose (2 h earlier)``, ``max_heart_rate`` →
    ``Heart rate max``, ``timestep_idx`` → ``Time since admission``.
    """
    if feature == _TIMESTEP_FEATURE:
        return _TIMESTEP_LABEL

    exact = _source_label(feature)
    if exact is not None:
        return exact

    lag = _LAG_RE.match(feature)
    if lag is not None:
        base = _source_label(lag.group(2))
        return None if base is None else f"{base} ({lag.group(1)} h earlier)"

    for prefix, transform in _TRANSFORMS:
        if not feature.startswith(prefix):
            continue
        base = _source_label(feature[len(prefix) :])
        if base is not None:
            return f"{base} ({transform})"
    return None
