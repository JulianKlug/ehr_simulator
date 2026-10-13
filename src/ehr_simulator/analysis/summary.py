"""S12a descriptive summaries of a validated bundle (pure, no I/O).

Spec: ``specs/session-12-phase25-release.md`` §3, §5. Descriptive only: no
test, interval or effect estimate. Every table is keyed by
``config_version`` (never pooled) and the ITT ``arm`` of the realised
activation, over a full grid (pinned timepoints × pinned questions × both
arms) so an empty cell is an explicit zero count, never a missing row::

    case_flow            planned schedule items vs activated cases + replacements
    response_provenance  answered (clinician / rule), not applicable, missing by reason
    option_proportions   categorical / multi-select option shares (by answer source)
    numeric_responses    likert / probability n, median, min, max (by answer source)
    attention            elapsed (S10) and foreground / active (S11j) time
    intervention         AI delivered / viewed, failure, leakage, PP
    denominators         one long table: n, denominator, missing/unknown per measure

Unknown is never zero: a blank duration, delivery, viewing or PP cell is
counted as unknown, and medians use known values only. Free text is never
summarised beyond its response status.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum

import pandas as pd

from ehr_simulator.analysis.bundle import ConfigVersion, ValidatedStudyBundle
from ehr_simulator.analysis.schema import (
    AnswerSource,
    Arm,
    FailureReason,
    LifecycleState,
    MissingReason,
    ResponseStatus,
    ResponseType,
    TelemetryStatus,
    TimepointTelemetry,
)

__all__ = [
    "AnalysisSummaries",
    "Cohort",
    "build_summaries",
    "case_flow",
    "cohort_status",
    "denominators",
    "intervention",
    "attention",
    "attention_observations",
    "numeric_observations",
    "numeric_responses",
    "option_proportions",
    "response_provenance",
]

ARMS = (Arm.AI.value, Arm.NO_AI.value)
SOURCES = (AnswerSource.CLINICIAN.value, AnswerSource.RULE.value)
OPTION_TYPES = (ResponseType.CATEGORICAL, ResponseType.MULTI_SELECT)
NUMERIC_TYPES = (ResponseType.LIKERT, ResponseType.PROBABILITY)
OPEN_STATES = (LifecycleState.ACTIVE, LifecycleState.PAUSED)

#: The only telemetry status whose foreground / active seconds are exact.
VALID_TELEMETRY = TelemetryStatus.COMPLETE

POPULATION_CASES = "activated measured cases"
POPULATION_REACHED = "reached primary observations"
POPULATION_APPLICABLE = "reached primary observations where the question applied"


class Cohort(StrEnum):
    HAS_CASES = "has_cases"
    NO_ACTIVATED_CASES = "no_activated_cases"


@dataclass(frozen=True)
class AnalysisSummaries:
    cohort: Cohort
    case_flow: pd.DataFrame
    response_provenance: pd.DataFrame
    option_proportions: pd.DataFrame
    numeric_responses: pd.DataFrame
    attention: pd.DataFrame
    intervention: pd.DataFrame
    denominators: pd.DataFrame


def build_summaries(bundle: ValidatedStudyBundle) -> AnalysisSummaries:
    tables = {
        "case_flow": case_flow(bundle),
        "response_provenance": response_provenance(bundle),
        "option_proportions": option_proportions(bundle),
        "numeric_responses": numeric_responses(bundle),
        "attention": attention(bundle),
        "intervention": intervention(bundle),
    }
    return AnalysisSummaries(
        cohort=cohort_status(bundle), denominators=denominators(**tables), **tables
    )


def cohort_status(bundle: ValidatedStudyBundle) -> Cohort:
    return Cohort.HAS_CASES if bundle.has_cases else Cohort.NO_ACTIVATED_CASES


# ---------------------------------------------------------------------------
# Grids
# ---------------------------------------------------------------------------


def _questions(version: ConfigVersion, types: tuple[ResponseType, ...] | None = None):
    for q in version.questions.questions:
        if types is None or q.response_type in types:
            yield q


def _timepoint_grid(bundle: ValidatedStudyBundle) -> Iterator[tuple[str, str, int]]:
    for version in bundle.versions.values():
        for arm in ARMS:
            for t_index in range(len(version.timepoints_minutes)):
                yield version.version, arm, t_index


def _frame(rows: list[dict], columns: list[str], ints: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=columns)
    return frame.astype({c: "Int64" for c in ints})


def _observations(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """Answers with their timepoint's ``timepoint_reached``."""
    reached = bundle.timepoints[
        ["clinician_id", "patient_id", "t_index", "timepoint_reached", "arm"]
    ]
    return bundle.answers.merge(reached, on=["clinician_id", "patient_id", "t_index"])


