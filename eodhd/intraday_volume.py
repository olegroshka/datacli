"""Consolidated volume for the US 1m bars of the ``intraday`` lane.

EODHD's 1m ``volume`` is not the consolidated tape. Checked against the tick
API (``/api/ticks``, which carries the sale conditions) on 2026-11-13:

* Away from the close, a vendor minute equals the minute's ticks to within a
  few shares, and the opening cross is in the 09:30 bar. The opening report
  (sale condition ``Q``, the listing market's "official open", which repeats
  the cross size) is left out, as it should be.
* **The closing cross changed treatment on 2023-06-22** (2023-06-20 and -21
  are mixed):

  - before, the closing cross (``6``) and its reports are missing from the
    bars, so a day's bars sum to about 0.9 of the daily volume and the 16:00
    bar is often absent (the cross was the only print in it);
  - from then on, the bars hold the cross *and* every "official close" report
    (``M``), each of which repeats the cross size: Nasdaq-listed names get two
    in the 16:00 bar (Nasdaq and NYSE Arca), NYSE- and Arca-listed names one
    there plus one each at 16:10, 18:30, 19:00 or 20:00 ET (13:10, 15:30, 17:00
    on half days). A day's bars sum to about 1.3 of the daily volume.

* The daily volume equals the ticks without the ``M`` / ``Q`` reports (AAPL
  2026-11-10: 29,747,255 against 29,756,060), so it is the anchor.

The fix, per US pair-day, makes the bars sum to the daily volume and puts the
difference where the tape has it:

* **new regime**: ``excess = bars - daily``. Report bars after the close
  (the minutes above, at least ``excess / 8`` and 20x the day's median post
  bar outside those minutes) each lose one report, sized as the smallest of
  them (a report repeats the cross; a block can share its minute). The cross
  bar, the largest post bar in the first ``CROSS_WINDOW`` minutes (NYSE
  sometimes prints the cross at 16:03), loses one or two reports: with late
  reports, the whole number that best fits the rest of the excess while the
  bar keeps the cross itself; without (Nasdaq-listed names), all of it, and
  the close bar's size says whether that was one report or two. Excess
  left over (prints the daily volume does not count, e.g. CLS 2026-10-02) stays
  in ``residual``.
* **old regime**: the shortfall ``daily - bars`` goes to the close minute, as a
  bar at the daily close price when the vendor has none (the cross trades at
  the official close). It is mostly the missing cross, but the old bars also
  lack some through-the-day prints (average-price and out-of-sequence
  reports), so on 39 tick-checked days the shortfall exceeded the cross by a
  median 0.6% of the day's volume (p90 5%): ``cross_est`` is an upper bound
  there.

Daily volume is split-adjusted by EODHD, but not always for every split (APH's
2024 split is not in its daily history). The split factor is therefore chosen
from the data: per ticker and stretch between split dates, the prefix product
of the later splits that brings the median ``bars / daily`` closest to its
regime's usual level.

A pair-day is fixed only when it reconciles (``status = 'ok'``): the cross bar
can carry the excess, or the shortfall is under half the daily volume. The
rest keep the vendor volume and say why in ``status``.

Outputs (under <data_root>/intraday/), rebuilt in full by every
``refresh intraday --run`` and by ``--volume-fix``:

    bars_1m_volume_days.parquet  one row per US pair-day: regime, vendor and
                                 daily volume, split factor, excess, cross
                                 estimate, report count, status
    bars_1m_volume_fix.parquet   one row per corrected bar: ts, delta (shares to
                                 add, raw), synthetic (a close bar the vendor
                                 lacks) and its price

``bars_1m_adj`` (explore) applies them: ``volume`` there is consolidated,
``volume_vendor`` is the delivered one. Design: docs/INTRADAY_1M_DESIGN.md
section 6, "Volume".
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

#: First day the vendor's bars hold the closing cross and its reports.
REGIME_SWITCH = date(2023, 6, 22)
#: Mixed days before the switch: decided per pair-day by the sign of the excess.
TRANSITION_FROM = date(2023, 6, 20)
#: Minutes (ET) at which "official close" reports land after the close.
REPORT_MINUTES = ("16:10", "18:30", "19:00", "20:00", "13:10", "15:30", "17:00")
#: The cross bar is the largest post bar this many minutes from the close.
CROSS_WINDOW = 10
#: Usual bars / daily per regime, for choosing the split factor.
LEVEL = {"old": 0.90, "new": 1.30}
#: A report bar is at least this many times the day's median post bar ...
REPORT_TO_MEDIAN = 20
#: ... and at least this share of the day's excess.
REPORT_SHARE = 1 / 8
#: An old-regime shortfall above this share of the daily volume is not a cross.
MAX_SHORTFALL = 0.5
#: Fewest days in a split stretch for the data to choose its factor.
MIN_SEGMENT_DAYS = 5

DAYS_NAME = "bars_1m_volume_days.parquet"
FIX_NAME = "bars_1m_volume_fix.parquet"

DAY_COLUMNS = [
    "ticker",
    "exchange",
    "date",
    "regime",
    "vendor_volume",
    "daily_volume",
    "split_factor",
    "excess",
    "cross_ts",
    "cross_est",
    "n_reports",
    "residual",
    "status",
]
FIX_COLUMNS = ["ticker", "exchange", "ts", "date", "delta", "synthetic", "price"]


def close_calendar(first: date, last: date) -> pd.DataFrame:
    """``date`` (str) -> ``close_hm`` ET for every weekday in ``[first, last]``."""
    from fetch_eodhd_intraday import early_close

    days, d = [], first
    while d <= last:
        if d.weekday() < 5:
            early = early_close("US", d)
            days.append((d.isoformat(), early.strftime("%H:%M") if early else "16:00"))
        d += timedelta(days=1)
    return pd.DataFrame(days, columns=["date", "close_hm"])


def _regime(day: str, vendor: float, daily: float) -> str:
    d = date.fromisoformat(day)
    if d >= REGIME_SWITCH:
        return "new"
    if d >= TRANSITION_FROM and daily > 0 and vendor > daily:
        return "new"
    return "old"


def choose_split_factors(days: pd.DataFrame, splits: pd.DataFrame) -> pd.Series:
    """Per row of ``days`` (ticker, date, regime, vendor_volume, daily_raw), the
    factor that turns the vendor's daily volume into shares traded that day.

    Candidates for a day are 1 and the prefix products, in ex-date order, of the
    splits after it (EODHD adjusts its daily history for a split only when the
    series is refreshed after it). Per ticker and stretch between ex-dates the
    candidate whose median ``bars / daily`` sits closest to the regime level
    wins; stretches with fewer than ``MIN_SEGMENT_DAYS`` usable days take the
    full product, the vendor's documented behaviour.
    """
    factor = pd.Series(1.0, index=days.index)
    if splits.empty or days.empty:
        return factor
    by_ticker = {
        t: g.sort_values("ex_date")[["ex_date", "ratio"]].to_records(index=False)
        for t, g in splits.groupby("ticker")
    }
    for ticker, g in days.groupby("ticker"):
        sp = by_ticker.get(ticker)
        if sp is None or not len(sp):
            continue
        ex = [str(e) for e, _r in sp]
        ratios = [float(r) for _e, r in sp]
        # stretch id = number of ex-dates on or before the day
        stretch = g["date"].map(lambda d: sum(e <= d for e in ex))
        for k, rows in g.groupby(stretch):
            later = ratios[k:]
            if not later:
                continue
            cands = [math.prod(later[: j + 1]) for j in range(len(later))]
            cands = [1.0] + cands
            ok = (rows["daily_raw"] > 0) & (rows["vendor_volume"] > 0)
            use = rows[ok]
            if len(use) < MIN_SEGMENT_DAYS:
                factor.loc[rows.index] = cands[-1]
                continue
            level = use["regime"].map(LEVEL)
            base = (use["vendor_volume"] / use["daily_raw"] / level).map(math.log)
            best = min(cands, key=lambda c: abs((base + math.log(c)).median()))
            factor.loc[rows.index] = best
    return factor


def compute(con: Any, bars_glob: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Day table and fix rows for the US bars under ``bars_glob``.

    ``con`` is a DuckDB connection with ``prices`` and ``splits`` views (as
    ``explore_eodhd.connect()`` gives).
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW _vb AS
        SELECT ticker, exchange, ts, date, session, volume,
               strftime(timezone('America/New_York', ts), '%H:%M') AS hm
        FROM read_parquet('{bars_glob}') WHERE upper(exchange) = 'US'
        """)
    span = con.execute("SELECT min(date), max(date) FROM _vb").fetchone()
    if span[0] is None:
        return pd.DataFrame(columns=DAY_COLUMNS), pd.DataFrame(columns=FIX_COLUMNS)
    cal = close_calendar(date.fromisoformat(span[0]), date.fromisoformat(span[1]))
    minutes = ", ".join(f"'{m}'" for m in REPORT_MINUTES)
    con.register("_cal", cal)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _vd AS
        WITH b AS (
            SELECT _vb.*, c.close_hm,
                   CAST(timezone('America/New_York', CAST(_vb.date || ' ' || c.close_hm AS TIMESTAMP)) AS TIMESTAMPTZ) AS close_ts
            FROM _vb JOIN _cal c USING (date))
        SELECT ticker, exchange, date, any_value(close_ts) AS close_ts,
               sum(volume) AS vendor_volume,
               max(volume) FILTER (WHERE ts = close_ts) AS close_vol,
               arg_max(ts, volume) FILTER (WHERE ts >= close_ts AND ts < close_ts + INTERVAL {CROSS_WINDOW} MINUTE) AS cross_ts,
               max(volume) FILTER (WHERE ts >= close_ts AND ts < close_ts + INTERVAL {CROSS_WINDOW} MINUTE) AS cross_vol,
               median(volume) FILTER (WHERE ts >= close_ts + INTERVAL {CROSS_WINDOW} MINUTE
                                        AND hm NOT IN ({minutes})) AS med_post
        FROM b GROUP BY ticker, exchange, date
        """)
    days = con.execute("""
        WITH p AS (SELECT upper(ticker) AS t, CAST(date AS VARCHAR) AS date,
                          any_value(volume) AS daily_raw, any_value(close) AS daily_close
                   FROM prices WHERE upper(exchange) = 'US' GROUP BY 1, 2)
        SELECT d.*, p.daily_raw, p.daily_close
        FROM _vd d LEFT JOIN p ON p.t = upper(d.ticker) AND p.date = d.date
        """).df()
    splits = con.execute("""
        SELECT DISTINCT upper(ticker) AS ticker, CAST(ex_date AS VARCHAR) AS ex_date, split_ratio AS ratio
        FROM splits WHERE upper(exchange) = 'US' AND split_ratio > 0 AND split_ratio <> 1
        """).df()
    days["ticker_u"] = days["ticker"].str.upper()
    days["regime"] = [
        _regime(d, v, x or 0)
        for d, v, x in zip(
            days["date"], days["vendor_volume"], days["daily_raw"].fillna(0)
        )
    ]
    factor = choose_split_factors(
        days.rename(columns={"ticker": "_t", "ticker_u": "ticker"}), splits
    )
    days["split_factor"] = factor
    days["daily_volume"] = days["daily_raw"] / days["split_factor"]
    # A split factor can move a day across the transition threshold; settle the regime on the final daily.
    days["regime"] = [
        _regime(d, v, x if x == x else 0)
        for d, v, x in zip(days["date"], days["vendor_volume"], days["daily_volume"])
    ]
    days["excess"] = days["vendor_volume"] - days["daily_volume"]

    con.register(
        "_days",
        days[
            ["ticker", "exchange", "date", "regime", "excess", "cross_ts", "med_post"]
        ],
    )
    late = con.execute(f"""
        SELECT b.ticker, b.exchange, b.ts, b.date, b.volume
        FROM _vb b JOIN _days d USING (ticker, exchange, date)
        WHERE d.regime = 'new' AND d.excess > 0 AND b.session = 'post' AND b.hm IN ({minutes})
          AND b.ts > d.cross_ts
          AND b.volume >= greatest({REPORT_TO_MEDIAN} * coalesce(d.med_post, 0), d.excess * {REPORT_SHARE})
          AND b.volume <= d.excess
        """).df()
    lg = (
        late.groupby(["ticker", "exchange", "date"])
        .agg(n_late=("volume", "size"), x_late=("volume", "min"))
        .reset_index()
    )
    days = days.merge(lg, on=["ticker", "exchange", "date"], how="left")
    days["n_late"] = days["n_late"].fillna(0)
    days["late_total"] = days["n_late"] * days["x_late"].fillna(0)

    status, cross_est, n_reports, residual = [], [], [], []
    fixes: list[tuple] = []
    late_by_day = (
        {k: g for k, g in late.groupby(["ticker", "exchange", "date"])}
        if len(late)
        else {}
    )
    for r in days.itertuples(index=False):
        key = (r.ticker, r.exchange, r.date)

        def skip(why: str) -> None:
            status.append(why), cross_est.append(None), n_reports.append(
                0
            ), residual.append(None)

        if not (r.daily_volume == r.daily_volume and r.daily_volume > 0):
            skip("no_daily")
            continue
        if r.regime == "new":
            rest = r.excess - r.late_total
            if r.excess <= 0:
                skip("no_excess")
                continue
            if r.cross_ts is None or r.cross_ts != r.cross_ts or rest <= 0:
                skip("unreconciled")
                continue
            if r.n_late > 0:
                x = r.x_late
                # the cross bar keeps the cross itself, the size of one report
                k_max = min(math.floor(r.cross_vol / x - 1 + 0.05), 2)
                if k_max < 1:
                    skip("unreconciled")
                    continue
                k = min(max(round(rest / x), 1), k_max)
            else:
                # Nasdaq-listed: one or two reports in the cross bar. With two,
                # bar minus excess (cross + other prints) is under the excess.
                k = 1 if r.cross_vol - rest >= rest else 2
                x = rest / k
            close_ph = k * x
            if close_ph > r.cross_vol:
                skip("unreconciled")
                continue
            status.append("ok"), cross_est.append(x), n_reports.append(
                int(r.n_late) + k
            )
            residual.append(r.excess - r.late_total - close_ph)
            fixes.append(
                (r.ticker, r.exchange, r.cross_ts, r.date, -close_ph, False, None)
            )
            g = late_by_day.get(key)
            if g is not None:
                fixes.extend(
                    (r.ticker, r.exchange, ts, r.date, -r.x_late, False, None)
                    for ts in g["ts"]
                )
        else:
            short = -r.excess
            if short < 0 or short > MAX_SHORTFALL * r.daily_volume:
                skip("unreconciled")
                continue
            status.append("ok"), cross_est.append(short), n_reports.append(
                0
            ), residual.append(0.0)
            synthetic = not (r.close_vol == r.close_vol and r.close_vol is not None)
            fixes.append(
                (
                    r.ticker,
                    r.exchange,
                    r.close_ts,
                    r.date,
                    short,
                    synthetic,
                    r.daily_close if synthetic else None,
                )
            )
    days["status"] = status
    days["cross_est"] = cross_est
    days["n_reports"] = n_reports
    days["residual"] = residual
    days["cross_ts"] = days["cross_ts"].where(days["regime"] == "new", days["close_ts"])
    fix = pd.DataFrame(fixes, columns=FIX_COLUMNS)
    if len(fix):
        fix["ts"] = pd.to_datetime(fix["ts"], utc=True)
        fix = fix.groupby(["ticker", "exchange", "ts", "date"], as_index=False).agg(
            delta=("delta", "sum"),
            synthetic=("synthetic", "max"),
            price=("price", "first"),
        )
    return days[DAY_COLUMNS].reset_index(drop=True), fix[FIX_COLUMNS]


def build(raw_root: Path, con: Any | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Recompute and write both tables under ``raw_root/intraday``."""
    import _atomic

    if con is None:
        import explore_eodhd

        con = explore_eodhd.connect()
    lane = Path(raw_root) / "intraday"
    glob = (lane / "bars_1m" / "*.US_*.parquet").as_posix()
    if not list((lane / "bars_1m").glob("*.US_*.parquet")):
        return pd.DataFrame(columns=DAY_COLUMNS), pd.DataFrame(columns=FIX_COLUMNS)
    days, fix = compute(con, glob)
    _atomic.to_parquet(days, lane / DAYS_NAME, index=False)
    _atomic.to_parquet(fix, lane / FIX_NAME, index=False)
    return days, fix


def summary(days: pd.DataFrame) -> str:
    if days.empty:
        return "volume fix: no US bars"
    counts = days["status"].value_counts().to_dict()
    parts = ", ".join(f"{k} {v:,}" for k, v in sorted(counts.items()))
    return f"volume fix: {len(days):,} US pair-days ({parts})"
