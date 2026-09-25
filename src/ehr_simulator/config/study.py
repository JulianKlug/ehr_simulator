"""Study config Pydantic model — the canonical study_config.yaml shape.

Locks ``schema_version: "1"`` (D6 from S1, deferred). Per /plan-eng-review
issue 2.2, relative ``csv_path`` and ``params_dir`` resolve against the YAML
file's parent directory — :func:`config.loader.load_study_config` passes that
directory through ``model_validate(..., context={"yaml_dir": ...})``.

``timepoints_minutes`` is the load-bearing derived property: every downstream
caller (URL routing, ``walk_preflight``, S6 storage) consumes it instead of
the raw ``timepoints`` list. It decouples wire-format unit (minutes vs hours)
from the computational unit (always minutes).

S11: ``study_id`` is required — a stable, filesystem-safe identifier
(:data:`STUDY_ID_PATTERN`) that binds this study to its database (see
``db/study_identity.py``). ``schema_version`` is locked to ``"2"``.

S11c: optional ``randomisation`` block (:class:`RandomisationConfig`) feeds
the Phase 2 scheduler. When absent it is omitted from serialization, so
pre-S11c snapshots and their ``config_hash`` stay byte-identical.

S11e: optional ``case_lifecycle`` block (:class:`CaseLifecycleConfig`) sets
reconnection grace, voluntary pause and per-clinician stopping limits.
Omitted from serialization when absent, like ``randomisation``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    SerializerFunctionWrapHandler,
    StrictBool,
    StrictInt,
    ValidationInfo,
    field_validator,
    model_serializer,
    model_validator,
)

from ehr_simulator.config.exceptions import ConfigError

__all__ = [
    "MASTER_SEED_MAX",
    "MIN_RECONNECTION_GRACE_SECONDS",
    "STUDY_ID_PATTERN",
    "BlockEntry",
    "CaseLifecycleConfig",
    "RandomisationConfig",
    "StudyConfig",
]

#: S11: a stable, filesystem-safe study identifier. Lowercase alphanumerics,
#: ``-`` and ``_``; must start with an alphanumeric; 1..64 characters. The
#: charset deliberately excludes ``/``, ``.`` and whitespace so a study id
#: can never smuggle a path segment into ``data/study_<id>.db``. The same
#: shape is re-checked at bind time in ``db/study_identity.py``; a test keeps
#: the two patterns in lockstep.
STUDY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: S11c: largest master seed — the SQLite signed 64-bit INTEGER ceiling.
MASTER_SEED_MAX = 2**63 - 1

#: S11e: smallest reconnection grace. Chrome throttles timers in background
#: tabs to one wake-up per minute; 180 s leaves a 3x margin over that for
#: the case heartbeat (a lockstep test pins it to ``HEARTBEAT_INTERVAL_SECONDS``).
MIN_RECONNECTION_GRACE_SECONDS = 180

#: ``start`` = the clinician's starting arm, ``other`` = the opposite arm.
BlockEntry = Literal["start", "other"]


class RandomisationConfig(BaseModel):
    """S11c block design for the Phase 2 scheduler.

    Each ``block_sequence`` entry is one block of ``block_length`` cases;
    the sequence repeats until every patient has a position. Example:
    ``block_length: 2``, ``[start, other]``, starting arm ``ai`` →
    ``ai, ai, no_ai, no_ai, ai, ai, ...``.
    """

    model_config = ConfigDict(extra="forbid")

    master_seed: StrictInt
    block_length: StrictInt
    block_sequence: list[BlockEntry]

    @field_validator("master_seed")
    @classmethod
    def _seed_in_range(cls, v: int) -> int:
        if not 0 <= v <= MASTER_SEED_MAX:
            raise ValueError(f"master_seed must be between 0 and 2^63 - 1; got {v}")
        return v

    @field_validator("block_length")
    @classmethod
    def _block_length_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"block_length must be >= 1; got {v}")
        return v

    @field_validator("block_sequence")
    @classmethod
    def _sequence_balanced(cls, v: list[str]) -> list[str]:
        # One cycle must hold as many ``start`` as ``other`` blocks, so a
        # complete cycle is arm-balanced within the clinician.
        starts = v.count("start")
        others = v.count("other")
        if starts == 0 or others == 0:
            raise ValueError("block_sequence must contain both 'start' and 'other'")
        if starts != others:
            raise ValueError(
                f"block_sequence must hold equal 'start' and 'other' counts; "
                f"got {starts} start, {others} other"
            )
        return v


class CaseLifecycleConfig(BaseModel):
    """S11e reconnection, pause and clinician stopping policy.

    ``voluntary_pause_grace_seconds`` is serialized as supplied; a null grace
    with pause enabled falls back to the reconnection grace at use
    (:attr:`effective_pause_grace_seconds`), never in the stored snapshot.
    """

    model_config = ConfigDict(extra="forbid")

    reconnection_grace_seconds: StrictInt
    voluntary_pause_enabled: StrictBool = False
    voluntary_pause_grace_seconds: StrictInt | None = None
    target_completed_cases_per_clinician: StrictInt
    max_activated_cases_per_clinician: StrictInt
    study_target_completed_cases: StrictInt | None = None

    @field_validator("reconnection_grace_seconds")
    @classmethod
    def _grace_above_floor(cls, v: int) -> int:
        if v < MIN_RECONNECTION_GRACE_SECONDS:
            raise ValueError(
                f"reconnection_grace_seconds must be >= {MIN_RECONNECTION_GRACE_SECONDS}; got {v}"
            )
        return v

    @field_validator("voluntary_pause_grace_seconds")
    @classmethod
    def _pause_grace_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"voluntary_pause_grace_seconds must be >= 1; got {v}")
        return v

    @field_validator("target_completed_cases_per_clinician", "study_target_completed_cases")
    @classmethod
    def _target_positive(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError(f"completed case targets must be >= 1; got {v}")
        return v

    @model_validator(mode="after")
    def _policy_consistent(self) -> CaseLifecycleConfig:
        if not self.voluntary_pause_enabled and self.voluntary_pause_grace_seconds is not None:
            raise ValueError(
                "voluntary_pause_grace_seconds is set while voluntary_pause_enabled is false"
            )
        if self.max_activated_cases_per_clinician < self.target_completed_cases_per_clinician:
            raise ValueError(
                "max_activated_cases_per_clinician must be >= "
                "target_completed_cases_per_clinician; got "
                f"{self.max_activated_cases_per_clinician} < "
                f"{self.target_completed_cases_per_clinician}"
            )
        return self

    @property
    def effective_pause_grace_seconds(self) -> int | None:
        """Pause grace in force; ``None`` when voluntary pause is disabled."""
        if not self.voluntary_pause_enabled:
            return None
        if self.voluntary_pause_grace_seconds is None:
            return self.reconnection_grace_seconds
        return self.voluntary_pause_grace_seconds


class StudyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["2"]
    study_id: str
    dataset: Literal["synthetic", "geneva", "mimic"]
    csv_path: Path | None = None
    params_dir: Path | None = None
    db_path: Path | None = None
    patient_ids: list[str]
    time_unit: Literal["minutes", "hours"]
    timepoints: list[float]
    randomisation: RandomisationConfig | None = None
    case_lifecycle: CaseLifecycleConfig | None = None

    @model_serializer(mode="wrap")
    def _omit_absent_randomisation(self, handler: SerializerFunctionWrapHandler) -> Any:
        # Absent block → absent key: pre-S11c/S11e snapshots re-render
        # byte-for-byte and their config_hash does not move.
        data = handler(self)
        if not isinstance(data, dict):
            return data

        if self.randomisation is None:
            data.pop("randomisation", None)
        if self.case_lifecycle is None:
            data.pop("case_lifecycle", None)
        return data

    @model_validator(mode="before")
    @classmethod
    def _resolve_relative_paths(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        context = info.context or {}
        yaml_dir = context.get("yaml_dir") if isinstance(context, dict) else None
        out = dict(data)
        # Traversal guard runs on the raw (pre-resolution) path so a
        # ``..`` segment is rejected regardless of yaml_dir presence
        # (review-fix R13). Applied to db_path only — csv_path/params_dir
        # are operator-managed deployment paths and stay unguarded for
        # back-compat.
        db_raw = out.get("db_path")
        if db_raw is not None:
            raw_path = Path(db_raw)
            if ".." in raw_path.parts:
                raise ValueError(f"db_path must not contain '..' segments; got {raw_path}")
        if yaml_dir is None:
            return out
        for key in ("csv_path", "params_dir", "db_path"):
            raw = out.get(key)
            if raw is None:
                continue
            p = Path(raw)
            if not p.is_absolute():
                out[key] = str((Path(yaml_dir) / p).resolve())
        return out

    @model_validator(mode="after")
    def _db_path_safe(self) -> StudyConfig:
        if self.db_path is None:
            return self
        resolved = self.db_path.resolve()
        cwd = Path.cwd().resolve()
        try:
            resolved.relative_to(cwd)
        except ValueError as exc:
            raise ConfigError(
                f"db_path must be inside the project working directory; got {resolved}"
            ) from exc
        return self

    @field_validator("study_id")
    @classmethod
    def _study_id_shape(cls, v: str) -> str:
        if not isinstance(v, str) or not STUDY_ID_PATTERN.fullmatch(v):
            raise ValueError(
                f"must match {STUDY_ID_PATTERN.pattern!r} "
                "(lowercase a-z, 0-9, '-' or '_', first char alphanumeric, "
                f"at most 64 characters); got {v!r}"
            )
        return v

    @field_validator("patient_ids")
    @classmethod
    def _patient_ids_non_empty_and_unique(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("patient_ids must be non-empty")
        if len(set(v)) != len(v):
            seen: set[str] = set()
            dups: list[str] = []
            for pid in v:
                if pid in seen:
                    dups.append(pid)
                seen.add(pid)
            raise ValueError(f"patient_ids must be unique; duplicates: {sorted(set(dups))}")
        return v

    @field_validator("timepoints")
    @classmethod
    def _timepoints_sorted_unique_nonnegative(cls, v: list[float]) -> list[float]:
        if not v:
            raise ValueError("timepoints must be non-empty")
        if any(t < 0 for t in v):
            raise ValueError("timepoints must all be >= 0")
        if len(set(v)) != len(v):
            raise ValueError("timepoints must be unique")
        if list(v) != sorted(v):
            raise ValueError("timepoints must be sorted ascending")
        return v

    @model_validator(mode="after")
    def _path_overrides_consistent(self) -> StudyConfig:
        csv_set = self.csv_path is not None
        params_set = self.params_dir is not None
        if csv_set != params_set:
            raise ValueError("csv_path and params_dir must both be set or both be unset")
        if self.dataset == "synthetic" and (csv_set or params_set):
            raise ValueError("csv_path and params_dir are forbidden when dataset='synthetic'")
        return self

    @property
    def timepoints_minutes(self) -> list[float]:
        if self.time_unit == "hours":
            return [t * 60.0 for t in self.timepoints]
        return [float(t) for t in self.timepoints]
