"""Round-03 vitals redesign — prototype renderers for the upper plot.

Renders three multi-axis techniques against denser synthetic data so the design
review can pick a winner. Outputs PNGs to ~/.gstack/projects/$SLUG/designs/.

Variants:
  A) Overlay  — all 4 streams on one shared y-scale, color-distinguished.
  B) Dual-y   — BP on left axis, HR/RR on right axis (matplotlib twinx).
  C) Facet    — one card with three sub-panels (BP/HR/RR), shared x, free y;
                strip headers rendered in HTML in the live UI but stamped on
                the panel for the mockup.

Run: uv run python scripts/round03_vitals_mockup.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotnine as p9


def _build_dense_frame(seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    t = np.linspace(0, 240, 13)
    sbp = 138 + 8 * np.sin(t / 50) + rng.normal(0, 3, t.size)
    dbp = 82 + 5 * np.sin(t / 50) + rng.normal(0, 2, t.size)
    hr = 88 + 6 * np.cos(t / 70) + rng.normal(0, 2, t.size)
    rr = 16 + 1.5 * np.sin(t / 80) + rng.normal(0, 0.6, t.size)
    rows = []
    for var, series, unit in (
        ("sbp", sbp, "mmHg"),
        ("dbp", dbp, "mmHg"),
        ("hr", hr, "bpm"),
        ("rr", rr, "breaths/min"),
    ):
        for tt, vv in zip(t, series, strict=True):
            rows.append({"t_minutes": float(tt), "variable": var, "value": float(vv), "unit": unit})
    return pd.DataFrame(rows)


COLORS = {
    "sbp": "#1f6feb",
    "dbp": "#7aa7ef",
    "hr": "#c0392b",
    "rr": "#8e44ad",
}


def render_overlay(df: pd.DataFrame, out_path: Path) -> None:
    """A) All four streams on a shared y-scale."""
    df = df.copy()
    df["variable"] = pd.Categorical(
        df["variable"], categories=["sbp", "dbp", "hr", "rr"], ordered=True
    )
    plot = (
        p9.ggplot(df, p9.aes(x="t_minutes", y="value", color="variable"))
        + p9.geom_line(size=1.0)
        + p9.geom_point(size=2.4)
        + p9.scale_color_manual(values=COLORS)
        + p9.scale_x_continuous(name="t (min)", breaks=[0, 60, 120, 180, 240])
        + p9.scale_y_continuous(name="value")
        + p9.labs(title="A) Overlay — shared y-scale, color legend")
        + p9.theme_minimal()
        + p9.theme(
            figure_size=(10.0, 3.2),
            legend_position="top",
            legend_title=p9.element_blank(),
            panel_grid_major=p9.element_line(color="#e6e6e6", size=0.4),
            panel_grid_minor=p9.element_blank(),
            axis_text=p9.element_text(size=10),
            axis_title=p9.element_text(size=11),
        )
    )
    plot.save(out_path, dpi=140, verbose=False)


def render_dual_y(df: pd.DataFrame, out_path: Path) -> None:
    """B) Dual y-axis via matplotlib twinx — BP left, HR/RR right."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax_left = plt.subplots(figsize=(10.0, 3.2), dpi=140)
    ax_right = ax_left.twinx()

    pivot = df.pivot(index="t_minutes", columns="variable", values="value").sort_index()
    ax_left.plot(pivot.index, pivot["sbp"], color=COLORS["sbp"], label="sbp", marker="o", lw=1.6)
    ax_left.plot(pivot.index, pivot["dbp"], color=COLORS["dbp"], label="dbp", marker="o", lw=1.6)
    ax_right.plot(pivot.index, pivot["hr"], color=COLORS["hr"], label="hr", marker="s", lw=1.6)
    ax_right.plot(pivot.index, pivot["rr"], color=COLORS["rr"], label="rr", marker="^", lw=1.6)

    ax_left.set_xlabel("t (min)", fontsize=11)
    ax_left.set_ylabel("BP (mmHg)", color="#1f6feb", fontsize=11)
    ax_right.set_ylabel("HR (bpm) / RR (breaths/min)", color="#c0392b", fontsize=11)
    ax_left.tick_params(axis="y", labelcolor="#1f6feb")
    ax_right.tick_params(axis="y", labelcolor="#c0392b")
    ax_left.grid(True, color="#e6e6e6", lw=0.4)
    ax_left.set_xticks([0, 60, 120, 180, 240])

    lines_l, labels_l = ax_left.get_legend_handles_labels()
    lines_r, labels_r = ax_right.get_legend_handles_labels()
    ax_left.legend(
        lines_l + lines_r,
        labels_l + labels_r,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.18),
        ncol=4,
        frameon=False,
    )
    ax_left.set_title("B) Dual y-axis — BP left, HR+RR right", fontsize=11, pad=18)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def render_facet(df: pd.DataFrame, out_path: Path) -> None:
    """C) Facet — three sub-panels (BP, HR, RR) sharing x-axis, free y.

    SBP+DBP combined into one BP panel (round-03 grouping), HR own panel, RR own.
    """
    df = df.copy()
    df["panel"] = df["variable"].map(
        {"sbp": "BP (mmHg)", "dbp": "BP (mmHg)", "hr": "HR (bpm)", "rr": "RR (br/min)"}
    )
    df["panel"] = pd.Categorical(
        df["panel"], categories=["BP (mmHg)", "HR (bpm)", "RR (br/min)"], ordered=True
    )
    df["variable"] = pd.Categorical(
        df["variable"], categories=["sbp", "dbp", "hr", "rr"], ordered=True
    )
    plot = (
        p9.ggplot(df, p9.aes(x="t_minutes", y="value", color="variable"))
        + p9.geom_line(size=1.0)
        + p9.geom_point(size=2.4)
        + p9.facet_wrap("panel", ncol=1, scales="free_y")
        + p9.scale_color_manual(values=COLORS, guide=None)
        + p9.scale_x_continuous(name="t (min)", breaks=[0, 60, 120, 180, 240])
        + p9.scale_y_continuous(name="")
        + p9.labs(title="C) Faceted — BP grouped, HR + RR own panels, shared x")
        + p9.theme_minimal()
        + p9.theme(
            figure_size=(10.0, 5.0),
            strip_background=p9.element_rect(fill="#eef2f7", color="#b8c2cc"),
            strip_text=p9.element_text(size=11, weight="bold", color="#222"),
            panel_grid_major=p9.element_line(color="#e6e6e6", size=0.4),
            panel_grid_minor=p9.element_blank(),
            panel_spacing=0.4,
            axis_text=p9.element_text(size=10),
            axis_title=p9.element_text(size=11),
        )
    )
    plot.save(out_path, dpi=140, verbose=False)


