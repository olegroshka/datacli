"""Inputs for the cost-realism backtest in btest (DD-002 WP15, ADR-001 D1).

btest consumes two files written under ``<positioning root>/exports/btest/``:

- ``prices_daily.parquet``: the long-format daily panel btest's ``parquet://``
  source reads (``date`` in UTC, ``ticker``, ``close``, ``open``, ``high``,
  ``low``, ``volume``, ``sector``), split-and-dividend adjusted (every price
  scaled by ``adjusted_close / close``), for the symbols of the short ladder
  whose median dollar volume since ``start`` clears ``min_dollar_adv`` and
  whose vendor bars never fail the price-quality rules (WP14,
  ``factors.bad_symbols``); dates with fewer than half the median number of
  bars (vendor rows on exchange holidays) are dropped.
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
from positioning import factors

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


def trading_days(frame: pd.DataFrame, *, date: str = "date", min_share: float = 0.5) -> pd.DataFrame:
    """Keep the dates on which at least ``min_share`` of the median number of symbols have a bar.

    The vendor lists a few bars on exchange holidays (14 symbols on
    2020-09-07, Labor Day); a portfolio marked on them sees a phantom day.
    """
    counts = frame.groupby(date).size()
    keep = counts[counts >= min_share * counts.median()].index
    return frame[frame[date].isin(keep)]


def universe(
    con: Any,
    *,
    start: str,
    min_dollar_adv: float,
    lanes: Sequence[str] = LANES,
    ladder: str = "positioning_short_ladder",
) -> list[str]:
    """``ladder``'s symbols whose median daily dollar volume since ``start`` clears the bar
    and whose vendor bars never fail the price-quality rules since ``start``."""
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    rows = con.execute(f"""
        WITH u AS (SELECT DISTINCT eodhd_code FROM {ladder} WHERE lane IN ({lane_list}) AND eodhd_code IS NOT NULL)
        SELECT p.ticker FROM prices p JOIN u ON u.eodhd_code = p.ticker
        WHERE p.lane IN ({lane_list}) AND CAST(p.date AS DATE) >= DATE '{start}'
          AND p.close > 0.0001 AND p.close < 999999
        GROUP BY p.ticker HAVING median(p.close * p.volume) >= {float(min_dollar_adv)}
        ORDER BY p.ticker
        """).fetchall()
    bad = factors.bad_symbols(con, lanes=lanes)
    if not bad.empty:
        bad = bad[pd.to_datetime(bad["last_bad"]) >= pd.Timestamp(start)]
    excluded = set(bad["ticker"]) if not bad.empty else set()
    return [r[0] for r in rows if r[0] not in excluded]


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
    frame["date"] = pd.to_datetime(frame["date"])
    frame = trading_days(frame).reset_index(drop=True)
    frame["date"] = frame["date"].dt.tz_localize("UTC")
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


# --- WP15, second iteration ---------------------------------------------------
#
# More inputs under the same directory, written by ``export_second``. Every
# signal file of this iteration holds a **demeaned per-date rank** in
# [-0.5, 0.5] (``rank_unit / 2`` over the evaluation cross-section at each
# publication), so that btest can weight a book by the score itself (rank
# weights, Grinold's linear tilt) rather than by membership of a decile; the
# ``_neg`` companion is the negated frame, because btest's ``SignalWeight``
# needs a positive score for each book.
#
# - ``short_z_inventory_size.parquet`` (+ = bearish): EVAL-001's z-score
#   residualised per date on reversal, momentum **and log market
#   capitalisation** (shares outstanding times the close at publication).
# - ``prices_daily_long.parquet``, ``long_flow_cohort.parquet`` (+ = bullish),
#   ``long_level_cohort.parquet`` (+ = bullish), ``long_size_tercile.parquet``:
#   EVAL-003's headline features on the hedge-fund cohort (flow residualised
#   on reversal, momentum, ownership share and size; ownership share on
#   reversal, momentum and size), carried forward from the day after each
#   period's ``published_at``, and the per-period size tercile of the
#   evaluation universe (0 = smallest third), for the long-ladder symbols that
#   clear the liquidity bar.
# - ``borrow_rates.parquet``: the latest Interactive Brokers snapshot (WP6),
#   ``ticker`` and ``fee_rate`` as a fraction a year, for the overlay in
#   ``positioning.btest_results`` (btest charges one flat rate).

SHORT_SIZE_FILE = "short_z_inventory_size.parquet"
LONG_PRICES_FILE = "prices_daily_long.parquet"
LONG_FLOW_FILE = "long_flow_cohort.parquet"
LONG_LEVEL_FILE = "long_level_cohort.parquet"
LONG_TERCILE_FILE = "long_size_tercile.parquet"
BORROW_FILE = "borrow_rates.parquet"
LONG_AGGREGATE = "cohort"


@dataclass(frozen=True)
class SecondReport:
    directory: Path
    short_size_symbols: int
    long_symbols: int
    long_price_rows: int
    long_signal_dates: int
    long_flow_symbols: int
    borrow_symbols: int
    borrow_snapshot: str


def negated_name(file: str) -> str:
    """``x.parquet`` -> ``x_neg.parquet``."""
    stem, dot, ext = file.rpartition(".")
    return f"{stem}_neg{dot}{ext}"


def write_pair(wide: pd.DataFrame, directory: Path, file: str) -> None:
    """Write the frame and its negation (btest weights each book by a positive score)."""
    wide.to_parquet(directory / file)
    (-wide).to_parquet(directory / negated_name(file))


def demeaned_rank(frame: pd.DataFrame, column: str, *, key: str = "date") -> pd.Series:
    """Per-``key`` rank mapped to [-0.5, 0.5]; missing where the group has fewer than two values."""
    from positioning import evaluation_long as evl

    return evl.rank_unit_by(frame, key, column) / 2.0


def log_mcap_at_publication(con: Any, observations: pd.DataFrame, *, lanes: Sequence[str] = LANES) -> pd.DataFrame:
    """``(symbol, published_at, log_mcap)``: shares outstanding (published before) times the close on or before."""
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    obs = observations[["symbol", "published_at"]].drop_duplicates().copy()
    obs["published_at"] = pd.to_datetime(obs["published_at"]).dt.date
    con.register("_export_obs", obs)
    try:
        out = con.execute(f"""
            WITH so AS (
              SELECT eodhd_code, published_at, shares_outstanding FROM finra_short_interest_float
              WHERE shares_outstanding > 0
            ), px AS (
              SELECT ticker, CAST(date AS DATE) AS date, close FROM prices
              WHERE lane IN ({lane_list}) AND close > 0.0001 AND close < 999999
            )
            SELECT o.symbol, o.published_at, ln(s.shares_outstanding * p.close) AS log_mcap
            FROM _export_obs o
            ASOF LEFT JOIN so s ON s.eodhd_code = o.symbol AND s.published_at < o.published_at
            ASOF LEFT JOIN px p ON p.ticker = o.symbol AND p.date <= o.published_at
            """).df()
    finally:
        con.unregister("_export_obs")
    out["published_at"] = pd.to_datetime(out["published_at"])
    return out


def short_size_observations(con: Any, symbols: Sequence[str]) -> pd.DataFrame:
    """EVAL-001's panel with the z-score residualised on reversal, momentum and size, as a demeaned rank."""
    panel = ev.load_panel(con, horizons=(10,))
    panel = panel[panel["symbol"].isin(set(symbols))]
    panel = panel[panel["seed_share"] < 0.5]
    frame = ev.add_features(panel)
    frame["published_at"] = pd.to_datetime(frame["published_at"])
    size = log_mcap_at_publication(con, frame)
    frame = frame.merge(size, on=["symbol", "published_at"], how="left")
    frame["resid"] = ev.residualise(frame, "z_inventory", ("reversal_21d", "momentum_12_1", "log_mcap"))
    frame["z_inventory_size"] = demeaned_rank(frame, "resid")
    return frame[["symbol", "published_at", "z_inventory_size"]]


