"""S11n: the linked Phase 2 export bundle.

Walks real cases through the guarded Phase 2 app (``study_lifecycle`` +
``telemetry``), then exports the DB::

    Start case ─► claim ─► telemetry + answers ─► advance … ─► completed
    Start case ─► answer ─► silent past grace ─► incomplete ─► replacement
    build_phase2_bundle(read only conn) ─► tables ─► write_bundle(dir)

Test numbers refer to ``specs/session-11n-phase2-exports.md``.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ehr_simulator import cli, export_phase2
from ehr_simulator.behavioral_timing import derive_observation_timings
from ehr_simulator.db import connect
from ehr_simulator.db.connection import AccessMode
from ehr_simulator.db.migrations import MIGRATIONS
from ehr_simulator.db.telemetry import RenderRow, TelemetryRow
from ehr_simulator.export_bundle import (
    MANIFEST_NAME,
    BundleWriteError,
    Overwrite,
    write_bundle,
)
from ehr_simulator.export_phase2 import (
    KeyfileRequest,
    Phase2ExportError,
    PracticeExport,
    build_phase2_bundle,
)
from ehr_simulator.panel_exposure import derive_panel_summaries
from ehr_simulator.pseudonym import pseudonymize
from ehr_simulator.study_variables_reader import load_case_variables
from tests.conftest import drop_append_only_triggers
from tests.support.cases import HX, _start, _started_patient
from tests.support.lifecycle import GRACE, LifecycleHarness, _harness
from tests.support.pseudonym import TEST_SECRET
from tests.support.tab_guard import (
    TAB_A,
    TAB_B,
    _answer_all,
    _claim,
    _owner,
    _page,
    _post,
    _tab,
)
from tests.support.telemetry import TELEMETRY, _event

FIXTURES = Path(__file__).parent / "fixtures" / "study"
HTTP_NO_CONTENT = 204
LAST_T_INDEX = 2
TIMEPOINTS = 3
FREE_TEXT_QUESTION = {
    "question_id": "notes",
    "prompt": "Notes",
    "response_type": "free-text",
    "required": False,
}


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _study(tmp_path: Path, fixture_dir: Path, name: str, **changes: Any) -> LifecycleHarness:
    data = yaml.safe_load((fixture_dir / name).read_text())
    data["telemetry"] = TELEMETRY
    data.update(changes)
    path = tmp_path / "study_export.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, fixture_dir / "questions.yaml")


@pytest.fixture
def xh(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    return _study(tmp_path, study_fixture_dir, "study_lifecycle_replacement.yaml")


def _view_render(response: Any) -> str:
    view = BeautifulSoup(response.text, "html.parser").select_one("#patient-view")
    return view["data-render-id"]


def _report(client: TestClient, render_id: str) -> None:
    """Complete telemetry: vitals fully on screen for 4.8 s, AI panel mounted."""
    events = [
        _event(render_id, 1, "browser.timepoint_enter", client_mono_ms=0.0),
        _event(
            render_id,
            2,
            "panel.mount",
            client_mono_ms=100.0,
            payload={
                "panel_id": "vitals",
                "expanded": True,
                "collapsible": True,
                "state": "loading",
            },
        ),
        _event(
            render_id,
            3,
            "panel.viewport",
            client_mono_ms=200.0,
            payload={"panel_id": "vitals", "intersection_ratio": 1.0},
        ),
        _event(render_id, 4, "browser.timepoint_exit", client_mono_ms=5000.0),
    ]
    assert _post(client, TAB_A, events).status_code == HTTP_NO_CONTENT


def _walk(
    client: TestClient,
    *,
    stop_at: int | None = None,
    patient_id: str | None = None,
    start_t: int = 0,
) -> str:
    """Start (or continue) a case and walk it; ``stop_at`` leaves it open there."""
    patient_id = patient_id or _started_patient(_start(client))
    render_id = _page(client, patient_id, start_t)
    assert _claim(client, patient_id, TAB_A, render_id).status_code == HTTP_NO_CONTENT
    for t_index in range(start_t, TIMEPOINTS):
        _report(client, render_id)
        headers = {**HX, **_owner(TAB_A, render_id)}
        _answer_all(client, patient_id, t_index, headers)
        if stop_at == t_index:
            return patient_id
        response = client.post(
            f"/patient/{patient_id}/timepoint/{t_index}/advance", headers=headers
        )
        if t_index < LAST_T_INDEX:
            render_id = _view_render(response)
    return patient_id


def _abandon_by_timeout(xh: LifecycleHarness, client: TestClient, patient_id: str) -> None:
    xh.clock.advance(GRACE + 1)
    client.post(f"/case/{patient_id}/heartbeat")


def _bundle(xh: LifecycleHarness, **kwargs: Any) -> export_phase2.Phase2Bundle:
    conn = connect(xh.db_path, access=AccessMode.READ_ONLY)
    try:
        kwargs.setdefault("pseudonym_secret", TEST_SECRET)
        return build_phase2_bundle(conn, study_id=xh.v1.study.study_id, **kwargs)
    finally:
        conn.close()


def _rows(bundle: export_phase2.Phase2Bundle, name: str, **match: str) -> list[dict[str, str]]:
    table = next(t for t in bundle.tables if t.name == name)
    rows = [dict(zip(table.header, row, strict=True)) for row in table.rows]
    return [r for r in rows if all(r[k] == v for k, v in match.items())]


def _sql(xh: LifecycleHarness, sql: str, params: tuple = ()) -> None:
    with xh.conn() as conn:
        drop_append_only_triggers(conn)
        conn.execute(sql, params)
        conn.commit()


@pytest.fixture(scope="session")
def walked_template(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, str]]:
    """Walk once per session: one completed case, one incomplete case, and its
    activated replacement (open). Tests get a copy of the resulting DB."""
    tmp = tmp_path_factory.mktemp("walked")
    template = _study(tmp, FIXTURES, "study_lifecycle_replacement.yaml")
    with _tab(template) as client:
        done = _walk(client)
        lost = _walk(client, stop_at=0)
        _abandon_by_timeout(template, client, lost)
        replacement = _walk(client, stop_at=0)
    return template.db_path, {"done": done, "lost": lost, "replacement": replacement}


@pytest.fixture
def walked(xh: LifecycleHarness, walked_template: tuple[Path, dict[str, str]]) -> dict[str, str]:
    db_path, cases = walked_template
    for suffix in ("-wal", "-shm"):
        Path(f"{xh.db_path}{suffix}").unlink(missing_ok=True)
    shutil.copyfile(db_path, xh.db_path)
    return cases


# ---------------------------------------------------------------------------
# Snapshot and provenance (#1-#10)
# ---------------------------------------------------------------------------


def test_one_version_exports_every_file(xh: LifecycleHarness, walked: dict) -> None:  # 1
    bundle = _bundle(xh)

    assert [t.name for t in bundle.tables] == [
        "timepoints.csv",
        "answers.csv",
        "panel_summaries.csv",
        "behavioral_events.csv",
        "randomisation_audit.csv",
        "configuration_history.csv",
        "configuration_counts.csv",
        "clinicians.csv",
    ]
    assert bundle.config_versions == ("v1",)
    assert bundle.source_schema_version == len(MIGRATIONS)


def _mixed(xh: LifecycleHarness) -> dict[str, str]:
    """Case A starts under v1, v2 is activated, A finishes, B starts under v2."""
    with _tab(xh) as client:
        first = _walk(client, stop_at=0)
    data = yaml.safe_load(xh.v1.study_yaml.read_text())
    data["case_lifecycle"]["study_target_completed_cases"] = 5  # display only
    v2 = xh.variant("v2", case_lifecycle=data["case_lifecycle"])
    xh.activate(v2)
    with xh.client(v2) as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        _walk(client, patient_id=first)
        second = _walk(client, stop_at=0)
    return {"v1": first, "v2": second}


def test_mixed_versions_export_with_row_provenance(xh: LifecycleHarness) -> None:  # 2, 3, 4
    cases = _mixed(xh)
    bundle = _bundle(xh)

    versions = {(r["patient_id"], r["config_version"]) for r in _rows(bundle, "timepoints.csv")}
    assert versions == {(cases["v1"], "v1"), (cases["v2"], "v2")}
    audit = {r["patient_id"]: r for r in _rows(bundle, "randomisation_audit.csv")}
    assert audit[cases["v2"]]["generation_config_version"] == "v1"
    assert audit[cases["v2"]]["activation_config_version"] == "v2"
    assert bundle.config_versions == ("v1", "v2")


def test_legacy_export_refuses_mixed_and_names_export_phase2(xh: LifecycleHarness) -> None:  # 60
    _mixed(xh)
    result = CliRunner().invoke(
        cli.app_typer,
        [
            "export-answers",
            str(xh.v1.study_yaml),
            str(xh.v1.questions_yaml),
            "--db-path",
            str(xh.db_path),
            "--out",
            str(xh.tmp_path / "legacy.csv"),
            *_secret_args(xh),
        ],
    )
    assert result.exit_code == 1
    assert "export-phase2" in result.stderr


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("UPDATE sessions SET config_version = NULL", "no config_version"),  # 5
        ("UPDATE sessions SET config_version = 'v9'", "activated under"),  # 6
        ("UPDATE randomisation_schedules SET config_hash = 'bad'", "registered as"),  # 7
        ("UPDATE configuration_history SET study_json = '{'", "no longer parses"),  # 8
        ("UPDATE progress SET config_hash = 'bad'", "activated under"),
    ],
)
def test_provenance_failures_refuse(
    xh: LifecycleHarness, walked: dict, sql: str, message: str
) -> None:
    _sql(xh, sql)
    with pytest.raises(Phase2ExportError, match=message):
        _bundle(xh)


def test_unknown_session_version_refuses(xh: LifecycleHarness, walked: dict) -> None:  # 6
    _sql(xh, "UPDATE randomisation_schedules SET config_version = 'v9'")
    with pytest.raises(Phase2ExportError, match="unknown config_version 'v9'"):
        _bundle(xh)


def test_foreign_study_refuses(xh: LifecycleHarness, walked: dict) -> None:  # 9
    conn = connect(xh.db_path, access=AccessMode.READ_ONLY)
    try:
        with pytest.raises(Phase2ExportError, match="belongs to study"):
            build_phase2_bundle(conn, study_id="another_study", pseudonym_secret=TEST_SECRET)
    finally:
        conn.close()


def test_snapshot_ignores_concurrent_commit(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:  # 10
    from ehr_simulator import timing

    original = timing.fetch_timing_events

    def write_then_read(conn: Any) -> Any:
        _sql(
            xh,
            "INSERT INTO events (clinician_id, patient_id, kind, payload_json) "
            "VALUES (?, ?, 'case.reconnected', '{}')",
            (xh.clinician_id, walked["done"]),
        )
        return original(conn)

    monkeypatch.setattr(timing, "fetch_timing_events", write_then_read)
    bundle = _bundle(xh)
    kinds = [r["kind"] for r in _rows(bundle, "behavioral_events.csv")]
    assert "case.reconnected" not in kinds


# ---------------------------------------------------------------------------
# Timepoints and answers (#11-#23)
# ---------------------------------------------------------------------------


def test_every_case_and_timepoint_appears(xh: LifecycleHarness, walked: dict) -> None:  # 11, 12
    rows = _rows(_bundle(xh), "timepoints.csv")
    by_case = {pid: [r for r in rows if r["patient_id"] == pid] for pid in walked.values()}

    assert all(len(v) == TIMEPOINTS for v in by_case.values())
    lost = by_case[walked["lost"]]
    assert lost[0]["lifecycle_state"] == "incomplete"
    assert lost[0]["incomplete_reason"] == "reconnection_timeout"
    assert lost[0]["timepoint_reached"] == "true"
    assert [r["timepoint_reached"] for r in lost[1:]] == ["false", "false"]
    assert lost[2]["timepoint_started_at"] == lost[2]["elapsed_seconds"] == ""
    assert lost[2]["telemetry_status"] == "missing"


def test_timepoint_values_match_the_pure_derivations(
    xh: LifecycleHarness, walked: dict
) -> None:  # 13-16
    rows = [r for r in _rows(_bundle(xh), "timepoints.csv") if r["patient_id"] == walked["done"]]
    conn = connect(xh.db_path, access=AccessMode.READ_ONLY)
    try:
        variables = load_case_variables(conn, xh.clinician_id, walked["done"])
    finally:
        conn.close()
    assert variables is not None

    for row, obs in zip(rows, variables.observations, strict=True):
        assert row["telemetry_status"] == "complete"
        assert float(row["foreground_seconds"]) == 5.0
        assert float(row["active_seconds"]) == 5.0
        assert row["elapsed_seconds"] != ""
        assert row["ai_delivered"] == (
            "" if obs.ai_delivered is None else str(obs.ai_delivered).lower()
        )
        assert row["pp_compliant"] == (
            "" if obs.pp_compliant is None else str(obs.pp_compliant).lower()
        )
        assert row["arm"] == variables.arm


def test_answer_statuses(xh: LifecycleHarness, walked: dict) -> None:  # 17, 18, 20
    rows = _rows(_bundle(xh), "answers.csv")
    lost = [r for r in rows if r["patient_id"] == walked["lost"]]

    answered_t0 = [r for r in lost if r["t_index"] == "0" and r["response_status"] == "answered"]
    assert answered_t0 and all(r["response_value"] for r in answered_t0)
    later = [r for r in lost if r["t_index"] != "0" and r["response_status"] == "missing"]
    assert later and {r["missing_reason"] for r in later} == {"case_abandoned"}
    assert {r["branch_state"] for r in rows} <= {"editable", "derived", "hidden"}
    hidden = [r for r in rows if r["branch_state"] == "hidden"]
    assert all(r["response_status"] == "not_applicable" and not r["missing_reason"] for r in hidden)


def test_integrity_warning_is_exported_not_refused(
    xh: LifecycleHarness, walked: dict
) -> None:  # 23
    _sql(xh, "UPDATE answers SET arm = CASE arm WHEN 'ai' THEN 'no_ai' ELSE 'ai' END")
    rows = _rows(_bundle(xh), "timepoints.csv")
    assert any("answer arm differs" in r["integrity_warnings"] for r in rows)


def test_undecodable_answer_refuses(xh: LifecycleHarness, walked: dict) -> None:  # 22
    _sql(xh, "UPDATE answers SET value = 'not an option'")
    with pytest.raises(Phase2ExportError, match="invalid persisted answer"):
        _bundle(xh)


def test_answer_at_unknown_question_refuses(xh: LifecycleHarness, walked: dict) -> None:
    _sql(
        xh,
        "UPDATE answers SET question_id = 'ghost' WHERE rowid = (SELECT MIN(rowid) FROM answers)",
    )
    with pytest.raises(Phase2ExportError, match="question the pinned config lacks"):
        _bundle(xh)


# ---------------------------------------------------------------------------
# Free text and privacy (#24-#30)
# ---------------------------------------------------------------------------


def _free_text_study(tmp_path: Path, fixture_dir: Path, routine: str) -> LifecycleHarness:
    questions = yaml.safe_load((fixture_dir / "questions.yaml").read_text())
    questions["questions"].append(FREE_TEXT_QUESTION)
    q_path = tmp_path / "questions_free.yaml"
    q_path.write_text(yaml.safe_dump(questions))
    data = yaml.safe_load((fixture_dir / "study_lifecycle.yaml").read_text())
    data["telemetry"] = TELEMETRY
    data["study_behaviour"] = {
        "backward_navigation": "allow_readonly",
        "free_text": {"enabled": True, "routine_export": routine},
    }
    path = tmp_path / "study_free.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, q_path)


@pytest.mark.parametrize(("routine", "exported"), [("exclude", False), ("include_explicit", True)])
def test_free_text_follows_pinned_policy(
    tmp_path: Path, study_fixture_dir: Path, routine: str, exported: bool
) -> None:  # 24-26
    fh = _free_text_study(tmp_path, study_fixture_dir, routine)
    with _tab(fh) as client:
        patient_id = _started_patient(_start(client))
        render_id = _page(client, patient_id)
        _claim(client, patient_id, TAB_A, render_id)
        client.post(
            f"/patient/{patient_id}/timepoint/0/answer",
            data={"question_id": "notes", "value": "=secret note"},
            headers={**HX, **_owner(TAB_A, render_id)},
        )
    bundle = _bundle(fh)
    cell = next(
        r
        for r in _rows(bundle, "answers.csv")
        if r["question_id"] == "notes" and r["t_index"] == "0"
    )

    assert cell["response_status"] == "answered"
    assert cell["value_exported"] == str(exported).lower()
    assert (cell["response_value"] == "=secret note") is exported
    everything_else = [t for t in bundle.tables if t.name != "answers.csv"]
    assert all("secret note" not in str(t.rows) for t in everything_else)


def test_bundle_has_no_names(xh: LifecycleHarness, walked: dict) -> None:  # 27
    with _tab(xh) as client:
        client.post("/login", data={"clinician_name": "Dr. Secret"}, follow_redirects=False)
    _sql(
        xh,
        "INSERT INTO events (clinician_id, kind, payload_json) "
        "VALUES (?, 'clinician.login', '{\"name_normalized\":\"dr. secret\"}')",
        (xh.clinician_id,),
    )
    bundle = _bundle(xh)
    text = "".join(str(t.rows) for t in bundle.tables)

    assert "name_normalized" not in text and "secret" not in text
    assert bundle.keyfile_rows is None


def test_keyfile_only_on_request_outside_bundle(
    xh: LifecycleHarness, walked: dict
) -> None:  # 28-30
    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    out = xh.tmp_path / "bundle"

    with pytest.raises(BundleWriteError, match="outside --out-dir"):
        write_bundle(bundle, out, keyfile=out / "key.csv")
    assert not out.exists()

    key = xh.tmp_path / "key.csv"
    write_bundle(bundle, out, keyfile=key)
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert key.read_text().splitlines()[0] == "clinician_id,name_normalized"
    assert not any("name_normalized" in p.read_text() for p in out.iterdir())


# ---------------------------------------------------------------------------
# Panels and events (#31-#37)
# ---------------------------------------------------------------------------


def _parse_events(rows: list[dict[str, str]]) -> tuple[list[RenderRow], list[TelemetryRow]]:
    renders, browser = [], []
    for r in rows:
        payload = json.loads(r["payload_json"])
        if r["kind"] == "timepoint.render":
            renders.append(
                RenderRow(
                    event_id=int(r["event_id"]),
                    render_id=r["render_id"],
                    session_id=r["session_id"] or None,
                    clinician_id=r["clinician_id"],
                    patient_id=r["patient_id"],
                    timepoint=float(r["timepoint"]),
                    payload=payload,
                )
            )
        elif r["kind"].startswith(("browser.", "panel.")):
            browser.append(
                TelemetryRow(
                    event_id=int(r["event_id"]),
                    render_id=r["render_id"],
                    tab_id=r["tab_id"],
                    kind=r["kind"],
                    client_seq=int(r["client_seq"]),
                    client_mono_ms=float(r["client_mono_ms"]),
                    client_ts=r["client_ts"] or None,
                    payload=payload,
                )
            )
    return renders, browser


def test_raw_events_reproduce_panels_and_timing(
    xh: LifecycleHarness, walked: dict
) -> None:  # 31, 35
    bundle = _bundle(xh)
    events = [
        r for r in _rows(bundle, "behavioral_events.csv") if r["patient_id"] == walked["done"]
    ]
    renders, browser = _parse_events(events)
    summaries = derive_panel_summaries(
        renders,
        browser,
        viewport_threshold=TELEMETRY["panel_viewport_threshold"],
        viewed_threshold_seconds=TELEMETRY["panel_viewed_threshold_seconds"],
    )
    timings = derive_observation_timings(
        renders, browser, inactivity_threshold_seconds=TELEMETRY["inactivity_threshold_seconds"]
    )

    panels = [r for r in _rows(bundle, "panel_summaries.csv") if r["patient_id"] == walked["done"]]
    assert panels
    for row in panels:
        summary = summaries[(int(row["t_index"]), row["visit_kind"], row["panel_id"])]
        assert row["qualifying_seconds"] == repr(float(summary.qualifying_seconds))
        assert row["viewed"] == str(summary.viewed).lower()
        assert int(row["episode_count"]) == summary.episode_count
    vitals = [r for r in panels if r["panel_id"] == "vitals"]
    assert {r["viewed"] for r in vitals} == {"true"}
    for row in [r for r in _rows(bundle, "timepoints.csv") if r["patient_id"] == walked["done"]]:
        timing_ = timings[(int(row["t_index"]), "primary")]
        assert float(row["foreground_seconds"]) == timing_.foreground_seconds
    assert all(r["config_version"] == "v1" and r["study_id"] for r in events)
    assert {r["tab_id"] for r in events if r["kind"].startswith("browser.")} == {TAB_A}


def test_no_ai_case_has_no_ai_panel_row(xh: LifecycleHarness, walked: dict) -> None:  # 37
    bundle = _bundle(xh)
    arms = {r["patient_id"]: r["arm"] for r in _rows(bundle, "timepoints.csv")}
    for row in _rows(bundle, "panel_summaries.csv"):
        if row["panel_id"] == "ai":
            assert arms[row["patient_id"]] == "ai"


def test_conflict_flag_next_to_complete_owner(xh: LifecycleHarness) -> None:  # 33, 34
    with _tab(xh) as client:
        patient_id = _walk(client, stop_at=0)
        _claim(client, patient_id, TAB_B, _page(client, patient_id))
    row = next(
        r
        for r in _rows(_bundle(xh), "timepoints.csv")
        if r["patient_id"] == patient_id and r["t_index"] == "0"
    )
    assert row["tab_conflict_detected"] == "true"
    assert row["telemetry_status"] == "complete"


def test_event_of_another_case_session_refuses(xh: LifecycleHarness, walked: dict) -> None:  # 36
    with xh.conn() as conn:
        other = conn.execute(
            "SELECT session_id FROM sessions WHERE patient_id = ? LIMIT 1", (walked["lost"],)
        ).fetchone()[0]
        drop_append_only_triggers(conn)
        conn.execute(
            "UPDATE events SET session_id = ? WHERE event_id = "
            "(SELECT MIN(event_id) FROM events WHERE patient_id = ? AND kind = 'answer.upsert')",
            (other, walked["done"]),
        )
        conn.commit()
    with pytest.raises(Phase2ExportError, match="session of another case"):
        _bundle(xh)


def test_excluded_event_families(xh: LifecycleHarness, walked: dict) -> None:
    kinds = {r["kind"] for r in _rows(_bundle(xh), "behavioral_events.csv")}
    assert not any(k.startswith(("clinician.", "progress.", "practice.")) for k in kinds)
    assert {"timepoint.render", "tab.claimed", "case.activated", "answer.upsert"} <= kinds


# ---------------------------------------------------------------------------
# Randomisation audit (#38-#44)
# ---------------------------------------------------------------------------


def test_audit_planned_and_realised(xh: LifecycleHarness, walked: dict) -> None:  # 38-43
    bundle = _bundle(xh)
    audit = _rows(bundle, "randomisation_audit.csv")
    with xh.conn() as conn:
        planned = conn.execute(
            "SELECT case_position, patient_id, planned_arm FROM randomisation_schedule_items "
            "ORDER BY case_position"
        ).fetchall()

    assert [(int(r["case_position"]), r["patient_id"], r["planned_arm"]) for r in audit] == [
        tuple(p) for p in planned
    ]
    by_patient = {r["patient_id"]: r for r in audit}
    for pid in walked.values():
        row = by_patient[pid]
        assert row["activated"] == "true"
        assert row["realised_arm"] == row["planned_arm"]
        assert row["activation_config_version"] == "v1" and row["activated_at"]
    lost, repl = by_patient[walked["lost"]], by_patient[walked["replacement"]]
    assert lost["lifecycle_state"] == "incomplete"
    assert lost["replaced_by_patient_id"] == walked["replacement"]
    assert repl["replaces_patient_id"] == walked["lost"]
    assert lost["replaced_by_replacement_id"] == repl["replacement_id"] != ""
    assert lost["algorithm_version"] and json.loads(lost["allocation_state_json"]) is not None


def test_planned_vs_realised_mismatch_refuses(xh: LifecycleHarness, walked: dict) -> None:  # 41
    _sql(
        xh,
        "UPDATE randomisation_schedule_items SET planned_arm = "
        "CASE planned_arm WHEN 'ai' THEN 'no_ai' ELSE 'ai' END WHERE patient_id = ?",
        (walked["done"],),
    )
    with pytest.raises(Phase2ExportError, match="realised an arm other than the planned"):
        _bundle(xh)


def test_exported_inputs_regenerate_the_schedule(xh: LifecycleHarness, walked: dict) -> None:  # 44
    from ehr_simulator.config.snapshot import parse_study_snapshot
    from ehr_simulator.randomisation import (
        ActivatedAllocationState,
        StartingArmCounts,
        generate_schedule,
    )

    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    audit = _rows(bundle, "randomisation_audit.csv")
    history = {r["config_version"]: r for r in _rows(bundle, "configuration_history.csv")}
    first = audit[0]
    # The keyfile (pseudonym → name) is the only way back to the DB id.
    name = dict(bundle.keyfile_rows or ())[first["clinician_id"]]
    db_id = hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]
    study = parse_study_snapshot(history[first["generation_config_version"]]["study_json"])
    state = json.loads(first["allocation_state_json"])
    regenerated = generate_schedule(
        study=study,
        config_version=first["generation_config_version"],
        config_hash=first["generation_config_hash"],
        clinician_id=db_id,
        allocation_state=ActivatedAllocationState.from_counts(
            {e["patient_id"]: (e["ai_count"], e["no_ai_count"]) for e in state}
        ),
        starting_arm_counts=StartingArmCounts(
            ai=int(first["starting_ai_count"]), no_ai=int(first["starting_no_ai_count"])
        ),
    )
    assert pseudonymize(TEST_SECRET, regenerated.schedule_id) == first["schedule_id"]
    assert [(i.patient_id, i.planned_arm) for i in regenerated.items] == [
        (r["patient_id"], r["planned_arm"]) for r in audit
    ]


# ---------------------------------------------------------------------------
# Configuration (#45-#47)
# ---------------------------------------------------------------------------


def test_history_and_counts(xh: LifecycleHarness, walked: dict) -> None:  # 45-47
    bundle = _bundle(xh)
    history = _rows(bundle, "configuration_history.csv")
    counts = _rows(bundle, "configuration_counts.csv")
    timepoints = _rows(bundle, "timepoints.csv")
    answers = _rows(bundle, "answers.csv")

    assert [h["config_version"] for h in history] == ["v1"]
    assert history[0]["change_description"] == "activate v1" and history[0]["study_json"]
    row = counts[0]
    assert int(row["activated_cases"]) == 3
    assert (row["completed_cases"], row["incomplete_cases"], row["open_cases"]) == ("1", "1", "1")
    assert int(row["expected_timepoints"]) == len(timepoints)
    assert int(row["reached_primary_timepoints"]) == sum(
        r["timepoint_reached"] == "true" for r in timepoints
    )
    assert int(row["answer_rows_present"]) == sum(
        r["response_status"] == "answered" for r in answers
    )
    assert int(row["scheduled_items_generated"]) == 3


# ---------------------------------------------------------------------------
# Practice (#48-#51)
# ---------------------------------------------------------------------------


@pytest.fixture
def ph(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    return _practice_study(tmp_path, study_fixture_dir, "exclude")


def _practice_study(tmp_path: Path, study_fixture_dir: Path, routine: str) -> LifecycleHarness:
    data = yaml.safe_load((study_fixture_dir / "study_lifecycle.yaml").read_text())
    data["telemetry"] = TELEMETRY
    data["patient_ids"] = ["synth_001", "synth_002"]
    data["case_lifecycle"]["max_activated_cases_per_clinician"] = 2
    data["study_behaviour"] = {
        "backward_navigation": "allow_readonly",
        "practice": {"enabled": True, "patient_ids": ["synth_003"], "arm": "no_ai"},
        "free_text": {"enabled": True, "routine_export": routine},
    }
    path = tmp_path / "study_practice.yaml"
    path.write_text(yaml.safe_dump(data))
    return _harness(tmp_path, path, study_fixture_dir / "questions.yaml")


def test_practice_stays_out_unless_included(ph: LifecycleHarness) -> None:  # 48-50
    with _tab(ph) as client:
        client.post("/practice/start", follow_redirects=False)
        _answer_all(client, "synth_003", 0, HX)
        _walk(client, stop_at=0)

    default = _bundle(ph)
    # Only the configuration snapshots name the practice patient.
    data_tables = [t for t in default.tables if t.name != "configuration_history.csv"]
    assert not any("synth_003" in str(t.rows) for t in data_tables)
    assert default.practice_included is False

    included = _bundle(ph, practice_export=PracticeExport.INCLUDE)
    names = [t.name for t in included.tables]
    assert names[-2:] == ["practice_timepoints.csv", "practice_answers.csv"]
    practice_rows = _rows(included, "practice_timepoints.csv")
    assert {r["patient_id"] for r in practice_rows} == {"synth_003"}
    assert {r["observation_mode"] for r in practice_rows} == {"practice"}
    measured = [
        t
        for t in included.tables
        if not t.name.startswith("practice_") and t.name != "configuration_history.csv"
    ]
    assert not any("synth_003" in str(t.rows) for t in measured)
    answered = [
        r for r in _rows(included, "practice_answers.csv") if r["response_status"] == "answered"
    ]
    assert answered and all(r["t_index"] == "0" for r in answered)


# ---------------------------------------------------------------------------
# Output (#52-#59)
# ---------------------------------------------------------------------------


def _cli(*args: str) -> Any:
    return CliRunner().invoke(cli.app_typer, ["export-phase2", *args])


def _secret_args(xh: LifecycleHarness) -> list[str]:
    return ["--pseudonym-secret", str(xh.tmp_path / "pseudonym.secret")]


def test_cli_writes_bundle_and_manifest(xh: LifecycleHarness, walked: dict) -> None:  # 55-57
    out = xh.tmp_path / "bundle"
    result = _cli(
        str(xh.v1.study_yaml),
        "--db-path",
        str(xh.db_path),
        "--out-dir",
        str(out),
        *_secret_args(xh),
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads((out / MANIFEST_NAME).read_text())
    assert manifest["export_schema_version"] == "phase2_export_v1"
    assert manifest["study_id"] == xh.v1.study.study_id
    assert manifest["practice_included"] is False
    assert set(manifest["files"]) == {p.name for p in out.iterdir()} - {MANIFEST_NAME}
    for name, entry in manifest["files"].items():
        content = (out / name).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert len(list(csv.reader(io.StringIO(content.decode())))) - 1 == entry["rows"]
    assert "Dr" not in (out / MANIFEST_NAME).read_text()


def test_existing_destination_and_force(xh: LifecycleHarness, walked: dict) -> None:  # 53, 54
    out = xh.tmp_path / "bundle"
    base = [
        str(xh.v1.study_yaml),
        "--db-path",
        str(xh.db_path),
        "--out-dir",
        str(out),
        *_secret_args(xh),
    ]
    assert _cli(*base).exit_code == 0
    before = (out / "timepoints.csv").read_bytes()

    assert _cli(*base).exit_code == 1
    _sql(xh, "UPDATE answers SET value = 'broken'")
    assert _cli(*base, "--force").exit_code == 1
    assert (out / "timepoints.csv").read_bytes() == before
    assert not [p for p in xh.tmp_path.iterdir() if p.name.startswith(".bundle.")]


def test_validation_failure_leaves_nothing(xh: LifecycleHarness, walked: dict) -> None:  # 52
    _sql(xh, "UPDATE answers SET value = 'broken'")
    out = xh.tmp_path / "bundle"
    result = _cli(
        str(xh.v1.study_yaml),
        "--db-path",
        str(xh.db_path),
        "--out-dir",
        str(out),
        *_secret_args(xh),
    )

    assert result.exit_code == 1
    assert not out.exists()


def test_write_failure_restores_previous_bundle(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:  # 54
    bundle = _bundle(xh)
    out = xh.tmp_path / "bundle"
    write_bundle(bundle, out)
    before = sorted(p.name for p in out.iterdir())

    from ehr_simulator import export_bundle

    def boom(_bundle: Any, _files: Any) -> bytes:
        raise OSError("disk full")

    monkeypatch.setattr(export_bundle, "_manifest", boom)
    with pytest.raises(OSError, match="disk full"):
        write_bundle(bundle, out, overwrite=Overwrite.REPLACE)
    assert sorted(p.name for p in out.iterdir()) == before
    assert not [p for p in xh.tmp_path.iterdir() if p.name.startswith(".bundle.")]


def test_exports_are_byte_stable(xh: LifecycleHarness, walked: dict) -> None:  # 58
    first, second = _bundle(xh), _bundle(xh)
    assert first.tables == second.tables


def test_process_exit_codes(xh: LifecycleHarness, walked: dict) -> None:  # 59
    from tests.support.cli import _console_script

    def run(*args: str) -> int:
        return subprocess.run(
            [str(_console_script()), "export-phase2", *args],
            capture_output=True,
            cwd=xh.tmp_path,
            check=False,
            timeout=180,
        ).returncode

    out = xh.tmp_path / "bundle"
    base = [
        str(xh.v1.study_yaml),
        "--db-path",
        str(xh.db_path),
        "--out-dir",
        str(out),
        *_secret_args(xh),
    ]
    assert run(*base) == 0
    assert run(*base) == 1


@pytest.mark.parametrize(
    "change",
    [
        "replacement_patient_id = (SELECT patient_id FROM randomisation_schedule_items "
        "WHERE patient_id NOT IN (SELECT replacement_patient_id FROM case_replacements) LIMIT 1)",
        "planned_arm = CASE planned_arm WHEN 'ai' THEN 'no_ai' ELSE 'ai' END",
        "clinician_id = 'ffffffffffffffff'",
    ],
    ids=["patient", "arm", "clinician"],
)
def test_inconsistent_replacement_refuses(xh: LifecycleHarness, walked: dict, change: str) -> None:
    """Review fix: a plan must agree with the schedule item it names."""
    with xh.conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER trg_case_replacements_activate_once")
        conn.execute(f"UPDATE case_replacements SET {change}")
        conn.commit()
    with pytest.raises(Phase2ExportError, match="replacement"):
        _bundle(xh)


# ---------------------------------------------------------------------------
# Review additions: targeted cases for #13, #21, #52
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("routine", "exported"), [("exclude", False), ("include_explicit", True)])
def test_practice_free_text_follows_its_policy(
    tmp_path: Path, study_fixture_dir: Path, routine: str, exported: bool
) -> None:  # 51
    ph = _practice_study(tmp_path, study_fixture_dir, routine)
    with _tab(ph) as client:
        client.post("/practice/start", follow_redirects=False)
        saved = client.post(
            "/patient/synth_003/timepoint/0/answer",
            data={"question_id": "free_notes", "value": "practice note"},
            headers=HX,
        )
        assert saved.status_code == 200, saved.text

    bundle = _bundle(ph, practice_export=PracticeExport.INCLUDE)
    cell = _rows(bundle, "practice_answers.csv", t_index="0", question_id="free_notes")[0]
    assert cell["response_status"] == "answered"
    assert cell["value_exported"] == str(exported).lower()
    assert (cell["response_value"] == "practice note") is exported


def test_complete_zero_exposure_differs_from_missing(xh: LifecycleHarness) -> None:  # 13
    with _tab(xh) as client:
        patient_id = _started_patient(_start(client))
        render_id = _page(client, patient_id)
        _claim(client, patient_id, TAB_A, render_id)
        mount = {"panel_id": "labs", "expanded": True, "collapsible": True, "state": "loading"}
        events = [
            _event(render_id, 1, "browser.timepoint_enter", client_mono_ms=0.0),
            _event(render_id, 2, "panel.mount", client_mono_ms=10.0, payload=mount),
            _event(render_id, 3, "browser.timepoint_exit", client_mono_ms=3000.0),
        ]
        assert _post(client, TAB_A, events).status_code == HTTP_NO_CONTENT

    bundle = _bundle(xh)
    labs = _rows(bundle, "panel_summaries.csv", patient_id=patient_id, t_index="0", panel_id="labs")
    assert labs[0]["mounted"] == "true" and labs[0]["telemetry_status"] == "complete"
    assert (labs[0]["qualifying_seconds"], labs[0]["viewed"]) == ("0.0", "false")
    later = _rows(bundle, "timepoints.csv", patient_id=patient_id, t_index="1")[0]
    assert later["telemetry_status"] == "missing"
    assert later["foreground_seconds"] == later["active_seconds"] == ""
    assert not _rows(bundle, "panel_summaries.csv", patient_id=patient_id, t_index="1")


def test_two_question_schemas_export_long(
    xh: LifecycleHarness, study_fixture_dir: Path
) -> None:  # 21
    with _tab(xh) as client:
        first = _walk(client, stop_at=0)
    questions = yaml.safe_load((study_fixture_dir / "questions.yaml").read_text())
    questions["questions"].append(
        {
            "question_id": "extra_q",
            "prompt": "Extra",
            "response_type": "categorical",
            "options": ["A", "B"],
            "required": False,
        }
    )
    q_path = xh.tmp_path / "questions_v2.yaml"
    q_path.write_text(yaml.safe_dump(questions))
    v2 = xh.variant("v2")
    v2 = type(v2)(v2.version, v2.study_yaml, q_path)
    xh.activate(v2)
    with xh.client(v2) as client:
        client.event_hooks["response"].clear()
        del client.tab_views
        _walk(client, patient_id=first)
        second = _walk(client, stop_at=0)

    bundle = _bundle(xh)
    table = next(t for t in bundle.tables if t.name == "answers.csv")
    assert "extra_q" not in table.header
    ids = {
        pid: {r["question_id"] for r in _rows(bundle, "answers.csv", patient_id=pid)}
        for pid in (first, second)
    }
    assert "extra_q" not in ids[first]
    assert ids[second] == ids[first] | {"extra_q"}
    extra = _rows(bundle, "answers.csv", patient_id=second, t_index="0", question_id="extra_q")
    assert extra[0]["config_version"] == "v2" and extra[0]["response_status"] == "missing"


# ---------------------------------------------------------------------------
# Review fixes: missing refusals, pseudonyms, keyfile + publish safety
# ---------------------------------------------------------------------------


def test_lifecycle_without_assignment_refuses(xh: LifecycleHarness, walked: dict) -> None:
    with xh.conn() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO case_lifecycle "
            "(clinician_id, patient_id, state, state_changed_at, last_seen_at) "
            "VALUES ('ffffffffffffffff', ?, 'active', '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00')",
            (walked["done"],),
        )
        conn.commit()
    with pytest.raises(Phase2ExportError, match="lifecycle row of patient .* has no assignment"):
        _bundle(xh)


def test_s10_timing_error_refuses(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rule 9 (end < start) is unreachable through ordered rows; inject it."""

    def invalid(*_args: Any, **_kwargs: Any) -> Any:
        raise export_phase2.timing.TimingError("exit precedes enter")

    monkeypatch.setattr(export_phase2.timing, "derive_timepoint_timings", invalid)
    with pytest.raises(Phase2ExportError, match="cannot derive timepoint timing"):
        _bundle(xh)


