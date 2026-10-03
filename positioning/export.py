"""Inputs for the cost-realism backtest in btest (DD-002 WP15, ADR-001 D1).

btest consumes two files written under ``<positioning root>/exports/btest/``:

- ``prices_daily.parquet``: the long-format daily panel btest's ``parquet://``
  source reads (``date`` in UTC, ``ticker``, ``close``, ``open``, ``high``,
  ``low``, ``volume``, ``sector``), split-and-dividend adjusted (every price
  scaled by ``adjusted_close / close``), for the symbols of the short ladder
  whose median dollar volume since ``start`` clears ``min_dollar_adv``.
- ``short_z_inventory.parquet``: a wide frame (``date`` index, one column per
  ticker) with EVAL-001's working feature, the per-symbol EWM z-score of log
  short inventory residualised per date on 21-day reversal and 12-1 momentum
  (the ``factors`` cleaning), carried forward daily from each report's
  ``published_at`` to the day before the next one. A reader on any day sees
  the latest value published strictly before that day: point in time.
  ``short_z_inventory_raw.parquet`` holds the unresidualised z-score for the
  ``none`` variant.

Nothing here is a signal decision; the orientation (SPEC: heavy or growing
short inventory is bearish) is applied in the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from positioning import evaluation as ev

SUBDIR = Path("exports") / "btest"
PRICES_FILE = "prices_daily.parquet"
SIGNAL_FILE = "short_z_inventory.parquet"
SIGNAL_RAW_FILE = "short_z_inventory_raw.parquet"
DEFAULT_START = "2018-01-01"
DEFAULT_MIN_DOLLAR_ADV = 5_000_000.0
LANES: tuple[str, ...] = ("us_common", "us_extended")


@dataclass(frozen=True)
class ExportReport:
    directory: Path
    symbols: int
    price_rows: int
    signal_dates: int
    signal_symbols: int
    first_date: str
    last_date: str


def carry_forward_wide(
    observations: pd.DataFrame,
    *,
    symbol: str = "symbol",
    published: str = "published_at",
    value: str = "value",
    dates: Sequence[Any],
) -> pd.DataFrame:
    """Pivot ``(symbol, published_at, value)`` rows to a daily wide frame over ``dates``.

    The value published on day ``p`` is visible from the first date **after**
    ``p`` (a report published after the close is not tradable that day) until
    the day before the next publication; before a symbol's first publication
    it is NaN. Duplicate publications on one day keep the last.
    """
    index = pd.DatetimeIndex(pd.to_datetime(list(dates))).sort_values().unique()
    frame = observations[[symbol, published, value]].copy()
    frame[published] = pd.to_datetime(frame[published])
    frame = frame.sort_values([symbol, published]).drop_duplicates([symbol, published], keep="last")
    wide = frame.pivot(index=published, columns=symbol, values=value).sort_index()
    # shift every publication to the next date of the trading index, then carry forward
    positions = index.searchsorted(wide.index.to_numpy(), side="right")
    keep = positions < len(index)
    wide = wide[keep]
    wide.index = index[positions[keep]]
    wide = wide.groupby(level=0).last()
    return wide.reindex(index).ffill()


def universe(con: Any, *, start: str, min_dollar_adv: float, lanes: Sequence[str] = LANES) -> list[str]:
    """Short-ladder symbols whose median daily dollar volume since ``start`` clears the bar."""
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    rows = con.execute(f"""
        WITH u AS (SELECT DISTINCT eodhd_code FROM positioning_short_ladder WHERE lane IN ({lane_list}))
        SELECT p.ticker FROM prices p JOIN u ON u.eodhd_code = p.ticker
        WHERE p.lane IN ({lane_list}) AND CAST(p.date AS DATE) >= DATE '{start}'
          AND p.close > 0.0001 AND p.close < 999999
        GROUP BY p.ticker HAVING median(p.close * p.volume) >= {float(min_dollar_adv)}
        ORDER BY p.ticker
        """).fetchall()
    return [r[0] for r in rows]


def price_panel(con: Any, symbols: Sequence[str], *, start: str, lanes: Sequence[str] = LANES) -> pd.DataFrame:
    """btest's long daily panel, adjusted for splits and dividends, with the vendor's sector."""
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    con.register("_export_symbols", pd.DataFrame({"ticker": list(symbols)}))
    try:
        frame = con.execute(f"""
            WITH sec AS (
              SELECT ticker, any_value(sector) AS sector FROM positioning_factors
              WHERE sector IS NOT NULL GROUP BY ticker)
            SELECT CAST(p.date AS DATE) AS date, p.ticker,
                   p.adjusted_close AS close,
                   p.open * p.adjusted_close / p.close AS open,
                   p.high * p.adjusted_close / p.close AS high,
                   p.low * p.adjusted_close / p.close AS low,
                   CAST(coalesce(p.volume, 0) AS BIGINT) AS volume,
                   sec.sector
            FROM prices p JOIN _export_symbols s USING (ticker)
            LEFT JOIN sec USING (ticker)
            WHERE p.lane IN ({lane_list}) AND CAST(p.date AS DATE) >= DATE '{start}'
              AND p.close > 0.0001 AND p.close < 999999 AND p.adjusted_close > 0
            ORDER BY p.ticker, date
            """).df()
    finally:
        con.unregister("_export_symbols")
    frame["date"] = pd.to_datetime(frame["date"]).dt.tz_localize("UTC")
    return frame


def signal_observations(con: Any, symbols: Sequence[str]) -> pd.DataFrame:
    """EVAL-001's panel restricted to ``symbols`` with the z-score and its residual."""
    panel = ev.load_panel(con, horizons=(10,))
    panel = panel[panel["symbol"].isin(set(symbols))]
    panel = panel[panel["seed_share"] < 0.5]
    frame = ev.add_features(panel)
    frame["z_inventory_resid"] = ev.residualise(frame, "z_inventory", ("reversal_21d", "momentum_12_1"))
    return frame[["symbol", "published_at", "z_inventory", "z_inventory_resid"]]


def export(
    con: Any,
    root: Path,
    *,
    start: str = DEFAULT_START,
    min_dollar_adv: float = DEFAULT_MIN_DOLLAR_ADV,
) -> ExportReport:
    """Write the two files; returns what was written."""
    directory = Path(root) / SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    symbols = universe(con, start=start, min_dollar_adv=min_dollar_adv)
    prices = price_panel(con, symbols, start=start)
    prices.to_parquet(directory / PRICES_FILE, index=False)
    dates = prices["date"].dt.tz_localize(None).drop_duplicates().sort_values()
    observations = signal_observations(con, symbols)
    resid = carry_forward_wide(observations, value="z_inventory_resid", dates=dates)
    raw = carry_forward_wide(observations, value="z_inventory", dates=dates)
    resid.to_parquet(directory / SIGNAL_FILE)
    raw.to_parquet(directory / SIGNAL_RAW_FILE)
    return ExportReport(
        directory=directory,
        symbols=len(symbols),
        price_rows=int(len(prices)),
        signal_dates=int(len(resid)),
        signal_symbols=int(resid.notna().any().sum()),
        first_date=str(dates.min().date()) if len(dates) else "",
        last_date=str(dates.max().date()) if len(dates) else "",
    )
