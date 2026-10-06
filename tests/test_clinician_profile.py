"""S11p: clinician characteristics — collection, gate, lock and export.

Runs the real Phase 2 app on ``study_lifecycle.yaml`` plus a
``clinician_profile`` block; the harness saves no profile on its own here::

    login ─► /profile (while missing) ─► POST /profile ─► Start case allowed
    first measured case ─► profile locked (service 409 + SQLite trigger)
    export-phase2 ─► clinicians.csv (characteristics, never the name)

Test numbers refer to ``specs/session-11p-clinician-characteristics.md``.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ehr_simulator.cli_support import walk_preflight_report
from ehr_simulator.config import load_questions, load_study_config
from ehr_simulator.db import clinician_profiles, connect
from ehr_simulator.db.connection import AccessMode
from ehr_simulator.export_phase2 import build_phase2_bundle
from ehr_simulator.pseudonym import pseudonymize
from tests.conftest import ProfileSetup, valid_profile_form
from tests.support.cases import HTTP_CONFLICT, HTTP_SEE_OTHER, _start, _started_patient
from tests.support.lifecycle import LifecycleHarness, _harness
from tests.support.pseudonym import TEST_SECRET

PROFILE = {"specialties": ["neurology", "emergency_medicine"], "countries": ["CH", "FR"]}
HTTP_NOT_FOUND = 404
HTTP_UNPROCESSABLE = 422
NURSE = {"professional_role": "nurse", "years_of_practice": "3", "country_of_practice": "FR"}


def _study(tmp_path: Path, fixture_dir: Path, **changes: Any) -> LifecycleHarness:
    tmp_path.mkdir(parents=True, exist_ok=True)
    data = yaml.safe_load((fixture_dir / "study_lifecycle.yaml").read_text())
    data.update(changes)
    path = tmp_path / "study_profile.yaml"
    path.write_text(yaml.safe_dump(data))
    h = _harness(tmp_path, path, fixture_dir / "questions.yaml")
    h.profile_setup = ProfileSetup.NONE
    return h


@pytest.fixture
def ph(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    return _study(tmp_path, study_fixture_dir, clinician_profile=PROFILE)


def _save(client: TestClient, form: dict[str, str]) -> Any:
    return client.post("/profile", data=form, follow_redirects=False)


def _physician() -> dict[str, str]:
    return valid_profile_form(type("C", (), PROFILE))


def _stored(h: LifecycleHarness) -> Any:
    with h.conn() as conn:
        return clinician_profiles.fetch(conn, h.clinician_id)


# ---------------------------------------------------------------------------
# Collection and validation (#1, #2, #4, #5)
# ---------------------------------------------------------------------------


def test_physician_profile_is_stored_and_restored(ph: LifecycleHarness) -> None:  # 1
    with ph.client() as client:
        saved = _save(client, _physician())
        page = client.get("/profile").text

    assert saved.status_code == HTTP_SEE_OTHER and saved.headers["location"] == "/"
    stored = _stored(ph)
    assert (stored.professional_role, stored.years_of_practice) == ("physician", 7.5)
    assert (stored.country_of_practice, stored.primary_specialty) == ("CH", "neurology")
    assert 'value="physician" selected' in page and 'value="7.5"' in page
    assert 'value="neurology" selected' in page


def test_nurse_profile_has_no_specialty(ph: LifecycleHarness) -> None:  # 2
    with ph.client() as client:
        assert _save(client, NURSE).status_code == HTTP_SEE_OTHER

    assert _stored(ph).primary_specialty is None


@pytest.mark.parametrize(
    "years", ["-1", "abc", "", "inf", "nan", "80.5", "7_5", "1e1", "1.", ".5", "+3"]
)
def test_invalid_years_are_refused(ph: LifecycleHarness, years: str) -> None:  # 4
    with ph.client() as client:
        response = _save(client, {**_physician(), "years_of_practice": years})

    assert response.status_code == HTTP_UNPROCESSABLE
    assert "Years of practice" in response.text or "number of years" in response.text
    assert _stored(ph) is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"primary_specialty": ""}, "Physicians choose a specialty"),
        ({"primary_specialty": "cardiology"}, "Physicians choose a specialty"),
        ({"professional_role": "surgeon"}, "Choose physician or nurse"),
        ({"country_of_practice": "US"}, "Choose a country"),
        ({"professional_role": "nurse"}, "Specialty applies to physicians only"),
    ],
    ids=["no specialty", "unknown specialty", "unknown role", "unknown country", "nurse"],
)
def test_invalid_combinations_are_refused(
    ph: LifecycleHarness, changes: dict[str, str], field: str
) -> None:  # 5
    with ph.client() as client:
        response = _save(client, {**_physician(), **changes})

    assert response.status_code == HTTP_UNPROCESSABLE
    assert field in response.text
    assert _stored(ph) is None


def test_database_enforces_the_specialty_rule(ph: LifecycleHarness) -> None:  # 12
    with ph.conn() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clinician_profiles VALUES (?, 'physician', 1, 'CH', NULL, "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (ph.clinician_id,),
        )


# ---------------------------------------------------------------------------
# Gate and lock (#3, #6, #7)
# ---------------------------------------------------------------------------


def test_start_case_needs_a_profile(ph: LifecycleHarness) -> None:  # 3
    with ph.client() as client:
        index = client.get("/").text
        refused = _start(client)
        rows = ph.count("SELECT COUNT(*) FROM randomisation_schedules") + ph.count(
            "SELECT COUNT(*) FROM arm_assignments"
        )
        _save(client, _physician())
        started = _start(client)

    assert 'href="/profile"' in index and "case-start" not in index
    assert refused.status_code == HTTP_CONFLICT
    assert rows == 0
    assert started.status_code == HTTP_SEE_OTHER


def test_profile_locks_at_the_first_measured_case(ph: LifecycleHarness) -> None:  # 6
    with ph.client() as client:
        _save(client, _physician())
        assert _save(client, {**_physician(), "years_of_practice": "8"}).status_code == 303
        _started_patient(_start(client))
        locked = _save(client, {**_physician(), "years_of_practice": "20"})
        page = client.get("/profile").text

    assert locked.status_code == HTTP_CONFLICT
    assert _stored(ph).years_of_practice == 8.0
    assert "Locked" in page and "<fieldset disabled>" in page
    assert [e["action"] for e in ph.events("clinician.profile_saved")] == ["created", "updated"]
    with ph.conn() as conn, pytest.raises(sqlite3.IntegrityError, match="locked"):
        conn.execute("UPDATE clinician_profiles SET years_of_practice = 30")
    with ph.conn() as conn, pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        conn.execute("DELETE FROM clinician_profiles")


def test_login_goes_to_the_profile_until_saved(ph: LifecycleHarness) -> None:  # 7
    with ph.client() as client:
        client.cookies.clear()
        first = client.post("/login", data={"clinician_name": "Dr. New"}, follow_redirects=False)
        _save(client, _physician())
        again = client.post("/login", data={"clinician_name": "Dr. New"}, follow_redirects=False)

    assert first.headers["location"] == "/profile"
    assert again.headers["location"] == "/"


def test_open_case_resumes_without_a_profile(tmp_path: Path, study_fixture_dir: Path) -> None:
    """A later version adding the block never strands an open case."""
    h = _study(tmp_path, study_fixture_dir)
    with h.client() as client:
        patient_id = _started_patient(_start(client))
    v2 = h.variant("v2", clinician_profile=PROFILE)
    h.activate(v2)
    with h.client(v2) as client:
        resumed = _start(client)

    assert resumed.status_code == HTTP_SEE_OTHER
    assert _started_patient(resumed) == patient_id


# ---------------------------------------------------------------------------
# Export and privacy (#8, #9)
# ---------------------------------------------------------------------------


def _clinicians_csv(h: LifecycleHarness) -> list[dict[str, str]]:
    conn = connect(h.db_path, access=AccessMode.READ_ONLY)
    try:
        bundle = build_phase2_bundle(
            conn, study_id=h.v1.study.study_id, pseudonym_secret=TEST_SECRET
        )
    finally:
        conn.close()
    table = next(t for t in bundle.tables if t.name == "clinicians.csv")
    assert "Dr" not in json.dumps([t.rows for t in bundle.tables])
    return [dict(zip(table.header, row, strict=True)) for row in table.rows]


def test_clinicians_csv_carries_the_profile(ph: LifecycleHarness) -> None:  # 8
    with ph.client() as client:
        _save(client, _physician())
        _start(client)

    assert _clinicians_csv(ph) == [
        {
            "study_id": ph.v1.study.study_id,
            "clinician_id": pseudonymize(TEST_SECRET, ph.clinician_id),
            "profile_status": "complete",
            "professional_role": "physician",
            "years_of_practice": "7.5",
            "country_of_practice": "CH",
            "primary_specialty": "neurology",
        }
    ]


def test_clinicians_csv_marks_a_missing_profile(tmp_path: Path, study_fixture_dir: Path) -> None:
    h = _study(tmp_path, study_fixture_dir)
    with h.client() as client:
        _start(client)

    row = _clinicians_csv(h)[0]
    assert row["profile_status"] == "missing" and row["professional_role"] == ""


def test_characteristics_never_enter_events(ph: LifecycleHarness) -> None:  # 9
    with ph.client() as client:
        _save(client, _physician())

    with ph.conn() as conn:
        payloads = [r[0] for r in conn.execute("SELECT payload_json FROM events")]
    text = " ".join(payloads)
    assert payloads and not any(v in text for v in ("physician", "neurology", "7.5", '"CH"'))


# ---------------------------------------------------------------------------
# No effect on allocation (#10)
# ---------------------------------------------------------------------------


def test_saving_writes_only_the_profile(ph: LifecycleHarness) -> None:  # 10
    before = ph.dump()
    with ph.client() as client:
        _save(client, _physician())
    after = ph.dump()

    # Assignments, sessions, progress and schedules are untouched.
    assert after == before
    assert _stored(ph) is not None
    assert ph.count("SELECT COUNT(*) FROM case_lifecycle") == 0


def _schedule(h: LifecycleHarness, form: dict[str, str]) -> list[tuple[str, str]]:
    with h.client() as client:
        _save(client, form)
        _start(client)
    with h.conn() as conn:
        rows = conn.execute(
            "SELECT patient_id, planned_arm FROM randomisation_schedule_items "
            "ORDER BY case_position"
        ).fetchall()
    return [tuple(r) for r in rows]


def test_profile_values_do_not_change_the_schedule(
    tmp_path: Path, study_fixture_dir: Path
) -> None:  # 10
    physician = _schedule(
        _study(tmp_path / "a", study_fixture_dir, clinician_profile=PROFILE), _physician()
    )
    nurse = _schedule(_study(tmp_path / "b", study_fixture_dir, clinician_profile=PROFILE), NURSE)
    assert physician == nurse and physician


# ---------------------------------------------------------------------------
# Studies without the block (#11)
# ---------------------------------------------------------------------------


def test_without_the_block_nothing_changes(tmp_path: Path, study_fixture_dir: Path) -> None:
    h = _study(tmp_path, study_fixture_dir)
    with h.client() as client:
        page = client.get("/profile")
        started = _start(client)

    assert page.status_code == HTTP_NOT_FOUND
    assert started.status_code == HTTP_SEE_OTHER


def test_preflight_warns_phase2_without_the_block(study_fixture_dir: Path) -> None:
    study = load_study_config(study_fixture_dir / "study_lifecycle.yaml")
    report, _ = walk_preflight_report(study, load_questions(study_fixture_dir / "questions.yaml"))

    warnings = [r.message for r in report.rows if r.status == "WARN"]
    assert any("clinician characteristics" in m for m in warnings)
