"""Build the derived daily panel ``news_symbol_daily`` from the news corpus.

One row per ``(date, ticker, exchange)`` for every symbol tag in the corpus:
how many articles tagged the symbol that UTC day, that count as a share of the
day's global volume (the feature to use -- volume is not stationary across
years), source diversity, vendor-sentiment aggregates, and the intraday bounds.
Built locally with DuckDB from ``news/articles/*.parquet``; no API calls.

Incremental: a day is rebuilt when its article partition is newer than the panel
file (or missing from it); ``--full`` rebuilds everything. ``refresh`` runs this
after the news top-up so the panel stays current without extra flags.

Usage:
    uv run python eodhd/build_news_symbol_daily.py            # incremental
    uv run python eodhd/build_news_symbol_daily.py --full     # rebuild all days
    uv run python eodhd/build_news_symbol_daily.py --since 2026-08-01

Outputs:
    data/raw/eodhd/news/news_symbol_daily.parquet
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

RAW_DIR = EODHD_RAW_ROOT / "news"
ARTICLES_DIR = RAW_DIR / "articles"
PANEL_PATH = RAW_DIR / "news_symbol_daily.parquet"

#: Vendor polarity beyond this counts as positive / negative.
DEAD_BAND = 0.05
#: An article with at most this many symbol tags is "about" each of them.
SOLO_MAX_TAGS = 3

PANEL_SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("ticker", pa.string()),
        ("exchange", pa.string()),
        ("symbol", pa.string()),
        ("n_articles", pa.int64()),
        ("n_articles_day", pa.int64()),
        ("share_of_day", pa.float64()),
        ("n_sources", pa.int64()),
        ("n_solo", pa.int64()),
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
log = logging.getLogger("news_symbol_daily")


# --------------------------------------------------------------------------- #
# pure pieces
# --------------------------------------------------------------------------- #
def partition_days(articles_dir: Path) -> dict[str, float]:
    """``{YYYY-MM-DD: mtime}`` for every article partition on disk."""
    out: dict[str, float] = {}
    for p in articles_dir.glob("*.parquet"):
        if len(p.stem) == 10:
            out[p.stem] = p.stat().st_mtime
    return out


def days_to_rebuild(
    partitions: dict[str, float],
    panel_days: set[str],
    panel_mtime: float | None,
    *,
    full: bool = False,
    since: str | None = None,
) -> list[str]:
    """Which days need (re)building.

    A day is due when the panel has no rows for it, or its article partition was
    written after the panel file (a re-crawl / top-up), or ``full``. ``since``
    narrows the candidate set (still incremental within it).
    """
    days = sorted(d for d in partitions if since is None or d >= since)
    if full or panel_mtime is None:
        return days
    return [d for d in days if d not in panel_days or partitions[d] > panel_mtime]


def build_days(
    con: Any, articles_glob: str, days: list[str], *, built_at: str
) -> pd.DataFrame:
    """Aggregate the panel rows for ``days`` from the article partitions."""
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
        day_tot AS (
          SELECT date, count(*) AS n_articles_day FROM latest GROUP BY date
        ),
        exploded AS (
          SELECT l.article_id, l.date, l.published_at, l.source, l.polarity,
                 upper(trim(t.s)) AS symbol, len(l.symbols) AS n_tags
          FROM latest l, unnest(l.symbols) AS t(s)
          WHERE t.s IS NOT NULL AND trim(t.s) <> ''
        ),
        agg AS (
          SELECT date, symbol,
                 count(DISTINCT article_id) AS n_articles,
                 count(DISTINCT source) AS n_sources,
                 count(DISTINCT CASE WHEN n_tags <= {SOLO_MAX_TAGS} THEN article_id END) AS n_solo,
                 avg(polarity) AS polarity_mean,
                 stddev_samp(polarity) AS polarity_std,
                 avg(CASE WHEN polarity > {DEAD_BAND} THEN 1.0 ELSE 0.0 END) AS pos_share,
                 avg(CASE WHEN polarity < -{DEAD_BAND} THEN 1.0 ELSE 0.0 END) AS neg_share,
                 min(published_at) AS first_published_at,
                 max(published_at) AS last_published_at
          FROM exploded GROUP BY date, symbol
        )
        SELECT a.date, a.symbol, a.n_articles, d.n_articles_day,
               a.n_articles * 1.0 / d.n_articles_day AS share_of_day,
               a.n_sources, a.n_solo, a.polarity_mean, a.polarity_std, a.pos_share, a.neg_share,
               a.first_published_at, a.last_published_at
        FROM agg a JOIN day_tot d USING (date)
        ORDER BY a.date, a.symbol
        """,
        days,
    ).df()
    if df.empty:
        return pd.DataFrame(columns=PANEL_COLUMNS)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    for col in ("first_published_at", "last_published_at"):
        df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    split = df["symbol"].str.rsplit(".", n=1, expand=True)
    df["ticker"] = split[0]
    df["exchange"] = split[1] if split.shape[1] > 1 else None
    df["built_at"] = built_at
    return df[PANEL_COLUMNS]


def merge_panel(
    existing: pd.DataFrame | None, rebuilt: pd.DataFrame, days: list[str]
) -> pd.DataFrame:
    """Replace the rebuilt days in the existing panel."""
    if existing is not None and not existing.empty:
        existing = existing.copy()
        existing["date"] = pd.to_datetime(existing["date"], errors="coerce").dt.date
        keep = existing[~existing["date"].astype(str).isin(set(days))]
        merged = pd.concat([keep, rebuilt], ignore_index=True)
    else:
        merged = rebuilt
    if merged.empty:
        return merged
    return merged.sort_values(["date", "symbol"]).reset_index(drop=True)


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
    _atomic.write_table(table, path, compression="zstd")
def read_panel_days(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        col = pq.read_table(path, columns=["date"]).column("date").to_pandas()
    except Exception:
        return set()
    return {str(d) for d in col.dropna().unique()}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build(
    *,
    articles_dir: Path = ARTICLES_DIR,
    panel_path: Path = PANEL_PATH,
    full: bool = False,
    since: str | None = None,
    chunk_days: int = 60,
) -> dict[str, Any]:
    """Incrementally (re)build the panel. Returns a summary dict."""
    import duckdb

    partitions = partition_days(articles_dir)
    if not partitions:
        log.warning(
            "no article partitions under %s -- crawl the news lane first", articles_dir
        )
        return {"days_rebuilt": 0, "rows": 0}
    panel_mtime = panel_path.stat().st_mtime if panel_path.exists() else None
    panel_days = read_panel_days(panel_path)
    due = days_to_rebuild(partitions, panel_days, panel_mtime, full=full, since=since)
    log.info(
        "news_symbol_daily: %d partition day(s), %d in panel, %d to (re)build%s",
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
    con.execute("SET TimeZone='UTC'")  # keep the intraday bounds in UTC
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
        description="Build the derived news_symbol_daily panel from the news corpus (local, no API)"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rebuild every day (default: only new/changed partitions)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only consider partitions on/after this day (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--chunk-days",
        type=int,
        default=60,
        help="Days per DuckDB pass (memory bound; default 60)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    build(full=args.full, since=args.since, chunk_days=args.chunk_days)


if __name__ == "__main__":
    main()