# ---------------------------------------------------------------------------
# Case flow
# ---------------------------------------------------------------------------

CASE_FLOW_COUNTS = [
    "planned_items",
    "activated",
    "completed",
    "incomplete",
    "open",
    "state_unknown",
    "replacement_planned",
    "replacement_activated",
    "replacement_pending",
]


def case_flow(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """Planned schedule items (generation version, planned arm) beside
    activated cases (activation version, realised arm).

    A planned item is never an activated case; replacements are counted on
    the original incomplete case (planned / activated / pending), the
    replacement itself once more as an activated case of its own.
    """
    audit, cases = bundle.audit, bundle.cases
    originals = audit[audit["replaced_by_replacement_id"] != ""]
    replacement_rows = audit[audit["replacement_id"] != ""].set_index(
        ["clinician_id", "patient_id"]
    )

    rows = []
    for version in bundle.versions:
        for arm in ARMS:
            mine = cases[(cases["config_version"] == version) & (cases["arm"] == arm)]
            states = mine["lifecycle_state"]
            lost = originals[
                (originals["activation_config_version"] == version)
                & (originals["realised_arm"] == arm)
            ]
            activated_replacements = sum(
                bool(replacement_rows.loc[(r.clinician_id, r.replaced_by_patient_id), "activated"])
                for r in lost.itertuples(index=False)
            )
            rows.append(
                {
                    "config_version": version,
                    "arm": arm,
                    "planned_items": int(
                        (
                            (audit["generation_config_version"] == version)
                            & (audit["planned_arm"] == arm)
                        ).sum()
                    ),
                    "activated": len(mine),
                    "completed": int((states == LifecycleState.COMPLETED).sum()),
                    "incomplete": int((states == LifecycleState.INCOMPLETE).sum()),
                    "open": int(states.isin(OPEN_STATES).sum()),
                    "state_unknown": int(states.isna().sum()),
                    "replacement_planned": len(lost),
                    "replacement_activated": activated_replacements,
                    "replacement_pending": len(lost) - activated_replacements,
                }
            )
    return _frame(rows, ["config_version", "arm", *CASE_FLOW_COUNTS], CASE_FLOW_COUNTS)


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------

MISSING_COLUMNS = [f"missing_{reason.value}" for reason in MissingReason]
PROVENANCE_COUNTS = [
    "observations",
    "reached",
    "reached_applicable",
    "answered_clinician",
    "answered_rule",
    "reached_answered",
    "reached_value_exported",
    "not_applicable",
    "value_not_exported",
    *MISSING_COLUMNS,
]
QUESTION_KEYS = ["config_version", "question_id", "response_type", "t_index", "arm"]


def response_provenance(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """Per version × question × timepoint × arm, over every case timepoint.

    A hidden question (``not_applicable``) is never missing; rule answers
    are counted apart from clinician answers.
    """
    obs = _observations(bundle)
    groups = dict(tuple(obs.groupby(["config_version", "question_id", "t_index", "arm"])))
    rows = []
    for version, question, t_index, arm in _question_grid(bundle):
        g = groups.get((version, question.question_id, t_index, arm), obs.iloc[0:0])
        status, reached = g["response_status"], g["timepoint_reached"]
        answered = status == ResponseStatus.ANSWERED
        row = {
            "config_version": version,
            "question_id": question.question_id,
            "response_type": question.response_type,
            "t_index": t_index,
            "arm": arm,
            "observations": len(g),
            "reached": int(reached.sum()),
            "reached_applicable": int((reached & (status != ResponseStatus.NOT_APPLICABLE)).sum()),
            "answered_clinician": int((g["answer_source"] == AnswerSource.CLINICIAN).sum()),
            "answered_rule": int((g["answer_source"] == AnswerSource.RULE).sum()),
            "reached_answered": int((reached & answered).sum()),
            "reached_value_exported": int((reached & g["value_exported"]).sum()),
            "not_applicable": int((status == ResponseStatus.NOT_APPLICABLE).sum()),
            "value_not_exported": int((answered & ~g["value_exported"]).sum()),
        }
        for reason, column in zip(MissingReason, MISSING_COLUMNS, strict=True):
            row[column] = int((g["missing_reason"] == reason).sum())
        rows.append(row)
    return _frame(rows, [*QUESTION_KEYS, *PROVENANCE_COUNTS], ["t_index", *PROVENANCE_COUNTS])


def _question_grid(bundle: ValidatedStudyBundle, types: tuple[ResponseType, ...] | None = None):
    for version_id, arm, t_index in _timepoint_grid(bundle):
        for question in _questions(bundle.versions[version_id], types):
            yield version_id, question, t_index, arm


def _valued(bundle: ValidatedStudyBundle) -> dict[tuple, pd.DataFrame]:
    """Answers with an exported value, by version × question × t × arm × source."""
    obs = _observations(bundle)
    obs = obs[obs["value_exported"]]
    keys = ["config_version", "question_id", "t_index", "arm", "answer_source"]
    return dict(tuple(obs.groupby(keys)))


def option_proportions(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """Share of answers naming each option, historical option order.

    Multi-select options are independent shares (they need not sum to one).
    ``proportion`` is ``<NA>`` when no answer exists (never 0 / 0).
    """
    groups = _valued(bundle)
    rows = []
    for version, question, t_index, arm in _question_grid(bundle, OPTION_TYPES):
        for source in SOURCES:
            g = groups.get((version, question.question_id, t_index, arm, source))
            selections = list(g["value_options"]) if g is not None else []
            for order, option in enumerate(question.options):
                count = sum(option in s for s in selections)
                rows.append(
                    {
                        "config_version": version,
                        "question_id": question.question_id,
                        "response_type": question.response_type,
                        "t_index": t_index,
                        "arm": arm,
                        "answer_source": source,
                        "option_order": order,
                        "option": option,
                        "count": count,
                        "answers": len(selections),
                        "proportion": count / len(selections) if selections else pd.NA,
                    }
                )
    frame = _frame(
        rows,
        [
            *QUESTION_KEYS,
            "answer_source",
            "option_order",
            "option",
            "count",
            "answers",
            "proportion",
        ],
        ["t_index", "option_order", "count", "answers"],
    )
    return frame.astype({"proportion": "Float64"})


def numeric_observations(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """One row per exported likert / probability value (for point plots)."""
    obs = _observations(bundle)
    numeric = obs[obs["value_exported"] & obs["response_type"].isin(list(NUMERIC_TYPES))]
    return numeric[[*QUESTION_KEYS, "answer_source", "value_number"]].reset_index(drop=True)


def numeric_responses(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """n, median, min and max of the exported numeric values; ``<NA>`` when n = 0."""
    groups = _valued(bundle)
    rows = []
    for version, question, t_index, arm in _question_grid(bundle, NUMERIC_TYPES):
        for source in SOURCES:
            g = groups.get((version, question.question_id, t_index, arm, source))
            values = g["value_number"] if g is not None else pd.Series([], dtype="Float64")
            n = len(values)
            rows.append(
                {
                    "config_version": version,
                    "question_id": question.question_id,
                    "response_type": question.response_type,
                    "t_index": t_index,
                    "arm": arm,
                    "answer_source": source,
                    "n": n,
                    "median": values.median() if n else pd.NA,
                    "min": values.min() if n else pd.NA,
                    "max": values.max() if n else pd.NA,
                }
            )
    frame = _frame(
        rows, [*QUESTION_KEYS, "answer_source", "n", "median", "min", "max"], ["t_index", "n"]
    )
    return frame.astype({"median": "Float64", "min": "Float64", "max": "Float64"})


# ---------------------------------------------------------------------------
# Attention and intervention
# ---------------------------------------------------------------------------

TELEMETRY_COLUMNS = [f"telemetry_{status.value}" for status in TimepointTelemetry]
TIMEPOINT_KEYS = ["config_version", "t_index", "arm"]
ATTENTION_MEASURES = ("elapsed", "foreground", "active")


def _reached_groups(bundle: ValidatedStudyBundle) -> dict[tuple, pd.DataFrame]:
    tp = bundle.timepoints
    return dict(tuple(tp[tp["timepoint_reached"]].groupby(TIMEPOINT_KEYS)))


def attention(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """Reached primary observations: S10 wall clock elapsed beside S11j
    foreground / active seconds, the latter only where ``telemetry_status``
    is ``complete``. Every other status is counted, never read as 0."""
    groups = _reached_groups(bundle)
    empty = bundle.timepoints.iloc[0:0]
    rows = []
    for version, arm, t_index in _timepoint_grid(bundle):
        g = groups.get((version, t_index, arm), empty)
        valid = g[g["telemetry_status"] == VALID_TELEMETRY]
        row = {"config_version": version, "t_index": t_index, "arm": arm, "reached": len(g)}
        for measure in ATTENTION_MEASURES:
            source = g if measure == "elapsed" else valid
            values = source[f"{measure}_seconds"].dropna()
            row[f"{measure}_n"] = len(values)
            row[f"{measure}_median_seconds"] = values.median() if len(values) else pd.NA
        for status, column in zip(TimepointTelemetry, TELEMETRY_COLUMNS, strict=True):
            row[column] = int((g["telemetry_status"] == status).sum())
        rows.append(row)

    counts = ["reached", *(f"{m}_n" for m in ATTENTION_MEASURES), *TELEMETRY_COLUMNS]
    medians = [f"{m}_median_seconds" for m in ATTENTION_MEASURES]
    columns = [*TIMEPOINT_KEYS, "reached"]
    for m in ATTENTION_MEASURES:
        columns += [f"{m}_n", f"{m}_median_seconds"]
    frame = _frame(rows, [*columns, *TELEMETRY_COLUMNS], ["t_index", *counts])
    return frame.astype({c: "Float64" for c in medians})


def attention_observations(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """One row per known duration of a reached observation (for point plots).

    Same eligibility as :func:`attention`: foreground / active only where
    ``telemetry_status`` is ``complete``. A blank duration has no row,
    never a 0 second one.
    """
    tp = bundle.timepoints
    reached = tp[tp["timepoint_reached"]]
    valid = reached["telemetry_status"] == VALID_TELEMETRY
    parts = []
    for measure in ATTENTION_MEASURES:
        source = reached if measure == "elapsed" else reached[valid]
        part = source[[*TIMEPOINT_KEYS, f"{measure}_seconds"]].rename(
            columns={f"{measure}_seconds": "seconds"}
        )
        parts.append(part.dropna(subset=["seconds"]).assign(measure=measure))
    frame = pd.concat(parts, ignore_index=True)
    return frame[[*TIMEPOINT_KEYS, "measure", "seconds"]].astype(
        {"t_index": "Int64", "seconds": "Float64"}
    )


FAILURE_COLUMNS = [f"failure_{reason.value}" for reason in FailureReason]
INTERVENTION_COUNTS = [
    "reached",
    "delivered_true",
    "delivered_false",
    "delivered_unknown",
    "viewed_true",
    "viewed_false",
    "viewed_unknown",
    "viewed_not_applicable",
    "intervention_failure",
    *FAILURE_COLUMNS,
    "leakage",
    "pp_compliant",
    "pp_noncompliant",
    "pp_indeterminate",
]


def intervention(bundle: ValidatedStudyBundle) -> pd.DataFrame:
    """AI delivery, viewing and PP over reached primary observations, grouped
    by ITT arm. Delivered / viewed apply to the AI arm only (``<NA>`` on the
    no AI arm, whose AI exposure is ``leakage``); no AI viewing is
    ``viewed_not_applicable``, never false."""
    groups = _reached_groups(bundle)
    empty = bundle.timepoints.iloc[0:0]
    rows = []
    for version, arm, t_index in _timepoint_grid(bundle):
        g = groups.get((version, t_index, arm), empty)
        is_ai = arm == Arm.AI
        delivered, viewed = g["ai_delivered"], g["ai_viewed"]
        determinate = g["pp_determinate"]
        row = {
            "config_version": version,
            "t_index": t_index,
            "arm": arm,
            "reached": len(g),
            "delivered_true": int(delivered.eq(True).sum()) if is_ai else pd.NA,
            "delivered_false": int(delivered.eq(False).sum()) if is_ai else pd.NA,
            "delivered_unknown": int(delivered.isna().sum()) if is_ai else pd.NA,
            "viewed_true": int(viewed.eq(True).sum()) if is_ai else pd.NA,
            "viewed_false": int(viewed.eq(False).sum()) if is_ai else pd.NA,
            "viewed_unknown": int(viewed.isna().sum()) if is_ai else pd.NA,
            "viewed_not_applicable": 0 if is_ai else len(g),
            "intervention_failure": int(g["intervention_failure"].sum()),
            "leakage": int(g["intervention_leakage"].sum()),
            "pp_compliant": int((determinate & g["pp_compliant"].eq(True)).sum()),
            "pp_noncompliant": int((determinate & g["pp_compliant"].eq(False)).sum()),
            "pp_indeterminate": int((~determinate).sum()),
        }
        reasons = g["intervention_failure_reasons"]
        for reason, column in zip(FailureReason, FAILURE_COLUMNS, strict=True):
            row[column] = sum(reason in r for r in reasons)
        rows.append(row)
    return _frame(rows, [*TIMEPOINT_KEYS, *INTERVENTION_COUNTS], ["t_index", *INTERVENTION_COUNTS])


# ---------------------------------------------------------------------------
# Denominators
# ---------------------------------------------------------------------------

DENOMINATOR_COLUMNS = [
    "summary",
    "measure",
    "config_version",
    "arm",
    "t_index",
    "question_id",
    "population",
    "n",
    "denominator",
    "missing_unknown",
]


def denominators(
    *,
    case_flow: pd.DataFrame,
    response_provenance: pd.DataFrame,
    attention: pd.DataFrame,
    intervention: pd.DataFrame,
    **_values: pd.DataFrame,
) -> pd.DataFrame:
    """One row per reported measure: numerator, denominator, missing/unknown."""
    rows: list[dict] = []

    def add(summary, measure, src, population, n, denominator, missing, question=None):
        rows.append(
            {
                "summary": summary,
                "measure": measure,
                "config_version": src["config_version"],
                "arm": src["arm"],
                "t_index": src.get("t_index", pd.NA),
                "question_id": question if question is not None else pd.NA,
                "population": population,
                "n": n,
                "denominator": denominator,
                "missing_unknown": missing,
            }
        )

    for r in case_flow.to_dict("records"):
        add(
            "case_flow",
            "completed",
            r,
            POPULATION_CASES,
            r["completed"],
            r["activated"],
            r["open"] + r["state_unknown"],
        )

    for r in response_provenance.to_dict("records"):
        unanswered = r[f"missing_{MissingReason.REACHED_UNANSWERED}"]
        unknown = r[f"missing_{MissingReason.UNKNOWN}"]
        add(
            "response_provenance",
            "answered",
            r,
            POPULATION_APPLICABLE,
            r["reached_answered"],
            r["reached_applicable"],
            unanswered + unknown,
            r["question_id"],
        )

        # Free text is withheld unless explicitly exported: never missing.
        if r["response_type"] == ResponseType.FREE_TEXT:
            continue

        exported = r["reached_value_exported"]
        add(
            "response_provenance",
            "value_exported",
            r,
            POPULATION_APPLICABLE,
            exported,
            r["reached_applicable"],
            r["reached_applicable"] - exported,
            r["question_id"],
        )

    for r in attention.to_dict("records"):
        for measure in ATTENTION_MEASURES:
            n = r[f"{measure}_n"]
            add("attention", measure, r, POPULATION_REACHED, n, r["reached"], r["reached"] - n)

    for r in intervention.to_dict("records"):
        if r["arm"] == Arm.AI:
            add(
                "intervention",
                "ai_delivered",
                r,
                POPULATION_REACHED,
                r["delivered_true"],
                r["reached"],
                r["delivered_unknown"],
            )
            add(
                "intervention",
                "ai_viewed",
                r,
                POPULATION_REACHED,
                r["viewed_true"],
                r["reached"],
                r["viewed_unknown"],
            )
        else:
            add("intervention", "leakage", r, POPULATION_REACHED, r["leakage"], r["reached"], 0)
        add(
            "intervention",
            "pp_compliant",
            r,
            POPULATION_REACHED,
            r["pp_compliant"],
            r["reached"],
            r["pp_indeterminate"],
        )

    frame = _frame(rows, DENOMINATOR_COLUMNS, ["t_index", "n", "denominator", "missing_unknown"])
    return frame.astype({"question_id": "string"})
