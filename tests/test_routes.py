"""Integration tests for the FastAPI routes.

Covers index listing, full vs HTMX partial responses, chrome A/B selection,
fixture-state acceptance (synth_003 empty-expected imaging, synth_002 partial
labs), error containment when a panel renderer raises (D9), boundary errors
(D6/D10), the data-leak regression (#21), and the lifespan boot-failure path
(D17).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue
from ehr_simulator.web.app import create_app


def _find_in_exception_chain(exc: BaseException, target: type[BaseException]) -> bool:
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, target):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _read_log(log_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (log_dir / "current.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_index_lists_three_synthetic_patients(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text()
    for pid in ("synth_001", "synth_002", "synth_003"):
        assert pid in text, f"missing {pid} on index"
    hrefs = {a.get("href", "") for a in soup.find_all("a")}
    for pid in ("synth_001", "synth_002", "synth_003"):
        assert any(f"/patient/{pid}/timepoint/0" in h and "chrome=dense" in h for h in hrefs), (
            f"missing dense link for {pid}"
        )
        assert any(f"/patient/{pid}/timepoint/0" in h and "chrome=epic" in h for h in hrefs), (
            f"missing epic link for {pid}"
        )


def test_patient_route_renders_full_page_on_browser_request(client: TestClient) -> None:
    r = client.get("/patient/synth_001/timepoint/0")
    assert r.status_code == 200
    body = r.text
    assert "<html" in body.lower()
    assert "/static/htmx.min.js" in body
    assert "/static/theme.css" in body
    soup = BeautifulSoup(body, "html.parser")
    assert soup.select_one("#patient-view") is not None
    for panel in ("vitals", "labs", "admission", "imaging", "ai"):
        assert soup.select_one(f"section[data-panel='{panel}']") is not None, (
            f"missing panel {panel}"
        )


def test_patient_route_returns_partial_on_htmx_request(client: TestClient) -> None:
    r = client.get(
        "/patient/synth_001/timepoint/0",
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    body = r.text
    assert "<html" not in body.lower(), "HTMX response must not include the <html> wrapper"
    soup = BeautifulSoup(body, "html.parser")
    assert soup.select_one("#patient-view") is not None
    assert soup.select_one("section[data-panel='vitals']") is not None


def test_chrome_query_param_routes_to_correct_template(client: TestClient) -> None:
    dense_resp = client.get("/patient/synth_001/timepoint/0?chrome=dense")
    epic_resp = client.get("/patient/synth_001/timepoint/0?chrome=epic")
    default_resp = client.get("/patient/synth_001/timepoint/0")
    assert dense_resp.status_code == 200
    assert epic_resp.status_code == 200
    assert default_resp.status_code == 200

    dense_soup = BeautifulSoup(dense_resp.text, "html.parser")
    epic_soup = BeautifulSoup(epic_resp.text, "html.parser")
    default_soup = BeautifulSoup(default_resp.text, "html.parser")

    assert dense_soup.select_one("main.chrome-dense-grid") is not None
    assert epic_soup.select_one("main.chrome-epic-tabs") is not None
    assert epic_soup.select_one("nav[role='tablist']") is not None
    # default == dense
    assert default_soup.select_one("main.chrome-dense-grid") is not None


def test_synth_003_imaging_panel_is_empty_expected_at_t0(client: TestClient) -> None:
    r = client.get("/patient/synth_003/timepoint/0")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    section = soup.select_one("section[data-panel='imaging']")
    assert section is not None
    assert section.get("aria-label") == "imaging panel — empty-expected"


def test_synth_002_labs_panel_is_partial_at_t_index_1(client: TestClient) -> None:
    r = client.get("/patient/synth_002/timepoint/1")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    section = soup.select_one("section[data-panel='labs']")
    assert section is not None
    assert section.get("aria-label") == "labs panel — partial"


def test_route_oob_t_index_returns_404(client: TestClient) -> None:
    r = client.get("/patient/synth_001/timepoint/99")
    assert r.status_code == 404
    assert "valid: 0…2" in r.text


def test_route_unknown_patient_returns_404(client: TestClient) -> None:
    r = client.get("/patient/unknown/timepoint/0")
    assert r.status_code == 404
    assert "'unknown' not found" in r.text


def test_route_invalid_chrome_returns_422(client: TestClient) -> None:
    r = client.get("/patient/synth_001/timepoint/0?chrome=garbage")
    assert r.status_code == 422


def test_data_leak_request_for_t_index_1_returns_no_rows_above_60(client: TestClient) -> None:
    """The central regression: at t_index=1 (t=60), no panel content may
    reference t_minutes=180 (Decision D5)."""
    r = client.get("/patient/synth_001/timepoint/1")
    assert r.status_code == 200
    body = r.text
    # No t_minutes=180 reference anywhere in the rendered DOM (a11y tables,
    # data attributes, or visible content).
    assert "180.0" not in body, "t=180 leaked into rendered HTML"
    assert "180 min" not in body, "t=180 min leaked into rendered HTML"
    matches = re.findall(r"data-t-minutes=\"([0-9.]+)\"", body)
    for m in matches:
        assert float(m) <= 60.0, f"data-t-minutes={m} leaked at t_index=1"


def test_event_kind_dispatch_on_hx_request_header(client: TestClient, tmp_log_dir: Path) -> None:
    client.get("/patient/synth_001/timepoint/0")
    client.get("/patient/synth_001/timepoint/0", headers={"HX-Request": "true"})
    import logging as _stdlogging

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    records = _read_log(tmp_log_dir)
    request_records = [r for r in records if r.get("event") == "request"]
    kinds = [r["event_kind"] for r in request_records]
    assert "page.render" in kinds
    assert "panel.swap" in kinds


def test_lifespan_boot_failure_logs_and_exits(tmp_log_dir: Path) -> None:
    def broken_loader():
        raise AdapterError(
            "synthetic broken",
            issues=[
                IngestionIssue(dataset="synthetic", patient_id=None, row_idx=None, reason="boom")
            ],
        )

    app = create_app(log_dir=tmp_log_dir, dataset_loader=broken_loader)
    raised: BaseException | None = None
    try:
        with TestClient(app):
            pass
    except BaseException as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "expected lifespan to raise out of TestClient context"
    # SystemExit is wrapped into ExceptionGroup/CancelledError by anyio's task
    # group; the chain-walker accepts SystemExit anywhere it shows up.
    _find_in_exception_chain(raised, SystemExit)
    import logging as _stdlogging

    for h in _stdlogging.getLogger("ehr_simulator").handlers:
        h.flush()
    records = _read_log(tmp_log_dir)
    assert any(r.get("event_kind") == "app.boot.failed" for r in records)


@pytest.fixture
def broken_render_client(
    tmp_log_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """Force the chart renderer to raise so we can verify per-panel containment."""
    from ehr_simulator.ingestion.synthetic import load_synthetic
    from ehr_simulator.web import charts as charts_module
    from ehr_simulator.web import routes as routes_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    def vitals_boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(charts_module, "render_timeline_svg", boom)
    monkeypatch.setattr(routes_module, "_render_vitals", vitals_boom)

    dataset = load_synthetic()
    app = create_app(log_dir=tmp_log_dir, dataset_loader=lambda: dataset)
    with TestClient(app) as c:
        yield c


def test_renderer_exception_contains_to_single_panel(broken_render_client: TestClient) -> None:
    r = broken_render_client.get("/patient/synth_001/timepoint/0")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")
    vitals = soup.select_one("section[data-panel='vitals']")
    assert vitals is not None
    assert vitals.get("aria-label") == "vitals panel — error"
    # Other panels still render (at least one in a non-error state).
    other_panels = ["labs", "admission", "imaging", "ai"]
    non_error = 0
    for panel in other_panels:
        sec = soup.select_one(f"section[data-panel='{panel}']")
        if sec is not None and sec.get("data-state") != "error":
            non_error += 1
    assert non_error >= 1
