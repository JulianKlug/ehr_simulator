"""S11g: AI/no AI intervention delivery, artifact provenance and preflight.

Route tests drive the real Phase 2 app on ``study_randomised.yaml`` (block
pattern start/other, block length 1), so the first two started cases hold
opposite arms.
"""

from __future__ import annotations

import hashlib
import json
import math
import warnings
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import yaml
from fastapi.testclient import TestClient

from ehr_simulator.cli_support import walk_preflight
from ehr_simulator.config import (
    ConfigError,
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
    parse_study_snapshot,
    render_study_snapshot,
)
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.ingestion import GenevaDataset, MimicDataset, load_synthetic
from ehr_simulator.ingestion.provenance import AIArtifactProvenance
from ehr_simulator.ingestion.synthetic import MODEL_SYSTEM_VERSION, ai_output_sha256
from ehr_simulator.web import panels
from ehr_simulator.web.app import app_from_study_config
from ehr_simulator.web.panels import (
    AIUnavailableReason,
    InterventionContext,
    InterventionMode,
    exposed_field_ids,
    select_measured_ai,
    slice_to_timepoint,
)
from tests.conftest import seed_progress
from tests.test_case_start import Harness, _start, _started_patient, harness  # noqa: F401

SYNTHETIC_SHA256 = "90c5448853d63e90b68310a26ba5bab110070a6334e94e0e07c64d6f2d245ca9"
OTHER_SHA256 = "0" * 64
AI_MARKERS = (
    'data-panel="ai"',
    'data-tab="ai"',
    "<header>AI output</header>",
    "badge-ai",
    "prob_",
    "demo_v0",
)
CLINICAL_MARKERS = ('data-panel="vitals"', 'data-panel="labs"', 'data-panel="admission"')
T_MINUTES = (0.0, 60.0, 180.0)
FIRST_T = 0
SECOND_T = 1
CHROMES = ("epic", "dense")