#: Columns carrying an id derived from the DB clinician id.
LINKED_ID_COLUMNS = ("clinician_id", "schedule_id", "replacement_id", "replaced_by_replacement_id")


def _db_linked_ids(xh: LifecycleHarness) -> set[str]:
    with xh.conn() as conn:
        rows = conn.execute(
            "SELECT clinician_id FROM clinicians "
            "UNION SELECT schedule_id FROM randomisation_schedules "
            "UNION SELECT replacement_id FROM case_replacements"
        ).fetchall()
    return {r[0] for r in rows}


def test_bundle_pseudonymizes_every_linked_id(xh: LifecycleHarness, walked: dict) -> None:
    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    raw = _db_linked_ids(xh)
    text = "".join(str(t.rows) for t in bundle.tables)

    assert raw and not any(value in text for value in raw)
    assert bundle.keyfile_rows == ((pseudonymize(TEST_SECRET, xh.clinician_id), "dr. test"),)
    for table in bundle.tables:
        for column in set(LINKED_ID_COLUMNS) & set(table.header):
            values = {r[column] for r in _rows(bundle, table.name)} - {""}
            assert values <= {pseudonymize(TEST_SECRET, v) for v in raw}, (table.name, column)

    # Payload ids link to the audit through the same pseudonym.
    activated = _rows(bundle, "behavioral_events.csv", kind="case.activated")[0]
    schedules = {r["schedule_id"] for r in _rows(bundle, "randomisation_audit.csv")}
    assert json.loads(activated["payload_json"])["schedule_id"] in schedules


