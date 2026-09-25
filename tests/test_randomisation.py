"""S11c: adaptive-block randomisation scheduler + planned-schedule persistence.

Covers the ``randomisation`` study block (:mod:`ehr_simulator.config.study`),
the pure scheduler and service (:mod:`ehr_simulator.randomisation`), and the
schedule DAO (:mod:`ehr_simulator.db.randomisation`).
"""

from __future__ import annotations

import json
import random
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pydantic
import pytest

from ehr_simulator.config import (
    Questions,
    StudyConfig,
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
    parse_study_snapshot,
    render_study_snapshot,
)
from ehr_simulator.db import (
    ConfigurationProvenanceError,
    RandomisationError,
    RandomisationIntegrityError,
    StaleConfigurationError,
    StudyIdentityError,
    arm_assignments,
    config_history,
    study_identity,
)
from ehr_simulator.db import randomisation as schedules_dao
from ehr_simulator.randomisation import (
    RANDOMISATION_ALGORITHM_VERSION,
    ActivatedAllocationState,
    StartingArmCounts,
    create_or_fetch_schedule,
    generate_schedule,
)

SQLITE_INT_MAX = 2**63 - 1
FAKE_HASH = "h" * 64
NO_STARTS = StartingArmCounts(ai=0, no_ai=0)

_RANDOMISATION = {"master_seed": 123456, "block_length": 2, "block_sequence": ["start", "other"]}


def _study(
    n_patients: int = 6,
    *,
    randomisation: dict[str, Any] | None = _RANDOMISATION,
    study_id: str = "golden",
) -> StudyConfig:
    data: dict[str, Any] = {
        "schema_version": "2",
        "study_id": study_id,
        "dataset": "synthetic",
        "patient_ids": [f"p{i:02d}" for i in range(1, n_patients + 1)],
        "time_unit": "minutes",
        "timepoints": [0],
    }
    if randomisation is not None:
        data["randomisation"] = randomisation
    return StudyConfig.model_validate(data)


def _generate(
    study: StudyConfig | None = None,
    *,
    clinician_id: str = "c1",
    state: ActivatedAllocationState | None = None,
    starts: StartingArmCounts = NO_STARTS,
    config_version: str = "v1",
):
    study = study or _study()
    return generate_schedule(
        study=study,
        config_version=config_version,
        config_hash=FAKE_HASH,
        clinician_id=clinician_id,
        allocation_state=state or ActivatedAllocationState.empty(study.patient_ids),
        starting_arm_counts=starts,
    )


def _order(schedule) -> list[str]:
    return [item.patient_id for item in schedule.items]


