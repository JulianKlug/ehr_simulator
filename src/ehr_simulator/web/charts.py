"""plotnine → inline SVG renderer.

The renderer is called only when the panel state is ``loading`` or ``partial``
(per Decision **D5**). Edge-case contract (Decision **D12**):

- single-row frame → valid SVG with a single point on the timeline (no
  degenerate-axis raise, locks the t=0 happy path);
- empty frame (zero rows) → valid SVG with axes but no data marks (defends
  against state-detection bugs cascading into the renderer);
- variable name not in ``frame['variable'].unique()`` (and frame non-empty)
  → :class:`KeyError` (loud failure; surfacing the bug is preferable to a
  silent empty SVG).

:func:`render_facet_timeline_svg` renders multiple variables in one faceted
plot with a shared x-axis and per-variable free y-scales (clinician feedback
on session-02: vitals must be comparable on the same time axis).

Round-02 readability fixes (FINDING-007 / FINDING-008): single-variable
charts get color, larger points, value labels, and a y-axis ``expand`` so
single-timestep frames render a visible point instead of pinning it flush
with the panel border.

Round-03 (session-02 feedback round-03 — design decisions):

- :func:`render_grouped_bp_svg` overlays SBP and DBP on a shared mmHg
  y-scale. Same-hue palette, no legend chrome — SBP is always the higher
  line. When one of SBP/DBP is missing from the slice entirely, a faint
  dashed grey "expected band" renders at the missing variable's reference
  range (mirrors the synthetic generator constants in
  :data:`BP_REFERENCE_RANGES`).
- ``is_bottom`` flag on per-panel renderers: when ``False``, x-tick labels
  + axis title are suppressed but the gridlines remain. Lets the route
  stack panels in a figure with the time axis labeled only on the
  bottom-most panel.
- Top + right spines are off (theme_minimal default); bottom + left
  ``axis_line`` are explicitly drawn for emphasis.
"""

from __future__ import annotations

import re
from io import BytesIO

import pandas as pd
import plotnine as p9

_SVG_OPEN_RE = re.compile(r"(<svg\b[^>]*?)(/?>)", re.DOTALL)

VITALS_COLORS = {
    "hr": "#c0392b",
    "sbp": "#1f6feb",
    "dbp": "#7aa7ef",
    "rr": "#8e44ad",
    "spo2": "#0e8a3a",
    "temp": "#a85d00",
}
_DEFAULT_COLOR = "#333333"

BP_REFERENCE_RANGES: dict[str, tuple[float, float]] = {
    "sbp": (110.0, 160.0),
    "dbp": (60.0, 95.0),
}

_BP_VARS: tuple[str, ...] = ("sbp", "dbp")

_AXIS_LINE_COLOR = "#888"
_AXIS_LINE_SIZE = 0.5

# All vitals panels render at exactly the same figsize so the SVGs are
# pixel-identical heights at any container width. The trade-off: the bottom
# panel's chart area is slightly smaller than the upper panels' (axis
# labels + "t (min)" title eat ~15% of the vertical space). Making the
# bottom panel taller to compensate visually breaks the "all panels the
# same size" contract that clinicians ask for first — uniformity wins.
_PANEL_WIDTH = 10.0
_PANEL_HEIGHT = 1.8


def _panel_figsize(*, is_bottom: bool) -> tuple[float, float]:  # noqa: ARG001
    # is_bottom kept in signature for callsite symmetry / future tuning;
    # currently both upper and bottom panels render at identical height.
    return (_PANEL_WIDTH, _PANEL_HEIGHT)


