"""The registry of macro series -- the single source of truth, two providers.

- **FRED** (``FRED_SERIES``): US/market daily & monthly time series (rates, curve,
  inflation breakevens, credit spreads, implied vol, FX, commodities, money,
  activity, financial conditions).
- **EODHD** (``EODHD_INDICATORS`` x ``EODHD_COUNTRIES``): cross-country annual
  macro indicators (GDP, CPI, unemployment, real rates).

Add a FRED series or an EODHD indicator/country by dropping an entry here; every
macro command and the views pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FredSeries:
    series_id: str
    name: str
    category: str


@dataclass(frozen=True)
class EodhdIndicator:
    indicator: str
    name: str
    category: str


@dataclass(frozen=True)
class EodhdMarket:
    symbol: str  # EODHD symbol, e.g. GSPC.INDX / EURUSD.FOREX
    name: str
    category: str


# --------------------------------------------------------------------------- #
# FRED: market / US macro time series
# --------------------------------------------------------------------------- #
def _f(sid: str, name: str, category: str) -> tuple[str, FredSeries]:
    return sid, FredSeries(sid, name, category)


FRED_SERIES: dict[str, FredSeries] = dict(
    [
        _f("DGS10", "10Y Treasury yield", "rates"),
        _f("DGS2", "2Y Treasury yield", "rates"),
        _f("DGS3MO", "3M Treasury yield", "rates"),
        _f("DGS1", "1Y Treasury yield", "rates"),
        _f("DGS5", "5Y Treasury yield", "rates"),
        _f("DGS30", "30Y Treasury yield", "rates"),
        _f("DFF", "Fed funds effective rate", "rates"),
        _f("DFII10", "10Y TIPS real yield", "rates"),
        _f("T10Y2Y", "10Y-2Y term spread", "curve"),
        _f("T10Y3M", "10Y-3M term spread", "curve"),
        _f("T10YIE", "10Y breakeven inflation", "inflation"),
        _f("T5YIE", "5Y breakeven inflation", "inflation"),
        _f("T5YIFR", "5Y5Y forward inflation", "inflation"),
        _f("CPIAUCSL", "CPI (all urban)", "inflation"),
        _f("CPILFESL", "Core CPI", "inflation"),
        _f("PCEPILFE", "Core PCE price index", "inflation"),
        _f("BAMLH0A0HYM2", "US high-yield OAS", "credit"),
        _f("BAMLC0A0CM", "US investment-grade OAS", "credit"),
        _f("BAMLC0A4CBBB", "US BBB OAS", "credit"),
        _f("VIXCLS", "CBOE VIX", "vol"),
        _f("VXVCLS", "CBOE 3-month VIX", "vol"),
        _f("OVXCLS", "CBOE oil VIX", "vol"),
        _f("DTWEXBGS", "Broad USD index", "fx"),
        _f("DEXUSEU", "USD/EUR", "fx"),
        _f("DEXJPUS", "JPY/USD", "fx"),
        _f("DEXUSUK", "USD/GBP", "fx"),
        _f("DEXCHUS", "CNY/USD", "fx"),
        _f("DEXCAUS", "CAD/USD", "fx"),
        _f("DCOILWTICO", "WTI crude oil", "commodity"),
        _f("DCOILBRENTEU", "Brent crude oil", "commodity"),
        _f("WALCL", "Fed total assets", "money"),
        _f("M2SL", "M2 money stock", "money"),
        _f("RRPONTSYD", "Overnight reverse repo", "money"),
        _f("UNRATE", "Unemployment rate", "activity"),
        _f("PAYEMS", "Nonfarm payrolls", "activity"),
        _f("INDPRO", "Industrial production", "activity"),
        _f("ICSA", "Initial jobless claims", "activity"),
        _f("UMCSENT", "Consumer sentiment (UMich)", "activity"),
        _f("HOUST", "Housing starts", "activity"),
        _f("NFCI", "Chicago Fed financial conditions", "conditions"),
        _f("STLFSI4", "St. Louis financial stress", "conditions"),
    ]
)

# Back-compat alias (Phase 2.5 shipped as SERIES).
SERIES = FRED_SERIES


# --------------------------------------------------------------------------- #
# EODHD: cross-country annual indicators
# --------------------------------------------------------------------------- #
EODHD_INDICATORS: dict[str, EodhdIndicator] = {
    "gdp_growth_annual": EodhdIndicator(
        "gdp_growth_annual", "GDP growth (annual %)", "growth"
    ),
    "inflation_consumer_prices_annual": EodhdIndicator(
        "inflation_consumer_prices_annual", "CPI inflation (annual %)", "inflation"
    ),
    "unemployment_total_percent": EodhdIndicator(
        "unemployment_total_percent", "Unemployment rate (%)", "activity"
    ),
    "real_interest_rate": EodhdIndicator(
        "real_interest_rate", "Real interest rate (%)", "rates"
    ),
    "gdp_current_usd": EodhdIndicator("gdp_current_usd", "GDP (current USD)", "growth"),
}

# ISO3 country codes (EODHD uses "EMU" for the euro area).
EODHD_COUNTRIES: dict[str, str] = {
    "USA": "United States",
    "GBR": "United Kingdom",
    "EMU": "Euro area",
    "DEU": "Germany",
    "FRA": "France",
    "ITA": "Italy",
    "ESP": "Spain",
    "JPN": "Japan",
    "CHN": "China",
    "CAN": "Canada",
    "AUS": "Australia",
    "IND": "India",
}


# EODHD end-of-day market series (index levels, FX, benchmark rates) -- the
# market-context complement to FRED's US series. Symbols use EODHD conventions.
EODHD_MARKET: dict[str, EodhdMarket] = {
    "GSPC.INDX": EodhdMarket("GSPC.INDX", "S&P 500", "index"),
    "IXIC.INDX": EodhdMarket("IXIC.INDX", "Nasdaq Composite", "index"),
    "DJI.INDX": EodhdMarket("DJI.INDX", "Dow Jones Industrial", "index"),
    "FTSE.INDX": EodhdMarket("FTSE.INDX", "FTSE 100", "index"),
    "GDAXI.INDX": EodhdMarket("GDAXI.INDX", "DAX", "index"),
    "N225.INDX": EodhdMarket("N225.INDX", "Nikkei 225", "index"),
    "HSI.INDX": EodhdMarket("HSI.INDX", "Hang Seng", "index"),
    "EURUSD.FOREX": EodhdMarket("EURUSD.FOREX", "EUR/USD", "fx"),
    "GBPUSD.FOREX": EodhdMarket("GBPUSD.FOREX", "GBP/USD", "fx"),
    "USDJPY.FOREX": EodhdMarket("USDJPY.FOREX", "USD/JPY", "fx"),
    "AUDUSD.FOREX": EodhdMarket("AUDUSD.FOREX", "AUD/USD", "fx"),
}


def fred_ids() -> list[str]:
    return list(FRED_SERIES)


def eodhd_market_symbols() -> list[str]:
    return list(EODHD_MARKET)


def eodhd_pairs() -> list[tuple[str, str]]:
    """All (country, indicator) pairs to fetch."""
    return [(c, ind) for c in EODHD_COUNTRIES for ind in EODHD_INDICATORS]


def categories() -> list[str]:
    seen: dict[str, None] = {}
    for s in FRED_SERIES.values():
        seen.setdefault(s.category, None)
    return list(seen)


# Legacy helper name kept for Phase 2.5 callers/tests.
def series_ids() -> list[str]:
    return fred_ids()
