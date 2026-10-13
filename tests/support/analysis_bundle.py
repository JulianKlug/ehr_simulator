"""S12a: copies of the committed analysis bundles, edited like a tamperer.

``edit_csv`` rewrites one file the way the exporter writes it and, by
default, re-digests it in ``manifest.json`` so only the intended check fires.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

FIXTURES = Path(__file__).parents[1] / "fixtures" / "analysis_bundle"
WALKED = "walked"
EMPTY = "empty"
MANIFEST = "manifest.json"

Row = dict[str, str]


class Digest(StrEnum):
    UPDATE = "update"  # manifest follows the edit
    KEEP = "keep"  # manifest keeps the old SHA256 / row count


def copy_fixture(tmp_path: Path, name: str = WALKED) -> Path:
    target = tmp_path / name
    shutil.copytree(FIXTURES / name, target)
    return target


def read_rows(bundle: Path, name: str) -> tuple[list[str], list[Row]]:
    reader = csv.DictReader(io.StringIO((bundle / name).read_text(encoding="utf-8"), newline=""))
    return list(reader.fieldnames or []), list(reader)


def write_rows(
    bundle: Path, name: str, header: list[str], rows: list[Row], digest: Digest = Digest.UPDATE
) -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows([row[c] for c in header] for row in rows)
    data = buffer.getvalue().encode("utf-8")
    (bundle / name).write_bytes(data)
    if digest is Digest.UPDATE:
        edit_manifest(
            bundle,
            lambda m: m["files"].__setitem__(
                name, {"rows": len(rows), "sha256": hashlib.sha256(data).hexdigest()}
            ),
        )


def edit_csv(
    bundle: Path,
    name: str,
    edit: Callable[[list[Row]], list[Row] | None],
    digest: Digest = Digest.UPDATE,
) -> None:
    """``edit`` mutates the rows in place or returns new ones."""
    header, rows = read_rows(bundle, name)
    edited = edit(rows)
    write_rows(bundle, name, header, rows if edited is None else edited, digest)


def edit_manifest(bundle: Path, edit: Callable[[dict], object]) -> None:
    manifest = json.loads((bundle / MANIFEST).read_text())
    edit(manifest)
    (bundle / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def snapshot(bundle: Path) -> dict[str, tuple[bytes, int]]:
    """Every file's bytes and mtime, to prove the reader never writes."""
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(bundle.iterdir())}
