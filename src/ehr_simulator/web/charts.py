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


def _stamp_data_variable(svg_text: str, variable: str) -> str:
    """Tag the root ``<svg>`` with ``data-variable=`` for downstream selectors."""
    ET.register_namespace("", "http://www.w3.org/2000/svg")
    try:
        root = ET.fromstring(svg_text)
    except ET.ParseError:
        return svg_text
    root.set("data-variable", variable)
    return ET.tostring(root, encoding="unicode")
