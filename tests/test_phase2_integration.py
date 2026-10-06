"""S11o: Phase 2 end to end through public boundaries only.

One study (the example Phase 2 config + first use case questions, with
replacements on) is walked once per module by three clinicians, then every
scenario reads the result::

    X: AI case + no AI case, normal (A, B); X's first timepoint branches (G)
    Y: AI case whose AI panel never mounts (E) + no AI case that leaks (F)
    Z: case silent past grace → incomplete → replacement via Start case (C)
    then: export-phase2 (A–G, J), backup + export from the copy (I)

Scenario D (configuration update) builds its own DB. Scenario H (two tabs)
lives in ``tests/test_tab_guard.py`` and ``tests/e2e/test_multitab_walk.py``.
Scenario numbers refer to ``specs/session-11o-phase2-integration.md``.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ehr_simulator import cli
from ehr_simulator.db import connect
from ehr_simulator.db.backup import create_backup, read_identity
from ehr_simulator.db.connection import AccessMode
from ehr_simulator.db.migrations import MIGRATIONS
from ehr_simulator.export_phase2 import Phase2Bundle, build_phase2_bundle
from ehr_simulator.pseudonym import pseudonymize
from tests.support.cases import HX, _start, _started_patient
from tests.support.lifecycle import GRACE, LifecycleHarness, _harness
from tests.support.pseudonym import TEST_SECRET
from tests.support.tab_guard import TAB_A, _answer_all, _claim, _owner, _page, _post
from tests.support.telemetry import _event

# One worker builds the module-scoped walked study once (else once per worker).
pytestmark = pytest.mark.xdist_group("phase2_integration")

CONFIGS = Path(__file__).parents[1] / "configs"
HTTP_OK = 200
HTTP_NO_CONTENT = 204
TIMEPOINTS = 3
LAST_T_INDEX = 2
AI_MARKERS = ('data-panel="ai"', 'data-tab="ai"', "badge-ai", "demo_v0")


def _config(tmp: Path, **lifecycle: Any) -> Path:
    data = yaml.safe_load((CONFIGS / "example_phase2_config.yaml").read_text())
    data["case_lifecycle"].update({"replacement_cases_enabled": True, **lifecycle})
    path = tmp / "study_integration.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _new_study(tmp: Path) -> LifecycleHarness:
    return _harness(tmp, _config(tmp), CONFIGS / "example_phase2_questions.yaml")


@contextmanager
def _tab(h: LifecycleHarness, clinician_id: str | None = None) -> Iterator[TestClient]:
    with h.client(clinician_id=clinician_id) as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        yield client


def _view(response: Any) -> Any:
    return BeautifulSoup(response.text, "html.parser").select_one("#patient-view")


def _arm(h: LifecycleHarness, clinician_id: str, patient_id: str) -> str:
    with h.conn() as conn:
        return conn.execute(
            "SELECT arm FROM arm_assignments WHERE clinician_id = ? AND patient_id = ?",
            (clinician_id, patient_id),
        ).fetchone()[0]


def _telemetry(client: TestClient, render_id: str, *, ai_mount: bool) -> None:
    """Complete telemetry: vitals on screen; with ``ai_mount`` the AI panel too."""
    mounts = [("vitals", 2, 3)] + ([("ai", 4, 5)] if ai_mount else [])
    events = [_event(render_id, 1, "browser.timepoint_enter", client_mono_ms=0.0)]
    for panel, mount_seq, view_seq in mounts:
        events.append(
            _event(
                render_id,
                mount_seq,
                "panel.mount",
                client_mono_ms=50.0 * mount_seq,
                payload={
                    "panel_id": panel,
                    "expanded": True,
                    "collapsible": True,
                    "state": "loading",
                },
            )
        )
        events.append(
            _event(
                render_id,
                view_seq,
                "panel.viewport",
                client_mono_ms=50.0 * view_seq,
                payload={"panel_id": panel, "intersection_ratio": 1.0},
            )
        )
    events.append(_event(render_id, 9, "browser.timepoint_exit", client_mono_ms=5000.0))
    assert _post(client, TAB_A, events).status_code == HTTP_NO_CONTENT


@dataclass
class Walked:
    patient_id: str
    arm: str
    pages: list[str]  # the HTML of every primary view


def _walk(
    h: LifecycleHarness,
    client: TestClient,
    clinician_id: str,
    *,
    ai_mount: Any = None,
    stop_at: int | None = None,
    first_answers: Any = None,
) -> Walked:
    """Start, claim, report, answer and advance; ``ai_mount(arm) -> bool``."""
    patient_id = _started_patient(_start(client))
    arm = _arm(h, clinician_id, patient_id)
    response = client.get(f"/patient/{patient_id}/timepoint/0")
    pages = [response.text]
    render_id = _view(response)["data-render-id"]
    assert _claim(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT
    mount = ai_mount(arm) if ai_mount else arm == "ai"
    for t_index in range(TIMEPOINTS):
        _telemetry(client, render_id, ai_mount=mount)
        headers = {**HX, **_owner(TAB_A, render_id)}
        if t_index == 0 and first_answers is not None:
            first_answers(client, patient_id, headers)
        else:
            _answer_all(client, patient_id, t_index, headers)
        if stop_at == t_index:
            break
        response = client.post(
            f"/patient/{patient_id}/timepoint/{t_index}/advance", headers=headers
        )
        assert response.status_code == HTTP_OK
        if t_index < LAST_T_INDEX:
            pages.append(response.text)
            render_id = _view(response)["data-render-id"]
    return Walked(patient_id, arm, pages)


def _answer(client: TestClient, patient_id: str, headers: dict, qid: str, value: str) -> Any:
    response = client.post(
        f"/patient/{patient_id}/timepoint/0/answer",
        data={"question_id": qid, "value": value},
        headers=headers,
    )
    assert response.status_code == HTTP_OK, response.text
    return response


def _branching_answers(client: TestClient, patient_id: str, headers: dict) -> None:
    """G: cause saved under Yes, then deterioration flips to No; good outcome
    Yes derives death, then No makes death an explicit answer."""
    _answer(client, patient_id, headers, "deterioration_6h", "Yes")
    _answer(client, patient_id, headers, "primary_cause", "Other")
    _answer(client, patient_id, headers, "deterioration_6h", "No")
    _answer(client, patient_id, headers, "confidence", "3")
    _answer(client, patient_id, headers, "good_outcome_3mo", "Yes")
    _answer(client, patient_id, headers, "good_outcome_3mo", "No")
    _answer(client, patient_id, headers, "death_3mo", "Yes")


@dataclass
class Integrated:
    h: LifecycleHarness
    x: list[Walked]
    y: list[Walked]
    z_lost: Walked
    z_replacement: Walked
    ids: dict[str, str]


@pytest.fixture(scope="module")
def integrated(tmp_path_factory: pytest.TempPathFactory) -> Integrated:
    h = _new_study(tmp_path_factory.mktemp("integration"))
    y_id = h.add_clinician("Dr. Integration Y")
    z_id = h.add_clinician("Dr. Integration Z")

    with _tab(h) as client:
        x = [_walk(h, client, h.clinician_id, first_answers=_branching_answers)]
        x.append(_walk(h, client, h.clinician_id))
    with _tab(h, y_id) as client:
        # AI panel never mounts on the AI case; the no AI case reports one.
        y = [_walk(h, client, y_id, ai_mount=lambda arm: arm != "ai") for _ in range(2)]
    with _tab(h, z_id) as client:
        lost = _walk(h, client, z_id, stop_at=0)
        h.clock.advance(GRACE + 1)
        client.post(f"/case/{lost.patient_id}/heartbeat")
        replacement = _walk(h, client, z_id)
    # ids as exported: the bundle never carries DB clinician ids.
    ids = {
        role: pseudonymize(TEST_SECRET, cid)
        for role, cid in {"x": h.clinician_id, "y": y_id, "z": z_id}.items()
    }
    return Integrated(h, x, y, lost, replacement, ids)


def _bundle(db_path: Path, study_id: str) -> Phase2Bundle:
    conn = connect(db_path, access=AccessMode.READ_ONLY)
    try:
        return build_phase2_bundle(conn, study_id=study_id, pseudonym_secret=TEST_SECRET)
    finally:
        conn.close()


@pytest.fixture(scope="module")
def bundle(integrated: Integrated) -> Phase2Bundle:
    return _bundle(integrated.h.db_path, integrated.h.v1.study.study_id)


def _rows(bundle: Phase2Bundle, name: str, **match: str) -> list[dict[str, str]]:
    table = next(t for t in bundle.tables if t.name == name)
    rows = [dict(zip(table.header, row, strict=True)) for row in table.rows]
    return [r for r in rows if all(r[k] == v for k, v in match.items())]


def _by_arm(walks: list[Walked], arm: str) -> Walked:
    return next(w for w in walks if w.arm == arm)


# ---------------------------------------------------------------------------
# A, B: normal AI and no AI cases (tests #1-#4)
# ---------------------------------------------------------------------------


def test_a_ai_case_delivers_views_and_is_pp(integrated: Integrated, bundle: Phase2Bundle) -> None:
    ai = _by_arm(integrated.x, "ai")
    cid = integrated.ids["x"]

    for page in ai.pages:
        assert page.count('data-panel="ai"') == 1 and "demo_v0" in page
    audit = _rows(bundle, "randomisation_audit.csv", clinician_id=cid, patient_id=ai.patient_id)[0]
    assert audit["planned_arm"] == audit["realised_arm"] == "ai"
    assert audit["activation_config_version"] == "v1"
    rows = _rows(bundle, "timepoints.csv", clinician_id=cid, patient_id=ai.patient_id)
    assert [r["arm"] for r in rows] == ["ai"] * TIMEPOINTS
    for row in rows:
        assert (row["ai_delivered"], row["ai_viewed"], row["pp_compliant"]) == ("true",) * 3
        assert float(row["ai_qualifying_seconds"]) >= 2.0
        assert row["telemetry_status"] == "complete"


def test_b_no_ai_case_has_no_ai_surface(integrated: Integrated, bundle: Phase2Bundle) -> None:
    no_ai = _by_arm(integrated.x, "no_ai")
    cid = integrated.ids["x"]

    for page in no_ai.pages:
        assert not any(marker in page for marker in AI_MARKERS)
        assert 'data-panel="vitals"' in page and "questions-pane" in page
    rows = _rows(bundle, "timepoints.csv", clinician_id=cid, patient_id=no_ai.patient_id)
    assert {(r["intervention_leakage"], r["pp_compliant"]) for r in rows} == {("false", "true")}
    panels = _rows(bundle, "panel_summaries.csv", clinician_id=cid, patient_id=no_ai.patient_id)
    assert panels and "ai" not in {p["panel_id"] for p in panels}


def test_ab_share_questions_and_join_keys(integrated: Integrated, bundle: Phase2Bundle) -> None:
    ai, no_ai = _by_arm(integrated.x, "ai"), _by_arm(integrated.x, "no_ai")
    ids = {
        w.patient_id: {
            r["question_id"] for r in _rows(bundle, "answers.csv", patient_id=w.patient_id)
        }
        for w in (ai, no_ai)
    }
    assert ids[ai.patient_id] == ids[no_ai.patient_id]

    keys = ("study_id", "clinician_id", "patient_id", "t_index", "config_version", "config_hash")
    timepoints = {tuple(r[k] for k in keys) for r in _rows(bundle, "timepoints.csv")}
    for name in ("answers.csv", "panel_summaries.csv"):
        assert {tuple(r[k] for k in keys) for r in _rows(bundle, name)} <= timepoints


# ---------------------------------------------------------------------------
# C: incomplete case and replacement (tests #5-#9)
# ---------------------------------------------------------------------------


def test_c_incomplete_case_and_its_replacement(
    integrated: Integrated, bundle: Phase2Bundle
) -> None:
    cid = integrated.ids["z"]
    lost, repl = integrated.z_lost.patient_id, integrated.z_replacement.patient_id
    audit = {r["patient_id"]: r for r in _rows(bundle, "randomisation_audit.csv", clinician_id=cid)}

    assert lost != repl
    assert audit[lost]["lifecycle_state"] == "incomplete"
    assert audit[lost]["incomplete_reason"] == "reconnection_timeout"
    assert audit[lost]["replaced_by_patient_id"] == repl
    assert audit[repl]["replaces_patient_id"] == lost
    assert audit[repl]["lifecycle_state"] == "completed"
    # activated_at is SQLite's clock, replacement_activated_at the app clock
    # (a FakeClock here): both are set, their values differ only in tests.
    assert audit[repl]["replacement_activated_at"] and audit[repl]["activated_at"]
    assert _rows(bundle, "timepoints.csv", clinician_id=cid, patient_id=lost)
    kinds = [r["kind"] for r in _rows(bundle, "behavioral_events.csv", clinician_id=cid)]
    assert "case.replacement_planned" in kinds and kinds.count("case.activated") == 2


# ---------------------------------------------------------------------------
# E, F: intervention failure and leakage (tests #15-#16)
# ---------------------------------------------------------------------------


def test_e_ai_display_failure_keeps_itt(integrated: Integrated, bundle: Phase2Bundle) -> None:
    ai = _by_arm(integrated.y, "ai")
    rows = _rows(
        bundle, "timepoints.csv", clinician_id=integrated.ids["y"], patient_id=ai.patient_id
    )

    assert {r["arm"] for r in rows} == {"ai"}
    for row in rows:
        assert row["ai_delivered"] == "false"
        assert row["intervention_failure_reasons"] == "display_failure"
        assert row["pp_compliant"] == "false"


def test_f_no_ai_leakage_keeps_itt(integrated: Integrated, bundle: Phase2Bundle) -> None:
    no_ai = _by_arm(integrated.y, "no_ai")
    cid = integrated.ids["y"]
    rows = _rows(bundle, "timepoints.csv", clinician_id=cid, patient_id=no_ai.patient_id)

    assert {r["arm"] for r in rows} == {"no_ai"}
    assert {(r["intervention_leakage"], r["pp_compliant"]) for r in rows} == {("true", "false")}
    audit = _rows(bundle, "randomisation_audit.csv", clinician_id=cid, patient_id=no_ai.patient_id)
    assert audit[0]["planned_arm"] == audit[0]["realised_arm"] == "no_ai"
    ai_panels = _rows(
        bundle, "panel_summaries.csv", clinician_id=cid, patient_id=no_ai.patient_id, panel_id="ai"
    )
    assert ai_panels  # the leaked panel is exported, not dropped


# ---------------------------------------------------------------------------
# G: conditional questions (tests #18-#21)
# ---------------------------------------------------------------------------


def test_g_branch_changes_export_their_provenance(
    integrated: Integrated, bundle: Phase2Bundle
) -> None:
    first = integrated.x[0]
    cells = {
        (r["t_index"], r["question_id"]): r
        for r in _rows(bundle, "answers.csv", patient_id=first.patient_id)
    }

    cause = cells[("0", "primary_cause")]
    assert (cause["branch_state"], cause["response_status"]) == ("hidden", "not_applicable")
    assert cause["missing_reason"] == ""
    death = cells[("0", "death_3mo")]
    assert (death["answer_source"], death["response_value"]) == ("clinician", "Yes")
    later = cells[("1", "death_3mo")]  # _answer_all answers good outcome Yes
    assert (later["branch_state"], later["answer_source"], later["response_value"]) == (
        "derived",
        "rule",
        "No",
    )
    assert cells[("1", "primary_cause")]["response_status"] == "answered"


def test_g_resume_keeps_the_branch(integrated: Integrated) -> None:
    first = integrated.x[0]
    with _tab(integrated.h) as client:
        html = client.get(f"/patient/{first.patient_id}/timepoint/0").text
    slot = BeautifulSoup(html, "html.parser").select_one("#q-slot-primary_cause")

    assert slot is not None and not slot.find("form")


# ---------------------------------------------------------------------------
# I: backup identity and restore smoke (tests #31-#32)
# ---------------------------------------------------------------------------


def test_i_backup_is_attributable_and_exports_the_same(
    integrated: Integrated, bundle: Phase2Bundle, tmp_path: Path
) -> None:
    h = integrated.h
    study_id = h.v1.study.study_id
    # Fresh snapshot: other scenarios (e.g. G's resume GET) may have written since.
    source = _bundle(h.db_path, study_id)
    dest = create_backup(h.db_path, tmp_path / "backups", expected_study_id=study_id)

    assert dest.parent == tmp_path / "backups" / study_id
    assert dest.name.startswith(f"study_{study_id}_schema_{len(MIGRATIONS)}_")
    copy = connect(dest, access=AccessMode.READ_ONLY)
    try:
        identity = read_identity(copy)
    finally:
        copy.close()
    assert (identity.study_id, identity.schema_version) == (study_id, len(MIGRATIONS))
    assert _bundle(dest, study_id).tables == source.tables


def test_shared_timelines_export_the_same_bundle(
    integrated: Integrated, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One timeline per render, shared across derivations, exports exactly
    what per-derivation timelines did — with fewer timeline builds."""
    from ehr_simulator import behavioral_timing

    h = integrated.h
    study_id = h.v1.study.study_id
    builds: list[str] = []
    build_timeline = behavioral_timing.build_timeline

    def counting(render: Any, rows: Any) -> Any:
        builds.append(render.render_id)
        return build_timeline(render, rows)

    monkeypatch.setattr(behavioral_timing, "build_timeline", counting)
    shared = _bundle(h.db_path, study_id)
    shared_builds = len(builds)

    # None: every derivation builds its own timelines (the unshared path).
    builds.clear()
    monkeypatch.setattr("ehr_simulator.export_phase2.cases.build_timelines", lambda *_: None)
    unshared = _bundle(h.db_path, study_id)

    assert unshared.tables == shared.tables
    assert 0 < shared_builds < len(builds)


