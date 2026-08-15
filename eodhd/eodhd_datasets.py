"""Declarative registry of btest-owned EODHD data lanes and their datasets.

This module is the single source of truth for *where each EODHD dataset lives on
disk* and *how to read its as-of / freshness signals*. Operational tools iterate
this registry instead of hardcoding lane or file names, so onboarding a new lane
or dataset is a one-entry change here rather than an edit scattered across every
reporting script.

The registry deliberately contains no I/O logic — only factual location and
column-mapping metadata. Consumers (e.g. ``status_eodhd.py``) own the reading and
interpretation of the referenced files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _datadir import EODHD_RAW_ROOT

__all__ = [
    "RAW_EODHD",
    "DatasetSpec",
    "LaneConfig",
    "LANES",
    "prices_spec",
    "event_spec",
    "fundamentals_spec",
    "iter_datasets",
    "registered_lane_names",
    "discover_lane_dirs",
    "NON_LANE_DIRS",
]

_ROOT = Path(__file__).resolve().parents[2]

#: Root landing zone for all raw EODHD lanes.
RAW_EODHD = EODHD_RAW_ROOT

#: Sub-directories under ``RAW_EODHD`` that are not data lanes (skip in discovery).
NON_LANE_DIRS = frozenset({"probe_cache"})


@dataclass(frozen=True)
class DatasetSpec:
    """One materialized dataset within a lane.

    A dataset is either an incrementally-fetched panel with a fetch-state sidecar
    (prices, dividend/split event histories) or a periodically-rebuilt snapshot
    with no sidecar (a fundamentals table).

    Attributes:
        kind: Stable machine label, unique within a lane (e.g. ``"prices"``).
        output: Main parquet filename under the lane root.
        state: Fetch-state sidecar filename, or ``None`` for snapshot datasets
            that carry no incremental sidecar.
        audit: Optional fetch-audit sidecar filename.
        as_of_state_col: State column holding the last real bar/event date.
        coverage_col: State column holding the queried upper bound.
        fetched_col: State column holding the fetch wall-clock timestamp.
        status_col: State column holding the per-pair fetch status.
        freshness_col: Which state column measures staleness. For prices this is
            the last real bar (``latest_data_date``); for event histories it is
            the query ceiling (``coverage_through``), because the last event date
            can be far in the past (a non-payer) or even a future announced
            ex-date, neither of which reflects how current the pull is.
        as_of_data_col: Date column in ``output`` used to derive the as-of date
            for snapshot datasets, and as a drift cross-check in ``--deep`` mode.
            ``None`` means fall back to the output file's mtime.
        key_cols: Identifier columns used for pair counting.
        label: Optional display override (defaults to ``kind``).
        fetcher: Filename (under ``eodhd/``) of the fetcher that refreshes
            this dataset, or ``None`` if it has no standalone fetcher.
        fetcher_args: Fixed CLI arguments always passed to ``fetcher`` (e.g.
            ``("--universe", "provider")`` for the US ETF lane).
    """

    kind: str
    output: str
    state: str | None = None
    audit: str | None = None
    as_of_state_col: str = "latest_data_date"
    coverage_col: str = "coverage_through"
    fetched_col: str = "fetched_at"
    status_col: str = "status"
    freshness_col: str = "latest_data_date"
    as_of_data_col: str | None = "date"
    key_cols: tuple[str, ...] = ("ticker", "exchange")
    label: str | None = None
    fetcher: str | None = None
    fetcher_args: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        """Human-facing dataset name."""
        return self.label or self.kind


@dataclass(frozen=True)
class LaneConfig:
    """A data lane: a directory of related datasets sharing a universe.

    Attributes:
        name: Lane key; also the default sub-directory name under ``RAW_EODHD``.
        region: Coarse geography label (e.g. ``"US"``, ``"UK/EU"``).
        asset_class: Coarse instrument label (e.g. ``"common"``, ``"etf"``).
        datasets: The datasets materialized in this lane.
        universe_path: Optional parquet listing the qualifying universe; ``None``
            for lanes whose universe is split across multiple files.
        root: Lane directory; defaults to ``RAW_EODHD / name`` when ``None``.
        universe_fetcher: Filename (under ``eodhd/``) of the universe
            refresh step run before prices/events, or ``None`` when the universe
            is a by-product of another stage (common-stock lanes).
        universe_fetcher_args: Fixed CLI arguments for ``universe_fetcher``.
    """

    name: str
    region: str
    asset_class: str
    datasets: tuple[DatasetSpec, ...]
    universe_path: Path | None = None
    root: Path | None = None
    universe_fetcher: str | None = None
    universe_fetcher_args: tuple[str, ...] = ()

    def resolved_root(self) -> Path:
        """Return the lane directory, defaulting to ``RAW_EODHD / name``."""
        return self.root if self.root is not None else RAW_EODHD / self.name


def prices_spec(
    fetcher: str | None = None, *, args: tuple[str, ...] = ()
) -> DatasetSpec:
    """Standard daily-price dataset spec (freshness = last real bar)."""
    return DatasetSpec(
        kind="prices",
        output="prices_daily.parquet",
        state="prices_fetch_state.csv",
        freshness_col="latest_data_date",
        as_of_data_col="date",
        fetcher=fetcher,
        fetcher_args=args,
    )


def event_spec(
    kind: str, fetcher: str | None = None, *, args: tuple[str, ...] = ()
) -> DatasetSpec:
    """Standard event-history dataset spec (freshness = query coverage).

    Args:
        kind: ``"dividends"`` or ``"splits"``.
        fetcher: Fetcher filename for this event lane.
        args: Fixed CLI arguments for the fetcher.
    """
    return DatasetSpec(
        kind=kind,
        output=f"{kind}_history.parquet",
        state=f"{kind}_fetch_state.csv",
        audit=f"{kind}_fetch_audit.csv",
        freshness_col="coverage_through",
        as_of_data_col="ex_date",
        fetcher=fetcher,
        fetcher_args=args,
    )


def fundamentals_spec(fetcher: str | None = None) -> DatasetSpec:
    """Quarterly-fundamentals spec.

    Refreshed incrementally by the fetcher's ``--update`` mode, which writes a
    per-firm ``fundamentals_fetch_state.csv`` sidecar. Freshness is the newest
    filing we hold (``latest_filing_date``); there is no query "coverage".
    """
    return DatasetSpec(
        kind="fundamentals",
        output="fundamentals_quarterly.parquet",
        state="fundamentals_fetch_state.csv",
        as_of_state_col="latest_filing_date",
        freshness_col="latest_filing_date",
        as_of_data_col="filing_date",
        label="fundamentals_q",
        fetcher=fetcher,
        fetcher_args=("--update",),
    )


def _lane(
    name: str,
    region: str,
    asset_class: str,
    universe: str | None,
    datasets: list[DatasetSpec],
    *,
    universe_fetcher: str | None = None,
) -> LaneConfig:
    return LaneConfig(
        name=name,
        region=region,
        asset_class=asset_class,
        universe_path=(RAW_EODHD / name / universe) if universe else None,
        datasets=tuple(datasets),
        universe_fetcher=universe_fetcher,
    )


#: The authoritative lane -> config map. Add a lane or dataset by editing here.
LANES: dict[str, LaneConfig] = {
    "us_common": _lane(
        "us_common",
        "US",
        "common",
        "tickers_US.parquet",
        [
            prices_spec("fetch_eodhd_us_prices.py"),
            event_spec("dividends", "fetch_eodhd_us_dividends.py"),
            event_spec("splits", "fetch_eodhd_us_splits.py"),
            fundamentals_spec("fetch_eodhd_us_fundamentals.py"),
        ],
    ),
    "uk_eu": _lane(
        "uk_eu",
        "UK/EU",
        "common",
        None,  # universe is split across tickers_LSE / _CO / _AS / _HE / _MC parquets
        [
            prices_spec("fetch_eodhd_eu_prices.py"),
            event_spec("dividends", "fetch_eodhd_dividends.py"),
            event_spec("splits", "fetch_eodhd_splits.py"),
            fundamentals_spec("fetch_eodhd_eu_fundamentals.py"),
        ],
    ),
    "us_etf": _lane(
        "us_etf",
        "US",
        "etf",
        "tickers_US_ETF.parquet",
        [
            prices_spec(
                "fetch_eodhd_us_etf_prices.py", args=("--universe", "provider")
            ),
            event_spec(
                "dividends",
                "fetch_eodhd_us_etf_dividends.py",
                args=("--universe", "provider"),
            ),
            event_spec(
                "splits",
                "fetch_eodhd_us_etf_splits.py",
                args=("--universe", "provider"),
            ),
        ],
        universe_fetcher="fetch_eodhd_us_etf_universe.py",
    ),
    "index_ref": _lane(
        "index_ref",
        "US",
        "index_ref",
        "tickers_INDX.parquet",
        [prices_spec("fetch_eodhd_index_ref_prices.py")],
        universe_fetcher="fetch_eodhd_index_ref_universe.py",
    ),
    "uk_eu_etf": _lane(
        "uk_eu_etf",
        "UK/EU",
        "etf",
        "tickers_UK_EU_ETF.parquet",
        [
            prices_spec("fetch_eodhd_uk_eu_etf_prices.py"),
            event_spec("dividends", "fetch_eodhd_uk_eu_etf_dividends.py"),
            event_spec("splits", "fetch_eodhd_uk_eu_etf_splits.py"),
        ],
        universe_fetcher="fetch_eodhd_uk_eu_etf_universe.py",
    ),
    "uk_eu_index_ref": _lane(
        "uk_eu_index_ref",
        "UK/EU",
        "index_ref",
        "tickers_INDX_UK_EU.parquet",
        [prices_spec("fetch_eodhd_uk_eu_index_ref_prices.py")],
        universe_fetcher="fetch_eodhd_uk_eu_index_ref_universe.py",
    ),
}


def iter_datasets(
    lanes: dict[str, LaneConfig] | None = None,
) -> list[tuple[LaneConfig, DatasetSpec]]:
    """Flatten the registry into ``(lane, dataset)`` pairs in declaration order.

    Args:
        lanes: Optional lane map to iterate (defaults to :data:`LANES`).
    """
    source = lanes if lanes is not None else LANES
    return [(lane, ds) for lane in source.values() for ds in lane.datasets]


def registered_lane_names() -> set[str]:
    """Return the set of lane keys known to the registry."""
    return set(LANES)


def discover_lane_dirs(raw_root: Path | None = None) -> list[str]:
    """List candidate lane sub-directories present on disk.

    Used for the auto-discovery guard: any directory here that is not a
    registered lane (and not in :data:`NON_LANE_DIRS`) is unmonitored data.
    Hidden directories (leading dot, e.g. ``.sync``) are infrastructure
    metadata, never lanes, and are skipped.

    Args:
        raw_root: Root to scan (defaults to :data:`RAW_EODHD`).
    """
    root = raw_root if raw_root is not None else RAW_EODHD
    if not root.exists():
        return []
    return sorted(
        p.name
        for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )
