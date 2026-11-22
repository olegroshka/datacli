"""Where Alpaca data lives and how the fetcher behaves.

Root resolution, mirroring the other sources::

    DATACLI_ALPACA_ROOT env var  >  datacli.toml [alpaca].data_root  >  default

where the default is an ``alpaca`` sibling of the resolved eodhd data root.

Behaviour comes from :class:`Settings`: every field has a default and may be
overridden in ``[alpaca]`` (see ``datacli.example.toml``). Unknown keys are an
error, so a typo cannot silently fall back to a default.
"""

from __future__ import annotations

import datetime as dt
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Mapping

ENV_ROOT = "DATACLI_ALPACA_ROOT"
SECTION = "alpaca"


def resolve_root() -> tuple[Path, str]:
    """The Alpaca root and where it came from: ``env``, ``config`` or ``default``."""
    explicit = os.environ.get(ENV_ROOT)
    if explicit:
        return Path(explicit).expanduser(), "env"
    import config as eodhd_config  # type: ignore[import-not-found]

    override = eodhd_config.section(SECTION).get("data_root")
    if override:
        return Path(str(override)).expanduser(), "config"
    eodhd_root, _ = eodhd_config.eodhd_data_root()
    return Path(eodhd_root).parent / SECTION, "default"


def alpaca_root() -> Path:
    return resolve_root()[0]


def eodhd_root() -> Path:
    """The eodhd raw-data root (read for the universe and the daily spans)."""
    import config as eodhd_config  # type: ignore[import-not-found]

    return Path(eodhd_config.eodhd_data_root()[0])


@dataclass(frozen=True)
class Settings:
    """Fetcher behaviour. Defaults are the documented recommendation."""

    data_url: str = "https://data.alpaca.markets/v2/stocks"
    # the only feed the auctions endpoint serves
    feed: str = "sip"
    # Alpaca's SIP history starts here
    history_start: dt.date = dt.date(2016, 1, 1)
    symbols_per_request: int = 100
    # day records per page asked for (the API maximum; pages hold fewer)
    page_limit: int = 10_000
    # a guard against an endless page chain
    max_pages: int = 2_000
    # well under the plan's 10,000: the key is shared
    max_requests_per_minute: int = 600
    timeout_seconds: float = 30.0
    # a top-up re-reads this many calendar days (late official-close reports)
    overlap_days: int = 3
    # no daily price for this long -> the symbol is retired
    retired_after_days: int = 10
    # New York time, Monday to Friday
    quiet_windows: tuple[str, ...] = ("15:30-16:15",)
    quiet_timezone: str = "America/New_York"
    # store ticker -> Alpaca symbol
    symbol_map: Mapping[str, str] = field(default_factory=dict)
    # a ticker is `ok` with a closing cross on this share of the store's sessions ...
    min_coverage: float = 0.8
    # ... and no run of more than this many store sessions without one
    max_gap_sessions: int = 20
    # an alias closing cross within this of the store's close matches it
    alias_tolerance: float = 0.002
    # an alias segment must match on this share of the compared days to be stored
    alias_min_match: float = 0.9

    def __post_init__(self) -> None:
        if self.feed != "sip":
            raise ValueError("the auctions endpoint serves the sip feed only")
        for name in (
            "symbols_per_request",
            "page_limit",
            "max_pages",
            "max_requests_per_minute",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"[alpaca].{name} must be a positive integer")
        if not 0 < self.min_coverage <= 1 or not 0 < self.alias_min_match <= 1:
            raise ValueError(
                "[alpaca].min_coverage and alias_min_match must be in (0, 1]"
            )
        if self.max_gap_sessions < 0 or self.alias_tolerance <= 0:
            raise ValueError(
                "[alpaca].max_gap_sessions must be >= 0 and alias_tolerance > 0"
            )
        if self.overlap_days < 0 or self.retired_after_days < 1:
            raise ValueError(
                "[alpaca].overlap_days must be >= 0 and retired_after_days >= 1"
            )


def settings_from(section: Mapping[str, Any]) -> Settings:
    """Build :class:`Settings` from a ``[alpaca]`` TOML section (``data_root`` is ignored here)."""
    known = {f.name for f in fields(Settings)}
    values = {k: v for k, v in section.items() if k != "data_root"}
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unknown [alpaca] setting(s): {', '.join(unknown)}")
    if "history_start" in values and not isinstance(values["history_start"], dt.date):
        values["history_start"] = dt.date.fromisoformat(str(values["history_start"]))
    if "quiet_windows" in values:
        values["quiet_windows"] = tuple(str(w) for w in values["quiet_windows"])
    if "symbol_map" in values:
        values["symbol_map"] = {
            str(k): str(v) for k, v in dict(values["symbol_map"]).items()
        }
    return Settings(**values)


def load_settings() -> Settings:
    import config as eodhd_config  # type: ignore[import-not-found]

    return settings_from(eodhd_config.section(SECTION))
