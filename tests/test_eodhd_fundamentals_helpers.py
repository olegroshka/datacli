from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_eu_fundamentals as fundamentals  # type: ignore  # noqa: E402
import probe_eodhd_fundamentals_schema as probe  # type: ignore  # noqa: E402


def _sample_raw_payload() -> dict:
    return {
        "General": {
            "Name": "Sample Corp",
            "CountryISO": "GB",
            "CurrencyCode": "GBP",
            "Exchange": "LSE",
            "GicsSector": "Industrials",
            "GicsIndustry": "Capital Goods",
            "ISIN": "GB0000000001",
            "MarketCapitalization": 123456789,
        },
        "Financials": {
            "Balance_Sheet": {
                "currency_symbol": "GBP",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "GBP",
                        "totalAssets": "1000",
                    }
                },
            },
            "Cash_Flow": {
                "currency_symbol": "GBP",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "GBP",
                        "capitalExpenditures": "25",
                    }
                },
            },
            "Income_Statement": {
                "currency_symbol": "GBP",
                "quarterly": {
                    "2025-03-31": {
                        "filing_date": "2025-05-01",
                        "currency_symbol": "GBP",
                        "totalRevenue": "300",
                    }
                },
            },
        },
        "SplitsDividends": {
            "ForwardAnnualDividendRate": 1.5,
            "ForwardAnnualDividendYield": 0.02,
            "PayoutRatio": 0.4,
            "DividendDate": "2025-06-01",
            "ExDividendDate": "2025-05-10",
            "LastSplitFactor": "2:1",
            "LastSplitDate": "2020-01-01",
            "NumberDividendsByYear": {
                "0": {"Year": 2024, "Count": 2},
                "1": {"Year": 2023, "Count": 1},
            },
        },
        "Earnings": {
            "History": {
                "2025-03-31": {
                    "date": "2025-03-31",
                    "reportDate": "2025-05-07",
                    "epsActual": 1.2,
                    "epsEstimate": 1.1,
                    "epsDifference": 0.1,
                    "surprisePercent": 9.09,
                    "beforeAfterMarket": "AfterMarket",
                }
            },
            "Trend": {
                "2026-12-31": {
                    "date": "2026-12-31",
                    "period": "0y",
                    "epsTrendCurrent": "4.9656",
                    "earningsEstimateAvg": "4.9656",
                    "revenueEstimateAvg": "322788107120.00",
                }
            },
            "Annual": {"2025-12-31": {"date": "2025-12-31", "epsActual": 3.02}},
        },
        "SharesStats": {
            "SharesOutstanding": 1000,
            "SharesFloat": 800,
            "PercentInsiders": 0.1,
            "PercentInstitutions": 0.7,
            "SharesShort": 20,
            "ShortPercentFloat": 0.025,
        },
        "outstandingShares": {
            "annual": {
                "0": {
                    "date": "2025",
                    "dateFormatted": "2025-12-31",
                    "sharesMln": "100.0",
                    "shares": 100000000,
                }
            },
            "quarterly": {
                "0": {
                    "date": "2025-Q1",
                    "dateFormatted": "2025-03-31",
                    "sharesMln": "99.0",
                    "shares": 99000000,
                }
            },
        },
        "Highlights": {"MarketCapitalization": 123456789, "EBITDA": 100},
        "Valuation": {
            "TrailingPE": 12.5,
            "ForwardPE": 11.2,
            "EnterpriseValue": 150000000,
        },
    }


def test_raw_cache_path_sanitizes_path_components(tmp_path: Path) -> None:
    cache_path = fundamentals.raw_cache_path("ABC/DEF", "XET/RA", raw_dir=tmp_path)
    assert (
        cache_path == tmp_path / "cache" / "fundamentals" / "XET_RA" / "ABC_DEF.json.gz"
    )


def test_save_and_load_raw_payload_round_trip(tmp_path: Path) -> None:
    raw = _sample_raw_payload()
    written = fundamentals.save_raw_payload(raw, "SHEL", "LSE", raw_dir=tmp_path)
    assert written.exists()

    loaded = fundamentals.load_cached_raw_payload("SHEL", "LSE", raw_dir=tmp_path)
    assert loaded == raw


