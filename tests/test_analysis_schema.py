"""S12a lockstep: the reader's frozen ``phase2_export_v1`` copy vs its sources.

An exporter change must fail here, never silently move the analysis
contract (spec ``specs/session-12-phase25-release.md`` §4.2).
"""

from __future__ import annotations

import ast
import typing
from enum import StrEnum
from pathlib import Path

import pytest

from ehr_simulator import behavioral_timing, domain_types, export, study_variables
from ehr_simulator.analysis import bundle, schema
from ehr_simulator.clinician_profile import ProfessionalRole
from ehr_simulator.config import questions
from ehr_simulator.config.study import STUDY_ID_PATTERN
from ehr_simulator.db import arm_assignments, case_lifecycle
from ehr_simulator.export_phase2 import headers, model
from ehr_simulator.panel_exposure import PANEL_IDS
from ehr_simulator.question_branching import AnswerSource, QuestionState

ANALYSIS_DIR = Path(schema.__file__).parent
#: Modules the analysis package must never import (spec §3.1, §4.2).
FORBIDDEN_IMPORTS = (
    "ehr_simulator.web",
    "ehr_simulator.db",
    "ehr_simulator.cli",
    "ehr_simulator.cli_support",
    "ehr_simulator.export",
    "ehr_simulator.export_phase2",
    "ehr_simulator.export_bundle",
    "ehr_simulator.pseudonym",
)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        (schema.TIMEPOINTS, headers.TIMEPOINTS_HEADER),
        (schema.ANSWERS, headers.ANSWERS_HEADER),
        (schema.PANELS, headers.PANELS_HEADER),
        (schema.EVENTS, headers.EVENTS_HEADER),
        (schema.AUDIT, headers.AUDIT_HEADER),
        (schema.HISTORY, headers.HISTORY_HEADER),
        (schema.COUNTS, headers.COUNTS_HEADER),
        (schema.CLINICIANS, headers.CLINICIANS_HEADER),
        (schema.PRACTICE_TIMEPOINTS, headers.PRACTICE_TIMEPOINTS_HEADER),
        (schema.PRACTICE_ANSWERS, headers.PRACTICE_ANSWERS_HEADER),
    ],
)
def test_headers_in_lockstep(name: str, source: tuple[str, ...]) -> None:
    files = {**schema.REQUIRED_FILES, **schema.OPTIONAL_PRACTICE_FILES}
    assert schema.header(files[name]) == source


def test_constants_in_lockstep() -> None:
    assert schema.EXPORT_SCHEMA_VERSION == model.EXPORT_SCHEMA_VERSION
    assert schema.LIST_SEPARATOR == model.LIST_SEPARATOR
    assert (schema.TRUE, schema.FALSE) == (model.TRUE, model.FALSE)
    assert schema.NOT_CONFIGURED == model.NOT_CONFIGURED
    assert bundle.STUDY_ID_PATTERN.pattern == STUDY_ID_PATTERN.pattern


def _values(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


@pytest.mark.parametrize(
    ("ours", "source"),
    [
        (schema.Arm, {domain_types.ARM_AI, domain_types.ARM_NO_AI}),
        (schema.ArmSource, {arm_assignments.ARM_SOURCE_PHASE2}),
        (schema.LifecycleState, _values(domain_types.CaseState)),
        (schema.IncompleteReason, _values(case_lifecycle.IncompleteReason)),
        (schema.TelemetryStatus, _values(behavioral_timing.TelemetryStatus)),
        (
            schema.TimepointTelemetry,
            _values(behavioral_timing.TelemetryStatus) | {model.NOT_CONFIGURED},
        ),
        (schema.ResponseStatus, _values(study_variables.ResponseStatus)),
        (schema.MissingReason, _values(study_variables.MissingReason)),
        (schema.FailureReason, _values(study_variables.FailureReason)),
        (schema.BranchState, _values(QuestionState)),
        (schema.AnswerSource, _values(AnswerSource)),
        (schema.ResponseType, set(typing.get_args(questions.ResponseType))),
        (schema.VisitKind, {model.PRIMARY, model.REVISIT}),
        (schema.PanelId, set(PANEL_IDS)),
        (schema.ProfileStatus, {headers.PROFILE_COMPLETE, headers.PROFILE_MISSING}),
        (schema.ProfessionalRole, _values(ProfessionalRole)),
    ],
)
def test_vocabularies_in_lockstep(ours: type[StrEnum], source: set[str]) -> None:
    assert _values(ours) == source


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Yes",
        "-2",
        "+3",
        "1e5",
        "-2+3",
        "=SUM(A1)",
        "+ new imaging finding",
        "@cmd",
        "\tx",
        "\rx",
        "\nx",
        "＝x",
        "＋x",
        "－x",
        "＠x",
        "'=x",
        "٣",
    ],
)
def test_guard_cell_in_lockstep(value: str) -> None:
    assert schema.guard_cell(value) == export.guard_cell(value)


def test_analysis_never_imports_the_app_db_or_exporter() -> None:
    for path in ANALYSIS_DIR.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not any(name == f or name.startswith(f"{f}.") for f in FORBIDDEN_IMPORTS), (
                    f"{path.name} imports {name}"
                )
