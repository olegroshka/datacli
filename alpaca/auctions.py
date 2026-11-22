"""Auction prints: parsing the vendor records and choosing the cross.

Pure functions over plain data; no I/O.

What the prints are (verified against the API on 2026-11-22, and by the S7
research session over 2016-2023 for 591 symbols):

- ``O`` (opening) and ``6`` (closing) are the auction crosses. Several venues
  print one on the same day; the listing exchange's cross is the largest.
- ``Q`` (official open) and ``M`` (official close) are reports. The listing
  exchange's report repeats its cross (same price and size), other venues
  report their own small books; they appear in bulk from 2021. Reports must
  never be summed with the crosses.
- A late official-close report can carry the next day's timestamp and come
  back for a day outside the requested range.

Every print is stored (:data:`PRINT_SCHEMA`); :func:`select_crosses` reduces
them to one row per symbol and day (:data:`DAILY_SCHEMA`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Mapping

import pandas as pd
import pyarrow as pa

OPEN, CLOSE = "open", "close"
SIDES = (OPEN, CLOSE)
SIDE_KEYS = {"o": OPEN, "c": CLOSE}
CROSS_CODE = {OPEN: "O", CLOSE: "6"}
REPORT_CODE = {OPEN: "Q", CLOSE: "M"}

PRINT_SCHEMA = pa.schema(
    [
        ("ticker", pa.string()),  # the store's symbol (eodhd spelling)
        ("symbol", pa.string()),  # the symbol Alpaca was asked for
        ("date", pa.date32()),  # the trading day (the record's ``d``)
        ("side", pa.string()),  # open | close
        ("condition", pa.string()),  # O / 6 = cross, Q / M = report, other codes kept
        ("price", pa.float64()),
        ("size", pa.int64()),
        ("ts", pa.timestamp("ns", tz="UTC")),
        ("exchange", pa.string()),
    ]
)

DAILY_SCHEMA = pa.schema(
    [
        ("ticker", pa.string()),
        ("date", pa.date32()),
        ("open_price", pa.float64()),
        ("open_size", pa.int64()),
        ("open_exchange", pa.string()),
        ("open_ts", pa.timestamp("ns", tz="UTC")),
        ("open_crosses", pa.int16()),  # how many venues printed an opening cross
        ("close_price", pa.float64()),
        ("close_size", pa.int64()),
        ("close_exchange", pa.string()),
        ("close_ts", pa.timestamp("ns", tz="UTC")),
        ("close_crosses", pa.int16()),
        ("n_reports", pa.int16()),  # Q and M reports, both sides (informational)
    ]
)

PRINT_COLUMNS = list(PRINT_SCHEMA.names)
DAILY_COLUMNS = list(DAILY_SCHEMA.names)


class RecordError(ValueError):
    """A vendor record broke the expected layout."""


def empty_prints() -> pd.DataFrame:
    return PRINT_SCHEMA.empty_table().to_pandas()


def parse_records(
    records: Mapping[str, Iterable[Mapping[str, Any]]],
    tickers: Mapping[str, str],
    start: dt.date,
    end: dt.date,
) -> pd.DataFrame:
    """Flatten ``{alpaca symbol: [day record]}`` into print rows.

    ``tickers`` maps each requested Alpaca symbol to the store ticker it
    stands for; a symbol the request did not ask for is an error (it would
    mean the vendor answered another question). Days outside ``[start, end]``
    are dropped. Duplicate prints (same day, side, condition, exchange, time,
    price and size, which page overlaps could produce) are kept once.
    """
    rows: list[dict[str, Any]] = []
    for symbol, days in records.items():
        if symbol not in tickers:
            raise RecordError(f"the response holds an unrequested symbol {symbol!r}")
        ticker = tickers[symbol]
        for day in days:
            try:
                date = dt.date.fromisoformat(str(day["d"]))
            except (KeyError, ValueError) as exc:
                raise RecordError(f"{symbol}: a day record has no valid 'd'") from exc
            if not start <= date <= end:
                continue
            for key, side in SIDE_KEYS.items():
                for item in day.get(key) or ():
                    rows.append(_print_row(ticker, symbol, date, side, item))
    if not rows:
        return empty_prints()
    frame = pd.DataFrame(rows, columns=PRINT_COLUMNS)
    # pandas infers the unit from the text; Alpaca sends up to nanoseconds, so pin ns
    frame["ts"] = pd.to_datetime(frame["ts"], utc=True, format="ISO8601").dt.as_unit(
        "ns"
    )
    frame = frame.drop_duplicates(ignore_index=True)
    return frame.sort_values(["ticker", "date", "side", "ts"], ignore_index=True)


def _print_row(
    ticker: str, symbol: str, date: dt.date, side: str, item: Mapping[str, Any]
) -> dict[str, Any]:
    try:
        price = float(item["p"])
        size = int(round(float(item["s"])))
        stamp = str(item["t"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RecordError(
            f"{symbol} {date} {side}: a print lacks a valid p / s / t"
        ) from exc
    return {
        "ticker": ticker,
        "symbol": symbol,
        "date": date,
        "side": side,
        "condition": str(item.get("c") or ""),
        "price": price,
        "size": size,
        "ts": stamp,
        "exchange": str(item.get("x") or ""),
    }


def select_crosses(prints: pd.DataFrame) -> pd.DataFrame:
    """One row per ``(ticker, date)``: each side's cross and how many venues printed one.

    The chosen cross is the largest print with the side's cross code (the
    listing exchange's); ties go to the earliest. A side with no cross print
    is null (a halted name, a day with only a late report). Reports are only
    counted, never chosen and never summed.
    """
    if prints.empty:
        return DAILY_SCHEMA.empty_table().to_pandas()
    keys = ["ticker", "date"]
    days = prints[keys].drop_duplicates().sort_values(keys, ignore_index=True)
    reports = prints[prints["condition"].isin(set(REPORT_CODE.values()))]
    out = days.merge(
        reports.groupby(keys).size().rename("n_reports").reset_index(),
        on=keys,
        how="left",
    )
    for side in SIDES:
        crosses = prints[
            (prints["side"] == side) & (prints["condition"] == CROSS_CODE[side])
        ]
        counts = crosses.groupby(keys).size().rename(f"{side}_crosses").reset_index()
        chosen = (
            crosses.sort_values(["size", "ts"], ascending=[False, True])
            .drop_duplicates(keys)
            .loc[:, [*keys, "price", "size", "exchange", "ts"]]
            .rename(
                columns={c: f"{side}_{c}" for c in ("price", "size", "exchange", "ts")}
            )
        )
        out = out.merge(chosen, on=keys, how="left").merge(counts, on=keys, how="left")
    for name in ("open_crosses", "close_crosses", "n_reports"):
        out[name] = out[name].fillna(0).astype("int16")
    for name in ("open_size", "close_size"):
        out[name] = out[name].astype("Int64")
    return out[DAILY_COLUMNS]
