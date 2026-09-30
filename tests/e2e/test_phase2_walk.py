"""S11o browser walk: the shipped Phase 2 example, AI and no AI cases.

Runs against ``live_phase2_server`` (``configs/example_phase2_config.yaml`` +
first use case questions, telemetry on, so every case is tab guarded)::

    Start case ─► claim granted ─► S11h branch ─► answers ─► advance × 3
    Start case ─► the other arm ─► … ─► completed
    export-phase2 on the live DB ─► both arms, delivery, no AI surface
"""

from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path

import pytest
from playwright.sync_api import Page
from typer.testing import CliRunner

from ehr_simulator import cli

GRANTED = '#patient-view[data-tab-state="granted"]'
CAUSE_FORM = "#q-slot-primary_cause form"
DEATH_SLOT = "#q-slot-death_3mo"
EXAMPLE_STUDY = Path(__file__).parents[2] / "configs" / "example_phase2_config.yaml"
TIMEPOINTS = 3
POLL_TIMEOUT_S = 15.0
POLL_STEP_S = 0.25


def _choose(page: Page, question_id: str, value: str) -> None:
    selector = f'form[data-question-id="{question_id}"] input[value="{value}"]'
    with page.expect_response(lambda res: "/answer" in res.url):
        page.click(selector)


def _answer_timepoint(page: Page, *, branch: bool) -> None:
    if branch:
        _choose(page, "deterioration_6h", "Yes")
        page.wait_for_selector(CAUSE_FORM)
        _choose(page, "deterioration_6h", "No")
        page.wait_for_selector(CAUSE_FORM, state="detached")
    else:
        _choose(page, "deterioration_6h", "No")
    _choose(page, "confidence", "3")
    _choose(page, "good_outcome_3mo", "Yes")
    page.wait_for_selector(f'{DEATH_SLOT}[data-q-state="derived"]')
    page.wait_for_selector('#advance-form[data-remaining="0"]')


def _walk_case(page: Page, *, branch: bool) -> tuple[str, bool]:
    """One whole case; returns (patient_id, rendered an AI panel)."""
    page.click("button.case-start")
    page.wait_for_selector(GRANTED)
    patient_id = page.url.split("/patient/")[1].split("/")[0]
    shows_ai = page.locator('[data-panel="ai"]').count() > 0
    for t_index in range(TIMEPOINTS):
        _answer_timepoint(page, branch=branch and t_index == 0)
        old = page.get_attribute("#patient-view", "data-render-id")
        page.click("#advance-btn")
        if t_index < TIMEPOINTS - 1:
            page.wait_for_function(
                "(old) => document.querySelector('#patient-view').dataset.renderId !== old",
                arg=old,
            )
            page.wait_for_selector(GRANTED)
            assert (page.locator('[data-panel="ai"]').count() > 0) is shows_ai
    page.wait_for_url(lambda url: "/patient/" not in url, timeout=10_000)
    return patient_id, shows_ai


def _ai_mounts(db: Path, patient_id: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'panel.mount' AND patient_id = ? "
            'AND payload_json LIKE \'%"panel_id":"ai"%\'',
            (patient_id,),
        ).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.e2e
def test_ai_and_no_ai_cases_walk_and_export(
    page: Page, live_phase2_server: str, phase2_db: Path, tmp_path: Path
) -> None:
    page.goto(f"{live_phase2_server}/login")
    page.fill('input[name="clinician_name"]', "Dr. Phase Two")
    page.click('button[type="submit"]')
    page.wait_for_url(f"{live_phase2_server}/")

    first = _walk_case(page, branch=True)
    second = _walk_case(page, branch=False)
    assert {first[1], second[1]} == {True, False}  # block pattern: one of each arm
    ai_patient = first[0] if first[1] else second[0]

    deadline = time.monotonic() + POLL_TIMEOUT_S
    while _ai_mounts(phase2_db, ai_patient) < TIMEPOINTS:
        assert time.monotonic() < deadline, "AI panel mounts never arrived"
        time.sleep(POLL_STEP_S)

    out = tmp_path / "bundle"
    result = CliRunner().invoke(
        cli.app_typer,
        ["export-phase2", str(EXAMPLE_STUDY), "--db-path", str(phase2_db), "--out-dir", str(out)],
    )
    assert result.exit_code == 0, result.output
    with (out / "timepoints.csv").open(newline="") as fh:
        rows = [r for r in csv.DictReader(fh) if r["patient_id"] in (first[0], second[0])]
    by_arm = {r["arm"]: [] for r in rows}
    for r in rows:
        by_arm[r["arm"]].append(r)

    assert {r["lifecycle_state"] for r in rows} == {"completed"}
    assert {r["ai_delivered"] for r in by_arm["ai"]} == {"true"}
    assert {r["intervention_leakage"] for r in by_arm["no_ai"]} == {"false"}
    assert {r["pp_compliant"] for r in by_arm["no_ai"]} == {"true"}
    assert {r["tab_conflict_detected"] for r in rows} == {"false"}
    with (out / "answers.csv").open(newline="") as fh:
        cells = {(r["patient_id"], r["t_index"], r["question_id"]): r for r in csv.DictReader(fh)}
    cause = cells[(first[0], "0", "primary_cause")]
    assert (cause["branch_state"], cause["response_status"]) == ("hidden", "not_applicable")
    death = cells[(first[0], "0", "death_3mo")]
    assert (death["answer_source"], death["response_value"]) == ("rule", "No")
