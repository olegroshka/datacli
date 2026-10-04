from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import register_long_leg as ll  # noqa: E402


def test_rebalance_dates_take_the_first_trading_day_of_each_week_and_month() -> None:
    idx = pd.DatetimeIndex(pd.bdate_range("2026-09-28", "2026-10-16")).drop(pd.Timestamp("2026-10-05"))  # Monday missing
    weekly = ll.rebalance_dates(idx, "W")
    assert list(weekly) == [pd.Timestamp("2026-09-28"), pd.Timestamp("2026-10-06"), pd.Timestamp("2026-10-12")]
    assert list(ll.rebalance_dates(idx, "M")) == [pd.Timestamp("2026-09-28"), pd.Timestamp("2026-10-01")]
    with pytest.raises(ValueError):
        ll.rebalance_dates(idx, "Q")


def test_long_leg_weights_take_the_bottom_ranks_cap_and_hold_between_rebalances() -> None:
    idx = pd.DatetimeIndex(pd.bdate_range("2026-09-28", periods=10))
    names = [f"N{i}" for i in range(10)]
    ranks = np.linspace(-0.5, 0.5, 10)  # N0 the most losing short, N9 the most profitable
    signal = pd.DataFrame([ranks] * 10, index=idx, columns=names)
    signal.loc[idx[5], :] = ranks[::-1]  # the second week flips the ranks
    w = ll.long_leg_weights(signal, edge=0.2, cap=0.5, freq="W")
    first = w.loc[idx[0]]
    assert set(first[first > 0].index) == {"N0", "N1", "N2"}  # -rank >= 0.2: 0.5, 0.389, 0.278
    assert first.sum() == pytest.approx(1.0) and first["N0"] > first["N2"]
    assert w.loc[idx[4]].equals(first)  # held through the week
    second = w.loc[idx[5]]
    assert set(second[second > 0].index) == {"N7", "N8", "N9"} and second.sum() == pytest.approx(1.0)
    capped = ll.long_leg_weights(signal, edge=0.2, cap=0.35, freq="W").loc[idx[0]]
    assert capped.max() <= 0.35 + 1e-9 and capped.sum() == pytest.approx(1.0)
    none = ll.long_leg_weights(signal.where(signal > 0.4), edge=0.2, freq="W")  # nobody at or below -0.2
    assert float(none.abs().sum().sum()) == 0.0


def test_portfolio_return_uses_the_previous_close_weights_and_zero_for_missing_bars() -> None:
    idx = pd.DatetimeIndex(pd.bdate_range("2026-09-28", periods=3))
    weights = pd.DataFrame({"A": [1.0, 1.0, 1.0], "B": [0.0, 0.0, 0.0]}, index=idx)
    returns = pd.DataFrame({"A": [np.nan, 0.02, np.nan], "B": [np.nan, -0.5, 0.1]}, index=idx)
    r = ll.portfolio_return(weights, returns)
    assert r.tolist() == pytest.approx([0.0, 0.02, 0.0])
    ew = ll.equal_weight(pd.DataFrame({"A": [True, True, False], "B": [True, False, False]}, index=idx))
    assert ew.loc[idx[0]].tolist() == [0.5, 0.5] and ew.loc[idx[1]].tolist() == [1.0, 0.0] and ew.loc[idx[2]].sum() == 0.0


def test_regress_recovers_alpha_and_beta_and_factor_spreads_have_the_right_sign() -> None:
    rng = np.random.default_rng(7)
    n = 2000
    idx = pd.DatetimeIndex(pd.bdate_range("2018-01-01", periods=n))
    mkt = pd.Series(rng.normal(0, 0.01, n), index=idx)
    y = 0.0004 + 1.5 * mkt + pd.Series(rng.normal(0, 0.005, n), index=idx)
    r = ll.regress(y, mkt.to_frame("mkt"))
    assert r.betas["mkt"] == pytest.approx(1.5, abs=0.05) and r.alpha_annual == pytest.approx(0.0004 * 252, abs=0.03)
    assert r.alpha_t > 3 and r.beta_t["mkt"] > 50 and r.r2 > 0.7 and r.n == n
    alone = ll.regress(y)
    assert alone.betas == {} and alone.alpha_annual == pytest.approx(float(y.mean() * 252))
    # a characteristic that is the next day's return itself: top minus bottom tercile must be positive
    names = [f"N{i}" for i in range(60)]
    rets = pd.DataFrame(rng.normal(0, 0.01, (n, 60)), index=idx, columns=names)
    char = rets.shift(-1)
    spread = ll.tercile_spread_weights(char, dates=idx, freq="M")
    assert spread.loc[idx[0]].sum() == pytest.approx(0.0) and (spread.loc[idx[0]] > 0).sum() == 20
    f = ll.factor_returns(rets, adv=np.exp(char), momentum=char, reversal=char, universe=rets.notna())
    assert set(f.columns) == {"mkt", "size", "mom", "rev"}
    assert ll.annualised(f["mom"])[0] > 0 and ll.annualised(f["size"])[0] < 0  # size is small minus big: the big (high adv) win here
    assert ll.regression_table([("y", r)]).loc[0, "b_mkt"] == pytest.approx(1.5, abs=0.05)
