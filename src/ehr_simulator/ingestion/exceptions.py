"""Ingestion exceptions and issue records.

Implements the tiered adapter contract: strict mode raises on the first offending
row, lenient mode accumulates issues so a pilot session can surface "here are the
12 bad rows we dropped" rather than halting.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IngestionIssue:
    dataset: str
    patient_id: str | None
    row_idx: int | None
    reason: str


class AdapterError(Exception):
    """Raised when an ingestion adapter violates the canonical shape contract.

    In strict mode, raised on the first offending row. In non-strict mode,
    issues are accumulated and attached to the cleaned frame via
    ``frame.attrs["adapter_error"]``; the caller decides whether to raise.
    """

    def __init__(self, message: str, issues: list[IngestionIssue] | None = None) -> None:
        super().__init__(message)
        self.issues: list[IngestionIssue] = issues or []

    def raise_if_any(self) -> None:
        if self.issues:
            raise self
