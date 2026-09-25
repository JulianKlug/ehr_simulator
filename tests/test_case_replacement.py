"""S11f: replacement planning and activation for incomplete cases.

Runs on ``study_lifecycle.yaml`` with ``replacement_cases_enabled: true``
(synth_001..003, block pattern start/other with block_length 1, so the
schedule's arms alternate ``A, B, A``)::

    pos 1 (A) times out ──► plan pos 3 (A, same arm) ahead of pos 2 (B)
    Start case          ──► activates pos 3, then pos 2 as usual

Test numbers refer to ``specs/session-11f-replacement-cases.md``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from ehr_simulator import replacement
from ehr_simulator.config import compute_config_hash_from_models, load_questions
from ehr_simulator.config.study import CaseLifecycleConfig
from ehr_simulator.db import arm_assignments, randomisation
from ehr_simulator.db import case_lifecycle as lifecycle_dao
from ehr_simulator.db import replacements as replacements_dao
from ehr_simulator.db.case_lifecycle import CaseState
from ehr_simulator.db.randomisation import ScheduleItem
from ehr_simulator.replacement import ArmCounts, select_replacement
from ehr_simulator.web import case_contact, case_start
from tests.test_case_lifecycle import (
    GRACE,
    VALID_LIFECYCLE,
    LifecycleHarness,
    _abandon,
    _cli,
    _finish,
    _study,
    _url,
    _with_lifecycle,
)
from tests.test_case_start import (
    HTTP_CONFLICT,
    HTTP_SEE_OTHER,
    INDEX_URL,
    _start,
    _started_patient,
)

SCHEDULE_TABLES = ("randomisation_schedules", "randomisation_schedule_items")


@pytest.fixture
def rh(tmp_path: Path, study_fixture_dir: Path) -> LifecycleHarness:
    return _replacing(tmp_path, study_fixture_dir)


def _replacing(tmp_path: Path, study_fixture_dir: Path, **overrides: Any) -> LifecycleHarness:
    return _with_lifecycle(tmp_path, study_fixture_dir, replacement_cases_enabled=True, **overrides)


def _time_out(lh: LifecycleHarness, client, patient_id: str) -> None:
    """Let the case fall silent past its grace; the heartbeat discovers it."""
    lh.clock.advance(GRACE + 1)
    client.post(f"/case/{patient_id}/heartbeat")
    assert lh.lifecycle(patient_id).state is CaseState.INCOMPLETE


def _plans(lh: LifecycleHarness):
    with lh.conn() as conn:
        return replacements_dao.list_for_clinician(conn, lh.clinician_id)


def _item_at(lh: LifecycleHarness, position: int) -> ScheduleItem:
    return next(i for i in lh.schedule().items if i.case_position == position)


def _schedule_rows(lh: LifecycleHarness) -> dict[str, list[tuple]]:
    with lh.conn() as conn:
        return {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
            for t in SCHEDULE_TABLES
        }


def _item(position: int, patient_id: str, arm: str) -> ScheduleItem:
    return ScheduleItem(position, patient_id, arm, position, 1, None, None, position)


# ---------------------------------------------------------------------------
# Eligibility and linkage (#1-#8)
# ---------------------------------------------------------------------------


def test_incomplete_case_plans_one_same_arm_replacement(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)

    first, second, third = (_item_at(rh, p) for p in (1, 2, 3))
    assert first.patient_id == original
    assert first.planned_arm == third.planned_arm != second.planned_arm

    (plan,) = _plans(rh)
    assert plan.original_patient_id == original
    assert plan.replacement_patient_id == third.patient_id
    assert plan.replacement_case_position == third.case_position
    assert plan.planned_arm == third.planned_arm
    assert plan.generated_at == rh.clock() and plan.is_pending
    assert rh.events("case.replacement_planned") == [
        {
            "replacement_id": plan.replacement_id,
            "replacement_patient_id": third.patient_id,
            "replacement_case_position": third.case_position,
        }
    ]


def test_no_plan_when_replacements_are_disabled(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _with_lifecycle(tmp_path, study_fixture_dir)
    with lh.client() as client:
        _time_out(lh, client, _started_patient(_start(client)))
        _start(client)

    assert _plans(lh) == ()


def test_completed_case_never_plans_a_replacement(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _finish(client, _started_patient(_start(client)))
        _start(client)

    assert _plans(rh) == ()
    with rh.conn() as conn, pytest.raises(Exception, match="only incomplete"):
        replacement.plan_replacement(
            conn,
            clinician_id=rh.clinician_id,
            original_patient_id=_item_at(rh, 1).patient_id,
            now=rh.clock(),
        )


def test_replacement_is_a_patient_the_clinician_never_held(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)

    (plan,) = _plans(rh)
    assert plan.replacement_patient_id not in {a.patient_id for a in rh.assignments()}


def test_original_case_is_never_rewritten(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)
        assignment_before = rh.assignments()
        lifecycle_before = rh.lifecycle(original)
        _start(client)

    assert rh.assignments()[0] == assignment_before[0]
    assert rh.lifecycle(original) == lifecycle_before


def test_link_is_navigable_in_both_directions(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)
        replacement_patient = _started_patient(_start(client))

    (plan,) = _plans(rh)
    assert (plan.original_patient_id, plan.replacement_patient_id) == (
        original,
        replacement_patient,
    )
    assert (
        rh.count(
            "SELECT COUNT(*) FROM case_replacements WHERE clinician_id = ? "
            "AND replacement_patient_id = ?",
            (rh.clinician_id, replacement_patient),
        )
        == 1
    )


def test_replanning_the_same_original_is_idempotent(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)

    (plan,) = _plans(rh)
    with rh.conn() as conn:
        again = replacement.plan_replacement(
            conn, clinician_id=rh.clinician_id, original_patient_id=original, now=rh.clock()
        )
    assert again == plan
    assert len(rh.events("case.replacement_planned")) == 1


def test_a_replacement_may_itself_be_replaced(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _time_out(rh, client, original)
        first_replacement = _started_patient(_start(client))
        _time_out(rh, client, first_replacement)
        second_replacement = _started_patient(_start(client))

    plans = _plans(rh)
    assert [(p.original_patient_id, p.replacement_patient_id) for p in plans] == [
        (original, first_replacement),
        (first_replacement, second_replacement),
    ]
    assert second_replacement == _item_at(rh, 2).patient_id  # the only one left


# ---------------------------------------------------------------------------
# Selection (#9-#15)
# ---------------------------------------------------------------------------


def _select(candidates, **overrides: Any):
    kwargs: dict[str, Any] = {
        "original_patient_id": "orig",
        "original_arm": "ai",
        "clinician_counts": ArmCounts(),
        "patient_counts": {},
        "next_nominal_position": 2,
        "schedule_key_hex": "00" * 32,
    }
    return select_replacement(candidates, **{**kwargs, **overrides})


def test_same_arm_beats_a_closer_opposite_arm_candidate() -> None:
    near_other = _item(2, "p2", "no_ai")
    far_same = _item(9, "p9", "ai")
    assert _select([near_other, far_same]) == far_same


def test_patient_imbalance_breaks_the_next_tie() -> None:
    near = _item(2, "p2", "ai")
    far = _item(5, "p5", "ai")
    counts = {"p2": ArmCounts(ai=2), "p5": ArmCounts(no_ai=1)}
    assert _select([near, far], patient_counts=counts) == far


def test_sequence_distance_breaks_the_next_tie() -> None:
    near = _item(3, "p3", "ai")
    far = _item(6, "p6", "ai")
    assert _select([far, near]) == near


def test_final_ties_are_deterministic() -> None:
    below = _item(1, "p1", "ai")
    above = _item(3, "p3", "ai")
    winner = _select([below, above])
    assert winner in (below, above)
    assert all(_select([above, below]) == winner for _ in range(5))

    from ehr_simulator.randomisation import replacement_tie_rank

    ranks = {
        i: replacement_tie_rank(
            "00" * 32,
            original_patient_id="orig",
            case_position=i.case_position,
            patient_id=i.patient_id,
            planned_arm=i.planned_arm,
        )
        for i in (below, above)
    }
    assert winner == min(ranks, key=ranks.get)


def test_same_inputs_reproduce_the_same_plan(tmp_path: Path, study_fixture_dir: Path) -> None:
    ids = []
    for run in ("a", "b"):
        (tmp_path / run).mkdir()
        lh = _replacing(tmp_path / run, study_fixture_dir)
        with lh.client() as client:
            _time_out(lh, client, _started_patient(_start(client)))
        ids.append(_plans(lh)[0].replacement_id)

    assert ids[0] == ids[1]


def test_planning_and_activation_never_touch_the_schedule(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        before = _schedule_rows(rh)
        _time_out(rh, client, original)
        _start(client)

    assert _schedule_rows(rh) == before


def test_no_eligible_candidate_plans_nothing(tmp_path: Path, study_fixture_dir: Path) -> None:
    lh = _replacing(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=3,
        max_activated_cases_per_clinician=3,
    )
    with lh.client() as client:
        for _ in range(2):
            _finish(client, _started_patient(_start(client)))
        last = _started_patient(_start(client))
        _time_out(lh, client, last)
        index = client.get(INDEX_URL).text

    assert _plans(lh) == ()
    assert lh.events("case.replacement_planned") == []
    assert "incomplete · no replacement" in index


# ---------------------------------------------------------------------------
# Activation (#16-#22)
# ---------------------------------------------------------------------------


def test_pending_replacement_precedes_the_next_ordinary_item(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        pending_index = client.get(INDEX_URL).text
        replacement_patient = _started_patient(_start(client))
        _finish(client, replacement_patient)
        ordinary = _started_patient(_start(client))
        replaced_index = client.get(INDEX_URL).text

    assert replacement_patient == _item_at(rh, 3).patient_id
    assert ordinary == _item_at(rh, 2).patient_id
    assert "incomplete · replacement pending" in pending_index
    assert "incomplete · replaced" in replaced_index


def test_replacement_still_needs_an_explicit_start(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        (plan,) = _plans(rh)
        page = client.get(_url(plan.replacement_patient_id), follow_redirects=False)

    assert page.status_code == HTTP_SEE_OTHER
    assert page.headers["location"] == INDEX_URL
    assert len(rh.assignments()) == 1


def test_replacement_copies_the_schedule_item(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        replacement_patient = _started_patient(_start(client))

    item = _item_at(rh, 3)
    activated = next(a for a in rh.assignments() if a.patient_id == replacement_patient)
    assert (activated.arm, activated.seed, activated.schedule_id, activated.case_position) == (
        item.planned_arm,
        item.assignment_seed,
        rh.schedule().schedule_id,
        item.case_position,
    )
    assert activated.config_version == "v1"
    (plan,) = _plans(rh)
    assert plan.activated_at == rh.clock()
    activation = rh.events("case.activated")[-1]
    assert activation["replacement_id"] == plan.replacement_id
    assert rh.lifecycle(replacement_patient).state is CaseState.ACTIVE


def test_replacement_activation_is_atomic_and_idempotent(rh: LifecycleHarness, monkeypatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("session insert failed")

    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        original_start = case_start.sessions.start_or_resume
        monkeypatch.setattr(case_start.sessions, "start_or_resume", boom)
        with pytest.raises(RuntimeError):
            _start(client)
        assert _plans(rh)[0].is_pending
        assert len(rh.assignments()) == 1

        monkeypatch.setattr(case_start.sessions, "start_or_resume", original_start)
        first = _started_patient(_start(client))
        again = _started_patient(_start(client))

    assert first == again
    assert len(rh.assignments()) == 2
    assert not _plans(rh)[0].is_pending


def test_replacement_counts_toward_activated_cases(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        _start(client)

    with rh.conn() as conn:
        assert lifecycle_dao.counts_for_clinician(conn, rh.clinician_id).activated == 2


def test_maximum_activated_count_blocks_the_replacement(
    tmp_path: Path, study_fixture_dir: Path
) -> None:
    lh = _replacing(
        tmp_path,
        study_fixture_dir,
        target_completed_cases_per_clinician=1,
        max_activated_cases_per_clinician=1,
    )
    with lh.client() as client:
        _time_out(lh, client, _started_patient(_start(client)))
        refused = _start(client)

    assert refused.status_code == HTTP_CONFLICT
    assert _plans(lh)[0].is_pending
    assert len(lh.assignments()) == 1


def test_invalidated_replacement_refuses_without_substitution(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
    (plan,) = _plans(rh)

    pool = [p for p in rh.v1.study.patient_ids if p != plan.replacement_patient_id]
    v2 = rh.variant("v2", patient_ids=pool)
    rh.activate(v2)
    with rh.client(v2) as client:
        refused = _start(client)

    assert refused.status_code == HTTP_CONFLICT
    assert len(rh.assignments()) == 1
    assert _plans(rh)[0].is_pending


# ---------------------------------------------------------------------------
# Balance and regression (#23-#26c)
# ---------------------------------------------------------------------------


def _study_counts(lh: LifecycleHarness) -> dict[str, tuple[int, int]]:
    with lh.conn() as conn:
        return arm_assignments.activated_arm_counts(conn)


def test_balance_counts_original_and_activated_replacement_only(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        after_original = _study_counts(rh)
        _time_out(rh, client, original)
        after_planning = _study_counts(rh)
        replacement_patient = _started_patient(_start(client))
        after_activation = _study_counts(rh)

    assert original in after_original
    assert after_planning == after_original  # a pending plan never counts
    assert original in after_activation and replacement_patient in after_activation
    assert sum(ai + no_ai for ai, no_ai in after_activation.values()) == 2


def test_clinician_never_receives_a_duplicate_patient(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        for _ in range(3):
            patient_id = _started_patient(_start(client))
            _time_out(rh, client, patient_id)

    held = [a.patient_id for a in rh.assignments()]
    assert len(held) == len(set(held)) == 3


def test_enabling_replacements_changes_the_hash_only_when_true(study_fixture_dir: Path) -> None:
    questions = load_questions(study_fixture_dir / "questions.yaml")
    implicit = compute_config_hash_from_models(_study(**VALID_LIFECYCLE), questions)
    explicit_false = compute_config_hash_from_models(
        _study(**VALID_LIFECYCLE, replacement_cases_enabled=False), questions
    )
    enabled = compute_config_hash_from_models(
        _study(**VALID_LIFECYCLE, replacement_cases_enabled=True), questions
    )
    assert implicit == explicit_false != enabled
    assert "replacement_cases_enabled" not in CaseLifecycleConfig(**VALID_LIFECYCLE).model_dump()


def test_pending_plan_reserves_its_position(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
    (plan,) = _plans(rh)

    with rh.conn() as conn:
        schedule = randomisation.fetch_for_clinician(
            conn, rh.v1.study.study_id, rh.clinician_id
        ).schedule
        candidates, _nominal = replacement.eligible_candidates(
            conn,
            schedule,
            clinician_id=rh.clinician_id,
            active_patient_ids=rh.v1.study.patient_ids,
        )
    assert plan.replacement_case_position not in {c.case_position for c in candidates}


def test_lost_plan_heals_on_the_next_start(rh: LifecycleHarness, monkeypatch) -> None:
    def crash(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("planner crashed")

    with rh.client() as client:
        original = _started_patient(_start(client))
        real_plan = case_contact.replacement.plan_replacement
        monkeypatch.setattr(case_contact.replacement, "plan_replacement", crash)
        _time_out(rh, client, original)
        assert _plans(rh) == ()

        monkeypatch.setattr(case_contact.replacement, "plan_replacement", real_plan)
        replacement_patient = _started_patient(_start(client))

    assert replacement_patient == _item_at(rh, 3).patient_id
    assert _plans(rh)[0].original_patient_id == original


def test_replacement_table_is_immutable(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        _time_out(rh, client, _started_patient(_start(client)))
        _start(client)

    with rh.conn() as conn:
        for sql in (
            "UPDATE case_replacements SET planned_arm = 'no_ai'",
            "UPDATE case_replacements SET activated_at = NULL",
            "DELETE FROM case_replacements",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
            conn.rollback()


def test_abandon_case_plans_the_replacement(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))

    result = _cli(
        rh, "abandon-case", str(rh.v1.study_yaml), "--clinician", "Dr. Test", "--patient", original
    )
    assert result.exit_code == 0, result.output
    (plan,) = _plans(rh)
    assert f"Replacement planned: {plan.replacement_patient_id}." in result.stdout


def test_service_abandon_then_start_activates_the_replacement(rh: LifecycleHarness) -> None:
    with rh.client() as client:
        original = _started_patient(_start(client))
        _abandon(rh, original)  # service only: no plan yet
        assert _plans(rh) == ()
        replacement_patient = _started_patient(_start(client))

    assert _plans(rh)[0].replacement_patient_id == replacement_patient
