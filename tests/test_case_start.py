"""S11d: explicit Start case, allocation activation and concealment.

Drives the real app on ``study_randomised.yaml`` (Phase 2 study mode:
synth_001..003, timepoints [0, 60, 180], block pattern start/other) plus the
service layer directly for concurrency, rollback and allocation-state tests.

    index GET ── never writes
    POST /case/start ── Phase A schedule ── Phase B activation (atomic)
    patient GET / answer / advance ── only for activated cases
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from ehr_simulator.config import compute_config_hash_from_models, load_questions, load_study_config
from ehr_simulator.db import arm_assignments, connect, events, sessions
from ehr_simulator.db import randomisation as schedules_dao
from ehr_simulator.db.arm_assignments import ARM_SOURCE_PHASE2
from ehr_simulator.randomisation import (
    ActivatedAllocationState,
    create_or_fetch_schedule,
    load_activated_allocation_state,
)
from ehr_simulator.web import case_start
from ehr_simulator.web.app import app_from_study_config
from tests.conftest import _activate_configuration, _seed_clinician, seed_progress

HTTP_OK = 200
HTTP_SEE_OTHER = 303
HTTP_CONFLICT = 409
HTTP_INTEGRITY_ERROR = 500
HX = {"HX-Request": "true"}
COOKIE = "ehrsim_clinician_id"
INDEX_URL = "/"
START_URL = "/case/start"
LAST_T_INDEX = 2
SECOND_CLINICIAN = "Dr. Two"
CASE_KINDS = ("case.activated", "session.start")
ARM_MARKERS = ("no_ai", "phase2_randomized", "data-arm")


def _clinician_id(name: str) -> str:
    normalized = " ".join(name.casefold().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Config:
    version: str
    study_yaml: Path
    questions_yaml: Path

    @property
    def study(self):
        return load_study_config(self.study_yaml)

    @property
    def questions(self):
        return load_questions(self.questions_yaml)

    @property
    def config_hash(self) -> str:
        return compute_config_hash_from_models(self.study, self.questions)


@dataclass
class Harness:
    tmp_path: Path
    db_path: Path
    clinician_id: str
    v1: Config

    def activate(self, config: Config) -> None:
        _activate_configuration(
            self.db_path,
            config.study_yaml,
            config.questions_yaml,
            version=config.version,
            description=f"activate {config.version}",
        )

    def variant(self, version: str, **changes: Any) -> Config:
        """A copy of v1's study YAML with top-level keys replaced."""
        data = yaml.safe_load(self.v1.study_yaml.read_text())
        data.update(changes)
        path = self.tmp_path / f"study_{version}.yaml"
        path.write_text(yaml.safe_dump(data))
        return Config(version, path, self.v1.questions_yaml)

    @contextmanager
    def boot(self, config: Config | None = None, clinician_id: str | None = None):
        config = config or self.v1
        app = app_from_study_config(
            config.study_yaml,
            config.questions_yaml,
            log_dir=self.tmp_path / "logs",
            db_path=self.db_path,
            backup_dir=self.tmp_path / "backups",
        )
        with TestClient(app) as client:
            client.cookies.set(COOKIE, clinician_id or self.clinician_id)
            yield client

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        conn = connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def count(self, sql: str, params: tuple = ()) -> int:
        with self.conn() as conn:
            return conn.execute(sql, params).fetchone()[0]

    def dump(self) -> dict[str, list[tuple]]:
        tables = (
            "arm_assignments",
            "sessions",
            "progress",
            "randomisation_schedules",
            "randomisation_schedule_items",
        )
        with self.conn() as conn:
            dumped = {
                t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in tables
            }
            dumped["case_events"] = [
                tuple(r)
                for r in conn.execute(
                    "SELECT kind, patient_id FROM events WHERE kind IN (?, ?) ORDER BY event_id",
                    CASE_KINDS,
                )
            ]
        return dumped

    def assignments(self, clinician_id: str | None = None):
        with self.conn() as conn:
            return arm_assignments.list_for_clinician(conn, clinician_id or self.clinician_id)

    def schedule(self, clinician_id: str | None = None):
        with self.conn() as conn:
            stored = schedules_dao.fetch_for_clinician(
                conn, self.v1.study.study_id, clinician_id or self.clinician_id
            )
        assert stored is not None
        return stored.schedule

    def add_clinician(self, name: str) -> str:
        clinician_id = _clinician_id(name)
        with self.conn() as conn:
            conn.execute(
                "INSERT INTO clinicians (clinician_id, name_normalized) VALUES (?, ?)",
                (clinician_id, " ".join(name.casefold().split())),
            )
            conn.commit()
        return clinician_id


