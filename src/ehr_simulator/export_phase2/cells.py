"""S11n cell formatting: every exported value is a string."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime

from ehr_simulator.export_phase2.model import FALSE, LIST_SEPARATOR, TRUE


def _bool(value: bool | None) -> str:
    if value is None:
        return ""
    return TRUE if value else FALSE


def _num(value: float | int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        raise TypeError("booleans are formatted with _bool")
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def _minutes(value: float) -> str:
    return repr(float(value))


def _ts(value: object) -> str:
    """Stored timestamp → ``YYYY-MM-DDTHH:MM:SS[.ffffff]Z`` (UTC)."""
    if value is None:
        return ""
    moment = value
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(moment, datetime):
        return str(value)
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.isoformat() + "Z"


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _joined(values: Iterable[str]) -> str:
    return LIST_SEPARATOR.join(sorted(str(v) for v in values))


def _canonical_json(raw: str) -> str:
    return json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"))
