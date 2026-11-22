"""The three linkage datasets: plan (compute every partition) and apply (write what changed).

Each dataset implements :class:`Dataset`: ``plan()`` computes every
partition its inputs support and digests the content; :func:`apply` compares
the digests with the store's manifest and, with ``run``, writes the new and
restated partitions and drops the ones the inputs no longer support. Planning
is local and CPU-only, so a dry run reports exactly what a run would write.

Dependencies: ``auction_cospike`` reads ``auction_abnormal``. When both are
built together the freshly computed panel is handed over in memory; built
alone, cospike reads the stored panel.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Callable, Protocol

import pandas as pd

from linkage import abnormal as abnormal_mod
from linkage import betas as betas_mod
from linkage import cospike as cospike_mod
from linkage.calendar import month_end_sessions, quarter_start
from linkage.config import AbnormalParams, BetaParams, CospikeParams, digest
from linkage.returns import ReturnsPanel, total_returns
from linkage.sources import ETF, STOCK, AuctionSource, PriceSource, UniverseSource
from linkage.store import NEW, RESTATED, UNCHANGED, PartitionStore

ETF_BETAS, AUCTION_ABNORMAL, AUCTION_COSPIKE = (
    "etf_betas",
    "auction_abnormal",
    "auction_cospike",
)
NAMES = (ETF_BETAS, AUCTION_ABNORMAL, AUCTION_COSPIKE)  # dependency order
SCHEMAS = {
    ETF_BETAS: betas_mod.SCHEMA,
    AUCTION_ABNORMAL: abnormal_mod.SCHEMA,
    AUCTION_COSPIKE: cospike_mod.SCHEMA,
}
#: calendar days of prices loaded before the first snapshot, for the longest window
WARMUP_FACTOR = 1.6


def content_digest(frame: pd.DataFrame) -> str:
    """A digest of a partition's content (columns, order and values)."""
    h = hashlib.sha256("|".join(map(str, frame.columns)).encode("utf-8"))
    if len(frame):
        h.update(pd.util.hash_pandas_object(frame, index=False).to_numpy().tobytes())
    return h.hexdigest()[:24]


@dataclass
class Partition:
    key: str
    frame: pd.DataFrame
    content: str = ""
    status: str = ""

    def __post_init__(self) -> None:
        if not self.content:
            self.content = content_digest(self.frame)


@dataclass
class DatasetPlan:
    name: str
    params: str
    partitions: list[Partition]
    notes: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)  # on disk but no longer produced

    def count(self, status: str) -> int:
        return sum(1 for p in self.partitions if p.status == status)


class Dataset(Protocol):
    name: str
    store: PartitionStore

    def plan(self) -> DatasetPlan: ...


def apply(
    plan: DatasetPlan, store: PartitionStore, *, run: bool, full: bool = False
) -> DatasetPlan:
    """Classify every partition against the store; with ``run``, write and drop."""
    produced = {p.key for p in plan.partitions}
    plan.stale = [k for k in store.keys_on_disk() if k not in produced]
    for part in plan.partitions:
        status = store.classify(part.key, part.content, plan.params)
        part.status = RESTATED if full and status == UNCHANGED else status
    if not run:
        return plan
    for part in plan.partitions:
        if part.status in (NEW, RESTATED):
            store.write(part.key, part.frame, inputs=part.content, params=plan.params)
    for key in plan.stale:
        store.drop(key)
    return plan