def test_summarize_payload_sections_reports_structure() -> None:
    summary = fundamentals.summarize_payload_sections(_sample_raw_payload(), max_keys=4)

    assert summary["General"]["type"] == "dict"
    assert summary["General"]["n_keys"] == 8
    assert summary["Financials"]["type"] == "dict"
    assert summary["SplitsDividends"]["type"] == "dict"
    assert summary["SplitsDividends"]["n_keys"] == 8
    assert summary["Earnings"]["type"] == "dict"


def test_cached_payload_can_be_reparsed_offline(tmp_path: Path) -> None:
    raw = _sample_raw_payload()
    fundamentals.save_raw_payload(raw, "SHEL", "LSE", raw_dir=tmp_path)

    loaded = fundamentals.load_cached_raw_payload("SHEL", "LSE", raw_dir=tmp_path)
    assert loaded is not None

    metadata = fundamentals.extract_general_info(loaded)
    statements = fundamentals.extract_quarterly_statements(loaded, "SHEL", "LSE")

    assert metadata["name"] == "Sample Corp"
    assert metadata["isin"] == "GB0000000001"
    assert set(statements["statement"].tolist()) == {"BS", "CF", "IS"}


def test_probe_build_targets_uses_defaults_and_explicit_specs() -> None:
    default_args = argparse.Namespace(tickers=[], max_tickers=3)
    explicit_args = argparse.Namespace(tickers=["SHEL.LSE", "SAP.XETRA"], max_tickers=8)

    assert probe._build_probe_targets(default_args) == fundamentals.SMOKE_TICKERS[:3]
    assert probe._build_probe_targets(explicit_args) == [
        ("SHEL", "LSE"),
        ("SAP", "XETRA"),
    ]


def test_parse_ticker_spec_accepts_valid_value() -> None:
    assert fundamentals.parse_ticker_spec("SHEL.LSE") == ("SHEL", "LSE")
    assert fundamentals.parse_ticker_spec("sap.xetra") == ("sap", "XETRA")


def test_parse_ticker_spec_rejects_invalid_value() -> None:
    import pytest

    with pytest.raises(ValueError, match="Ticker spec must look like TICKER.EXCHANGE"):
        fundamentals.parse_ticker_spec("SHEL")


def test_extract_same_call_section_frames_returns_expected_outputs() -> None:
    frames = fundamentals.extract_same_call_section_frames(
        _sample_raw_payload(), "SHEL", "LSE"
    )

    assert set(frames) == set(fundamentals.SECTION_OUTPUT_SPECS)
    assert frames["splits_dividends_snapshot"].loc[0, "last_split_factor"] == "2:1"
    assert frames["dividend_counts_by_year"]["year"].tolist() == [2024, 2023]
    assert frames["shares_stats_snapshot"].loc[0, "shares_outstanding"] == 1000
    assert frames["highlights_snapshot"].loc[0, "ebitda"] == 100
    assert frames["valuation_snapshot"].loc[0, "trailing_pe"] == 12.5
    assert frames["outstanding_shares_annual"].loc[0, "date_formatted"] == "2025-12-31"
    assert frames["outstanding_shares_quarterly"].loc[0, "shares"] == 99000000
    assert frames["earnings_history"].loc[0, "report_date"] == "2025-05-07"
    assert frames["earnings_trend"].loc[0, "period"] == "0y"
    assert frames["earnings_annual"].loc[0, "eps_actual"] == 3.02


def test_merge_output_frame_dedupes_by_keys() -> None:
    existing = fundamentals.extract_same_call_section_frames(
        _sample_raw_payload(), "SHEL", "LSE"
    )["shares_stats_snapshot"]
    newer = existing.copy()
    newer.loc[0, "shares_outstanding"] = 2000

    merged = fundamentals._merge_output_frame(
        existing,
        newer,
        key_columns=["ticker", "exchange"],
    )

    assert len(merged) == 1
    assert merged.loc[0, "shares_outstanding"] == 2000


def test_get_api_key_reads_from_cwd_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("EODHD_API_KEY", raising=False)
    monkeypatch.setattr(fundamentals, "_get_api_key_from_windows_user_env", lambda: "")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("EODHD_API_KEY=test-key\n", encoding="utf-8")

    assert fundamentals._get_api_key() == "test-key"
