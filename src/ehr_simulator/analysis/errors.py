"""The analysis reader's one refusal type."""

from __future__ import annotations


class AnalysisError(ValueError):
    """The bundle cannot be analysed faithfully; nothing is produced.

    Messages name the file, column and data row, never cell content beyond
    identifiers already in the bundle.
    """