def test_pseudonyms_follow_the_secret(xh: LifecycleHarness, walked: dict) -> None:
    other = bytes(reversed(TEST_SECRET))
    first, again = _bundle(xh), _bundle(xh)
    rotated = _bundle(xh, pseudonym_secret=other)

    ids = {r["clinician_id"] for r in _rows(first, "clinicians.csv")}
    assert ids == {r["clinician_id"] for r in _rows(again, "clinicians.csv")}
    assert ids.isdisjoint({r["clinician_id"] for r in _rows(rotated, "clinicians.csv")})
    assert xh.clinician_id not in ids


def test_cli_creates_the_secret_and_keeps_ids_stable(xh: LifecycleHarness, walked: dict) -> None:
    secret = xh.tmp_path / "keys" / "pseudonym.secret"
    common = [str(xh.v1.study_yaml), "--db-path", str(xh.db_path), "--pseudonym-secret"]

    first = _cli(*common, str(secret), "--out-dir", str(xh.tmp_path / "a"))
    second = _cli(*common, str(secret), "--out-dir", str(xh.tmp_path / "b"))
    assert first.exit_code == 0 and second.exit_code == 0, first.output + second.output

    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    a = (xh.tmp_path / "a" / "clinicians.csv").read_text()
    assert a == (xh.tmp_path / "b" / "clinicians.csv").read_text()
    assert pseudonymize(secret.read_bytes(), xh.clinician_id) in a
    assert xh.clinician_id not in a


