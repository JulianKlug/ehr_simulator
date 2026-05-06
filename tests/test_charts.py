"""plotnine→SVG renderer tests (Decision D12).

Edge-case contract:

- single-row → valid SVG (locks the t=0 happy path);
- empty frame → valid SVG with axes only;
- variable-not-in-frame → :class:`KeyError`.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ehr_simulator.web.charts import render_facet_timeline_svg, render_timeline_svg


def test_render_timeline_svg_returns_svg_string() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": [0.0, 60.0, 180.0],
            "variable": ["hr", "hr", "hr"],
            "value": [72.0, 80.0, 75.0],
            "unit": ["bpm", "bpm", "bpm"],
        }
    )
    out = render_timeline_svg(frame, "hr")
    assert out.startswith(("<?xml", "<svg")), f"expected SVG prefix, got: {out[:40]!r}"
    assert 'data-variable="hr"' in out
    assert len(out) > 0


def test_render_timeline_svg_handles_single_row() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": [0.0],
            "variable": ["hr"],
            "value": [72.0],
            "unit": ["bpm"],
        }
    )
    out = render_timeline_svg(frame, "hr")
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-variable="hr"' in out


def test_render_timeline_svg_handles_empty_frame() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": pd.Series([], dtype="float64"),
            "variable": pd.Series([], dtype="object"),
            "value": pd.Series([], dtype="float64"),
            "unit": pd.Series([], dtype="object"),
        }
    )
    out = render_timeline_svg(frame, "hr")
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-variable="hr"' in out


def test_render_timeline_svg_raises_on_missing_variable() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": [0.0, 60.0],
            "variable": ["hr", "hr"],
            "value": [72.0, 80.0],
            "unit": ["bpm", "bpm"],
        }
    )
    with pytest.raises(KeyError):
        render_timeline_svg(frame, "sbp")


def test_render_facet_timeline_svg_returns_svg_with_panel_metadata() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": [0.0, 60.0, 0.0, 60.0],
            "variable": ["hr", "hr", "sbp", "sbp"],
            "value": [72.0, 80.0, 120.0, 130.0],
            "unit": ["bpm", "bpm", "mmHg", "mmHg"],
        }
    )
    out = render_facet_timeline_svg(frame, ["hr", "sbp"])
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-panel="vitals"' in out
    assert 'data-variables="hr,sbp"' in out


def test_render_facet_timeline_svg_handles_empty_frame() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": pd.Series([], dtype="float64"),
            "variable": pd.Series([], dtype="object"),
            "value": pd.Series([], dtype="float64"),
            "unit": pd.Series([], dtype="object"),
        }
    )
    out = render_facet_timeline_svg(frame, ["hr", "sbp"])
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-panel="vitals"' in out


def test_render_facet_timeline_svg_rejects_empty_variables() -> None:
    frame = pd.DataFrame(
        {
            "t_minutes": [0.0],
            "variable": ["hr"],
            "value": [72.0],
            "unit": ["bpm"],
        }
    )
    with pytest.raises(ValueError):
        render_facet_timeline_svg(frame, [])
