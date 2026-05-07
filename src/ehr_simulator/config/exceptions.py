"""Config-loading exceptions.

:class:`ConfigError` wraps a Pydantic :class:`pydantic.ValidationError` as a
single human-readable string that names the offending YAML file plus the
field path. The CLI catches it, prints the message to stderr, and exits 1
— callers do not introspect Pydantic's error tree directly.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError


class ConfigError(Exception):
    """Human-readable error for config validation failures."""

    @classmethod
    def from_validation_error(cls, exc: ValidationError, *, path: Path) -> ConfigError:
        lines = [f"{path.name}: {len(exc.errors())} validation error(s):"]
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"]) or "<root>"
            lines.append(f"  - {loc}: {err['msg']}")
        return cls("\n".join(lines))
