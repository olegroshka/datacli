"""The FCA's net short position disclosures, per holder (United Kingdom).

``https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx``:
one sheet named ``Historic Disclosures DD.MM.YYYY`` with the columns
``Position Holder, Name of Share Issuer, ISIN, Net Short Position (%),
Position Date``: every published position change since 2012-10-31 (108,850
rows, 602 holders, 917 issuers on the file dated 10.07.2026). A row with
``0.00`` records a position that fell below the publication threshold.

The file **stopped updating on 2026-07-11** (last position date 2026-07-09):
under the UK's Short Selling Regulations 2025 the FCA publishes aggregated
net short positions per issuer instead (``aggregated-*-net-short-positions.csv``
on the same page, not captured here). The fetch still works and returns the
frozen file; the history is a backtest dataset, not a live one.

Publication: a position is disclosed by 15:30 on the trading day after the
position date, so ``published_from`` is the next weekday (UK bank holidays
ignored: a known limit of one day on a few rows a year); ``published_to`` is
not published.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import urllib.request

import pandas as pd

from registers.common import Parsed, RegisterError, canonical, next_weekday

MARKET = "uk"
URL = "https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx"
COLUMNS: tuple[str, ...] = ("Position Holder", "Name of Share Issuer", "ISIN", "Net Short Position (%)", "Position Date")
_SHEET = re.compile(r"^Historic Disclosures (\d{2})\.(\d{2})\.(\d{4})$")
USER_AGENT = "datacli registers (research; see repository)"


def fetch(url: str = URL, *, timeout: float = 120.0) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URL)
        return response.read(), url


def parse(data: bytes) -> Parsed:
    """Parse the workbook strictly: exactly one ``Historic Disclosures`` sheet with the five columns."""
    try:
        book = pd.ExcelFile(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 (any reader error is a format error here)
        raise RegisterError(f"not a readable workbook: {exc}") from None
    matches = [(name, _SHEET.match(name)) for name in book.sheet_names]
    matches = [(name, m) for name, m in matches if m]
    if len(matches) != 1:
        raise RegisterError(f"expected one 'Historic Disclosures DD.MM.YYYY' sheet, found {book.sheet_names}")
    name, match = matches[0]
    day, month, year = (int(g) for g in match.groups())
    file_date = dt.date(year, month, day)
    frame = book.parse(name)
    if tuple(frame.columns) != COLUMNS:
        raise RegisterError(f"unexpected columns: {list(frame.columns)}")
    frame = frame.rename(
        columns={
            "Position Holder": "holder",
            "Name of Share Issuer": "issuer",
            "ISIN": "isin",
            "Net Short Position (%)": "net_short_pct",
            "Position Date": "position_date",
        }
    )
    frame["holder_lei"] = None
    position = pd.to_datetime(frame["position_date"], errors="coerce")
    if position.isna().any():
        raise RegisterError(f"{int(position.isna().sum())} rows without a position date")
    frame["position_date"] = position.dt.date
    frame["published_from"] = [next_weekday(d) for d in frame["position_date"]]
    frame["published_to"] = None
    return Parsed(file_date, canonical(frame, market=MARKET, file_date=file_date))
