"""Observation mode (S11i): measured study data versus practice.

Stored on ``sessions``, ``progress`` and ``answers`` at insert and never
rewritten; a write whose mode differs from the stored row is refused.
"""

from __future__ import annotations

from enum import StrEnum


class ObservationMode(StrEnum):
    MEASURED = "measured"
    PRACTICE = "practice"
