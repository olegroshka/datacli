"""Build ``news_issuer_daily``: the daily news panel at *issuer* grain.

``news_symbol_daily`` counts articles per symbol tag; because EODHD tags an
issuer's US line / ADR / mirror lines far more often than its home line, an
EU/UK ticker under-counts badly (``SAP.XETRA`` 1 vs ``SAP.US`` 248 in a month).
This panel maps every tag to its issuer via ``issuer_map.parquet``, counts each
article **once per issuer**, and then writes one row per ``(date, ticker,
exchange)`` for every *universe* member of that issuer -- so ``describe
SAP.XETRA`` shows SAP SE's whole news flow, and the panel is ticker-keyed like
the rest of the lane.

Columns: ``date, ticker, exchange, issuer_id, issuer_name, primary_symbol,
n_articles, n_articles_day, share_of_day, n_sources, n_solo (articles tagging
<= 3 distinct issuers), n_symbols (distinct lines of the issuer tagged that day),
polarity_mean/std, pos_share, neg_share, first/last_published_at, built_at``.

Rows are written for every member that is in the price universe **or** is a
lane firm (has fundamentals), so a UK/EU issuer that did not qualify for the
price pull still gets its panel.

Incremental like the symbol panel; a newer ``issuer_map.parquet`` forces a full
rebuild (the mapping changed). Universe symbols missing from the map count as
their own issuer (``SYM:<symbol>``) so every ticker gets rows.

Usage:
    uv run python eodhd/build_news_issuer_daily.py           # incremental
    uv run python eodhd/build_news_issuer_daily.py --full

Outputs:
    data/raw/eodhd/news/news_issuer_daily.parquet
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import _atomic
from _datadir import EODHD_RAW_ROOT
from build_news_symbol_daily import (
    DEAD_BAND,
    days_to_rebuild,
    partition_days,
    read_panel_days,
)

RAW_DIR = EODHD_RAW_ROOT / "news"
ARTICLES_DIR = RAW_DIR / "articles"
MAP_PATH = RAW_DIR / "issuer_map.parquet"
PANEL_PATH = RAW_DIR / "news_issuer_daily.parquet"
SOLO_MAX_ISSUERS = 3

PANEL_SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("ticker", pa.string()),
        ("exchange", pa.string()),
        ("issuer_id", pa.string()),
        ("issuer_name", pa.string()),
        ("primary_symbol", pa.string()),
        ("n_articles", pa.int64()),
        ("n_articles_day", pa.int64()),
        ("share_of_day", pa.float64()),
        ("n_sources", pa.int64()),
        ("n_solo", pa.int64()),
        ("n_symbols", pa.int64()),
        ("polarity_mean", pa.float64()),
        ("polarity_std", pa.float64()),
        ("pos_share", pa.float64()),
        ("neg_share", pa.float64()),
        ("first_published_at", pa.timestamp("us", tz="UTC")),
        ("last_published_at", pa.timestamp("us", tz="UTC")),
        ("built_at", pa.timestamp("us", tz="UTC")),
    ]
)
PANEL_COLUMNS = PANEL_SCHEMA.names

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("news_issuer_daily")


def universe_symbols(root: Path = EODHD_RAW_ROOT) -> set[str]:
    out: set[str] = set()
    for state in root.glob("*/prices_fetch_state.csv"):
        try:
            df = pd.read_csv(state, usecols=["ticker", "exchange"], dtype=str)
        except Exception:
            continue
        out |= {
            f"{t}.{e}".upper()
            for t, e in zip(df["ticker"], df["exchange"])
            if isinstance(t, str) and isinstance(e, str)
        }
    return out


def install_mapping(con: Any, issuer_map: pd.DataFrame, universe: set[str]) -> None:
    """Temp tables ``_sym2issuer`` (every mapped symbol + unmapped universe
    symbols as their own issuer) and ``_members`` (universe symbols per issuer)."""
    m = issuer_map[["symbol", "issuer_id", "issuer_name", "primary_symbol"]].copy()
    mapped = set(m["symbol"])
    extra = sorted(universe - mapped)
    if extra:
        m = pd.concat(
            [
                m,
                pd.DataFrame(
                    {
                        "symbol": extra,
                        "issuer_id": [f"SYM:{s}" for s in extra],
                        "issuer_name": [None] * len(extra),
                        "primary_symbol": extra,
                    }
                ),
            ],
            ignore_index=True,
        )
    m["symbol"] = m["symbol"].str.upper()
    con.register("_sym2issuer_df", m)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _sym2issuer AS SELECT * FROM _sym2issuer_df"
    )
    # members = price universe + every lane firm (anything with fundamentals),
    # so an issuer gets rows for each of *our* tickers, not only the priced ones
    lane_syms = set(issuer_map.loc[issuer_map["lane"].notna(), "symbol"].str.upper())
    members = m[m["symbol"].isin(universe | lane_syms)][["symbol", "issuer_id"]].copy()
    split = members["symbol"].str.rsplit(".", n=1, expand=True)
    members["ticker"] = split[0]
    members["exchange"] = split[1] if split.shape[1] > 1 else None
    con.register("_members_df", members)
    con.execute("CREATE OR REPLACE TEMP TABLE _members AS SELECT * FROM _members_df")


def build_days(
    con: Any, articles_glob: str, days: list[str], *, built_at: str
) -> pd.DataFrame:
    if not days:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    placeholders = ", ".join("?" for _ in days)
    df = con.execute(
        f"""
        WITH latest AS (
          SELECT article_id, date, published_at, source, symbols, polarity
          FROM read_parquet('{articles_glob}')
          WHERE cast(date AS VARCHAR) IN ({placeholders})
          QUALIFY row_number() OVER (PARTITION BY article_id, date ORDER BY published_at DESC) = 1
        ),
        day_tot AS (SELECT date, count(*) AS n_articles_day FROM latest GROUP BY date),
        tagged AS (
          SELECT l.article_id, l.date, l.published_at, l.source, l.polarity,
                 upper(trim(t.s)) AS symbol,
                 coalesce(m.issuer_id, 'SYM:' || upper(trim(t.s))) AS issuer_id
          FROM latest l, unnest(l.symbols) AS t(s)
          LEFT JOIN _sym2issuer m ON m.symbol = upper(trim(t.s))
          WHERE t.s IS NOT NULL AND trim(t.s) <> ''
        ),
        per_article AS (
          SELECT article_id, count(DISTINCT issuer_id) AS n_issuers FROM tagged GROUP BY article_id
        ),
        -- one row per (article, issuer): an article tagging five lines of the
        -- same issuer counts once for that issuer
        art_iss AS (
          SELECT date, article_id, issuer_id, count(DISTINCT symbol) AS n_lines
          FROM tagged GROUP BY date, article_id, issuer_id
        ),
        lines_day AS (
          SELECT date, issuer_id, count(DISTINCT symbol) AS n_symbols FROM tagged GROUP BY date, issuer_id
        ),
        agg AS (
          SELECT ai.date, ai.issuer_id,
                 count(*) AS n_articles,
                 count(DISTINCT l.source) AS n_sources,
                 count(CASE WHEN pa.n_issuers <= {SOLO_MAX_ISSUERS} THEN 1 END) AS n_solo,
                 avg(l.polarity) AS polarity_mean,
                 stddev_samp(l.polarity) AS polarity_std,
                 avg(CASE WHEN l.polarity > {DEAD_BAND} THEN 1.0 ELSE 0.0 END) AS pos_share,
                 avg(CASE WHEN l.polarity < -{DEAD_BAND} THEN 1.0 ELSE 0.0 END) AS neg_share,
                 min(l.published_at) AS first_published_at,
                 max(l.published_at) AS last_published_at
          FROM art_iss ai
          JOIN per_article pa USING (article_id)
          JOIN latest l USING (article_id)
          GROUP BY ai.date, ai.issuer_id
        )
        SELECT a.date, mb.ticker, mb.exchange, a.issuer_id, s.issuer_name, s.primary_symbol,
               a.n_articles, d.n_articles_day, a.n_articles * 1.0 / d.n_articles_day AS share_of_day,
               a.n_sources, a.n_solo, ld.n_symbols, a.polarity_mean, a.polarity_std,
               a.pos_share, a.neg_share, a.first_published_at, a.last_published_at
        FROM agg a
        JOIN day_tot d USING (date)
        JOIN lines_day ld ON ld.date = a.date AND ld.issuer_id = a.issuer_id
        JOIN _members mb ON mb.issuer_id = a.issuer_id
        LEFT JOIN (SELECT issuer_id, any_value(issuer_name) AS issuer_name, any_value(primary_symbol) AS primary_symbol
                   FROM _sym2issuer GROUP BY issuer_id) s ON s.issuer_id = a.issuer_id
        ORDER BY a.date, mb.ticker, mb.exchange
        """,
        days,
    ).df()
    if df.empty:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("first_published_at", "last_published_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df["built_at"] = built_at
    return df[PANEL_COLUMNS]


def merge_panel(
    existing: pd.DataFrame | None, rebuilt: pd.DataFrame, days: list[str]
) -> pd.DataFrame:
    if existing is not None and not existing.empty:
        existing = existing.copy()
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce").dt.date
        keep = existing[~existing["date"].astype(str).isin(set(days))]
        merged = pd.concat([keep, rebuilt], ignore_index=True)
    else:
        merged = rebuilt
    if merged.empty:
        return merged
    return merged.sort_values(["date", "ticker", "exchange"]).reset_index(drop=True)


def write_panel(df: pd.DataFrame, path: Path) -> None:
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce").dt.date
    for col in ("first_published_at", "last_published_at", "built_at"):
        work[col] = pd.to_datetime(work[col], utc=True, errors="coerce")
    for col in PANEL_COLUMNS:
        if col not in work.columns:
            work[col] = None
    table = pa.Table.from_pandas(
        work[PANEL_COLUMNS], schema=PANEL_SCHEMA, preserve_index=False
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    _atomic.write_table(table, tmp, compression="zstd")
    tmp.replace(path)


def build(
    *,
    root: Path = EODHD_RAW_ROOT,
    articles_dir: Path = ARTICLES_DIR,
    map_path: Path = MAP_PATH,
    panel_path: Path = PANEL_PATH,
    full: bool = False,
    since: str | None = None,
    chunk_days: int = 60,
    universe: set[str] | None = None,
) -> dict[str, Any]:
    import duckdb

    if not map_path.exists():
        log.warning("no %s -- run build_issuer_map.py first", map_path)
        return {"days_rebuilt": 0, "rows": 0}
    partitions = partition_days(articles_dir)
    if not partitions:
        log.warning("no article partitions under %s", articles_dir)
        return {"days_rebuilt": 0, "rows": 0}
    panel_mtime = panel_path.stat().st_mtime if panel_path.exists() else None
    if panel_mtime is not None and map_path.stat().st_mtime > panel_mtime:
        log.info("issuer_map.parquet is newer than the panel -> full rebuild")
        full = True
    panel_days = read_panel_days(panel_path)
    due = days_to_rebuild(partitions, panel_days, panel_mtime, full=full, since=since)
    log.info(
        "news_issuer_daily: %d partition day(s), %d in panel, %d to (re)build%s",
        len(partitions),
        len(panel_days),
        len(due),
        " (full)" if full else "",
    )
    if not due:
        return {
            "days_rebuilt": 0,
            "rows": (
                int(pq.ParquetFile(panel_path).metadata.num_rows)
                if panel_path.exists()
                else 0
            ),
        }
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    install_mapping(
        con,
        pd.read_parquet(map_path),
        universe if universe is not None else universe_symbols(root),
    )
    glob = (articles_dir / "*.parquet").as_posix()
    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    existing = (
        pd.read_parquet(panel_path) if (panel_path.exists() and not full) else None
    )
    t0 = time.perf_counter()
    frames = []
    for start in range(0, len(due), chunk_days):
        chunk = due[start : start + chunk_days]
        frames.append(build_days(con, glob, chunk, built_at=built_at))
        log.info(
            "  built %d/%d day(s)  %.0fs",
            min(start + chunk_days, len(due)),
            len(due),
            time.perf_counter() - t0,
        )
    rebuilt = (
        pd.concat(frames, ignore_index=True)
        if frames
        else pd.DataFrame(columns=PANEL_COLUMNS)
    )
    merged = merge_panel(existing, rebuilt, due)
    write_panel(merged, panel_path)
    log.info(
        "wrote %s: %d rows (%d rebuilt over %d day(s)) in %.0fs",
        panel_path.name,
        len(merged),
        len(rebuilt),
        len(due),
        time.perf_counter() - t0,
    )
    return {
        "days_rebuilt": len(due),
        "rows": int(len(merged)),
        "rebuilt_rows": int(len(rebuilt)),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the issuer-grain daily news panel (local, no API)"
    )
    parser.add_argument("--full", action="store_true", help="Rebuild every day")
    parser.add_argument(
        "--since", default=None, help="Only consider partitions on/after this day"
    )
    parser.add_argument("--chunk-days", type=int, default=60)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build(full=args.full, since=args.since, chunk_days=args.chunk_days)


if __name__ == "__main__":
    main()
