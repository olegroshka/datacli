"""Build the ``intraday`` lane universe (local, no API calls).

Reads ``eodhd/intraday_universe.toml`` and the daily prices of the stock lanes,
ranks US common stocks by median daily dollar volume at every quarter start,
applies the tier hysteresis, adds the curated context / index / FX / UK lists,
and writes:

- ``<data_root>/intraday/universe_membership.parquet``: one row per
  (quarter_start, ticker, exchange) member, point in time;
- ``<data_root>/intraday/tickers_INTRADAY.parquet``: the current members (the
  latest quarter), with role, ISA tradability and the bar fetch floor.

Design: docs/INTRADAY_1M_DESIGN.md, section 4.

Usage:
    uv run python eodhd/build_intraday_universe.py            # as of today
    uv run python eodhd/build_intraday_universe.py --as-of 2026-11-13
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _atomic  # noqa: E402
from _datadir import EODHD_RAW_ROOT  # noqa: E402

CONFIG_PATH = Path(__file__).resolve().parent / "intraday_universe.toml"
LANE = "intraday"
RAW_DIR = EODHD_RAW_ROOT / LANE
TICKERS_PATH = RAW_DIR / "tickers_INTRADAY.parquet"
MEMBERSHIP_PATH = RAW_DIR / "universe_membership.parquet"

#: Curated sections of the toml, in output order.
CURATED_SECTIONS = ("context", "index", "fx", "uk")

CURRENT_COLUMNS = [
    "ticker",
    "exchange",
    "tier",
    "role",
    "asset_type",
    "group",
    "adv_rank",
    "adv_musd",
    "isa_tradable",
    "reason",
    "fetch_from",
]
MEMBERSHIP_COLUMNS = [
    "quarter_start",
    "ticker",
    "exchange",
    "tier",
    "role",
    "asset_type",
    "group",
    "adv_rank",
    "adv_musd",
    "isa_tradable",
    "listed_from",
]


@dataclass(frozen=True)
class Tier:
    """A ranked stock tier: joins at ``rank <= size``, stays while ``rank <= keep``."""

    name: str
    size: int
    keep: int
    role: str


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "rb") as handle:
        config = tomllib.load(handle)
    tiers = [Tier(**t) for t in config["rules"]["tiers"]]
    sizes = [t.size for t in tiers]
    if sizes != sorted(sizes) or any(t.keep < t.size for t in tiers):
        raise ValueError("tiers must be ordered by size and keep >= size")
    config["rules"]["tiers"] = tiers
    return config


def curated_rows(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The fixed (non-ranked) members, one dict per instrument."""
    rows: list[dict[str, Any]] = []
    for section in CURATED_SECTIONS:
        block = config.get(section)
        if not block:
            continue
        groups: dict[str, list[str]]
        if "groups" in block:
            groups = dict(block["groups"])
        else:
            groups = {section: list(block["tickers"])}
        for group, tickers in groups.items():
            for ticker in tickers:
                rows.append(
                    {
                        "ticker": ticker,
                        "exchange": block["exchange"],
                        "tier": section,
                        "role": block["role"],
                        "asset_type": block["asset_type"],
                        "group": group,
                        "isa_tradable": block["isa_tradable"],
                    }
                )
    return rows


