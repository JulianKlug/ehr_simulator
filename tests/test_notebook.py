"""S12b notebook: headless execution on the committed synthetic bundle.

Spec ``specs/session-12-phase25-release.md`` §6, §10 tests 19, 20. Needs the
``analysis`` dependency group (``uv sync --group analysis``); CI runs
``pytest -m notebook`` on Python 3.11 and 3.12.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ehr_simulator.analysis import load_phase2_bundle
from ehr_simulator.analysis.figures import FIGURE_FORMATS, FIGURES
from ehr_simulator.analysis.report import MANIFEST_NAME, TABLES, render_csv
from ehr_simulator.analysis.summary import build_summaries
from tests.support.analysis_bundle import FIXTURES, WALKED

NOTEBOOK = Path(__file__).parents[1] / "notebooks" / "phase2_analysis.ipynb"
KERNEL = "python3"
CELL_TIMEOUT_SECONDS = 600


def _execute(out: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Run the notebook from its own directory, as a reader would.

    Imported here, not skipped: a missing ``analysis`` group must fail loudly.
    """
    import nbclient
    import nbformat

    monkeypatch.setenv("EHR_SIM_PHASE2_BUNDLE", str(FIXTURES / WALKED))
    monkeypatch.setenv("EHR_SIM_ANALYSIS_OUT", str(out))
    notebook = nbformat.read(NOTEBOOK, as_version=4)
    client = nbclient.NotebookClient(
        notebook,
        timeout=CELL_TIMEOUT_SECONDS,
        kernel_name=KERNEL,
        resources={"metadata": {"path": str(NOTEBOOK.parent)}},
    )
    client.execute()
    return notebook


def _output_text(notebook) -> str:
    parts = []
    for cell in notebook.cells:
        for output in cell.get("outputs", []):
            parts.append(output.get("text", ""))
            parts += [str(v) for k, v in output.get("data", {}).items() if k.startswith("text/")]
    return "\n".join(parts)


def test_committed_notebook_has_no_outputs() -> None:
    notebook = json.loads(NOTEBOOK.read_text())
    assert notebook["metadata"]["kernelspec"]["name"] == KERNEL
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == [] and cell["execution_count"] is None


@pytest.mark.notebook
def test_notebook_regenerates_the_report_from_the_bundle_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    notebook = _execute(first, monkeypatch)
    _execute(second, monkeypatch)

    # 19: every expected figure file, one per config version.
    bundle = load_phase2_bundle(FIXTURES / WALKED)
    for stem in FIGURES:
        for version in bundle.versions:
            for fmt in FIGURE_FORMATS:
                assert (first / "figures" / f"{stem}_{version}.{fmt}").stat().st_size > 0

    # 20: the notebook's tables are the library's, independently recomputed.
    summaries = build_summaries(load_phase2_bundle(FIXTURES / WALKED))
    for name in TABLES:
        expected = render_csv(getattr(summaries, name))
        assert (first / "tables" / f"{name}.csv").read_bytes() == expected
    assert (first / "denominators.csv").read_bytes() == render_csv(summaries.denominators)

    # 20: a rerun gives equal values and figure inventory.
    manifests = [json.loads((d / MANIFEST_NAME).read_text()) for d in (first, second)]
    assert manifests[0] == manifests[1]

    # Privacy: no clinician / patient id in any printed or displayed output.
    text = _output_text(notebook)
    for identifier in set(bundle.cases["clinician_id"]) | set(bundle.cases["patient_id"]):
        assert identifier not in text
