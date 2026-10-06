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


# ---------------------------------------------------------------------------
# S8: aggregate families (min / median / max)
# ---------------------------------------------------------------------------


def _family_rows(rows: list[tuple[float, str, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"t_minutes": t, "variable": v, "value": x, "unit": "bpm"} for t, v, x in rows]
    )


def test_range_renderer_complete_rows_valid_svg() -> None:
    from ehr_simulator.web.charts import family_frame, render_range_timeline_svg

    frame = _family_rows(
        [
            (t, v, x)
            for t in (0.0, 60.0)
            for v, x in (("hr_min", 60.0), ("hr", 70.0), ("hr_max", 90.0))
        ]
    )
    out = render_range_timeline_svg(frame, min_var="hr_min", median_var="hr", max_var="hr_max")
    assert out.lstrip().startswith(("<?xml", "<svg"))
    assert 'data-variable="hr"' in out and 'data-range="true"' in out
    family = family_frame(frame, min_var="hr_min", median_var="hr", max_var="hr_max")
    assert family[["low", "median", "high"]].values.tolist() == [[60.0, 70.0, 90.0]] * 2


def test_range_renderer_missing_bound_never_invented() -> None:
    from ehr_simulator.web.charts import _range_bars, family_frame, render_range_timeline_svg

    frame = _family_rows(
        [(0.0, "hr", 70.0), (0.0, "hr_max", 90.0), (60.0, "hr_min", 50.0), (120.0, "hr", 75.0)]
    )
    family = family_frame(frame, min_var="hr_min", median_var="hr", max_var="hr_max")
    bars = _range_bars(family)
    # t=0: median → max only; t=60: bound without median → no bar; t=120: no bounds.
    assert bars[["t_minutes", "ymin", "ymax"]].values.tolist() == [[0.0, 70.0, 90.0]]
    assert pd.isna(family.loc[family.t_minutes == 0.0, "low"]).all()
    out = render_range_timeline_svg(frame, min_var="hr_min", median_var="hr", max_var="hr_max")
    assert 'data-variable="hr"' in out


def test_range_line_breaks_at_gap_over_one_bucket() -> None:
    from ehr_simulator.web.charts import _segments

    t = pd.Series([0.0, 60.0, 180.0, 240.0])
    assert _segments(t).tolist() == [0, 0, 1, 1]


def test_grouped_bp_with_bounds_renders() -> None:
    frame = _family_rows(
        [
            (t, v, x)
            for t in (0.0, 60.0)
            for v, x in (("sbp_min", 120.0), ("sbp", 140.0), ("sbp_max", 170.0), ("dbp", 80.0))
        ]
    )
    out = render_grouped_bp_svg(frame, present_vars=frozenset({"sbp", "dbp"}), x_range=(0.0, 60.0))
    assert 'data-group="bp"' in out
