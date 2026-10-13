"""Strict per column cell parsing: one CSV table → one typed DataFrame.

Every cell is a string in the bundle. ``OPT_`` kinds map ``""`` to a pandas
missing value (``<NA>``), never to ``False`` or ``0``::

    "true" / "false" / ""   → boolean  True / False / <NA>
    "3" / ""                → Int64    3 / <NA>
    "4.85" / ""             → Float64  4.85 / <NA>   (finite, ≥ 0)
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence
from enum import StrEnum

import pandas as pd

from ehr_simulator.analysis.errors import AnalysisError
from ehr_simulator.analysis.schema import FALSE, LIST_SEPARATOR, TRUE, Column, ColumnKind

#: Server timestamps: ``YYYY-MM-DDTHH:MM:SS[.f]Z`` (S11n ``_ts``) or the S10
#: ``YYYY-MM-DD HH:MM:SS`` form of ``timepoint_started_at``/``ended_at``.
_TS = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z?")
_INT = re.compile(r"[0-9]+")
_BOOLS = {TRUE: True, FALSE: False}

K = ColumnKind


class _RefusedError(Exception):
    """One cell does not fit its kind (located by the caller)."""


def parse_table(
    name: str, columns: Sequence[Column], rows: Sequence[Sequence[str]]
) -> pd.DataFrame:
    """Typed frame of *rows* (header excluded); refuses the first bad cell."""
    data: dict[str, pd.Series] = {}
    for position, (column, kind, vocab) in enumerate(columns):
        cells = [row[position] for row in rows]
        data[column] = _series(name, column, kind, vocab, cells)
    return pd.DataFrame(data, index=pd.RangeIndex(len(rows)))


def _series(
    name: str, column: str, kind: ColumnKind, vocab: type[StrEnum] | None, cells: list[str]
) -> pd.Series:
    parse, dtype = _PARSERS[kind]
    values = []
    for row, cell in enumerate(cells, start=1):
        try:
            values.append(parse(cell, vocab))
        except _RefusedError:
            raise AnalysisError(
                f"{name}: data row {row}, column {column!r} is not a valid {kind} cell"
            ) from None
    return pd.Series(values, dtype=dtype)


def _optional(parse: Callable[[str, type[StrEnum] | None], object]):
    def wrapped(cell: str, vocab: type[StrEnum] | None) -> object:
        return pd.NA if cell == "" else parse(cell, vocab)

    return wrapped


def _text(cell: str, _vocab: type[StrEnum] | None) -> str:
    return cell


def _id(cell: str, _vocab: type[StrEnum] | None) -> str:
    if not cell:
        raise _RefusedError
    return cell


def _int(cell: str, _vocab: type[StrEnum] | None) -> int:
    if not _INT.fullmatch(cell):
        raise _RefusedError
    return int(cell)


def _float(cell: str, _vocab: type[StrEnum] | None) -> float:
    try:
        value = float(cell)
    except ValueError:
        raise _RefusedError from None
    if not math.isfinite(value) or value < 0:
        raise _RefusedError
    return value


def _bool(cell: str, _vocab: type[StrEnum] | None) -> bool:
    if cell not in _BOOLS:
        raise _RefusedError
    return _BOOLS[cell]


def _ts(cell: str, _vocab: type[StrEnum] | None) -> str:
    if not _TS.fullmatch(cell):
        raise _RefusedError
    return cell


def _enum(cell: str, vocab: type[StrEnum] | None) -> str:
    assert vocab is not None
    if cell not in {v.value for v in vocab}:
        raise _RefusedError
    return cell


def _enum_list(cell: str, vocab: type[StrEnum] | None) -> tuple[str, ...]:
    """Sorted, unique, ``|`` joined vocabulary members; ``""`` = none."""
    if cell == "":
        return ()
    items = tuple(cell.split(LIST_SEPARATOR))
    if list(items) != sorted(set(items)):
        raise _RefusedError
    for item in items:
        _enum(item, vocab)
    return items


def _json(cell: str, _vocab: type[StrEnum] | None) -> str:
    try:
        json.loads(cell)
    except json.JSONDecodeError:
        raise _RefusedError from None
    return cell


_PARSERS: dict[ColumnKind, tuple[Callable[[str, type[StrEnum] | None], object], str]] = {
    K.TEXT: (_text, "string"),
    K.ID: (_id, "string"),
    K.INT: (_int, "Int64"),
    K.OPT_INT: (_optional(_int), "Int64"),
    K.FLOAT: (_float, "Float64"),
    K.OPT_FLOAT: (_optional(_float), "Float64"),
    K.BOOL: (_bool, "boolean"),
    K.OPT_BOOL: (_optional(_bool), "boolean"),
    K.TS: (_ts, "string"),
    K.OPT_TS: (_optional(_ts), "string"),
    K.ENUM: (_enum, "string"),
    K.OPT_ENUM: (_optional(_enum), "string"),
    K.ENUM_LIST: (_enum_list, "object"),
    K.JSON: (_json, "string"),
}
