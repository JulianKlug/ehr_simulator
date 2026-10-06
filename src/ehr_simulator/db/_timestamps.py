"""Stored UTC timestamp text form shared by the DAOs.

Written as ``YYYY-MM-DD HH:MM:SS[.ffffff]``: whole seconds keep the
``CURRENT_TIMESTAMP`` shape, a fraction is kept (a truncated ``last_seen_at``
would fire a lifecycle timeout up to one second early). Both shapes parse.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from typing import overload

_DATE_TIME_SEPARATOR = " "

#: Declared column type every DAO timestamp uses (``PARSE_DECLTYPES`` key).
_TIMESTAMP_DECLTYPE = "TIMESTAMP"


def to_db_timestamp(moment: datetime) -> str:
    """Aware or naive-UTC datetime → the stored UTC text form."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return moment.isoformat(sep=_DATE_TIME_SEPARATOR)


@overload
def _as_utc(value: datetime | str) -> datetime: ...
@overload
def _as_utc(value: None) -> None: ...
@overload
def _as_utc(value: datetime | str | None) -> datetime | None: ...
def _as_utc(value: datetime | str | None) -> datetime | None:
    """Stored value → aware UTC datetime.

    PARSE_DECLTYPES yields naive datetimes; a raw string can still come back
    from a connection opened without it. Both are UTC.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _adapt_datetime(moment: datetime) -> str:
    return moment.isoformat(sep=_DATE_TIME_SEPARATOR)


def _adapt_date(day: date) -> str:
    return day.isoformat()


def _convert_timestamp(raw: bytes) -> datetime:
    """Stored text → datetime.

    ``2026-01-02 03:04:05[.f]`` → naive (UTC, as Python's former default
    converter returned); ISO with ``T``/``Z``/offset (S11j ``client_ts``) →
    aware.
    """
    return datetime.fromisoformat(raw.decode())


def register_sqlite_codecs() -> None:
    """Explicit replacements for sqlite3's default adapters/converters,
    deprecated since Python 3.12. Process-wide; idempotent."""
    sqlite3.register_adapter(datetime, _adapt_datetime)
    sqlite3.register_adapter(date, _adapt_date)
    sqlite3.register_converter(_TIMESTAMP_DECLTYPE, _convert_timestamp)
