"""S12a: read and verify one ``export-phase2`` bundle, read only.

Spec: ``specs/session-12-phase25-release.md`` §4. The only input of every
analysis output; never SQLite, a pseudonym secret or a keyfile::

    manifest.json ─► schema version · study_id · declared file set
    *.csv bytes   ─► SHA256 + data rows (manifest) · frozen header
                  ─► typed frames (_cells)          · practice files dropped
    configuration_history.csv ─► ConfigVersion per version (hash recomputed)
    frames + versions ─► _integrity (keys, joins, provenance, answer values)
                      ─► ValidatedStudyBundle

Any failure raises :class:`AnalysisError` before a result exists. The
input directory is only read.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd

from ehr_simulator.analysis import _integrity
from ehr_simulator.analysis._cells import parse_table
from ehr_simulator.analysis.errors import AnalysisError
from ehr_simulator.analysis.schema import (
    AUDIT,
    CLINICIANS,
    COUNTS,
    EXPORT_SCHEMA_VERSION,
    HISTORY,
    MANIFEST_NAME,
    OPTIONAL_PRACTICE_FILES,
    PANELS,
    REQUIRED_FILES,
    TIMEPOINTS,
    header,
)
from ehr_simulator.config.loader import compute_config_hash_from_models
from ehr_simulator.config.questions import Question, Questions
from ehr_simulator.config.snapshot import parse_questions_snapshot, parse_study_snapshot
from ehr_simulator.config.study import StudyConfig

__all__ = ["ConfigVersion", "ValidatedStudyBundle", "load_phase2_bundle"]

_MANIFEST_KEYS = frozenset(
    {
        "export_schema_version",
        "study_id",
        "generated_at_utc",
        "source_schema_version",
        "included_config_versions",
        "practice_included",
        "files",
    }
)
_FILE_KEYS = frozenset({"rows", "sha256"})
_SHA256 = re.compile(r"[0-9a-f]{64}")
#: Same pattern as ``config.study.STUDY_ID_PATTERN`` (lockstep test).
STUDY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class ConfigVersion:
    """One registered configuration, parsed from its stored snapshots."""

    version: str
    config_hash: str
    activated_at: str
    study: StudyConfig
    questions: Questions

    @property
    def timepoints_minutes(self) -> tuple[float, ...]:
        return tuple(float(t) for t in self.study.timepoints_minutes)

    def question(self, question_id: str) -> Question | None:
        return next((q for q in self.questions.questions if q.question_id == question_id), None)


@dataclass(frozen=True)
class ValidatedStudyBundle:
    """A verified bundle. Frames are typed (``boolean``/``Int64``/``Float64``,
    ``<NA>`` = unknown); callers must not mutate them.

    ``cases``: one row per activated measured case, key
    ``(study_id, clinician_id, patient_id)``. ``answers`` adds
    ``value_number`` (likert, probability) and ``value_options`` (categorical,
    multi-select: tuple of historical options) to the exported columns.
    """

    study_id: str
    manifest_sha256: str
    source_schema_version: int
    practice_declared: bool
    versions: Mapping[str, ConfigVersion]
    cases: pd.DataFrame
    timepoints: pd.DataFrame
    answers: pd.DataFrame
    panels: pd.DataFrame
    audit: pd.DataFrame
    counts: pd.DataFrame
    clinicians: pd.DataFrame

    @property
    def has_cases(self) -> bool:
        return not self.cases.empty


@dataclass(frozen=True)
class _Manifest:
    study_id: str
    source_schema_version: int
    included_config_versions: tuple[str, ...]
    practice_included: bool
    files: Mapping[str, tuple[int, str]]  # name → (rows, sha256)


def load_phase2_bundle(path: Path | str) -> ValidatedStudyBundle:
    """Verify the bundle at *path* and return its typed, linked tables.

    Raises:
        AnalysisError: any contract, digest, header, value or linkage failure.
    """
    root = Path(path)
    if not root.is_dir():
        raise AnalysisError(f"{root} is not a bundle directory")

    manifest_bytes = _read(root / MANIFEST_NAME)
    manifest = _parse_manifest(manifest_bytes)
    _check_file_set(root, manifest)

    frames: dict[str, pd.DataFrame] = {}
    for name, columns in REQUIRED_FILES.items():
        rows = _verified_rows(root, name, header(columns), manifest)
        frames[name] = parse_table(name, columns, rows)
    for name, columns in OPTIONAL_PRACTICE_FILES.items():
        if name in manifest.files:
            _verified_rows(root, name, header(columns), manifest)  # checked, then excluded

    versions = _versions(frames[HISTORY], manifest)
    linked = _integrity.link(manifest.study_id, frames, versions, manifest.practice_included)

    return ValidatedStudyBundle(
        study_id=manifest.study_id,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_schema_version=manifest.source_schema_version,
        practice_declared=manifest.practice_included,
        versions=versions,
        cases=linked.cases,
        timepoints=frames[TIMEPOINTS],
        answers=linked.answers,
        panels=frames[PANELS],
        audit=frames[AUDIT],
        counts=frames[COUNTS],
        clinicians=frames[CLINICIANS],
    )


# ---------------------------------------------------------------------------
# Manifest and files
# ---------------------------------------------------------------------------


def _read(path: Path) -> bytes:
    if not path.is_file():
        raise AnalysisError(f"{path.name} is missing")
    return path.read_bytes()


def _parse_manifest(data: bytes) -> _Manifest:
    try:
        raw: Any = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise AnalysisError(f"{MANIFEST_NAME} is not JSON") from None
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
        raise AnalysisError(f"{MANIFEST_NAME} does not have the {EXPORT_SCHEMA_VERSION} keys")
    if raw["export_schema_version"] != EXPORT_SCHEMA_VERSION:
        raise AnalysisError(
            f"export schema {raw['export_schema_version']!r} is not {EXPORT_SCHEMA_VERSION!r}; "
            "this reader refuses other export schemas"
        )

    study_id = raw["study_id"]
    if not isinstance(study_id, str) or not STUDY_ID_PATTERN.fullmatch(study_id):
        raise AnalysisError(f"{MANIFEST_NAME}: study_id is not a valid study id")
    versions = raw["included_config_versions"]
    if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
        raise AnalysisError(f"{MANIFEST_NAME}: included_config_versions is not a list of names")
    if not isinstance(raw["practice_included"], bool):
        raise AnalysisError(f"{MANIFEST_NAME}: practice_included is not a boolean")
    if not _is_count(raw["source_schema_version"]):
        raise AnalysisError(f"{MANIFEST_NAME}: source_schema_version is not an integer")

    files = raw["files"]
    if not isinstance(files, dict):
        raise AnalysisError(f"{MANIFEST_NAME}: files is not an object")
    declared: dict[str, tuple[int, str]] = {}
    for name, entry in files.items():
        if (
            not isinstance(entry, dict)
            or set(entry) != _FILE_KEYS
            or not _is_count(entry["rows"])
            or not isinstance(entry["sha256"], str)
            or not _SHA256.fullmatch(entry["sha256"])
        ):
            raise AnalysisError(f"{MANIFEST_NAME}: entry of {name!r} is malformed")
        declared[name] = (entry["rows"], entry["sha256"])

    return _Manifest(
        study_id=study_id,
        source_schema_version=raw["source_schema_version"],
        included_config_versions=tuple(versions),
        practice_included=raw["practice_included"],
        files=MappingProxyType(declared),
    )


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _check_file_set(root: Path, manifest: _Manifest) -> None:
    """Declared = the eight required files (+ both practice files when included);
    the directory holds nothing else."""
    declared = set(manifest.files)
    expected = set(REQUIRED_FILES)
    if manifest.practice_included:
        expected |= set(OPTIONAL_PRACTICE_FILES)

    unknown = sorted(declared - expected)
    if unknown:
        raise AnalysisError(f"{MANIFEST_NAME} declares files outside the contract: {unknown}")
    absent = sorted(expected - declared)
    if absent:
        raise AnalysisError(f"{MANIFEST_NAME} does not declare required files: {absent}")

    present = {p.name for p in root.iterdir()} - {MANIFEST_NAME}
    undeclared = sorted(present - declared)
    if undeclared:
        raise AnalysisError(f"bundle directory holds undeclared files: {undeclared}")


def _verified_rows(
    root: Path, name: str, expected_header: tuple[str, ...], manifest: _Manifest
) -> list[list[str]]:
    """Data rows of *name* after the digest, row count and header checks."""
    data = _read(root / name)
    rows_declared, digest = manifest.files[name]
    if hashlib.sha256(data).hexdigest() != digest:
        raise AnalysisError(f"{name}: SHA256 differs from {MANIFEST_NAME}")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise AnalysisError(f"{name} is not UTF-8") from None
    try:
        table = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error:
        raise AnalysisError(f"{name} is not a readable CSV") from None
    if not table:
        raise AnalysisError(f"{name} has no header")

    found_header, rows = tuple(table[0]), table[1:]
    if found_header != expected_header:
        raise AnalysisError(f"{name}: header differs from {EXPORT_SCHEMA_VERSION}")
    if len(rows) != rows_declared:
        raise AnalysisError(
            f"{name}: {len(rows)} data rows, {MANIFEST_NAME} declares {rows_declared}"
        )
    for number, row in enumerate(rows, start=1):
        if len(row) != len(expected_header):
            raise AnalysisError(f"{name}: data row {number} has {len(row)} cells")
    return rows


# ---------------------------------------------------------------------------
# Configuration versions
# ---------------------------------------------------------------------------


def _versions(history: pd.DataFrame, manifest: _Manifest) -> Mapping[str, ConfigVersion]:
    """Every registered version, history order; snapshot hashes recomputed."""
    if history["config_version"].duplicated().any():
        raise AnalysisError(f"{HISTORY}: a config_version is registered twice")
    order = tuple(history["config_version"])
    if order != manifest.included_config_versions:
        raise AnalysisError(f"{MANIFEST_NAME}: included_config_versions differ from {HISTORY}")

    versions: dict[str, ConfigVersion] = {}
    for row in history.itertuples(index=False):
        where = f"{HISTORY}: version {row.config_version!r}"
        try:
            study = parse_study_snapshot(row.study_json)
            questions = parse_questions_snapshot(row.questions_json)
        except Exception as exc:  # noqa: BLE001 — any parse failure refuses
            raise AnalysisError(f"{where} snapshot does not parse: {type(exc).__name__}") from None
        if study.study_id != manifest.study_id:
            raise AnalysisError(f"{where} belongs to another study")
        if compute_config_hash_from_models(study, questions) != row.config_hash:
            raise AnalysisError(f"{where}: config_hash does not match its snapshots")
        versions[row.config_version] = ConfigVersion(
            version=row.config_version,
            config_hash=row.config_hash,
            activated_at=row.activated_at,
            study=study,
            questions=questions,
        )
    return MappingProxyType(versions)
