"""S8: Geneva shaped presentation through the real routes.

Boots the app on :func:`tests.support.geneva_ui.geneva_shaped_dataset`
(canonical frames as ``load_geneva`` produces them). Non study mode walks
the dataset timepoints ``0, 60, 120, 180, 240, 300``; ``LEAK_T`` (120) is
index 2.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from ehr_simulator.ingestion.canonical import CanonicalShape, empty_frame
from ehr_simulator.ingestion.mimic import MimicDataset
from ehr_simulator.web.app import create_app
from tests.conftest import _seed_clinician
from tests.support.geneva_ui import (
    PATIENT,
    SPARSE_PATIENT,
    geneva_shaped_dataset,
    stub_request,
)

LEAK_T_INDEX = 2


def _client(tmp_path: Path, loader: Callable[[], object]) -> Iterator[TestClient]:
    db_path = tmp_path / "ehr.db"
    clinician_id = _seed_clinician(db_path)
    app = create_app(
        log_dir=tmp_path / "logs",
        dataset_loader=loader,
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )
    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        yield client


@pytest.fixture
def geneva_client(tmp_path: Path) -> Iterator[TestClient]:
    yield from _client(tmp_path, geneva_shaped_dataset)


def _soup(client: TestClient, patient_id: str, t_index: int, chrome: str = "dense"):
    response = client.get(f"/patient/{patient_id}/timepoint/{t_index}?chrome={chrome}")
    assert response.status_code == 200, response.text
    return BeautifulSoup(response.text, "html.parser")


def _panel(soup: BeautifulSoup, panel: str):
    section = soup.select_one(f"section[data-panel='{panel}']")
    assert section is not None, panel
    return section


def test_geneva_fixture_renders_every_clinical_panel(geneva_client: TestClient) -> None:
    soup = _soup(geneva_client, PATIENT, LEAK_T_INDEX)
    for panel in ("vitals", "neuro_support", "labs", "admission", "imaging", "ai"):
        assert _panel(soup, panel)["data-state"] not in {"error", "empty-expected"}, panel

    vitals = _panel(soup, "vitals")
    assert vitals.select("svg[data-range='true'][data-variable='hr']")
    assert vitals.select_one("svg[data-variable='map']") is not None
    assert "(65.00–75.00)" in vitals.select_one("table.vitals-values").get_text()

    neuro = _panel(soup, "neuro_support")
    assert neuro.select_one("svg[data-range='true'][data-variable='nihss']") is not None
    assert {"GCS", "FiO₂"} <= {
        p.get_text(" ", strip=True).split(" (")[0] for p in neuro.select(".vitals-row-label")
    }
    fallback = neuro.select_one("table.a11y-fallback").get_text(" ", strip=True)
    assert "NIHSS 3.00 8.00 13.00" in fallback


def test_admission_sections_and_other(geneva_client: TestClient) -> None:
    admission = _panel(_soup(geneva_client, PATIENT, 0), "admission")
    sections = {
        group.h3.get_text(strip=True): [dt.get_text(strip=True) for dt in group.select("dt")]
        for group in admission.select(".admission-section")
    }
    assert list(sections) == [
        "Demographics",
        "Prestroke medication",
        "Medical history",
        "Stroke context",
        "Treatment / presentation",
        "Other",
    ]
    assert sections["Demographics"] == ["Age", "Sex", "Weight"]
    assert sections["Treatment / presentation"] == [
        "IAT timing",
        "IVT timing",
        "Onset to admission",
        "Referral",
    ]
    assert sections["Other"] == ["zz_unregistered"]
    assert "vascular_occlusion" not in admission.get_text()
    assert "Vascular occlusion" not in admission.get_text()


def test_sparse_labs_render_dashes_not_warnings(geneva_client: TestClient) -> None:
    labs = _panel(_soup(geneva_client, PATIENT, LEAK_T_INDEX), "labs")
    assert labs["data-state"] == "loading"
    assert labs.select_one(".flash-warn") is None
    rows = {
        tr.th.get_text(" ", strip=True).split(" (")[0]: [
            td.get_text(strip=True) for td in tr.select("td")
        ]
        for tr in labs.select("tbody tr")
    }
    # Registry order, observation time columns 0 and 120 only.
    assert list(rows) == ["Hematocrit", "Creatinine", "Glucose"]
    assert rows["Creatinine"] == ["88.00", "—"]
    current = labs.select("th[aria-current='time']")
    assert [th.get_text(" ", strip=True) for th in current] == ["t = 120 min"]
    assert labs.select_one(".table-scroll") is not None


def test_imaging_derived_values_prevent_empty_state(geneva_client: TestClient) -> None:
    imaging = _panel(_soup(geneva_client, PATIENT, 0), "imaging")
    assert imaging["data-state"] == "loading"
    assert "No imaging" not in imaging.get_text()
    perfusion = imaging.select_one("table.perfusion-table").get_text(" ", strip=True)
    assert "Tmax > 6 s" in perfusion and "23.50" in perfusion
    findings = {
        tr.th.get_text(strip=True): tr.td.get_text(strip=True)
        for tr in imaging.select("table.imaging-findings tr")
    }
    assert findings == {"Hypoperfusion with mismatch": "Yes", "Vascular occlusion": "No"}


def test_dataset_loader_called_once(tmp_path: Path) -> None:
    calls = []

    def loader():
        calls.append(1)
        return geneva_shaped_dataset()

    for client in _client(tmp_path, loader):
        for t_index in (0, 1, 2):
            _soup(client, PATIENT, t_index)
            _soup(client, SPARSE_PATIENT, t_index)
            client.get(f"/patient/{PATIENT}/timepoint/{t_index}", headers={"HX-Request": "true"})
    assert len(calls) == 1


def test_future_existence_is_not_clinician_facing(tmp_path: Path) -> None:
    """§9.2: labs only later vs never read the same, except ``data-state``."""

    def never():
        dataset = geneva_shaped_dataset()
        scalar = dataset.scalar_ts
        dataset.scalar_ts = scalar.loc[
            ~((scalar.patient_id == SPARSE_PATIENT) & (scalar.variable == "glucose"))
        ]
        return dataset

    pages = {}
    for name, loader in (("later", geneva_shaped_dataset), ("never", never)):
        for client in _client(tmp_path / name, loader):
            pages[name] = _panel(_soup(client, SPARSE_PATIENT, 0), "labs")

    later, never_ = pages["later"], pages["never"]
    assert (later["data-state"], never_["data-state"]) == ("empty-unexpected", "empty-expected")
    assert later["aria-label"] == never_["aria-label"] == "labs panel — empty"
    assert later.get_text(" ", strip=True) == never_.get_text(" ", strip=True)
    assert "up to this timepoint" in later.get_text()


def test_mimic_imaging_derived_scalars_render(tmp_path: Path) -> None:
    def mimic():
        scalar = pd.DataFrame(
            [
                {
                    "patient_id": "m1",
                    "t_minutes": 0.0,
                    "variable": "tmax_gt_6",
                    "value": 15.0,
                    "unit": None,
                    "source": "EHR",
                }
            ]
        )
        admission = pd.DataFrame([{"patient_id": "m1", "field": "age", "value": "66"}])
        return MimicDataset(
            scalar_ts=scalar,
            admission=admission,
            imaging=empty_frame(CanonicalShape.IMAGING),
            ai_output=empty_frame(CanonicalShape.AI_OUTPUT),
        )

    for client in _client(tmp_path, mimic):
        imaging = _panel(_soup(client, "m1", 0), "imaging")
    assert imaging["data-state"] == "loading"
    assert "Tmax > 6 s" in imaging.get_text()


def test_epic_tab_and_dense_section(geneva_client: TestClient) -> None:
    epic = _soup(geneva_client, PATIENT, 0, chrome="epic")
    tabs = [b["data-tab"] for b in epic.select("nav[role='tablist'] button")]
    assert tabs[:3] == ["admission", "vitals", "neuro_support"]
    tab = epic.select_one("button[data-tab='neuro_support']")
    assert tab.get_text(strip=True) == "Neuro / Support"
    assert epic.select_one("#tab-neuro_support section[data-panel='neuro_support']") is not None

    dense = _soup(geneva_client, PATIENT, 0, chrome="dense")
    order = [s["data-panel"] for s in dense.select("main > section[data-panel]")]
    assert order.index("neuro_support") == order.index("vitals") + 1


def test_summary_card_shows_geneva_sex(geneva_client: TestClient) -> None:
    meta = _soup(geneva_client, PATIENT, 0).select_one(".summary-meta").get_text(" ", strip=True)
    assert " ".join(meta.split()) == "age 71.5 · sex Male"


def test_synthetic_summary_sex_unchanged(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/patient/synth_001/timepoint/0").text, "html.parser")
    assert "sex ?" not in soup.select_one(".summary-meta").get_text(" ", strip=True)


# ---------------------------------------------------------------------------
# Geneva AI panel (§6) and the exposed field inventory (§15)
# ---------------------------------------------------------------------------


def test_legacy_geneva_ai_bounded_and_latest_row_only(geneva_client: TestClient) -> None:
    ai = _panel(_soup(geneva_client, PATIENT, LEAK_T_INDEX), "ai")
    assert ai["data-state"] == "loading"
    text = ai.get_text(" ", strip=True)
    assert "Predicted probability" in text and "32.0%" in text
    assert "fixture_xgb" in text

    # One contributors table (the latest row), ten rows, labelled effects.
    tables = ai.select("table.ai-contributors")
    assert len(tables) == 1
    rows = tables[0].select("tbody tr")
    assert len(rows) == 10
    assert rows[0].th.get_text(strip=True) == "Glucose (2 h earlier)"
    assert ai.select_one("tbody tr th code").get_text() == "unmapped_feature_x"
    effects = {tr.select("td")[0].get_text(strip=True) for tr in rows}
    assert effects == {"increases model output", "decreases model output"}

    # Earlier rows: probability history only.
    history = [
        [c.get_text(strip=True) for c in tr.select("th, td")]
        for tr in ai.select("table.ai-history tbody tr")
    ]
    assert history == [["0", "20.0%"], ["60", "26.0%"]]


def test_geneva_ai_html_is_not_the_shap_dictionary(geneva_client: TestClient) -> None:
    html = str(_panel(_soup(geneva_client, PATIENT, LEAK_T_INDEX), "ai"))
    for absent in ("base_value", "contributions", "std_glucose", "timestep_idx", "{'", "-0.64"):
        assert absent not in html, absent
    assert "Weight" not in html  # sixth negative contributor


@pytest.mark.parametrize(
    ("label", "heading"),
    [(None, "Predicted probability"), ("poor outcome", "Predicted probability of poor outcome")],
)
def test_measured_heading_uses_pinned_outcome_label(label: str | None, heading: str) -> None:
    from ehr_simulator.config.study import AIInterventionConfig
    from ehr_simulator.web.panel_render import _render_measured_ai
    from ehr_simulator.web.panels import InterventionContext, InterventionMode, slice_to_timepoint
    from tests.support.geneva_ui import MODEL_ID

    dataset = geneva_shaped_dataset()
    configured = AIInterventionConfig(
        model_id=MODEL_ID,
        model_system_version="c" * 64,
        prediction_artifact_sha256="a" * 64,
        explanation_artifact_sha256="b" * 64,
        presentation_version="ai_panel_v2",
        intervention_build_id="test",
        outcome_label=label,
    )
    ctx = InterventionContext(InterventionMode.AI, configured, dataset.ai_provenance)
    sliced = slice_to_timepoint(dataset, PATIENT, 60.0, 1)
    soup = BeautifulSoup(
        _render_measured_ai(sliced, stub_request(), intervention=ctx), "html.parser"
    )

    assert soup.h3.get_text(strip=True) == heading
    assert "26.0%" in soup.get_text()
    assert soup.select_one("table.ai-history") is None
    assert len(soup.select("table.ai-contributors tbody tr")) == 10


def test_exposed_field_ids_match_new_panels() -> None:
    from ehr_simulator.web.ai_payload import decode
    from ehr_simulator.web.panels import exposed_field_ids, slice_to_timepoint

    dataset = geneva_shaped_dataset()
    sliced = slice_to_timepoint(dataset, PATIENT, 0.0, 0)
    payload = decode(sliced.ai_output["output_json"].iloc[0])
    exposed = exposed_field_ids(sliced, [payload])

    for field in (
        "scalar:hr",
        "scalar:hr_min",
        "scalar:sbp_max",
        "scalar:map",
        "scalar:nihss_max",
        "scalar:gcs",
        "scalar:fio2",
        "scalar:hct",
        "scalar:tmax_gt_6",
        "admission:MedHist CHD",
        "admission:vascular_occlusion",
        "admission:zz_unregistered",
        "ai:probability",
        "ai:explanation.contributions.lag2_glucose",
        "ai:explanation.contributions.rolling_mean_FIO2",
    ):
        assert field in exposed, field
    for field in (
        "scalar:mystery_score",  # never rendered
        "ai:explanation.contributions.timestep_idx",
        "ai:explanation.contributions.weight",
        "ai:explanation.contributions.std_glucose",
        "ai:explanation.base_value",
        "scalar:crp",  # first measured after t
    ):
        assert field not in exposed, field


# ---------------------------------------------------------------------------
# Timepoint locality (§9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("chrome", ["dense", "epic"])
@pytest.mark.parametrize("t_index", [0, 1, LEAK_T_INDEX])
def test_no_future_value_reaches_any_surface(
    geneva_client: TestClient, chrome: str, t_index: int
) -> None:
    """DATA LEAK REGRESSION: tables, charts, fallbacks, AI and summary."""
    from tests.support.geneva_ui import FUTURE_FEATURE

    html = geneva_client.get(f"/patient/{PATIENT}/timepoint/{t_index}?chrome={chrome}").text
    for future in (
        "7777.",  # every scalar after LEAK_T (tables, chart labels, fallbacks)
        "86.4",  # AI probability after LEAK_T
        FUTURE_FEATURE,
        "proBNP",  # its label
        "CRP",  # lab first drawn at 240
        "CBV",  # perfusion volume at 300
        "t = 180 min",
        "t = 240 min",
        "t = 300 min",
    ):
        assert future not in html, (t_index, future)


def test_slice_never_holds_rows_after_t() -> None:
    from ehr_simulator.web.panels import slice_to_timepoint

    dataset = geneva_shaped_dataset()
    for t in (0.0, 60.0, 120.0):
        sliced = slice_to_timepoint(dataset, PATIENT, t, 0)
        for frame in (sliced.scalar_ts, sliced.imaging, sliced.ai_output):
            assert (frame["t_minutes"] <= t).all()


def test_study_timepoints_stay_authoritative(
    tmp_path: Path, study_fixture_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Six dataset timepoints, four study ones: the walk has four."""
    from ehr_simulator.config import load_study_config
    from ehr_simulator.web.app import app_from_study_config
    from tests.conftest import _activate_configuration

    study_yaml = study_fixture_dir / "study_geneva.yaml"
    questions_yaml = study_fixture_dir / "questions.yaml"
    db_path = tmp_path / "ehr.db"
    study = load_study_config(study_yaml)
    clinician_id = _seed_clinician(db_path, study_id=study.study_id)
    _activate_configuration(
        db_path, study_yaml, questions_yaml, version="v1", description="S8 geneva"
    )
    monkeypatch.setattr(
        "ehr_simulator.cli_support.build_dataset_loader",
        lambda *_a, **_k: geneva_shaped_dataset,
    )
    app = app_from_study_config(
        study_yaml,
        questions_yaml,
        log_dir=tmp_path / "logs",
        db_path=db_path,
        backup_dir=tmp_path / "backups",
    )
    with TestClient(app) as client:
        client.cookies.set("ehrsim_clinician_id", clinician_id)
        soup = _soup(client, PATIENT, 0)

    position = soup.select_one(".tp-position").get_text(" ", strip=True)
    assert " ".join(position.split()) == "t = 0 min (1/4)"
    assert _panel(soup, "vitals")["data-state"] == "loading"
