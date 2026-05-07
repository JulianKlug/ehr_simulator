"""Lift-equivalence + FNF + unrecognized-source defense for `_shared.py`.

These tests lock the parameterization contract introduced when S4 lifted
S3's helpers out of :mod:`ehr_simulator.ingestion.geneva`. Each test
exercises a helper across both Geneva and MIMIC inputs (or against
synthetic frames spanning both source vocabularies) so that re-forking
the helpers later — adding dataset-specific behavior in either adapter
— surfaces as a failing test, not a silent divergence.

The function-identity sub-test (#4) lands when ``mimic.py`` does, in
commit 2; the remaining six tests land in commit 1.
"""

from __future__ import annotations

import math
from pathlib import Path

import pandas as pd
import pytest
import structlog

from ehr_simulator.ingestion import _shared
from ehr_simulator.ingestion._shared import (
    CategoricalGroup,
    _decode_categorical,
    _drop_imputed,
    _inverse_normalize,
    _load_categorical_encoding,
    _load_normalisation_params,
    _path_traversal_guard,
    _read_features_csv,
)
from ehr_simulator.ingestion.exceptions import AdapterError

_REQUIRED_COLUMNS: tuple[str, ...] = (
    "relative_sample_date_hourly_cat",
    "case_admission_id",
    "sample_label",
    "source",
    "value",
)


# ---------------------------------------------------------------------------
# #1 — _drop_imputed across both source vocabularies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_vocab", "non_imputed_survivors"),
    [
        (
            (
                "EHR",
                "EHR_locf_imputed",
                "EHR_pop_imputed",
                "EHR_pop_imputed_locf_imputed",
                "stroke_registry",
                "stroke_registry_locf_imputed",
                "stroke_registry_pop_imputed",
                "stroke_registry_pop_imputed_locf_imputed",
            ),
            ("EHR", "stroke_registry"),
        ),
        (
            (
                "EHR",
                "EHR_locf_imputed",
                "EHR_pop_imputed",
                "EHR_pop_imputed_locf_imputed",
                "notes",
                "notes_locf_imputed",
                "missing_pop_imputed",
                "missing_pop_imputed_locf_imputed",
            ),
            ("EHR", "notes"),
        ),
    ],
    ids=["geneva", "mimic"],
)
def test_shared_drop_imputed_handles_geneva_and_mimic_source_vocabularies(
    source_vocab: tuple[str, ...], non_imputed_survivors: tuple[str, ...]
) -> None:
    frame = pd.DataFrame({"source": list(source_vocab), "value": list(range(len(source_vocab)))})
    out = _drop_imputed(frame)
    assert sorted(out["source"].tolist()) == sorted(non_imputed_survivors)


# ---------------------------------------------------------------------------
# #2 — inverse_normalize round-trip on real (mean, std) pairs from both datasets
# ---------------------------------------------------------------------------


def test_shared_inverse_normalize_pure_math() -> None:
    geneva_params = pd.read_csv(
        Path(__file__).parent / "fixtures" / "geneva" / "normalisation_parameters.csv"
    )
    pairs = [
        (float(row.original_mean), float(row.original_std))
        for row in geneva_params.itertuples(index=False)
        if float(row.original_std) > 0.0
    ]
    assert pairs, "expected at least one (mean, std) pair in Geneva normalisation_parameters"
    for x, (mean, std) in zip([0.5, -1.2, 17.3, -42.0], pairs[:4], strict=True):
        z = (x - mean) / std
        assert math.isclose(_inverse_normalize(z, mean, std), x, abs_tol=1e-9)


# ---------------------------------------------------------------------------
# #3 — path-traversal-guard threads dataset kwarg into the issue
# ---------------------------------------------------------------------------


def test_shared_path_traversal_guard_dataset_param_in_issue(tmp_path: Path) -> None:
    outside = Path("/tmp") / "ehr_traversal_test_shared_outside"

    with pytest.raises(AdapterError) as exc_geneva:
        _path_traversal_guard(outside, tmp_path, dataset="geneva")
    assert exc_geneva.value.issues[0].dataset == "geneva"

    with pytest.raises(AdapterError) as exc_mimic:
        _path_traversal_guard(outside, tmp_path, dataset="mimic")
    assert exc_mimic.value.issues[0].dataset == "mimic"