def _arms(schedule) -> list[str]:
    return [item.planned_arm for item in schedule.items]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class TestRandomisationConfig:
    def test_v2_config_without_randomisation_parses_and_serializes_unchanged(
        self, study_fixture_dir: Path
    ) -> None:
        study = load_study_config(study_fixture_dir / "study_synthetic.yaml")

        assert study.randomisation is None
        # Absent block → absent key: pre-S11c snapshots and hashes stay put.
        snapshot = render_study_snapshot(study)
        assert "randomisation" not in snapshot
        assert parse_study_snapshot(snapshot) == study

    def test_pre_s11c_snapshot_still_parses(self) -> None:
        stored = (
            '{"schema_version":"2","study_id":"fixture_synthetic","dataset":"synthetic",'
            '"patient_ids":["synth_001","synth_002","synth_003"],"time_unit":"minutes",'
            '"timepoints":[0.0,60.0,180.0]}'
        )

        assert parse_study_snapshot(stored).randomisation is None

    def test_valid_randomisation_parses_and_changes_config_hash(
        self, study_fixture_dir: Path
    ) -> None:
        questions = load_questions(study_fixture_dir / "questions.yaml")
        plain = load_study_config(study_fixture_dir / "study_synthetic.yaml")
        randomised = plain.model_copy(
            update={"randomisation": _study().randomisation},
        )

        assert randomised.randomisation is not None
        assert randomised.randomisation.block_sequence == ["start", "other"]
        assert compute_config_hash_from_models(plain, questions) != (
            compute_config_hash_from_models(randomised, questions)
        )
        # Settings survive the snapshot round-trip.
        assert parse_study_snapshot(render_study_snapshot(randomised)) == randomised

    def test_fixture_yaml_parses(self, study_fixture_dir: Path) -> None:
        study = load_study_config(study_fixture_dir / "study_randomised.yaml")

        assert study.randomisation is not None
        assert study.randomisation.master_seed == 123456

    @pytest.mark.parametrize(
        "override",
        [
            {"master_seed": -1},
            {"master_seed": SQLITE_INT_MAX + 1},
            {"master_seed": True},
            {"master_seed": "123"},
            {"block_length": 0},
            {"block_length": 1.5},
            {"block_sequence": []},
            {"block_sequence": ["start"]},
            {"block_sequence": ["other", "other"]},
            {"block_sequence": ["start", "start", "other"]},
            {"block_sequence": ["start", "ai"]},
            {"unknown": 1},
        ],
    )
    def test_invalid_randomisation_is_rejected(self, override: dict[str, Any]) -> None:
        with pytest.raises(pydantic.ValidationError):
            _study(randomisation={**_RANDOMISATION, **override})

    def test_seed_bounds_are_inclusive(self) -> None:
        assert _study(randomisation={**_RANDOMISATION, "master_seed": 0})
        assert _study(randomisation={**_RANDOMISATION, "master_seed": SQLITE_INT_MAX})

    def test_generation_refuses_config_without_randomisation(self) -> None:
        with pytest.raises(RandomisationError, match="no 'randomisation'"):
            _generate(_study(randomisation=None))


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_identical_inputs_produce_identical_schedule(self) -> None:
        first = _generate()
        second = _generate()

        assert first == second
        assert repr(first) == repr(second)
        assert first.schedule_id == second.schedule_id

    def test_golden_schedule_pins_adaptive_block_v1(self) -> None:
        """Any behaviour change here needs a new algorithm version."""
        schedule = _generate()

        assert schedule.algorithm_version == "adaptive_block_v1"
        assert schedule.schedule_id == (
            "86a8ee90d2a7ff54d360c0c050f11eab562ff2c5ee1fa451fc6f26d23bd14dcc"
        )
        assert schedule.derived_seed_hex == (
            "dcbea21bda4ae9bd8be043f04058d4b9f81609d3ad8db8e8c6d0e4dcce3b4c6b"
        )
        assert schedule.starting_arm == "ai"
        assert _order(schedule) == ["p02", "p03", "p05", "p06", "p01", "p04"]
        assert schedule.items[0].assignment_seed == 4024199581596822135

    def test_clinicians_get_independent_schedules(self) -> None:
        study = _study(12)
        a = _generate(study, clinician_id="c1")
        b = _generate(study, clinician_id="c2")

        assert a.schedule_id != b.schedule_id
        assert a.derived_seed_hex != b.derived_seed_hex
        assert _order(a) != _order(b)

    def test_master_seed_changes_schedule(self) -> None:
        a = _generate(_study(12))
        b = _generate(_study(12, randomisation={**_RANDOMISATION, "master_seed": 654321}))

        assert a.derived_seed_hex != b.derived_seed_hex
        assert _order(a) != _order(b)

    def test_allocation_state_participates_in_derivation(self) -> None:
        study = _study()
        empty = ActivatedAllocationState.empty(study.patient_ids)
        # Same scores for every candidate (all balanced) → only the key differs.
        shifted = ActivatedAllocationState.from_counts({pid: (1, 1) for pid in study.patient_ids})

        a = _generate(study, state=empty)
        b = _generate(study, state=shifted)

        assert a.schedule_id != b.schedule_id
        assert a.derived_seed_hex != b.derived_seed_hex
        assert json.loads(b.allocation_state_json)[0] == {
            "patient_id": "p01",
            "ai_count": 1,
            "no_ai_count": 1,
        }

    def test_allocation_state_order_is_canonical(self) -> None:
        study = _study()
        forward = ActivatedAllocationState.from_counts({pid: (0, 0) for pid in study.patient_ids})
        backward = ActivatedAllocationState.from_counts(
            {pid: (0, 0) for pid in reversed(study.patient_ids)}
        )

        assert _generate(study, state=forward) == _generate(study, state=backward)

    def test_global_rng_state_has_no_effect(self) -> None:
        random.seed(1)
        a = _generate()
        random.seed(2)
        random.random()
        b = _generate()

        assert a == b

    def test_other_config_version_changes_derivation(self) -> None:
        assert _generate(config_version="v1").schedule_id != (
            _generate(config_version="v2").schedule_id
        )


