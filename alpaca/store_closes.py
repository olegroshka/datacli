"""The store's daily closes, for alias checks and coverage (adapter over ``daily_store``)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Sequence

import pandas as pd


class DailyStoreCloses:
    """``StoreCloses`` over the eodhd US daily lanes (read-only)."""

    def __init__(self, eodhd_root: Path) -> None:
        from daily_store import DailyStore  # type: ignore[import-not-found]

        self._store = DailyStore(Path(eodhd_root))

    def closes(
        self, tickers: Sequence[str], start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        frame = self._store.prices(
            start, end, tickers=list(tickers), columns=("close",)
        )
        frame = frame.loc[:, ["ticker", "date", "close"]].copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        return frame
