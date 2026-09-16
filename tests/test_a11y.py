"""Accessibility baseline: every chart has an a11y-fallback table sibling."""

from __future__ import annotations

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tests.conftest import seed_progress


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


def test_every_question_control_has_label_and_legend(study_client: TestClient) -> None:
    """S9a questions pane: every control is labelled, every question is a
    fieldset with one legend, every badge is a live status region."""
    r = study_client.get("/patient/synth_001/timepoint/0")
    assert r.status_code == 200
    pane = BeautifulSoup(r.text, "html.parser").select_one("#questions-pane")
    assert pane is not None
    assert pane.get("aria-label")

    forms = pane.select("form.question")
    assert forms
    for form in forms:
        assert len(form.select("fieldset > legend")) == 1
        badge = form.select_one(".answer-status")
        assert badge is not None and badge.get("role") == "status"

        controls = [c for c in form.select("input, textarea") if c.get("type") != "hidden"]
        assert controls, f"no controls in {form['data-question-id']}"
        for control in controls:
            wrapped = control.find_parent("label") is not None
            referenced = bool(control.get("id")) and (
                pane.select_one(f'label[for="{control.get("id")}"]') is not None
            )
            assert wrapped or referenced, f"unlabelled control: {control}"


def _pane(client: TestClient, t_index: int) -> BeautifulSoup:
    r = client.get(f"/patient/synth_001/timepoint/{t_index}")
    assert r.status_code == 200
    return BeautifulSoup(r.text, "html.parser")


def _assert_labelled(pane: BeautifulSoup) -> None:
    for form in pane.select("form.question"):
        assert len(form.select("fieldset > legend")) == 1
        for control in (c for c in form.select("input, textarea") if c.get("type") != "hidden"):
            assert control.find_parent("label") is not None, control


def test_advance_cta_and_locked_pane_a11y(study_client: TestClient) -> None:
    """S9b: blocked CTA is aria-disabled + described; locked pane is a note
    with every control inside a disabled fieldset; both keep the S9a labels."""
    open_pane = _pane(study_client, 0).select_one("#questions-pane")
    btn = open_pane.select_one("#advance-btn")
    assert btn["aria-disabled"] == "true"
    hint = open_pane.select_one("#" + btn["aria-describedby"])
    assert hint is not None
    assert "6" in hint.get_text()
    _assert_labelled(open_pane)

    seed_progress(study_client, "synth_001", 1)
    locked_pane = _pane(study_client, 0).select_one("#questions-pane")
    note = locked_pane.select_one(".pane-lock-note")
    assert note["role"] == "note" and note.get_text(strip=True)
    controls = [c for c in locked_pane.select("input, textarea") if c.get("type") != "hidden"]
    assert controls
    for control in controls:
        fieldset = control.find_parent("fieldset")
        assert fieldset is not None and fieldset.has_attr("disabled"), control
    link = locked_pane.select_one(".resume-link")
    assert link.get_text(strip=True)
    _assert_labelled(locked_pane)
