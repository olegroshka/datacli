from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import schema  # type: ignore  # noqa: E402


def test_datasets_and_canonical_columns() -> None:
    assert set(schema.datasets()) == {
        "prices",
        "dividends",
        "splits",
        "fundamentals",
        "news",
        "news_daily",
        "issuer_map",
        "news_issuer_daily",
    }
    assert "ex_date" in schema.canonical_columns("dividends")
    assert "article_id" in schema.canonical_columns("news")
    assert schema.canonical_columns("nonexistent") == []


def test_projection_passes_through_and_adds_lane() -> None:
    # all canonical columns present -> no NULL/rename extras, just passthrough + lane
    avail = schema.canonical_columns("dividends")
    proj = schema.projection("dividends", avail, "us_common")
    assert proj == "*, 'us_common' AS lane"


def test_projection_null_fills_missing_canonical() -> None:
    # drop a canonical column -> projection NULL-fills it so the view stays stable
    avail = [c for c in schema.canonical_columns("dividends") if c != "currency"]
    proj = schema.projection("dividends", avail, "us_common")
    assert 'NULL AS "currency"' in proj
    assert proj.endswith("'us_common' AS lane")


def test_projection_escapes_lane_literal() -> None:
    proj = schema.projection("prices", schema.canonical_columns("prices"), "o'brien")
    assert "'o''brien' AS lane" in proj


def test_diff_present_missing_extra() -> None:
    available = ["ticker", "exchange", "ex_date", "dividend", "surprise_col"]
    d = schema.diff("dividends", available)
    assert "surprise_col" in d["extra"]
    assert "currency" in d["missing"]  # canonical but not on disk
    assert "ticker" in d["present"]
    # case-insensitive matching
    d2 = schema.diff("dividends", ["TICKER", "Exchange", "EX_DATE"])
    assert "ticker" in d2["present"]
    assert "TICKER" not in d2["extra"]