@pytest.fixture
def harness(tmp_path: Path, study_fixture_dir: Path) -> Harness:
    v1 = Config(
        "v1", study_fixture_dir / "study_randomised.yaml", study_fixture_dir / "questions.yaml"
    )
    db_path = tmp_path / "study.db"
    clinician_id = _seed_clinician(db_path, study_id=v1.study.study_id)
    h = Harness(tmp_path, db_path, clinician_id, v1)
    h.activate(v1)
    return h


def _start(client: TestClient, *, htmx: bool = False):
    return client.post(START_URL, headers=HX if htmx else {}, follow_redirects=False)


def _started_patient(response) -> str:
    assert response.status_code == HTTP_SEE_OTHER, response.text
    return response.headers["location"].split("/")[2]


def _complete(client: TestClient, patient_id: str) -> None:
    seed_progress(client, patient_id, LAST_T_INDEX, completed=True)


def _service_state(config: Config) -> SimpleNamespace:
    return SimpleNamespace(
        study=config.study,
        questions=config.questions,
        config_version=config.version,
        config_hash=config.config_hash,
        write_counter=0,
    )


# ---------------------------------------------------------------------------
# Activation (#1-#13)
# ---------------------------------------------------------------------------


def test_get_unactivated_patient_creates_nothing(harness: Harness) -> None:
    before = harness.dump()
    with harness.boot() as client:
        r = client.get("/patient/synth_001/timepoint/0", follow_redirects=False)

    assert r.status_code == HTTP_SEE_OTHER
    assert harness.dump() == before


def test_index_get_consumes_nothing(harness: Harness) -> None:
    before = harness.dump()
    with harness.boot() as client:
        r = client.get(INDEX_URL)

    assert r.status_code == HTTP_OK
    assert 'data-case-action="start"' in r.text
    assert harness.dump() == before


def test_start_creates_one_phase2_assignment(harness: Harness) -> None:
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))

    rows = harness.assignments()
    assert [(a.patient_id, a.arm_source) for a in rows] == [(patient_id, ARM_SOURCE_PHASE2)]


def test_activation_copies_the_planned_item(harness: Harness) -> None:
    with harness.boot() as client:
        _start(client)

    (row,) = harness.assignments()
    first = harness.schedule().items[0]
    assert (row.patient_id, row.arm, row.seed) == (
        first.patient_id,
        first.planned_arm,
        first.assignment_seed,
    )


def test_activation_stores_schedule_position_and_timestamp(harness: Harness) -> None:
    with harness.boot() as client:
        _start(client)

    (row,) = harness.assignments()
    assert row.schedule_id == harness.schedule().schedule_id
    assert row.case_position == 1
    assert row.activated_at is not None
    assert row.activated_at == row.assigned_at


def test_assignment_receives_active_configuration(harness: Harness) -> None:
    with harness.boot() as client:
        _start(client)

    (row,) = harness.assignments()
    assert (row.config_version, row.config_hash) == ("v1", harness.v1.config_hash)


def test_repeated_start_resumes_the_same_case(harness: Harness) -> None:
    with harness.boot() as client:
        first = _started_patient(_start(client))
        after_first = harness.dump()
        second = _started_patient(_start(client))

    assert second == first
    assert harness.dump() == after_first


def test_concurrent_starts_consume_one_position(harness: Harness) -> None:
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        conn = connect(harness.db_path)
        try:
            barrier.wait()
            started = case_start.start_next_case(
                conn, _service_state(harness.v1), clinician_id=harness.clinician_id
            )
            results.append(started.patient_id)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(set(results)) == 1
    assert len(harness.assignments()) == 1
    assert harness.count("SELECT COUNT(*) FROM events WHERE kind = 'case.activated'") == 1


