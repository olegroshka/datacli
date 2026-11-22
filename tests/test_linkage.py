"""linkage: moments, returns, betas, abnormal auctions, co-spikes, store, datasets, CLI -- offline.

Every computation that feeds a stored value has a truncation test: removing
the data after a date leaves that date's value unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from linkage import (  # noqa: E402
    abnormal,
    betas,
    cli,
    cospike,
    datasets,
    moments,
    qc,
    returns,
    views,
)
from linkage.calendar import month_end_sessions, quarter_start  # noqa: E402
from linkage.config import (  # noqa: E402
    ENV_ROOT,
    AbnormalParams,
    BetaParams,
    CospikeParams,
    Params,
    digest,
    params_from,
)
from linkage.store import NEW, RESTATED, UNCHANGED, PartitionStore  # noqa: E402

duckdb = pytest.importorskip("duckdb")

SESSIONS = pd.bdate_range("2023-01-02", "2024-06-28")


# --------------------------------------------------------------------------- #
# synthetic market
# --------------------------------------------------------------------------- #
def _market(seed: int = 7) -> dict[str, np.ndarray]:
    """Daily returns: a market, a sector ETF on top of it, and stocks with known loadings."""
    rng = np.random.default_rng(seed)
    n = len(SESSIONS)
    mkt = rng.normal(0.0004, 0.01, n)
    sector = rng.normal(0.0, 0.008, n)
    out = {
        "SPY": mkt,
        "XLK": mkt + sector,
        "TLT": rng.normal(0, 0.007, n),
        "AAA": 1.2 * mkt + 1.5 * sector + rng.normal(0, 0.004, n),  # a tech name
        "BBB": 0.8 * mkt + rng.normal(0, 0.006, n),  # not tech
        "PENNY": 1.0 * mkt + rng.normal(0, 0.02, n),
    }
    return out


def _prices(
    rets: dict[str, np.ndarray], *, start_price: dict[str, float] | None = None
) -> pd.DataFrame:
    rows = []
    for ticker, r in rets.items():
        level = (start_price or {}).get(ticker, 50.0) * np.cumprod(1 + r)
        asset = "etf" if ticker in ("SPY", "XLK", "TLT") else "stock"
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "asset_type": asset,
                    "date": SESSIONS,
                    "close": level,
                    "volume": 1e6,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


class _Prices:
    """An in-memory PriceSource."""

    def __init__(
        self,
        prices: pd.DataFrame,
        splits: pd.DataFrame | None = None,
        divs: pd.DataFrame | None = None,
    ) -> None:
        self.frame = prices
        self.splits = (
            splits
            if splits is not None
            else pd.DataFrame(columns=["ticker", "ex_date", "ratio"])
        )
        self.divs = (
            divs
            if divs is not None
            else pd.DataFrame(columns=["ticker", "ex_date", "amount"])
        )

    def prices(
        self, start, end, *, tickers=None, asset_types=None, columns=("close", "volume")
    ):
        f = self.frame[
            (self.frame["date"] >= pd.Timestamp(start))
            & (self.frame["date"] <= pd.Timestamp(end))
        ]
        if tickers is not None:
            f = f[f["ticker"].isin(tickers)]
        if asset_types is not None:
            f = f[f["asset_type"].isin(asset_types)]
        return f.reset_index(drop=True)

    def split_days(self, start, end, *, tickers=None):
        return (
            self.splits[self.splits["ticker"].isin(tickers)]
            if tickers is not None
            else self.splits
        )

    def dividends(self, start, end, *, tickers=None):
        return (
            self.divs[self.divs["ticker"].isin(tickers)]
            if tickers is not None
            else self.divs
        )


#: independent names, so a day's median is a market level and not one of the names under test
BACKGROUND = [f"N{i:02d}" for i in range(30)]
MEMBERS = {
    "AAA": "stock",
    "BBB": "stock",
    "CCC": "stock",
    "XLK": "etf",
    **{b: "stock" for b in BACKGROUND},
}


class _Universe:
    def __init__(self, members: dict[str, str] | None = None) -> None:
        self.members = members or MEMBERS

    def context_etfs(self) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "ticker": ["SPY", "XLK", "TLT"],
                "group": ["broad", "sectors", "rates_credit"],
            }
        )

    def membership(self) -> pd.DataFrame:
        quarters = pd.date_range("2022-01-01", "2024-10-01", freq="QS")
        rows = [
            (q, t, a, "T1" if a == "stock" else "context")
            for q in quarters
            for t, a in self.members.items()
        ]
        return pd.DataFrame(
            rows, columns=["quarter_start", "ticker", "asset_type", "tier"]
        )


def _beta_params(**kw) -> BetaParams:
    base = dict(
        start=dt.date(2023, 6, 1),
        windows=(63, 126),
        market="SPY",
        min_price=5.0,
        min_dollar_volume=1e6,
    )
    base.update(kw)
    return BetaParams(**base)


# --------------------------------------------------------------------------- #
# moments, calendar
# --------------------------------------------------------------------------- #
def test_pairwise_matches_numpy_on_shared_rows() -> None:
    rng = np.random.default_rng(1)
    x = rng.normal(size=(50, 3))
    y = rng.normal(size=(50, 2)) + 0.5 * x[:, :2]
    x[5, 0] = np.nan
    y[7, 1] = np.nan
    got = moments.pairwise(x, y)
    rows = ~np.isnan(x[:, 0]) & ~np.isnan(y[:, 1])
    assert got.n[0, 1] == rows.sum()
    expect_beta = np.polyfit(y[rows, 1], x[rows, 0], 1)[0]
    assert got.beta()[0, 1] == pytest.approx(expect_beta)
    assert got.corr()[0, 1] == pytest.approx(np.corrcoef(x[rows, 0], y[rows, 1])[0, 1])
    empty = moments.pairwise(np.full((4, 1), np.nan), np.ones((4, 1)))
    assert empty.n[0, 0] == 0 and np.isnan(empty.beta()[0, 0])


def test_market_residuals_are_orthogonal_to_the_market() -> None:
    rng = np.random.default_rng(2)
    m = rng.normal(size=200)
    r = np.column_stack(
        [
            0.3 + 1.7 * m + rng.normal(size=200) * 0.1,
            np.r_[np.full(190, np.nan), m[:10]],
        ]
    )
    res = moments.market_residuals(r, m, min_obs=50)
    assert abs(np.corrcoef(res[:, 0], m)[0, 1]) < 1e-9 and abs(res[:, 0].mean()) < 1e-12
    assert np.isnan(res[:, 1]).all()  # too few rows


def test_month_ends_need_a_later_session_and_quarters_start_on_the_first() -> None:
    s = pd.DatetimeIndex(
        ["2024-01-30", "2024-01-31", "2024-02-28", "2024-02-29", "2024-03-01"]
    )
    assert list(month_end_sessions(s)) == [
        pd.Timestamp("2024-01-31"),
        pd.Timestamp("2024-02-29"),
    ]
    assert list(month_end_sessions(s, pd.Timestamp("2024-02-01"))) == [
        pd.Timestamp("2024-02-29")
    ]
    assert quarter_start(pd.Timestamp("2024-05-17")) == pd.Timestamp("2024-04-01")


# --------------------------------------------------------------------------- #
# returns
# --------------------------------------------------------------------------- #
def test_returns_add_dividends_mask_splits_jumps_and_gaps() -> None:
    days = SESSIONS[:8]
    close = [
        100,
        101,
        102,
        25.6,
        25.8,
        26.0,
        52.0,
        26.1,
    ]  # a 4:1 split on day 3, a bad print on day 6
    prices = pd.DataFrame({"ticker": "X", "date": days, "close": close, "volume": 1e6})
    prices = prices.drop(index=4)  # day 4 missing: day 5 is a gap
    splits = pd.DataFrame({"ticker": ["X"], "ex_date": [days[3]], "ratio": [4.0]})
    divs = pd.DataFrame(
        {"ticker": ["X", "X"], "ex_date": [days[1], days[7]], "amount": [1.0, 20.0]}
    )
    panel = returns.total_returns(
        prices,
        splits,
        divs,
        days,
        event_window=1,
        max_abs_log_return=0.5,
        max_dividend_yield=0.25,
    )
    r = panel.returns["X"]
    assert np.isnan(r.iloc[0])
    assert r.iloc[1] == pytest.approx(
        (101 + 1.0) / 100 - 1, rel=1e-6
    )  # two sessions before: kept, dividend added
    assert np.isnan(r.iloc[2]) and np.isnan(
        r.iloc[3]
    )  # within one session of the ex-date
    assert np.isnan(r.iloc[5])  # gap after the missing day
    assert np.isnan(r.iloc[6])  # 26.0 -> 52.0 is a jump
    assert np.isnan(r.iloc[7])  # a 20.0 dividend on a 52.0 close is no return
    counts = panel.masked.loc["X"].to_dict()
    assert counts == {"gap": 1, "event": 2, "dividend": 1, "jump": 1}
    clean = returns.total_returns(
        prices.iloc[:3],
        splits.iloc[:0],
        divs.iloc[:1],
        days,
        event_window=1,
        max_abs_log_return=0.5,
        max_dividend_yield=0.25,
    )
    assert clean.returns["X"].iloc[1] == pytest.approx((101 + 1.0) / 100 - 1, rel=1e-6)


# --------------------------------------------------------------------------- #
# betas
# --------------------------------------------------------------------------- #
def _beta_panels(rets: dict[str, np.ndarray], **price_kw):
    prices = _prices(rets, **price_kw)
    empty_s = pd.DataFrame(columns=["ticker", "ex_date", "ratio"])
    empty_d = pd.DataFrame(columns=["ticker", "ex_date", "amount"])
    kw = dict(event_window=1, max_abs_log_return=0.5, max_dividend_yield=0.25)
    stocks = returns.total_returns(
        prices[prices["asset_type"] == "stock"], empty_s, empty_d, SESSIONS, **kw
    )
    etfs = returns.total_returns(
        prices[prices["asset_type"] == "etf"], empty_s, empty_d, SESSIONS, **kw
    )
    return stocks, etfs.returns


def test_betas_recover_known_loadings_and_net_out_the_market() -> None:
    stocks, etf_returns = _beta_panels(_market(), start_price={"PENNY": 2.0})
    groups = {"SPY": "broad", "XLK": "sectors", "TLT": "rates_credit"}
    as_of = SESSIONS[-1]
    frame = betas.snapshot(stocks, etf_returns, groups, as_of, _beta_params())
    assert "PENNY" not in set(frame["ticker"])  # below the price floor
    a = frame[(frame["ticker"] == "AAA") & (frame["window"] == 126)].set_index("etf")
    b = frame[(frame["ticker"] == "BBB") & (frame["window"] == 126)].set_index("etf")
    assert (
        np.isnan(a.loc["SPY", "beta_ex_mkt"]) and a.loc["SPY", "etf_group"] == "broad"
    )
    assert (
        a.loc["XLK", "beta_ex_mkt"] == pytest.approx(1.5, abs=0.15)
        and a.loc["XLK", "corr_ex_mkt"] > 0.8
    )
    assert abs(b.loc["XLK", "corr_ex_mkt"]) < 0.25 and b.loc[
        "SPY", "beta"
    ] == pytest.approx(0.8, abs=0.1)
    assert set(frame["window"]) == {63, 126} and (frame["n_obs"] >= 50).all()
    assert list(frame.columns) == betas.COLUMNS


def test_betas_are_point_in_time() -> None:
    rets = _market()
    as_of = SESSIONS[250]
    full = betas.snapshot(
        *_beta_panels(rets), {"SPY": "b", "XLK": "s", "TLT": "r"}, as_of, _beta_params()
    )
    cut = {k: v[:251] for k, v in rets.items()}
    stocks, etf_returns = _beta_panels_cut(cut)
    truncated = betas.snapshot(
        stocks, etf_returns, {"SPY": "b", "XLK": "s", "TLT": "r"}, as_of, _beta_params()
    )
    pd.testing.assert_frame_equal(full, truncated)


def _beta_panels_cut(rets: dict[str, np.ndarray]):
    days = SESSIONS[: len(next(iter(rets.values())))]
    rows = []
    for ticker, r in rets.items():
        asset = "etf" if ticker in ("SPY", "XLK", "TLT") else "stock"
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "asset_type": asset,
                    "date": days,
                    "close": 50 * np.cumprod(1 + r),
                    "volume": 1e6,
                }
            )
        )
    prices = pd.concat(rows, ignore_index=True)
    e_s = pd.DataFrame(columns=["ticker", "ex_date", "ratio"])
    e_d = pd.DataFrame(columns=["ticker", "ex_date", "amount"])
    kw = dict(event_window=1, max_abs_log_return=0.5, max_dividend_yield=0.25)
    stocks = returns.total_returns(
        prices[prices["asset_type"] == "stock"], e_s, e_d, days, **kw
    )
    etfs = returns.total_returns(
        prices[prices["asset_type"] == "etf"], e_s, e_d, days, **kw
    )
    return stocks, etfs.returns


# --------------------------------------------------------------------------- #
# abnormal auctions and co-spikes
# --------------------------------------------------------------------------- #
def _auctions(seed: int = 3, days: pd.DatetimeIndex = SESSIONS) -> pd.DataFrame:
    """Closing auctions where AAA and BBB share spike days, CCC is independent, XLK is an ETF."""
    rng = np.random.default_rng(seed)
    n = len(days)
    common_day = rng.normal(0, 0.3, n)  # what every name shares (expiries, rebalances)
    shared = (rng.random(n) < 0.08) * 1.5  # AAA and BBB spike together
    logs = {
        "AAA": 18 + common_day + shared + rng.normal(0, 0.2, n),
        "BBB": 17 + common_day + shared + rng.normal(0, 0.2, n),
        "CCC": 16 + common_day + (rng.random(n) < 0.08) * 1.5 + rng.normal(0, 0.2, n),
        "XLK": 19 + common_day + rng.normal(0, 0.2, n),
        **{
            b: 15 + common_day + (rng.random(n) < 0.08) * 1.5 + rng.normal(0, 0.2, n)
            for b in BACKGROUND
        },
    }
    rows = []
    for ticker, lv in logs.items():
        price = 100.0
        size = np.exp(lv) / price
        rows.append(
            pd.DataFrame(
                {
                    "ticker": ticker,
                    "date": days,
                    "open_price": price,
                    "open_size": (size / 10).astype("int64"),
                    "close_price": price,
                    "close_size": size.astype("int64"),
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_abnormal_uses_only_the_past_and_centres_each_day_on_members() -> None:
    auctions = _auctions()
    membership = _Universe().membership()
    panel = abnormal.build(
        auctions, membership, AbnormalParams(baseline_window=20, baseline_min_obs=10)
    )
    close = panel[panel["side"] == "close"]
    first_days = close[close["ticker"] == "AAA"].head(10)
    assert (
        first_days["baseline"].isna().all()
    )  # fewer than baseline_min_obs earlier days
    members = close[close["is_member"] & close["resid"].notna()]
    centre = members.groupby(["date", "asset_type"])["resid"].median().abs().max()
    assert centre < 1e-5
    # a 2:1 split halves the price and doubles the size: dollar value, so no spike
    split = auctions.copy()
    later = split["date"] >= SESSIONS[200]
    split.loc[later & (split["ticker"] == "AAA"), "close_price"] /= 2
    split.loc[later & (split["ticker"] == "AAA"), "close_size"] *= 2
    again = abnormal.build(
        split, membership, AbnormalParams(baseline_window=20, baseline_min_obs=10)
    )
    a0 = close[close["ticker"] == "AAA"]["abnormal"].to_numpy()
    a1 = again[(again["side"] == "close") & (again["ticker"] == "AAA")][
        "abnormal"
    ].to_numpy()
    np.testing.assert_allclose(a0, a1, atol=1e-5, equal_nan=True)
    # point in time: dropping the later days leaves earlier rows unchanged
    cut = abnormal.build(
        auctions[auctions["date"] <= SESSIONS[150]],
        membership,
        AbnormalParams(baseline_window=20, baseline_min_obs=10),
    )
    head = panel[pd.to_datetime(panel["date"]) <= SESSIONS[150]].reset_index(drop=True)
    pd.testing.assert_frame_equal(head, cut.reset_index(drop=True))


def test_cospike_finds_the_pair_that_spikes_together() -> None:
    membership = _Universe().membership()
    panel = abnormal.build(
        _auctions(), membership, AbnormalParams(baseline_window=20, baseline_min_obs=10)
    )
    close = panel[panel["side"] == "close"].assign(
        date=lambda f: pd.to_datetime(f["date"])
    )
    resid = close.pivot_table(
        index="date", columns="ticker", values="resid", aggfunc="first"
    )
    params = CospikeParams(window=200, min_overlap=150, top_k=2)
    types = MEMBERS
    names = sorted(MEMBERS)
    as_of = SESSIONS[-1]
    frame = cospike.snapshot(resid, names, types, as_of, params)
    top = frame[frame["rank"] == 1].set_index("ticker")["neighbor"]
    assert top["AAA"] == "BBB" and top["BBB"] == "AAA"
    pair = frame[(frame["ticker"] == "AAA") & (frame["neighbor"] == "BBB")].iloc[0]
    assert pair["co_spikes"] > 5 and pair["lift"] > 3 and pair["n_overlap"] >= 150
    assert (frame["ticker"] != frame["neighbor"]).all() and frame.groupby(
        "ticker"
    ).size().max() <= 2
    # point in time
    cut = cospike.snapshot(
        resid.loc[: SESSIONS[300]], names, types, SESSIONS[300], params
    )
    full = cospike.snapshot(resid, names, types, SESSIONS[300], params)
    pd.testing.assert_frame_equal(cut, full)
    assert cospike.snapshot(resid, ["AAA"], types, as_of, params).empty


# --------------------------------------------------------------------------- #
# store and datasets
# --------------------------------------------------------------------------- #
def test_store_classifies_and_apply_writes_only_what_changed(tmp_path: Path) -> None:
    store = PartitionStore(tmp_path, "auction_abnormal", abnormal.SCHEMA)
    ds = datasets.AuctionAbnormal(
        type("A", (), {"daily": staticmethod(_auctions)})(),
        _Universe(),
        store,
        AbnormalParams(baseline_window=20, baseline_min_obs=10),
    )
    plan = datasets.apply(ds.plan(), store, run=False)
    assert (
        [p.key for p in plan.partitions] == ["2023", "2024"]
        and plan.count(NEW) == 2
        and not store.keys_on_disk()
    )
    datasets.apply(ds.plan(), store, run=True)
    assert store.keys_on_disk() == ["2023", "2024"]
    again = datasets.apply(ds.plan(), store, run=True)
    assert again.count(UNCHANGED) == 2
    assert datasets.apply(ds.plan(), store, run=False, full=True).count(RESTATED) == 2
    ds.params = AbnormalParams(baseline_window=30, baseline_min_obs=10)
    assert datasets.apply(ds.plan(), store, run=True).count(RESTATED) == 2
    (tmp_path / "auction_abnormal" / "2019.parquet").write_bytes(
        (tmp_path / "auction_abnormal" / "2023.parquet").read_bytes()
    )
    plan = datasets.apply(ds.plan(), store, run=True)
    assert plan.stale == ["2019"] and store.keys_on_disk() == ["2023", "2024"]
    assert store.read_all().shape[0] == sum(len(p.frame) for p in plan.partitions)


def test_etf_betas_dataset_wires_sources_into_month_end_snapshots(
    tmp_path: Path,
) -> None:
    prices = _prices(_market())
    splits = pd.DataFrame(
        {"ticker": ["AAA"], "ex_date": [SESSIONS[300]], "ratio": [2.0]}
    )
    store = PartitionStore(tmp_path, "etf_betas", betas.SCHEMA)
    ds = datasets.EtfBetas(
        _Prices(prices, splits),
        _Universe(),
        store,
        _beta_params(),
        today=dt.date(2024, 7, 1),
    )
    plan = ds.plan()
    keys = [p.key for p in plan.partitions]
    assert (
        keys[0] == "2023-06-30" and keys[-1] == "2024-05-31"
    )  # June 2024 has no later session
    assert "event 3" in plan.notes[1]  # the ex-date and one session on each side
    with pytest.raises(ValueError, match="market"):
        datasets.EtfBetas(
            _Prices(prices),
            _Universe(),
            store,
            _beta_params(market="VTI"),
            today=dt.date(2024, 7, 1),
        ).plan()


def test_params_parse_from_toml_tables_and_reject_typos() -> None:
    params = params_from(
        {
            "data_root": "x",
            "etf_betas": {"windows": [21, 63], "start": "2018-01-01"},
            "auction_cospike": {"top_k": 5},
        }
    )
    assert params.etf_betas.windows == (21, 63) and params.etf_betas.start == dt.date(
        2018, 1, 1
    )
    assert (
        params.auction_cospike.top_k == 5
        and params.auction_abnormal == AbnormalParams()
    )
    assert digest(params.etf_betas) != digest(BetaParams())
    with pytest.raises(ValueError, match="unknown"):
        params_from({"etf_betas": {"windwos": [63]}})
    with pytest.raises(ValueError, match="unknown"):
        params_from({"etf_beta": {}})
    with pytest.raises(ValueError):
        BetaParams(windows=(5,))


# --------------------------------------------------------------------------- #
# CLI over fixture roots
# --------------------------------------------------------------------------- #
def _fixture_roots(base: Path) -> tuple[Path, Path]:
    eodhd, alpaca = base / "eodhd", base / "alpaca"
    (eodhd / "intraday").mkdir(parents=True)
    members = _Universe().membership()
    etfs = pd.DataFrame(
        [
            (q, t, "etf", "context")
            for q in members["quarter_start"].unique()
            for t in ("SPY", "XLK", "TLT")
        ],
        columns=["quarter_start", "ticker", "asset_type", "tier"],
    )
    allm = pd.concat(
        [members[members["asset_type"] == "stock"], etfs], ignore_index=True
    )
    allm["quarter_start"] = allm["quarter_start"].dt.strftime("%Y-%m-%d")
    allm["exchange"] = "US"
    allm["group"] = (
        allm["ticker"]
        .map({"SPY": "broad", "XLK": "sectors", "TLT": "rates_credit"})
        .fillna("")
    )
    allm.to_parquet(eodhd / "intraday" / "universe_membership.parquet")
    prices = _prices(_market())
    for lane, asset in (("us_common", "stock"), ("us_etf", "etf")):
        (eodhd / lane).mkdir()
        part = prices[prices["asset_type"] == asset].drop(columns="asset_type")
        part = part.assign(
            date=part["date"].dt.strftime("%Y-%m-%d"),
            exchange="US",
            open=part["close"],
            adjusted_close=part["close"],
        )
        part.to_parquet(eodhd / lane / "prices_daily.parquet")
    (alpaca / "auctions").mkdir(parents=True)
    auctions = _auctions()
    auctions["date"] = auctions["date"].dt.date
    auctions.to_parquet(alpaca / "auctions" / "auctions_daily.parquet")
    return eodhd, alpaca


def test_cli_builds_all_three_and_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    eodhd, alpaca = _fixture_roots(tmp_path)
    monkeypatch.setenv("EODHD_DATA_ROOT", str(eodhd))
    monkeypatch.setenv("DATACLI_ALPACA_ROOT", str(alpaca))
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "linkage"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    params = Params(
        etf_betas=_beta_params(),
        auction_abnormal=AbnormalParams(baseline_window=20, baseline_min_obs=10),
        auction_cospike=CospikeParams(
            start=dt.date(2024, 1, 1), window=200, min_overlap=150, top_k=2
        ),
    )
    monkeypatch.setattr(cli.config, "load_params", lambda: params)
    monkeypatch.setattr(cli, "_today", lambda: dt.date(2024, 7, 1))
    assert cli.main(["build"]) == 0
    out = capsys.readouterr().out
    assert "nothing written" in out and not (tmp_path / "linkage").exists()
    assert cli.main(["build", "--run"]) == 0
    out = capsys.readouterr().out
    assert (
        "etf_betas: 12 partitions" in out
        and "auction_abnormal: 2 partitions" in out
        and "auction_cospike: 5 partitions" in out
    )
    assert cli.main(["build", "--run"]) == 0
    assert "0 new, 0 restated" in capsys.readouterr().out
    assert (
        cli.main(["build", "--dataset", "auction_cospike"]) == 0
    )  # alone: reads the stored panel
    assert "0 new, 0 restated, 5 unchanged" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [d["partitions"] for d in payload["datasets"]] == [12, 2, 5] and all(
        d["current_params"] for d in payload["datasets"]
    )
    assert cli.main(["qc"]) == 0, capsys.readouterr().out
    capsys.readouterr()
    con = duckdb.connect()
    assert all(views.register(con, root=tmp_path / "linkage").values())
    top = con.execute(
        "SELECT neighbor FROM auction_cospike WHERE ticker = 'AAA' AND rank = 1 ORDER BY as_of DESC LIMIT 1"
    ).fetchone()
    assert top == ("BBB",)
    assert cli.main(["build", "--dataset", "nope"]) == 2
    assert cli.main(["build", "extra"]) == 2
    assert cli.main(["nope"]) == 2 and cli.main(["--help"]) == 0


def test_qc_flags_a_broken_manifest(tmp_path: Path) -> None:
    store = PartitionStore(tmp_path, "auction_cospike", cospike.SCHEMA)
    assert qc.findings(store)[0][1] == "empty"
    frame = pd.DataFrame(
        {
            "as_of": [dt.date(2024, 1, 31)] * 2,
            "ticker": ["A", "A"],
            "neighbor": ["A", "B"],
            "rank": [1, 2],
            "corr": [0.5, 1.5],
            "n_overlap": [200, 200],
            "co_spikes": [3, 3],
            "expected_co_spikes": [1.0, 1.0],
            "lift": [3.0, 3.0],
            "asset_type": ["stock"] * 2,
            "neighbor_asset_type": ["stock"] * 2,
        }
    )
    store.write("2024-01-31", frame, inputs="x", params="p")
    (tmp_path / "auction_cospike" / "2024-02-29.parquet").write_bytes(
        store.path("2024-01-31").read_bytes()
    )
    checks = {c for _, c, _ in qc.findings(store)}
    assert {"manifest", "self", "range"} <= checks


def test_scheduler_admits_the_linkage_family(tmp_path: Path) -> None:
    from scheduler.commands import (
        CommandValidationError,
        ValidationContext,
        default_registry,
    )

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    context = ValidationContext.current(
        _REPO_ROOT, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    build = registry.validate(
        "linkage", "build", ["--dataset", "etf_betas", "--run"], context
    )
    names = {b.name: b for b in build.bindings}
    claims = {c.resource_id: c.mode for c in build.spec.resources}
    assert claims[names["linkage_data_root"].resource_id] == "exclusive"
    assert (
        claims[names["eodhd_data_root"].resource_id] == "shared"
        and claims[names["alpaca_data_root"].resource_id] == "shared"
    )
    assert build.spec.mutation and not build.spec.network
    assert registry.validate(
        "linkage", "qc", ["--dataset", "auction_cospike"], context
    ).spec.resources
    with pytest.raises(CommandValidationError, match="must be one of"):
        registry.validate(
            "linkage", "build", ["--dataset", "holdings", "--run"], context
        )
    with pytest.raises(CommandValidationError, match="requires its own --run"):
        registry.validate("linkage", "build", [], context)


def test_lab_and_explorer_carry_the_alpaca_and_linkage_views(
    tmp_path: Path, monkeypatch
) -> None:
    """The lab's schema text names the new views once data exists; the explorer registers them."""
    from lab import data as lab_data

    eodhd, alpaca = _fixture_roots(tmp_path)
    con = duckdb.connect()
    text = lab_data.schema_text(con)
    assert "Alpaca views" not in text and "Linkage views" not in text
    from alpaca import views as alpaca_views
    from alpaca.store import AuctionStore

    AuctionStore(alpaca).state_path.write_text(
        "ticker,status\nAAA,ok\n", encoding="utf-8"
    )
    assert alpaca_views.register(con, root=alpaca)[alpaca_views.DAILY_VIEW]
    store = PartitionStore(tmp_path / "linkage", "auction_abnormal", abnormal.SCHEMA)
    panel = abnormal.build(
        _auctions(),
        _Universe().membership(),
        AbnormalParams(baseline_window=20, baseline_min_obs=10),
    )
    store.write("2024", panel, inputs="x", params="p")
    assert views.register(con, root=tmp_path / "linkage")["auction_abnormal"]
    text = lab_data.schema_text(con)
    assert "Alpaca views" in text and "Linkage views" in text
    monkeypatch.setenv("DATACLI_LINKAGE_ROOT", str(tmp_path / "linkage"))
    monkeypatch.setenv("DATACLI_ALPACA_ROOT", str(alpaca))
    sys.path.insert(0, str(_REPO_ROOT / "eodhd"))
    import explore_eodhd  # type: ignore[import-not-found]

    explorer = explore_eodhd.connect()
    assert explorer.execute("SELECT count(*) FROM auction_abnormal").fetchone()[
        0
    ] == len(panel)
    assert explorer.execute("SELECT count(*) FROM alpaca_auctions").fetchone()[
        0
    ] == len(_auctions())
