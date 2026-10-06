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