def render_html_stitched(df: pd.DataFrame, out_path: Path) -> None:
    """D) Per-group mini-charts with HTML strip labels (FINDING-007-style).

    Three plotnine charts (BP, HR, RR) rendered separately with a shared
    x-range, then composited in matplotlib gridspec for the mockup. In the
    live UI, these would be three SVGs stitched in the Jinja template (like
    the current per-variable approach) so HTML/CSS owns the labels — bypasses
    plotnine's facet-strip rendering bug.

    BP panel groups SBP + DBP (round-03 ask), HR + RR each get their own
    panel. All three share the time axis.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = plt.figure(figsize=(10.0, 4.6), dpi=140)
    gs = fig.add_gridspec(3, 1, height_ratios=[1.6, 1.0, 1.0], hspace=0.15)

    bp = df[df["variable"].isin(["sbp", "dbp"])]
    hr = df[df["variable"] == "hr"]
    rr = df[df["variable"] == "rr"]

    # BP panel — both lines share y-axis (mmHg)
    ax_bp = fig.add_subplot(gs[0])
    for var in ("sbp", "dbp"):
        sub = bp[bp["variable"] == var]
        ax_bp.plot(
            sub["t_minutes"],
            sub["value"],
            color=COLORS[var],
            label=var,
            marker="o",
            lw=1.6,
            ms=4,
        )
    ax_bp.set_ylabel("BP (mmHg)", fontsize=10, color="#222")
    ax_bp.legend(loc="upper right", frameon=False, fontsize=9, ncol=2)
    ax_bp.grid(True, color="#e6e6e6", lw=0.4)
    ax_bp.set_xticklabels([])
    ax_bp.tick_params(axis="x", length=0)

    # HR panel
    ax_hr = fig.add_subplot(gs[1], sharex=ax_bp)
    ax_hr.plot(hr["t_minutes"], hr["value"], color=COLORS["hr"], marker="s", lw=1.6, ms=4)
    ax_hr.set_ylabel("HR (bpm)", fontsize=10, color="#222")
    ax_hr.grid(True, color="#e6e6e6", lw=0.4)
    ax_hr.set_xticklabels([])
    ax_hr.tick_params(axis="x", length=0)

    # RR panel — shared x with BP/HR
    ax_rr = fig.add_subplot(gs[2], sharex=ax_bp)
    ax_rr.plot(rr["t_minutes"], rr["value"], color=COLORS["rr"], marker="^", lw=1.6, ms=4)
    ax_rr.set_ylabel("RR (br/min)", fontsize=10, color="#222")
    ax_rr.set_xlabel("t (min)", fontsize=11)
    ax_rr.grid(True, color="#e6e6e6", lw=0.4)
    ax_rr.set_xticks([0, 60, 120, 180, 240])

    fig.suptitle(
        "D) HTML-stitched groups — BP grouped, HR + RR own panels, shared x-axis",
        fontsize=11,
        y=0.99,
    )
    fig.savefig(out_path, dpi=140, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    out_dir = Path(os.environ.get("OUT_DIR", ""))
    if not out_dir:
        print("OUT_DIR env var required", file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)

    df = _build_dense_frame()
    render_overlay(df, out_dir / "variant-A-overlay.png")
    render_dual_y(df, out_dir / "variant-B-dual-y.png")
    render_facet(df, out_dir / "variant-C-facet.png")
    render_html_stitched(df, out_dir / "variant-D-stitched.png")
    print(f"wrote 4 variants to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
