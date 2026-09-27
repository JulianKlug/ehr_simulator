"""Rules that need the study and the questions together (S11i).

Each model validates alone; this module holds the few rules spanning both.
Called by ``validate-config``, ``activate-config``, study mode boot and
preflight. Stored snapshots are never re-checked, so activated history
stays readable.
"""

from __future__ import annotations

from ehr_simulator.config.exceptions import ConfigError
from ehr_simulator.config.questions import Questions
from ehr_simulator.config.study import StudyConfig

__all__ = ["validate_study_questions"]


def validate_study_questions(study: StudyConfig, questions: Questions) -> None:
    """Raise :class:`ConfigError` when the pair breaks a cross-model rule.

    Free text: a study declaring ``study_behaviour`` without
    ``free_text.enabled: true`` may not ask a free-text question. Example:
    ``free_notes`` under ``free_text: {enabled: false}`` is refused.
    """
    behaviour = study.study_behaviour
    if behaviour is None or behaviour.free_text.enabled:
        return

    free_text = [q.question_id for q in questions.questions if q.response_type == "free-text"]
    if free_text:
        raise ConfigError(
            f"free-text question(s) {free_text} are configured while "
            "study_behaviour.free_text.enabled is false"
        )