# ---------------------------------------------------------------------------
# J: privacy and keyfile separation (tests #28-#30)
# ---------------------------------------------------------------------------


def test_j_no_names_in_events_or_bundle(integrated: Integrated, bundle: Phase2Bundle) -> None:
    with integrated.h.conn() as conn:
        payloads = [json.loads(r[0]) for r in conn.execute("SELECT payload_json FROM events")]
    assert payloads and not any("name_normalized" in json.dumps(p) for p in payloads)
    text = json.dumps([t.rows for t in bundle.tables])
    assert "Integration" not in text and "name_normalized" not in text


def test_j_keyfile_is_separate_and_protected(integrated: Integrated, tmp_path: Path) -> None:
    h = integrated.h
    out, key = tmp_path / "bundle", tmp_path / "keys.csv"
    result = CliRunner().invoke(
        cli.app_typer,
        [
            "export-phase2",
            str(h.v1.study_yaml),
            "--db-path",
            str(h.db_path),
            "--out-dir",
            str(out),
            "--keyfile",
            str(key),
            "--pseudonym-secret",
            str(tmp_path / "pseudonym.secret"),
        ],
    )

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert len(key.read_text().splitlines()) == 1 + 3  # header + X, Y, Z
    assert not any("dr. integration" in p.read_text() for p in out.iterdir())


