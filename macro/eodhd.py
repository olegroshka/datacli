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

from macro.config import EODHD_PARQUET

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
) -> dict[str, Any]:
    """Fetch each (country, indicator) and write ``eodhd_indicators.parquet``."""
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