def test_activated_assignment_is_immutable(harness: Harness) -> None:
    with harness.boot() as client:
        _start(client)

    with harness.conn() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE arm_assignments SET arm = 'x'")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM arm_assignments")
        conn.rollback()


def test_same_patient_is_never_activated_twice(harness: Harness) -> None:
    with harness.conn() as conn:
        stored = create_or_fetch_schedule(
            conn,
            study=harness.v1.study,
            config_version="v1",
            config_hash=harness.v1.config_hash,
            clinician_id=harness.clinician_id,
            load_allocation_state=lambda _c: ActivatedAllocationState.empty(
                harness.v1.study.patient_ids
            ),
        )
        # A legacy row already holds the first scheduled patient.
        conn.execute(
            "INSERT INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, config_hash, config_version) "
            "VALUES (?, ?, 'no_ai', 'phase1_stub', ?, 'v1')",
            (harness.clinician_id, stored.schedule.items[0].patient_id, harness.v1.config_hash),
        )
        conn.commit()
    before = harness.dump()

    with harness.boot() as client:
        r = _start(client)

    assert r.status_code == HTTP_INTEGRITY_ERROR
    assert harness.dump() == before


def test_phase2_insert_without_activation_fields_is_rejected(harness: Harness) -> None:
    with (
        harness.conn() as conn,
        pytest.raises(sqlite3.IntegrityError, match="activation provenance"),
    ):
        conn.execute(
            "INSERT INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version) "
            "VALUES (?, 'synth_001', 'ai', ?, 1, 'h', 'v1')",
            (harness.clinician_id, ARM_SOURCE_PHASE2),
        )


def _fail_after(original: Callable[..., Any], *, kind: str | None = None) -> Callable[..., Any]:
    """Run the real write, then raise — the write must be rolled back."""

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        if kind is None or kwargs.get("kind") == kind:
            raise RuntimeError("injected failure")
        return result

    return wrapper


@pytest.mark.parametrize(
    ("target", "name", "kind"),
    [
        (arm_assignments, "activate_planned_assignment", None),
        (sessions, "start_or_resume", None),
        (events, "append", "case.activated"),
        (events, "append", "session.start"),
    ],
    ids=["assignment", "session", "case.activated", "session.start"],
)
def test_phase_b_failure_rolls_back_everything(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, target: Any, name: str, kind: str | None
) -> None:
    state = _service_state(harness.v1)
    with harness.conn() as conn:
        with monkeypatch.context() as patch:
            patch.setattr(target, name, _fail_after(getattr(target, name), kind=kind))
            with pytest.raises(RuntimeError, match="injected"):
                case_start.start_next_case(conn, state, clinician_id=harness.clinician_id)

        assert not conn.in_transaction
        dumped = harness.dump()
        assert dumped["arm_assignments"] == []
        assert dumped["sessions"] == []
        assert dumped["case_events"] == []
        # Phase A committed on its own: the schedule stays, nothing consumed.
        assert len(dumped["randomisation_schedules"]) == 1
        assert state.write_counter == 0

        retried = case_start.start_next_case(conn, state, clinician_id=harness.clinician_id)

    (row,) = harness.assignments()
    assert row.case_position == 1
    assert retried.patient_id == row.patient_id


# ---------------------------------------------------------------------------
# Activated allocation state (#14-#18)
# ---------------------------------------------------------------------------


def _insert_phase2(
    conn: sqlite3.Connection, clinician_id: str, patient_id: str, arm: str, *, version: str
) -> None:
    conn.execute(
        "INSERT INTO arm_assignments "
        "(clinician_id, patient_id, arm, arm_source, seed, config_hash, config_version, "
        " schedule_id, case_position, activated_at) "
        "VALUES (?, ?, ?, ?, 1, 'h', ?, ?, 1, CURRENT_TIMESTAMP)",
        (
            clinician_id,
            patient_id,
            arm,
            ARM_SOURCE_PHASE2,
            version,
            f"s-{clinician_id}-{patient_id}",
        ),
    )