def _study_dict(study_fixture_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((study_fixture_dir / "study_randomised.yaml").read_text())


def _study(study_fixture_dir: Path, **changes: Any) -> StudyConfig:
    data = _study_dict(study_fixture_dir)
    data.update(changes)
    return StudyConfig.model_validate(data)


def _intervention(study_fixture_dir: Path, **changes: Any) -> dict[str, Any]:
    block = dict(_study_dict(study_fixture_dir)["ai_intervention"])
    block.update(changes)
    return block


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_valid_intervention_and_clinician_facing_load(study_fixture_dir: Path) -> None:
    study = load_study_config(study_fixture_dir / "study_randomised.yaml")
    assert study.ai_intervention is not None
    assert study.ai_intervention.prediction_artifact_sha256 == SYNTHETIC_SHA256
    assert study.clinician_facing is not None
    assert study.clinician_facing.prohibited_fields == []


@pytest.mark.parametrize("field", ["prediction_artifact_sha256", "explanation_artifact_sha256"])
@pytest.mark.parametrize("bad", ["ABC", "A" * 64, "g" * 64, "0" * 63])
def test_malformed_sha256_rejected(study_fixture_dir: Path, field: str, bad: str) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex"):
        _study(study_fixture_dir, ai_intervention=_intervention(study_fixture_dir, **{field: bad}))


@pytest.mark.parametrize(
    "field", ["model_id", "model_system_version", "presentation_version", "intervention_build_id"]
)
def test_blank_identifier_rejected(study_fixture_dir: Path, field: str) -> None:
    with pytest.raises(ValueError, match="non-blank"):
        _study(study_fixture_dir, ai_intervention=_intervention(study_fixture_dir, **{field: " "}))


@pytest.mark.parametrize(
    "fields",
    [["admission:x", "admission:x"], ["nope:x"], ["admission:"], ["admission:a b"], ["mrs"]],
)
def test_bad_prohibited_fields_rejected(study_fixture_dir: Path, fields: list[str]) -> None:
    with pytest.raises(ValueError, match="prohibited fields"):
        _study(study_fixture_dir, clinician_facing={"prohibited_fields": fields})


def test_absent_blocks_keep_historical_snapshot_bytes(study_fixture_dir: Path) -> None:
    study = _study(study_fixture_dir, ai_intervention=None, clinician_facing=None)
    rendered = render_study_snapshot(study)

    assert "ai_intervention" not in rendered
    assert "clinician_facing" not in rendered
    assert parse_study_snapshot(rendered) == study


def test_null_explanation_hash_is_omitted(study_fixture_dir: Path) -> None:
    rendered = render_study_snapshot(_study(study_fixture_dir))
    assert "explanation_artifact_sha256" not in rendered


def test_intervention_identity_changes_config_hash(study_fixture_dir: Path) -> None:
    questions = load_questions(study_fixture_dir / "questions.yaml")
    base = _study(study_fixture_dir)
    changed = _study(
        study_fixture_dir,
        ai_intervention=_intervention(study_fixture_dir, presentation_version="ai_panel_v2"),
    )
    assert compute_config_hash_from_models(base, questions) != compute_config_hash_from_models(
        changed, questions
    )


def test_yaml_randomisation_without_intervention_refused(
    study_fixture_dir: Path, tmp_path: Path
) -> None:
    data = _study_dict(study_fixture_dir)
    del data["ai_intervention"]
    path = tmp_path / "study.yaml"
    path.write_text(yaml.safe_dump(data))

    with pytest.raises(ConfigError, match="requires an ai_intervention"):
        load_study_config(path)

    # A stored snapshot without the block still parses.
    stored = render_study_snapshot(StudyConfig.model_validate(data))
    assert parse_study_snapshot(stored).ai_intervention is None


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_synthetic_provenance_matches_documented_canonical_form() -> None:
    dataset = load_synthetic()
    frame = dataset.ai_output.sort_values(["patient_id", "t_minutes", "model_id"])
    rows = [
        [r.patient_id, float(r.t_minutes), r.model_id, r.output_json]
        for r in frame.itertuples(index=False)
    ]
    expected = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert dataset.ai_provenance == AIArtifactProvenance(expected, None, MODEL_SYSTEM_VERSION)
    assert expected == SYNTHETIC_SHA256
    assert ai_output_sha256(load_synthetic().ai_output) == expected


def test_row_order_does_not_change_synthetic_hash() -> None:
    frame = load_synthetic().ai_output
    assert ai_output_sha256(frame.iloc[::-1]) == ai_output_sha256(frame)


@pytest.mark.parametrize("cls", [GenevaDataset, MimicDataset])
def test_real_adapters_expose_no_ai_provenance(cls: type) -> None:
    empty = pd.DataFrame()
    assert cls(empty, empty, empty, empty).ai_provenance is None


def _boot_refused(harness: Harness, config) -> bool:  # noqa: F811
    app = app_from_study_config(
        config.study_yaml,
        config.questions_yaml,
        log_dir=harness.tmp_path / "logs",
        db_path=harness.db_path,
        backup_dir=harness.tmp_path / "backups",
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            with TestClient(app):
                return False
        except BaseException:  # noqa: BLE001 — lifespan SystemExit(1) routed via the portal
            return True


def test_boot_refuses_prediction_hash_mismatch(harness: Harness) -> None:  # noqa: F811
    wrong = harness.variant(
        "v2",
        ai_intervention=_intervention(
            harness.v1.study_yaml.parent, prediction_artifact_sha256=OTHER_SHA256
        ),
    )
    harness.activate(wrong)
    assert _boot_refused(harness, wrong)


def test_boot_refuses_missing_provenance(
    harness: Harness,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ehr_simulator.web.app.ai_provenance_of", lambda _dataset: None)
    assert _boot_refused(harness, harness.v1)


def test_boot_accepts_matching_artifact(harness: Harness) -> None:  # noqa: F811
    assert not _boot_refused(harness, harness.v1)


# ---------------------------------------------------------------------------
# Arm aware rendering
# ---------------------------------------------------------------------------


def _cases_by_arm(harness: Harness, client: TestClient) -> dict[str, str]:  # noqa: F811
    """Start two cases (opposite arms under block length 1); ``{arm: patient}``."""
    first = _started_patient(_start(client))
    seed_progress(client, first, len(T_MINUTES) - 1, completed=True)
    second = _started_patient(_start(client))
    arms = {a.patient_id: a.arm for a in harness.assignments()}
    assert {arms[first], arms[second]} == {"ai", "no_ai"}
    return {arms[first]: first, arms[second]: second}


def _page(client: TestClient, patient_id: str, t_index: int = FIRST_T, chrome: str = "epic") -> str:
    response = client.get(f"/patient/{patient_id}/timepoint/{t_index}?chrome={chrome}")
    assert response.status_code == 200, response.text
    return response.text


@pytest.mark.parametrize("chrome", CHROMES)
def test_ai_case_renders_one_frozen_panel(harness: Harness, chrome: str) -> None:  # noqa: F811
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)
        html = _page(client, cases["ai"], chrome=chrome)

    assert html.count('data-panel="ai"') == 1
    assert 'data-intervention="ai"' in html
    assert "prob_deterioration_6h" in html
    assert "badge-ai" not in html


@pytest.mark.parametrize("chrome", CHROMES)
def test_no_ai_case_has_no_ai_surface(harness: Harness, chrome: str) -> None:  # noqa: F811
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)
        html = _page(client, cases["no_ai"], chrome=chrome)

    for marker in AI_MARKERS:
        assert marker not in html, marker


def test_no_ai_case_keeps_clinical_panels_questions_and_controls(
    harness: Harness,  # noqa: F811
) -> None:
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)
        no_ai = _page(client, cases["no_ai"])
        ai = _page(client, cases["ai"])

    for html in (no_ai, ai):
        for marker in (*CLINICAL_MARKERS, 'id="questions-pane"'):
            assert marker in html, marker


