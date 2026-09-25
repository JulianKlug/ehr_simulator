"""S11c: deterministic adaptive-block randomisation scheduler.

Generates one planned patient + arm sequence per clinician. Planned only:
nothing here activates an allocation (S11d does that).

::

    master_seed ──HMAC──► derived_seed ◄── canonical context
                              │            (study, clinician, config,
                              │             algorithm, allocation state,
                              │             starting-arm counts)
          ┌───────────────────┼─────────────────────┐
          ▼                   ▼                     ▼
    starting arm        patient ranking       assignment_seed
    (fewer starts,      (lowest projected     (63-bit, per item;
     HMAC on tie)        imbalance, HMAC       S11d copies it into
          │              on tie)               arm_assignments.seed)
          ▼                   │
    block_sequence ──► arm per position ──► patient per position

Every random decision is an HMAC-SHA256 over canonical JSON, so the
schedule never depends on Python's ``random``, ``hash()`` or UUIDs.
:func:`generate_schedule` is pure; :func:`create_or_fetch_schedule` is the
service that validates provenance and persists through
:mod:`ehr_simulator.db.randomisation`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from ehr_simulator.config.snapshot import render_study_snapshot
from ehr_simulator.config.study import RandomisationConfig, StudyConfig
from ehr_simulator.db import arm_assignments, config_history, study_identity
from ehr_simulator.db import randomisation as schedules
from ehr_simulator.db.exceptions import (
    ConfigurationProvenanceError,
    RandomisationError,
    StaleConfigurationError,
)
from ehr_simulator.db.randomisation import GeneratedSchedule, ScheduleItem, StoredSchedule

__all__ = [
    "RANDOMISATION_ALGORITHM_VERSION",
    "ActivatedAllocationState",
    "Arm",
    "GeneratedSchedule",
    "PatientAllocation",
    "ScheduleIncompatibleError",
    "ScheduleItem",
    "StartingArmCounts",
    "StoredSchedule",
    "create_or_fetch_schedule",
    "generate_schedule",
    "load_activated_allocation_state",
    "require_schedule_compatible",
]

#: Bump whenever allocation behaviour changes; never change behaviour
#: while keeping this label.
RANDOMISATION_ALGORITHM_VERSION = "adaptive_block_v1"

#: ``assignment_seed`` width: fits SQLite's signed 64-bit INTEGER.
_ASSIGNMENT_SEED_BITS = 63
_ASSIGNMENT_SEED_BYTES = 8

_BLOCK_START = "start"


class ScheduleIncompatibleError(RandomisationError):
    """S11d: a stored schedule no longer fits the active configuration.

    Its randomisation settings or algorithm differ, or the selected item's
    patient left the active pool. Never regenerated or reinterpreted.
    """


class Arm(StrEnum):
    AI = "ai"
    NO_AI = "no_ai"


_OPPOSITE_ARM = {Arm.AI: Arm.NO_AI, Arm.NO_AI: Arm.AI}


class _Purpose(StrEnum):
    """HMAC domain-separation labels: one independent stream per decision."""

    STARTING_ARM = "starting_arm"
    PATIENT_RANK = "patient_rank"
    ASSIGNMENT_SEED = "assignment_seed"


# ---------------------------------------------------------------------------
# Scheduler inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatientAllocation:
    """Activated (measured) case counts for one patient — never planned ones."""

    patient_id: str
    ai_count: int
    no_ai_count: int


@dataclass(frozen=True)
class ActivatedAllocationState:
    """Per-patient activated arm counts the scheduler balances against.

    S11c takes it explicitly; S11d derives it from realised assignments.
    """

    patients: tuple[PatientAllocation, ...]

    @classmethod
    def empty(cls, patient_ids: Iterable[str]) -> ActivatedAllocationState:
        """No activated cases yet: every patient at ``0 / 0``."""
        return cls(tuple(PatientAllocation(pid, 0, 0) for pid in patient_ids))

    @classmethod
    def from_counts(cls, counts: Mapping[str, tuple[int, int]]) -> ActivatedAllocationState:
        """Build from ``{patient_id: (ai_count, no_ai_count)}``."""
        return cls(tuple(PatientAllocation(pid, ai, no_ai) for pid, (ai, no_ai) in counts.items()))

    def canonical(self) -> list[dict[str, object]]:
        """Order-independent JSON-ready form (sorted by patient id)."""
        return [
            {"patient_id": p.patient_id, "ai_count": p.ai_count, "no_ai_count": p.no_ai_count}
            for p in sorted(self.patients, key=lambda p: p.patient_id)
        ]

    def counts_for(self, patient_id: str) -> PatientAllocation:
        for p in self.patients:
            if p.patient_id == patient_id:
                return p

        raise RandomisationError(f"allocation state has no entry for patient {patient_id!r}")

    def require_covers(self, patient_ids: list[str]) -> None:
        """Exactly one well-formed entry per configured patient, nothing else."""
        seen = [p.patient_id for p in self.patients]
        if len(set(seen)) != len(seen):
            raise RandomisationError("allocation state lists a patient more than once")

        if set(seen) != set(patient_ids):
            missing = sorted(set(patient_ids) - set(seen))
            extra = sorted(set(seen) - set(patient_ids))
            raise RandomisationError(
                "allocation state must cover exactly the configured patients; "
                f"missing {missing}, unexpected {extra}"
            )

        for p in self.patients:
            for count in (p.ai_count, p.no_ai_count):
                if type(count) is not int or count < 0:
                    raise RandomisationError(
                        f"allocation counts must be non-negative integers; "
                        f"patient {p.patient_id!r} has {count!r}"
                    )


@dataclass(frozen=True)
class StartingArmCounts:
    """How many previously generated schedules started on each arm."""

    ai: int
    no_ai: int

    def canonical(self) -> dict[str, int]:
        return {Arm.AI.value: self.ai, Arm.NO_AI.value: self.no_ai}


# ---------------------------------------------------------------------------
# Deterministic primitives
# ---------------------------------------------------------------------------


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _hmac_digest(key: bytes, purpose: _Purpose, *parts: object) -> bytes:
    """HMAC-SHA256 of ``[purpose, *parts]`` as canonical JSON."""
    message = _canonical_json([purpose.value, *parts]).encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).digest()


# ---------------------------------------------------------------------------
# Schedule construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    """One planned position before a patient is chosen for it."""

    case_position: int
    planned_arm: Arm
    block_number: int
    position_in_block: int
    preceding_block_arm: Arm | None


def _choose_starting_arm(counts: StartingArmCounts, key: bytes) -> Arm:
    """Less-represented starting arm; HMAC coin flip on a tie."""
    if counts.ai < counts.no_ai:
        return Arm.AI

    if counts.no_ai < counts.ai:
        return Arm.NO_AI

    coin = _hmac_digest(key, _Purpose.STARTING_ARM)[0] % 2
    return Arm.AI if coin == 0 else Arm.NO_AI


def _expand_slots(config: RandomisationConfig, starting_arm: Arm, n_positions: int) -> list[_Slot]:
    """Repeat ``block_sequence`` until ``n_positions`` slots exist.

    Example: length 2, ``[start, other]``, start ``ai``, 5 positions →
    ``ai ai | no_ai no_ai | ai`` (the last block is truncated).
    """
    slots: list[_Slot] = []
    for index in range(n_positions):
        block_index = index // config.block_length
        entry = config.block_sequence[block_index % len(config.block_sequence)]
        arm = starting_arm if entry == _BLOCK_START else _OPPOSITE_ARM[starting_arm]

        preceding = None
        if block_index > 0:
            previous_entry = config.block_sequence[(block_index - 1) % len(config.block_sequence)]
            preceding = (
                starting_arm if previous_entry == _BLOCK_START else _OPPOSITE_ARM[starting_arm]
            )

        slots.append(
            _Slot(
                case_position=index + 1,
                planned_arm=arm,
                block_number=block_index + 1,
                position_in_block=index % config.block_length + 1,
                preceding_block_arm=preceding,
            )
        )
    return slots


def _projected_imbalance(allocation: PatientAllocation, arm: Arm) -> int:
    """``|ai - no_ai|`` after one more case on ``arm`` (activated counts only)."""
    ai = allocation.ai_count + (1 if arm is Arm.AI else 0)
    no_ai = allocation.no_ai_count + (1 if arm is Arm.NO_AI else 0)
    return abs(ai - no_ai)


def _assign_patients(
    slots: list[_Slot],
    patient_ids: list[str],
    state: ActivatedAllocationState,
    key: bytes,
) -> list[str]:
    """Greedy per slot: lowest projected imbalance, HMAC rank breaks ties."""
    remaining = list(patient_ids)
    chosen: list[str] = []
    for slot in slots:
        best = min(
            remaining,
            key=lambda pid: (
                _projected_imbalance(state.counts_for(pid), slot.planned_arm),
                _hmac_digest(key, _Purpose.PATIENT_RANK, slot.case_position, pid),
            ),
        )
        remaining.remove(best)
        chosen.append(best)
    return chosen


def _assignment_seed(key: bytes, case_position: int, patient_id: str, arm: Arm) -> int:
    digest = _hmac_digest(key, _Purpose.ASSIGNMENT_SEED, case_position, patient_id, arm.value)
    raw = int.from_bytes(digest[:_ASSIGNMENT_SEED_BYTES], "big")
    return raw >> (_ASSIGNMENT_SEED_BYTES * 8 - _ASSIGNMENT_SEED_BITS)


def generate_schedule(
    *,
    study: StudyConfig,
    config_version: str,
    config_hash: str,
    clinician_id: str,
    allocation_state: ActivatedAllocationState,
    starting_arm_counts: StartingArmCounts,
) -> GeneratedSchedule:
    """Pure: same inputs → byte-identical schedule. No database access.

    Raises:
        RandomisationError: ``study.randomisation`` is absent, or the
            allocation state does not cover exactly ``study.patient_ids``.
    """
    config = study.randomisation
    if config is None:
        raise RandomisationError(
            f"study {study.study_id!r} has no 'randomisation' settings; cannot generate a schedule"
        )

    allocation_state.require_covers(study.patient_ids)
    allocation_canonical = allocation_state.canonical()

    # The context names the schedule (SHA256) and keys every decision (HMAC).
    context_json = _canonical_json(
        {
            "study_id": study.study_id,
            "clinician_id": clinician_id,
            "config_version": config_version,
            "config_hash": config_hash,
            "algorithm_version": RANDOMISATION_ALGORITHM_VERSION,
            "activated_allocation_state": allocation_canonical,
            "starting_arm_counts": starting_arm_counts.canonical(),
        }
    ).encode("utf-8")
    schedule_id = hashlib.sha256(context_json).hexdigest()
    master_key = str(config.master_seed).encode("utf-8")
    derived_seed_hex = hmac.new(master_key, context_json, hashlib.sha256).hexdigest()
    key = bytes.fromhex(derived_seed_hex)

    starting_arm = _choose_starting_arm(starting_arm_counts, key)
    slots = _expand_slots(config, starting_arm, len(study.patient_ids))
    patients = _assign_patients(slots, study.patient_ids, allocation_state, key)

    # planned_cases_since_ai: None before any AI slot, 0 on AI, else the
    # distance to the most recent AI slot (ai, no_ai, no_ai → 0, 1, 2).
    items: list[ScheduleItem] = []
    last_ai_position: int | None = None
    for slot, patient_id in zip(slots, patients, strict=True):
        if slot.planned_arm is Arm.AI:
            last_ai_position = slot.case_position

        since_ai = None if last_ai_position is None else slot.case_position - last_ai_position
        items.append(
            ScheduleItem(
                case_position=slot.case_position,
                patient_id=patient_id,
                planned_arm=slot.planned_arm.value,
                block_number=slot.block_number,
                position_in_block=slot.position_in_block,
                preceding_block_arm=(
                    None if slot.preceding_block_arm is None else slot.preceding_block_arm.value
                ),
                planned_cases_since_ai=since_ai,
                assignment_seed=_assignment_seed(
                    key, slot.case_position, patient_id, slot.planned_arm
                ),
            )
        )

    return GeneratedSchedule(
        schedule_id=schedule_id,
        study_id=study.study_id,
        clinician_id=clinician_id,
        config_version=config_version,
        config_hash=config_hash,
        algorithm_version=RANDOMISATION_ALGORITHM_VERSION,
        master_seed=config.master_seed,
        derived_seed_hex=derived_seed_hex,
        allocation_state_json=_canonical_json(allocation_canonical),
        starting_ai_count=starting_arm_counts.ai,
        starting_no_ai_count=starting_arm_counts.no_ai,
        starting_arm=starting_arm.value,
        block_length=config.block_length,
        block_sequence_json=_canonical_json(config.block_sequence),
        items=tuple(items),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


def _count_starting_arms(stored: tuple[StoredSchedule, ...]) -> StartingArmCounts:
    starts = [s.schedule.starting_arm for s in stored]
    return StartingArmCounts(ai=starts.count(Arm.AI.value), no_ai=starts.count(Arm.NO_AI.value))


def _require_active_configuration(
    conn: sqlite3.Connection, study: StudyConfig, config_version: str, config_hash: str
) -> None:
    """The (version, hash) is registered, matches ``study``, and is active."""
    registered = config_history.require_known(conn, config_version, config_hash)
    if registered.study_json != render_study_snapshot(study):
        raise ConfigurationProvenanceError(
            f"the study config differs from the snapshot registered as {config_version!r}; "
            "refusing to generate a schedule under unregistered settings"
        )

    active = config_history.fetch_active(conn)
    if active is None or active.config_version != config_version:
        raise StaleConfigurationError(
            f"config_version {config_version!r} is not the active configuration; "
            "new schedules are generated only under the active one"
        )


AllocationStateLoader = Callable[[sqlite3.Connection], ActivatedAllocationState]


def load_activated_allocation_state(
    conn: sqlite3.Connection, patient_ids: Iterable[str]
) -> ActivatedAllocationState:
    """Realised Phase 2 counts covering exactly ``patient_ids``.

    Patients without activations are ``0 / 0``; activations of patients
    outside the pool are excluded (``require_covers`` wants exactly the pool).
    """
    counts = arm_assignments.activated_arm_counts(conn)
    return ActivatedAllocationState.from_counts(
        {pid: counts.get(pid, (0, 0)) for pid in patient_ids}
    )


def require_schedule_compatible(
    schedule: GeneratedSchedule, study: StudyConfig, item: ScheduleItem
) -> None:
    """A schedule generated earlier may serve ``item`` under ``study``.

    Unrelated study content may change; randomisation settings, the
    algorithm and the item patient's pool membership may not.

    Raises:
        ScheduleIncompatibleError: any of those changed.
    """
    config = study.randomisation
    if config is None:
        raise ScheduleIncompatibleError("the active configuration has no randomisation settings")

    settings_match = (
        schedule.master_seed == config.master_seed
        and schedule.block_length == config.block_length
        and schedule.block_sequence_json == _canonical_json(config.block_sequence)
    )
    if not settings_match:
        raise ScheduleIncompatibleError(
            "the active randomisation settings differ from those the schedule was generated under"
        )

    if schedule.algorithm_version != RANDOMISATION_ALGORITHM_VERSION:
        raise ScheduleIncompatibleError(
            f"the schedule was generated by {schedule.algorithm_version!r}, "
            f"not {RANDOMISATION_ALGORITHM_VERSION!r}"
        )

    if item.patient_id not in study.patient_ids:
        raise ScheduleIncompatibleError(
            "the next scheduled patient is not in the active configuration's patient pool"
        )


def create_or_fetch_schedule(
    conn: sqlite3.Connection,
    *,
    study: StudyConfig,
    config_version: str,
    config_hash: str,
    clinician_id: str,
    load_allocation_state: AllocationStateLoader,
) -> StoredSchedule:
    """Return the clinician's schedule, generating and storing it if absent.

    An existing schedule is returned unchanged — never recalculated. The
    starting-arm count, allocation-state read, generation and insert run
    under one ``BEGIN IMMEDIATE`` so concurrent enrolments or activations
    cannot slip between the read and the stored ``allocation_state_json``
    (S11d: the state is loaded inside the lock, only when generating).

    Raises:
        StudyIdentityError: the database belongs to another study.
        RandomisationError: no ``randomisation`` settings, a bad allocation
            state, or the connection already holds an open transaction.
        ConfigurationProvenanceError: unknown/mismatched version or hash.
        StaleConfigurationError: the version is not the active one.
    """
    study_identity.require(conn, study.study_id)
    if conn.in_transaction:
        raise RandomisationError(
            "create_or_fetch_schedule owns its transaction; commit or roll back first"
        )

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = schedules.fetch_for_clinician(conn, study.study_id, clinician_id)
        if existing is not None:
            conn.rollback()
            return existing

        if study.randomisation is None:
            raise RandomisationError(
                f"study {study.study_id!r} has no 'randomisation' settings; "
                "cannot generate a schedule"
            )

        _require_active_configuration(conn, study, config_version, config_hash)
        generated = generate_schedule(
            study=study,
            config_version=config_version,
            config_hash=config_hash,
            clinician_id=clinician_id,
            allocation_state=load_allocation_state(conn),
            starting_arm_counts=_count_starting_arms(
                schedules.list_schedules(conn, study.study_id)
            ),
        )
    except Exception:
        conn.rollback()
        raise

    return schedules.insert_schedule(conn, generated)
