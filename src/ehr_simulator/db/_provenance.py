"""Write-side provenance guard shared by the case-table DAOs (S11b)."""

from __future__ import annotations

import sqlite3

from ehr_simulator.db.config_history import has_any
from ehr_simulator.db.exceptions import ConfigurationProvenanceError


def _require_version_provenance(conn: sqlite3.Connection, config_version: str | None) -> None:
    """Write-side provenance guard (S11b): once the study has any activated
    configuration, every new case row must carry its ``config_version``.

    Raises:
        ConfigurationProvenanceError: ``config_version`` is NULL but
            ``configuration_history`` is non-empty.
    """
    if config_version is None and has_any(conn):
        raise ConfigurationProvenanceError(
            "refusing to write a case row without a config_version: the study "
            "has activated configurations in its history; every new case row "
            "must pin the version under which it was started"
        )
