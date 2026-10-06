"""S11n CSV headers and the column names pseudonymization keys on."""

from __future__ import annotations

_TIMEPOINT_KEYS = (
    "study_id",
    "clinician_id",
    "patient_id",
    "t_index",
    "timepoint_minutes",
    "config_version",
    "config_hash",
)

TIMEPOINTS_HEADER = (
    *_TIMEPOINT_KEYS,
    "arm",
    "arm_source",
    "schedule_id",
    "case_position",
    "activated_at",
    "lifecycle_state",
    "case_completed_at",
    "case_incomplete_at",
    "incomplete_reason",
    "timepoint_reached",
    "timepoint_started_at",
    "timepoint_ended_at",
    "elapsed_seconds",
    "telemetry_status",
    "foreground_seconds",
    "active_seconds",
    "tab_conflict_detected",
    "ai_delivered",
    "ai_viewed",
    "ai_viewing_status",
    "ai_qualifying_seconds",
    "ai_episode_count",
    "intervention_failure",
    "intervention_failure_reasons",
    "intervention_leakage",
    "pp_compliant",
    "pp_determinate",
    "integrity_warnings",
)

ANSWERS_HEADER = (
    *_TIMEPOINT_KEYS,
    "question_id",
    "response_type",
    "branch_state",
    "required_now",
    "response_status",
    "response_value",
    "value_exported",
    "answer_source",
    "derived_from_question_id",
    "missing_reason",
    "ts_recorded",
)

PANELS_HEADER = (
    *_TIMEPOINT_KEYS,
    "visit_kind",
    "panel_id",
    "mounted",
    "telemetry_status",
    "qualifying_seconds",
    "viewed",
    "episode_count",
    "panel_open_count",
    "time_to_first_view_seconds",
    "first_view_client_ts",
    "last_view_client_ts",
    "tab_conflict_detected",
)

EVENTS_HEADER = (
    "study_id",
    "event_id",
    "session_id",
    "clinician_id",
    "patient_id",
    "timepoint",
    "t_index",
    "visit_kind",
    "config_version",
    "config_hash",
    "render_id",
    "tab_id",
    "kind",
    "client_ts",
    "server_ts",
    "client_seq",
    "client_mono_ms",
    "payload_json",
)

AUDIT_HEADER = (
    "study_id",
    "clinician_id",
    "schedule_id",
    "generation_config_version",
    "generation_config_hash",
    "generated_at",
    "algorithm_version",
    "master_seed",
    "derived_seed_hex",
    "allocation_state_json",
    "starting_ai_count",
    "starting_no_ai_count",
    "starting_arm",
    "block_length",
    "block_sequence_json",
    "case_position",
    "patient_id",
    "planned_arm",
    "block_number",
    "position_in_block",
    "preceding_block_arm",
    "planned_cases_since_ai",
    "assignment_seed",
    "activated",
    "activated_at",
    "activation_config_version",
    "activation_config_hash",
    "realised_arm",
    "lifecycle_state",
    "completed_at",
    "incomplete_at",
    "incomplete_reason",
    "replacement_id",
    "replaces_patient_id",
    "replacement_generated_at",
    "replacement_activated_at",
    "replaced_by_replacement_id",
    "replaced_by_patient_id",
)

HISTORY_HEADER = (
    "study_id",
    "config_version",
    "config_hash",
    "activated_at",
    "change_description",
    "change_reason",
    "study_json",
    "questions_json",
)

COUNTS_HEADER = (
    "study_id",
    "config_version",
    "config_hash",
    "scheduled_items_generated",
    "activated_cases",
    "completed_cases",
    "incomplete_cases",
    "open_cases",
    "expected_timepoints",
    "reached_primary_timepoints",
    "answer_rows_present",
)

PRACTICE_TIMEPOINTS_HEADER = (
    *_TIMEPOINT_KEYS,
    "observation_mode",
    "arm",
    "practice_started_at",
    "practice_completed_at",
    "timepoint_started_at",
    "timepoint_ended_at",
    "elapsed_seconds",
    "telemetry_status",
    "foreground_seconds",
    "active_seconds",
)

PRACTICE_ANSWERS_HEADER = (*ANSWERS_HEADER, "observation_mode")

#: S11p: one row per bundle clinician; never the name.
CLINICIANS_HEADER = (
    "study_id",
    "clinician_id",
    "profile_status",
    "professional_role",
    "years_of_practice",
    "country_of_practice",
    "primary_specialty",
)
PROFILE_COMPLETE = "complete"
PROFILE_MISSING = "missing"

#: Columns holding the DB clinician id or an id hashed from it with
#: exported inputs (schedule, replacement): a roster would reverse them.
LINKED_ID_COLUMNS = frozenset(
    {"clinician_id", "schedule_id", "replacement_id", "replaced_by_replacement_id"}
)
#: Top-level event payload keys holding such an id (``case.activated``,
#: ``case.replacement_planned``).
LINKED_ID_PAYLOAD_KEYS = frozenset({"clinician_id", "schedule_id", "replacement_id"})
PAYLOAD_COLUMN = "payload_json"
CLINICIAN_COLUMN = "clinician_id"
#: Tables with this column keep their event order.
EVENT_ORDER_COLUMN = "event_id"