# --------------------------------------------------------------------------- #
# quarters and ranking
# --------------------------------------------------------------------------- #
def quarter_start(day: date) -> date:
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def quarter_starts(first: date, as_of: date) -> list[date]:
    """Quarter starts from the quarter holding ``first`` up to the one holding ``as_of``."""
    out: list[date] = []
    current = quarter_start(first)
    last = quarter_start(as_of)
    while current <= last:
        out.append(current)
        month = current.month + 3
        current = date(current.year + (month - 1) // 12, (month - 1) % 12 + 1, 1)
    return out


def quarter_end(start: date) -> date:
    month = start.month + 3
    nxt = date(start.year + (month - 1) // 12, (month - 1) % 12 + 1, 1)
    return nxt - timedelta(days=1)


def load_quarter_ranks(
    sources: list[tuple[str, Path, Path | None, Path | None]],
    quarters: list[date],
    *,
    min_price: float,
    min_days: int,
    stock_types: Iterable[str] = ("Common Stock",),
) -> pd.DataFrame:
    """Dollar-volume ranks per quarter start from the stock lanes' daily prices.

    ``sources`` is ``(lane, prices_parquet, universe_parquet_or_None,
    splits_parquet_or_None)`` in priority order: a ticker priced by an earlier
    lane is ignored in later ones. A universe parquet with a ``Type`` column
    restricts that lane to ``stock_types`` (us_extended also prices ETFs and funds).

    EODHD's daily ``close`` is the raw traded price but its ``volume`` is
    split-adjusted (AAPL 2020-08-28: close 499.23, volume 187.6M = 4 x the
    46.9M shares traded). ``close * volume`` would inflate every name by the
    splits that came *later* -- lookahead that ranks future splitters (NVDA,
    AMZN, GOOGL, TSLA) first. Volume is therefore divided by the product of the
    *forward* split ratios with an ex-date after the bar (the lane's own splits
    file), which gives the shares actually traded that day. Reverse splits are
    left alone: for the serial reverse-splitters in us_extended the vendor
    series is already fully adjusted (SNCA closes at 104 in 2017 against a real
    price of cents), so undoing them would multiply volume by up to 12,500x
    (SPLP). Ignoring them can only understate a penny stock's dollar volume.

    Returns ``quarter_start, ticker, exchange, n_days, adv_musd, last_close, adv_rank``
    for the names that pass the price and history filters, ``adv_rank`` 1 = most liquid.
    """
    import duckdb

    con = duckdb.connect()
    if not quarters:
        return pd.DataFrame(
            columns=[
                "quarter_start",
                "ticker",
                "exchange",
                "n_days",
                "adv_musd",
                "last_close",
                "adv_rank",
            ]
        )
    con.register("quarters", pd.DataFrame({"q": pd.to_datetime(quarters)}))
    floor = (min(quarters) - timedelta(days=366)).isoformat()
    parts: list[str] = []
    split_parts: list[str] = []
    for priority, (lane, prices, universe, splits) in enumerate(sources):
        if splits is not None and Path(splits).exists():
            split_parts.append(
                f"SELECT DISTINCT ticker, CAST(ex_date AS DATE) AS xd, split_ratio AS r, {priority} AS pri "
                f"FROM read_parquet('{Path(splits).as_posix()}') "
                "WHERE split_ratio > 1 AND ex_date IS NOT NULL"
            )
        if not Path(prices).exists():
            continue
        where = [f"date >= '{floor}'", "close > 0", "volume > 0"]
        if universe is not None and Path(universe).exists():
            types = ", ".join(f"'{t}'" for t in stock_types)
            uni = Path(universe).as_posix()
            where.append(
                f"ticker IN (SELECT Code FROM read_parquet('{uni}') WHERE Type IN ({types}))"
            )
        source = f"read_parquet('{Path(prices).as_posix()}')"
        columns = {
            row[0] for row in con.execute(f"DESCRIBE SELECT * FROM {source}").fetchall()
        }
        adj = "adjusted_close" if "adjusted_close" in columns else "NULL"
        parts.append(
            f"SELECT ticker, exchange, CAST(date AS DATE) AS d, close, volume, {adj} AS adj, {priority} AS pri "
            f"FROM {source} WHERE {' AND '.join(where)}"
        )
    if not parts:
        raise FileNotFoundError("no stock-lane prices_daily.parquet found")
    con.execute(
        "CREATE TEMP TABLE daily_adj AS SELECT ticker, exchange, d, close, volume, adj, pri FROM ("
        + " UNION ALL ".join(parts)
        + ") QUALIFY pri = min(pri) OVER (PARTITION BY ticker)"
    )
    if split_parts:
        con.execute("CREATE TEMP TABLE spl_all AS " + " UNION ALL ".join(split_parts))
        # Undo a split only where the lane's history was re-adjusted for it: the
        # bulk --fast top-up appends new bars without re-adjusting old ones, so
        # until the next per-ticker full refresh a fresh split is not in the
        # history yet (close/adjusted_close then shows no step at the ex-date).
        # Without adjusted_close the check cannot run and every split counts.
        con.execute("""
            CREATE TEMP TABLE spl AS
            WITH k AS (SELECT ticker, pri, d, close / adj AS k FROM daily_adj WHERE adj > 0)
            SELECT s.* FROM spl_all s
            WHERE NOT EXISTS (SELECT 1 FROM k WHERE k.ticker = s.ticker AND k.pri = s.pri)
               OR (SELECT arg_max(k, d) FROM k WHERE k.ticker = s.ticker AND k.pri = s.pri AND k.d < s.xd)
                  / (SELECT arg_min(k, d) FROM k WHERE k.ticker = s.ticker AND k.pri = s.pri AND k.d >= s.xd)
                  > sqrt(s.r)
            """)
        con.execute("""
            CREATE TEMP TABLE daily AS
            WITH f AS (
                SELECT a.ticker, a.d, exp(sum(ln(s.r))) AS factor
                FROM daily_adj a JOIN spl s ON s.ticker = a.ticker AND s.pri = a.pri AND s.xd > a.d
                GROUP BY 1, 2)
            SELECT a.ticker, a.exchange, a.d, a.close, a.volume / coalesce(f.factor, 1) AS volume
            FROM daily_adj a LEFT JOIN f USING (ticker, d)
            """)
    else:
        con.execute(
            "CREATE TEMP TABLE daily AS SELECT ticker, exchange, d, close, volume FROM daily_adj"
        )
    ranks = con.execute("""
        SELECT CAST(q.q AS DATE) AS quarter_start, ticker, any_value(exchange) AS exchange,
               count(*) AS n_days,
               median(close * volume) / 1e6 AS adv_musd,
               arg_max(close, d) AS last_close
        FROM daily JOIN quarters q ON d >= CAST(q.q AS DATE) - INTERVAL 365 DAY AND d < CAST(q.q AS DATE)
        GROUP BY q.q, ticker
        """).df()
    ranks = ranks[
        (ranks["n_days"] >= min_days) & (ranks["last_close"] >= min_price)
    ].copy()
    ranks["quarter_start"] = pd.to_datetime(ranks["quarter_start"]).dt.date
    ranks = ranks.sort_values(
        ["quarter_start", "adv_musd", "ticker"], ascending=[True, False, True]
    )
    ranks["adv_rank"] = ranks.groupby("quarter_start").cumcount() + 1
    return ranks.reset_index(drop=True)


def assign_tiers(ranks: pd.DataFrame, tiers: list[Tier]) -> pd.DataFrame:
    """Tier membership per quarter with hysteresis.

    Per quarter and tier, in order: incumbents (names in this tier or a higher
    one last quarter, not yet placed, with ``rank <= keep``) fill first, best
    rank first; remaining slots go to the best-ranked other names with
    ``rank <= size``. Each tier holds ``size - previous tier size`` names.
    """
    rows: list[pd.DataFrame] = []
    previous: dict[str, int] = {}  # ticker -> tier index last quarter
    for quarter, frame in ranks.groupby("quarter_start", sort=True):
        frame = frame.sort_values("adv_rank")
        placed: dict[str, int] = {}
        floor_size = 0
        for index, tier in enumerate(tiers):
            target = tier.size - floor_size
            free = frame[~frame["ticker"].isin(placed)]
            incumbent_mask = free["ticker"].map(lambda t: previous.get(t, 99) <= index)
            incumbents = free[incumbent_mask & (free["adv_rank"] <= tier.keep)]
            chosen = list(incumbents["ticker"][:target])
            if len(chosen) < target:
                newcomers = free[
                    ~free["ticker"].isin(chosen) & (free["adv_rank"] <= tier.size)
                ]
                chosen += list(newcomers["ticker"][: target - len(chosen)])
            for ticker in chosen:
                placed[ticker] = index
            floor_size = tier.size
        member = frame[frame["ticker"].isin(placed)].copy()
        member["tier"] = member["ticker"].map(lambda t: tiers[placed[t]].name)
        member["role"] = member["ticker"].map(lambda t: tiers[placed[t]].role)
        rows.append(member)
        previous = placed
    if not rows:
        return pd.DataFrame(columns=[*ranks.columns, "tier", "role"])
    return pd.concat(rows, ignore_index=True)


# --------------------------------------------------------------------------- #
# membership and fetch spans
# --------------------------------------------------------------------------- #
def build_membership(
    config: dict[str, Any], ranks: pd.DataFrame, quarters: list[date]
) -> pd.DataFrame:
    """Stock tiers plus the curated lists, one row per quarter and member."""
    adr = set(config.get("adr", {}).get("tickers", []))
    excluded = set(config.get("exclude", {}).get("tickers", []))
    if excluded:
        # ranks arrive sorted by quarter and liquidity, so re-numbering keeps the order
        ranks = ranks[~ranks["ticker"].isin(excluded)].copy()
        ranks["adv_rank"] = ranks.groupby("quarter_start").cumcount() + 1
    stocks = assign_tiers(ranks, config["rules"]["tiers"])
    stocks = stocks.assign(
        asset_type="stock",
        group=stocks["tier"],
        isa_tradable=stocks["ticker"].map(lambda t: "no" if t in adr else "unverified"),
    )
    curated = pd.DataFrame(curated_rows(config))
    fixed = (
        pd.concat(
            [curated.assign(quarter_start=q) for q in quarters], ignore_index=True
        )
        if quarters and not curated.empty
        else pd.DataFrame(columns=MEMBERSHIP_COLUMNS)
    )
    fixed = fixed.assign(adv_rank=pd.NA, adv_musd=float("nan"))
    stocks = stocks.assign(listed_from=None)
    fixed = fixed.assign(listed_from=None)
    out = pd.concat(
        [stocks[MEMBERSHIP_COLUMNS], fixed[MEMBERSHIP_COLUMNS]], ignore_index=True
    )
    out["adv_rank"] = out["adv_rank"].astype("Int64")
    out["adv_musd"] = out["adv_musd"].astype(float).round(2)
    return out.sort_values(
        ["quarter_start", "tier", "adv_rank", "ticker"], na_position="last"
    ).reset_index(drop=True)


def listing_dates(raw_root: Path) -> pd.DataFrame:
    """First daily bar per (ticker, exchange) across every lane's ``prices_daily``.

    Bars are never requested before a pair's first daily price, so a recent
    listing costs no empty history windows, and an empty stretch after it is
    a vendor gap (TSLA has one in 2023-24), not the start of history.
    """
    import duckdb

    con = duckdb.connect()
    parts = []
    for path in sorted(Path(raw_root).glob("*/prices_daily.parquet")):
        cols = {
            c[0]
            for c in con.execute(
                f"SELECT * FROM read_parquet('{path.as_posix()}') LIMIT 0"
            ).description
        }
        if {"ticker", "exchange", "date"} <= cols:
            parts.append(
                f"SELECT ticker, exchange, min(CAST(date AS VARCHAR)) AS d FROM read_parquet('{path.as_posix()}') GROUP BY 1, 2"
            )
    if not parts:
        return pd.DataFrame(columns=["ticker", "exchange", "listed_from"])
    return con.execute(
        "SELECT ticker, exchange, min(d) AS listed_from FROM ("
        + " UNION ALL ".join(parts)
        + ") GROUP BY 1, 2"
    ).df()


def fetch_targets(
    membership: pd.DataFrame, *, history_start: date, warmup_days: int
) -> pd.DataFrame:
    """Per pair, the span of bars to hold: ``fetch_from`` .. ``fetch_to``.

    ``fetch_from`` = first member quarter minus ``warmup_days``, clipped at
    ``history_start`` and at the pair's first daily price (``listed_from``,
    when the membership carries it). ``fetch_to`` is empty for current members
    (latest quarter: fetch up to today) and the end of the last member quarter
    otherwise. Columns: ``ticker, exchange, fetch_from, fetch_to, current``.
    """
    if membership.empty:
        return pd.DataFrame(
            columns=["ticker", "exchange", "fetch_from", "fetch_to", "current"]
        )
    quarters = pd.to_datetime(membership["quarter_start"]).dt.date
    latest = quarters.max()
    frame = membership.assign(q=quarters)
    if "listed_from" not in frame.columns:
        frame["listed_from"] = None
    spans = frame.groupby(["ticker", "exchange"], as_index=False).agg(
        first_q=("q", "min"), last_q=("q", "max"), listed=("listed_from", "first")
    )
    rows = []
    for r in spans.itertuples(index=False):
        start = max(history_start, r.first_q - timedelta(days=warmup_days))
        listed = (
            None
            if r.listed is None or pd.isna(r.listed) or not str(r.listed)
            else date.fromisoformat(str(r.listed)[:10])
        )
        if listed is not None:
            start = max(start, listed)
        current = r.last_q == latest
        rows.append(
            {
                "ticker": r.ticker,
                "exchange": r.exchange,
                "fetch_from": start.isoformat(),
                "fetch_to": "" if current else quarter_end(r.last_q).isoformat(),
                "current": bool(current),
            }
        )
    return pd.DataFrame(rows).sort_values(["exchange", "ticker"]).reset_index(drop=True)


def current_members(membership: pd.DataFrame, targets: pd.DataFrame) -> pd.DataFrame:
    """The latest quarter's members, with a human-readable reason and fetch floor."""
    if membership.empty:
        return pd.DataFrame(columns=CURRENT_COLUMNS)
    latest = pd.to_datetime(membership["quarter_start"]).dt.date.max()
    cur = membership[
        pd.to_datetime(membership["quarter_start"]).dt.date == latest
    ].copy()
    cur = cur.merge(
        targets[["ticker", "exchange", "fetch_from"]],
        on=["ticker", "exchange"],
        how="left",
    )

    def reason(row: Any) -> str:
        if row.asset_type == "stock":
            return f"adv rank {int(row.adv_rank)} (${row.adv_musd:,.0f}M/day)"
        return f"{row.tier}:{row.group}"

    cur["reason"] = [reason(r) for r in cur.itertuples(index=False)]
    return cur[CURRENT_COLUMNS].reset_index(drop=True)


def load_membership(path: Path = MEMBERSHIP_PATH) -> pd.DataFrame:
    return pd.read_parquet(path)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def stock_sources(
    config: dict[str, Any], raw_root: Path
) -> list[tuple[str, Path, Path | None, Path | None]]:
    universes = {"us_extended": "tickers_US_EXT.parquet"}
    out = []
    for lane in config["rules"]["stock_lanes"]:
        uni = universes.get(lane)
        out.append(
            (
                lane,
                raw_root / lane / "prices_daily.parquet",
                (raw_root / lane / uni) if uni else None,
                raw_root / lane / "splits_history.parquet",
            )
        )
    return out


def build(
    *,
    as_of: date,
    config: dict[str, Any],
    raw_root: Path,
    out_dir: Path,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = config["rules"]
    history_start = date.fromisoformat(rules["history_start"])
    quarters = quarter_starts(history_start, as_of)
    ranks = load_quarter_ranks(
        stock_sources(config, raw_root),
        quarters,
        min_price=float(rules["min_price"]),
        min_days=int(rules["min_days"]),
    )
    membership = build_membership(config, ranks, quarters)
    membership = membership.drop(columns=["listed_from"], errors="ignore").merge(
        listing_dates(raw_root), on=["ticker", "exchange"], how="left"
    )[MEMBERSHIP_COLUMNS]
    targets = fetch_targets(
        membership, history_start=history_start, warmup_days=int(rules["warmup_days"])
    )
    current = current_members(membership, targets)
    out = membership.assign(
        quarter_start=membership["quarter_start"].map(
            lambda d: pd.Timestamp(d).date().isoformat()
        )
    )
    _atomic.to_parquet(out, out_dir / MEMBERSHIP_PATH.name, index=False)
    _atomic.to_parquet(current, out_dir / TICKERS_PATH.name, index=False)
    return membership, current


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--as-of", default="", help="Build as of YYYY-MM-DD (default: today, UTC)."
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Universe toml (default: eodhd/intraday_universe.toml).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    as_of = (
        date.fromisoformat(args.as_of)
        if args.as_of
        else datetime.now(timezone.utc).date()
    )
    config = load_config(Path(args.config))
    membership, current = build(
        as_of=as_of, config=config, raw_root=EODHD_RAW_ROOT, out_dir=RAW_DIR
    )
    pairs = membership[["ticker", "exchange"]].drop_duplicates()
    counts = current["tier"].value_counts().to_dict()
    print(
        f"intraday universe as of {as_of}: {len(current)} current members "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        + f"; {len(pairs)} pairs ever included across "
        f"{membership['quarter_start'].nunique()} quarters -> {TICKERS_PATH.name}, {MEMBERSHIP_PATH.name}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