def _panel_theme(*, is_bottom: bool, figure_size: tuple[float, float]) -> p9.theme:
    """Shared theme for round-03 stacked panels.

    Flat axes (bottom + left ``axis_line`` only — top/right come from
    ``theme_minimal``'s blank ``panel_border``). When ``is_bottom`` is False,
    x-tick labels, ticks, and axis title are suppressed; gridlines stay so
    upper panels visually align with the bottom panel's tick positions.
    """
    elements: dict[str, object] = {
        "figure_size": figure_size,
        "axis_text_y": p9.element_text(size=9, color="#333"),
        "axis_title_y": p9.element_text(size=10, color="#333"),
        "panel_grid_major": p9.element_line(color="#e6e6e6", size=0.4),
        "panel_grid_minor": p9.element_blank(),
        "panel_border": p9.element_blank(),
        "axis_line_x": p9.element_line(color=_AXIS_LINE_COLOR, size=_AXIS_LINE_SIZE),
        "axis_line_y": p9.element_line(color=_AXIS_LINE_COLOR, size=_AXIS_LINE_SIZE),
        "plot_margin": 0.02,
    }
    if is_bottom:
        elements["axis_text_x"] = p9.element_text(size=9, color="#333")
        elements["axis_title_x"] = p9.element_text(size=10, color="#333")
    else:
        elements["axis_text_x"] = p9.element_blank()
        elements["axis_title_x"] = p9.element_blank()
        elements["axis_ticks_major_x"] = p9.element_blank()
        elements["axis_ticks_minor_x"] = p9.element_blank()
    return p9.theme_minimal() + p9.theme(**elements)


def render_timeline_svg(
    frame: pd.DataFrame,
    variable: str,
    *,
    x_range: tuple[float, float] | None = None,
    is_bottom: bool = True,
) -> str:
    """Render a single-variable timeline as inline SVG.

    The root ``<svg>`` element carries ``data-variable="{variable}"`` so a11y
    fallback tables and CSS hooks can target it without parsing.

    ``x_range`` pins the x-axis when rendering a stack of per-variable charts
    so each chart shares the same time window even when one variable has
    fewer observations than another.

    ``is_bottom`` controls (1) whether x-tick labels and the "t (min)" title
    render and (2) the figsize — bottom panels get ~0.25in extra height so
    axis labels don't eat into the drawing area. Set to ``False`` for upper
    panels in a stacked figure (round-03 layout).
    """
    if not frame.empty:
        present = set(frame["variable"].astype(str).unique().tolist())
        if variable not in present:
            raise KeyError(f"variable {variable!r} not present in frame")

    color = VITALS_COLORS.get(variable, _DEFAULT_COLOR)
    plot_df = frame.loc[frame["variable"].astype(str) == variable, ["t_minutes", "value"]].copy()
    figsize = _panel_figsize(is_bottom=is_bottom)

    scale_x = (
        p9.scale_x_continuous(name="t (min)", limits=x_range)
        if x_range is not None
        else p9.scale_x_continuous(name="t (min)")
    )

    if plot_df.empty:
        plot_df = pd.DataFrame({"t_minutes": [0.0], "value": [0.0], "_phantom": [True]})
        plot = (
            p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
            + p9.geom_blank()
            + scale_x
            + p9.scale_y_continuous(name=variable)
            + _panel_theme(is_bottom=is_bottom, figure_size=figsize)
        )
    else:
        plot = (
            p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
            + p9.geom_line(color=color, size=1.0)
            + p9.geom_point(color=color, size=3.2)
            + p9.geom_text(
                p9.aes(label="value"),
                format_string="{:.1f}",
                color="#222222",
                size=8,
                nudge_y=0,
                va="bottom",
                ha="left",
            )
            + scale_x
            + p9.scale_y_continuous(name=variable, expand=(0.5, 0.5))
            + _panel_theme(is_bottom=is_bottom, figure_size=figsize)
        )

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    return _stamp_data_variable(raw, variable)


