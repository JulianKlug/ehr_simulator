"""S11p clinician characteristics: parse and validate one profile form (pure).

Spec: ``specs/session-11p-clinician-characteristics.md``.

The raw form strings are checked against the study's pinned vocabulary and
turned into a :class:`ClinicianProfile`, or every problem is reported per
field so the page can show them together::

    {"professional_role": "physician", "years_of_practice": "7.5",
     "country_of_practice": "CH", "primary_specialty": "neurology"}
        ──► ClinicianProfile(PHYSICIAN, 7.5, "CH", "neurology")

Characteristics describe the clinician only; nothing here feeds scheduling.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ehr_simulator.config.study import ClinicianProfileConfig

__all__ = [
    "FIELDS",
    "MAX_YEARS_OF_PRACTICE",
    "ClinicianProfile",
    "ProfileValidationError",
    "ProfessionalRole",
    "parse_profile",
]

#: A generous upper bound that still catches typos such as ``750``.
MAX_YEARS_OF_PRACTICE = 80.0

ROLE = "professional_role"
YEARS = "years_of_practice"
COUNTRY = "country_of_practice"
SPECIALTY = "primary_specialty"
FIELDS = (ROLE, YEARS, COUNTRY, SPECIALTY)


#: A non-negative decimal, e.g. ``7`` or ``7.5``.
_YEARS_PATTERN = re.compile(r"[0-9]+(?:\.[0-9]+)?")


class ProfessionalRole(StrEnum):
    PHYSICIAN = "physician"
    NURSE = "nurse"


@dataclass(frozen=True)
class ClinicianProfile:
    professional_role: ProfessionalRole
    years_of_practice: float
    country_of_practice: str
    primary_specialty: str | None  # physicians only


class ProfileValidationError(ValueError):
    """The form is invalid; ``errors`` maps each field to its message."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = dict(errors)


def _years(raw: str, errors: dict[str, str]) -> float | None:
    # Plain decimals only: ``float`` alone also takes "7_5", "1e1", "inf".
    if not _YEARS_PATTERN.fullmatch(raw):
        errors[YEARS] = "Enter a number of years."
        return None

    years = float(raw)

    if not math.isfinite(years) or not 0 <= years <= MAX_YEARS_OF_PRACTICE:
        errors[YEARS] = f"Years of practice must be between 0 and {MAX_YEARS_OF_PRACTICE:g}."
        return None
    return years


def parse_profile(form: Mapping[str, str], config: ClinicianProfileConfig) -> ClinicianProfile:
    """Validate one submitted form; raise :class:`ProfileValidationError`.

    A nurse's submitted specialty is refused rather than dropped, so a
    mis-selected role never silently loses information.
    """
    values = {name: (form.get(name) or "").strip() for name in FIELDS}
    errors: dict[str, str] = {}

    role: ProfessionalRole | None = None
    try:
        role = ProfessionalRole(values[ROLE])
    except ValueError:
        errors[ROLE] = "Choose physician or nurse."

    years = _years(values[YEARS], errors)

    if values[COUNTRY] not in config.countries:
        errors[COUNTRY] = "Choose a country from the list."

    specialty = values[SPECIALTY] or None
    if role is ProfessionalRole.PHYSICIAN and specialty not in config.specialties:
        errors[SPECIALTY] = "Physicians choose a specialty from the list."
    if role is ProfessionalRole.NURSE and specialty is not None:
        errors[SPECIALTY] = "Specialty applies to physicians only; leave it empty."

    if errors:
        raise ProfileValidationError(errors)
    assert role is not None and years is not None
    return ClinicianProfile(role, years, values[COUNTRY], specialty)