def test_ai_case_shows_exact_timepoint_row_only(harness: Harness) -> None:  # noqa: F811
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)
        seed_progress(client, cases["ai"], SECOND_T)
        html = _page(client, cases["ai"], t_index=SECOND_T)

    assert html.count('class="ai-time"') == 1
    assert 'class="ai-time">t=60.0' in html


def test_arm_ignores_query_parameters(harness: Harness) -> None:  # noqa: F811
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)
        response = client.get(f"/patient/{cases['no_ai']}/timepoint/0?arm=ai&intervention=ai")

    assert 'data-panel="ai"' not in response.text


def test_phase1_study_mode_keeps_legacy_ai_panel(study_client: TestClient) -> None:
    html = study_client.get("/patient/synth_001/timepoint/0").text
    assert 'data-panel="ai"' in html
    assert "badge-ai" in html
    assert 'data-intervention="ai"' not in html


def test_non_study_mode_keeps_legacy_ai_panel(client: TestClient) -> None:
    html = client.get("/patient/synth_001/timepoint/0").text
    assert 'data-panel="ai"' in html
    assert 'data-tab="ai"' in html


def test_older_case_keeps_pinned_intervention_after_new_activation(
    harness: Harness,  # noqa: F811
) -> None:
    with harness.boot() as client:
        cases = _cases_by_arm(harness, client)

    v2 = harness.variant(
        "v2",
        ai_intervention=_intervention(harness.v1.study_yaml.parent, presentation_version="v2"),
    )
    harness.activate(v2)
    with harness.boot(v2) as client:
        html = _page(client, cases["ai"])
    # Still rendered from v1's identity: the artifact matches, so it shows.
    assert 'data-state="unavailable"' not in html
    assert "prob_deterioration_6h" in html


