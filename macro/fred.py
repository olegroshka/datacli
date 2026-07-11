"""FRED fetcher: pull registered series into a single observations parquet.

Read-only against FRED, dry-run by default (``run=False``). The HTTP session is
injectable so the fetch path is unit-testable without a live API. A free
``FRED_API_KEY`` (fred.stlouisfed.org) is required to actually fetch.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from macro.config import OBSERVATIONS

FRED_OBS = "https://api.stlouisfed.org/fred/series/observations"
HTTP_TIMEOUT = 60
DEFAULT_START = "2000-01-01"


def api_key() -> str | None:
    return os.environ.get("FRED_API_KEY")


def fetch_series(
    session: Any, series_id: str, key: str, *, start: str = DEFAULT_START
) -> list[tuple[str, float]]:
    """Return ``[(date, value)]`` for one series, skipping missing (``.``) points."""
    resp = session.get(
        FRED_OBS,
        params={
            "series_id": series_id,
            "api_key": key,
            "file_type": "json",
            "observation_start": start,
        },
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    out: list[tuple[str, float]] = []
    for obs in resp.json().get("observations", []):
        value = obs.get("value")
        if value in (None, ".", ""):
            continue
        try:
            out.append((obs["date"], float(value)))
        except (ValueError, KeyError):
            continue
    return out


def refresh(
    series_ids: Iterable[str],
    *,
    run: bool,
    root: Path,
    start: str = DEFAULT_START,
    session: Any = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Fetch each series and write ``<root>/observations.parquet`` (full refresh)."""
    ids = list(series_ids)
    if not run:
        return {"run": False, "planned": ids}

    key = key or api_key()
    if not key:
        raise RuntimeError("FRED_API_KEY not set (free at fred.stlouisfed.org)")
    if session is None:
        import requests

        session = requests.Session()

    rows: list[tuple[str, str, float]] = []
    per_series: dict[str, int] = {}
    for series_id in ids:
        observations = fetch_series(session, series_id, key, start=start)
        per_series[series_id] = len(observations)
        rows.extend((series_id, date, value) for date, value in observations)

    frame = pd.DataFrame(rows, columns=["series_id", "date", "value"])
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / OBSERVATIONS
    frame.to_parquet(path, index=False)
    return {
        "run": True,
        "series": len(ids),
        "rows": int(len(frame)),
        "per_series": per_series,
        "path": str(path),
    }


def load(root: Path) -> pd.DataFrame | None:
    path = Path(root) / OBSERVATIONS
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except Exception:
        return None
