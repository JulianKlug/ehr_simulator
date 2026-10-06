"""``clinicians.csv`` rows (S11p)."""

from __future__ import annotations

import sqlite3

from ehr_simulator.db import clinician_profiles
from ehr_simulator.export_phase2.cells import _num, _text
from ehr_simulator.export_phase2.headers import PROFILE_COMPLETE, PROFILE_MISSING


def _clinician_rows(
    conn: sqlite3.Connection, study_id: str, ids: tuple[str, ...]
) -> list[tuple[str, ...]]:
    """S11p characteristics as stored; blank with ``missing`` when none."""
    profiles = clinician_profiles.fetch_by_ids(conn, ids)
    out = []
    for clinician_id in ids:
        profile = profiles.get(clinician_id)
        if profile is None:
            out.append((study_id, clinician_id, PROFILE_MISSING, "", "", "", ""))
            continue
        out.append(
            (
                study_id,
                clinician_id,
                PROFILE_COMPLETE,
                profile.professional_role,
                _num(profile.years_of_practice),
                profile.country_of_practice,
                _text(profile.primary_specialty),
            )
        )
    return out