def long_observations(con: Any, symbols: Sequence[str], *, aggregate: str = LONG_AGGREGATE) -> pd.DataFrame:
    """EVAL-003's prepared frame for one aggregate: the two headline residuals as demeaned ranks, the size tercile.

    The residuals, ranks and terciles are taken on the whole evaluation
    universe (as in EVAL-003), then the rows are restricted to ``symbols``.
    """
    from positioning import evaluation_long as evl
    from positioning import evaluation_ownership as evo

    frame = evo.prepare(evl.load_panel(con), aggregate)
    frame["flow_r"] = ev.residualise(frame, "flow", ("reversal_21d", "momentum_12_1", "level", "log_mcap"))
    frame["level_r"] = ev.residualise(frame, "level", ("reversal_21d", "momentum_12_1", "log_mcap"))
    frame["flow_resid"] = demeaned_rank(frame, "flow_r")
    frame["level_resid"] = demeaned_rank(frame, "level_r")
    frame["size_tercile"] = evo.size_tercile(frame)
    frame = frame[frame["eodhd_code"].isin(set(symbols))]
    out = frame[["eodhd_code", "published_at", "flow_resid", "level_resid", "size_tercile"]].rename(columns={"eodhd_code": "symbol"})
    out["published_at"] = pd.to_datetime(out["published_at"])
    return out


