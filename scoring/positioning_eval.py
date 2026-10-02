"""Does short-sale positioning change how big the move after material news is?

The scoring result to build on (``scoring.panel_eval``, README): the model's
materiality judgement orders the *size* of the next session's market-adjusted
move (per-day rank correlation about 0.10, t about 8.5), while direction is
dead. FINRA's daily short volume adds a positioning variable. This module asks
three questions, all as per-day cross-sectional statistics with a t over days:

1. **Alone:** does the trailing short ratio order ``|next move|`` at all?
2. **Interaction:** is materiality's ordering stronger among heavily shorted
   names than among lightly shorted ones (per-day correlation inside short-ratio
   terciles, high minus low)?
3. **Jointly:** a per-day rank regression of ``|move|`` on materiality, short
   ratio and their product, averaged over days (Fama-MacBeth), with Newey-West
   lags for overlapping horizons.

Everything cross-sectional is grouped by the trading day; the positioning
feature is the ``*_known`` column from :mod:`finra.panel`, observable at the
close the decision is taken at. Pure functions over DataFrames; the driver is
``scripts/finra_materiality_eval.py``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from scoring.panel_eval import _daily_rank_corr, _lags_for, _spearman_int, _t_stat

MIN_ROWS_PER_DAY = 30


def _abs_ret(d: pd.DataFrame, horizon: str) -> pd.Series:
    return d[horizon].abs()


def positioning_alone(
    d: pd.DataFrame, *, field: str, horizon: str = "f1_ex"
) -> dict[str, Any]:
    """Per-day Spearman of ``field`` against ``|horizon|``, t over days."""
    x = d.dropna(subset=[field, horizon])
    return _daily_rank_corr(x, field, horizon)


def interaction_by_tercile(
    d: pd.DataFrame,
    *,
    mat_field: str = "mat_max",
    pos_field: str,
    horizon: str = "f1_ex",
    min_rows: int = MIN_ROWS_PER_DAY,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Materiality's per-day rank correlation with ``|move|`` inside positioning terciles.

    Terciles are formed **within each trading day** on ``pos_field``. The
    statistic is the per-day difference (high tercile minus low tercile) of the
    Spearman between materiality and ``|move|``, with a t over days.
    """
    x = d.dropna(subset=[mat_field, pos_field, horizon]).copy()
    x["abs_ret"] = _abs_ret(x, horizon)
    rows = []
    diffs = []
    key = "trade_date" if "trade_date" in x.columns else "date"
    for day, g in x.groupby(key):
        if len(g) < min_rows or g[mat_field].nunique() < 2:
            continue
        g = g.copy()
        g["tercile"] = pd.qcut(
            g[pos_field].rank(method="first"), 3, labels=["low", "mid", "high"]
        )
        corr = {}
        for name, h in g.groupby("tercile", observed=True):
            if len(h) < 10 or h[mat_field].nunique() < 2:
                continue
            c = _spearman_int(h[mat_field], h["abs_ret"])
            if c == c:
                corr[name] = c
                rows.append(
                    {
                        "trade_date": day,
                        "tercile": name,
                        "corr": c,
                        "n": len(h),
                        "mean_abs_bps": float(h["abs_ret"].mean()) * 10000,
                    }
                )
        if "high" in corr and "low" in corr:
            diffs.append(corr["high"] - corr["low"])
    per = pd.DataFrame(rows)
    if per.empty:
        return per, {}
    table = (
        per.groupby("tercile", observed=True)
        .agg(
            n_days=("corr", "size"),
            corr_mean=("corr", "mean"),
            mean_abs_bps=("mean_abs_bps", "mean"),
        )
        .reindex(["low", "mid", "high"])
        .reset_index()
    )
    table["corr_mean"] = table["corr_mean"].round(4)
    table["mean_abs_bps"] = table["mean_abs_bps"].round(1)
    stats: dict[str, Any] = {"horizon": horizon, "pos_field": pos_field}
    if len(diffs) >= 5:
        s = pd.Series(diffs)
        t = _t_stat(s, lags=_lags_for(horizon))
        stats.update(
            {
                "diff_n_days": t.get("n_days"),
                "diff_mean": round(float(s.mean()), 4),
                "diff_t": t.get("t"),
                "diff_p": t.get("p"),
            }
        )
    return table, stats


