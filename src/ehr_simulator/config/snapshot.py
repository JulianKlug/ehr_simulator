"""S11b configuration snapshots: reversible render + parse of study/questions.

An activation freezes *the parsed models* (not the YAML files) as two JSON
snapshots in ``configuration_history``:

- ``study_json`` — ``StudyConfig.model_dump_json(...)`` with the deployment
  paths (``csv_path``, ``params_dir``, ``db_path``) excluded — the exact
  canonical payload :func:`config.loader.compute_config_hash_from_models`
  hashes, so stored ``config_hash`` and stored snapshot always agree by
  construction.
- ``questions_json`` — ``Questions.model_dump_json(by_alias=True)``.

Both directions are locked down: ``render_*`` produce the canonical bytes,
``parse_*`` re-validate through the strict Pydantic models (``extra="forbid"``),
and a snapshot that cannot be parsed back — or that would not re-render to
the same bytes — is a stored-state integrity failure, never a fallback.
"""

from __future__ import annotations

import re

from ehr_simulator.config.exceptions import ConfigValidationError
from ehr_simulator.config.questions import Questions
from ehr_simulator.config.study import StudyConfig

__all__ = [
    "CONFIG_VERSION_PATTERN",
    "DESCRIPTION_MAX_CHARS",
    "REASON_MAX_CHARS",
    "parse_questions_snapshot",
    "parse_study_snapshot",
    "render_questions_snapshot",
    "render_study_snapshot",
    "validate_config_version",
    "validate_description",
    "validate_reason",
]

#: Activation version label: alphanumeric start, then alphanumerics, dot,
#: underscore, dash; 1..64 characters total. Case-insensitive on purpose —
#: version labels are human-written identifiers, not lowercased ids.
CONFIG_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

DESCRIPTION_MAX_CHARS = 500
REASON_MAX_CHARS = 1000

# Deployment/environment paths — excluded from the study snapshot, mirroring
# ``compute_config_hash_from_models`` (they are not part of the study
# definition).
_STUDY_SNAPSHOT_EXCLUDES = {"csv_path", "params_dir", "db_path"}


def validate_config_version(value: object) -> str:
    """Return ``value`` if it is a valid version label; raise otherwise."""
    if not isinstance(value, str) or not CONFIG_VERSION_PATTERN.fullmatch(value):
        raise ConfigValidationError(
            f"config_version must match {CONFIG_VERSION_PATTERN.pattern} "
            "(start alphanumeric; letters, digits, '.', '_', '-' allowed; "
            f"1..64 characters); got {value!r}"
        )
    return value


def validate_description(value: object) -> str:
    """Non-blank string, at most :data:`DESCRIPTION_MAX_CHARS` characters."""
    if not isinstance(value, str):
        raise ConfigValidationError(f"change description must be a string, got {value!r}")
    if not value.strip():
        raise ConfigValidationError("change description must be non-blank")
    if len(value) > DESCRIPTION_MAX_CHARS:
        raise ConfigValidationError(
            f"change description must be at most {DESCRIPTION_MAX_CHARS} characters, "
            f"got {len(value)}"
        )
    return value


def validate_reason(value: object) -> str | None:
    """Optional; when present it must be non-blank after trimming and at most
    :data:`REASON_MAX_CHARS` characters. A whitespace-only reason is a
    validation failure (spec: provided reason may not be empty) — it is never
    silently dropped."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigValidationError(f"change reason must be a string or omitted, got {value!r}")
    if not value.strip():
        raise ConfigValidationError("change reason must be non-blank when provided")
    if len(value) > REASON_MAX_CHARS:
        raise ConfigValidationError(
            f"change reason must be at most {REASON_MAX_CHARS} characters, got {len(value)}"
        )
    return value


def render_study_snapshot(study: StudyConfig) -> str:
    """Canonical JSON snapshot of a parsed study config (deployment paths excluded)."""
    return study.model_dump_json(by_alias=True, exclude=_STUDY_SNAPSHOT_EXCLUDES)


def parse_study_snapshot(payload: str) -> StudyConfig:
    """Re-validate a stored study snapshot back into a strict ``StudyConfig``.

    Raises:
        ValidationError: the snapshot no longer parses (model schema drift,
            tampered JSON, missing deployment-independent fields).
    """
    data = StudyConfig.model_validate_json(payload)
    # Round-trip must be byte-stable: a snapshot that parses to a model which
    # re-renders differently is a stored-state integrity failure.
    if render_study_snapshot(data) != payload:
        raise ConfigValidationError(
            "stored study snapshot parses, but does not re-render to the same "
            "bytes (stored-state integrity failure)"
        )
    return data


def render_questions_snapshot(questions: Questions) -> str:
    """Canonical JSON snapshot of a parsed questions config."""
    return questions.model_dump_json(by_alias=True)


def parse_questions_snapshot(payload: str) -> Questions:
    """Re-validate a stored questions snapshot back into strict ``Questions``."""
    data = Questions.model_validate_json(payload)
    if render_questions_snapshot(data) != payload:
        raise ConfigValidationError(
            "stored questions snapshot parses, but does not re-render to the same "
            "bytes (stored-state integrity failure)"
        )
    return data
