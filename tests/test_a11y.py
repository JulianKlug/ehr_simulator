"""Accessibility baseline: every chart has an a11y-fallback table sibling."""

from __future__ import annotations

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient


def test_a11y_fallback_table_present_for_every_chart(client: TestClient) -> None:
    r = client.get("/patient/synth_001/timepoint/2")
    assert r.status_code == 200
    soup = BeautifulSoup(r.text, "html.parser")

    figures = soup.select("figure.chart")
    assert figures, "expected at least one chart figure on synth_001/t=180"
    for fig in figures:
        svg = fig.find("svg")
        table = fig.find("table", class_="a11y-fallback")
        assert svg is not None, f"missing svg in chart figure: {fig}"
        assert table is not None, f"missing a11y-fallback table for chart: {fig}"
        cells = table.select("tbody td")
        assert any(cell.get_text(strip=True) for cell in cells), (
            "a11y-fallback table must contain at least one numeric cell"
        )
