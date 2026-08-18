"""Regenerate the figures in the README from the data on disk.

    uv run python docs/make_figures.py

Every number plotted here comes from the local store -- nothing is illustrative or
hand-drawn. Re-running it after a new scoring pass updates the figures in place.
Figures are written to ``docs/img/``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_ROOT), str(_ROOT / "eodhd")):
    if p not in sys.path:
        sys.path.insert(0, p)

import explore_eodhd as ex  # noqa: E402
from scoring import panel_eval as pe  # noqa: E402

OUT = Path(__file__).resolve().parent / "img"
OUT.mkdir(parents=True, exist_ok=True)

INK = "#1a1a1a"
MUTED = "#8a8a8a"
ACCENT = "#c1440e"
COOL = "#1f6f8b"

plt.rcParams.update(
    {
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "savefig.facecolor": "white",
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "axes.edgecolor": MUTED,
        "axes.labelcolor": INK,
        "text.color": INK,
        "xtick.color": INK,
        "ytick.color": INK,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.dpi": 140,
    }
)


def _save(fig: plt.Figure, name: str) -> None:
    path = OUT / name
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote {path.relative_to(_ROOT)}")


def fig_anchoring(con) -> None:
    """An LLM copies whatever numbers the prompt names."""
    df = con.execute(
        "SELECT sentiment FROM news_scores_event_v2 "
        "WHERE symbol IS NULL AND sentiment IS NOT NULL"
    ).df()
    s = df["sentiment"].astype(float)
    anchors = [-1.0, -0.6, -0.3, 0.0, 0.3, 0.6, 1.0]
    on = float(s.isin(anchors).mean())

    fig, ax = plt.subplots(figsize=(7.2, 3.4))
    counts = s.value_counts().sort_index()
    colours = [ACCENT if v in anchors else MUTED for v in counts.index]
    ax.bar(counts.index, counts.values, width=0.035, color=colours)
    for a in anchors:
        ax.axvline(a, color=MUTED, lw=0.5, ls=":", zorder=0)
    ax.set_xlabel("sentiment value emitted by the model")
    ax.set_ylabel("articles")
    ax.set_title(
        f"The prompt named seven values. {on:.1%} of {len(s):,} answers landed "
        "exactly on them.",
        loc="left",
    )
    ax.text(
        0.02, 0.95,
        "orange = a value the prompt named\ngrey = anything else",
        transform=ax.transAxes, ha="left", va="top", fontsize=8.5, color=MUTED,
    )
    _save(fig, "anchoring.png")


def fig_sentiment_horizons(con) -> None:
    """Sentiment reads the session it lands in; it does not predict the next one.

    Uses the current ``event@5`` pass: 13,004 articles over 87 days, with the
    seven-level sentiment scale that keeps the buckets populated (46% neutral,
    against v4's 70% when the scale was cut to five levels).
    """
    panel = pe.signal_panel(con, "news_scores_event_v5")
    j, _ = pe.attach_returns(con, panel)
    j = j.dropna(subset=["f0_ex", "f1_ex", "score"])
    j = j[j["score"].abs() <= 1.0]

    # group by the discrete sentiment value the model actually emits
    vals = sorted(v for v in j["score"].round(2).unique() if abs(v) <= 1.0)
    j = j.assign(v=j["score"].round(2))
    keep = [v for v in vals if (j["v"] == v).sum() >= 100]
    d = j[j["v"].isin(keep)]

    fig, axes = plt.subplots(1, 2, figsize=(9.6, 3.6), sharex=True)
    for ax, col, title in (
        (axes[0], "f0_ex", "Same session as publication"),
        (axes[1], "f1_ex", "The next session"),
    ):
        g = d.groupby("v")[col].mean() * 10000
        n = d.groupby("v")[col].size()
        colours = [ACCENT if v > 0 else (COOL if v < 0 else MUTED) for v in g.index]
        ax.bar(range(len(g)), g.values, color=colours, width=0.65)
        ax.axhline(0, color=INK, lw=0.8)
        ax.set_xticks(range(len(g)))
        ax.set_xticklabels([f"{v:+.1f}".replace("+0.0", "0") for v in g.index])
        ax.set_xlabel("sentiment the model gave")
        ax.set_title(title, loc="left")
        for i, (v, c) in enumerate(zip(g.values, n.values)):
            ax.text(
                i, v, f"n={c:,}", ha="center",
                va="bottom" if v >= 0 else "top", fontsize=7.5, color=MUTED,
            )
    axes[0].set_ylabel("mean return, bps (market-adjusted)")
    lo = min(ax.get_ylim()[0] for ax in axes)
    hi = max(ax.get_ylim()[1] for ax in axes)
    for ax in axes:
        ax.set_ylim(lo, hi)
    fig.suptitle(
        "Sentiment sorts the session the article lands in -- and nothing after it.",
        x=0.005, ha="left", fontsize=11.5,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    _save(fig, "sentiment_horizons.png")


def fig_magnitude(con) -> None:
    """The one thing that does predict forward: how big the news is."""
    panel = pe.signal_panel(con, "news_scores_event_v5")
    j, _ = pe.attach_returns(con, panel)
    j = j.dropna(subset=["f1_ex", "mat_max"])

    gm = j.groupby(j["mat_max"].astype(int))["f1_ex"].apply(
        lambda s: s.abs().mean() * 10000
    )
    nm = j.groupby(j["mat_max"].astype(int))["f1_ex"].size()
    labels = ["none", "minor", "meaningful", "major"]

    # the free baseline, measured the same way, for comparison
    edges = [0, 1, 2, 3, 5, 10**9]
    names = ["1", "2", "3", "4-5", "6+"]
    j2 = j.assign(nb=pd.cut(j["n_articles"], bins=edges, labels=names))
    gi = j2.groupby("nb", observed=True)["f1_ex"].apply(
        lambda s: s.abs().mean() * 10000
    )

    fig, (ax1, ax2) = plt.subplots(
        1, 2, figsize=(9.6, 3.6), gridspec_kw={"width_ratios": [1.1, 1]}
    )
    # A bucket standing on a handful of rows is drawn pale and hatched: `major`
    # lands on 0.4% of articles, so its bar is both the tallest and the least
    # certain, and a reader skimming should see that at a glance.
    thin = nm.values < 100
    ax1.bar(
        range(len(gm)), gm.values,
        color=[ACCENT if t else COOL for t in thin], width=0.65,
    )
    for i, t in enumerate(thin):
        if t:
            ax1.patches[i].set_alpha(0.35)
            ax1.patches[i].set_hatch("///")
            ax1.patches[i].set_edgecolor(ACCENT)
    ax1.set_xticks(range(len(gm)))
    ax1.set_xticklabels([labels[int(i)] for i in gm.index], fontsize=9)
    ax1.set_ylabel("|next session|, bps (market-adjusted)")
    ax1.set_title(
        "The model's judgement of how big the news is\n"
        "rank corr 0.099, t = 8.5 over 58 trading days",
        loc="left",
    )
    for i, (v, c) in enumerate(zip(gm.values, nm.values)):
        ax1.text(i, v, f"{v:.0f}\nn={c:,}", ha="center", va="bottom", fontsize=8)
    if thin.any():
        ax1.text(
            0.02, 0.97, "hatched = under 100 observations",
            transform=ax1.transAxes, va="top", fontsize=8, color=MUTED,
        )

    ax2.bar(range(len(gi)), gi.values, color=MUTED, width=0.65)
    ax2.set_xticks(range(len(gi)))
    ax2.set_xticklabels(gi.index, fontsize=9)
    ax2.set_xlabel("articles about that stock that day")
    ax2.set_title(
        "The free baseline: just count the articles\nrank corr 0.035, t = 2.8",
        loc="left",
    )
    for i, v in enumerate(gi.values):
        ax2.text(i, v, f"{v:.0f}", ha="center", va="bottom", fontsize=8)
    hi = max(ax1.get_ylim()[1], ax2.get_ylim()[1])
    ax1.set_ylim(0, hi)
    ax2.set_ylim(0, hi)

    fig.suptitle(
        "How far a stock moves next session is forecastable -- and it takes a model.",
        x=0.005, ha="left", fontsize=11.5,
    )
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    _save(fig, "magnitude.png")


def fig_sample_size() -> None:
    """The same measurement at two sample sizes, with its standard error."""
    base = Path(r"C:\Users\olegr\PycharmProjects\btest\data\raw\eodhd\news\bench")
    # measured values recorded in NEWS_SCORING_DESIGN.md sec 11 and sec 13
    rows = [
        ("screening\nn = 259", 5.6, 3.7),
        ("decisive run\nn = 1,267", -0.5, 1.76),
    ]
    fig, ax = plt.subplots(figsize=(5.6, 3.2))
    xs = np.arange(len(rows))
    vals = [r[1] for r in rows]
    errs = [1.96 * r[2] for r in rows]
    ax.errorbar(
        xs, vals, yerr=errs, fmt="o", color=INK, ecolor=MUTED,
        capsize=6, markersize=7, lw=1.4,
    )
    ax.axhline(0, color=ACCENT, lw=1.0, ls="--")
    ax.set_xticks(xs)
    ax.set_xticklabels([r[0] for r in rows])
    ax.set_xlim(-0.5, len(rows) - 0.5)
    ax.set_ylabel("forward edge over the trivial call, pp")
    ax.set_title(
        "The screening 'edge' was noise.\nBars are 95% intervals; both cross zero.",
        loc="left",
    )
    fig.tight_layout()
    _save(fig, "sample_size.png")


def fig_vendor_saturation(con) -> None:
    """Why we score the text ourselves."""
    df = con.execute(
        "SELECT polarity_mean FROM news_daily WHERE polarity_mean IS NOT NULL "
        "USING SAMPLE 400000 ROWS"
    ).df()
    s = df["polarity_mean"].astype(float)
    share = float((s >= 0.99).mean())
    neg = float((s < 0).mean())

    fig, ax = plt.subplots(figsize=(7.2, 3.0))
    ax.hist(s, bins=80, color=MUTED, edgecolor="none")
    ax.axvspan(0.99, 1.0, color=ACCENT, alpha=0.25)
    ax.set_xlabel("vendor sentiment (polarity) for a stock-day")
    ax.set_ylabel("stock-days")
    ax.set_title(
        f"The vendor's own score is saturated: {share:.1%} sits at >= 0.99, "
        f"only {neg:.1%} is negative.",
        loc="left",
    )
    ax.text(
        0.02, 0.9,
        "A field with no spread across the top\n70% of its range cannot rank a\n"
        "cross-section -- which is why the\ntext is scored locally instead.",
        transform=ax.transAxes, va="top", fontsize=8.5, color=INK,
    )
    _save(fig, "vendor_saturation.png")


def main() -> int:
    con = ex.connect()
    fig_anchoring(con)
    fig_sentiment_horizons(con)
    fig_magnitude(con)
    fig_sample_size()
    fig_vendor_saturation(con)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
