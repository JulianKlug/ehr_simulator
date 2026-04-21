"""Public surface for the ingestion package."""

from __future__ import annotations

from ehr_simulator.ingestion.canonical import (
    ADMISSION_SCHEMA,
    AI_OUTPUT_SCHEMA,
    IMAGING_SCHEMA,
    SCALAR_TS_SCHEMA,
    SCHEMAS,
    CanonicalShape,
    validate,
)
from ehr_simulator.ingestion.exceptions import AdapterError, IngestionIssue
from ehr_simulator.ingestion.synthetic import SyntheticDataset, load_synthetic

__all__ = [
    "ADMISSION_SCHEMA",
    "AI_OUTPUT_SCHEMA",
    "AdapterError",
    "CanonicalShape",
    "IMAGING_SCHEMA",
    "IngestionIssue",
    "SCALAR_TS_SCHEMA",
    "SCHEMAS",
    "SyntheticDataset",
    "load_synthetic",
    "validate",
]