def _cli_with_secret(xh: LifecycleHarness, out: Path, secret: Path) -> Any:
    return _cli(
        str(xh.v1.study_yaml),
        "--db-path",
        str(xh.db_path),
        "--out-dir",
        str(out),
        "--pseudonym-secret",
        str(secret),
    )


def test_cli_refuses_a_loose_secret(xh: LifecycleHarness, walked: dict) -> None:
    secret = xh.tmp_path / "pseudonym.secret"
    secret.write_bytes(bytes(32))
    secret.chmod(0o644)
    out = xh.tmp_path / "bundle"

    result = _cli_with_secret(xh, out, secret)

    assert result.exit_code == 1 and "0600" in result.output
    assert not out.exists()


def test_cli_refuses_a_secret_inside_the_bundle(xh: LifecycleHarness, walked: dict) -> None:
    out = xh.tmp_path / "bundle"

    result = _cli_with_secret(xh, out, out / "pseudonym.secret")

    assert result.exit_code == 1 and "outside --out-dir" in result.output
    assert not out.exists()


def test_cli_requires_the_secret(xh: LifecycleHarness, walked: dict) -> None:
    out = xh.tmp_path / "bundle"
    result = _cli(str(xh.v1.study_yaml), "--db-path", str(xh.db_path), "--out-dir", str(out))
    assert result.exit_code != 0
    assert not out.exists()