# ---------------------------------------------------------------------------
# Measured AI selection (pure)
# ---------------------------------------------------------------------------


@pytest.fixture
def ai_context(study_fixture_dir: Path) -> InterventionContext:
    dataset = load_synthetic()
    study = load_study_config(study_fixture_dir / "study_randomised.yaml")
    return InterventionContext(InterventionMode.AI, study.ai_intervention, dataset.ai_provenance)


def _with_ai_rows(frame: pd.DataFrame) -> Any:
    dataset = load_synthetic()
    dataset.ai_output = frame.reset_index(drop=True)
    return dataset


def test_missing_current_row_is_unavailable_not_earlier(ai_context: InterventionContext) -> None:
    dataset = load_synthetic()
    frame = dataset.ai_output
    dataset = _with_ai_rows(
        frame.loc[~((frame.patient_id == "synth_001") & (frame.t_minutes == 60))]
    )
    sliced = slice_to_timepoint(dataset, "synth_001", 60.0, SECOND_T)

    measured = select_measured_ai(sliced, ai_context)
    assert measured.unavailable is AIUnavailableReason.MISSING_ROW
    assert measured.output_json is None


def test_future_rows_never_selected(ai_context: InterventionContext) -> None:
    sliced = slice_to_timepoint(load_synthetic(), "synth_001", 0.0, FIRST_T)
    assert select_measured_ai(sliced, ai_context).t_minutes == 0.0


def test_other_model_rows_never_selected(ai_context: InterventionContext) -> None:
    frame = load_synthetic().ai_output
    other = frame.copy()
    other["model_id"] = "other_model"
    dataset = _with_ai_rows(pd.concat([other, frame.loc[frame.t_minutes != 0]]))
    sliced = slice_to_timepoint(dataset, "synth_001", 0.0, FIRST_T)

    assert select_measured_ai(sliced, ai_context).unavailable is AIUnavailableReason.MISSING_ROW


def test_pinned_hash_differing_from_loaded_is_unavailable(
    ai_context: InterventionContext,
) -> None:
    loaded = AIArtifactProvenance(OTHER_SHA256, None, MODEL_SYSTEM_VERSION)
    ctx = InterventionContext(InterventionMode.AI, ai_context.intervention, loaded)
    sliced = slice_to_timepoint(load_synthetic(), "synth_001", 0.0, FIRST_T)

    assert select_measured_ai(sliced, ctx).unavailable is AIUnavailableReason.ARTIFACT_MISMATCH


def test_snapshot_without_intervention_is_unavailable() -> None:
    ctx = InterventionContext(InterventionMode.AI, None, load_synthetic().ai_provenance)
    sliced = slice_to_timepoint(load_synthetic(), "synth_001", 0.0, FIRST_T)
    assert select_measured_ai(sliced, ctx).unavailable is AIUnavailableReason.NOT_CONFIGURED


def test_exposed_field_ids_mirror_renderers() -> None:
    sliced = slice_to_timepoint(load_synthetic(), "synth_001", 0.0, FIRST_T)
    exposed = exposed_field_ids(sliced, ["prob_deterioration_6h"])

    assert "admission:nihss_admission" in exposed
    assert "scalar:hr" in exposed
    assert "imaging:report_text" in exposed
    assert "ai:prob_deterioration_6h" in exposed
    assert "ai:prob_mrs_0_2_90d" not in exposed


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _preflight(study: StudyConfig, dataset: Any = None) -> list[str]:
    questions_path = Path(__file__).parent / "fixtures" / "study" / "questions.yaml"
    report = walk_preflight(study, load_questions(questions_path), dataset or load_synthetic())
    return [r.message for r in report.rows if r.status == "FAIL"]


