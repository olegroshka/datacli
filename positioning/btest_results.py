"""Read a btest run's outputs and overlay the real borrow rates (WP15, second iteration).

btest charges one flat annual borrow rate on the short notional. The
Interactive Brokers snapshot (WP6) gives a rate per name, so the overlay
replaces the flat charge by the per-name one on the weights btest stored:

    drag_t = sum_i max(-w_it, 0) * (fee_i - flat) / periods_per_year

and the adjusted daily return is ``returns_t - drag_t``. Names without a
rate keep the flat one. One snapshot stands for the whole history (recorded
assumption: today's rates applied backwards, until the daily snapshots
accumulate), so the overlay is a level of cost, not its history.

``summarise`` gives the headline metrics of a daily return series the way
EVAL-001's WP15 section reports them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PERIODS_PER_YEAR = 252


@dataclass(frozen=True)
class Summary:
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    years: float


def summarise(returns: pd.Series, *, periods_per_year: int = PERIODS_PER_YEAR) -> Summary:
    r = returns.dropna().astype(float)
    if r.empty:
        return Summary(float("nan"), float("nan"), float("nan"), float("nan"), 0.0)
    equity = (1.0 + r).cumprod()
    years = len(r) / periods_per_year
    total = float(equity.iloc[-1] - 1.0)
    cagr = float(equity.iloc[-1] ** (1.0 / years) - 1.0) if years > 0 and equity.iloc[-1] > 0 else float("nan")
    sd = float(r.std(ddof=1))
    sharpe = float(r.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else float("nan")
    drawdown = float((equity / equity.cummax() - 1.0).min())
    return Summary(total_return=total, cagr=cagr, sharpe=sharpe, max_drawdown=drawdown, years=years)


def borrow_drag(
    weights: pd.DataFrame,
    rates: pd.Series,
    *,
    flat_rate: float,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> pd.Series:
    """Per-period extra drag of the per-name rates over the flat one, on the short weights.

    ``weights`` is btest's wide frame (date x ticker, fractions of equity,
    negative = short); ``rates`` maps ticker to an annual fraction. Missing
    tickers keep ``flat_rate`` (zero extra drag).
    """
    short = (-weights).clip(lower=0.0).fillna(0.0)
    extra = rates.reindex(short.columns).fillna(flat_rate) - flat_rate
    return short.mul(extra, axis=1).sum(axis=1) / periods_per_year


def overlay(
    returns: pd.Series,
    weights: pd.DataFrame,
    rates: pd.Series,
    *,
    flat_rate: float,
    periods_per_year: int = PERIODS_PER_YEAR,
) -> pd.Series:
    """The daily returns with the flat borrow charge replaced by the per-name one."""
    drag = borrow_drag(weights, rates, flat_rate=flat_rate, periods_per_year=periods_per_year)
    return returns - drag.reindex(returns.index).fillna(0.0)


def short_rate_profile(weights: pd.DataFrame, rates: pd.Series) -> pd.Series:
    """Mean annual rate paid on the short book over time (weight-averaged), ignoring names without a rate."""
    short = (-weights).clip(lower=0.0).fillna(0.0)
    known = short.loc[:, short.columns.isin(rates.index)]
    weighted = known.mul(rates.reindex(known.columns), axis=1).sum(axis=1)
    return weighted / known.sum(axis=1).replace(0.0, np.nan)


def load_run(directory: Path) -> tuple[pd.Series, pd.DataFrame, dict]:
    """``(returns, weights, summary)`` from a btest output directory."""
    directory = Path(directory)
    returns = pd.read_parquet(directory / "returns.parquet")
    if isinstance(returns, pd.DataFrame):
        column = "returns" if "returns" in returns.columns else returns.columns[0]
        returns = returns[column]
    weights = pd.read_parquet(directory / "weights.parquet")
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    return returns.astype(float), weights, summary


def load_rates(path: Path) -> pd.Series:
    frame = pd.read_parquet(path)
    return frame.set_index("ticker")["fee_rate"].astype(float)