def conditional_magnitude(
    d: pd.DataFrame,
    *,
    mat_field: str = "mat_max",
    pos_field: str,
    horizon: str = "f1_ex",
    material_at_least: float = 2.0,
    min_rows: int = MIN_ROWS_PER_DAY,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Mean ``|move|`` by materiality level and positioning tercile, plus the
    per-day spread (high minus low tercile) among material names, t over days."""
    x = d.dropna(subset=[mat_field, pos_field, horizon]).copy()
    x["abs_ret"] = _abs_ret(x, horizon)
    key = "trade_date" if "trade_date" in x.columns else "date"
    parts = []
    spreads = []
    for day, g in x.groupby(key):
        if len(g) < min_rows:
            continue
        g = g.copy()
        g["tercile"] = pd.qcut(
            g[pos_field].rank(method="first"), 3, labels=["low", "mid", "high"]
        )
        parts.append(g)
        m = g[g[mat_field] >= material_at_least]
        hi, lo = (
            m[m["tercile"] == "high"]["abs_ret"],
            m[m["tercile"] == "low"]["abs_ret"],
        )
        if len(hi) >= 3 and len(lo) >= 3:
            spreads.append(float(hi.mean() - lo.mean()))
    if not parts:
        return pd.DataFrame(), {}
    allx = pd.concat(parts)
    cells = (
        allx.groupby([mat_field, "tercile"], observed=True)["abs_ret"]
        .agg(n_rows="size", mean_abs_bps=lambda s: round(float(s.mean()) * 10000, 1))
        .reset_index()
    )
    table = (
        cells.pivot(index=mat_field, columns="tercile", values="mean_abs_bps")
        .reindex(columns=["low", "mid", "high"])
        .reset_index()
    )
    counts = (
        cells.pivot(index=mat_field, columns="tercile", values="n_rows")
        .reindex(columns=["low", "mid", "high"])
        .fillna(0)
        .astype(int)
        .reset_index()
    )
    # the row counts travel with the means: a 600 bps cell built on a dozen
    # rows must never be read as a result
    table = table.merge(counts, on=mat_field, suffixes=("", "_n"))
    stats: dict[str, Any] = {
        "horizon": horizon,
        "pos_field": pos_field,
        "material_at_least": material_at_least,
    }
    if len(spreads) >= 5:
        stats.update(
            {
                f"spread_{k}": v
                for k, v in _t_stat(pd.Series(spreads), lags=_lags_for(horizon)).items()
            }
        )
    return table, stats


def fama_macbeth_ranks(
    d: pd.DataFrame,
    *,
    mat_field: str = "mat_max",
    pos_field: str,
    horizon: str = "f1_ex",
    min_rows: int = MIN_ROWS_PER_DAY,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Per-day OLS of rank(|move|) on rank(mat), rank(pos) and their product.

    Ranks are within-day percentiles centred at zero, so the coefficients are
    comparable across days and the product term reads as an interaction. The
    per-day coefficients are averaged and given a t over days (Newey-West for
    overlapping horizons).
    """
    x = d.dropna(subset=[mat_field, pos_field, horizon]).copy()
    x["abs_ret"] = _abs_ret(x, horizon)
    key = "trade_date" if "trade_date" in x.columns else "date"
    coefs = []
    for day, g in x.groupby(key):
        if (
            len(g) < min_rows
            or g[mat_field].nunique() < 2
            or g[pos_field].nunique() < 2
        ):
            continue
        y = g["abs_ret"].rank(pct=True) - 0.5
        m = g[mat_field].rank(pct=True) - 0.5
        p = g[pos_field].rank(pct=True) - 0.5
        X = np.column_stack([np.ones(len(g)), m, p, m * p])
        beta, *_ = np.linalg.lstsq(X, y.to_numpy(), rcond=None)
        coefs.append(
            {
                "trade_date": day,
                "const": beta[0],
                "mat": beta[1],
                "pos": beta[2],
                "mat_x_pos": beta[3],
            }
        )
    per = pd.DataFrame(coefs)
    if len(per) < 5:
        return per, {"n_days": int(len(per))}
    lags = _lags_for(horizon)
    rows = []
    for name in ("mat", "pos", "mat_x_pos"):
        t = _t_stat(per[name], lags=lags)
        rows.append(
            {
                "term": name,
                "n_days": t["n_days"],
                "coef": round(float(per[name].mean()), 4),
                "t": t["t"],
                "p": t["p"],
            }
        )
    return pd.DataFrame(rows), {
        "horizon": horizon,
        "pos_field": pos_field,
        "n_days": int(len(per)),
        "nw_lags": lags,
    }


def bonferroni_note(n_tests: int, alpha: float = 0.05) -> str:
    return (
        f"{n_tests} tests in this family: a result needs p < {alpha / n_tests:.4f} "
        f"(Bonferroni at {alpha}) to count as significant"
    )
