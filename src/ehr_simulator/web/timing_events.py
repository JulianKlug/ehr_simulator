"""S10 behavioural event emission — the single place the new kinds are
written.

Callers:

* ``web.routes.patient_timepoint`` (GET) — one ``timepoint.enter`` per
  render of the **current editable frontier** (``pane_mode == "open"``).
  Refreshes and resume-after-redirect legitimately duplicate the enter;
  the exporter pairs the first valid (enter, exit), so duplicates are
  append-only behavioural data, never a correctness issue.
* ``web.routes._advance_response`` (the 200 "advanced" path) — one
  ``timepoint.enter`` for the pane the response now renders: the htmx swap
  shows it without a new GET, so the enter rides on the advance response.
  The 303/409/412 paths write neither (the browser follows to a full
  GET, which records it, or the advance was blocked/stale).
* ``web.gating.advance`` — one ``timepoint.exit`` (``reason="advance"`` or
  ``"finish"``) per successful advance. Gating wraps the progress /
  sessions state write and this event in a single explicit transaction
  (``commit=False`` on both, one ``conn.commit()``, rollback on failure),
  so a failed exit never leaves an advanced frontier — or a closed
  session — behind. The
locked/412 outcomes emit nothing.

The helpers raise on DB failure. On the GET path the enter is
best-effort: :func:`record_enter` runs only after a successful render, so
a failed render leaves no enter without a matching exit, and a later
advance failure has no enter to orphan.

S11j adds :func:`record_render`: one ``timepoint.render`` per successful
study render whose pinned config has ``telemetry``, naming the view by its
``render_id`` so browser telemetry can bind to it.

These are server-side behavioural facts: the payload carries the ``t_index``
(``timepoint`` already holds the minutes; ``client_ts`` / ``client_seq`` do
not apply to server-emitted events). The authoritative timestamp is
``server_ts`` (SQLite ``CURRENT_TIMESTAMP``), never a browser clock.
"""

from __future__ import annotations

import sqlite3
import uuid
from enum import StrEnum
from typing import Any

from ehr_simulator.db import events
from ehr_simulator.db.telemetry import RENDER_KIND
from ehr_simulator.timing import ENTER_KIND, EXIT_KIND
from ehr_simulator.web.study_session import SessionContext

__all__ = [
    "REVISIT_KIND",
    "VisitKind",
    "new_render_id",
    "record_enter",
    "record_exit",
    "record_render",
    "record_revisit",
]


class VisitKind(StrEnum):
    """S11i: ``primary`` ⇔ the editable frontier; anything else is a revisit."""

    PRIMARY = "primary"
    REVISIT = "revisit"


#: S11i: a render behind the editable frontier (an occurrence marker, never
#: a timing interval — S10 enter/exit stay untouched).
REVISIT_KIND = "timepoint.revisit"


def record_enter(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    t_minutes: float,
) -> None:
    """One ``timepoint.enter`` for ``t_index`` (``t_minutes`` must be the
    configured minutes for that index — the caller already resolved the
    timepoint for the pane about to render)."""
    events.append(
        conn,
        app_state=app_state,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=float(t_minutes),
        # type: ignore[arg-type] -- ENTER_KIND is an EventKind member
        kind=ENTER_KIND,
        payload={"t_index": t_index},
    )


def record_exit(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    t_minutes: float,
    reason: str,
    commit: bool = True,
) -> None:
    """One ``timepoint.exit`` for the pane just left. Only
    :func:`ehr_simulator.web.gating.advance` calls this, and only on the
    advanced/finished outcomes — exactly one per timepoint.

    ``commit=False`` joins the connection's open transaction (and skips
    the write-counter bump) so gating can atomically commit the state
    write and this event, rolling both back on failure.
    """
    if reason not in ("advance", "finish"):
        raise ValueError(f"invalid exit reason: {reason!r}")
    events.append(
        conn,
        app_state=app_state,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=float(t_minutes),
        # type: ignore[arg-type] -- EXIT_KIND is an EventKind member
        kind=EXIT_KIND,
        payload={"t_index": t_index, "reason": reason},
        commit=commit,
    )


def record_revisit(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    t_minutes: float,
) -> None:
    """S11i: one ``timepoint.revisit`` after a successful read-only render."""
    events.append(
        conn,
        app_state=app_state,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=float(t_minutes),
        kind=REVISIT_KIND,
        payload={"t_index": t_index},
    )


def new_render_id() -> str:
    """S11j: a fresh, unguessable id for one rendered view."""
    return uuid.uuid4().hex


def record_render(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    ctx: SessionContext,
    clinician_id: str,
    patient_id: str,
    t_index: int,
    t_minutes: float,
    render_id: str,
    visit_kind: VisitKind,
    ai_delivery: dict[str, str],
) -> None:
    """S11j: one ``timepoint.render`` after a successful telemetry render.

    ``ai_delivery`` is the S11l evidence of what the AI surface showed
    (``{"ai": "shown"}``, ``{"ai": "unavailable", "ai_unavailable_reason":
    "missing_row"}``, …).
    """
    events.append(
        conn,
        app_state=app_state,
        session_id=ctx.session_id,
        clinician_id=clinician_id,
        patient_id=patient_id,
        timepoint=float(t_minutes),
        kind=RENDER_KIND,  # type: ignore[arg-type]
        payload={"t_index": t_index, "visit_kind": str(visit_kind), **ai_delivery},
        render_id=render_id,
    )
