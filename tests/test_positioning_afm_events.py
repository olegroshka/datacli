"""positioning.afm_events and positioning.afm_synthetic: EVAL-009's two readings on synthetic data."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import afm_events as ae  # noqa: E402
from positioning import afm_synthetic as asy  # noqa: E402

DATES = pd.bdate_range("2024-01-01", "2024-12-31")


def _notes(rows: list[tuple]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["holder", "issuer", "obligation_date", "pct_capital", "pct_capital_potential"])
    frame["obligation_date"] = pd.to_datetime(frame["obligation_date"]).dt.date
    frame["published_from"] = (pd.to_datetime(frame["obligation_date"]) + pd.offsets.BDay(2)).dt.date
    frame["has_potential"] = frame["pct_capital_potential"] > 0
    return frame


HOLDERS = pd.DataFrame({"holder": ["HF", "HF2", "Bank", "Mr X"], "kind": ["hedge_fund", "hedge_fund", "bank_or_passive", "person"], "adv_match": ["exact", "core", "", ""]})


def test_crossings_read_direction_against_the_previous_notification_and_start_in_2013() -> None:
    notes = _notes(
        [
            ("HF", "A", "2012-06-01", 3.1, 0.0),  # before the sample: sets the previous level only
            ("HF", "A", "2024-02-01", 5.2, 0.0),  # up
            ("HF", "A", "2024-03-01", 5.2, 0.0),  # unchanged: no event
            ("HF", "A", "2024-04-01", 2.9, 0.0),  # down (exit)
            ("HF2", "B", "2024-02-01", 3.4, 3.4),  # first notification at the threshold: up, potential
            ("HF2", "C", "2024-02-01", 2.5, 0.0),  # first notification below: an exit seen first, down
            ("HF2", "C", "2024-02-01", 2.7, 0.0),  # same day: the last stands
            ("Bank", "A", "2024-02-01", 5.0, 5.0),  # not a hedge fund
            ("Mr X", "A", "2024-02-01", 3.2, 0.0),
        ]
    )
    ev_ = ae.crossings(notes, HOLDERS)
    assert list(ev_.columns) == list(ae.EVENT_COLUMNS)
    assert len(ev_) == 4
    a = ev_[ev_["issuer"] == "A"].sort_values("obligation_date")
    assert a["direction"].tolist() == ["up", "down"] and a["prev_pct"].tolist() == [3.1, 5.2]
    b = ev_[ev_["issuer"] == "B"].iloc[0]
    assert b["direction"] == "up" and bool(b["has_potential"]) is True and pd.isna(b["prev_pct"])
    c = ev_[ev_["issuer"] == "C"].iloc[0]
    assert c["direction"] == "down" and c["pct_capital"] == 2.7
    assert ev_["published_from"].iloc[0] == dt.date(2024, 2, 5)  # Thursday + 2 weekdays = Monday
    others = ae.crossings(notes, HOLDERS, kinds=ae.NON_BANK_KINDS)
    assert others["holder"].tolist() == ["Mr X"]


def test_event_cars_use_the_entry_close_the_market_and_the_pre_window() -> None:
    up = pd.Series(np.linspace(100.0, 150.0, len(DATES)), index=DATES)  # a steady riser
    flat = pd.Series(100.0, index=DATES)
    market = ae.market_returns({"UP": up, "FLAT": flat})
    assert market.index[0] == DATES[1] and market.iloc[0] == pytest.approx(up.pct_change().iloc[1] / 2)
    events = pd.DataFrame(
        {
            "holder": ["HF", "HF", "HF"],
            "issuer": ["A", "B", "Z"],
            "obligation_date": [dt.date(2024, 6, 1), dt.date(2024, 12, 20), dt.date(2024, 6, 1)],
            "published_from": [dt.date(2024, 6, 4), dt.date(2024, 12, 24), dt.date(2024, 6, 4)],
            "pct_capital": [3.5, 3.5, 3.5],
            "prev_pct": [np.nan, np.nan, np.nan],
            "direction": ["up", "up", "up"],
            "has_potential": [False, False, False],
            "kind": ["hedge_fund"] * 3,
        }
    )
    cars = ae.event_cars(events, {"A": up, "B": flat}, market, horizons=(5, 21))
    a = cars[cars["issuer"] == "A"].iloc[0]
    assert a["entry_date"] == pd.Timestamp("2024-06-04")
    i = DATES.get_loc(pd.Timestamp("2024-06-04"))
    own = up.iloc[i + 21] / up.iloc[i] - 1.0
    mkt = float(np.prod(1.0 + market.loc[DATES[i + 1] : DATES[i + 21]]) - 1.0)
    assert a["car_21"] == pytest.approx(own - mkt)
    assert a["pre_car"] == pytest.approx((up.iloc[i] / up.iloc[i - 21] - 1.0) - float(np.prod(1.0 + market.loc[DATES[i - 20] : DATES[i]]) - 1.0))
    b = cars[cars["issuer"] == "B"].iloc[0]
    assert np.isnan(b["car_21"]) and not np.isnan(b["car_5"])  # too few closes left for 21 days
    assert b["car_5"] == pytest.approx(-float(np.prod(1.0 + market.loc[DATES[DATES.get_loc(pd.Timestamp('2024-12-24')) + 1] :][:5]) - 1.0))
    z = cars[cars["issuer"] == "Z"].iloc[0]
    assert pd.isna(z["entry_date"]) and np.isnan(z["car_5"])  # no series
    assert cars["cluster"].iloc[0] == "2024-W23"


def test_cluster_t_and_run_events_and_halves() -> None:
    rng = np.random.default_rng(3)
    n = 200
    weeks = pd.Series([f"W{i // 4}" for i in range(n)])
    values = pd.Series(0.02 + rng.normal(0, 0.05, n))
    mean, t, count, g = ae.cluster_t(values, weeks)
    assert count == n and g == 50 and mean == pytest.approx(values.mean())
    # the clustered t is below the naive one when the cluster shares a shock
    shock = pd.Series(np.repeat(rng.normal(0, 0.05, 50), 4))
    _, t_cl, _, _ = ae.cluster_t(values + shock, weeks)
    naive = (values + shock).mean() / ((values + shock).std(ddof=1) / np.sqrt(n))
    assert abs(t_cl) < abs(naive)
    assert ae.cluster_t(pd.Series([1.0, 2.0]), pd.Series(["a", "b"]))[1] != ae.cluster_t(pd.Series([1.0, 2.0]), pd.Series(["a", "b"]))[1]  # nan
    cars = pd.DataFrame(
        {
            "direction": ["up"] * n + ["down"] * 10,
            "car_21": list(values) + [-0.01] * 10,
            "cluster": list(weeks) + ["X"] * 10,
            "published_from": list(pd.date_range("2015-01-01", periods=n, freq="7D").date) + [dt.date(2026, 12, 1)] * 10,
        }
    )
    results = ae.run_events(cars)
    table = ae.results_table(results)
    assert list(table["direction"]) == ["up", "down"] and table["events"].tolist() == [n, 10]
    up = results[0]
    assert up.readable and up.sign_agrees and up.p_value < 0.025 and up.significant
    down = results[1]
    assert not down.readable and not down.significant  # fewer than 30 events
    first, second = ae.halves(cars)
    assert len(first) + len(second) == len(cars) and first["published_from"].max() < second["published_from"].min()
    assert len(ae.ledger(cars)) == 10


def test_synthetic_panel_sums_the_latest_visible_broker_potential_interest() -> None:
    holders = pd.DataFrame(
        {"holder": ["Goldman Sachs Group Inc., The", "UBS Group AG", "BlackRock Inc.", "Goldman Sachs Asset Management, L.P.", "HF"], "kind": ["bank_or_passive"] * 4 + ["hedge_fund"], "adv_match": [""] * 5}
    )
    brokers = asy.broker_holders(holders)
    assert brokers == {"Goldman Sachs Group Inc., The", "UBS Group AG"}  # the asset-management arm is not the broker-dealer
    notes = _notes(
        [
            ("Goldman Sachs Group Inc., The", "A", "2024-01-03", 4.0, 3.5),
            ("UBS Group AG", "A", "2024-02-01", 3.2, 3.2),
            ("Goldman Sachs Group Inc., The", "A", "2024-03-01", 2.9, 0.0),  # Goldman falls out
            ("BlackRock Inc.", "A", "2024-01-03", 6.0, 6.0),  # a passive: not a broker
            ("UBS Group AG", "B", "2024-06-03", 5.0, 4.8),
        ]
    )
    weeks = pd.DatetimeIndex(["2024-01-05", "2024-02-09", "2024-03-08", "2024-06-07"])
    panel = asy.synthetic_panel(notes, brokers, weeks, ["A", "B", "C"])
    assert list(panel.columns) == list(asy.PANEL_COLUMNS) and len(panel) == 12
    a = panel[panel["issuer"] == "A"].set_index("date")
    assert a["synthetic_pct"].tolist() == [3.5, 6.7, 3.2, 3.2] and a["n_brokers"].tolist() == [1, 2, 1, 1]
    b = panel[panel["issuer"] == "B"].set_index("date")
    assert b["synthetic_pct"].tolist() == [0.0, 0.0, 0.0, 4.8]
    assert (panel[panel["issuer"] == "C"]["synthetic_pct"] == 0.0).all()
    diag = asy.diagnostic(panel)
    assert diag["with_book"].tolist() == [1, 1, 1, 2] and diag["broker_lines"].tolist() == [1, 2, 1, 2]
    assert diag["median_pct"].iloc[3] == pytest.approx(4.0)


def test_synthetic_observations_and_family_on_a_planted_ordering() -> None:
    rng = np.random.default_rng(11)
    issuers = [f"I{i}" for i in range(30)]
    weeks = pd.DatetimeIndex([d for d in pd.bdate_range("2024-01-05", "2024-12-27", freq="W-FRI")])
    closes = {}
    for k, issuer in enumerate(issuers):
        drift = 0.0005 * (k / 29)  # the higher the index, the higher the drift
        closes[issuer] = pd.Series(100.0 * np.cumprod(1.0 + drift + rng.normal(0, 0.01, len(DATES) + 260)), index=pd.bdate_range("2023-01-02", periods=len(DATES) + 260))
    panel = pd.DataFrame({"date": np.repeat(weeks, 30), "issuer": issuers * len(weeks)})
    panel["synthetic_pct"] = [float(issuers.index(i)) / 10.0 for i in panel["issuer"]]  # the level orders the drift
    panel["n_brokers"] = (panel["synthetic_pct"] > 0).astype(int)
    shares = pd.DataFrame({"issuer": issuers, "visible_from": pd.Timestamp("2020-01-01"), "issued_capital": 1e6})
    volumes = {issuer: pd.Series(1000.0 * (k + 1), index=closes[issuer].index) for k, issuer in enumerate(issuers)}
    obs = asy.observations(panel, closes, shares=shares, volumes=volumes)
    assert {"ret_5", "ret_adj_21", "reversal_21d", "momentum_12_1", "log_mcap", "log_adv", "synthetic_chg_4w"} <= set(obs.columns)
    assert obs["log_mcap"].notna().all() and obs["momentum_12_1"].notna().all() and obs["log_adv"].notna().all()
    first_week = obs[obs["date"] == obs["date"].min()].set_index("issuer")
    assert first_week.loc["I29", "log_adv"] > first_week.loc["I0", "log_adv"]
    assert asy.run_family(obs, features=("synthetic_pct",), cleanings=asy.EXTENDED_CLEANINGS)[0].family_size == 3
    assert obs.groupby("date").size().min() == 30
    results = asy.run_family(obs)
    assert len(results) == asy.FAMILY_SIZE == 18
    level_none_21 = next(r for r in results if r.feature == "synthetic_pct" and r.cleaning == "none" and r.horizon == 21)
    assert level_none_21.mean_ic > 0 and level_none_21.sign_agrees and level_none_21.family_size == 18
    chg = [r for r in results if r.feature == "synthetic_chg_4w"]
    assert all(r.n_dates == 0 or np.isnan(r.mean_ic) for r in chg)  # a constant level has no change: no cross-section