# ---------------------------------------------------------------------------
# Balance and scheduling
# ---------------------------------------------------------------------------


class TestScheduling:
    @pytest.mark.parametrize(
        ("block_length", "sequence", "n_patients"),
        [
            (2, ["start", "other"], 8),
            (1, ["start", "other"], 6),
            (3, ["start", "other", "other", "start"], 12),
        ],
    )
    def test_full_cycles_balance_arms_within_clinician(
        self, block_length: int, sequence: list[str], n_patients: int
    ) -> None:
        study = _study(
            n_patients,
            randomisation={
                **_RANDOMISATION,
                "block_length": block_length,
                "block_sequence": sequence,
            },
        )

        arms = _arms(_generate(study))

        assert arms.count("ai") == arms.count("no_ai") == n_patients // 2

    def test_every_patient_appears_exactly_once(self) -> None:
        study = _study(7)

        order = _order(_generate(study))

        assert sorted(order) == sorted(study.patient_ids)
        assert len(order) == len(set(order)) == 7

    def test_prefers_patient_that_reduces_imbalance(self) -> None:
        study = _study(6)
        counts = {pid: (0, 0) for pid in study.patient_ids}
        counts["p05"] = (1, 0)  # needs no_ai
        counts["p06"] = (0, 1)  # needs ai
        state = ActivatedAllocationState.from_counts(counts)

        # ai starts outnumber → this schedule starts no_ai.
        schedule = _generate(study, state=state, starts=StartingArmCounts(ai=1, no_ai=0))

        assert schedule.starting_arm == "no_ai"
        assert schedule.items[0].patient_id == "p05"
        first_ai = next(item for item in schedule.items if item.planned_arm == "ai")
        assert first_ai.patient_id == "p06"

    def test_planned_schedules_of_other_clinicians_do_not_affect_ordering(self) -> None:
        study = _study()
        state = ActivatedAllocationState.empty(study.patient_ids)

        # Only activated counts enter the score; generating c1 first is irrelevant.
        _generate(study, clinician_id="c1", state=state)
        assert _generate(study, clinician_id="c2", state=state) == _generate(
            study, clinician_id="c2", state=state
        )

    def test_ties_resolve_reproducibly_and_vary_by_clinician(self) -> None:
        study = _study(10)
        orders = {tuple(_order(_generate(study, clinician_id=f"c{i}"))) for i in range(8)}

        assert len(orders) > 1
        assert _order(_generate(study, clinician_id="c3")) == _order(
            _generate(study, clinician_id="c3")
        )

    def test_block_metadata(self) -> None:
        # 5 patients, blocks of 2: ai ai | no_ai no_ai | ai (truncated).
        schedule = _generate(_study(5), starts=StartingArmCounts(ai=0, no_ai=1))

        assert schedule.starting_arm == "ai"
        rows = [
            (
                i.case_position,
                i.planned_arm,
                i.block_number,
                i.position_in_block,
                i.preceding_block_arm,
            )
            for i in schedule.items
        ]
        assert rows == [
            (1, "ai", 1, 1, None),
            (2, "ai", 1, 2, None),
            (3, "no_ai", 2, 1, "ai"),
            (4, "no_ai", 2, 2, "ai"),
            (5, "ai", 3, 1, "no_ai"),
        ]

    def test_preceding_block_arm_with_repeated_entries(self) -> None:
        study = _study(
            4,
            randomisation={
                **_RANDOMISATION,
                "block_length": 1,
                "block_sequence": ["start", "other", "other", "start"],
            },
        )

        schedule = _generate(study, starts=StartingArmCounts(ai=0, no_ai=1))

        assert _arms(schedule) == ["ai", "no_ai", "no_ai", "ai"]
        assert [i.preceding_block_arm for i in schedule.items] == [None, "ai", "no_ai", "no_ai"]

    @pytest.mark.parametrize(
        ("starts", "expected"),
        [
            (StartingArmCounts(ai=1, no_ai=3), "ai"),
            (StartingArmCounts(ai=3, no_ai=1), "no_ai"),
        ],
    )
    def test_starting_arm_prefers_less_represented(
        self, starts: StartingArmCounts, expected: str
    ) -> None:
        schedule = _generate(starts=starts)

        assert schedule.starting_arm == expected
        assert schedule.items[0].planned_arm == expected
        assert (schedule.starting_ai_count, schedule.starting_no_ai_count) == (
            starts.ai,
            starts.no_ai,
        )

    def test_equal_starting_counts_break_ties_deterministically(self) -> None:
        starts = StartingArmCounts(ai=2, no_ai=2)
        picks = [_generate(clinician_id=f"c{i}", starts=starts).starting_arm for i in range(16)]

        assert set(picks) == {"ai", "no_ai"}
        assert picks == [
            _generate(clinician_id=f"c{i}", starts=starts).starting_arm for i in range(16)
        ]

    def test_planned_cases_since_ai(self) -> None:
        # Starts no_ai: no_ai no_ai | ai ai | no_ai no_ai.
        schedule = _generate(_study(6), starts=StartingArmCounts(ai=1, no_ai=0))

        assert _arms(schedule) == ["no_ai", "no_ai", "ai", "ai", "no_ai", "no_ai"]
        assert [i.planned_cases_since_ai for i in schedule.items] == [None, None, 0, 0, 1, 2]

    def test_assignment_seed_fits_sqlite_and_is_reproducible(self) -> None:
        seeds = [i.assignment_seed for i in _generate(_study(40)).items]

        assert all(0 <= s <= SQLITE_INT_MAX for s in seeds)
        assert len(set(seeds)) == len(seeds)
        assert seeds == [i.assignment_seed for i in _generate(_study(40)).items]

    @pytest.mark.parametrize(
        "counts",
        [
            {"p01": (0, 0)},
            {**{f"p{i:02d}": (0, 0) for i in range(1, 7)}, "p99": (0, 0)},
            {**{f"p{i:02d}": (0, 0) for i in range(1, 7)}, "p01": (-1, 0)},
            {**{f"p{i:02d}": (0, 0) for i in range(1, 7)}, "p01": (True, 0)},
        ],
    )
    def test_allocation_state_must_cover_configured_patients(
        self, counts: dict[str, tuple[int, int]]
    ) -> None:
        with pytest.raises(RandomisationError):
            _generate(state=ActivatedAllocationState.from_counts(counts))