# ---------------------------------------------------------------------------
# D: configuration update while a case exists (tests #10-#14)
# ---------------------------------------------------------------------------


def test_d_configuration_update_keeps_old_cases_pinned(tmp_path: Path) -> None:
    h = _new_study(tmp_path)
    with _tab(h) as client:
        first = _walk(h, client, h.clinician_id, stop_at=0)

    data = yaml.safe_load(h.v1.study_yaml.read_text())
    data["case_lifecycle"]["study_target_completed_cases"] = 40  # display only
    v2 = h.variant("v2", case_lifecycle=data["case_lifecycle"])
    h.activate(v2)
    with h.client(v2) as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        render_id = _page(client, first.patient_id)
        _claim(client, first.patient_id, TAB_A, render_id)
        headers = {**HX, **_owner(TAB_A, render_id)}
        for t_index in range(TIMEPOINTS):
            _answer_all(client, first.patient_id, t_index, headers)
            response = client.post(
                f"/patient/{first.patient_id}/timepoint/{t_index}/advance", headers=headers
            )
            if t_index < LAST_T_INDEX:
                headers = {**HX, **_owner(TAB_A, _view(response)["data-render-id"])}
        second = _walk(h, client, h.clinician_id, stop_at=0)

    bundle = _bundle(h.db_path, h.v1.study.study_id)
    versions = {(r["patient_id"], r["config_version"]) for r in _rows(bundle, "timepoints.csv")}
    assert versions == {(first.patient_id, "v1"), (second.patient_id, "v2")}
    answers_v = {
        r["config_version"] for r in _rows(bundle, "answers.csv", patient_id=first.patient_id)
    }
    assert answers_v == {"v1"}
    counts = {r["config_version"]: r for r in _rows(bundle, "configuration_counts.csv")}
    assert (counts["v1"]["activated_cases"], counts["v2"]["activated_cases"]) == ("1", "1")
    assert counts["v1"]["completed_cases"] == "1"


# ---------------------------------------------------------------------------
# Examples (tests #33-#34)
# ---------------------------------------------------------------------------


def test_example_phase2_config_validates_and_preflights() -> None:
    runner = CliRunner()
    study, questions = (
        CONFIGS / "example_phase2_config.yaml",
        CONFIGS / "example_phase2_questions.yaml",
    )
    validated = runner.invoke(cli.app_typer, ["validate-config", str(study), str(questions)])
    preflight = runner.invoke(cli.app_typer, ["preflight", str(study), str(questions)])

    assert validated.exit_code == 0, validated.output
    assert preflight.exit_code == 0, preflight.output
    assert " 0 FAIL" in preflight.output
