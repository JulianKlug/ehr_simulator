"""S11n: write a Phase 2 bundle as one atomically published directory.

::

    tables ─► CSV bytes (formula guard, RFC 4180, "\\n") ─► SHA256 + row count
           ─► <parent>/.<name>.staging-<id>/   every CSV, manifest.json last (fsynced)
           ─► keyfile staged beside its target   (0600, before any swap)
           ─► rename to <parent>/<name>/        (--force: old one aside first)
           ─► keyfile linked / replaced         (--force: old one linked aside first)

Keyfile preconditions are checked before anything is written. A failure
(interrupts included) removes the staging files and puts any previous
bundle and keyfile back. The keyfile never lies inside the bundle.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from ehr_simulator.export import ExportError, guard_cell, write_keyfile
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
        BundleWriteError: destination or keyfile exists without
            ``Overwrite.REPLACE``, keyfile inside the bundle, or the keyfile
            cannot be written (nothing is published then).
    """
    out_dir = Path(out_dir)
    keyfile = Path(keyfile) if keyfile is not None else None
    _check_preconditions(bundle, out_dir, overwrite, keyfile)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = out_dir.parent / f".{out_dir.name}.staging-{token}"
    aside = out_dir.parent / f".{out_dir.name}.previous-{token}"
    staged_key = keyfile.parent / f".{keyfile.name}.staging-{token}" if keyfile else None
    key_aside = keyfile.parent / f".{keyfile.name}.previous-{token}" if keyfile else None
    published = False
    key_published = False
    staging.mkdir()
    try:
        _write_staging(bundle, staging)
        if keyfile is not None and staged_key is not None:
            _stage_keyfile(bundle, keyfile, staged_key)

        if out_dir.exists():
            out_dir.rename(aside)
        staging.rename(out_dir)
        published = True
        _fsync_dir(out_dir.parent)

        if keyfile is not None and staged_key is not None and key_aside is not None:
            _publish_keyfile(staged_key, keyfile, key_aside, overwrite)
            key_published = True
            staged_key.unlink(missing_ok=True)
            _fsync_dir(keyfile.parent)
    except BaseException:
        # Interrupts too: never strand the previous bundle under its aside name.
        shutil.rmtree(staging, ignore_errors=True)
        if staged_key is not None:
            staged_key.unlink(missing_ok=True)
        if keyfile is not None and key_aside is not None:
            _restore_keyfile(keyfile, key_aside, key_published)
        if published:
            shutil.rmtree(out_dir, ignore_errors=True)
        if aside.exists() and not out_dir.exists():
            aside.rename(out_dir)  # put the previous bundle back
        raise

    shutil.rmtree(aside, ignore_errors=True)
    if key_aside is not None:
        key_aside.unlink(missing_ok=True)
    return out_dir


def _check_preconditions(
    bundle: Phase2Bundle, out_dir: Path, overwrite: Overwrite, keyfile: Path | None
) -> None:
    """Every refusal happens here, before any file is written."""
    if keyfile is not None and _inside(keyfile, out_dir):
        raise BundleWriteError(
            "--keyfile must lie outside --out-dir: the name mapping never travels with the bundle"
        )
    if out_dir.exists() and overwrite is Overwrite.REFUSE:
        raise BundleWriteError(f"{out_dir} already exists; pass --force to replace it")
    if keyfile is None:
        return

    if bundle.keyfile_rows is None:
        raise BundleWriteError("a keyfile was requested but the bundle carries no keyfile rows")
    if keyfile.is_dir():
        raise BundleWriteError(f"keyfile {keyfile} is a directory")
    if keyfile.exists() and overwrite is Overwrite.REFUSE:
        raise BundleWriteError(f"keyfile {keyfile} already exists; pass --force to replace it")


def _write_staging(bundle: Phase2Bundle, staging: Path) -> None:
    files: dict[str, tuple[int, str]] = {}
    for table in bundle.tables:
        content = render_table(table)
        _write_synced(staging / table.name, content)
        files[table.name] = (len(table.rows), hashlib.sha256(content).hexdigest())
    _write_synced(staging / MANIFEST_NAME, _manifest(bundle, files))
    _fsync_dir(staging)


def _stage_keyfile(bundle: Phase2Bundle, keyfile: Path, staged: Path) -> None:
    """Complete mode-0600 keyfile under a sibling name; the target is untouched."""
    try:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        write_keyfile(bundle.keyfile_rows or (), staged)
    except ExportError as exc:
        raise BundleWriteError(f"the keyfile was not written: {exc}") from exc


def _publish_keyfile(staged: Path, keyfile: Path, aside: Path, overwrite: Overwrite) -> None:
    """Put the staged keyfile at ``keyfile``; a replaced one stays linked at ``aside``.

    The old mapping is the only way back to the clinicians of the previous
    bundle, so it survives until the whole publish succeeds.
    """
    if overwrite is Overwrite.REPLACE:
        if keyfile.exists():
            os.link(keyfile, aside)
        os.replace(staged, keyfile)
        return

    try:
        os.link(staged, keyfile)  # no clobber: a keyfile created since the check wins
    except FileExistsError as exc:
        raise BundleWriteError(f"keyfile {keyfile} appeared during the export") from exc


def _restore_keyfile(keyfile: Path, aside: Path, published: bool) -> None:
    """Undo a keyfile publish: the old mapping back, or no new one left behind."""
    if aside.exists():
        os.replace(aside, keyfile)
        return

    if published:
        keyfile.unlink(missing_ok=True)


def _write_synced(path: Path, content: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    """Make a rename or new entry in ``path`` durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