def test_counts_ignore_phase1_stub_rows(harness: Harness) -> None:
    with harness.conn() as conn:
        conn.execute(
            "INSERT INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, config_hash) "
            "VALUES (?, 'synth_001', 'no_ai', 'phase1_stub', 'h')",
            (harness.clinician_id,),
        )
        conn.commit()

        assert arm_assignments.activated_arm_counts(conn) == {}


def test_counts_span_clinicians_and_versions(harness: Harness) -> None:
    second = harness.add_clinician(SECOND_CLINICIAN)
    with harness.conn() as conn:
        _insert_phase2(conn, harness.clinician_id, "synth_001", "ai", version="v1")
        _insert_phase2(conn, second, "synth_001", "no_ai", version="v2")
        _insert_phase2(conn, second, "synth_002", "ai", version="v2")
        conn.commit()

        assert arm_assignments.activated_arm_counts(conn) == {
            "synth_001": (1, 1),
            "synth_002": (1, 0),
        }


def test_state_covers_exactly_the_pool(harness: Harness) -> None:
    with harness.conn() as conn:
        _insert_phase2(conn, harness.clinician_id, "synth_001", "ai", version="v1")
        _insert_phase2(conn, harness.clinician_id, "off_pool", "no_ai", version="v1")
        conn.commit()

        state = load_activated_allocation_state(conn, ["synth_001", "synth_002"])

    assert state.canonical() == [
        {"patient_id": "synth_001", "ai_count": 1, "no_ai_count": 0},
        {"patient_id": "synth_002", "ai_count": 0, "no_ai_count": 0},
    ]


def test_earlier_activations_shape_later_schedules(harness: Harness) -> None:
    second = harness.add_clinician(SECOND_CLINICIAN)
    with harness.boot() as client:
        _start(client)
    (first_row,) = harness.assignments()

    with harness.boot(clinician_id=second) as client:
        _start(client)

    stored = harness.schedule(second)
    expected = {pid: (0, 0) for pid in harness.v1.study.patient_ids}
    ai, no_ai = expected[first_row.patient_id]
    expected[first_row.patient_id] = (ai + 1, no_ai) if first_row.arm == "ai" else (ai, no_ai + 1)
    assert json.loads(stored.allocation_state_json) == [
        {"patient_id": pid, "ai_count": ai, "no_ai_count": no_ai}
        for pid, (ai, no_ai) in sorted(expected.items())
    ]


def test_allocation_state_is_read_inside_the_schedule_lock(harness: Harness) -> None:
    seen: list[bool] = []

    def loader(conn: sqlite3.Connection) -> ActivatedAllocationState:
        seen.append(conn.in_transaction)
        return load_activated_allocation_state(conn, harness.v1.study.patient_ids)

    with harness.conn() as conn:
        create_or_fetch_schedule(
            conn,
            study=harness.v1.study,
            config_version="v1",
            config_hash=harness.v1.config_hash,
            clinician_id=harness.clinician_id,
            load_allocation_state=loader,
        )

    assert seen == [True]


# ---------------------------------------------------------------------------
# Mode and lifecycle rules (#19-#23)
# ---------------------------------------------------------------------------


def test_study_without_randomisation_keeps_phase1(study_client: TestClient) -> None:
    index = study_client.get(INDEX_URL)
    assert "data-case-action" not in index.text

    assert study_client.get("/patient/synth_001/timepoint/0").status_code == HTTP_OK
    assert _start(study_client).status_code == HTTP_CONFLICT


def test_phase2_never_creates_stub_but_resumes_legacy_rows(harness: Harness) -> None:
    with harness.conn() as conn:
        conn.execute(
            "INSERT INTO arm_assignments "
            "(clinician_id, patient_id, arm, arm_source, config_hash, config_version) "
            "VALUES (?, 'synth_002', 'no_ai', 'phase1_stub', ?, 'v1')",
            (harness.clinician_id, harness.v1.config_hash),
        )
        conn.commit()

    with harness.boot() as client:
        assert client.get("/patient/synth_002/timepoint/0").status_code == HTTP_OK
        client.get("/patient/synth_003/timepoint/0", follow_redirects=False)

    assert [a.patient_id for a in harness.assignments()] == ["synth_002"]