# --------------------------------------------------------------------------- #
# etf_betas
# --------------------------------------------------------------------------- #
class EtfBetas:
    name = ETF_BETAS

    def __init__(
        self,
        prices: PriceSource,
        universe: UniverseSource,
        store: PartitionStore,
        params: BetaParams,
        *,
        today: dt.date,
    ) -> None:
        self.prices, self.universe, self.store, self.params, self.today = (
            prices,
            universe,
            store,
            params,
            today,
        )

    def plan(self) -> DatasetPlan:
        p = self.params
        etfs = self.universe.context_etfs()
        groups = dict(zip(etfs["ticker"], etfs["group"]))
        if p.market not in groups:
            raise ValueError(f"the market {p.market!r} is not a context ETF")
        load_from = p.start - dt.timedelta(
            days=int(max(p.windows) * WARMUP_FACTOR) + 30
        )
        etf_prices = self.prices.prices(
            load_from, self.today, tickers=sorted(groups), asset_types=(ETF,)
        )
        sessions = pd.DatetimeIndex(
            sorted(etf_prices.loc[etf_prices["ticker"] == p.market, "date"].unique())
        )
        stock_names = self.prices_liquid(load_from)
        stock_prices = self.prices.prices(
            load_from, self.today, tickers=stock_names, asset_types=(STOCK,)
        )
        stocks = self._panel(stock_prices, sessions, load_from)
        etf_panel = self._panel(etf_prices, sessions, load_from)
        notes = [
            f"{len(stocks.returns.columns)} stocks screened in, {len(etf_panel.returns.columns)} ETFs, "
            f"{len(sessions)} sessions from {sessions[0].date() if len(sessions) else '-'}",
            f"masked stock-days: "
            + ", ".join(
                f"{r} {int(stocks.masked[r].sum()) if r in stocks.masked else 0}"
                for r in ("event", "jump", "dividend", "gap")
            ),
        ]
        partitions = []
        for as_of in month_end_sessions(sessions, pd.Timestamp(p.start)):
            frame = betas_mod.snapshot(stocks, etf_panel.returns, groups, as_of, p)
            if not frame.empty:
                partitions.append(Partition(as_of.date().isoformat(), frame))
        return DatasetPlan(self.name, digest(p), partitions, notes)

    def prices_liquid(self, load_from: dt.date) -> list[str]:
        screen = getattr(self.prices, "liquid_tickers", None)
        if screen is None:  # a source without the screen: every stock
            return sorted(
                self.prices.prices(load_from, self.today, asset_types=(STOCK,))[
                    "ticker"
                ].unique()
            )
        return screen(
            load_from,
            self.today,
            min_price=self.params.min_price,
            min_dollar_volume=self.params.min_dollar_volume,
            asset_types=(STOCK,),
        )

    def _panel(
        self, prices: pd.DataFrame, sessions: pd.DatetimeIndex, load_from: dt.date
    ) -> ReturnsPanel:
        names = sorted(prices["ticker"].unique())
        return total_returns(
            prices,
            self.prices.split_days(load_from, self.today, tickers=names),
            self.prices.dividends(load_from, self.today, tickers=names),
            sessions,
            event_window=self.params.event_window,
            max_abs_log_return=self.params.max_abs_log_return,
            max_dividend_yield=self.params.max_dividend_yield,
        )


# --------------------------------------------------------------------------- #
# auction_abnormal
# --------------------------------------------------------------------------- #
class AuctionAbnormal:
    name = AUCTION_ABNORMAL

    def __init__(
        self,
        auctions: AuctionSource,
        universe: UniverseSource,
        store: PartitionStore,
        params: AbnormalParams,
    ) -> None:
        self.auctions, self.universe, self.store, self.params = (
            auctions,
            universe,
            store,
            params,
        )
        self.panel: pd.DataFrame | None = (
            None  # the full panel of the last plan, for cospike
        )

    def plan(self) -> DatasetPlan:
        panel = abnormal_mod.build(
            self.auctions.daily(), self.universe.membership(), self.params
        )
        self.panel = panel
        years = pd.to_datetime(panel["date"]).dt.year
        partitions = [
            Partition(str(year), part.reset_index(drop=True))
            for year, part in panel.groupby(years, sort=True)
        ]
        members = int(panel["is_member"].sum()) if len(panel) else 0
        notes = [f"{len(panel):,} name-side-days, {members:,} of them members"]
        return DatasetPlan(self.name, digest(self.params), partitions, notes)


# --------------------------------------------------------------------------- #
# auction_cospike
# --------------------------------------------------------------------------- #
class AuctionCospike:
    name = AUCTION_COSPIKE

    def __init__(
        self,
        panel: Callable[[], pd.DataFrame],
        universe: UniverseSource,
        store: PartitionStore,
        params: CospikeParams,
    ) -> None:
        self.panel, self.universe, self.store, self.params = (
            panel,
            universe,
            store,
            params,
        )

    def plan(self) -> DatasetPlan:
        p = self.params
        panel = self.panel()
        side = panel[panel["side"] == p.side]
        side = side.assign(date=pd.to_datetime(side["date"]))
        resid = side.pivot_table(
            index="date", columns="ticker", values="resid", aggfunc="first"
        ).sort_index()
        membership = self.universe.membership()
        types = {
            str(t): str(a)
            for t, a in zip(membership["ticker"], membership["asset_type"])
        }
        wanted = {STOCK, ETF} if p.include_etfs else {STOCK}
        by_quarter = {
            q: sorted(g.loc[g["asset_type"].isin(wanted), "ticker"].unique())
            for q, g in membership.groupby("quarter_start")
        }
        member_days = side[side["is_member"]].groupby("date").size()
        sessions = pd.DatetimeIndex(member_days[member_days > 0].index)
        partitions = []
        for as_of in month_end_sessions(sessions, pd.Timestamp(p.start)):
            members = by_quarter.get(quarter_start(as_of), [])
            frame = cospike_mod.snapshot(resid, members, types, as_of, p)
            if not frame.empty:
                partitions.append(Partition(as_of.date().isoformat(), frame))
        notes = [f"{resid.shape[1]} names over {len(sessions)} auction days ({p.side})"]
        # an upstream (abnormal) parameter change shows up as changed content: restated
        return DatasetPlan(self.name, digest(p), partitions, notes)
