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
"""

from __future__ import annotations

from io import BytesIO
from xml.etree import ElementTree as ET

import pandas as pd
import plotnine as p9


def render_timeline_svg(
    frame: pd.DataFrame,
    variable: str,
    *,
    width: float = 4.0,
    height: float = 1.6,
) -> str:
    """Render a single-variable timeline as inline SVG.

    The root ``<svg>`` element carries ``data-variable="{variable}"`` so a11y
    fallback tables and CSS hooks can target it without parsing.
    """
    if not frame.empty:
        present = set(frame["variable"].astype(str).unique().tolist())
        if variable not in present:
            raise KeyError(f"variable {variable!r} not present in frame")

    plot_df = frame.loc[frame["variable"].astype(str) == variable, ["t_minutes", "value"]].copy()
    if plot_df.empty:
        plot_df = pd.DataFrame({"t_minutes": [0.0], "value": [0.0], "_phantom": [True]})
        plot = (
            p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
            + p9.geom_blank()
            + p9.scale_x_continuous(name="t (min)")
            + p9.scale_y_continuous(name=variable)
            + p9.theme_minimal()
            + p9.theme(figure_size=(width, height))
        )
    else:
        plot = (
            p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
            + p9.geom_line()
            + p9.geom_point(size=2)
            + p9.scale_x_continuous(name="t (min)")
            + p9.scale_y_continuous(name=variable)
            + p9.theme_minimal()
            + p9.theme(figure_size=(width, height))
        )

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    return _stamp_data_variable(raw, variable)


def render_facet_timeline_svg(
    frame: pd.DataFrame,
    variables: list[str],
    *,
    width: float = 6.5,
    height_per_var: float = 1.1,
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

    plot = (
        p9.ggplot(plot_df, p9.aes(x="t_minutes", y="value"))
        + p9.facet_wrap("variable", ncol=1, scales="free_y")
        + p9.scale_x_continuous(name="t (min)")
        + p9.scale_y_continuous(name="")
        + p9.theme_minimal()
        + p9.theme(
            figure_size=(width, height_per_var * len(variables)),
            strip_background=p9.element_rect(fill="#f3f3f3", color="#cccccc"),
            panel_spacing=0.25,
        )
    )
    if has_data:  # noqa: SIM108 — plotnine geom layers don't support `geom + geom` outside `plot + geom`
        plot = plot + p9.geom_line() + p9.geom_point(size=1.8)
    else:
        plot = plot + p9.geom_blank()

    buf = BytesIO()
    plot.save(buf, format="svg", verbose=False)
    raw = buf.getvalue().decode("utf-8")
    return _stamp_data_panel(raw, "vitals", variables)


def _stamp_data_variable(svg_text: str, variable: str) -> str:
    """Tag the root ``<svg>`` with ``data-variable=`` for downstream selectors."""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text
    root.set("data-variable", variable)
    return ET.tostring(root, encoding="unicode")


def _stamp_data_panel(svg_text: str, panel: str, variables: list[str]) -> str:
    """Tag a faceted ``<svg>`` with ``data-panel=`` and ``data-variables=``."""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text
    root.set("data-panel", panel)
    root.set("data-variables", ",".join(variables))
    return ET.tostring(root, encoding="unicode")
