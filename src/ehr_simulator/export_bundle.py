"""S11n: write a Phase 2 bundle as one atomically published directory.

::

    tables ─► CSV bytes (formula guard, RFC 4180, "\\n") ─► SHA256 + row count
           ─► <parent>/.<name>.staging-<id>/   every CSV, manifest.json last
           ─► rename to <parent>/<name>/        (--force: old one aside first)

A failure removes the staging directory and leaves any previous bundle
untouched. The keyfile is written only after the bundle is published and
never inside it.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import shutil
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ehr_simulator.export import guard_cell, write_keyfile
from ehr_simulator.export_phase2 import EXPORT_SCHEMA_VERSION, Phase2Bundle, Table

__all__ = ["MANIFEST_NAME", "BundleWriteError", "Overwrite", "render_table", "write_bundle"]

MANIFEST_NAME = "manifest.json"


class BundleWriteError(ValueError):
    """The bundle or its keyfile could not be written as requested."""


class Overwrite(StrEnum):
    REFUSE = "refuse"
    REPLACE = "replace"


def render_table(table: Table) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([guard_cell(cell) for cell in table.header])
    for row in table.rows:
        writer.writerow([guard_cell(cell) for cell in row])
    return buf.getvalue().encode("utf-8")


def _manifest(bundle: Phase2Bundle, files: dict[str, tuple[int, str]]) -> bytes:
    body = {
        "export_schema_version": EXPORT_SCHEMA_VERSION,
        "study_id": bundle.study_id,
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_schema_version": bundle.source_schema_version,
        "included_config_versions": list(bundle.config_versions),
        "practice_included": bundle.practice_included,
        "files": {name: {"rows": rows, "sha256": digest} for name, (rows, digest) in files.items()},
    }
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _inside(path: Path, directory: Path) -> bool:
    resolved, root = path.resolve(), directory.resolve()
    return resolved == root or root in resolved.parents


def write_bundle(
    bundle: Phase2Bundle,
    out_dir: Path,
    *,
    overwrite: Overwrite = Overwrite.REFUSE,
    keyfile: Path | None = None,
) -> Path:
    """Publish ``bundle`` at ``out_dir`` (and ``keyfile`` beside it); return ``out_dir``.

    Raises:
        BundleWriteError: destination exists without ``Overwrite.REPLACE``,
            keyfile inside the bundle, or a keyfile failure after publication.
    """
    out_dir = Path(out_dir)
    if keyfile is not None and _inside(Path(keyfile), out_dir):
        raise BundleWriteError(
            "--keyfile must lie outside --out-dir: the name mapping never travels with the bundle"
        )
    if out_dir.exists() and overwrite is Overwrite.REFUSE:
        raise BundleWriteError(f"{out_dir} already exists; pass --force to replace it")
    if keyfile is not None and bundle.keyfile_rows is None:
        raise BundleWriteError("a keyfile was requested but the bundle carries no keyfile rows")

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = out_dir.parent / f".{out_dir.name}.staging-{token}"
    aside = out_dir.parent / f".{out_dir.name}.previous-{token}"
    staging.mkdir()
    try:
        files: dict[str, tuple[int, str]] = {}
        for table in bundle.tables:
            content = render_table(table)
            (staging / table.name).write_bytes(content)
            files[table.name] = (len(table.rows), hashlib.sha256(content).hexdigest())
        (staging / MANIFEST_NAME).write_bytes(_manifest(bundle, files))

        if out_dir.exists():
            out_dir.rename(aside)
        staging.rename(out_dir)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        if aside.exists() and not out_dir.exists():
            aside.rename(out_dir)  # put the previous bundle back
        raise

    shutil.rmtree(aside, ignore_errors=True)
    if keyfile is not None:
        _write_keyfile(bundle, Path(keyfile), overwrite, out_dir)
    return out_dir


def _write_keyfile(
    bundle: Phase2Bundle, keyfile: Path, overwrite: Overwrite, out_dir: Path
) -> None:
    try:
        if overwrite is Overwrite.REPLACE:
            with contextlib.suppress(FileNotFoundError):
                keyfile.unlink()
        write_keyfile(bundle.keyfile_rows or (), keyfile)
    except Exception as exc:
        raise BundleWriteError(
            f"bundle published at {out_dir}, but the keyfile was not written: {exc}"
        ) from exc