# ---------------------------------------------------------------------------
# #4 — layered parity regression: function identity + behavioral cross-vocab
# ---------------------------------------------------------------------------


def test_shared_helpers_produce_identical_output_for_equivalent_inputs() -> None:
    """ROADMAP-mandated parity regression.

    Sub-(a) function identity: every helper exported by ``_shared.__all__``
    is the same Python object whether reached via ``geneva`` or ``mimic``.
    Catches accidental re-fork.

    Sub-(b) behavioral parity on synthetic cross-vocabulary inputs: the
    helper output is identical regardless of which adapter's source vocab
    the input rows came from.
    """
    from ehr_simulator.ingestion import geneva as geneva_module
    from ehr_simulator.ingestion import mimic as mimic_module

    # Sub-(a): function identity over _shared.__all__
    for name in _shared.__all__:
        shared_obj = getattr(_shared, name)
        geneva_obj = getattr(geneva_module, name)
        mimic_obj = getattr(mimic_module, name)
        assert shared_obj is geneva_obj, f"{name} re-forked between _shared and geneva"
        assert shared_obj is mimic_obj, f"{name} re-forked between _shared and mimic"

    # Sub-(b1): _drop_imputed produces same surviving row-count regardless of
    # which dataset's source vocab the rows came from.
    cross_vocab = pd.DataFrame(
        {
            "source": [
                "EHR",
                "stroke_registry",
                "stroke_registry_pop_imputed",
                "notes",
                "notes_locf_imputed",
                "missing_pop_imputed",
            ],
            "value": [1, 2, 3, 4, 5, 6],
        }
    )
    survivors_via_geneva = geneva_module._drop_imputed(cross_vocab)
    survivors_via_mimic = mimic_module._drop_imputed(cross_vocab)
    pd.testing.assert_frame_equal(survivors_via_geneva, survivors_via_mimic)
    assert sorted(survivors_via_geneva["source"].tolist()) == [
        "EHR",
        "notes",
        "stroke_registry",
    ]

    # Sub-(b2): _inverse_normalize is pure math — same z, mean, std → same float
    z, mean, std = 1.5, 73.6, 14.5
    via_geneva = geneva_module._inverse_normalize(z, mean, std)
    via_mimic = mimic_module._inverse_normalize(z, mean, std)
    assert via_geneva == via_mimic

    # Sub-(b3): _decode_categorical returns the same (label, None) regardless
    # of the dataset kwarg when only one row is >=0.5.
    group = CategoricalGroup(
        group_name="Sex",
        baseline="Female",
        other_labels=("Male",),
        one_hot_columns=("sex_male",),
    )
    rows = pd.DataFrame([{"sample_label": "sex_male", "value": 0.7}])
    decoded_g, issue_g = _decode_categorical(
        rows, group, strict=True, patient_id="p1", dataset="geneva"
    )
    decoded_m, issue_m = _decode_categorical(
        rows, group, strict=True, patient_id="p1", dataset="mimic"
    )
    assert decoded_g == decoded_m == "Male"
    assert issue_g is None and issue_m is None


# ---------------------------------------------------------------------------
# #5 — _load_normalisation_params wraps FileNotFoundError as AdapterError
# ---------------------------------------------------------------------------


def test_shared_load_normalisation_params_wraps_fnf_as_adapter_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "reference_population_normalisation_parameters.csv"
    with pytest.raises(AdapterError) as exc:
        _load_normalisation_params(missing, dataset="mimic")
    assert "mimic" in str(exc.value)
    assert missing.name in str(exc.value)
    assert str(missing) in str(exc.value)
    assert exc.value.issues[0].dataset == "mimic"
    assert "not found" in exc.value.issues[0].reason


# ---------------------------------------------------------------------------
# #6 — _load_categorical_encoding wraps FileNotFoundError as AdapterError
# ---------------------------------------------------------------------------


