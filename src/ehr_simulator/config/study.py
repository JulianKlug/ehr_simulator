"""Study config Pydantic model — the canonical study_config.yaml shape.

Locks ``schema_version: "1"`` (D6 from S1, deferred). Per /plan-eng-review
issue 2.2, relative ``csv_path`` and ``params_dir`` resolve against the YAML
file's parent directory — :func:`config.loader.load_study_config` passes that
directory through ``model_validate(..., context={"yaml_dir": ...})``.

``timepoints_minutes`` is the load-bearing derived property: every downstream
caller (URL routing, ``walk_preflight``, S6 storage) consumes it instead of
the raw ``timepoints`` list. It decouples wire-format unit (minutes vs hours)
from the computational unit (always minutes).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator, model_validator


class StudyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"]
    dataset: Literal["synthetic", "geneva", "mimic"]
    csv_path: Path | None = None
    params_dir: Path | None = None
    patient_ids: list[str]
    time_unit: Literal["minutes", "hours"]
    timepoints: list[float]

    @model_validator(mode="before")
    @classmethod
    def _resolve_relative_paths(cls, data: Any, info: ValidationInfo) -> Any:
        if not isinstance(data, dict):
            return data
        context = info.context or {}
        yaml_dir = context.get("yaml_dir") if isinstance(context, dict) else None
        if yaml_dir is None:
            return data
        out = dict(data)
        for key in ("csv_path", "params_dir"):
            raw = out.get(key)
            if raw is None:
                continue
            p = Path(raw)
            if not p.is_absolute():
                out[key] = str((Path(yaml_dir) / p).resolve())
        return out

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
