"""``answers.csv`` rows (shared with the practice tables)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ehr_simulator.answer_codec import decode_stored_answer, deserialize_answer
from ehr_simulator.config.questions import Question, Questions
from ehr_simulator.config.study import FreeTextExport, StudyConfig
from ehr_simulator.db import answers
from ehr_simulator.export_phase2.cases import CaseRecord, _keys
from ehr_simulator.export_phase2.cells import _bool, _text, _ts
from ehr_simulator.export_phase2.model import FALSE, FREE_TEXT
from ehr_simulator.question_branching import AnswerSource, QuestionState, evaluate
from ehr_simulator.study_variables import ResponseProvenance, ResponseStatus


def _exports_free_text(study: StudyConfig) -> bool:
    """S11i: a study declaring ``study_behaviour`` exports free text only
    under ``include_explicit``; legacy configs export it as before."""
    behaviour = study.study_behaviour
    return (
        behaviour is None or behaviour.free_text.routine_export is FreeTextExport.INCLUDE_EXPLICIT
    )


def _answer_cells(
    study_id: str,
    keys: tuple[str, ...],
    questions: Questions,
    study: StudyConfig,
    t_index: int,
    rows: Mapping[tuple[int, str], answers.AnswerRow],
    recorded_at: Mapping[tuple[float, str], object],
    responses: Mapping[tuple[int, str], ResponseProvenance] | None,
) -> list[tuple[str, ...]]:
    """One row per question of one timepoint, questions.yaml order."""
    by_id = {q.question_id: q for q in questions.questions}
    values = {
        qid: deserialize_answer(by_id[qid], row.value)
        for (t, qid), row in rows.items()
        if t == t_index and row.answer_source != AnswerSource.RULE
    }
    free_text = _exports_free_text(study)
    out = []
    for item in evaluate(questions, values).questions:
        question: Question = item.question
        qid = question.question_id
        row = rows.get((t_index, qid))
        provenance = responses.get((t_index, qid)) if responses is not None else None
        status = provenance.status if provenance else _practice_status(item, row)
        exported = row is not None and (question.response_type != FREE_TEXT or free_text)
        value = decode_stored_answer(question, row.value) if exported and row else ""
        out.append(
            (
                *keys,
                qid,
                question.response_type,
                str(item.state),
                _bool(item.required_now),
                str(status),
                value,
                _bool(exported) if row is not None else FALSE,
                _text(row.answer_source if row else None),
                _text(row.derived_from_question_id if row else None),
                _text(provenance.missing_reason if provenance else None),
                _ts(recorded_at.get((float(row.timepoint), qid))) if row else "",
            )
        )
    return out


def _practice_status(item: Any, row: answers.AnswerRow | None) -> ResponseStatus:
    if item.state is QuestionState.HIDDEN:
        return ResponseStatus.NOT_APPLICABLE
    return ResponseStatus.ANSWERED if row is not None else ResponseStatus.MISSING


def _answers_rows(study_id: str, case: CaseRecord) -> list[tuple[str, ...]]:
    responses = {(r.t_index, r.question_id): r for r in case.variables.responses}
    out: list[tuple[str, ...]] = []
    for t_index in range(len(case.pinned.timepoints)):
        out.extend(
            _answer_cells(
                study_id,
                _keys(study_id, case, t_index),
                case.pinned.questions,
                case.pinned.study,
                t_index,
                case.answer_rows,
                case.recorded_at,
                responses,
            )
        )
    return out
