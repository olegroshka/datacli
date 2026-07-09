from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_us_fundamentals as us  # type: ignore  # noqa: E402


def _sample_raw_payload() -> dict:
    return {
        "General": {
            "Name": "Apple Inc.",
            "CountryISO": "US",
            "CurrencyCode": "USD",
            "Exchange": "US",
            "GicsSector": "Information Technology",
            "GicsIndustry": "Technology Hardware",
            "ISIN": "US0378331005",
            "MarketCapitalization": 1000000,
        },
        "Financials": {
            "Balance_Sheet": {
                "currency_symbol": "USD",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "USD",
                        "totalAssets": "1000",
                    }
                },
            },
            "Cash_Flow": {
                "currency_symbol": "USD",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "USD",
                        "capitalExpenditures": "25",
                    }
                },
            },
            "Income_Statement": {
                "currency_symbol": "USD",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "USD",
                        "totalRevenue": "300",
                    }
                },
            },
        },
    }


def test_section_output_specs_live_under_us_common() -> None:
    assert all(
        "us_common" in str(spec["path"]) for spec in us.SECTION_OUTPUT_SPECS.values()
    )


def test_raw_cache_round_trip_uses_us_common_root(tmp_path: Path) -> None:
    raw = _sample_raw_payload()

    written = us.save_raw_payload(raw, "AAPL", "US", raw_dir=tmp_path)
    loaded = us.load_cached_raw_payload("AAPL", "US", raw_dir=tmp_path)

    assert written == tmp_path / "cache" / "fundamentals" / "US" / "AAPL.json.gz"
    assert loaded == raw


def test_fetch_exchange_tickers_filters_common_stocks(monkeypatch) -> None:
    def fake_api_get(
        session: object, endpoint: str, params: dict | None = None
    ) -> list[dict[str, object]]:
        assert endpoint == "exchange-symbol-list/US"
        return [
            {"Code": "AAPL", "Type": "Common Stock", "Exchange": "NASDAQ"},
            {"Code": "SPY", "Type": "ETF", "Exchange": "NYSE ARCA"},
            {"Code": "MSFT", "Type": "Common Stock", "Exchange": "NASDAQ"},
            {"Code": "AABB", "Type": "Common Stock", "Exchange": "PINK"},
        ]

    monkeypatch.setattr(us, "_api_get", fake_api_get)
    monkeypatch.setattr(us.time, "sleep", lambda _: None)

    df = us.fetch_exchange_tickers(session=object(), exchange="US")

    assert df["Code"].tolist() == ["AAPL", "MSFT"]
    assert set(df["Type"]) == {"Common Stock"}
    assert set(df["Exchange"]) == {"NASDAQ"}


def test_extract_quarterly_statements_handles_us_payload() -> None:
    df = us.extract_quarterly_statements(_sample_raw_payload(), "AAPL", "US")

    assert not df.empty
    assert set(df["statement"].tolist()) == {"BS", "CF", "IS"}
    assert set(df["exchange"].tolist()) == {"US"}
    assert "2025-03-31" in df["date"].tolist()
