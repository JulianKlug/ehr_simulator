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
    "dbp": "#2c7be5",
    "spo2": "#0e8a3a",
    "temp": "#a85d00",
}
_DEFAULT_COLOR = "#333333"


def render_timeline_svg(
    frame: pd.DataFrame,
    variable: str,
    *,
    width: float = 6.5,
    height: float = 1.6,
    x_range: tuple[float, float] | None = None,
) -> str:
    """Render a single-variable timeline as inline SVG.

    The root ``<svg>`` element carries ``data-variable="{variable}"`` so a11y
    fallback tables and CSS hooks can target it without parsing.

    ``x_range`` pins the x-axis when rendering a stack of per-variable charts
    so each chart shares the same time window even when one variable has
    fewer observations than another.
    """
    if not frame.empty:
        present = set(frame["variable"].astype(str).unique().tolist())
        if variable not in present:
            raise KeyError(f"variable {variable!r} not present in frame")

    color = VITALS_COLORS.get(variable, _DEFAULT_COLOR)
    plot_df = frame.loc[frame["variable"].astype(str) == variable, ["t_minutes", "value"]].copy()

    base_theme = p9.theme_minimal() + p9.theme(
        figure_size=(width, height),
        axis_text=p9.element_text(size=9, color="#333333"),
        axis_title_x=p9.element_text(size=10, color="#333333"),
        axis_title_y=p9.element_blank(),
        panel_grid_major=p9.element_line(color="#e6e6e6", size=0.4),
        panel_grid_minor=p9.element_blank(),
        plot_margin=0.02,
    )

    if plot_df.empty:
        plot_df = pd.DataFrame({"t_minutes": [0.0], "value": [0.0], "_phantom": [True]})
        scale_x = (
            p9.scale_x_continuous(name="t (min)", limits=x_range)
            if x_range is not None
            else p9.scale_x_continuous(name="t (min)")
        )
        plot = (
            p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
            + p9.geom_blank()
            + scale_x
            + p9.scale_y_continuous(name=variable)
            + base_theme
        )
    else:
        scale_x = (
            p9.scale_x_continuous(name="t (min)", limits=x_range)
            if x_range is not None
            else p9.scale_x_continuous(name="t (min)")
        )
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
            + base_theme
        )

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    return _stamp_data_variable(raw, variable)


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


def _stamp_root_attrs(svg_text: str, attrs: dict[str, str]) -> str:
    """Inject ``key="value"`` attributes onto the root ``<svg ...>`` tag.

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
