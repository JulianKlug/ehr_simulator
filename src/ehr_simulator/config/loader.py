"""YAML loaders + canonical config hash.

``compute_config_hash`` hashes the **canonicalized parsed model** (not raw
file bytes). Per /plan-eng-review tension A: the invariant is "same study
definition means same hash", not "same file bytes". Two researchers
re-saving the same study config through different editors (LF vs CRLF,
trailing newline, key reordering) get the same hash. Semantic edits
(different patient_ids, different timepoint, different question prompt) DO
change the hash. Consumed by S6 columns: ``answers.config_hash``,
``events.config_hash``, ``arm_assignments.config_hash``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from pydantic import ValidationError

from ehr_simulator.config.exceptions import ConfigError
from ehr_simulator.config.questions import Questions
from ehr_simulator.config.study import StudyConfig


class _ConfigLoader(yaml.SafeLoader):
    """SafeLoader with the YAML 1.1 ``_``-as-digit-separator int resolver
    removed.

    Geneva's ``case_admission_id`` values look like ``100023_4784``. Under
    pyyaml's default YAML 1.1 parsing those tokens match the int implicit
    resolver (``100023_4784`` → int ``1000234784``, underscore stripped),
    so the original ID is unrecoverable by the time Pydantic sees the data.
    Removing the underscore-aware int pattern lets such tokens fall
    through as plain strings, while plain numerics like ``60`` (in
    ``timepoints``) still parse as ints.
    """


_ConfigLoader.yaml_implicit_resolvers = {
    first_char: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:int"]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_ConfigLoader.add_implicit_resolver(
    "tag:yaml.org,2002:int",
    re.compile(
        r"""^(?:
            [-+]?0b[0-1]+
            |[-+]?0o?[0-7]+
            |[-+]?(?:0|[1-9][0-9]*)
            |[-+]?0x[0-9a-fA-F]+
        )$""",
        re.VERBOSE,
    ),
    list("-+0123456789"),
)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"{path}: file not found")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read file ({exc})") from exc
    try:
        data = yaml.load(text, Loader=_ConfigLoader)  # noqa: S506 - custom safe loader
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML ({exc})") from exc
    if data is None:
        raise ConfigError(f"{path}: file is empty")
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a YAML mapping, got {type(data).__name__}")
    return data


def load_study_config(path: Path) -> StudyConfig:
    """Parse a study_config.yaml and validate via Pydantic.

    Relative ``csv_path`` and ``params_dir`` resolve against ``path.parent``
    (per /plan-eng-review issue 2.2). Mismatched ``schema_version`` surfaces
    a :class:`ConfigError` whose message names both the expected and observed
    versions (D6 D-resolution from S1).
    """
    path = Path(path)
    data = _read_yaml(path)
    observed_version = data.get("schema_version")
    try:
        return StudyConfig.model_validate(data, context={"yaml_dir": path.parent})
    except ValidationError as exc:
        if observed_version is not None and observed_version != "1":
            raise ConfigError(
                f"{path.name}: schema_version mismatch — expected '1', got {observed_version!r}"
            ) from exc
        raise ConfigError.from_validation_error(exc, path=path) from exc


def load_questions(path: Path) -> Questions:
    """Parse a questions.yaml and validate via Pydantic."""
    path = Path(path)
    data = _read_yaml(path)
    observed_version = data.get("schema_version")
    try:
        return Questions.model_validate(data)
    except ValidationError as exc:
        if observed_version is not None and observed_version != "1":
            raise ConfigError(
                f"{path.name}: schema_version mismatch — expected '1', got {observed_version!r}"
            ) from exc
        raise ConfigError.from_validation_error(exc, path=path) from exc


def compute_config_hash(study_path: Path, questions_path: Path) -> str:
    """SHA256 of the canonicalized parsed configs, hex-encoded.

    Computed as ``sha256(study.model_dump_json(...) || 0x00 ||
    questions.model_dump_json(...))``. Pydantic v2's ``model_dump_json``
    produces a deterministic JSON serialization with sorted keys, normalised
    value formatting, and stable booleans/numbers — immune to YAML
    whitespace, key ordering, and editor/OS line-ending differences.

    ``csv_path`` and ``params_dir`` are excluded from the canonical payload:
    they are deployment/environment concerns (where the data lives on this
    machine), not part of the study definition. Two researchers running the
    same dataset enum + same patients + same questions on different machines
    get the same hash.
    """
    study = load_study_config(Path(study_path))
    questions = load_questions(Path(questions_path))
    study_payload = study.model_dump_json(
        by_alias=True,
        exclude={"csv_path", "params_dir"},
    )
    questions_payload = questions.model_dump_json(by_alias=True)
    payload = study_payload.encode("utf-8") + b"\x00" + questions_payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