def render_grouped_bp_svg(
    frame: pd.DataFrame,
    *,
    present_vars: frozenset[str],
    x_range: tuple[float, float],
    is_bottom: bool = False,
) -> str:
    """Render SBP+DBP overlay on a shared mmHg y-scale (round-03 BP grouping).

    ``present_vars`` is the subset of ``{"sbp", "dbp"}`` present in
    ``frame``. Variables NOT in ``present_vars`` get a faint dashed grey
    rectangle at their reference range from :data:`BP_REFERENCE_RANGES` —
    clinically honest signal that "an isolated SBP read without DBP context
    is misleading" (round-03 partial-state behavior).

    Figsize matches :func:`render_timeline_svg` so a stack of mixed grouped
    + single-variable panels scales uniformly when the container grows.

    The root ``<svg>`` is stamped with ``data-panel="vitals"``,
    ``data-group="bp"``, and ``data-bp-missing=`` (comma-separated missing
    variables, omitted when nothing is missing).
    """
    plot_df = frame.loc[
        frame["variable"].astype(str).isin(_BP_VARS),
        ["t_minutes", "variable", "value"],
    ].copy()

    has_data = not plot_df.empty
    if not has_data:
        # Phantom row keeps plotnine's axis machinery happy; geom_blank
        # renders nothing but reference bands (added below) still draw.
        mid_t = (x_range[0] + x_range[1]) / 2.0
        plot_df = pd.DataFrame(
            {
                "t_minutes": [mid_t],
                "variable": ["sbp"],
                "value": [(BP_REFERENCE_RANGES["sbp"][0] + BP_REFERENCE_RANGES["sbp"][1]) / 2.0],
            }
        )

    plot_df["variable"] = pd.Categorical(plot_df["variable"], categories=_BP_VARS, ordered=True)
    color_map = {v: VITALS_COLORS.get(v, _DEFAULT_COLOR) for v in _BP_VARS}

    plot = (
        p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value", color="variable"))
        + p9.scale_x_continuous(name="t (min)", limits=x_range)
        + p9.scale_y_continuous(name="BP (mmHg)", expand=(0.1, 0.0))
        + p9.scale_color_manual(values=color_map, guide=None)
        + _panel_theme(is_bottom=is_bottom, figure_size=_panel_figsize(is_bottom=is_bottom))
    )

    missing_vars = [v for v in _BP_VARS if v not in present_vars]
    for var in missing_vars:
        lo, hi = BP_REFERENCE_RANGES[var]
        plot = plot + p9.annotate(
            "rect",
            xmin=x_range[0],
            xmax=x_range[1],
            ymin=lo,
            ymax=hi,
            fill="#cccccc",
            alpha=0.18,
            color="#888888",
            linetype="dashed",
            size=0.4,
        )

    if has_data:
        plot = (
            plot
            + p9.geom_line(size=1.0)
            + p9.geom_point(size=3.0)
            + p9.geom_text(
                p9.aes(label="value"),
                format_string="{:.0f}",
                color="#222222",
                size=8,
                nudge_y=0,
                va="bottom",
                ha="left",
            )
        )
    else:
        plot = plot + p9.geom_blank()

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    attrs: dict[str, str | None] = {
        "data-panel": "vitals",
        "data-group": "bp",
        "data-bp-missing": ",".join(missing_vars) if missing_vars else None,
    }
    return _stamp_root_attrs(raw, attrs)


