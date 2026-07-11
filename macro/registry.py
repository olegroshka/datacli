"""The registry of macro series -- the single source of truth.

Add a market-relevant FRED series by dropping an entry in ``SERIES``; every macro
command (list / status / fetch) and the ``macro`` view pick it up automatically.
No series ids are hardcoded anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    name: str
    category: (
        str  # rates | curve | inflation | credit | vol | fx | commodity | activity
    )


# A curated, market-relevant starter set. Extend freely.
SERIES: dict[str, FredSeries] = {
    "DGS10": FredSeries("DGS10", "10Y Treasury yield", "rates"),
    "DGS2": FredSeries("DGS2", "2Y Treasury yield", "rates"),
    "DFF": FredSeries("DFF", "Fed funds effective rate", "rates"),
    "T10Y2Y": FredSeries("T10Y2Y", "10Y-2Y term spread", "curve"),
    "T10Y3M": FredSeries("T10Y3M", "10Y-3M term spread", "curve"),
    "T10YIE": FredSeries("T10YIE", "10Y breakeven inflation", "inflation"),
    "CPIAUCSL": FredSeries("CPIAUCSL", "CPI (all urban)", "inflation"),
    "BAMLH0A0HYM2": FredSeries("BAMLH0A0HYM2", "US high-yield OAS", "credit"),
    "BAMLC0A0CM": FredSeries("BAMLC0A0CM", "US investment-grade OAS", "credit"),
    "VIXCLS": FredSeries("VIXCLS", "CBOE VIX", "vol"),
    "DTWEXBGS": FredSeries("DTWEXBGS", "Broad USD index", "fx"),
    "DEXUSEU": FredSeries("DEXUSEU", "USD/EUR", "fx"),
    "DCOILWTICO": FredSeries("DCOILWTICO", "WTI crude oil", "commodity"),
    "UNRATE": FredSeries("UNRATE", "Unemployment rate", "activity"),
}


def series_ids() -> list[str]:
    return list(SERIES)


def categories() -> list[str]:
    seen: dict[str, None] = {}
    for s in SERIES.values():
        seen.setdefault(s.category, None)
    return list(seen)
