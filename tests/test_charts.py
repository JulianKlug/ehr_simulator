"""plotnine→SVG renderer tests (Decision D12).

Edge-case contract:

- single-row → valid SVG (locks the t=0 happy path);
- empty frame → valid SVG with axes only;
- variable-not-in-frame → :class:`KeyError`.

Round-03 additions:

- ``render_grouped_bp_svg`` overlays SBP+DBP on a shared mmHg y-scale and
  renders a faint dashed reference band when one of them is missing.
- ``is_bottom=False`` suppresses x-tick labels and the "t (min)" title so
  upper panels in a stacked figure don't repeat the time axis.
"""

from __future__ import annotations

import pandas as pd
import pytest

from ehr_simulator.web.charts import (
    BP_REFERENCE_RANGES,
    render_facet_timeline_svg,
    render_grouped_bp_svg,
    render_timeline_svg,
)


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


# Round-03: BP-grouped renderer.


def _bp_frame(variables: tuple[str, ...]) -> pd.DataFrame:
    values_by_var = {"sbp": (130.0, 125.0), "dbp": (80.0, 78.0)}
    rows = []
    for var in variables:
        for t, v in zip((0.0, 60.0), values_by_var[var], strict=True):
            rows.append({"t_minutes": t, "variable": var, "value": v, "unit": "mmHg"})
    return pd.DataFrame(rows)


def test_render_grouped_bp_svg_both_present_no_missing_flag() -> None:
    frame = _bp_frame(("sbp", "dbp"))
    out = render_grouped_bp_svg(frame, present_vars=frozenset({"sbp", "dbp"}), x_range=(0.0, 60.0))
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-panel="vitals"' in out
    assert 'data-group="bp"' in out
    assert "data-bp-missing" not in out


def test_render_grouped_bp_svg_partial_dbp_missing_stamps_attr() -> None:
    frame = _bp_frame(("sbp",))
    out = render_grouped_bp_svg(frame, present_vars=frozenset({"sbp"}), x_range=(0.0, 60.0))
    assert 'data-bp-missing="dbp"' in out
    assert 'data-group="bp"' in out


def test_render_grouped_bp_svg_partial_sbp_missing_stamps_attr() -> None:
    frame = _bp_frame(("dbp",))
    out = render_grouped_bp_svg(frame, present_vars=frozenset({"dbp"}), x_range=(0.0, 60.0))
    assert 'data-bp-missing="sbp"' in out


def test_render_grouped_bp_svg_neither_present_renders_axes() -> None:
    """Defensive: state-detection bugs should not crash the renderer."""
    frame = pd.DataFrame(
        {
            "t_minutes": pd.Series([], dtype="float64"),
            "variable": pd.Series([], dtype="object"),
            "value": pd.Series([], dtype="float64"),
        }
    )
    out = render_grouped_bp_svg(frame, present_vars=frozenset(), x_range=(0.0, 60.0))
    assert out.startswith(("<?xml", "<svg"))
    assert 'data-bp-missing="sbp,dbp"' in out


def test_bp_reference_ranges_match_synthetic_generator() -> None:
    """The expected band uses the same y-range as the synthetic generator —
    drift here means the partial-state visual no longer reflects the
    distribution of synthetic data."""
    assert BP_REFERENCE_RANGES["sbp"] == (110.0, 160.0)
    assert BP_REFERENCE_RANGES["dbp"] == (60.0, 95.0)


def test_render_timeline_svg_is_bottom_false_suppresses_x_axis_title() -> None:
    """Upper panels in a stacked figure must not repeat the time axis title."""
    frame = pd.DataFrame(
        {"t_minutes": [0.0, 60.0], "variable": ["hr", "hr"], "value": [72.0, 80.0]}
    )
    bottom = render_timeline_svg(frame, "hr", x_range=(0.0, 60.0), is_bottom=True)
    upper = render_timeline_svg(frame, "hr", x_range=(0.0, 60.0), is_bottom=False)
    # The bottom panel renders "t (min)" as the x-axis title; upper doesn't.
    assert "t (min)" in bottom
    assert "t (min)" not in upper
