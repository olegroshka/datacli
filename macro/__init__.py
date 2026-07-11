"""Macro data adapter -- external reference series (FRED) for the Raw Data Lab.

Widens the lab's grounding beyond the equity tape: rates, the curve, credit
spreads, implied vol, FX and activity, fetched from FRED into parquet and exposed
as read-only DuckDB ``macro`` views the agents can join against the price data.
Registry-driven (add a series to ``registry.SERIES``); fetching is optional and
needs a free ``FRED_API_KEY``.
"""

from __future__ import annotations

__all__ = ["registry", "config", "fred", "views"]
