"""S11p: the clinician profile page service (between routes and the DAO).

::

    GET  /profile ─► profile_context   (stored values, vocabulary, locked?)
    POST /profile ─► save_profile      parse (pure) ─► BEGIN IMMEDIATE
                                       ─► clinician_profiles.save
                                       ─► clinician.profile_saved {action}
                                       ─► COMMIT

Validation uses the **active** configuration's vocabulary; the stored
values are never re-validated later.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ehr_simulator.clinician_profile import (
    ClinicianProfile,
    ProfessionalRole,
    parse_profile,
)
from ehr_simulator.config.study import ClinicianProfileConfig
from ehr_simulator.db import clinician_profiles, events
from ehr_simulator.db.clinician_profiles import StoredProfile
from ehr_simulator.web.case_contact import now

__all__ = ["ProfileContext", "profile_config", "profile_context", "save_profile"]

_CREATED = "created"
_UPDATED = "updated"


@dataclass(frozen=True)
class ProfileContext:
    config: ClinicianProfileConfig
    stored: StoredProfile | None
    locked: bool
    roles: tuple[str, ...] = tuple(r.value for r in ProfessionalRole)


def profile_config(app_state: Any) -> ClinicianProfileConfig | None:
    """The active configuration's profile block, or ``None`` (feature off)."""
    study = getattr(app_state, "study", None)
    return None if study is None else study.clinician_profile


def profile_context(
    conn: sqlite3.Connection, config: ClinicianProfileConfig, clinician_id: str
) -> ProfileContext:
    return ProfileContext(
        config=config,
        stored=clinician_profiles.fetch(conn, clinician_id),
        locked=clinician_profiles.is_locked(conn, clinician_id),
    )


def save_profile(
    conn: sqlite3.Connection,
    app_state: Any,
    *,
    clinician_id: str,
    config: ClinicianProfileConfig,
    form: Mapping[str, str],
) -> ClinicianProfile:
    """Validate and store one form in one transaction.

    Raises:
        ProfileValidationError: invalid input, nothing written.
        ProfileLockedError: a measured case exists, nothing written.
    """
    profile = parse_profile(form, config)

    conn.execute("BEGIN IMMEDIATE")
    try:
        created = clinician_profiles.save(
            conn,
            clinician_id=clinician_id,
            professional_role=str(profile.professional_role),
            years_of_practice=profile.years_of_practice,
            country_of_practice=profile.country_of_practice,
            primary_specialty=profile.primary_specialty,
            now=now(app_state),
        )
        events.append(
            conn,
            session_id=None,
            clinician_id=clinician_id,
            patient_id=None,
            timepoint=None,
            kind="clinician.profile_saved",
            payload={"action": _CREATED if created else _UPDATED},
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    app_state.write_counter = getattr(app_state, "write_counter", 0) + 1
    return profile
