from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from positioning import panel_decomposition as pdc  # noqa: E402


def _ar1_panel(n_dates: int = 200, names: int = 80, rho: float = 0.8, seed: int = 0, common: float = 0.0, block_rho: float | None = None):
    """A panel of AR(1) names; the first half of the names shares a common residual factor with loading ``common``."""
    rng = np.random.default_rng(seed)
    x = np.zeros((n_dates, names))
    factor = rng.normal(size=n_dates)
    means = rng.normal(size=names)
    for t in range(1, n_dates):
        shock = rng.normal(size=names)
        shock[: names // 2] += common * factor[t]
        r = np.full(names, rho)
        if block_rho is not None:
            r[: names // 2] = block_rho
        x[t] = means + r * (x[t - 1] - means) + shock
    dates = pd.bdate_range("2015-01-02", periods=n_dates, freq="W-FRI")
    panel = pd.DataFrame(x, index=dates, columns=[f"N{i}" for i in range(names)])
    blocks = pd.DataFrame({"block": ["A"] * (names // 2) + ["B"] * (names - names // 2)}, index=panel.columns)
    return panel, blocks, factor


def test_fixed_effect_ar1_recovers_the_persistence() -> None:
    panel, _, _ = _ar1_panel(rho=0.7)
    rho, means = pdc.fixed_effect_ar1(panel.to_numpy())
    assert rho == pytest.approx(0.7, abs=0.05) and len(means) == 80


def test_block_ar1_gives_each_large_block_its_own_rho_and_the_pool_to_small_ones() -> None:
    panel, blocks, _ = _ar1_panel(rho=0.9, block_rho=0.3)
    rho, _, rhos = pdc.block_ar1(panel.to_numpy(), blocks["block"].tolist(), min_block=25)
    assert rhos["A"] == pytest.approx(0.3, abs=0.08) and rhos["B"] == pytest.approx(0.9, abs=0.05)
    assert rho[0] == pytest.approx(rhos["A"]) and rho[-1] == pytest.approx(rhos["B"])
    labels = ["A"] * 10 + ["B"] * 70
    rho2, _, rhos2 = pdc.block_ar1(panel.to_numpy(), labels, min_block=25)
    assert "A" not in rhos2 and rho2[0] == pytest.approx(rhos2[pdc.POOL])  # too small: the pool's rho


def test_decomposition_splits_the_residual_into_a_common_and_an_idiosyncratic_part() -> None:
    panel, blocks, factor = _ar1_panel(common=2.0, seed=1)
    fit = pdc.fit_window(panel.to_numpy()[:150], blocks["block"].tolist(), k=2, min_block=25)
    assert set(fit.bases) == {"A", "B"} and fit.bases["A"][1].shape == (40, 2)
    out = pdc.decompose_rows(fit, panel.to_numpy()[150], panel.to_numpy()[151], None)
    np.testing.assert_allclose(out["c"] + out["eps"], out["e"], atol=1e-12)
    idx, u, _ = fit.bases["A"]
    assert abs(float(out["eps"][idx] @ u[:, 0])) < 1e-9  # the surprise is orthogonal to the basis
    # the common component of block A follows the planted factor across dates
    cs, es = [], []
    for t in range(151, 200):
        o = pdc.decompose_rows(fit, panel.to_numpy()[t - 1], panel.to_numpy()[t], None)
        cs.append(o["c"][idx].mean())
        es.append(o["eps"][idx].mean())
    assert abs(np.corrcoef(cs, factor[151:200])[0, 1]) > 0.8 and np.std(es) < 0.25 * np.std(cs)  # the factor lives in c, not in eps


def test_rolling_decomposition_is_causal_and_forecasts_an_ar1_panel() -> None:
    panel, blocks, _ = _ar1_panel(rho=0.8, seed=2)
    out = pdc.rolling_decomposition(panel, window=60, step=10, blocks=blocks, k=2, min_block=25)
    assert out["date"].min() == panel.index[60] and set(out["block"]) == {"A", "B"}
    assert len(out) == (200 - 60) * 80
    r2 = pdc.forecast_r2(out)
    assert (r2["r2_g0"] > 0.4).all() and (r2["r2_ba"] > 0.4).all()
    # a name missing on a date yields no row for that date and the next
    holed = panel.copy()
    holed.iloc[100, 3] = np.nan
    out2 = pdc.rolling_decomposition(holed, window=60, step=10, blocks=blocks, k=2, min_block=25)
    gone = out2[(out2["name"] == "N3") & out2["date"].isin(panel.index[[100, 101]])]
    assert gone.empty and len(out2) == len(out) - 2


def test_own_history_zscore_and_halves() -> None:
    panel, _, _ = _ar1_panel(n_dates=40, names=3)
    z = pdc.own_history_zscore(panel, halflife=6, min_history=6)
    assert z.shape == panel.shape and z.iloc[:6].isna().all().all() and np.isfinite(z.iloc[-1]).all()
    frame = pd.DataFrame({"date": pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-04"])})
    a, b = pdc.halves(frame)
    assert len(a) == 2 and len(b) == 2


def test_ic_family_reads_a_planted_bearish_surprise() -> None:
    rng = np.random.default_rng(3)
    dates = pd.bdate_range("2022-01-07", periods=50, freq="W-FRI")
    rows = []
    for d in dates:
        x = rng.normal(size=200)
        for i in range(200):
            rows.append({"date": d, "eps": x[i], "c": rng.normal(), "reversal_21d": rng.normal(), "momentum_12_1": rng.normal(),
                         "level": rng.normal(), "ret_adj_10": -0.4 * x[i] + rng.normal(), "ret_adj_21": rng.normal()})
    table = pdc.ic_family(pd.DataFrame(rows), features=("eps", "c"), min_names=50, nw_lag=lambda h: 2)
    assert len(table) == 12
    hit = table[(table["feature"] == "eps") & (table["cleaning"] == "factors") & (table["horizon"] == 10)].iloc[0]
    assert hit["mean_ic"] < -0.25 and hit["significant"] and hit["sign_as_spec"]
    miss = table[(table["feature"] == "c") & (table["cleaning"] == "none") & (table["horizon"] == 21)].iloc[0]
    assert not miss["significant"]
