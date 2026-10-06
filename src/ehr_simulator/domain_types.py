"""Domain types shared by the db layer and the pure derivation modules.

The pure modules (``behavioral_timing``, ``panel_exposure``,
``study_variables``) read these shapes but must not depend on the db layer;
the db modules re-export them under their historical names::

    db/telemetry, db/arm_assignments, db/case_lifecycle ──┐
                                                          ├──► domain_types
    behavioral_timing, panel_exposure, study_variables ───┘
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "ARM_AI",
    "ARM_NO_AI",
    "CLAIMED_KIND",
    "CONFLICT_KIND",
    "LEASE_EXPIRED_KIND",
    "RELEASED_KIND",
    "CaseState",
    "RenderRow",
    "TabAuditRow",
    "TelemetryRow",
]

ARM_AI = "ai"
ARM_NO_AI = "no_ai"

#: S11m: a tab was granted / refused the lease for a render, gave it up, or
#: lost it to the TTL.
CLAIMED_KIND = "tab.claimed"
CONFLICT_KIND = "tab.conflict"
RELEASED_KIND = "tab.released"
LEASE_EXPIRED_KIND = "tab.lease_expired"


class CaseState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class RenderRow:
    """One server-rendered view. ``payload``: ``t_index``, ``visit_kind``
    and the S11l ``ai`` delivery value."""

    event_id: int
    render_id: str
    session_id: str | None
    clinician_id: str
    patient_id: str | None
    timepoint: float | None
    payload: dict[str, Any]

    @property
    def t_index(self) -> int:
        return int(self.payload["t_index"])

    @property
    def visit_kind(self) -> str:
        return str(self.payload["visit_kind"])


@dataclass(frozen=True)
class TelemetryRow:
    """One browser event bound to a render."""

    event_id: int
    render_id: str
    tab_id: str
    kind: str
    client_seq: int
    client_mono_ms: float
    client_ts: str | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class TabAuditRow:
    """One S11m ``tab.*`` audit row: who held (or gave up) a render's lease,
    in server ``event_id`` order."""

    event_id: int
    kind: str
    tab_id: str
    render_id: str | None