def test_completed_case_is_not_open(harness: Harness) -> None:
    with harness.boot() as client:
        first = _started_patient(_start(client))
        _complete(client, first)
        second = _started_patient(_start(client))

    assert second != first
    assert [a.case_position for a in harness.assignments()] == [1, 2]


def test_start_with_open_case_writes_nothing(harness: Harness) -> None:
    with harness.boot() as client:
        _start(client)
        before = harness.dump()
        state = client.app.state
        with harness.conn() as conn:
            started = case_start.start_next_case(conn, state, clinician_id=harness.clinician_id)

    assert started.outcome is case_start.StartOutcome.RESUMED
    assert harness.dump() == before


def test_exhausted_schedule_refuses(harness: Harness) -> None:
    with harness.boot() as client:
        for _ in harness.v1.study.patient_ids:
            _complete(client, _started_patient(_start(client)))
        before = harness.dump()

        refused = _start(client)
        index = client.get(INDEX_URL)

    assert refused.status_code == HTTP_CONFLICT
    assert harness.dump() == before
    assert 'data-case-action="exhausted"' in index.text
    assert "All cases completed" in index.text
    assert "Start case" not in index.text


# ---------------------------------------------------------------------------
# Concealment and routing (#24-#31)
# ---------------------------------------------------------------------------


def test_pages_never_reveal_allocation(harness: Harness) -> None:
    with harness.boot() as client:
        before = client.get(INDEX_URL).text
        patient_id = _started_patient(_start(client))
        after = client.get(INDEX_URL).text
        page = client.get(f"/patient/{patient_id}/timepoint/0").text

    item = harness.schedule().items[0]
    for html in (before, after, page):
        for marker in (*ARM_MARKERS, str(item.assignment_seed), harness.schedule().schedule_id):
            assert marker not in html


def test_index_and_jumper_list_only_activated_cases(harness: Harness) -> None:
    with harness.boot() as client:
        before = client.get(INDEX_URL).text
        patient_id = _started_patient(_start(client))
        index = client.get(INDEX_URL).text
        page = client.get(f"/patient/{patient_id}/timepoint/0").text

    others = [p for p in harness.v1.study.patient_ids if p != patient_id]
    assert "/patient/" not in before
    assert f"/patient/{patient_id}/" in index
    assert 'data-case-action="resume"' in index
    for other in others:
        assert f"/patient/{other}/" not in index
        assert f"/patient/{other}/" not in page


@pytest.mark.parametrize("htmx", [False, True], ids=["browser", "htmx"])
def test_direct_get_to_unactivated_patient_redirects(harness: Harness, htmx: bool) -> None:
    with harness.boot() as client:
        r = client.get(
            "/patient/synth_001/timepoint/0", headers=HX if htmx else {}, follow_redirects=False
        )

    if htmx:
        assert (r.status_code, r.headers["HX-Redirect"]) == (HTTP_OK, INDEX_URL)
    else:
        assert (r.status_code, r.headers["location"]) == (HTTP_SEE_OTHER, INDEX_URL)


def test_get_activated_patient_succeeds(harness: Harness) -> None:
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))
        r = client.get(f"/patient/{patient_id}/timepoint/0")

    assert r.status_code == HTTP_OK


def test_answer_and_advance_cannot_create_assignments(harness: Harness) -> None:
    before = harness.dump()
    with harness.boot() as client:
        answer = client.post(
            "/patient/synth_001/timepoint/0/answer", data={"question_id": "x", "value": "y"}
        )
        advance = client.post("/patient/synth_001/timepoint/0/advance", follow_redirects=False)

    assert answer.status_code == HTTP_CONFLICT
    assert advance.status_code == HTTP_CONFLICT
    assert harness.dump() == before


def test_activated_case_keeps_its_configuration(harness: Harness) -> None:
    with harness.boot() as client:
        patient_id = _started_patient(_start(client))
    v2 = harness.variant("v2", timepoints=[0, 120])
    harness.activate(v2)

    with harness.boot(v2) as client:
        assert client.get(f"/patient/{patient_id}/timepoint/0").status_code == HTTP_OK

    (row,) = harness.assignments()
    assert (row.config_version, row.config_hash) == ("v1", harness.v1.config_hash)


