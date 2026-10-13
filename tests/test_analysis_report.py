"""S12b report publication: validate first, stage, publish atomically.

Spec ``specs/session-12-phase25-release.md`` §4.7, §5, §10 tests 10, 13, 15
(output directory half). One full publish per module; refusal and
atomicity checks swap in a one figure registry to stay fast.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from ehr_simulator import __version__
from ehr_simulator.analysis import AnalysisError, load_phase2_bundle, report
from ehr_simulator.analysis.figures import FIGURES
from ehr_simulator.analysis.report import (
    ANALYSIS_SCHEMA_VERSION,
    MANIFEST_NAME,
    TABLES,
    Overwrite,
    publish_report,
    render_csv,
)
from ehr_simulator.analysis.summary import build_summaries
from tests.support.analysis_bundle import (
    EMPTY,
    FIXTURES,
    WALKED,
    copy_fixture,
    edit_manifest,
    snapshot,
)

pytestmark = pytest.mark.xdist_group("analysis_report")

FAST_STEM = "figure_02_case_flow"


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("report") / "phase2"
    publish_report(FIXTURES / WALKED, out)
    return out


@pytest.fixture
def fast_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(report, "FIGURES", {FAST_STEM: FIGURES[FAST_STEM]})


def _manifest(out: Path) -> dict:
    return json.loads((out / MANIFEST_NAME).read_text())


def _tree(root: Path) -> dict[str, bytes]:
    files = sorted(p for p in root.rglob("*") if p.is_file())
    return {str(p.relative_to(root)): p.read_bytes() for p in files}


def _leftovers(parent: Path) -> list[str]:
    return [p.name for p in parent.iterdir() if p.name.startswith(".")]


# ---------------------------------------------------------------------------
# Inventory and manifest (tests 10, 12)
# ---------------------------------------------------------------------------


def test_publish_writes_the_full_inventory(published: Path) -> None:
    files = set(_tree(published))
    figures = {f"figures/{stem}.{fmt}" for stem in FIGURES for fmt in ("svg", "pdf")}
    tables = {f"tables/{name}.csv" for name in TABLES}
    assert files == figures | tables | {"denominators.csv", MANIFEST_NAME}
    assert _leftovers(published.parent) == []


def test_manifest_records_inputs_versions_and_settings(published: Path) -> None:
    manifest = _manifest(published)
    bundle = load_phase2_bundle(FIXTURES / WALKED)

    assert manifest["analysis_schema_version"] == ANALYSIS_SCHEMA_VERSION
    assert manifest["input"] == {
        "export_schema_version": "phase2_export_v1",
        "manifest_sha256": bundle.manifest_sha256,
        "study_id": bundle.study_id,
    }
    assert manifest["software"]["ehr_simulator"] == __version__
    assert set(manifest["software"]) == {"ehr_simulator", "pandas", "plotnine", "matplotlib"}
    assert manifest["config_versions"] == [
        {"config_version": v.version, "config_hash": v.config_hash}
        for v in bundle.versions.values()
    ]
    assert manifest["cohort"] == "has_cases"
    assert manifest["settings"]["descriptive_only"] is True
    assert manifest["settings"]["valid_telemetry_status"] == "complete"
    assert manifest["settings"]["rule_answers"] == "plotted_distinguished"
    assert [f["stem"] for f in manifest["figures"]] == list(FIGURES)
    assert "generated_at" not in json.dumps(manifest)  # equal inputs, equal manifest


def test_table_digests_and_rows_match_the_files(published: Path) -> None:
    for entry in _manifest(published)["tables"]:
        content = (published / entry["file"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"]
        assert content.count(b"\n") - 1 == entry["rows"]


def test_tables_are_the_library_summaries(published: Path) -> None:
    summaries = build_summaries(load_phase2_bundle(FIXTURES / WALKED))
    for name in TABLES:
        expected = render_csv(getattr(summaries, name))
        assert (published / "tables" / f"{name}.csv").read_bytes() == expected
    assert (published / "denominators.csv").read_bytes() == render_csv(summaries.denominators)


def test_csv_text_cells_are_formula_guarded(published: Path) -> None:
    options = pd.read_csv(published / "tables" / "option_proportions.csv", dtype=str)
    assert "'+ new imaging finding" in set(options["option"])
    assert "+ new imaging finding" not in set(options["option"])


def test_unknown_stays_blank_not_zero(published: Path) -> None:
    options = pd.read_csv(
        published / "tables" / "option_proportions.csv", dtype=str, keep_default_na=False
    )
    no_answers = options[options["answers"] == "0"]
    assert not no_answers.empty
    assert set(no_answers["proportion"]) == {""}


def test_rerun_gives_equal_tables_and_manifest(tmp_path: Path, fast_registry: None) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    publish_report(FIXTURES / WALKED, first)
    publish_report(FIXTURES / WALKED, second)

    assert _manifest(first) == _manifest(second)
    for name in [*(f"tables/{t}.csv" for t in TABLES), "denominators.csv"]:
        assert (first / name).read_bytes() == (second / name).read_bytes()


# ---------------------------------------------------------------------------
# No activated cases (test 13)
# ---------------------------------------------------------------------------


def test_empty_bundle_publishes_a_no_data_report(tmp_path: Path) -> None:
    out = tmp_path / "empty"
    publish_report(FIXTURES / EMPTY, out)

    manifest = _manifest(out)
    assert manifest["cohort"] == "no_activated_cases"
    assert [f["stem"] for f in manifest["figures"]] == list(FIGURES)
    options = pd.read_csv(out / "tables" / "option_proportions.csv", keep_default_na=False)
    assert set(options["proportion"]) <= {""}  # no fabricated rate
    flow = pd.read_csv(out / "tables" / "case_flow.csv")
    assert (flow["activated"] == 0).all()


# ---------------------------------------------------------------------------
# Refusals and atomicity (test 15, output half)
# ---------------------------------------------------------------------------


def test_existing_output_is_refused_and_untouched(tmp_path: Path) -> None:
    out = tmp_path / "report"
    out.mkdir()
    (out / "keep.txt").write_text("previous")

    with pytest.raises(AnalysisError, match="already exists"):
        publish_report(FIXTURES / WALKED, out)

    assert _tree(out) == {"keep.txt": b"previous"}
    assert _leftovers(tmp_path) == []


def test_bad_bundle_writes_nothing(tmp_path: Path) -> None:
    bundle = copy_fixture(tmp_path)
    (bundle / "answers.csv").write_bytes((bundle / "answers.csv").read_bytes() + b"\n")
    out = tmp_path / "out" / "report"

    with pytest.raises(AnalysisError, match="SHA256"):
        publish_report(bundle, out)

    assert not out.exists()
    assert not out.parent.exists() or _leftovers(out.parent) == []


def test_bad_bundle_keeps_a_previous_report(tmp_path: Path, fast_registry: None) -> None:
    out = tmp_path / "report"
    publish_report(FIXTURES / WALKED, out)
    before = _tree(out)
    bundle = copy_fixture(tmp_path)
    edit_manifest(bundle, lambda m: m.__setitem__("export_schema_version", "phase2_export_v2"))

    with pytest.raises(AnalysisError, match="export schema"):
        publish_report(bundle, out, overwrite=Overwrite.REPLACE)

    assert _tree(out) == before
    assert _leftovers(tmp_path) == []


@pytest.mark.parametrize("inside", ["bundle_child", "bundle_itself", "bundle_parent"])
def test_output_overlapping_the_bundle_is_refused(tmp_path: Path, inside: str) -> None:
    bundle = copy_fixture(tmp_path)
    before = snapshot(bundle)
    out = {
        "bundle_child": bundle / "report",
        "bundle_itself": bundle,
        "bundle_parent": tmp_path,
    }[inside]

    with pytest.raises(AnalysisError, match="overlap"):
        publish_report(bundle, out, overwrite=Overwrite.REPLACE)

    assert snapshot(bundle) == before


def test_replace_swaps_the_whole_directory(tmp_path: Path, fast_registry: None) -> None:
    out = tmp_path / "report"
    publish_report(FIXTURES / WALKED, out)
    (out / "stale.txt").write_text("old")

    publish_report(FIXTURES / WALKED, out, overwrite=Overwrite.REPLACE)

    assert not (out / "stale.txt").exists()
    assert (out / MANIFEST_NAME).is_file()
    assert _leftovers(tmp_path) == []


def test_replace_fills_an_empty_directory(tmp_path: Path, fast_registry: None) -> None:
    out = tmp_path / "report"
    out.mkdir()

    publish_report(FIXTURES / WALKED, out, overwrite=Overwrite.REPLACE)

    assert (out / MANIFEST_NAME).is_file()
    assert _leftovers(tmp_path) == []


def test_replace_refuses_a_directory_that_is_not_a_report(
    tmp_path: Path, fast_registry: None
) -> None:
    """REPLACE deletes only a previous report, e.g. never a mistyped home dir."""
    out = tmp_path / "notes"
    out.mkdir()
    (out / "keep.txt").write_text("unrelated")

    with pytest.raises(AnalysisError, match="not an analysis report"):
        publish_report(FIXTURES / WALKED, out, overwrite=Overwrite.REPLACE)

    assert _tree(out) == {"keep.txt": b"unrelated"}
    assert _leftovers(tmp_path) == []


def test_replace_refuses_a_symlinked_output(tmp_path: Path, fast_registry: None) -> None:
    target = tmp_path / "real_report"
    publish_report(FIXTURES / WALKED, target)
    before = _tree(target)
    link = tmp_path / "report"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(AnalysisError, match="symbolic link"):
        publish_report(FIXTURES / WALKED, link, overwrite=Overwrite.REPLACE)

    assert link.is_symlink()
    assert _tree(target) == before


def test_refuse_never_replaces_an_output_created_while_rendering(
    tmp_path: Path, fast_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent run that publishes first keeps its report."""
    out = tmp_path / "report"
    save = report.save_figure

    def concurrent_publish(*args, **kwargs):
        out.mkdir()
        (out / "keep.txt").write_text("other run")
        return save(*args, **kwargs)

    monkeypatch.setattr(report, "save_figure", concurrent_publish)
    with pytest.raises(AnalysisError, match="already exists"):
        publish_report(FIXTURES / WALKED, out)

    assert _tree(out) == {"keep.txt": b"other run"}
    assert _leftovers(tmp_path) == []


def test_failure_while_staging_restores_the_previous_report(
    tmp_path: Path, fast_registry: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "report"
    publish_report(FIXTURES / WALKED, out)
    before = _tree(out)

    def broken(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(report, "save_figure", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        publish_report(FIXTURES / WALKED, out, overwrite=Overwrite.REPLACE)

    assert _tree(out) == before
    assert _leftovers(tmp_path) == []


def test_input_bundle_is_only_read(tmp_path: Path, fast_registry: None) -> None:
    bundle = copy_fixture(tmp_path)
    before = snapshot(bundle)
    publish_report(bundle, tmp_path / "report")
    assert snapshot(bundle) == before