def _leftovers(xh: LifecycleHarness) -> list[str]:
    return [p.name for p in xh.tmp_path.iterdir() if p.name.startswith((".bundle.", ".key.csv"))]


def test_existing_keyfile_refused_before_publishing(xh: LifecycleHarness, walked: dict) -> None:
    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    out, key = xh.tmp_path / "bundle", xh.tmp_path / "key.csv"
    key.write_text("old keyfile")

    with pytest.raises(BundleWriteError, match="keyfile"):
        write_bundle(bundle, out, keyfile=key)

    assert not out.exists()
    assert key.read_text() == "old keyfile"
    assert not _leftovers(xh)


def test_replace_keeps_old_keyfile_when_the_bundle_fails(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    out, key = xh.tmp_path / "bundle", xh.tmp_path / "key.csv"
    write_bundle(bundle, out, keyfile=key)
    key.write_text("old keyfile")

    from ehr_simulator import export_bundle

    def boom(_bundle: Any, _files: Any) -> bytes:
        raise OSError("disk full")

    monkeypatch.setattr(export_bundle, "_manifest", boom)
    with pytest.raises(OSError, match="disk full"):
        write_bundle(bundle, out, overwrite=Overwrite.REPLACE, keyfile=key)

    assert key.read_text() == "old keyfile"
    assert (out / MANIFEST_NAME).exists()
    assert not _leftovers(xh)


def test_replace_swaps_the_keyfile(xh: LifecycleHarness, walked: dict) -> None:
    bundle = _bundle(xh, keyfile=KeyfileRequest.REQUESTED)
    out, key = xh.tmp_path / "bundle", xh.tmp_path / "key.csv"
    key.write_text("old keyfile")

    write_bundle(bundle, out, overwrite=Overwrite.REPLACE, keyfile=key)

    assert key.read_text().splitlines()[0] == "clinician_id,name_normalized"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert not _leftovers(xh)


def test_interrupt_restores_previous_bundle(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(xh)
    out = xh.tmp_path / "bundle"
    write_bundle(bundle, out)
    before = sorted(p.name for p in out.iterdir())

    real_rename = Path.rename

    def interrupt_publish(self: Path, target: Any) -> Any:
        if self.name.startswith(".bundle.staging-"):
            raise KeyboardInterrupt
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", interrupt_publish)
    with pytest.raises(KeyboardInterrupt):
        write_bundle(bundle, out, overwrite=Overwrite.REPLACE)

    assert sorted(p.name for p in out.iterdir()) == before
    assert not _leftovers(xh)


def test_bundle_files_and_directories_are_fsynced(
    xh: LifecycleHarness, walked: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(xh)
    synced: list[str] = []
    real_fsync = os.fsync

    def record(fd: int) -> None:
        synced.append(os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", record)
    out = write_bundle(bundle, xh.tmp_path / "bundle")

    names = {Path(p).name for p in synced}
    assert {t.name for t in bundle.tables} | {MANIFEST_NAME} <= names
    assert any(Path(p).name.startswith(".bundle.staging-") for p in synced)  # staging dir
    assert str(out.parent.resolve()) in synced  # parent after the rename
