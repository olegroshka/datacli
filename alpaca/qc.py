"""Quality checks over the auctions store, and a cross-check against eodhd.

:func:`findings` is the routine check (``alpaca qc``): files and state agree,
no ticker in error, nothing stale. :func:`crosscheck` (``alpaca qc
--crosscheck``) measures the two eodhd facts the auctions settle:

- how close ``bars_1m_volume_days.cross_est`` (the closing cross volume the
  intraday lane estimates from vendor bars) is to the official cross size,
  per volume regime;
- how often the vendor's daily ``open`` differs from the official opening
  cross price, and by how much.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from alpaca.store import STATUS_ERROR, STATUS_PARTIAL, AuctionStore

Finding = tuple[str, str, str]  # (severity, check, detail)
STALE_AFTER_DAYS = 4


def findings(store: AuctionStore, *, target: dt.date) -> list[Finding]:
    out: list[Finding] = []
    states = store.load_state()
    if not states:
        return [("warn", "empty", "no auctions fetched yet -- run: alpaca fetch --run")]
    on_disk = set(store.tickers_on_disk())
    with_prints = {s.ticker for s in states.values() if s.n_prints > 0}
    missing = sorted(with_prints - on_disk)
    if missing:
        out.append(
            (
                "error",
                "files",
                f"{len(missing)} ticker(s) have prints in state but no file: {', '.join(missing[:5])}",
            )
        )
    orphans = sorted(on_disk - {s.ticker for s in states.values()})
    if orphans:
        out.append(
            (
                "warn",
                "files",
                f"{len(orphans)} prints file(s) have no state row: {', '.join(orphans[:5])}",
            )
        )
    errors = sorted(t for t, s in states.items() if s.status == STATUS_ERROR)
    if errors:
        out.append(
            (
                "error",
                "fetch",
                f"{len(errors)} fetch unit(s) in error, e.g. {errors[0]}: {states[errors[0]].detail}",
            )
        )
    partial = sorted(t for t, s in states.items() if s.status == STATUS_PARTIAL)
    if partial:
        out.append(
            (
                "warn",
                "coverage",
                f"{len(partial)} fetch unit(s) not read over their span yet",
            )
        )
    current = [
        s
        for s in states.values()
        if s.role == "main" and s.asof is None and s.coverage_through is not None
    ]
    if current:
        newest = max(s.coverage_through for s in current if s.coverage_through)
        if (target - newest).days > STALE_AFTER_DAYS:
            out.append(
                (
                    "warn",
                    "stale",
                    f"newest coverage {newest} is more than {STALE_AFTER_DAYS} days before {target}",
                )
            )
    out.extend(coverage_findings(store))
    if on_disk and not store.daily_path.exists():
        out.append(
            (
                "error",
                "daily",
                "prints exist but auctions_daily.parquet does not -- rerun alpaca fetch --run",
            )
        )
    elif on_disk:
        newest_print = max(store.prints_path(t).stat().st_mtime for t in on_disk)
        if store.daily_path.stat().st_mtime + 1 < newest_print:
            out.append(
                (
                    "warn",
                    "daily",
                    "auctions_daily.parquet is older than the newest prints file",
                )
            )
        daily = store.read_daily()
        dupes = int(daily.duplicated(["ticker", "date"]).sum())
        if dupes:
            out.append(
                (
                    "error",
                    "keys",
                    f"{dupes} duplicate (ticker, date) rows in the daily file",
                )
            )
        no_close = int(daily["close_price"].isna().sum())
        if len(daily) and no_close / len(daily) > 0.02:
            out.append(
                (
                    "warn",
                    "close",
                    f"{no_close} of {len(daily)} ticker-days have no closing cross",
                )
            )
    return out


def coverage_findings(store: AuctionStore) -> list[Finding]:
    """Tickers whose stored auctions cover less than the store prices (``thin``)."""
    frame = store.read_coverage()
    if frame.empty:
        return [
            ("warn", "coverage", "coverage not assessed yet -- run: alpaca fetch --run")
        ]
    thin = frame[frame["status"] == "thin"].sort_values("coverage")
    if thin.empty:
        return []
    examples = ", ".join(
        f"{ticker} ({share:.0%}, longest gap {gap} sessions)"
        for ticker, share, gap in zip(
            thin["ticker"].head(6),
            thin["coverage"].head(6).astype(float),
            thin["longest_gap"].head(6).astype(int),
        )
    )
    return [
        (
            "warn",
            "coverage",
            f"{len(thin)} ticker(s) thin: {examples}. A predecessor security goes in "
            "alpaca/auctions_universe.toml as a verified [[alias]], a pre-listing stretch as [[listed]], "
            "a reviewed stretch without auctions as [[gap]]",
        )
    ]


@dataclass(frozen=True)
class RatioSummary:
    label: str
    n: int
    median: float
    p10: float
    p90: float
    within_5pct: float


@dataclass(frozen=True)
class OpenSummary:
    n: int
    differ_share: (
        float  # share of ticker-days where the daily open is not the cross price
    )
    median_abs_bp: float  # among those, the median absolute difference in basis points


def crosscheck(
    store: AuctionStore, eodhd_root: Path
) -> tuple[list[RatioSummary], OpenSummary | None]:
    from daily_store import DailyStore  # type: ignore[import-not-found]

    daily = store.read_daily()
    if daily.empty:
        return [], None
    daily = daily.assign(date=pd.to_datetime(daily["date"]))
    ratios: list[RatioSummary] = []
    volume_days = Path(eodhd_root) / "intraday" / "bars_1m_volume_days.parquet"
    if volume_days.exists():
        est = pd.read_parquet(
            volume_days,
            columns=["ticker", "exchange", "date", "regime", "cross_est", "status"],
        )
        est = est[(est["exchange"] == "US") & (est["status"] == "ok")]
        est = est.assign(date=pd.to_datetime(est["date"]))
        joined = est.merge(
            daily[["ticker", "date", "close_size"]], on=["ticker", "date"], how="inner"
        )
        joined = joined[(joined["close_size"] > 0) & (joined["cross_est"] > 0)]
        for regime, part in joined.groupby("regime"):
            ratio = (part["cross_est"] / part["close_size"].astype(float)).to_numpy()
            ratios.append(_ratio_summary(f"cross_est / cross ({regime} regime)", ratio))
    tickers = sorted(daily["ticker"].unique())
    start, end = daily["date"].min().date(), daily["date"].max().date()
    prices = DailyStore(Path(eodhd_root)).prices(
        start, end, tickers=tickers, columns=("open", "close")
    )
    joined = prices.merge(
        daily[["ticker", "date", "open_price"]], on=["ticker", "date"], how="inner"
    )
    joined = joined[(joined["open"] > 0) & (joined["open_price"] > 0)]
    if joined.empty:
        return ratios, None
    diff = (joined["open"] - joined["open_price"]).abs()
    differ = diff > 0.005  # more than half a cent
    open_ratio = (joined["open"] / joined["open_price"]).astype(float).to_numpy()
    bp = pd.Series(np.abs(np.log(open_ratio)) * 1e4, index=joined.index)[differ]
    return ratios, OpenSummary(
        len(joined), float(differ.mean()), float(bp.median()) if len(bp) else 0.0
    )


def _ratio_summary(label: str, ratio: np.ndarray) -> RatioSummary:
    return RatioSummary(
        label=label,
        n=int(ratio.size),
        median=float(np.median(ratio)),
        p10=float(np.quantile(ratio, 0.1)),
        p90=float(np.quantile(ratio, 0.9)),
        within_5pct=float(np.mean(np.abs(ratio - 1.0) <= 0.05)),
    )