def borrow_rates(con: Any) -> pd.DataFrame:
    """``(snapshot_date, ticker, fee_rate)`` of the latest borrow snapshot, rates as a fraction a year."""
    return con.execute("""
        SELECT snapshot_date, eodhd_code AS ticker, fee_rate / 100.0 AS fee_rate
        FROM borrow_us
        WHERE snapshot_date = (SELECT max(snapshot_date) FROM borrow_us)
          AND eodhd_code IS NOT NULL AND fee_rate IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY eodhd_code ORDER BY available DESC NULLS LAST) = 1
        ORDER BY ticker
        """).df()


def export_second(
    con: Any,
    root: Path,
    *,
    start: str = DEFAULT_START,
    min_dollar_adv: float = DEFAULT_MIN_DOLLAR_ADV,
    with_borrow: bool = True,
) -> SecondReport:
    """Write the second iteration's files next to the first's (which must exist)."""
    directory = Path(root) / SUBDIR
    prices = pd.read_parquet(directory / PRICES_FILE, columns=["date", "ticker"])
    dates = prices["date"].dt.tz_localize(None).drop_duplicates().sort_values()
    short_symbols = sorted(prices["ticker"].unique())
    short_obs = short_size_observations(con, short_symbols)
    short_wide = carry_forward_wide(short_obs, value="z_inventory_size", dates=dates)
    write_pair(short_wide, directory, SHORT_SIZE_FILE)

    long_symbols = universe(con, start=start, min_dollar_adv=min_dollar_adv, ladder="positioning_long_ladder")
    long_prices = price_panel(con, long_symbols, start=start)
    long_prices.to_parquet(directory / LONG_PRICES_FILE, index=False)
    long_dates = long_prices["date"].dt.tz_localize(None).drop_duplicates().sort_values()
    long_obs = long_observations(con, long_symbols)
    flow = carry_forward_wide(long_obs, value="flow_resid", dates=long_dates)
    write_pair(flow, directory, LONG_FLOW_FILE)
    write_pair(carry_forward_wide(long_obs, value="level_resid", dates=long_dates), directory, LONG_LEVEL_FILE)
    carry_forward_wide(long_obs, value="size_tercile", dates=long_dates).to_parquet(directory / LONG_TERCILE_FILE)

    borrow = pd.DataFrame(columns=["snapshot_date", "ticker", "fee_rate"])
    if with_borrow:
        borrow = borrow_rates(con)
        borrow.to_parquet(directory / BORROW_FILE, index=False)
    return SecondReport(
        directory=directory,
        short_size_symbols=int(short_wide.notna().any().sum()),
        long_symbols=len(long_symbols),
        long_price_rows=int(len(long_prices)),
        long_signal_dates=int(len(flow)),
        long_flow_symbols=int(flow.notna().any().sum()),
        borrow_symbols=int(len(borrow)),
        borrow_snapshot=str(borrow["snapshot_date"].iloc[0]) if len(borrow) else "",
    )
