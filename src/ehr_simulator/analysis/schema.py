"""Frozen ``phase2_export_v1`` input contract of the analysis reader (S12a).

A deliberate copy of the S11n exporter's headers, vocabularies and cell
guard: ``tests/test_analysis_schema.py`` pins every copy to its source, so
an exporter change fails that test instead of silently moving the analysis
contract. Nothing here imports the exporter, ``db`` or ``web``.

Cell kinds (``ColumnKind``) are what the reader accepts per column; ``OPT_``
kinds also accept ``""`` (unknown / not applicable), which is never read as
false or zero.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

EXPORT_SCHEMA_VERSION = "phase2_export_v1"
MANIFEST_NAME = "manifest.json"
LIST_SEPARATOR = "|"
TRUE = "true"
FALSE = "false"
NOT_CONFIGURED = "not_configured"

TIMEPOINTS = "timepoints.csv"
ANSWERS = "answers.csv"
PANELS = "panel_summaries.csv"
EVENTS = "behavioral_events.csv"
AUDIT = "randomisation_audit.csv"
HISTORY = "configuration_history.csv"
COUNTS = "configuration_counts.csv"
CLINICIANS = "clinicians.csv"
PRACTICE_TIMEPOINTS = "practice_timepoints.csv"
PRACTICE_ANSWERS = "practice_answers.csv"


# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class Arm(StrEnum):
    AI = "ai"
    NO_AI = "no_ai"


class ArmSource(StrEnum):
    PHASE2_RANDOMIZED = "phase2_randomized"


class LifecycleState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


class IncompleteReason(StrEnum):
    RECONNECTION_TIMEOUT = "reconnection_timeout"
    PAUSE_TIMEOUT = "pause_timeout"
    OPERATOR_ABANDONED = "operator_abandoned"


class TelemetryStatus(StrEnum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    GAPPED = "gapped"
    MISSING = "missing"
    MULTI_TAB = "multi_tab"
    INVALID = "invalid"


class TimepointTelemetry(StrEnum):
    """``timepoints.csv`` ``telemetry_status``: S11j statuses + no telemetry block."""

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    GAPPED = "gapped"
    MISSING = "missing"
    MULTI_TAB = "multi_tab"
    INVALID = "invalid"
    NOT_CONFIGURED = NOT_CONFIGURED


class ResponseStatus(StrEnum):
    ANSWERED = "answered"
    NOT_APPLICABLE = "not_applicable"
    MISSING = "missing"


class MissingReason(StrEnum):
    TECHNICAL_FAILURE = "technical_failure"
    CASE_ABANDONED = "case_abandoned"
    REACHED_UNANSWERED = "reached_unanswered"
    PENDING = "pending"
    TIMEPOINT_NEVER_REACHED = "timepoint_never_reached"
    UNKNOWN = "unknown"


class BranchState(StrEnum):
    HIDDEN = "hidden"
    EDITABLE = "editable"
    DERIVED = "derived"


class AnswerSource(StrEnum):
    CLINICIAN = "clinician"
    RULE = "rule"


class ResponseType(StrEnum):
    LIKERT = "likert"
    CATEGORICAL = "categorical"
    MULTI_SELECT = "multi-select"
    PROBABILITY = "probability-0-100"
    FREE_TEXT = "free-text"


class VisitKind(StrEnum):
    PRIMARY = "primary"
    REVISIT = "revisit"


class PanelId(StrEnum):
    ADMISSION = "admission"
    VITALS = "vitals"
    NEURO_SUPPORT = "neuro_support"
    LABS = "labs"
    IMAGING = "imaging"
    AI = "ai"


class FailureReason(StrEnum):
    MISSING_ARTIFACT = "missing_artifact"
    RENDER_FAILURE = "render_failure"
    DISPLAY_FAILURE = "display_failure"
    OTHER_INTEGRITY_FAILURE = "other_integrity_failure"


class PracticeFiles(StrEnum):
    """Manifest ``practice_included``: practice clinicians may be listed."""

    DECLARED = "declared"
    ABSENT = "absent"


class ProfileStatus(StrEnum):
    COMPLETE = "complete"
    MISSING = "missing"


class ProfessionalRole(StrEnum):
    PHYSICIAN = "physician"
    NURSE = "nurse"


# ---------------------------------------------------------------------------
# Column kinds
# ---------------------------------------------------------------------------


class ColumnKind(StrEnum):
    TEXT = "text"  # any string, "" allowed (ids are checked by the joins)
    ID = "id"  # non-empty string
    INT = "int"  # non-negative integer
    OPT_INT = "opt_int"
    FLOAT = "float"  # finite, non-negative
    OPT_FLOAT = "opt_float"
    BOOL = "bool"
    OPT_BOOL = "opt_bool"
    TS = "ts"  # server timestamp
    OPT_TS = "opt_ts"
    ENUM = "enum"
    OPT_ENUM = "opt_enum"
    ENUM_LIST = "enum_list"  # sorted, "|" joined, "" = empty
    JSON = "json"


Column = tuple[str, ColumnKind, type[StrEnum] | None]


def _c(name: str, kind: ColumnKind, vocab: type[StrEnum] | None = None) -> Column:
    return (name, kind, vocab)


K = ColumnKind

_KEYS: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("clinician_id", K.ID),
    _c("patient_id", K.ID),
    _c("t_index", K.INT),
    _c("timepoint_minutes", K.FLOAT),
    _c("config_version", K.ID),
    _c("config_hash", K.ID),
)

_TIMEPOINTS: tuple[Column, ...] = (
    *_KEYS,
    _c("arm", K.ENUM, Arm),
    _c("arm_source", K.ENUM, ArmSource),
    _c("schedule_id", K.ID),
    _c("case_position", K.INT),
    _c("activated_at", K.TS),
    _c("lifecycle_state", K.OPT_ENUM, LifecycleState),
    _c("case_completed_at", K.OPT_TS),
    _c("case_incomplete_at", K.OPT_TS),
    _c("incomplete_reason", K.OPT_ENUM, IncompleteReason),
    _c("timepoint_reached", K.BOOL),
    _c("timepoint_started_at", K.OPT_TS),
    _c("timepoint_ended_at", K.OPT_TS),
    _c("elapsed_seconds", K.OPT_FLOAT),
    _c("telemetry_status", K.ENUM, TimepointTelemetry),
    _c("foreground_seconds", K.OPT_FLOAT),
    _c("active_seconds", K.OPT_FLOAT),
    _c("tab_conflict_detected", K.BOOL),
    _c("ai_delivered", K.OPT_BOOL),
    _c("ai_viewed", K.OPT_BOOL),
    _c("ai_viewing_status", K.OPT_ENUM, TelemetryStatus),
    _c("ai_qualifying_seconds", K.OPT_FLOAT),
    _c("ai_episode_count", K.OPT_INT),
    _c("intervention_failure", K.BOOL),
    _c("intervention_failure_reasons", K.ENUM_LIST, FailureReason),
    _c("intervention_leakage", K.BOOL),
    _c("pp_compliant", K.OPT_BOOL),
    _c("pp_determinate", K.BOOL),
    _c("integrity_warnings", K.TEXT),
)

_ANSWERS: tuple[Column, ...] = (
    *_KEYS,
    _c("question_id", K.ID),
    _c("response_type", K.ENUM, ResponseType),
    _c("branch_state", K.ENUM, BranchState),
    _c("required_now", K.BOOL),
    _c("response_status", K.ENUM, ResponseStatus),
    _c("response_value", K.TEXT),  # decoded against the pinned question
    _c("value_exported", K.BOOL),
    _c("answer_source", K.OPT_ENUM, AnswerSource),
    _c("derived_from_question_id", K.TEXT),
    _c("missing_reason", K.OPT_ENUM, MissingReason),
    _c("ts_recorded", K.OPT_TS),
)

_PANELS: tuple[Column, ...] = (
    *_KEYS,
    _c("visit_kind", K.ENUM, VisitKind),
    _c("panel_id", K.ENUM, PanelId),
    _c("mounted", K.OPT_BOOL),
    _c("telemetry_status", K.ENUM, TelemetryStatus),
    _c("qualifying_seconds", K.OPT_FLOAT),
    _c("viewed", K.OPT_BOOL),
    _c("episode_count", K.OPT_INT),
    _c("panel_open_count", K.OPT_INT),
    _c("time_to_first_view_seconds", K.OPT_FLOAT),
    _c("first_view_client_ts", K.TEXT),
    _c("last_view_client_ts", K.TEXT),
    _c("tab_conflict_detected", K.BOOL),
)

_EVENTS: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("event_id", K.INT),
    _c("session_id", K.TEXT),
    _c("clinician_id", K.TEXT),
    _c("patient_id", K.TEXT),
    _c("timepoint", K.TEXT),
    _c("t_index", K.OPT_INT),
    _c("visit_kind", K.OPT_ENUM, VisitKind),
    _c("config_version", K.TEXT),
    _c("config_hash", K.TEXT),
    _c("render_id", K.TEXT),
    _c("tab_id", K.TEXT),
    _c("kind", K.ID),
    _c("client_ts", K.TEXT),
    _c("server_ts", K.TS),
    _c("client_seq", K.OPT_INT),
    _c("client_mono_ms", K.TEXT),
    _c("payload_json", K.JSON),  # verified, never exposed
)

_AUDIT: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("clinician_id", K.ID),
    _c("schedule_id", K.ID),
    _c("generation_config_version", K.ID),
    _c("generation_config_hash", K.ID),
    _c("generated_at", K.TS),
    _c("algorithm_version", K.ID),
    _c("master_seed", K.INT),
    _c("derived_seed_hex", K.ID),
    _c("allocation_state_json", K.JSON),
    _c("starting_ai_count", K.INT),
    _c("starting_no_ai_count", K.INT),
    _c("starting_arm", K.ENUM, Arm),
    _c("block_length", K.INT),
    _c("block_sequence_json", K.JSON),
    _c("case_position", K.INT),
    _c("patient_id", K.ID),
    _c("planned_arm", K.ENUM, Arm),
    _c("block_number", K.INT),
    _c("position_in_block", K.INT),
    _c("preceding_block_arm", K.OPT_ENUM, Arm),
    _c("planned_cases_since_ai", K.OPT_INT),
    _c("assignment_seed", K.INT),
    _c("activated", K.BOOL),
    _c("activated_at", K.OPT_TS),
    _c("activation_config_version", K.TEXT),
    _c("activation_config_hash", K.TEXT),
    _c("realised_arm", K.OPT_ENUM, Arm),
    _c("lifecycle_state", K.OPT_ENUM, LifecycleState),
    _c("completed_at", K.OPT_TS),
    _c("incomplete_at", K.OPT_TS),
    _c("incomplete_reason", K.OPT_ENUM, IncompleteReason),
    _c("replacement_id", K.TEXT),
    _c("replaces_patient_id", K.TEXT),
    _c("replacement_generated_at", K.OPT_TS),
    _c("replacement_activated_at", K.OPT_TS),
    _c("replaced_by_replacement_id", K.TEXT),
    _c("replaced_by_patient_id", K.TEXT),
)

_HISTORY: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("config_version", K.ID),
    _c("config_hash", K.ID),
    _c("activated_at", K.TS),
    _c("change_description", K.TEXT),
    _c("change_reason", K.TEXT),
    _c("study_json", K.JSON),
    _c("questions_json", K.JSON),
)

_COUNTS: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("config_version", K.ID),
    _c("config_hash", K.ID),
    _c("scheduled_items_generated", K.INT),
    _c("activated_cases", K.INT),
    _c("completed_cases", K.INT),
    _c("incomplete_cases", K.INT),
    _c("open_cases", K.INT),
    _c("expected_timepoints", K.INT),
    _c("reached_primary_timepoints", K.INT),
    _c("answer_rows_present", K.INT),
)

_CLINICIANS: tuple[Column, ...] = (
    _c("study_id", K.ID),
    _c("clinician_id", K.ID),
    _c("profile_status", K.ENUM, ProfileStatus),
    _c("professional_role", K.OPT_ENUM, ProfessionalRole),
    _c("years_of_practice", K.OPT_FLOAT),
    _c("country_of_practice", K.TEXT),
    _c("primary_specialty", K.TEXT),
)

_PRACTICE_TIMEPOINTS: tuple[Column, ...] = (
    *_KEYS,
    _c("observation_mode", K.ID),
    _c("arm", K.ENUM, Arm),
    _c("practice_started_at", K.OPT_TS),
    _c("practice_completed_at", K.OPT_TS),
    _c("timepoint_started_at", K.OPT_TS),
    _c("timepoint_ended_at", K.OPT_TS),
    _c("elapsed_seconds", K.OPT_FLOAT),
    _c("telemetry_status", K.ENUM, TimepointTelemetry),
    _c("foreground_seconds", K.OPT_FLOAT),
    _c("active_seconds", K.OPT_FLOAT),
)

_PRACTICE_ANSWERS: tuple[Column, ...] = (*_ANSWERS, _c("observation_mode", K.ID))

#: The eight measured files, export order.
REQUIRED_FILES: Mapping[str, tuple[Column, ...]] = MappingProxyType(
    {
        TIMEPOINTS: _TIMEPOINTS,
        ANSWERS: _ANSWERS,
        PANELS: _PANELS,
        EVENTS: _EVENTS,
        AUDIT: _AUDIT,
        HISTORY: _HISTORY,
        COUNTS: _COUNTS,
        CLINICIANS: _CLINICIANS,
    }
)
#: Opt in practice files: verified, then excluded from every measured result.
OPTIONAL_PRACTICE_FILES: Mapping[str, tuple[Column, ...]] = MappingProxyType(
    {PRACTICE_TIMEPOINTS: _PRACTICE_TIMEPOINTS, PRACTICE_ANSWERS: _PRACTICE_ANSWERS}
)


def header(columns: tuple[Column, ...]) -> tuple[str, ...]:
    return tuple(name for name, _kind, _vocab in columns)


# ---------------------------------------------------------------------------
# S9c formula guard (copy of ``export.guard_cell``)
# ---------------------------------------------------------------------------

_FORMULA_TRIGGERS = frozenset("=+-@\t\r\n＝＋－＠")  # = + - @ ＝ ＋ － ＠
_PLAIN_NUMBER = re.compile(r"[+-]?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
GUARD_PREFIX = "'"


def guard_cell(value: str) -> str:
    """The exported form of *value*: ``'`` before a formula trigger.

    Example: ``"+ new finding"`` → ``"'+ new finding"``; ``"-2"`` unchanged.
    """
    if not value:
        return value
    if _PLAIN_NUMBER.fullmatch(value):
        return value
    if _FORMULA_TRIGGERS.issuperset(value[:1]):
        return GUARD_PREFIX + value
    return value
