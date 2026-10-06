"""S11n bundle types, refusal error and shared export constants."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

EXPORT_SCHEMA_VERSION = "phase2_export_v1"

#: Event kinds carried by ``behavioral_events.csv`` (operational identity,
#: operator maintenance and practice events are excluded).
RESEARCH_EVENT_PREFIXES = (
    "answer.",
    "advance.",
    "timepoint.",
    "browser.",
    "panel.",
    "tab.",
    "case.",
    "session.",
)

PRIMARY = "primary"
REVISIT = "revisit"
NOT_CONFIGURED = "not_configured"
LIST_SEPARATOR = "|"
FREE_TEXT = "free-text"
TRUE = "true"
FALSE = "false"


class Phase2ExportError(ValueError):
    """The bundle cannot be produced faithfully; nothing is published.

    Messages name the failure shape and coordinates, never answer content
    or clinician names.
    """


class PracticeExport(StrEnum):
    EXCLUDE = "exclude"
    INCLUDE = "include"


class KeyfileRequest(StrEnum):
    NONE = "none"
    REQUESTED = "requested"


@dataclass(frozen=True)
class Table:
    """One bundle CSV: raw cells, guarded and quoted by the writer."""

    name: str
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class Phase2Bundle:
    study_id: str
    source_schema_version: int
    config_versions: tuple[str, ...]
    practice_included: bool
    tables: tuple[Table, ...]
    clinician_ids: tuple[str, ...]
    keyfile_rows: tuple[tuple[str, str], ...] | None = None
