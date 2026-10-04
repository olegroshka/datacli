"""The long leg alone: the losing shorts against the market, size, momentum and reversal (DD-003 WP22).

WP21's sized run of the funds' short profit ordering lost before costs, and
its decomposition showed the long leg (the names whose disclosed shorts are
losing, the bottom of the profit ranks) beating the median name by 8 to 9
percent a year while the shorted names in the money rose as much. This
module isolates that leg and asks what it is: its daily gross return against
the equal-weight register universe, against the equal-weight book of every
name with a visible holder (the crowded group it is drawn from), and against
factor portfolios built inside the same universe (the market, size by dollar
volume, 12-1 momentum, 21-day reversal), with Newey-West statistics on the
intercept.

The leg: on the first trading day of each ISO week (or month), the names
whose demeaned profit rank is at or below ``-edge`` (the bottom 30 percent at
0.2), weighted by the negated rank, summing to one, capped per name; held
until the next rebalance. The weights set at the close of day ``d`` earn day
``d + 1``; the exported signal is already shown from the day after it is
marked, so no further delay is applied. The same weights go to btest
(``TargetWeights``) for the cost-realism run. Daily returns are clipped at
plus or minus 50 percent (the lanes carry a few bad bars), and a held name
without a bar on a pooled trading day (its market's holiday) earns zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

EDGE = 0.2
CAP = 0.04
RETURN_CLIP = 0.5
ADV_WINDOW = 63
NW_LAG = 5
PERIODS_PER_YEAR = 252


def rebalance_dates(index: pd.DatetimeIndex, freq: str = "W") -> pd.DatetimeIndex:
    """The first trading day of each ISO week (``W``) or calendar month (``M``) in ``index``."""
    idx = pd.DatetimeIndex(index).sort_values().unique()
    if freq == "W":
        iso = idx.isocalendar()
        key = iso["year"].astype(str) + "-" + iso["week"].astype(str)
    elif freq == "M":
        key = pd.Series(idx.strftime("%Y-%m"), index=idx)
    else:
        raise ValueError(f"freq must be W or M, got {freq!r}")
    first = pd.Series(idx, index=idx).groupby(key.to_numpy()).min()
    return pd.DatetimeIndex(sorted(first))


def _cap_and_normalise(weights: pd.Series, cap: float) -> pd.Series:
    """Weights summing to one with no name above ``cap``: the excess of a capped name goes to the others in proportion."""
    w = weights / weights.sum()
    if cap * len(w) <= 1.0:  # the book cannot reach one: every name sits at the cap
        return pd.Series(cap, index=w.index)
    capped = pd.Series(False, index=w.index)
    for _ in range(len(w)):
        over = (w > cap + 1e-12) & ~capped
        if not over.any():
            break
        capped |= over
        w[capped] = cap
        free = ~capped
        w[free] = w[free] / w[free].sum() * (1.0 - cap * float(capped.sum()))
    return w


def long_leg_weights(signal: pd.DataFrame, *, edge: float = EDGE, cap: float = CAP, freq: str = "W") -> pd.DataFrame:
    """Daily weights of the bottom-of-the-ranks book, set on the rebalance days and held between them."""
    index = pd.DatetimeIndex(signal.index).sort_values()
    rows = {}
    for day in rebalance_dates(index, freq):
        row = pd.Series(0.0, index=signal.columns)
        score = -signal.loc[day].dropna()
        chosen = score[score >= edge]
        if not chosen.empty:
            row[chosen.index] = _cap_and_normalise(chosen, cap)
        rows[day] = row
    wide = pd.DataFrame(rows).T  # a full row per rebalance day: a name not chosen is zero, not carried
    return wide.reindex(index).ffill().fillna(0.0)


def daily_returns(prices: pd.DataFrame, *, clip: float = RETURN_CLIP) -> pd.DataFrame:
    """Close-to-close returns from the long price panel (``date``, ``ticker``, ``close``), clipped."""
    frame = prices[["date", "ticker", "close"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is not None:
        frame["date"] = frame["date"].dt.tz_localize(None)
    close = frame.pivot(index="date", columns="ticker", values="close").sort_index()
    return close.pct_change(fill_method=None).clip(-clip, clip)


def portfolio_return(weights: pd.DataFrame, returns: pd.DataFrame) -> pd.Series:
    """The daily return of weights set at the previous close; a held name without a bar earns zero."""
    held = weights.shift(1).fillna(0.0)
    r = returns.reindex(index=held.index, columns=held.columns).fillna(0.0)
    return (held * r).sum(axis=1)


def equal_weight(mask: pd.DataFrame) -> pd.DataFrame:
    """Equal weights over the names flagged on each day (zero rows where none is)."""
    m = mask.astype(float)
    n = m.sum(axis=1).replace(0.0, np.nan)
    return m.div(n, axis=0).fillna(0.0)


def dollar_volume_adv(prices: pd.DataFrame, *, window: int = ADV_WINDOW) -> pd.DataFrame:
    frame = prices[["date", "ticker", "close", "volume"]].copy()
    frame["date"] = pd.to_datetime(frame["date"])
    if frame["date"].dt.tz is not None:
        frame["date"] = frame["date"].dt.tz_localize(None)
    frame["dv"] = frame["close"] * frame["volume"]
    wide = frame.pivot(index="date", columns="ticker", values="dv").sort_index()
    return wide.rolling(window, min_periods=window // 2).median()


def tercile_spread_weights(characteristic: pd.DataFrame, *, dates: pd.DatetimeIndex, freq: str = "M") -> pd.DataFrame:
    """Top tercile minus bottom tercile, equal weight, set on the rebalance days of ``freq`` and held."""
    rows = {}
    for day in rebalance_dates(dates, freq):
        if day not in characteristic.index:
            continue
        x = characteristic.loc[day].dropna()
        if len(x) < 30:
            continue
        lo, hi = x.quantile([1 / 3, 2 / 3])
        top, bottom = x[x >= hi].index, x[x <= lo].index
        w = pd.Series(0.0, index=x.index)
        w[top] = 1.0 / len(top)
        w[bottom] = -1.0 / len(bottom)
        rows[day] = w
    wide = pd.DataFrame(rows).T.reindex(columns=characteristic.columns)
    return wide.reindex(dates).ffill().fillna(0.0)


def factor_returns(
    returns: pd.DataFrame,
    *,
    adv: pd.DataFrame,
    momentum: pd.DataFrame,
    reversal: pd.DataFrame,
    universe: pd.DataFrame,
    freq: str = "M",
) -> pd.DataFrame:
    """``mkt`` (equal-weight universe), ``size`` (small minus big by dollar volume), ``mom`` (12-1 winners minus losers),
    ``rev`` (21-day winners minus losers), inside the names flagged in ``universe`` (a boolean daily frame)."""
    dates = pd.DatetimeIndex(returns.index)
    in_universe = universe.reindex(index=dates, columns=returns.columns).fillna(False).astype(bool)
    mkt = portfolio_return(equal_weight(in_universe), returns)
    size = -portfolio_return(tercile_spread_weights(np.log(adv.where(in_universe & (adv > 0))), dates=dates, freq=freq), returns)
    mom = portfolio_return(tercile_spread_weights(momentum.where(in_universe), dates=dates, freq=freq), returns)
    rev = portfolio_return(tercile_spread_weights(reversal.where(in_universe), dates=dates, freq=freq), returns)
    return pd.DataFrame({"mkt": mkt, "size": size, "mom": mom, "rev": rev})


@dataclass(frozen=True)
class Regression:
    alpha_annual: float
    alpha_t: float
    betas: dict[str, float]
    beta_t: dict[str, float]
    r2: float
    n: int


def regress(y: pd.Series, x: pd.DataFrame | None = None, *, lag: int = NW_LAG) -> Regression:
    """OLS of ``y`` on a constant and ``x`` with Newey-West (Bartlett, ``lag``) standard errors."""
    frame = pd.concat([y.rename("_y"), x], axis=1) if x is not None else y.rename("_y").to_frame()
    frame = frame.dropna()
    names = [c for c in frame.columns if c != "_y"]
    yy = frame["_y"].to_numpy(dtype=float)
    xx = np.column_stack([np.ones(len(frame)), *(frame[c].to_numpy(dtype=float) for c in names)])
    n, k = xx.shape
    xtx_inv = np.linalg.inv(xx.T @ xx)
    beta = xtx_inv @ xx.T @ yy
    e = yy - xx @ beta
    s = (xx * e[:, None]).T @ (xx * e[:, None])
    for j in range(1, min(lag, n - 1) + 1):
        w = 1.0 - j / (lag + 1)
        g = (xx[j:] * e[j:, None]).T @ (xx[:-j] * e[:-j, None])
        s += w * (g + g.T)
    cov = xtx_inv @ s @ xtx_inv
    se = np.sqrt(np.clip(np.diag(cov), 1e-30, None))
    t = beta / se
    ss_tot = float(((yy - yy.mean()) ** 2).sum())
    r2 = 1.0 - float((e ** 2).sum()) / ss_tot if ss_tot > 0 else float("nan")
    return Regression(
        alpha_annual=float(beta[0] * PERIODS_PER_YEAR),
        alpha_t=float(t[0]),
        betas={name: float(b) for name, b in zip(names, beta[1:])},
        beta_t={name: float(v) for name, v in zip(names, t[1:])},
        r2=r2,
        n=int(n),
    )


def annualised(series: pd.Series) -> tuple[float, float]:
    """``(mean return a year, Sharpe)`` of a daily series."""
    s = series.dropna()
    if s.empty or s.std() == 0:
        return float("nan"), float("nan")
    return float(s.mean() * PERIODS_PER_YEAR), float(s.mean() / s.std() * np.sqrt(PERIODS_PER_YEAR))


def regression_table(rows: Sequence[tuple[str, Regression]]) -> pd.DataFrame:
    out = []
    for label, r in rows:
        row = {"series": label, "alpha a year": round(r.alpha_annual * 100, 2), "t": round(r.alpha_t, 2), "R2": round(r.r2, 3), "days": r.n}
        for name in r.betas:
            row[f"b_{name}"] = round(r.betas[name], 3)
            row[f"t_{name}"] = round(r.beta_t[name], 2)
        out.append(row)
    return pd.DataFrame(out)