# ---------------------------------------------------------------------------
# Persistence and provenance
# ---------------------------------------------------------------------------


@pytest.fixture
def study(study_fixture_dir: Path) -> StudyConfig:
    return load_study_config(study_fixture_dir / "study_randomised.yaml")


@pytest.fixture
def questions(study_fixture_dir: Path) -> Questions:
    return load_questions(study_fixture_dir / "questions.yaml")


@pytest.fixture
def config_hash(study: StudyConfig, questions: Questions) -> str:
    return compute_config_hash_from_models(study, questions)


@pytest.fixture
def activated(
    db: sqlite3.Connection, study: StudyConfig, questions: Questions, config_hash: str
) -> sqlite3.Connection:
    config_history.activate(
        db,
        study_id=study.study_id,
        config_version="v1",
        config_hash=config_hash,
        description="initial activation",
        reason=None,
        study=study,
        questions=questions,
    )
    return db


def _create(
    conn: sqlite3.Connection,
    study: StudyConfig,
    config_hash: str,
    *,
    clinician_id: str = "c1",
    config_version: str = "v1",
    state: ActivatedAllocationState | None = None,
):
    return create_or_fetch_schedule(
        conn,
        study=study,
        config_version=config_version,
        config_hash=config_hash,
        clinician_id=clinician_id,
        load_allocation_state=lambda _conn: (
            state or ActivatedAllocationState.empty(study.patient_ids)
        ),
    )


def _dump(conn: sqlite3.Connection) -> tuple[list[tuple], list[tuple]]:
    headers = conn.execute("SELECT * FROM randomisation_schedules ORDER BY schedule_id").fetchall()
    items = conn.execute(
        "SELECT * FROM randomisation_schedule_items ORDER BY schedule_id, case_position"
    ).fetchall()
    return [tuple(r) for r in headers], [tuple(r) for r in items]


