"""Which store symbols to fetch auctions for, over which span, as which Alpaca symbol.

The universe is a :class:`SymbolUniverse`; the default,
:class:`IntradayUniverse`, is every US stock and ETF that has ever been a
member of the eodhd intraday lane (``intraday/universe_membership.parquet``),
so the auctions line up with the names that have minute bars.

Each ticker has an **extent** and is fetched as one or more **units**
(:class:`SymbolSpan`), each a symbol and an ``asof`` over a date range:

- the extent runs from its first daily price (not before the history start,
  nor before a reviewed ``[[listed]]`` date) to its last; a name priced within
  ``retired_after_days`` of the target date is current and its extent runs to
  the target date;
- Alpaca maps a symbol to a security *as of* a date (default today), so a
  current symbol returns the security's whole history under renames, while a
  retired symbol (MMC, GPS, BHGE) returns nothing unless ``asof`` is a day it
  was in use. A retired symbol is therefore asked for ``asof`` its last daily
  price;
- the store often files a company's history under one symbol across a
  predecessor *security* (a merger, a new holding company), which Alpaca keeps
  apart. A reviewed ``[[alias]]`` (``alpaca/auctions_universe.toml``) fills
  such a date range from another symbol, or the same symbol at an earlier
  ``asof``; the main symbol keeps the rest of the extent, which must stay one
  interval;
- share classes are spelt with a dot at Alpaca (``BRK-B`` -> ``BRK.B``);
  ``[alpaca].symbol_map`` overrides the rule.
"""

from __future__ import annotations

import datetime as dt
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

import pandas as pd

_SHARE_CLASS = re.compile(r"^([A-Z]+)-([A-Z])$")
ASSET_TYPES = ("stock", "etf")
MAIN, ALIAS = "main", "alias"
OVERRIDES_PATH = Path(__file__).with_name("auctions_universe.toml")


@dataclass(frozen=True)
class SymbolSpan:
    """One fetch unit: a store ticker's date range under one Alpaca symbol and ``asof``."""

    ticker: str  # store spelling (eodhd)
    symbol: str  # Alpaca spelling
    asset_type: str  # stock | etf
    start: dt.date
    end: dt.date
    asof: dt.date | None = None  # set for a retired symbol and for every alias
    role: str = MAIN  # main | alias

    @property
    def retired(self) -> bool:
        return self.asof is not None

    @property
    def unit(self) -> str:
        """The state key: the ticker for its main unit, ``TICKER<SYMBOL@ASOF`` for an alias."""
        if self.role == MAIN:
            return self.ticker
        return (
            f"{self.ticker}<{self.symbol}@{self.asof.isoformat() if self.asof else '-'}"
        )


@dataclass(frozen=True)
class Alias:
    ticker: str
    symbol: str
    until: dt.date
    asof: dt.date
    start: dt.date | None = None
    note: str = ""


@dataclass(frozen=True)
class Listing:
    ticker: str
    auctions_from: dt.date
    note: str = ""


@dataclass(frozen=True)
class Gap:
    """A reviewed stretch with no auctions inside a ticker's extent."""

    ticker: str
    start: dt.date
    until: dt.date
    note: str = ""


@dataclass(frozen=True)
class Overrides:
    aliases: tuple[Alias, ...] = ()
    listings: tuple[Listing, ...] = ()
    gaps: tuple[Gap, ...] = ()

    def gaps_of(self, ticker: str) -> list[tuple[dt.date, dt.date]]:
        return sorted((g.start, g.until) for g in self.gaps if g.ticker == ticker)

    def aliases_of(self, ticker: str) -> list[Alias]:
        return sorted(
            (a for a in self.aliases if a.ticker == ticker), key=lambda a: a.until
        )

    def listing_of(self, ticker: str) -> Listing | None:
        return next((item for item in self.listings if item.ticker == ticker), None)


