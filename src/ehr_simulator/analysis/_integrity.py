"""Cross table checks of a typed bundle; builds ``cases`` and decodes answers.

Keys (spec §4.4)::

    case       (study_id, clinician_id, patient_id)   one version/hash/arm each
    timepoint  case + t_index                          = pinned study timepoints
    answer     timepoint + question_id                 = pinned questions, all
    panel      timepoint + visit_kind + panel_id
    audit      clinician_id + schedule_id + case_position

Refuses (``AnalysisError``) a duplicate key, an unregistered or drifted
``(config_version, config_hash)``, a broken join, an inconsistent answer
status or a value that does not decode against its historical question.
Answer values are compared in their exported (S9c guarded) form.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd

from ehr_simulator.analysis.errors import AnalysisError
from ehr_simulator.analysis.schema import (
    ANSWERS,
    AUDIT,
    CLINICIANS,
    COUNTS,
    EVENTS,
    GUARD_PREFIX,
    LIST_SEPARATOR,
    PANELS,
    TIMEPOINTS,
    AnswerSource,
    BranchState,
    LifecycleState,
    ResponseStatus,
    ResponseType,
    guard_cell,
)

if TYPE_CHECKING:
    from ehr_simulator.analysis.bundle import ConfigVersion
    from ehr_simulator.config.questions import Question

CASE_KEY = ("study_id", "clinician_id", "patient_id")
TIMEPOINT_KEY = (*CASE_KEY, "t_index")
ANSWER_KEY = (*TIMEPOINT_KEY, "question_id")
PANEL_KEY = (*TIMEPOINT_KEY, "visit_kind", "panel_id")
AUDIT_KEY = ("clinician_id", "schedule_id", "case_position")
PROVENANCE = ("config_version", "config_hash")

#: Columns of ``timepoints.csv`` that describe the case, not the timepoint.
CASE_COLUMNS = (
    *PROVENANCE,
    "arm",
    "arm_source",
    "schedule_id",
    "case_position",
    "activated_at",
    "lifecycle_state",
    "case_completed_at",
    "case_incomplete_at",
    "incomplete_reason",
)
OPEN_STATES = (LifecycleState.ACTIVE, LifecycleState.PAUSED)
_SIGNED_INT = re.compile(r"[+-]?[0-9]+")


@dataclass(frozen=True)
class Linked:
    cases: pd.DataFrame
    answers: pd.DataFrame


def link(
    study_id: str,
    frames: Mapping[str, pd.DataFrame],
    versions: Mapping[str, ConfigVersion],
    practice_included: bool,
) -> Linked:
    for name, frame in frames.items():
        _single_study(name, frame, study_id)

    registered = {(v.version, v.config_hash) for v in versions.values()}
    for name in (TIMEPOINTS, ANSWERS, PANELS, COUNTS):
        _registered(name, frames[name], PROVENANCE, registered)
    _registered_events(frames[EVENTS], registered)

    cases = _cases(frames[TIMEPOINTS], versions)
    answers = _answers(frames[ANSWERS], frames[TIMEPOINTS], versions)
    _panels(frames[PANELS], frames[TIMEPOINTS])
    cases = _audit(frames[AUDIT], cases, registered)
    _counts(frames[COUNTS], versions, frames[AUDIT], cases, frames[TIMEPOINTS], answers)
    _clinicians(frames[CLINICIANS], cases, frames[AUDIT], practice_included)
    _events(frames[EVENTS], cases)
    return Linked(cases=cases, answers=answers)


# ---------------------------------------------------------------------------
# Shared checks
# ---------------------------------------------------------------------------


def _single_study(name: str, frame: pd.DataFrame, study_id: str) -> None:
    if (frame["study_id"] != study_id).any():
        raise AnalysisError(f"{name}: a row belongs to another study than the manifest's")


def _registered(
    name: str, frame: pd.DataFrame, columns: Sequence[str], registered: set[tuple[str, str]]
) -> None:
    pairs = set(frame[list(columns)].itertuples(index=False, name=None))
    unknown = pairs - registered
    if unknown:
        raise AnalysisError(
            f"{name}: configuration {sorted(v for v, _h in unknown)} is not registered "
            "or its config_hash drifted"
        )


def _registered_events(events: pd.DataFrame, registered: set[tuple[str, str]]) -> None:
    pinned = events[events["config_version"] != ""]
    _registered(EVENTS, pinned, PROVENANCE, registered)
    if (events.loc[events["config_version"] == "", "config_hash"] != "").any():
        raise AnalysisError(f"{EVENTS}: a config_hash without its config_version")


def _unique(name: str, frame: pd.DataFrame, key: Sequence[str]) -> None:
    if frame.duplicated(list(key)).any():
        raise AnalysisError(f"{name}: duplicate key rows {tuple(key)}")


def _same(a: object, b: object) -> bool:
    """Equality where two missing values agree."""
    if pd.isna(a) and pd.isna(b):
        return True
    if pd.isna(a) or pd.isna(b):
        return False
    return a == b


# ---------------------------------------------------------------------------
# Cases and timepoints
# ---------------------------------------------------------------------------


def _cases(timepoints: pd.DataFrame, versions: Mapping[str, ConfigVersion]) -> pd.DataFrame:
    """One row per case; every case attribute constant, timepoints complete."""
    _unique(TIMEPOINTS, timepoints, TIMEPOINT_KEY)

    rows = []
    for key, group in timepoints.groupby(list(CASE_KEY), sort=False):
        where = f"{TIMEPOINTS}: case of patient {key[2]!r}"
        for column in CASE_COLUMNS:
            if group[column].nunique(dropna=False) != 1:
                raise AnalysisError(f"{where} carries more than one {column}")

        first = group.iloc[0]
        expected = versions[first["config_version"]].timepoints_minutes
        found = sorted(zip(group["t_index"], group["timepoint_minutes"], strict=True))
        if found != list(enumerate(expected)):
            raise AnalysisError(f"{where} does not hold its pinned timepoints")
        rows.append({**dict(zip(CASE_KEY, key, strict=True)), **first[list(CASE_COLUMNS)]})

    columns = [*CASE_KEY, *CASE_COLUMNS]
    cases = pd.DataFrame(rows, columns=columns)
    return cases.astype(timepoints[columns].dtypes.to_dict())


# ---------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------


def _answers(
    answers: pd.DataFrame, timepoints: pd.DataFrame, versions: Mapping[str, ConfigVersion]
) -> pd.DataFrame:
    _unique(ANSWERS, answers, ANSWER_KEY)

    pinned = timepoints[[*TIMEPOINT_KEY, *PROVENANCE, "timepoint_minutes"]]
    joined = answers.merge(
        pinned, on=list(TIMEPOINT_KEY), how="left", suffixes=("", "_tp"), indicator=True
    )
    if (joined["_merge"] != "both").any():
        raise AnalysisError(f"{ANSWERS}: a row names a timepoint absent from {TIMEPOINTS}")
    for column in (*PROVENANCE, "timepoint_minutes"):
        if (joined[column] != joined[f"{column}_tp"]).any():
            raise AnalysisError(f"{ANSWERS}: {column} differs from its timepoint row")

    _question_sets(answers, timepoints, versions)

    numbers: list[object] = []
    options: list[tuple[str, ...] | None] = []
    for number, row in enumerate(answers.itertuples(index=False), start=1):
        where = f"{ANSWERS}: data row {number}"
        question = versions[row.config_version].question(row.question_id)
        assert question is not None  # _question_sets checked
        if row.response_type != question.response_type:
            raise AnalysisError(f"{where}: response_type differs from the pinned question")
        _status_consistent(row, where)
        value_number, value_options = _decoded(row, question, where)
        numbers.append(value_number)
        options.append(value_options)

    out = answers.copy()
    out["value_number"] = pd.Series(numbers, dtype="Float64", index=out.index)
    out["value_options"] = pd.Series(options, dtype="object", index=out.index)
    return out


def _question_sets(
    answers: pd.DataFrame, timepoints: pd.DataFrame, versions: Mapping[str, ConfigVersion]
) -> None:
    """Every timepoint row carries exactly its pinned questions."""
    by_timepoint = answers.groupby(list(TIMEPOINT_KEY), sort=False)["question_id"].agg(frozenset)
    for row in timepoints.itertuples(index=False):
        key = tuple(getattr(row, c) for c in TIMEPOINT_KEY)
        expected = frozenset(
            q.question_id for q in versions[row.config_version].questions.questions
        )
        if by_timepoint.get(key, frozenset()) != expected:
            raise AnalysisError(
                f"{ANSWERS}: patient {row.patient_id!r}, t_index {row.t_index} does not hold "
                "exactly its pinned questions"
            )


def _status_consistent(row: object, where: str) -> None:
    """S11h / S11l invariants between branch, status, source and value."""
    status = row.response_status
    hidden = row.branch_state == BranchState.HIDDEN
    answered = status == ResponseStatus.ANSWERED
    source = None if pd.isna(row.answer_source) else row.answer_source
    reason_set = not pd.isna(row.missing_reason)

    checks = (
        (hidden == (status == ResponseStatus.NOT_APPLICABLE), "hidden ⇔ not_applicable"),
        (reason_set == (status == ResponseStatus.MISSING), "missing ⇔ missing_reason"),
        ((source is not None) == answered, "answered ⇔ answer_source"),
        (
            (row.derived_from_question_id != "") == (source == AnswerSource.RULE),
            "rule ⇔ derived_from_question_id",
        ),
        (not (hidden and row.required_now), "hidden ⇒ not required_now"),
        (not row.value_exported or answered, "value_exported ⇒ answered"),
        (
            row.value_exported or not answered or row.response_type == ResponseType.FREE_TEXT,
            "answered ∧ not exported ⇒ free-text",
        ),
        (row.value_exported or row.response_value == "", "not exported ⇒ empty value"),
    )
    for ok, rule in checks:
        if not ok:
            raise AnalysisError(f"{where}: inconsistent answer status ({rule})")


def _decoded(row: object, question: Question, where: str) -> tuple[object, tuple[str, ...] | None]:
    """``(value_number, value_options)`` of one exported value."""
    if not row.value_exported:
        return pd.NA, None

    cell = row.response_value
    kind = question.response_type
    if kind == ResponseType.LIKERT:
        return _bounded_int(cell, question.scale_min, question.scale_max, where), None
    if kind == ResponseType.PROBABILITY:
        return _bounded_int(cell, 0, 100, where), None
    if kind == ResponseType.CATEGORICAL:
        matches = [o for o in question.options if guard_cell(o) == cell]
        if len(matches) != 1:
            raise AnalysisError(f"{where}: value is not one option of the pinned question")
        return pd.NA, (matches[0],)
    if kind == ResponseType.MULTI_SELECT:
        return pd.NA, _multi_select(cell, question.options, where)
    return pd.NA, None  # free text: kept as exported, never decoded


def _bounded_int(cell: str, low: int, high: int, where: str) -> int:
    if not _SIGNED_INT.fullmatch(cell) or not low <= int(cell) <= high:
        raise AnalysisError(f"{where}: value is not an integer in [{low}, {high}]")
    return int(cell)


def _multi_select(cell: str, options: Sequence[str], where: str) -> tuple[str, ...]:
    """The selection whose guarded, ``|`` joined historical order is *cell*.

    Example: ``"'+ new finding|Labs"`` → ``("+ new finding", "Labs")``.
    A leading ``'`` is stripped only when re-guarding reproduces the cell.
    """
    raws = {cell}
    if cell.startswith(GUARD_PREFIX):
        raws.add(cell[len(GUARD_PREFIX) :])

    found = set()
    for raw in raws:
        if guard_cell(raw) != cell:
            continue
        selected = tuple(raw.split(LIST_SEPARATOR))
        in_order = tuple(o for o in options if o in selected)
        if selected and len(set(selected)) == len(selected) and selected == in_order:
            found.add(selected)
    if len(found) != 1:
        raise AnalysisError(
            f"{where}: multi-select value is not a unique, ordered subset of the pinned options"
        )
    return found.pop()


# ---------------------------------------------------------------------------
# Panels, audit, counts, clinicians, events
# ---------------------------------------------------------------------------


def _panels(panels: pd.DataFrame, timepoints: pd.DataFrame) -> None:
    _unique(PANELS, panels, PANEL_KEY)
    pinned = timepoints[[*TIMEPOINT_KEY, *PROVENANCE]]
    joined = panels.merge(pinned, on=list(TIMEPOINT_KEY), how="left", suffixes=("", "_tp"))
    for column in PROVENANCE:
        if (joined[column] != joined[f"{column}_tp"]).fillna(True).any():
            raise AnalysisError(f"{PANELS}: a row does not match its timepoint's {column}")


def _audit(
    audit: pd.DataFrame, cases: pd.DataFrame, registered: set[tuple[str, str]]
) -> pd.DataFrame:
    """Activated audit rows ↔ cases one to one; replacement links two sided.

    Returns *cases* with ``replaces_patient_id`` / ``replaced_by_patient_id``.
    """
    _unique(AUDIT, audit, AUDIT_KEY)
    _registered(AUDIT, audit, ("generation_config_version", "generation_config_hash"), registered)
    if audit.groupby("clinician_id")["schedule_id"].nunique().gt(1).any():
        raise AnalysisError(f"{AUDIT}: a clinician holds more than one schedule")

    activated = audit[audit["activated"]]
    _registered(
        AUDIT, activated, ("activation_config_version", "activation_config_hash"), registered
    )
    planned = audit[~audit["activated"]]
    planned_only = planned[
        ["activated_at", "realised_arm", "lifecycle_state", "completed_at", "incomplete_at"]
    ]
    if (
        planned_only.notna().any().any()
        or (planned[["activation_config_version", "activation_config_hash"]] != "").any().any()
    ):
        raise AnalysisError(f"{AUDIT}: a never activated item carries realised values")

    if activated.duplicated(["clinician_id", "patient_id"]).any():
        raise AnalysisError(f"{AUDIT}: a clinician activated one patient twice")
    joined = cases.merge(
        activated,
        on=["study_id", "clinician_id", "patient_id"],
        how="outer",
        indicator=True,
        suffixes=("", "_audit"),
    )
    if (joined["_merge"] != "both").any():
        raise AnalysisError(f"{AUDIT}: activated items and {TIMEPOINTS} cases differ")
    pairs = (
        ("config_version", "activation_config_version"),
        ("config_hash", "activation_config_hash"),
        ("arm", "realised_arm"),
        ("arm", "planned_arm"),
        ("schedule_id", "schedule_id_audit"),
        ("case_position", "case_position_audit"),
        ("lifecycle_state", "lifecycle_state_audit"),
        ("incomplete_reason", "incomplete_reason_audit"),
    )
    for case_col, audit_col in pairs:
        if not all(_same(a, b) for a, b in zip(joined[case_col], joined[audit_col], strict=True)):
            raise AnalysisError(f"{AUDIT}: {audit_col} differs from the case's {case_col}")

    _replacement_links(audit)
    links = activated[
        ["clinician_id", "patient_id", "replaces_patient_id", "replaced_by_patient_id"]
    ]
    return cases.merge(links, on=["clinician_id", "patient_id"], how="left", validate="1:1")


def _replacement_links(audit: pd.DataFrame) -> None:
    """Each replacement item names an incomplete original that names it back."""
    by_patient = {(r.clinician_id, r.patient_id): r for r in audit.itertuples(index=False)}
    replacing = audit[audit["replacement_id"] != ""]
    replaced = audit[audit["replaced_by_replacement_id"] != ""]

    for r in replacing.itertuples(index=False):
        original = by_patient.get((r.clinician_id, r.replaces_patient_id))
        if (
            original is None
            or not original.activated
            or original.lifecycle_state != LifecycleState.INCOMPLETE
            or original.replaced_by_replacement_id != r.replacement_id
            or original.replaced_by_patient_id != r.patient_id
        ):
            raise AnalysisError(f"{AUDIT}: replacement of patient {r.patient_id!r} is not linked")
        if not pd.isna(r.replacement_activated_at) and not r.activated:
            raise AnalysisError(f"{AUDIT}: replacement {r.patient_id!r} activated without a case")

    for r in replaced.itertuples(index=False):
        target = by_patient.get((r.clinician_id, r.replaced_by_patient_id))
        if target is None or target.replacement_id != r.replaced_by_replacement_id:
            raise AnalysisError(f"{AUDIT}: replaced case {r.patient_id!r} is not linked")


def _counts(
    counts: pd.DataFrame,
    versions: Mapping[str, ConfigVersion],
    audit: pd.DataFrame,
    cases: pd.DataFrame,
    timepoints: pd.DataFrame,
    answers: pd.DataFrame,
) -> None:
    """``configuration_counts.csv`` equals the counts recomputed from the rows."""
    _unique(COUNTS, counts, ("config_version",))
    if list(counts["config_version"]) != list(versions):
        raise AnalysisError(f"{COUNTS}: rows differ from the registered versions")

    for row in counts.itertuples(index=False):
        v = row.config_version
        mine = cases[cases["config_version"] == v]
        states = mine["lifecycle_state"]
        expected = {
            "scheduled_items_generated": int((audit["generation_config_version"] == v).sum()),
            "activated_cases": len(mine),
            "completed_cases": int((states == LifecycleState.COMPLETED).sum()),
            "incomplete_cases": int((states == LifecycleState.INCOMPLETE).sum()),
            "open_cases": int(states.isin(OPEN_STATES).sum()),
            "expected_timepoints": int((timepoints["config_version"] == v).sum()),
            "reached_primary_timepoints": int(
                timepoints.loc[timepoints["config_version"] == v, "timepoint_reached"].sum()
            ),
            "answer_rows_present": int(
                (
                    (answers["config_version"] == v)
                    & (answers["response_status"] == ResponseStatus.ANSWERED)
                ).sum()
            ),
        }
        for column, value in expected.items():
            if getattr(row, column) != value:
                raise AnalysisError(f"{COUNTS}: {column} of {v!r} differs from the rows")


def _clinicians(
    clinicians: pd.DataFrame, cases: pd.DataFrame, audit: pd.DataFrame, practice_included: bool
) -> None:
    _unique(CLINICIANS, clinicians, ("clinician_id",))
    listed = set(clinicians["clinician_id"])
    measured = set(cases["clinician_id"]) | set(audit["clinician_id"])
    if not measured <= listed:
        raise AnalysisError(f"{CLINICIANS}: a case or schedule clinician is not listed")
    if not practice_included and listed != measured:
        raise AnalysisError(f"{CLINICIANS}: lists a clinician without a case or schedule")


def _events(events: pd.DataFrame, cases: pd.DataFrame) -> None:
    """Event ids unique; an event of a case carries that case's configuration."""
    _unique(EVENTS, events, ("event_id",))
    pinned = events[events["config_version"] != ""]
    joined = pinned.merge(
        cases[["clinician_id", "patient_id", *PROVENANCE]],
        on=["clinician_id", "patient_id"],
        how="inner",
        suffixes=("", "_case"),
    )
    for column in PROVENANCE:
        if (joined[column] != joined[f"{column}_case"]).any():
            raise AnalysisError(f"{EVENTS}: an event differs from its case's {column}")