def render_facet_timeline_svg(
    frame: pd.DataFrame,
    variables: list[str],
    *,
    width: float = 7.5,
    height_per_var: float = 1.6,
) -> str:
    """Render multiple variables on a shared x-axis with per-variable free y-scales.

    Used for the vitals panel: clinicians compare HR/SBP/DBP/SpO2/temp by
    looking at the same time window across panels. One SVG, one DOM swap.

    The root ``<svg>`` is stamped with ``data-panel="vitals"`` and
    ``data-variables="hr,sbp,…"`` so downstream selectors can find it without
    parsing the plotnine output.

    Edge cases mirror :func:`render_timeline_svg`:

    - empty frame → blank canvas with a single faux row so plotnine can
      compute axes; no data marks are drawn.
    - any variable in ``variables`` missing from ``frame`` (frame non-empty)
      → that facet is rendered with no marks; we do **not** raise — the
      partial-state visual treatment is the right call when one stream is
      late but others arrived.
    - single-row-per-variable (round-02 FINDING-008): a free_y facet would
      collapse the y-range to ``[v, v]`` and render the point flush with the
      panel border; ``expand=(0.5, 0.5)`` gives the y-axis breathing room so
      the point lands in the middle of its panel.
    """
    if not variables:
        raise ValueError("variables must be non-empty")

    cols = ["t_minutes", "variable", "value"]
    plot_df = frame.loc[frame["variable"].astype(str).isin(variables), cols].copy()

    has_data = not plot_df.empty
    if not has_data:
        plot_df = pd.DataFrame(
            {
                "t_minutes": [0.0] * len(variables),
                "variable": list(variables),
                "value": [0.0] * len(variables),
            }
        )

    plot_df["variable"] = pd.Categorical(plot_df["variable"], categories=variables, ordered=True)
    color_map = {v: VITALS_COLORS.get(v, _DEFAULT_COLOR) for v in variables}

    plot = (
        p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value", color="variable"))
        + p9.facet_wrap("variable", ncol=1, scales="free_y")
        + p9.scale_x_continuous(name="t (min)")
        + p9.scale_y_continuous(name="", expand=(0.5, 0.5))
        + p9.scale_color_manual(values=color_map, guide=None)
        + p9.theme_minimal()
        + p9.theme(
            figure_size=(width, height_per_var * len(variables)),
            strip_background=p9.element_rect(fill="#eef2f7", color="#b8c2cc"),
            strip_text=p9.element_text(size=11, weight="bold", color="#222222"),
            axis_text=p9.element_text(size=9, color="#333333"),
            axis_title_x=p9.element_text(size=10, color="#333333"),
            panel_grid_major=p9.element_line(color="#e6e6e6", size=0.4),
            panel_grid_minor=p9.element_blank(),
            panel_spacing=0.4,
        )
    )
    if has_data:
        plot = (
            plot
            + p9.geom_line(size=0.9)
            + p9.geom_point(size=3.0)
            + p9.geom_text(
                p9.aes(label="value"),
                format_string="{:.1f}",
                nudge_y=0,
                va="bottom",
                ha="left",
                size=8,
                color="#222222",
            )
        )
    else:
        plot = plot + p9.geom_blank()

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    return _stamp_data_panel(raw, "vitals", variables)


def _stamp_root_attrs(svg_text: str, attrs: dict[str, str | None]) -> str:
    """Inject ``key="value"`` attributes onto the root ``<svg ...>`` tag.

    ``None`` values are skipped so callers can conditionally include attrs
    (e.g. ``data-bp-missing`` is omitted when nothing is missing).

    Round-02 fix: an earlier implementation used ``xml.etree.ElementTree`` to
    parse + re-serialize the SVG, but matplotlib's output relies on
    ``<use xlink:href="#mXX">`` references for point markers, and the ET
    roundtrip rewrote the xlink namespace prefix in a way Chromium quietly
    dropped — single-timepoint charts came back with no visible marks at all.
    String-based injection is fine here: matplotlib's SVG output is stable
    and the only thing we need is a deterministic root tag we can edit.
    """
    extra = " ".join(f'{k}="{v}"' for k, v in attrs.items() if v is not None)
    if not extra:
        return svg_text

    def _inject(match: re.Match[str]) -> str:
        return f"{match.group(1)} {extra}{match.group(2)}"

    return _SVG_OPEN_RE.sub(_inject, svg_text, count=1)


def _stamp_data_variable(svg_text: str, variable: str) -> str:
    """Tag the root ``<svg>`` with ``data-variable=`` for downstream selectors."""
    return _stamp_root_attrs(svg_text, {"data-variable": variable})


def _stamp_data_panel(svg_text: str, panel: str, variables: list[str]) -> str:
    """Tag a faceted ``<svg>`` with ``data-panel=`` and ``data-variables=``."""
    return _stamp_root_attrs(
        svg_text,
        {"data-panel": panel, "data-variables": ",".join(variables)},
    )
