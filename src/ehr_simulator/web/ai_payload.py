"""AI payload families, bounded SHAP selection, exposed field ids (S8 §6, §15).

Pure: shared by the AI renderer, panel state detection and preflight, so
what the clinician sees and what the prohibited field gate checks cannot
diverge. Two payload families exist::

    synthetic  {"prob_deterioration_6h": p, "prob_mrs_0_2_90d": q}
    geneva     {"probability": p,
                "explanation": {"base_value": b, "contributions": {feature: shap}}}

A Geneva row shows one probability and at most ten contributors (five
largest positive, five most negative); ``base_value`` is never shown.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from ehr_simulator.web.clinical_fields import contributor_label

SYNTHETIC_KEYS = frozenset({"prob_deterioration_6h", "prob_mrs_0_2_90d"})
PROBABILITY_KEY = "probability"
EXPLANATION_KEY = "explanation"
BASE_VALUE_KEY = "base_value"
CONTRIBUTIONS_KEY = "contributions"
CONTRIBUTORS_PER_SIGN = 5

PayloadState = Literal["loading", "partial"]


class PayloadFamily(StrEnum):
    GENEVA = "geneva"
    SYNTHETIC = "synthetic"
    UNKNOWN = "unknown"  # neither: ``partial``, rendered as a key list


class AIDetail(StrEnum):
    """How much of a Geneva row is shown (legacy history rows: probability)."""

    FULL = "full"
    PROBABILITY_ONLY = "probability_only"


@dataclass(frozen=True)
class Contributor:
    feature: str  # raw SHAP feature name
    value: float
    label: str | None  # clinical label, ``None`` → render the raw name


@dataclass(frozen=True)
class Contributors:
    increasing: tuple[Contributor, ...]
    decreasing: tuple[Contributor, ...]

    @property
    def all(self) -> tuple[Contributor, ...]:
        return self.increasing + self.decreasing


@dataclass(frozen=True)
class CheckedPayload:
    """A payload after family validation; only fields that passed are set."""

    family: PayloadFamily
    state: PayloadState
    probability: float | None = None
    contributors: Contributors | None = None


def decode(raw: object) -> dict[str, Any] | None:
    """``output_json`` → object, or ``None`` when it is not a JSON object."""
    try:
        payload = json.loads(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _finite(value: object) -> bool:
    return _is_number(value) and math.isfinite(value)  # type: ignore[arg-type]


def _valid_contributions(explanation: object) -> Mapping[str, float] | None:
    """The contributions mapping when the explanation is well formed."""
    if not isinstance(explanation, Mapping):
        return None
    if not _is_number(explanation.get(BASE_VALUE_KEY)):
        return None
    contributions = explanation.get(CONTRIBUTIONS_KEY)
    if not isinstance(contributions, Mapping):
        return None
    if not all(isinstance(k, str) and _finite(v) for k, v in contributions.items()):
        return None
    return contributions


def top_contributors(contributions: Mapping[str, float]) -> Contributors:
    """Five largest positive and five most negative contributions.

    Every feature competes (``timestep_idx`` included); zeros never show; a
    sign with fewer than five entries is not padded. Ties: magnitude
    descending, then feature name ascending.
    """

    def ranked(items: list[tuple[str, float]]) -> tuple[Contributor, ...]:
        items.sort(key=lambda kv: (-abs(kv[1]), kv[0]))
        return tuple(
            Contributor(feature, float(value), contributor_label(feature))
            for feature, value in items[:CONTRIBUTORS_PER_SIGN]
        )

    positive = [(k, v) for k, v in contributions.items() if v > 0]
    negative = [(k, v) for k, v in contributions.items() if v < 0]
    return Contributors(ranked(positive), ranked(negative))


def check_payload(payload: Mapping[str, Any]) -> CheckedPayload:
    """Family detection then validation (§6.3).

    ``probability`` present → Geneva: a finite probability in ``[0, 1]``,
    an optional explanation (numeric ``base_value``, contributions of
    finite numbers). Any failed check → ``partial``, failed field unset.
    Every synthetic key → synthetic. Otherwise ``partial``.
    """
    if PROBABILITY_KEY in payload:
        probability = payload[PROBABILITY_KEY]
        valid_probability = _finite(probability) and 0.0 <= probability <= 1.0
        contributors = None
        valid_explanation = True
        if EXPLANATION_KEY in payload:
            contributions = _valid_contributions(payload[EXPLANATION_KEY])
            valid_explanation = contributions is not None
            if contributions is not None:
                contributors = top_contributors(contributions)
        return CheckedPayload(
            family=PayloadFamily.GENEVA,
            state="loading" if valid_probability and valid_explanation else "partial",
            probability=float(probability) if valid_probability else None,
            contributors=contributors,
        )
    if SYNTHETIC_KEYS.issubset(payload.keys()):
        return CheckedPayload(PayloadFamily.SYNTHETIC, "loading")
    return CheckedPayload(PayloadFamily.UNKNOWN, "partial")


def plan_display(payloads: Sequence[Mapping[str, Any]]) -> list[AIDetail]:
    """Detail per payload (time order) for the cumulative legacy panel.

    The latest Geneva row is shown in full, earlier ones as probability
    history; other families are shown in full, as before S8.
    """
    geneva = [i for i, p in enumerate(payloads) if PROBABILITY_KEY in p]
    latest = geneva[-1] if geneva else None
    return [
        AIDetail.PROBABILITY_ONLY if i in geneva and i != latest else AIDetail.FULL
        for i in range(len(payloads))
    ]


def exposed_ai_field_ids(
    payload: Mapping[str, Any], detail: AIDetail = AIDetail.FULL
) -> frozenset[str]:
    """Clinician facing ids of one shown payload.

    Geneva: ``ai:probability`` and one ``ai:explanation.contributions.<f>``
    per displayed contributor; synthetic/unknown: every top-level key.
    """
    checked = check_payload(payload)
    if checked.family is not PayloadFamily.GENEVA:
        return frozenset(f"ai:{key}" for key in payload)

    ids: set[str] = set()
    if checked.probability is not None:
        ids.add(f"ai:{PROBABILITY_KEY}")
    if detail is AIDetail.FULL and checked.contributors is not None:
        ids.update(
            f"ai:{EXPLANATION_KEY}.{CONTRIBUTIONS_KEY}.{c.feature}"
            for c in checked.contributors.all
        )
    return frozenset(ids)
