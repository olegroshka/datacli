"""Style exposures and specific risk as a DuckDB view (INV-002 R8, R9; ADR-001 D13).

``positioning_factors`` has one row per ``(ticker, date)`` for the US stock
lanes (``us_common`` and ``us_extended``, which includes delisted names). Every column on date ``d`` uses closes up to ``d`` and reports published
strictly before ``d``, so the row is known after the close of ``d``:

- ``ret_1d``            close-to-close total return (adjusted close)
- ``reversal_21d``      trailing 21-session return
- ``momentum_12_1``     return from 252 to 21 sessions ago (skips the last month)
- ``specific_risk_63d`` annualised volatility, over the last 63 sessions (at
                        least 40), of the return in excess of the same-day median
                        of the stock's sector (the market when the sector has
                        fewer than 5 names or is unknown)
- ``short_interest_ratio`` latest short interest over shares outstanding with
                        ``published_at < d`` (``finra_short_interest_float``)
- ``price_quality_flag`` true when the bar fails a vendor-quality rule (a
                        non-positive price, a high below the open, close or
                        low, a low above them, a one-day absolute return above
                        ``MAX_ABS_RETURN``) or when such a bar lies inside the
                        trailing 252 sessions the factors are computed over
                        (DD-002 WP14); filter on it in SQL, the view keeps the
                        rows

It is a view, not a stored dataset: it is a pure function of the current price
and short interest stores and takes about a second to evaluate. The vendor
re-rounds adjusted closes after every dividend, so stored values would differ
from recomputed ones in the fifth decimal without any real restatement.
Exposures are raw; cross-sectional standardisation belongs to the consumer.
Sector is the vendor's current classification, not point-in-time.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # pragma: no cover
    import pandas as pd

VIEW = "positioning_factors"
LANE = "us_common"  # where the sector metadata lives
LANES: tuple[str, ...] = ("us_common", "us_extended")
WARMUP_START = "2016-12-01"
FIRST_DATE = "2018-01-01"
REVERSAL_SESSIONS = 21
MOMENTUM_SESSIONS = 252
RISK_SESSIONS = 63
RISK_MIN_SESSIONS = 40
MIN_SECTOR_NAMES = 5
SI_VIEW = "finra_short_interest_float"
#: A one-day absolute return above this is a vendor tick, not a price (DD-002 WP14).
MAX_ABS_RETURN = 3.0
#: Vendor placeholder closes seen in the data (KB-003).
PLACEHOLDER_LOW = 0.0001
PLACEHOLDER_HIGH = 999999.0
#: The predicate, over a ``prices`` row, of a bar that fails the vendor-quality rules
#: (the full form needs open, high and low; the close-only form serves a surface without them).
BAD_BAR_SQL = (
    "(coalesce(open <= 0, false) OR coalesce(high <= 0, false) OR coalesce(low <= 0, false) "
    "OR coalesce(close <= 0, false) OR coalesce(adjusted_close <= 0, false) "
    f"OR coalesce(close <= {PLACEHOLDER_LOW}, false) OR coalesce(close >= {PLACEHOLDER_HIGH}, false) "
    "OR coalesce(high < greatest(open, close, low), false) OR coalesce(low > least(open, close, high), false))"
)
OHLC_COLUMNS: tuple[str, ...] = ("open", "high", "low")
#: Calendar days a failing bar taints after itself: the 252-session momentum window.
TAINT_DAYS = 366


def _price_columns(con: Any) -> set[str]:
    return {
        r[0]
        for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'prices'"
        ).fetchall()
    }


def bad_bar_sql(con: Any) -> str:
    """The failing-bar predicate built from the columns the ``prices`` surface on ``con`` has."""
    columns = _price_columns(con)
    parts = [
        "coalesce(close <= 0, false)",
        f"coalesce(close <= {PLACEHOLDER_LOW}, false)",
        f"coalesce(close >= {PLACEHOLDER_HIGH}, false)",
    ]
    if "adjusted_close" in columns:
        parts.append("coalesce(adjusted_close <= 0, false)")
    if all(c in columns for c in OHLC_COLUMNS):
        parts += [
            "coalesce(open <= 0, false)",
            "coalesce(high <= 0, false)",
            "coalesce(low <= 0, false)",
            "coalesce(high < greatest(open, close, low), false)",
            "coalesce(low > least(open, close, high), false)",
        ]
    return "(" + " OR ".join(parts) + ")"


def _has(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def _sector_source(eodhd_root: Path | None) -> str:
    """SQL for ``(ticker, sector)``; an empty relation when the metadata is absent."""
    if eodhd_root is not None:
        path = Path(eodhd_root) / LANE / "firm_metadata.parquet"
        if path.exists():
            return (
                "SELECT ticker, any_value(sector) AS sector "
                f"FROM read_parquet('{path.as_posix()}') "
                "WHERE sector IS NOT NULL AND sector <> '' GROUP BY ticker"
            )
    return "SELECT NULL::VARCHAR AS ticker, NULL::VARCHAR AS sector WHERE false"


def view_sql(*, sector_sql: str, with_short_interest: bool, bad_bar: str = BAD_BAR_SQL) -> str:
    short_join = (
        f"ASOF LEFT JOIN {SI_VIEW} s ON s.eodhd_code = x.ticker AND s.published_at < x.date"
        if with_short_interest
        else ""
    )
    short_cols = (
        "s.short_over_outstanding AS short_interest_ratio, s.published_at AS si_published_at"
        if with_short_interest
        else "NULL::DOUBLE AS short_interest_ratio, NULL::DATE AS si_published_at"
    )
    lanes = ", ".join(f"'{lane}'" for lane in LANES)
    return f"""
    WITH bad AS (
      SELECT ticker, CAST(date AS DATE) AS date, bool_or({bad_bar}) AS bad_bar
      FROM prices WHERE lane IN ({lanes}) AND CAST(date AS DATE) >= DATE '{WARMUP_START}'
      GROUP BY ticker, CAST(date AS DATE)
    ), px AS (
      SELECT ticker, any_value(lane) OVER (PARTITION BY ticker) AS lane,
             CAST(date AS DATE) AS date, adjusted_close AS a
      FROM prices
      WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > {PLACEHOLDER_LOW} AND close < {PLACEHOLDER_HIGH}
        AND CAST(date AS DATE) >= DATE '{WARMUP_START}'
    ), r0 AS (
      SELECT ticker, lane, date,
             a / lag(a) OVER w - 1 AS ret_1d,
             a / lag(a, {REVERSAL_SESSIONS}) OVER w - 1 AS reversal_21d,
             lag(a, {REVERSAL_SESSIONS}) OVER w / lag(a, {MOMENTUM_SESSIONS}) OVER w - 1
               AS momentum_12_1
      FROM px WINDOW w AS (PARTITION BY ticker ORDER BY date)
    ), bad_dates AS (
      SELECT ticker, date FROM bad WHERE bad_bar
      UNION SELECT ticker, date FROM r0 WHERE abs(ret_1d) > {MAX_ABS_RETURN}
    ), r AS (
      SELECT r0.ticker, r0.lane, r0.date, r0.ret_1d, r0.reversal_21d, r0.momentum_12_1,
             coalesce(b.date >= r0.date - {TAINT_DAYS}, false) AS price_quality_flag
      FROM r0 ASOF LEFT JOIN bad_dates b ON b.ticker = r0.ticker AND b.date <= r0.date
    ), sec AS ({sector_sql}),
    g AS (
      SELECT r.*, sec.sector,
             median(ret_1d) OVER (PARTITION BY r.date) AS market_ret,
             median(ret_1d) OVER (PARTITION BY r.date, sec.sector) AS sector_ret,
             count(ret_1d) OVER (PARTITION BY r.date, sec.sector) AS sector_n
      FROM r LEFT JOIN sec ON sec.ticker = r.ticker
    ), e AS (
      SELECT *, ret_1d - CASE WHEN sector IS NOT NULL AND sector_n >= {MIN_SECTOR_NAMES}
                              THEN sector_ret ELSE market_ret END AS excess
      FROM g
    ), x AS (
      SELECT ticker, lane, date, ret_1d, reversal_21d, momentum_12_1, sector, price_quality_flag,
             CASE WHEN count(excess) OVER v >= {RISK_MIN_SESSIONS}
                  THEN stddev_samp(excess) OVER v * sqrt(252) END AS specific_risk_63d
      FROM e
      WINDOW v AS (PARTITION BY ticker ORDER BY date
                   ROWS BETWEEN {RISK_SESSIONS - 1} PRECEDING AND CURRENT ROW)
    )
    SELECT x.ticker, x.lane, x.date, x.ret_1d, x.reversal_21d, x.momentum_12_1,
           x.specific_risk_63d, x.sector, {short_cols}, x.price_quality_flag
    FROM x {short_join}
    WHERE x.date >= DATE '{FIRST_DATE}'
    """


def register(con: Any, *, eodhd_root: Path | None = None) -> bool:
    """Create ``positioning_factors`` when the ``prices`` view exists."""
    if not _has(con, "prices"):
        return False
    sql = view_sql(
        sector_sql=_sector_source(eodhd_root),
        with_short_interest=_has(con, SI_VIEW),
        bad_bar=bad_bar_sql(con),
    )
    con.execute(f"CREATE OR REPLACE VIEW {VIEW} AS {sql}")
    return True


def bad_symbols_sql(
    lanes: Sequence[str] = LANES, *, bad_bar: str = BAD_BAR_SQL, price_col: str = "adjusted_close"
) -> str:
    """SQL listing, per ticker, how many bars fail the vendor-quality rules (DD-002 WP14).

    Columns: ``ticker, lane, bad_bars, wild_returns, first_bad, last_bad``; one
    row per ticker with at least one failing bar or one-day absolute return
    above ``MAX_ABS_RETURN``.
    """
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    return f"""
    WITH px AS (
      SELECT ticker, lane, CAST(date AS DATE) AS date, {price_col} AS a, {bad_bar} AS bad_bar
      FROM prices WHERE lane IN ({lane_list})
    ), good AS (
      SELECT ticker, date, a / lag(a) OVER (PARTITION BY ticker ORDER BY date) - 1 AS ret_1d
      FROM px WHERE NOT bad_bar AND a > 0
    ), r AS (
      SELECT px.*, g.ret_1d FROM px LEFT JOIN good g USING (ticker, date)
    )
    SELECT ticker, any_value(lane) AS lane,
           count(*) FILTER (WHERE bad_bar) AS bad_bars,
           count(*) FILTER (WHERE abs(ret_1d) > {MAX_ABS_RETURN}) AS wild_returns,
           min(date) FILTER (WHERE bad_bar OR abs(ret_1d) > {MAX_ABS_RETURN}) AS first_bad,
           max(date) FILTER (WHERE bad_bar OR abs(ret_1d) > {MAX_ABS_RETURN}) AS last_bad
    FROM r GROUP BY ticker
    HAVING bad_bars > 0 OR wild_returns > 0
    """


def bad_symbols(con: Any, lanes: Sequence[str] = LANES) -> "pd.DataFrame":
    """The symbols ``bad_symbols_sql`` lists, sorted by ticker (empty without ``prices``)."""
    import pandas as pd

    if not _has(con, "prices"):
        return pd.DataFrame(columns=["ticker", "lane", "bad_bars", "wild_returns", "first_bad", "last_bad"])
    price_col = "adjusted_close" if "adjusted_close" in _price_columns(con) else "close"
    sql = bad_symbols_sql(lanes, bad_bar=bad_bar_sql(con), price_col=price_col)
    return con.execute(sql).df().sort_values("ticker").reset_index(drop=True)


def schema_snippet() -> str:
    return "\n".join(
        [
            f"- {VIEW}(ticker, lane, date, ret_1d, reversal_21d, momentum_12_1, specific_risk_63d, "
            "sector, short_interest_ratio, si_published_at, price_quality_flag)",
            "  [US stocks (lanes us_common and us_extended, delisted included), daily from 2018: trailing 21-session return, 12-1 momentum, annualised",
            "  63-session volatility of the sector-excess return, latest short interest over shares",
            "  outstanding published before the date; raw values, known after the close of `date`;",
            "  price_quality_flag = a vendor-quality failure (non-positive or inconsistent bar, a",
            "  one-day move above 300 percent) on the day or inside the trailing year: filter it out;",
            "  computed on the fly, so always filter by date or ticker]",
        ]
    )