def test_matching_provenance_passes(study_fixture_dir: Path) -> None:
    assert _preflight(_study(study_fixture_dir)) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prediction_artifact_sha256", OTHER_SHA256),
        ("explanation_artifact_sha256", OTHER_SHA256),
        ("model_system_version", "other_version"),
    ],
)
def test_artifact_identity_mismatch_fails(study_fixture_dir: Path, field: str, value: str) -> None:
    study = _study(
        study_fixture_dir, ai_intervention=_intervention(study_fixture_dir, **{field: value})
    )
    failures = _preflight(study)
    assert any("AI artifact mismatch" in f for f in failures)


def test_missing_provenance_fails(study_fixture_dir: Path) -> None:
    dataset = load_synthetic()
    dataset.ai_provenance = None
    assert any(
        "no AI artifact provenance" in f for f in _preflight(_study(study_fixture_dir), dataset)
    )


def test_missing_ai_row_fails(study_fixture_dir: Path) -> None:
    frame = load_synthetic().ai_output
    dataset = _with_ai_rows(
        frame.loc[~((frame.patient_id == "synth_002") & (frame.t_minutes == 180))]
    )
    failures = _preflight(_study(study_fixture_dir), dataset)
    assert failures == ["patient synth_002 t=180: no AI row for model 'demo_v0'"]


def test_future_row_in_slice_fails_guard(
    study_fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_slice = panels.slice_to_timepoint

    def leaky_slice(dataset: Any, patient_id: str, t_minutes: float, timepoint_index: int):
        sliced = real_slice(dataset, patient_id, t_minutes, timepoint_index)
        full = dataset.scalar_ts.loc[dataset.scalar_ts.patient_id == patient_id]
        object.__setattr__(sliced, "scalar_ts", full.reset_index(drop=True))
        return sliced

    monkeypatch.setattr("ehr_simulator.cli_support.slice_to_timepoint", leaky_slice)
    failures = _preflight(_study(study_fixture_dir))
    assert any("scalar_ts row after the timepoint" in f for f in failures)


@pytest.mark.parametrize(
    "field", ["admission:nihss_admission", "scalar:hr", "ai:prob_deterioration_6h"]
)
def test_prohibited_field_exposed_fails(study_fixture_dir: Path, field: str) -> None:
    study = _study(study_fixture_dir, clinician_facing={"prohibited_fields": [field]})
    failures = _preflight(study)
    assert failures and all("prohibited clinician facing field" in f for f in failures)
    assert all(field in f for f in failures)


def test_never_rendered_scalar_variable_does_not_fail(study_fixture_dir: Path) -> None:
    dataset = load_synthetic()
    extra = dataset.scalar_ts.iloc[[0]].copy()
    extra["variable"] = "mrs_3mo"
    dataset.scalar_ts = pd.concat([dataset.scalar_ts, extra], ignore_index=True)
    study = _study(study_fixture_dir, clinician_facing={"prohibited_fields": ["scalar:mrs_3mo"]})

    assert _preflight(study, dataset) == []


def test_phase2_without_clinician_facing_fails(study_fixture_dir: Path) -> None:
    failures = _preflight(_study(study_fixture_dir, clinician_facing=None))
    assert any("declares no clinician_facing" in f for f in failures)


def test_study_level_rows_have_no_coordinates(study_fixture_dir: Path) -> None:
    questions = load_questions(study_fixture_dir / "questions.yaml")
    report = walk_preflight(
        _study(study_fixture_dir, clinician_facing=None), questions, load_synthetic()
    )
    study_rows = [r for r in report.rows if r.patient_id == "*"]
    assert study_rows and all(math.isnan(r.t_minutes) for r in study_rows)


def test_example_phase2_config_passes_preflight() -> None:
    root = Path(__file__).parent.parent / "configs"
    study = load_study_config(root / "example_phase2_config.yaml")
    report = walk_preflight(
        study, load_questions(root / "phase2_first_use_case_questions.yaml"), load_synthetic()
    )
    assert not report.has_fail
