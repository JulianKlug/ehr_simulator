"""Public surface for the config package."""

from __future__ import annotations

from ehr_simulator.config.exceptions import ConfigError, ConfigValidationError
from ehr_simulator.config.loader import (
    compute_config_hash,
    compute_config_hash_from_models,
    load_questions,
    load_study_config,
)
from ehr_simulator.config.questions import (
    CategoricalQuestion,
    FreeTextQuestion,
    LikertQuestion,
    MultiSelectQuestion,
    ProbabilityQuestion,
    Question,
    Questions,
    ResponseType,
)
from ehr_simulator.config.snapshot import (
    DESCRIPTION_MAX_CHARS,
    REASON_MAX_CHARS,
    parse_questions_snapshot,
    parse_study_snapshot,
    render_questions_snapshot,
    render_study_snapshot,
    validate_config_version,
    validate_description,
    validate_reason,
)
from ehr_simulator.config.study import StudyConfig

__all__ = [
    "CategoricalQuestion",
    "ConfigError",
    "ConfigValidationError",
    "DESCRIPTION_MAX_CHARS",
    "FreeTextQuestion",
    "REASON_MAX_CHARS",
    "LikertQuestion",
    "MultiSelectQuestion",
    "ProbabilityQuestion",
    "Question",
    "Questions",
    "ResponseType",
    "StudyConfig",
    "compute_config_hash",
    "compute_config_hash_from_models",
    "load_questions",
    "load_study_config",
    "parse_questions_snapshot",
    "parse_study_snapshot",
    "render_questions_snapshot",
    "render_study_snapshot",
    "validate_config_version",
    "validate_description",
    "validate_reason",
]