def _date(value: Any, where: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{where}: {value!r} is not a date") from exc


def parse_overrides(data: Mapping[str, Any]) -> Overrides:
    """Validate the TOML tables; unknown keys and overlapping aliases are errors."""
    unknown = sorted(set(data) - {"alias", "listed", "gap"})
    if unknown:
        raise ValueError(
            f"unknown table(s) in the auctions overrides: {', '.join(unknown)}"
        )
    aliases = []
    for i, row in enumerate(data.get("alias", [])):
        where = f"[[alias]] #{i + 1}"
        extra = sorted(set(row) - {"ticker", "symbol", "until", "from", "asof", "note"})
        if extra or not {"ticker", "symbol", "until"} <= set(row):
            raise ValueError(
                f"{where}: needs ticker, symbol, until (optional from, asof, note); got {sorted(row)}"
            )
        until = _date(row["until"], where)
        start = _date(row["from"], where) if "from" in row else None
        if start is not None and start > until:
            raise ValueError(f"{where}: from is after until")
        aliases.append(
            Alias(
                ticker=str(row["ticker"]),
                symbol=str(row["symbol"]),
                until=until,
                asof=_date(row.get("asof", until), where),
                start=start,
                note=str(row.get("note", "")),
            )
        )
    by_ticker: dict[str, list[Alias]] = {}
    for alias in aliases:
        by_ticker.setdefault(alias.ticker, []).append(alias)
    for ticker, group in by_ticker.items():
        group.sort(key=lambda a: a.until)
        for earlier, later in zip(group, group[1:]):
            if later.start is None or later.start <= earlier.until:
                raise ValueError(
                    f"[[alias]] for {ticker}: segments overlap; give each later one a `from` after the previous `until`"
                )
    listings = []
    for i, row in enumerate(data.get("listed", [])):
        where = f"[[listed]] #{i + 1}"
        if set(row) - {"ticker", "auctions_from", "note"} or not {
            "ticker",
            "auctions_from",
        } <= set(row):
            raise ValueError(f"{where}: needs ticker and auctions_from (optional note)")
        listings.append(
            Listing(
                str(row["ticker"]),
                _date(row["auctions_from"], where),
                str(row.get("note", "")),
            )
        )
    gaps = []
    for i, row in enumerate(data.get("gap", [])):
        where = f"[[gap]] #{i + 1}"
        if set(row) - {"ticker", "from", "until", "note"} or not {
            "ticker",
            "from",
            "until",
        } <= set(row):
            raise ValueError(f"{where}: needs ticker, from and until (optional note)")
        start, until = _date(row["from"], where), _date(row["until"], where)
        if start > until:
            raise ValueError(f"{where}: from is after until")
        gaps.append(Gap(str(row["ticker"]), start, until, str(row.get("note", ""))))
    return Overrides(tuple(aliases), tuple(listings), tuple(gaps))


def load_overrides(path: Path = OVERRIDES_PATH) -> Overrides:
    if not path.exists():
        return Overrides()
    with path.open("rb") as handle:
        return parse_overrides(tomllib.load(handle))


@dataclass(frozen=True)
class UniverseResult:
    spans: tuple[SymbolSpan, ...]  # the fetch units
    skipped: Mapping[str, str] = field(default_factory=dict)  # ticker -> reason
    # ticker -> (start, end)
    extents: Mapping[str, tuple[dt.date, dt.date]] = field(default_factory=dict)
    # reviewed stretches without auctions, per ticker
    gaps: Mapping[str, tuple[tuple[dt.date, dt.date], ...]] = field(
        default_factory=dict
    )


class SymbolUniverse(Protocol):
    """Anything that can say what to fetch up to a target date."""

    def resolve(self, target: dt.date) -> UniverseResult: ...


def alpaca_symbol(ticker: str, overrides: Mapping[str, str] | None = None) -> str:
    """The Alpaca spelling of a store ticker."""
    if overrides and ticker in overrides:
        return overrides[ticker]
    match = _SHARE_CLASS.match(ticker)
    return f"{match.group(1)}.{match.group(2)}" if match else ticker


def segment(
    ticker: str,
    asset_type: str,
    start: dt.date,
    end: dt.date,
    main: tuple[str, dt.date | None],
    aliases: Sequence[Alias],
) -> list[SymbolSpan]:
    """Pure: one ticker's extent split into alias units and the main unit.

    Aliases are clipped to the extent; the main symbol keeps what they leave,
    which must be one interval (aliases sit at the start, the end, or both).
    """
    units: list[SymbolSpan] = []
    taken: list[tuple[dt.date, dt.date]] = []
    for alias in aliases:
        a_start, a_end = max(start, alias.start or start), min(end, alias.until)
        if a_start > a_end:
            continue
        units.append(
            SymbolSpan(
                ticker, alias.symbol, asset_type, a_start, a_end, alias.asof, ALIAS
            )
        )
        taken.append((a_start, a_end))
    main_start, main_end = start, end
    for a_start, a_end in sorted(taken):
        if a_start <= main_start:
            main_start = max(main_start, a_end + dt.timedelta(days=1))
    for a_start, a_end in sorted(taken, reverse=True):
        if a_end >= main_end:
            main_end = min(main_end, a_start - dt.timedelta(days=1))
    if any(main_start <= a_end and a_start <= main_end for a_start, a_end in taken):
        raise ValueError(f"aliases of {ticker} would split its main span in two")
    if main_start <= main_end:
        symbol, asof = main
        units.append(
            SymbolSpan(ticker, symbol, asset_type, main_start, main_end, asof, MAIN)
        )
    return sorted(units, key=lambda u: u.start)


def spans_from_daily(
    members: pd.DataFrame,
    daily: pd.DataFrame,
    *,
    target: dt.date,
    history_start: dt.date,
    retired_after_days: int,
    symbol_map: Mapping[str, str] | None = None,
    overrides: Overrides | None = None,
) -> UniverseResult:
    """Pure: member tickers + their daily ``first`` / ``last`` -> extents and fetch units.

    ``members`` has ``ticker`` and ``asset_type``; ``daily`` has ``ticker``,
    ``first`` and ``last`` (dates).
    """
    overrides = overrides or Overrides()
    by_ticker: dict[str, tuple[dt.date, dt.date]] = {
        str(t): (first, last)
        for t, first, last in zip(daily["ticker"], daily["first"], daily["last"])
    }
    spans: list[SymbolSpan] = []
    extents: dict[str, tuple[dt.date, dt.date]] = {}
    skipped: dict[str, str] = {}
    cutoff = target - dt.timedelta(days=retired_after_days)
    for ticker, asset_type in sorted(
        set(zip(members["ticker"], members["asset_type"]))
    ):
        dates = by_ticker.get(ticker)
        if dates is None:
            skipped[ticker] = "no daily prices"
            continue
        first, last = dates
        retired = last < cutoff
        listing = overrides.listing_of(ticker)
        start = max(
            history_start, first, listing.auctions_from if listing else history_start
        )
        end = min(last, target) if retired else target
        if start > end:
            skipped[ticker] = "no daily prices inside the history"
            continue
        extents[ticker] = (start, end)
        main = (alpaca_symbol(ticker, symbol_map), last if retired else None)
        spans.extend(
            segment(ticker, asset_type, start, end, main, overrides.aliases_of(ticker))
        )
    gaps = {t: tuple(overrides.gaps_of(t)) for t in extents if overrides.gaps_of(t)}
    return UniverseResult(tuple(spans), skipped, extents, gaps)


class IntradayUniverse:
    """Every US stock and ETF ever in the intraday lane, spanned by its daily prices."""

    MEMBERSHIP = Path("intraday") / "universe_membership.parquet"

    def __init__(
        self,
        eodhd_root: Path,
        *,
        history_start: dt.date,
        retired_after_days: int,
        symbol_map: Mapping[str, str] | None = None,
        tickers: Sequence[str] | None = None,
        overrides: Overrides | None = None,
    ) -> None:
        self.eodhd_root = Path(eodhd_root)
        self.history_start = history_start
        self.retired_after_days = retired_after_days
        self.symbol_map = dict(symbol_map or {})
        self.only = None if tickers is None else {t.upper() for t in tickers}
        self.overrides = overrides if overrides is not None else load_overrides()

    def members(self) -> pd.DataFrame:
        path = self.eodhd_root / self.MEMBERSHIP
        if not path.exists():
            raise FileNotFoundError(
                f"no intraday membership file at {path}; build the intraday universe first"
            )
        frame = pd.read_parquet(path, columns=["ticker", "exchange", "asset_type"])
        frame = frame[
            (frame["exchange"] == "US") & frame["asset_type"].isin(ASSET_TYPES)
        ]
        if self.only is not None:
            frame = frame[frame["ticker"].isin(self.only)]
        return frame[["ticker", "asset_type"]].drop_duplicates(ignore_index=True)

    def resolve(self, target: dt.date) -> UniverseResult:
        from daily_store import DailyStore  # type: ignore[import-not-found]

        members = self.members()
        daily = DailyStore(self.eodhd_root).spans(sorted(members["ticker"]))
        return spans_from_daily(
            members,
            daily,
            target=target,
            history_start=self.history_start,
            retired_after_days=self.retired_after_days,
            symbol_map=self.symbol_map,
            overrides=self.overrides,
        )