class TestPersistence:
    def test_schedule_and_items_round_trip(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        stored = _create(activated, study, config_hash)

        fetched = schedules_dao.fetch_for_clinician(activated, study.study_id, "c1")
        assert fetched == stored
        assert len(stored.schedule.items) == len(study.patient_ids)
        assert stored.generated_at is not None

    def test_insert_is_atomic(self, db: sqlite3.Connection) -> None:
        # Duplicate patient in the second item → UNIQUE fails after the header.
        schedule = _generate()
        duplicate = replace(schedule.items[1], patient_id=schedule.items[0].patient_id)
        broken = replace(schedule, items=(schedule.items[0], duplicate))

        with pytest.raises(sqlite3.IntegrityError):
            schedules_dao.insert_schedule(db, broken)

        assert _dump(db) == ([], [])
        assert not db.in_transaction

    def test_stored_record_carries_provenance(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        stored = _create(activated, study, config_hash).schedule

        assert stored.study_id == study.study_id
        assert stored.config_version == "v1"
        assert stored.config_hash == config_hash
        assert stored.algorithm_version == RANDOMISATION_ALGORITHM_VERSION
        assert stored.master_seed == 123456
        assert len(stored.derived_seed_hex) == 64
        assert json.loads(stored.allocation_state_json) == [
            {"patient_id": pid, "ai_count": 0, "no_ai_count": 0}
            for pid in sorted(study.patient_ids)
        ]
        assert (stored.starting_ai_count, stored.starting_no_ai_count) == (0, 0)
        assert stored.block_length == 1
        assert json.loads(stored.block_sequence_json) == ["start", "other"]

        # The persisted inputs regenerate the exact schedule.
        regenerated = generate_schedule(
            study=parse_study_snapshot(config_history.fetch_version(activated, "v1").study_json),
            config_version=stored.config_version,
            config_hash=stored.config_hash,
            clinician_id=stored.clinician_id,
            allocation_state=ActivatedAllocationState.from_counts(
                {
                    row["patient_id"]: (row["ai_count"], row["no_ai_count"])
                    for row in json.loads(stored.allocation_state_json)
                }
            ),
            starting_arm_counts=StartingArmCounts(
                ai=stored.starting_ai_count, no_ai=stored.starting_no_ai_count
            ),
        )
        assert regenerated == stored

    def test_recreate_is_idempotent(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        first = _create(activated, study, config_hash)
        before = _dump(activated)

        again = _create(activated, study, config_hash)
        reinserted = schedules_dao.insert_schedule(activated, first.schedule)

        assert again == first
        assert reinserted == first
        assert _dump(activated) == before

    def test_existing_schedule_is_never_recalculated(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        first = _create(activated, study, config_hash)
        changed_state = ActivatedAllocationState.from_counts(
            {pid: (3, 0) for pid in study.patient_ids}
        )

        assert _create(activated, study, config_hash, state=changed_state) == first

    def test_replacing_a_schedule_is_refused(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        first = _create(activated, study, config_hash)
        before = _dump(activated)
        other = generate_schedule(
            study=study,
            config_version="v1",
            config_hash=config_hash,
            clinician_id="c1",
            allocation_state=ActivatedAllocationState.from_counts(
                {pid: (1, 0) for pid in study.patient_ids}
            ),
            starting_arm_counts=StartingArmCounts(ai=5, no_ai=0),
        )

        with pytest.raises(RandomisationIntegrityError):
            schedules_dao.insert_schedule(activated, other)

        assert _dump(activated) == before
        assert schedules_dao.fetch_for_clinician(activated, study.study_id, "c1") == first

    def test_later_clinicians_never_change_earlier_schedules(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        _create(activated, study, config_hash, clinician_id="c1")
        headers_before, items_before = _dump(activated)

        for i in range(2, 6):
            _create(activated, study, config_hash, clinician_id=f"c{i}")

        headers_after, items_after = _dump(activated)
        assert set(headers_before) <= set(headers_after)
        assert set(items_before) <= set(items_after)
        assert len(schedules_dao.list_schedules(activated, study.study_id)) == 5

    def test_starting_arm_balances_across_clinicians(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        starts = [
            _create(activated, study, config_hash, clinician_id=f"c{i}").schedule.starting_arm
            for i in range(6)
        ]

        assert starts.count("ai") == starts.count("no_ai") == 3
        # Every pair after the first tie is corrected immediately.
        for i in range(0, 6, 2):
            assert {starts[i], starts[i + 1]} == {"ai", "no_ai"}

    def test_assignment_seed_survives_sqlite(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        stored = _create(activated, study, config_hash)
        generated = generate_schedule(
            study=study,
            config_version="v1",
            config_hash=config_hash,
            clinician_id="c1",
            allocation_state=ActivatedAllocationState.empty(study.patient_ids),
            starting_arm_counts=NO_STARTS,
        )

        assert [i.assignment_seed for i in stored.schedule.items] == [
            i.assignment_seed for i in generated.items
        ]
        assert all(0 <= i.assignment_seed <= SQLITE_INT_MAX for i in stored.schedule.items)


class TestServiceProvenance:
    def test_refuses_study_without_randomisation(
        self, db: sqlite3.Connection, study_fixture_dir: Path, questions: Questions
    ) -> None:
        plain = load_study_config(study_fixture_dir / "study_synthetic.yaml")
        plain_hash = compute_config_hash_from_models(plain, questions)
        config_history.activate(
            db,
            study_id=plain.study_id,
            config_version="v1",
            config_hash=plain_hash,
            description="plain",
            reason=None,
            study=plain,
            questions=questions,
        )

        with pytest.raises(RandomisationError, match="no 'randomisation'"):
            _create(db, plain, plain_hash)
        assert _dump(db) == ([], [])

    def test_refuses_unbound_database(
        self, db: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        with pytest.raises(StudyIdentityError):
            _create(db, study, config_hash)

    def test_refuses_foreign_study(
        self, db: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        study_identity.bind(db, "another_study")

        with pytest.raises(StudyIdentityError):
            _create(db, study, config_hash)

    def test_refuses_unknown_version(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        with pytest.raises(ConfigurationProvenanceError):
            _create(activated, study, config_hash, config_version="v9")
        assert _dump(activated) == ([], [])

    def test_refuses_hash_mismatch(self, activated: sqlite3.Connection, study: StudyConfig) -> None:
        with pytest.raises(ConfigurationProvenanceError):
            _create(activated, study, FAKE_HASH)

    def test_refuses_study_differing_from_registered_snapshot(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        tampered = study.model_copy(
            update={"randomisation": _study().randomisation},
        )

        with pytest.raises(ConfigurationProvenanceError, match="snapshot"):
            _create(activated, tampered, config_hash)

    def test_refuses_inactive_version(
        self,
        activated: sqlite3.Connection,
        study: StudyConfig,
        questions: Questions,
        config_hash: str,
    ) -> None:
        later = study.model_copy(update={"timepoints": [0.0, 60.0]})
        later_hash = compute_config_hash_from_models(later, questions)
        config_history.activate(
            activated,
            study_id=study.study_id,
            config_version="v2",
            config_hash=later_hash,
            description="drop last timepoint",
            reason=None,
            study=later,
            questions=questions,
        )

        with pytest.raises(StaleConfigurationError):
            _create(activated, study, config_hash, config_version="v1")
        assert _dump(activated) == ([], [])

    def test_later_activation_keeps_existing_schedule(
        self,
        activated: sqlite3.Connection,
        study: StudyConfig,
        questions: Questions,
        config_hash: str,
    ) -> None:
        first = _create(activated, study, config_hash)
        later = study.model_copy(update={"timepoints": [0.0, 60.0]})
        later_hash = compute_config_hash_from_models(later, questions)
        config_history.activate(
            activated,
            study_id=study.study_id,
            config_version="v2",
            config_hash=later_hash,
            description="drop last timepoint",
            reason=None,
            study=later,
            questions=questions,
        )

        assert _create(activated, later, later_hash, config_version="v2") == first
        assert first.schedule.config_version == "v1"

    def test_refuses_open_transaction(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        activated.execute(
            "INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('x', 'x')"
        )

        with pytest.raises(RandomisationError, match="transaction"):
            _create(activated, study, config_hash)
        activated.rollback()


# ---------------------------------------------------------------------------
# Regression: S11c never activates an allocation
# ---------------------------------------------------------------------------


class TestNoActivation:
    def test_schedule_creation_writes_no_arm_assignment(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        _create(activated, study, config_hash)

        assert arm_assignments.fetch_all(activated) == ()

    def test_assignments_stay_phase1_stub(
        self, activated: sqlite3.Connection, study: StudyConfig, config_hash: str
    ) -> None:
        _create(activated, study, config_hash)
        activated.execute(
            "INSERT INTO clinicians (clinician_id, name_normalized) VALUES ('c1', 'dr')"
        )
        activated.commit()

        arm = arm_assignments.assign_or_lookup(
            activated, "c1", "synth_001", config_hash=config_hash, config_version="v1"
        )

        assert arm == ("no_ai", "phase1_stub")
        assert all(
            a.arm_source != "phase2_randomized" for a in arm_assignments.fetch_all(activated)
        )
        assert all(a.seed is None for a in arm_assignments.fetch_all(activated))
