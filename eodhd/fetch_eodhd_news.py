"""Crawl the EODHD global news feed by day into the ``news`` lane.

One global crawl per UTC day (``/news?from=D&to=D`` paginated by ``offset``),
deduplicated on a hash of the article link and stored as one parquet partition
per publication day. Symbol / tag / sentiment aggregates are derived downstream
from this corpus, so no per-ticker ``/news`` or ``/sentiments`` calls are made.

See ``EODHD_NEWS_SENTIMENT_FINDINGS.md`` for the measured feed characteristics
that drove this design (5 API units per page regardless of ``limit``, ~3-4 pages
and ~7 MB on disk per day, history dense from 2021). Daily partitions keep each
flush a small write and each re-crawl a single-file rewrite.

Usage:
    uv run python eodhd/fetch_eodhd_news.py                       # resume / top-up
    uv run python eodhd/fetch_eodhd_news.py --from 2026-08-01     # bounded window
    uv run python eodhd/fetch_eodhd_news.py --limit-days 3        # smoke test
    uv run python eodhd/fetch_eodhd_news.py --full-refresh --from 2026-07-01

Outputs:
    data/raw/eodhd/news/articles/YYYY-MM-DD.parquet
    data/raw/eodhd/news/news_fetch_state.csv
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests
import _atomic
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_us_fundamentals import _get_api_key

RAW_DIR = EODHD_RAW_ROOT / "news"
ARTICLES_DIR = RAW_DIR / "articles"
STATE_PATH = RAW_DIR / "news_fetch_state.csv"

EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 120
DELAY = 0.12
RETRY_SLEEP = 5.0
MAX_ATTEMPTS = 3
PAGE_SIZE = 1000
MAX_PAGES = 50
FLUSH_EVERY_DAYS = 5
DEFAULT_FROM = "2021-01-01"
DEFAULT_OVERLAP_DAYS = 2

#: Column order of the article partitions (see the findings doc, §5.2).
ARTICLE_COLUMNS = [
    "article_id",
    "date",
    "published_at",
    "title",
    "content",
    "link",
    "source",
    "symbols",
    "tags",
    "polarity",
    "neg",
    "neu",
    "pos",
    "fetched_at",
]

#: Pinned Arrow schema so every partition is byte-compatible for glob reads
#: (otherwise an all-empty ``tags`` month would be inferred as ``list<null>``).
ARTICLE_SCHEMA = pa.schema(
    [
        ("article_id", pa.string()),
        ("date", pa.date32()),
        ("published_at", pa.timestamp("us", tz="UTC")),
        ("title", pa.string()),
        ("content", pa.string()),
        ("link", pa.string()),
        ("source", pa.string()),
        ("symbols", pa.list_(pa.string())),
        ("tags", pa.list_(pa.string())),
        ("polarity", pa.float64()),
        ("neg", pa.float64()),
        ("neu", pa.float64()),
        ("pos", pa.float64()),
        ("fetched_at", pa.timestamp("us", tz="UTC")),
    ]
)

STATE_COLUMNS = [
    "date",
    "status",
    "pages",
    "articles",
    "unique_articles",
    "min_published",
    "max_published",
    "fetched_at",
    "detail",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_news")


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #


def article_id(link: str) -> str:
    """Stable 16-hex identity for an article, derived from its link."""
    return hashlib.sha1(link.strip().encode("utf-8")).hexdigest()[:16]


def _source(link: str) -> str | None:
    try:
        host = urlparse(link).netloc.lower()
    except ValueError:
        return None
    return host or None


def _as_str_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    try:
        return [str(v) for v in value if v is not None and str(v) != ""]
    except TypeError:
        return []


def normalize_articles(
    raw: list[dict], *, fetched_at: str, crawl_day: date | None = None
) -> pd.DataFrame:
    """Shape a raw ``/news`` page into the article schema.

    Rows without a link are dropped (no identity). ``symbols``/``tags`` become
    plain string lists; the sentiment dict is flattened; ``date`` is the UTC
    publication day, falling back to ``crawl_day`` when the vendor timestamp
    does not parse (so every row lands in a real partition).
    """
    records: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        link = str(item.get("link") or "").strip()
        if not link:
            continue
        sentiment = item.get("sentiment") or {}
        if not isinstance(sentiment, dict):
            sentiment = {}
        records.append(
            {
                "article_id": article_id(link),
                "published_at": item.get("date"),
                "title": item.get("title"),
                "content": item.get("content"),
                "link": link,
                "source": _source(link),
                "symbols": _as_str_list(item.get("symbols")),
                "tags": _as_str_list(item.get("tags")),
                "polarity": sentiment.get("polarity"),
                "neg": sentiment.get("neg"),
                "neu": sentiment.get("neu"),
                "pos": sentiment.get("pos"),
                "fetched_at": fetched_at,
            }
        )
    if not records:
        return pd.DataFrame(columns=ARTICLE_COLUMNS)
    df = pd.DataFrame(records)
    df["published_at"] = pd.to_datetime(
        df["published_at"], utc=True, errors="coerce", format="ISO8601"
    )
    df["fetched_at"] = pd.to_datetime(
        df["fetched_at"], utc=True, errors="coerce", format="ISO8601"
    )
    day = df["published_at"].dt.date
    if crawl_day is not None:
        day = day.where(df["published_at"].notna(), crawl_day)
    df["date"] = day
    for col in ("polarity", "neg", "neu", "pos"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df[ARTICLE_COLUMNS]


def merge_articles(existing: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    """Upsert ``new`` into ``existing`` on ``article_id`` (last write wins).

    Uniqueness is *per partition*: the vendor re-publishes some articles under
    the same link with a later timestamp (market wraps updated a few hours on),
    and when that crosses midnight UTC both versions are kept, one per day.
    Downstream views that need one row per article take the latest
    ``published_at`` per ``article_id``.
    """
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new.copy()
    if merged.empty:
        return merged
    merged = merged.drop_duplicates(subset=["article_id"], keep="last")
    return merged.sort_values(["published_at", "article_id"]).reset_index(drop=True)


def partition_key(day: date | str) -> str:
    """``YYYY-MM-DD`` partition name for a publication day (one file per day)."""
    text = day.isoformat() if isinstance(day, date) else str(day)
    return text[:10]


def partition_path(key: str, articles_dir: Path = ARTICLES_DIR) -> Path:
    return articles_dir / f"{key}.parquet"


def write_partition(df: pd.DataFrame, path: Path) -> None:
    """Write one daily partition under the pinned :data:`ARTICLE_SCHEMA`."""
    table = pa.Table.from_pandas(
        df[ARTICLE_COLUMNS], schema=ARTICLE_SCHEMA, preserve_index=False
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_table(table, path, compression="zstd")
def read_partition(path: Path) -> pd.DataFrame | None:
    return pd.read_parquet(path) if path.exists() else None


def build_state_lookup(state: pd.DataFrame | None) -> dict[str, dict[str, object]]:
    if state is None or state.empty or "date" not in state.columns:
        return {}
    return {
        str(row["date"]): row
        for row in state.to_dict(orient="records")
        if str(row.get("date", "")).strip()
    }


def plan_days(
    *,
    from_date: str,
    to_date: str,
    state_lookup: dict[str, dict[str, object]],
    overlap_days: int,
    full_refresh: bool,
) -> list[date]:
    """Days to crawl this run, **newest first**.

    A day is skipped when the state already records it as ``ok``, except the
    trailing ``overlap_days`` before ``to_date`` (inclusive), which are always
    re-crawled because the vendor's current-day pages shift as articles arrive.

    Newest-first means a run capped with ``--limit-days`` always refreshes the
    most recent days and only then works backwards into history, so a routine
    top-up keeps the corpus current while a backfill fills in behind it.
    """
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if end < start:
        return []
    overlap_from = end - timedelta(days=max(overlap_days, 0))
    days: list[date] = []
    cursor = end
    while cursor >= start:
        prior = state_lookup.get(cursor.isoformat())
        done = prior is not None and str(prior.get("status", "")).strip() == "ok"
        if full_refresh or not done or cursor >= overlap_from:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def merge_state_rows(
    existing: pd.DataFrame | None, new_rows: list[dict[str, object]]
) -> pd.DataFrame:
    new_df = pd.DataFrame(new_rows, columns=STATE_COLUMNS)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df
    merged = merged.drop_duplicates(subset=["date"], keep="last")
    return merged.sort_values("date").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def _get_page(
    session: requests.Session, day: date, *, offset: int, page_size: int
) -> tuple[list[dict] | None, str, str]:
    """Fetch one page; returns ``(rows, status, detail)``. Retries 429/5xx."""
    params = {
        "from": day.isoformat(),
        "to": day.isoformat(),
        "limit": page_size,
        "offset": offset,
        "fmt": "json",
    }
    detail = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(
                f"{EODHD_BASE}/news", params=params, timeout=HTTP_TIMEOUT
            )
        except requests.RequestException as exc:
            detail = f"request_error: {exc.__class__.__name__}"
            if attempt < MAX_ATTEMPTS:
                time.sleep(RETRY_SLEEP * attempt)
                continue
            return None, "request_error", detail
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError:
                return None, "decode_error", "decode_error"
            if not isinstance(data, list):
                return None, "decode_error", f"unexpected payload: {type(data)}"
            return data, "ok", ""
        detail = response.text[:200]
        if response.status_code in (429, 500, 502, 503, 504) and attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_SLEEP * attempt)
            continue
        return None, f"http_{response.status_code}", detail
    return None, "request_error", detail


def fetch_day(
    session: requests.Session,
    day: date,
    *,
    page_size: int = PAGE_SIZE,
    max_pages: int = MAX_PAGES,
) -> tuple[list[dict], int, str, str]:
    """Crawl every page of one UTC day.

    Returns ``(rows, pages_fetched, status, detail)``. A failure mid-way keeps
    the rows already collected but reports the failure status so the day is
    re-crawled on the next run.
    """
    rows: list[dict] = []
    pages = 0
    for page in range(max_pages):
        data, status, detail = _get_page(
            session, day, offset=page * page_size, page_size=page_size
        )
        if data is None:
            return rows, pages, status, detail
        pages += 1
        rows.extend(data)
        time.sleep(DELAY)
        if len(data) < page_size:
            return rows, pages, ("ok" if rows else "empty"), ""
    return rows, pages, "ok", f"max_pages={max_pages} reached"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crawl the EODHD global news feed by day into the news lane"
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        default=DEFAULT_FROM,
        help=f"First UTC day to crawl (YYYY-MM-DD, default {DEFAULT_FROM})",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Last UTC day to crawl (YYYY-MM-DD); defaults to today UTC",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=DEFAULT_OVERLAP_DAYS,
        help="Trailing days always re-crawled to absorb late-arriving articles",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Ignore fetch state and re-crawl every day in the window",
    )
    parser.add_argument(
        "--limit-days",
        type=int,
        default=0,
        help="Crawl at most this many pending days this run (0 = no cap)",
    )
    parser.add_argument(
        "--page-size", type=int, default=PAGE_SIZE, help="Articles per page (<=1000)"
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help="Safety cap on pages per day",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the pending-day plan and the API-unit estimate; crawl nothing",
    )
    return parser.parse_args()


#: Planning constants (measured on the 2021-2026 backfill: 5,511 pages / 2,053 days).
PAGES_PER_DAY = 2.7
UNITS_PER_PAGE = 5


def crawl_estimate(n_days: int) -> tuple[int, int]:
    """``(pages, api_units)`` a crawl of ``n_days`` will roughly cost."""
    pages = int(round(n_days * PAGES_PER_DAY))
    return pages, pages * UNITS_PER_PAGE


def main() -> None:
    args = parse_args()

    existing_state = pd.read_csv(STATE_PATH, dtype=str) if STATE_PATH.exists() else None
    state_lookup = build_state_lookup(existing_state)
    days = plan_days(
        from_date=args.from_date,
        to_date=args.to_date,
        state_lookup=state_lookup,
        overlap_days=args.overlap_days,
        full_refresh=args.full_refresh,
    )
    if args.limit_days > 0:
        days = days[: args.limit_days]
    pages, units = crawl_estimate(len(days))
    log.info(
        "News crawl: %d day(s) pending in %s..%s (overlap=%d, full_refresh=%s) "
        "~%d pages ~%d API units",
        len(days),
        args.from_date,
        args.to_date,
        args.overlap_days,
        args.full_refresh,
        pages,
        units,
    )
    if args.dry_run:
        if days:
            log.info(
                "dry-run: newest %s .. oldest %s; nothing crawled. Re-run without "
                "--dry-run to fetch.",
                days[0].isoformat(),
                days[-1].isoformat(),
            )
        else:
            log.info("dry-run: nothing pending.")
        return
    if not days:
        return

    api_key = _get_api_key()  # only a real crawl needs the key
    session = requests.Session()
    session.params = {"api_token": api_key}

    pending_frames: dict[str, list[pd.DataFrame]] = {}
    new_state_rows: list[dict[str, object]] = []
    days_since_flush = 0
    total_articles = 0
    total_pages = 0
    failures = 0

    def _flush() -> None:
        nonlocal existing_state, days_since_flush
        for key, frames in sorted(pending_frames.items()):
            path = partition_path(key)
            new_df = pd.concat(frames, ignore_index=True)
            merged = merge_articles(read_partition(path), new_df)
            write_partition(merged, path)
            log.info("  [FLUSH] %s: %d rows on disk", path.name, len(merged))
        pending_frames.clear()
        if new_state_rows:
            merged_state = merge_state_rows(existing_state, new_state_rows)
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(merged_state, STATE_PATH, index=False)
            existing_state = merged_state
            new_state_rows.clear()
        days_since_flush = 0

    for i, day in enumerate(days, start=1):
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows, pages, status, detail = fetch_day(
            session, day, page_size=args.page_size, max_pages=args.max_pages
        )
        df = normalize_articles(rows, fetched_at=fetched_at, crawl_day=day)
        total_pages += pages
        total_articles += len(df)
        if status not in ("ok", "empty"):
            failures += 1
        elif detail:
            log.warning("%s: %s", day.isoformat(), detail)
        # Articles are bucketed by their own publication day, which normally
        # equals the crawled day but is not assumed to.
        if not df.empty:
            for key, part in df.groupby(df["date"].map(partition_key)):
                pending_frames.setdefault(str(key), []).append(part)
        new_state_rows.append(
            {
                "date": day.isoformat(),
                "status": status,
                "pages": pages,
                "articles": len(rows),
                "unique_articles": (
                    int(df["article_id"].nunique()) if not df.empty else 0
                ),
                "min_published": (
                    df["published_at"].min().isoformat() if not df.empty else None
                ),
                "max_published": (
                    df["published_at"].max().isoformat() if not df.empty else None
                ),
                "fetched_at": fetched_at,
                "detail": detail,
            }
        )
        log.info(
            "%s  %-13s pages=%d articles=%d unique=%d  (%d/%d)",
            day.isoformat(),
            status,
            pages,
            len(rows),
            int(df["article_id"].nunique()) if not df.empty else 0,
            i,
            len(days),
        )
        days_since_flush += 1
        if days_since_flush >= FLUSH_EVERY_DAYS:
            _flush()

    _flush()
    log.info(
        "Done: days=%d pages=%d articles=%d failures=%d",
        len(days),
        total_pages,
        total_articles,
        failures,
    )


if __name__ == "__main__":
    main()