def test_shared_load_categorical_encoding_wraps_fnf_as_adapter_error(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist" / "categorical_variable_encoding.csv"
    with pytest.raises(AdapterError) as exc:
        _load_categorical_encoding(missing, sample_labels=set(), dataset="geneva")
    assert "geneva" in str(exc.value)
    assert missing.name in str(exc.value)
    assert str(missing) in str(exc.value)
    assert exc.value.issues[0].dataset == "geneva"
    assert "not found" in exc.value.issues[0].reason


# ---------------------------------------------------------------------------
# #7 — _read_features_csv emits IngestionIssue for unrecognized source
# ---------------------------------------------------------------------------


def test_shared_read_features_csv_emits_issue_for_unrecognized_source(tmp_path: Path) -> None:
    csv = tmp_path / "tiny.csv"
    pd.DataFrame(
        {
            "relative_sample_date_hourly_cat": [0, 0, 0],
            "case_admission_id": ["p1", "p1", "p1"],
            "sample_label": ["age", "weight", "age"],
            "source": ["EHR", "stroke_registry", "unknown_vocab_v2"],
            "value": [0.1, 0.2, 0.3],
        }
    ).to_csv(csv, index=False)

    frame, issues = _read_features_csv(
        csv,
        required_columns=_REQUIRED_COLUMNS,
        dataset="geneva",
        known_sources=("EHR", "stroke_registry"),
    )
    assert "unknown_vocab_v2" not in frame["source"].astype(str).tolist()
    assert any(
        i.dataset == "geneva" and i.reason == "unrecognized source value: unknown_vocab_v2"
        for i in issues
    )


# ---------------------------------------------------------------------------
# #8 — _decode_categorical argmax fallback emits structlog WARNING (S5)
# ---------------------------------------------------------------------------


def test_decode_categorical_argmax_fallback_emits_warning() -> None:
    """Per /plan-eng-review note in §3: use ``structlog.testing.capture_logs``
    NOT pytest's ``caplog`` — structlog events do not flow through stdlib
    logging unless explicitly chained, so caplog would silently miss them.
    """
    group = CategoricalGroup(
        group_name="stroke_location",
        baseline="left_MCA",
        other_labels=("right_MCA", "no_stroke"),
        one_hot_columns=("stroke_location_right_mca", "stroke_location_no_stroke"),
    )
    rows = pd.DataFrame(
        {
            "sample_label": ["stroke_location_right_mca", "stroke_location_no_stroke"],
            "value": [0.7, 0.6],  # both >=0.5 → ambiguous
        }
    )

    with structlog.testing.capture_logs() as captured:
        decoded, issue = _decode_categorical(
            rows,
            group,
            strict=False,
            patient_id="p1",
            dataset="geneva",
        )

    assert issue is not None  # S3 behavior preserved
    assert decoded in {"right_MCA", "no_stroke"}
    warning_events = [
        e for e in captured if e.get("event_kind") == "ingest.categorical.argmax_fallback"
    ]
    assert len(warning_events) == 1
    event = warning_events[0]
    assert event["log_level"] == "warning"
    assert event["dataset"] == "geneva"
    assert event["patient_id"] == "p1"
    assert event["group_name"] == "stroke_location"
    assert event["candidate_count"] == 2


def test_read_features_csv_unrecognized_source_emits_warning(tmp_path: Path) -> None:
    csv = tmp_path / "tiny.csv"
    pd.DataFrame(
        {
            "relative_sample_date_hourly_cat": [0, 0],
            "case_admission_id": ["p1", "p1"],
            "sample_label": ["age", "weight"],
            "source": ["EHR", "leaked_vocab"],
            "value": [0.1, 0.2],
        }
    ).to_csv(csv, index=False)

    with structlog.testing.capture_logs() as captured:
        _read_features_csv(
            csv,
            required_columns=_REQUIRED_COLUMNS,
            dataset="geneva",
            known_sources=("EHR", "stroke_registry"),
        )

    warning_events = [e for e in captured if e.get("event_kind") == "ingest.source.unrecognized"]
    assert len(warning_events) == 1
    event = warning_events[0]
    assert event["log_level"] == "warning"
    assert event["dataset"] == "geneva"
    assert event["source_value"] == "leaked_vocab"
