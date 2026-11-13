"""The US volume fix of the intraday lane (eodhd/intraday_volume.py) and its view."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = _REPO_ROOT / "eodhd"
for path in (_REPO_ROOT, _EODHD):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_volume as iv  # noqa: E402

duckdb = pytest.importorskip("duckdb")

RTH = 20_000.0  # each of five regular-hours bars


def _day(ticker: str, day: str, post: dict[str, float]) -> list[dict]:
    """Five 1000-share regular-hours bars plus ``post`` bars at ET minutes."""
    rows = []
    for hm, vol, session in [
        *[(f"10:0{i}", RTH, "rth") for i in range(5)],
        *[(hm, v, "post") for hm, v in post.items()],
    ]:
        ts = (
            pd.Timestamp(f"{day} {hm}", tz="America/New_York")
            .tz_convert("UTC")
            .tz_localize(None)
        )
        rows.append(
            {
                "ticker": ticker,
                "exchange": "US",
                "ts": ts,
                "date": day,
                "session": session,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": vol,
            }
        )
    return rows


def _write(bars_dir: Path, rows: list[dict]) -> None:
    bars_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize("UTC")
    for (t, y), g in df.groupby([df["ticker"], df["date"].str[:4]]):
        g.to_parquet(bars_dir / f"{t}.US_{y}.parquet", index=False)


def _con(prices: list[tuple], splits: list[tuple] | None = None):
    con = duckdb.connect()
    con.register(
        "_p",
        pd.DataFrame(prices, columns=["ticker", "date", "close", "volume"]).assign(
            exchange="US"
        ),
    )
    con.execute("CREATE VIEW prices AS SELECT * FROM _p")
    con.register(
        "_s",
        pd.DataFrame(splits or [], columns=["ticker", "ex_date", "split_ratio"]).assign(
            exchange="US"
        ),
    )
    con.execute("CREATE VIEW splits AS SELECT * FROM _s")
    return con


def _run(tmp_path: Path, rows: list[dict], prices, splits=None):
    bars_dir = tmp_path / "intraday" / "bars_1m"
    _write(bars_dir, rows)
    con = _con(prices, splits)
    days, fix = iv.compute(con, (bars_dir / "*.parquet").as_posix())
    return con, days.set_index(["ticker", "date"]), fix


def _fixed_sum(rows: list[dict], fix: pd.DataFrame, ticker: str, day: str) -> float:
    vendor = sum(
        r["volume"] for r in rows if r["ticker"] == ticker and r["date"] == day
    )
    f = fix[(fix["ticker"] == ticker) & (fix["date"] == day)]
    return vendor + float(f["delta"].sum())


def test_new_regime_nyse_removes_one_report_per_late_bar_and_one_from_the_cross_bar(
    tmp_path: Path,
) -> None:
    x, c = 50_000.0, 700.0  # cross size, other prints in the cross minute
    day = "2024-03-05"
    # cross bar = cross + one report + other; late bars = one report each, one
    # sharing its minute with a 9,000-share block; a quiet post bar at 16:30.
    rows = _day(
        "NYS",
        day,
        {
            "16:00": 2 * x + c,
            "16:10": x + 9_000,
            "16:30": 300.0,
            "18:30": x,
            "19:00": x,
        },
    )
    daily = 5 * RTH + x + c + 9_000 + 300.0
    _con_, days, fix = _run(tmp_path, rows, [("NYS", day, 10.0, daily)])
    d = days.loc[("NYS", day)]
    assert d["regime"] == "new" and d["status"] == "ok"
    assert d["cross_est"] == x and d["n_reports"] == 4 and d["residual"] == 0
    assert _fixed_sum(rows, fix, "NYS", day) == pytest.approx(daily)
    by_minute = {
        pd.Timestamp(t).tz_convert("America/New_York").strftime("%H:%M"): v
        for t, v in zip(fix["ts"], fix["delta"])
    }
    assert by_minute == {"16:00": -x, "16:10": -x, "18:30": -x, "19:00": -x}


@pytest.mark.parametrize("reports", [1, 2])
def test_new_regime_nasdaq_tells_one_report_from_two(
    tmp_path: Path, reports: int
) -> None:
    x, c = 40_000.0, 2_000.0
    day = "2025-09-03"
    rows = _day("NAS", day, {"16:00": (1 + reports) * x + c, "16:20": 100.0})
    daily = 5 * RTH + x + c + 100.0
    _c, days, fix = _run(tmp_path, rows, [("NAS", day, 10.0, daily)])
    d = days.loc[("NAS", day)]
    assert d["status"] == "ok" and d["n_reports"] == reports
    assert d["cross_est"] == pytest.approx(x)
    assert _fixed_sum(rows, fix, "NAS", day) == pytest.approx(daily)


def test_a_delayed_cross_is_fixed_where_it_printed(tmp_path: Path) -> None:
    x = 30_000.0
    day = "2023-06-28"
    rows = _day("DLY", day, {"16:03": 2 * x, "16:10": x, "18:30": x, "19:00": x})
    daily = 5 * RTH + x
    _c, days, fix = _run(tmp_path, rows, [("DLY", day, 10.0, daily)])
    assert days.loc[("DLY", day), "status"] == "ok"
    cross = fix[fix["delta"] == -x]["ts"].min()
    assert (
        pd.Timestamp(cross).tz_convert("America/New_York").strftime("%H:%M") == "16:03"
    )


def test_unexplained_excess_stays_in_residual(tmp_path: Path) -> None:
    x = 30_000.0
    day = "2026-10-02"
    # two equal prints at 17:49 / 18:07 that the daily volume does not count
    rows = _day(
        "CLX",
        day,
        {
            "16:01": 2 * x,
            "16:10": x,
            "17:49": 15_000.0,
            "18:07": 15_000.0,
            "18:30": x,
            "19:00": x,
            **{f"16:2{i}": 100.0 for i in range(10)},  # ordinary post-market prints
        },
    )
    daily = 5 * RTH + x + 1_000.0
    _c, days, fix = _run(tmp_path, rows, [("CLX", day, 10.0, daily)])
    d = days.loc[("CLX", day)]
    assert d["status"] == "ok" and d["n_reports"] == 4
    assert d["residual"] == pytest.approx(30_000.0)
    cross_fix = fix[fix["ts"] == fix["ts"].min()]["delta"].iloc[0]
    assert cross_fix == -x  # one report, not the whole excess


def test_old_regime_puts_the_missing_cross_at_the_close(tmp_path: Path) -> None:
    x = 20_000.0
    rows = _day("OLD", "2019-03-05", {"16:02": 500.0}) + _day(
        "OLD", "2019-03-06", {"16:00": 800.0}
    )
    prices = [
        ("OLD", "2019-03-05", 12.5, 5 * RTH + 500.0 + x),
        ("OLD", "2019-03-06", 12.75, 5 * RTH + 800.0 + x),
    ]
    con, days, fix = _run(tmp_path, rows, prices)
    assert (days["regime"] == "old").all() and (days["status"] == "ok").all()
    assert days["cross_est"].tolist() == [x, x]
    syn = fix[fix["synthetic"]]
    assert (
        len(syn) == 1
        and syn["date"].iloc[0] == "2019-03-05"
        and syn["price"].iloc[0] == 12.5
    )
    real = fix[~fix["synthetic"]]
    assert real["date"].tolist() == ["2019-03-06"] and real["delta"].tolist() == [x]


def test_split_factor_follows_the_data_not_the_split_list(tmp_path: Path) -> None:
    """ADJ's daily volume is adjusted for its later 2:1 split, UNA's is not."""
    x = 10_000.0
    days_ = [f"2019-03-{d:02d}" for d in (4, 5, 6, 7, 8, 11)]
    rows, prices = [], []
    for t, mult in (("ADJ", 2.0), ("UNA", 1.0)):
        for d in days_:
            rows += _day(t, d, {"16:00": 100.0})
            prices.append((t, d, 10.0, (5 * RTH + 100.0 + x) * mult))
    splits = [("ADJ", "2020-01-02", 2.0), ("UNA", "2020-01-02", 2.0)]
    _c, days, _fix = _run(tmp_path, rows, prices, splits)
    assert (days.loc["ADJ", "split_factor"] == 2.0).all()
    assert (days.loc["UNA", "split_factor"] == 1.0).all()
    assert (days["cross_est"] == x).all()


def test_days_that_do_not_reconcile_keep_the_vendor_volume(tmp_path: Path) -> None:
    rows = (
        _day("NOD", "2024-03-05", {"16:00": 5_000.0})  # no daily price
        + _day("LOW", "2024-03-05", {"16:00": 5_000.0})  # bars below daily, new regime
        + _day("HOL", "2019-03-05", {"16:00": 5_000.0})  # old regime, shortfall > half
    )
    prices = [
        ("LOW", "2024-03-05", 10.0, 200_000.0),
        ("HOL", "2019-03-05", 10.0, 300_000.0),
    ]
    _c, days, fix = _run(tmp_path, rows, prices)
    assert days.loc[("NOD", "2024-03-05"), "status"] == "no_daily"
    assert days.loc[("LOW", "2024-03-05"), "status"] == "no_excess"
    assert days.loc[("HOL", "2019-03-05"), "status"] == "unreconciled"
    assert fix.empty


def test_early_close_days_use_the_half_day_close(tmp_path: Path) -> None:
    x = 10_000.0
    day = "2024-11-29"  # day after Thanksgiving, 13:00 close
    rows = _day(
        "HLF", day, {"13:00": 2 * x, "13:10": x, "15:30": x, "17:00": x, "16:00": 50.0}
    )
    daily = 5 * RTH + x + 50.0
    _c, days, fix = _run(tmp_path, rows, [("HLF", day, 10.0, daily)])
    assert days.loc[("HLF", day), "status"] == "ok"
    assert days.loc[("HLF", day), "n_reports"] == 4
    assert _fixed_sum(rows, fix, "HLF", day) == pytest.approx(daily)


def test_bars_1m_adj_serves_consolidated_volume_and_synthetic_close_bars(
    tmp_path: Path,
) -> None:
    import explore_eodhd as ex

    x = 20_000.0
    rows = _day("OLD", "2019-03-05", {"16:02": 500.0}) + _day(
        "NYS", "2024-03-05", {"16:00": 2 * x, "16:10": x, "18:30": x, "19:00": x}
    )
    prices = [
        ("OLD", "2019-03-05", 12.5, 5 * RTH + 500.0 + x),
        ("NYS", "2024-03-05", 10.0, 5 * RTH + x),
    ]
    con, _days, _fix = _run(tmp_path, rows, prices)
    iv.build(tmp_path, con=con)
    assert (tmp_path / "intraday" / iv.FIX_NAME).exists()
    glob = (tmp_path / "intraday" / "bars_1m" / "*.parquet").as_posix()
    con.execute(
        f"CREATE VIEW bars_1m AS SELECT *, 'intraday' AS lane FROM read_parquet('{glob}')"
    )
    assert "bars_1m_adj" in ex.register_intraday_views(con, root=tmp_path)
    got = dict(
        con.execute("SELECT ticker, sum(volume) FROM bars_1m_adj GROUP BY 1").fetchall()
    )
    assert got == {"OLD": 5 * RTH + 500.0 + x, "NYS": 5 * RTH + x}
    vendor = dict(
        con.execute(
            "SELECT ticker, sum(volume_vendor) FROM bars_1m_adj GROUP BY 1"
        ).fetchall()
    )
    assert vendor == {"OLD": 5 * RTH + 500.0, "NYS": 5 * RTH + 5 * x}
    syn = con.execute(
        "SELECT ticker, close, volume, session FROM bars_1m_adj WHERE synthetic"
    ).fetchall()
    assert syn == [("OLD", 12.5, x, "post")]
