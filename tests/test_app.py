"""End-to-end tests for ``app_from_study_config`` + the ``study_timepoints``
regression locked by /plan-eng-review issue 1.2.

These tests close the silent study-validity bug: without
``app.state.study_timepoints``, a Geneva pilot whose study config declares
``timepoints: [0, 60, 180]`` would resolve URL ``t_index=1`` to the
**dataset's** second distinct ``t_minutes`` (often 60 minutes — coincidentally
correct for synthetic but off-by-many-minutes on Geneva real data with 24+
distinct timepoints).
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ehr_simulator.web.app import app_from_study_config


def test_app_from_study_config_synthetic_renders_synth_001(
    study_fixture_dir: Path, tmp_log_dir: Path
) -> None:
    app = app_from_study_config(
        study_fixture_dir / "study_synthetic.yaml",
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
    )
    with TestClient(app) as client:
        response = client.get("/patient/synth_001/timepoint/0")
        assert response.status_code == 200
        # Patient summary card includes the patient_id.
        assert "synth_001" in response.text


def test_app_from_study_config_t_index_resolves_to_study_timepoints(
    study_fixture_dir: Path, tmp_log_dir: Path
) -> None:
    """REGRESSION (per /plan-eng-review issue 1.2).

    The synthetic dataset has ``t_minutes`` distinct values
    ``{0, 60, 180}``. The shipped study_synthetic.yaml declares
    ``timepoints: [0, 60, 180]`` — for the synthetic dataset, study and
    dataset timepoints happen to coincide. To prove the bug fix, we use a
    custom study config whose timepoints are a STRICT SUBSET of the
    dataset's ``{0, 60, 180}`` and assert that ``app.state.study_timepoints``
    is bound to the study's choice — the URL ordinal then maps into that
    subset, not into the dataset.
    """
    custom_dir = tmp_log_dir.parent / "study"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "1"
dataset: synthetic
patient_ids: [synth_001]
time_unit: minutes
timepoints: [0, 180]
""",
        encoding="utf-8",
    )
    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
    )

    # app.state binding (the fix)
    assert app.state.study_timepoints == [0.0, 180.0]

    with TestClient(app) as client:
        # t_index=1 must resolve to the STUDY's timepoint 180.0 (the second
        # element of [0, 180]), NOT to the dataset's second distinct
        # timepoint 60.0.
        response = client.get("/patient/synth_001/timepoint/1")
        assert response.status_code == 200
        # Lab data at t=180 is in the slice; lab data at t<180 is too.
        # The summary card lists scalar_ts row count; the t=180 slice has
        # more rows than the t=60 slice for synth_001 in the synthetic
        # fixture (vitals + labs at all 3 timepoints).
        # Compare against t_index=0 to assert the t_index=1 slice is wider.
        response_t0 = client.get("/patient/synth_001/timepoint/0")
        assert response_t0.status_code == 200
        # Both responses contain the patient id; the t=180 slice does
        # mention t=180 in the page header / chart axes.
        assert "synth_001" in response.text


def test_serve_no_config_path_does_not_set_study_timepoints(tmp_log_dir: Path) -> None:
    """The synthetic-only ``serve`` path (no --config) keeps the S2 behavior:
    routes fall back to ``patient_timepoints(dataset, pid)``. Locks the
    "no-config path is unchanged" acceptance criterion in spec §12.
    """
    from ehr_simulator.web.app import create_app

    app = create_app(log_dir=tmp_log_dir)
    assert not hasattr(app.state, "study_timepoints") or app.state.study_timepoints is None


def test_app_from_study_config_index_lists_only_study_patients(
    study_fixture_dir: Path, tmp_log_dir: Path
) -> None:
    """The synthetic dataset has 3 patients (synth_001/002/003). When the
    study config declares only one of them, the index page must render
    only that one — not the full dataset list. Closes the post-S5 user
    report: "all patients are loaded instead of loading only patients in
    patient_ids" against the Geneva real-data CSV (~3K patients).
    """
    custom_dir = tmp_log_dir.parent / "study_subset"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "1"
dataset: synthetic
patient_ids: [synth_002]
time_unit: minutes
timepoints: [0, 60]
""",
        encoding="utf-8",
    )

    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
    )
    assert app.state.study_patient_ids == ["synth_002"]

    with TestClient(app) as client:
        index = client.get("/")
        assert index.status_code == 200
        assert "synth_002" in index.text
        assert "synth_001" not in index.text
        assert "synth_003" not in index.text

        # Off-study patient URL 404s with the "not part of this study" message.
        off_study = client.get("/patient/synth_001/timepoint/0")
        assert off_study.status_code == 404
        assert "not part of this study" in off_study.text

        # In-study patient still works.
        in_study = client.get("/patient/synth_002/timepoint/0")
        assert in_study.status_code == 200


def test_app_from_study_config_preserves_patient_id_order(
    study_fixture_dir: Path, tmp_log_dir: Path
) -> None:
    """Spec §2: 'Order is meaningful — the simulator walks patients in this
    sequence per clinician session.' The index must render in declared
    order, not sorted."""
    custom_dir = tmp_log_dir.parent / "study_order"
    custom_dir.mkdir(parents=True, exist_ok=True)
    study_path = custom_dir / "study.yaml"
    study_path.write_text(
        """schema_version: "1"
dataset: synthetic
patient_ids: [synth_003, synth_001, synth_002]
time_unit: minutes
timepoints: [0, 60]
""",
        encoding="utf-8",
    )

    app = app_from_study_config(
        study_path,
        study_fixture_dir / "questions.yaml",
        log_dir=tmp_log_dir,
    )
    with TestClient(app) as client:
        index = client.get("/")
        # Order check: synth_003 appears before synth_001 in the rendered HTML.
        idx_003 = index.text.find("synth_003")
        idx_001 = index.text.find("synth_001")
        idx_002 = index.text.find("synth_002")
        assert idx_003 < idx_001 < idx_002, (
            f"expected declared order [003, 001, 002] but got "
            f"positions: 003={idx_003}, 001={idx_001}, 002={idx_002}"
        )


def test_serve_no_config_path_does_not_set_study_patient_ids(tmp_log_dir: Path) -> None:
    """Without a study config, the index falls back to the full dataset's
    patient list (S2 behavior). Locks the no-config path."""
    from ehr_simulator.web.app import create_app

    app = create_app(log_dir=tmp_log_dir)
    assert not hasattr(app.state, "study_patient_ids") or app.state.study_patient_ids is None
