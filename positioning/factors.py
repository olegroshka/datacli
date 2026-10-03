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

It is a view, not a stored dataset: it is a pure function of the current price
and short interest stores and takes about a second to evaluate. The vendor
re-rounds adjusted closes after every dividend, so stored values would differ
from recomputed ones in the fifth decimal without any real restatement.
Exposures are raw; cross-sectional standardisation belongs to the consumer.
Sector is the vendor's current classification, not point-in-time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def view_sql(*, sector_sql: str, with_short_interest: bool) -> str:
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
    WITH px AS (
      SELECT ticker, any_value(lane) OVER (PARTITION BY ticker) AS lane,
             CAST(date AS DATE) AS date, adjusted_close AS a
      FROM prices
      WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
        AND CAST(date AS DATE) >= DATE '{WARMUP_START}'
    ), r AS (
      SELECT ticker, lane, date,
             a / lag(a) OVER w - 1 AS ret_1d,
             a / lag(a, {REVERSAL_SESSIONS}) OVER w - 1 AS reversal_21d,
             lag(a, {REVERSAL_SESSIONS}) OVER w / lag(a, {MOMENTUM_SESSIONS}) OVER w - 1
               AS momentum_12_1
      FROM px WINDOW w AS (PARTITION BY ticker ORDER BY date)
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
      SELECT ticker, lane, date, ret_1d, reversal_21d, momentum_12_1, sector,
             CASE WHEN count(excess) OVER v >= {RISK_MIN_SESSIONS}
                  THEN stddev_samp(excess) OVER v * sqrt(252) END AS specific_risk_63d
      FROM e
      WINDOW v AS (PARTITION BY ticker ORDER BY date
                   ROWS BETWEEN {RISK_SESSIONS - 1} PRECEDING AND CURRENT ROW)
    )
    SELECT x.ticker, x.lane, x.date, x.ret_1d, x.reversal_21d, x.momentum_12_1,
           x.specific_risk_63d, x.sector, {short_cols}
    FROM x {short_join}
    WHERE x.date >= DATE '{FIRST_DATE}'
    """


def register(con: Any, *, eodhd_root: Path | None = None) -> bool:
    """Create ``positioning_factors`` when the ``prices`` view exists."""
    if not _has(con, "prices"):
        return False
    sql = view_sql(
        sector_sql=_sector_source(eodhd_root), with_short_interest=_has(con, SI_VIEW)
    )
    con.execute(f"CREATE OR REPLACE VIEW {VIEW} AS {sql}")
    return True


def schema_snippet() -> str:
    return "\n".join(
        [
            f"- {VIEW}(ticker, lane, date, ret_1d, reversal_21d, momentum_12_1, specific_risk_63d, "
            "sector, short_interest_ratio, si_published_at)",
            "  [US stocks (lanes us_common and us_extended, delisted included), daily from 2018: trailing 21-session return, 12-1 momentum, annualised",
            "  63-session volatility of the sector-excess return, latest short interest over shares",
            "  outstanding published before the date; raw values, known after the close of `date`;",
            "  computed on the fly, so always filter by date or ticker]",
        ]
    )