def test_htmx_start_redirects_with_header(harness: Harness) -> None:
    with harness.boot() as client:
        r = _start(client, htmx=True)

    (row,) = harness.assignments()
    assert r.status_code == HTTP_OK
    assert r.headers["HX-Redirect"] == f"/patient/{row.patient_id}/timepoint/0?chrome=epic"


def test_start_on_stale_server_refuses_before_any_write(harness: Harness) -> None:
    with harness.boot() as client:
        before = harness.dump()
        harness.activate(harness.variant("v2", timepoints=[0, 120]))
        r = _start(client)

    assert r.status_code == HTTP_CONFLICT
    assert harness.dump() == before


def test_refusal_bodies_carry_no_allocation(harness: Harness) -> None:
    with harness.boot() as client:
        for _ in harness.v1.study.patient_ids:
            _complete(client, _started_patient(_start(client)))
        body = _start(client).text

    for marker in (*ARM_MARKERS, *harness.v1.study.patient_ids):
        assert marker not in body


# ---------------------------------------------------------------------------
# Schedule/config integrity (#32-#37)
# ---------------------------------------------------------------------------


def test_items_are_activated_in_schedule_order(harness: Harness) -> None:
    with harness.boot() as client:
        started = []
        for _ in harness.v1.study.patient_ids:
            patient_id = _started_patient(_start(client))
            started.append(patient_id)
            _complete(client, patient_id)

    assert started == [i.patient_id for i in harness.schedule().items]


def test_schedule_survives_compatible_config_change(harness: Harness) -> None:
    with harness.boot() as client:
        _complete(client, _started_patient(_start(client)))
    schedule_before = harness.schedule()
    v2 = harness.variant("v2", timepoints=[0, 120])
    harness.activate(v2)

    with harness.boot(v2) as client:
        patient_id = _started_patient(_start(client))

    assert harness.schedule() == schedule_before
    assert patient_id == schedule_before.items[1].patient_id
    second = harness.assignments()[1]
    assert (second.config_version, second.case_position) == ("v2", 2)


def test_changed_randomisation_rules_refuse(harness: Harness) -> None:
    with harness.boot() as client:
        _complete(client, _started_patient(_start(client)))
    randomisation = harness.v1.study.randomisation.model_dump()
    v2 = harness.variant("v2", randomisation={**randomisation, "master_seed": 999})
    harness.activate(v2)
    before = harness.dump()

    with harness.boot(v2) as client:
        r = _start(client)

    assert r.status_code == HTTP_CONFLICT
    assert harness.dump() == before


def test_patient_pool_change_invalidating_next_item_refuses(harness: Harness) -> None:
    with harness.boot() as client:
        _complete(client, _started_patient(_start(client)))
    next_patient = harness.schedule().items[1].patient_id
    pool = [p for p in harness.v1.study.patient_ids if p != next_patient]
    v2 = harness.variant("v2", patient_ids=pool)
    harness.activate(v2)
    before = harness.dump()

    with harness.boot(v2) as client:
        r = _start(client)

    assert r.status_code == HTTP_CONFLICT
    assert harness.dump() == before


def test_starts_never_rewrite_schedules(harness: Harness) -> None:
    second = harness.add_clinician(SECOND_CLINICIAN)
    with harness.boot() as client:
        _start(client)
    first_tables = harness.dump()
    first_schedule = harness.schedule()

    with harness.boot(clinician_id=second) as client:
        _start(client)
    with harness.boot() as client:
        _start(client)

    after = harness.dump()
    for table in ("randomisation_schedules", "randomisation_schedule_items"):
        assert set(first_tables[table]) <= set(after[table])
    assert harness.schedule() == first_schedule


def test_stale_server_creates_no_schedule(harness: Harness) -> None:
    with harness.boot() as client:
        harness.activate(harness.variant("v2", timepoints=[0, 120]))
        r = _start(client)

    assert r.status_code == HTTP_CONFLICT
    assert harness.count("SELECT COUNT(*) FROM randomisation_schedules") == 0
