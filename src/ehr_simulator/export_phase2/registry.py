"""S11n provenance registry: every case reads its pinned configuration."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ehr_simulator.config.questions import Questions
from ehr_simulator.config.snapshot import parse_questions_snapshot, parse_study_snapshot
from ehr_simulator.config.study import StudyConfig
from ehr_simulator.db.arm_assignments import ActivatedAssignment
from ehr_simulator.db.config_history import ConfigHistoryRow
from ehr_simulator.export_phase2.model import Phase2ExportError


@dataclass(frozen=True)
class PinnedConfig:
    row: ConfigHistoryRow
    study: StudyConfig
    questions: Questions

    @property
    def timepoints(self) -> tuple[float, ...]:
        return tuple(float(t) for t in self.study.timepoints_minutes)


def _registry(rows: Sequence[ConfigHistoryRow], study_id: str) -> dict[str, PinnedConfig]:
    registry: dict[str, PinnedConfig] = {}
    for row in rows:
        if row.study_id != study_id:
            raise Phase2ExportError(
                f"configuration {row.config_version!r} belongs to study {row.study_id!r}, "
                f"not {study_id!r}"
            )
        try:
            study = parse_study_snapshot(row.study_json)
            questions = parse_questions_snapshot(row.questions_json)
        except Exception as exc:  # noqa: BLE001 — any parse failure refuses
            raise Phase2ExportError(
                f"stored snapshot of configuration {row.config_version!r} no longer parses: "
                f"{type(exc).__name__}"
            ) from None
        registry[row.config_version] = PinnedConfig(row, study, questions)
    return registry


def _pinned(
    registry: Mapping[str, PinnedConfig],
    version: str | None,
    config_hash: str | None,
    where: str,
) -> PinnedConfig:
    if version is None:
        raise Phase2ExportError(f"{where} has no config_version")
    pinned = registry.get(version)
    if pinned is None:
        raise Phase2ExportError(f"{where} names unknown config_version {version!r}")
    if config_hash != pinned.row.config_hash:
        raise Phase2ExportError(
            f"{where} carries config_hash {str(config_hash)[:8]}… but version {version!r} "
            f"is registered as {pinned.row.config_hash[:8]}…"
        )
    return pinned


def _same_provenance(
    assignment: ActivatedAssignment, version: str | None, config_hash: str, where: str
) -> None:
    if version is None:
        raise Phase2ExportError(f"{where} has no config_version")
    if (version, config_hash) != (assignment.config_version, assignment.config_hash):
        raise Phase2ExportError(
            f"{where} was written under configuration {version!r}, but its case was "
            f"activated under {assignment.config_version!r}"
        )
