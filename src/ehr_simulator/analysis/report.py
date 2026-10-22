"""S12b: publish the descriptive report of one bundle as one directory.

Spec: ``specs/session-12-phase25-release.md`` §4.7, §5. Validate first, then
stage and publish atomically (same pattern as ``export_bundle.write_bundle``,
which this package may not import)::

    refusals (output exists / overlaps the bundle) ─► nothing read or written
    load_phase2_bundle + build_summaries            ─► AnalysisError: nothing written
    <parent>/.<name>.staging-<id>/
        figures/<stem>_<version>.svg|.pdf   every FIGURES entry × config version
        tables/<table>.csv           the six S12a summary tables
        denominators.csv
        analysis_manifest.json       last: inputs, versions, settings, inventory
    rename to <parent>/<name>/        (REPLACE: the old one aside first)

REPLACE only ever removes an empty directory or a previous report (its
manifest names a known analysis schema), never a symbolic link; both modes re-check the
output right before the rename, so a report published meanwhile by another
run is never replaced. Any failure (interrupts included) removes the staging
directory and puts a replaced report back. The input bundle is only read.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from enum import StrEnum
from importlib.metadata import version as package_version
from pathlib import Path

import pandas as pd

from ehr_simulator import __version__
from ehr_simulator.analysis.bundle import ValidatedStudyBundle, load_phase2_bundle
from ehr_simulator.analysis.errors import AnalysisError
from ehr_simulator.analysis.figures import FIGURE_FORMATS, FIGURES, build_figure, save_figure
from ehr_simulator.analysis.schema import EXPORT_SCHEMA_VERSION, guard_cell
from ehr_simulator.analysis.summary import (
    POPULATION_APPLICABLE,
    POPULATION_CASES,
    POPULATION_REACHED,
    VALID_TELEMETRY,
    AnalysisSummaries,
    build_summaries,
)

__all__ = ["ANALYSIS_SCHEMA_VERSION", "Overwrite", "ReportInventory", "publish_report"]

#: v2: one figure file per config version, ``figure_role``, per figure ``config_version``.
ANALYSIS_SCHEMA_VERSION = "phase2_analysis_v2"
#: A previous report of either schema may be replaced.
KNOWN_ANALYSIS_SCHEMA_VERSIONS = frozenset({"phase2_analysis_v1", ANALYSIS_SCHEMA_VERSION})
#: The registry figures are supplementary descriptive figures; the main
#: message figures follow the approved analysis plan (owner, 2026-10-19).
FIGURE_ROLE = "supplementary"
#: One figure file per registry stem × config version, e.g. ``supplementary_02_case_flow_v1``.
FIGURE_FILE = "{stem}_{version}"
MANIFEST_NAME = "analysis_manifest.json"
DENOMINATORS_NAME = "denominators.csv"
FIGURES_DIR = "figures"
TABLES_DIR = "tables"
#: Summary tables written under ``tables/`` (``denominators`` sits at the root).
TABLES = (
    "case_flow",
    "response_provenance",
    "option_proportions",
    "numeric_responses",
    "attention",
    "intervention",
)
#: Libraries whose versions shape the output, recorded in the manifest.
RECORDED_PACKAGES = ("pandas", "plotnine", "matplotlib")
#: Figure 1 plots rule (auto_value) answers beside clinician ones (owner decision).
RULE_ANSWERS = "plotted_distinguished"


class Overwrite(StrEnum):
    REFUSE = "refuse"
    REPLACE = "replace"


@dataclass(frozen=True)
class ReportInventory:
    out_dir: Path
    figures: tuple[str, ...]  # file stems ``<stem>_<version>``, registry × version order
    tables: tuple[str, ...]  # file names relative to out_dir


def publish_report(
    bundle_path: Path | str,
    out_dir: Path | str,
    *,
    overwrite: Overwrite = Overwrite.REFUSE,
) -> ReportInventory:
    """Validate the bundle at *bundle_path* and publish its report at *out_dir*.

    Raises:
        AnalysisError: *out_dir* exists without ``Overwrite.REPLACE``, overlaps
            the bundle, or the bundle fails validation (nothing written).
    """
    bundle_path, out_dir = Path(bundle_path), Path(out_dir)
    _check_preconditions(bundle_path, out_dir, overwrite)

    bundle = load_phase2_bundle(bundle_path)
    summaries = build_summaries(bundle)

    out_dir.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    staging = out_dir.parent / f".{out_dir.name}.staging-{token}"
    aside = out_dir.parent / f".{out_dir.name}.previous-{token}"
    staging.mkdir()
    try:
        inventory = _write_staging(bundle, summaries, staging)

        # Rendering takes minutes: the output may have appeared meanwhile.
        _check_output(out_dir, overwrite)
        if out_dir.exists():
            out_dir.rename(aside)
        staging.rename(out_dir)
        _fsync_dir(out_dir.parent)
    except BaseException:
        # Interrupts too: a staging directory already renamed was published.
        published = not staging.exists()
        shutil.rmtree(staging, ignore_errors=True)
        if published:
            shutil.rmtree(out_dir, ignore_errors=True)
        if aside.exists() and not out_dir.exists():
            aside.rename(out_dir)
        raise

    shutil.rmtree(aside, ignore_errors=True)
    return ReportInventory(out_dir=out_dir, figures=inventory[0], tables=inventory[1])


def _check_preconditions(bundle_path: Path, out_dir: Path, overwrite: Overwrite) -> None:
    """Every refusal before anything is read or written."""
    bundle, out = bundle_path.resolve(), out_dir.resolve()
    if out == bundle or out.is_relative_to(bundle) or bundle.is_relative_to(out):
        raise AnalysisError("the output directory must not overlap the input bundle")
    _check_output(out_dir, overwrite)


def _check_output(out_dir: Path, overwrite: Overwrite) -> None:
    """REPLACE removes only an empty directory or a previous report."""
    if out_dir.is_symlink():
        raise AnalysisError(f"{out_dir} is a symbolic link; name the directory itself")
    if not out_dir.exists():
        return
    if overwrite is Overwrite.REFUSE:
        raise AnalysisError(f"{out_dir} already exists; choose another directory or replace it")
    if not out_dir.is_dir() or (any(out_dir.iterdir()) and not _is_report(out_dir)):
        raise AnalysisError(f"{out_dir} is not an analysis report; refusing to replace it")


def _is_report(directory: Path) -> bool:
    try:
        manifest = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(manifest, dict)
        and manifest.get("analysis_schema_version") in KNOWN_ANALYSIS_SCHEMA_VERSIONS
    )


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def _write_staging(
    bundle: ValidatedStudyBundle, summaries: AnalysisSummaries, staging: Path
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    figures_dir = staging / FIGURES_DIR
    figures_dir.mkdir()
    figure_entries = []
    for stem, spec in FIGURES.items():
        # One file per version: versions never share a figure.
        for version in bundle.versions:
            name = FIGURE_FILE.format(stem=stem, version=version)
            plot = build_figure(stem, bundle, summaries, version)
            written = save_figure(plot, figures_dir / name)
            for path in written:
                _fsync_file(path)
            figure_entries.append(
                {
                    "stem": stem,
                    "title": spec.title,
                    "config_version": version,
                    "files": [f"{FIGURES_DIR}/{p.name}" for p in written],
                }
            )
    _fsync_dir(figures_dir)

    (staging / TABLES_DIR).mkdir()
    named = {f"{TABLES_DIR}/{name}.csv": getattr(summaries, name) for name in TABLES}
    named[DENOMINATORS_NAME] = summaries.denominators
    table_entries = []
    for name, frame in named.items():
        content = render_csv(frame)
        _write_synced(staging / name, content)
        table_entries.append(
            {"file": name, "rows": len(frame), "sha256": hashlib.sha256(content).hexdigest()}
        )
    _fsync_dir(staging / TABLES_DIR)

    manifest = _manifest(bundle, summaries, figure_entries, table_entries)
    _write_synced(staging / MANIFEST_NAME, manifest)
    _fsync_dir(staging)
    files = tuple(
        FIGURE_FILE.format(stem=f["stem"], version=f["config_version"]) for f in figure_entries
    )
    return files, tuple(named)


def render_csv(frame: pd.DataFrame) -> bytes:
    """``<NA>`` → empty cell; text cells pass the S9c formula guard (option
    labels are operator text)."""
    out = frame.copy()
    for column in out.columns:
        if out[column].dtype == object or pd.api.types.is_string_dtype(out[column]):
            out[column] = out[column].map(lambda v: guard_cell(v) if isinstance(v, str) else v)
    buffer = io.StringIO()
    out.to_csv(buffer, index=False, na_rep="", lineterminator="\n")
    return buffer.getvalue().encode("utf-8")


def _manifest(
    bundle: ValidatedStudyBundle,
    summaries: AnalysisSummaries,
    figures: list[dict],
    tables: list[dict],
) -> bytes:
    """No wall-clock timestamp: equal inputs give an equal manifest."""
    body = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "input": {
            "export_schema_version": EXPORT_SCHEMA_VERSION,
            "manifest_sha256": bundle.manifest_sha256,
            "study_id": bundle.study_id,
        },
        "software": {
            "ehr_simulator": __version__,
            **{name: package_version(name) for name in RECORDED_PACKAGES},
        },
        "config_versions": [
            {"config_version": v.version, "config_hash": v.config_hash}
            for v in bundle.versions.values()
        ],
        "cohort": summaries.cohort.value,
        "figure_role": FIGURE_ROLE,
        "settings": {
            "descriptive_only": True,
            "pooling": "none; tables are keyed by config_version, one figure file per version",
            "grouping": "ITT arm of the realised activation",
            "valid_telemetry_status": VALID_TELEMETRY.value,
            "populations": {
                "cases": POPULATION_CASES,
                "reached": POPULATION_REACHED,
                "applicable": POPULATION_APPLICABLE,
            },
            "rule_answers": RULE_ANSWERS,
            "figure_formats": list(FIGURE_FORMATS),
        },
        "figures": figures,
        "tables": tables,
    }
    return (json.dumps(body, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_synced(path: Path, content: bytes) -> None:
    with open(path, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_file(path: Path) -> None:
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    """Make a rename or new entry in ``path`` durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
