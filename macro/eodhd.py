"""EODHD macro provider: cross-country annual indicators.

Fetches ``/macro-indicator/{COUNTRY}?indicator=...`` for each registered
(country, indicator) pair into a single parquet. Read-only, dry-run by default,
injectable session for tests. Needs an EODHD API key (reused from the eodhd
tooling, else ``EODHD_API_KEY`` from the environment).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from macro.config import EODHD_MARKET_PARQUET, EODHD_PARQUET

EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 30


def api_key() -> str | None:
    """Prefer the eodhd toolkit's key resolver (may read a key file), else env."""
    try:
        eodhd_dir = Path(__file__).resolve().parents[1] / "eodhd"
        if str(eodhd_dir) not in sys.path:
            sys.path.insert(0, str(eodhd_dir))
        from fetch_eodhd_eu_fundamentals import _get_api_key  # type: ignore

        key = _get_api_key()
        if key:
            return str(key)
    except Exception:
        pass
    return os.environ.get("EODHD_API_KEY")


def fetch_indicator(
    session: Any, country: str, indicator: str, key: str
) -> list[tuple[str, float]]:
    """Return ``[(date, value)]`` for one (country, indicator)."""
    resp = session.get(
        f"{EODHD_BASE}/macro-indicator/{country}",
        params={"indicator": indicator, "api_token": key, "fmt": "json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    out: list[tuple[str, float]] = []
    for rec in data:
        date = rec.get("Date", rec.get("date"))
        value = rec.get("Value", rec.get("value"))
        if date is None or value in (None, "", "."):
            continue
        try:
            out.append((str(date)[:10], float(value)))
        except (ValueError, TypeError):
            continue
    return out


def refresh(
    pairs: Iterable[tuple[str, str]],
    *,
    run: bool,
    root: Path,
    session: Any = None,
    key: str | None = None,
    full_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch each (country, indicator) and upsert into ``eodhd_indicators.parquet``.

    Incremental by default (merge over existing on country/indicator/date).
    """
    all_pairs = list(pairs)
    if not run:
        return {"run": False, "planned": len(all_pairs)}

    key = key or api_key()
    if not key:
        raise RuntimeError(
            "EODHD API key not set (EODHD_API_KEY or the eodhd key file)"
        )
    if session is None:
        import requests

        session = requests.Session()

    rows: list[tuple[str, str, str, float]] = []
    with_data = 0
    for country, indicator in all_pairs:
        try:
            observations = fetch_indicator(session, country, indicator, key)
        except Exception:
            observations = []
        if observations:
            with_data += 1
        rows.extend((country, indicator, date, value) for date, value in observations)

    frame = pd.DataFrame(rows, columns=["country", "indicator", "date", "value"])
    if not full_refresh:
        from macro.util import merge_on

        frame = merge_on(load(root), frame, ["country", "indicator", "date"])
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / EODHD_PARQUET
    frame.to_parquet(path, index=False)
    return {
        "run": True,
        "pairs": len(all_pairs),
        "series_with_data": with_data,
        "rows": int(len(frame)),
        "path": str(path),
    }


def load(root: Path) -> pd.DataFrame | None:
    path = Path(root) / EODHD_PARQUET
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# EODHD end-of-day market series (index levels / FX)
# --------------------------------------------------------------------------- #
def fetch_eod(
    session: Any, symbol: str, key: str, *, start: str = "2000-01-01"
) -> list[tuple[str, float]]:
    """Return ``[(date, close)]`` for one EODHD symbol (index / FX / etc.)."""
    resp = session.get(
        f"{EODHD_BASE}/eod/{symbol}",
        params={"api_token": key, "fmt": "json", "from": start},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        return []
    out: list[tuple[str, float]] = []
    for rec in data:
        date = rec.get("date")
        close = rec.get("close", rec.get("adjusted_close"))
        if date is None or close in (None, ""):
            continue
        try:
            out.append((str(date)[:10], float(close)))
        except (ValueError, TypeError):
            continue
    return out


def refresh_market(
    symbols: Iterable[str],
    *,
    run: bool,
    root: Path,
    session: Any = None,
    key: str | None = None,
    start: str = "2000-01-01",
    full_refresh: bool = False,
) -> dict[str, Any]:
    """Fetch each symbol's daily close and upsert into ``eodhd_market.parquet``."""
    syms = list(symbols)
    if not run:
        return {"run": False, "planned": len(syms)}
    key = key or api_key()
    if not key:
        raise RuntimeError(
            "EODHD API key not set (EODHD_API_KEY or the eodhd key file)"
        )
    if session is None:
        import requests

        session = requests.Session()

    rows: list[tuple[str, str, float]] = []
    with_data = 0
    for symbol in syms:
        try:
            observations = fetch_eod(session, symbol, key, start=start)
        except Exception:
            observations = []
        if observations:
            with_data += 1
        rows.extend((symbol, date, value) for date, value in observations)

    frame = pd.DataFrame(rows, columns=["symbol", "date", "value"])
    if not full_refresh:
        from macro.util import merge_on

        frame = merge_on(load_market(root), frame, ["symbol", "date"])
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / EODHD_MARKET_PARQUET
    frame.to_parquet(path, index=False)
    return {
        "run": True,
        "symbols": len(syms),
        "symbols_with_data": with_data,
        "rows": int(len(frame)),
        "path": str(path),
    }


def load_market(root: Path) -> pd.DataFrame | None:
    path = Path(root) / EODHD_MARKET_PARQUET
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None
