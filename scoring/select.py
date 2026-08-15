"""Target selection: which articles a run scores, straight from the DuckDB views.

Selection is per publication day (the partition unit) so memory stays bounded and
runs resume cleanly. Filters: non-empty content, latest version per
``article_id`` (re-publications), optional universe filter (any symbol tag in
our price universe), and the per-symbol rule (``target_symbols`` = symbol tags
∩ universe when there are at most ``max_symbols`` of them, else empty -> the
article is scored at article level only).
"""

from __future__ import annotations

import random
from typing import Any, Iterable

from scoring.backends.base import Item

MIN_CHARS = 200
_UNIVERSE_TABLE = "_score_universe"


def has_view(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def universe_symbols(con: Any) -> set[str]:
    """``TICKER.EXCH`` for every pair in any price lane's state sidecar."""
    if not has_view(con, "prices_state"):
        return set()
    rows = con.execute(
        "SELECT DISTINCT upper(ticker) || '.' || upper(exchange) FROM prices_state "
        "WHERE ticker IS NOT NULL AND exchange IS NOT NULL"
    ).fetchall()
    return {r[0] for r in rows}


def install_universe(con: Any, symbols: Iterable[str]) -> None:
    """Materialise the universe as a temp table for fast list intersection."""
    con.execute(f"CREATE OR REPLACE TEMP TABLE {_UNIVERSE_TABLE}(sym VARCHAR)")
    syms = sorted(set(symbols))
    if syms:
        con.executemany(
            f"INSERT INTO {_UNIVERSE_TABLE} VALUES (?)", [(s,) for s in syms]
        )


def days_with_articles(con: Any, since: str, until: str) -> list[str]:
    """Publication days in ``[since, until]`` that have articles, newest first."""
    if not has_view(con, "news"):
        return []
    rows = con.execute(
        "SELECT DISTINCT cast(date AS VARCHAR) FROM news "
        "WHERE date BETWEEN ? AND ? ORDER BY 1 DESC",
        [since, until],
    ).fetchall()
    return [r[0] for r in rows]


def select_day(
    con: Any,
    day: str,
    *,
    use_universe: bool,
    max_symbols: int,
    min_chars: int = MIN_CHARS,
    exclude_ids: set[str] | None = None,
    sample: int | None = None,
    seed: int = 0,
) -> list[Item]:
    """Items to score for one day (after filters, exclusions and sampling)."""
    # The universe filter runs in Python: the per-day frame is a few thousand
    # rows, while a correlated EXISTS/unnest in SQL costs ~30 s per day.
    df = con.execute(
        """
        WITH latest AS (
          SELECT n.article_id, cast(n.date AS VARCHAR) AS date, n.title, n.content,
                 n.symbols, n.source, n.polarity, n.pos, n.neg, n.published_at
          FROM news n
          WHERE n.date = ? AND n.content IS NOT NULL AND length(n.content) >= ?
          QUALIFY row_number() OVER (PARTITION BY n.article_id ORDER BY n.published_at DESC) = 1
        )
        SELECT article_id, date, title, content, symbols, source, polarity, pos, neg
        FROM latest ORDER BY article_id
        """,
        [day, min_chars],
    ).df()
    uni: set[str] | None = None
    if use_universe:
        uni = {
            r[0] for r in con.execute(f"SELECT sym FROM {_UNIVERSE_TABLE}").fetchall()
        }
    items: list[Item] = []
    excl = exclude_ids or set()
    for row in df.itertuples(index=False):
        if row.article_id in excl:
            continue
        symbols = tuple(
            str(s).upper()
            for s in (list(row.symbols) if row.symbols is not None else [])
        )
        if uni is not None and not any(s in uni for s in symbols):
            continue  # article does not touch our universe
        candidates = [s for s in symbols if (uni is None or s in uni)]
        # de-dup while keeping order
        seen: set[str] = set()
        candidates = [s for s in candidates if not (s in seen or seen.add(s))]
        target = tuple(candidates) if 0 < len(candidates) <= max_symbols else ()
        items.append(
            Item(
                article_id=str(row.article_id),
                date=str(row.date),
                title=str(row.title or ""),
                content=str(row.content or ""),
                symbols=symbols,
                target_symbols=target,
                vendor_polarity=_f(row.polarity),
                vendor_pos=_f(row.pos),
                vendor_neg=_f(row.neg),
                source=str(row.source) if row.source is not None else None,
            )
        )
    if sample is not None and sample < len(items):
        rng = random.Random(f"{seed}:{day}")
        items = sorted(rng.sample(items, sample), key=lambda it: it.article_id)
    return items


def _f(value: Any) -> float | None:
    try:
        if value is None:
            return None
        v = float(value)
        return None if v != v else v
    except (TypeError, ValueError):
        return None
