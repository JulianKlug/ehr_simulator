"""AI artifact provenance (S11g): the identity of the AI output an adapter loaded.

Describes the artifact actually loaded, never the configured expectation;
the study config's ``ai_intervention`` is compared against it at boot and
in preflight. An adapter without AI output exposes ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AIArtifactProvenance:
    prediction_sha256: str
    explanation_sha256: str | None
    model_system_version: str


def ai_provenance_of(dataset: Any) -> AIArtifactProvenance | None:
    """The dataset's loaded AI provenance; ``None`` for datasets without one."""
    provenance = getattr(dataset, "ai_provenance", None)
    return provenance if isinstance(provenance, AIArtifactProvenance) else None
