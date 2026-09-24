"""Question gating service (S9b): who may see what, and when the frontier moves.

::

    t_index:        0        1        2        (timepoint_count = 3)
    unlocked = 1:   locked   OPEN     ─ gate ─▶ not viewable
                    read-only editable
    completed:      locked   locked   locked    every pane read-only

- viewable  ⇔ t_index ≤ unlocked_t_index
- open      ⇔ t_index == unlocked_t_index and not completed
- complete  ⇔ every ``required`` question has a saved row for the cell
- advance   moves the frontier by exactly one; the last timepoint's
  advance marks the walk complete and closes the session instead.

Completeness is computed here, once, on the same ``saved_answers`` mapping
the pane pre-fills from — the browser never counts badges. Writes go
state-first (``progress``, ``sessions``) and events after, like S9a's
``record_answer`` — and S10 makes a successful advance atomic: the state
write, ``advance.ok`` and ``timepoint.exit`` ride one explicit
transaction (``commit=False`` everywhere, a single ``conn.commit()``), so
a failed exit rolls the frontier (and the final close) back with it.
``write_counter`` is bumped exactly once, after that commit succeeds.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from ehr_simulator.config.questions import Questions
from ehr_simulator.db import events, progress, sessions
from ehr_simulator.db.events import EventKind
from ehr_simulator.logging import get_logger
from ehr_simulator.web import timing_events
from ehr_simulator.web.answer_capture import (
    normalize_client_seq,
    normalize_client_ts,
    saved_answers,
)
from ehr_simulator.web.study_session import NOT_STARTED_T_INDEX, Frontier, SessionContext

PaneMode = Literal["open", "locked"]
AdvanceOutcome = Literal["advanced", "finished", "blocked", "stale"]
ProgressState = Literal["not_started", "in_progress", "complete"]

SESSION_END_REASON_COMPLETE = "patient_complete"


@dataclass(frozen=True)
class Completeness:
    remaining: tuple[str, ...]  # required question_ids without a row, questions.yaml order

    @property
    def complete(self) -> bool:
        return not self.remaining


@dataclass(frozen=True)
class AdvanceResult:
    outcome: AdvanceOutcome
    unlocked_t_index: int  # the frontier after the call
    remaining: tuple[str, ...]  # non-empty only for "blocked"


@dataclass(frozen=True)
class PatientProgress:
    state: ProgressState
    unlocked_t_index: int  # == resume target; 0 when not started
    timepoint_count: int


# ---------------------------------------------------------------------------
# Pure predicates
# ---------------------------------------------------------------------------


def completeness(questions: Questions, saved: Mapping[str, object]) -> Completeness:
    remaining = tuple(
        q.question_id for q in questions.questions if q.required and q.question_id not in saved
    )
    return Completeness(remaining=remaining)


def is_viewable(frontier: Frontier, t_index: int) -> bool:
    return t_index <= frontier.unlocked_t_index


def pane_mode(frontier: Frontier, t_index: int) -> PaneMode:
    if t_index == frontier.unlocked_t_index and not frontier.completed:
        return "open"
    return "locked"


def required_count(questions: Questions) -> int:
    return sum(1 for q in questions.questions if q.required)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def progress_overview(
    conn: sqlite3.Connection,
    *,
    clinician_id: str,
    patient_ids: Sequence[str],
    timepoint_count: int,
) -> dict[str, PatientProgress]:
    """Per-patient frontier for the index page and the patient jumper.

    Total over ``patient_ids`` (a missing row is ``not_started``), so the
    templates can index it without a Jinja ``Undefined``.
    """
    rows = progress.list_for_clinician(conn, clinician_id)
    last = max(timepoint_count - 1, 0)
    overview: dict[str, PatientProgress] = {}
    for pid in patient_ids:
        row = rows.get(pid)
        if row is None:
            overview[pid] = PatientProgress("not_started", NOT_STARTED_T_INDEX, timepoint_count)
            continue
        unlocked = min(row.unlocked_t_index, last)
        state: ProgressState = "complete" if row.completed_at is not None else "in_progress"
        overview[pid] = PatientProgress(state, unlocked, timepoint_count)
    return overview


# ---------------------------------------------------------------------------
# The one write path
# ---------------------------------------------------------------------------


def advance(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    timepoints: Sequence[float],
    questions: Questions,
    client_ts: str | None = None,
    client_seq: str | None = None,
) -> AdvanceResult:
    """Try to leave ``t_index``. See the module docstring for the outcomes."""
    frontier = ctx.frontier
    if t_index != frontier.unlocked_t_index or frontier.completed:
        get_logger().warning(
            "advance from a stale timepoint",
            event_kind="advance.stale",
            requested=t_index,
            unlocked=frontier.unlocked_t_index,
            completed=frontier.completed,
        )
        return AdvanceResult("stale", frontier.unlocked_t_index, ())

    t_minutes = float(timepoints[t_index])
    saved = saved_answers(
        conn,
        clinician_id=clinician_id,
        patient_id=patient_id,
        t_minutes=t_minutes,
        questions=questions,
        config_hash=ctx.config_hash,
        config_version=ctx.config_version,
    )
    comp = completeness(questions, saved)
    n_required = required_count(questions)
    base_payload: dict[str, Any] = {
        "t_index": t_index,
        "answered_required": n_required - len(comp.remaining),
        "answered_total": len(saved),
        "required": n_required,
    }
    clock = {
        "client_ts": normalize_client_ts(client_ts),
        "client_seq": normalize_client_seq(client_seq),
    }

    def _event(
        kind: EventKind, timepoint: float | None, payload: dict[str, Any], *, commit: bool = True
    ) -> None:
        try:
            events.append(
                conn,
                session_id=ctx.session_id,
                clinician_id=clinician_id,
                patient_id=patient_id,
                timepoint=timepoint,
                kind=kind,
                payload=payload,
                app_state=app_state,
                **clock,
                commit=commit,
            )
        except Exception:
            # Make the hole in the event stream visible instead of silent,
            # then re-raise — the caller owns the transaction boundary.
            get_logger().exception(
                "event append failed after state write", event_kind="advance.event_lost", kind=kind
            )
            raise

    def _bump() -> None:
        # Bumped exactly once per successful advance, after the outer
        # commit succeeds (the ``commit=False`` writes skip their own bumps).
        if app_state is not None:
            app_state.write_counter = getattr(app_state, "write_counter", 0) + 1

    if not comp.complete:
        _event("advance.blocked", t_minutes, {**base_payload, "remaining": list(comp.remaining)})
        return AdvanceResult("blocked", frontier.unlocked_t_index, comp.remaining)

    is_last = t_index == len(timepoints) - 1
    if is_last:
        # S10: the completion write and its events are one transaction —
        # a failed ``timepoint.exit`` rolls the mark_complete / close back.
        try:
            progress.mark_complete(
                conn,
                clinician_id=clinician_id,
                patient_id=patient_id,
                unlocked_t_index=t_index,
                config_hash=ctx.config_hash,
                config_version=ctx.config_version,
                app_state=app_state,
                commit=False,
            )
            sessions.close(conn, ctx.session_id, commit=False)
            _event(
                "advance.ok",
                t_minutes,
                {**base_payload, "to_t_index": None, "final": True},
                commit=False,
            )
            timing_events.record_exit(
                conn,
                None,
                ctx=ctx,
                clinician_id=clinician_id,
                patient_id=patient_id,
                t_index=t_index,
                t_minutes=t_minutes,
                reason="finish",
                commit=False,
            )
            _event("session.end", None, {"reason": SESSION_END_REASON_COMPLETE}, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            get_logger().exception(
                "terminal advance transaction rolled back (state + events restored)",
                event_kind="advance.rollback",
            )
            raise
        _bump()
        return AdvanceResult("finished", t_index, ())

    # Compare-and-set: a racing second request finds the row already moved
    # and degrades to "stale" here instead of unlocking twice. mark_complete
    # above has no CAS: it is guarded by frontier.completed and by the single
    # shared connection; a multi-connection refactor must revisit it.
    # S10: the unlock and its events commit atomically (see the module
    # docstring); a failed ``timepoint.exit`` rolls the unlock back, and the
    # next attempt re-runs from the unchanged frontier.
    try:
        moved = progress.unlock(
            conn,
            clinician_id=clinician_id,
            patient_id=patient_id,
            from_t_index=t_index,
            to_t_index=t_index + 1,
            config_hash=ctx.config_hash,
            config_version=ctx.config_version,
            app_state=app_state,
            commit=False,
        )
        if not moved:
            # CAS miss (and possibly a failed INSERT OR IGNORE): discard the
            # open transaction; nothing durable happened.
            conn.rollback()
            get_logger().warning(
                "advance lost the frontier race", event_kind="advance.stale", requested=t_index
            )
            return AdvanceResult("stale", t_index + 1, ())
        _event(
            "advance.ok",
            t_minutes,
            {**base_payload, "to_t_index": t_index + 1, "final": False},
            commit=False,
        )
        # The sole producer rule: the exit pairs with the advance.ok above.
        timing_events.record_exit(
            conn,
            None,
            ctx=ctx,
            clinician_id=clinician_id,
            patient_id=patient_id,
            t_index=t_index,
            t_minutes=t_minutes,
            reason="advance",
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        get_logger().exception(
            "advance transaction rolled back (frontier + events restored)",
            event_kind="advance.rollback",
        )
        raise
    _bump()
    return AdvanceResult("advanced", t_index + 1, ())
