"""Read-only tools the analyst agent may call, plus the schema it reasons over.

The only executable tool is ``run_sql`` (guarded read-only DuckDB). The schema is
handed to the model as static context -- accurate column names are what make
NL->SQL reliable, especially for small/local models.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lab import sqlguard

_EODHD = Path(__file__).resolve().parents[1] / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
import schema as sch  # type: ignore[import-not-found]  # noqa: E402


@dataclass
class ToolResult:
    ok: bool
    sql: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    error: str = ""


class Tools:
    """Guarded read-only access to the DuckDB views."""

    def __init__(self, con: Any, *, max_rows: int = 200) -> None:
        self.con = con
        self.max_rows = max_rows

    def run_sql(self, query: str) -> ToolResult:
        ok, err, normalized = sqlguard.validate(query, max_rows=self.max_rows)
        if not ok:
            return ToolResult(ok=False, error=err, sql=query)
        try:
            cur = self.con.execute(normalized)
            columns = [d[0] for d in cur.description] if cur.description else []
            rows = [tuple(r) for r in cur.fetchmany(self.max_rows)]
        except Exception as exc:  # surface the DB error back to the model
            return ToolResult(
                ok=False, error=f"{type(exc).__name__}: {exc}", sql=normalized
            )
        return ToolResult(ok=True, sql=normalized, columns=columns, rows=rows)


def schema_context() -> str:
    """A compact, accurate description of the queryable views for the prompt."""
    lines = ["Read-only DuckDB views (every view has a `lane` column):"]
    for dataset in sch.datasets():
        cols = ", ".join(sch.canonical_columns(dataset))
        lines.append(f"- {dataset}({cols})")
    lines.append(
        "- state sidecars: prices_state, dividends_state, splits_state "
        "(ticker, exchange, coverage_through, latest_data_date, status); "
        "news_state (date, status, articles) -- one row per crawled UTC day"
    )
    if "news_daily" in sch.datasets():
        lines.append(
            "- news_daily (derived, present only if built): one row per (date, ticker, "
            "exchange) with n_articles, share_of_day (= n_articles / that day's global "
            "article count -- use this, not raw counts: volume is not stationary across "
            "years), n_solo (articles with <= 3 symbol tags), n_sources, vendor "
            "polarity_mean/pos_share/neg_share, first/last_published_at"
        )
    if "news" in sch.datasets():
        lines.append(
            "- news_scores_<schema> (e.g. news_scores_event; present only after `score "
            "run`): our own per-article scores -- one row per (article_id, symbol); "
            "symbol IS NULL = article-level row; columns include event_type, summary, "
            "sentiment [-1,1], confidence, materiality 0-3, novelty, horizon, and per-"
            "symbol role/direction/relevance; `backend`/`model` say which model scored "
            "it. Prefer these over the vendor polarity when present."
        )
        lines.append(
            "- news is article-level (present only if crawled): symbols and tags are "
            "list<string> columns -- use unnest(symbols) or list_contains(symbols, "
            "'AAPL.US') for symbol-level views; polarity/neg/neu/pos are the "
            "vendor's per-article sentiment"
        )
    lines.append(
        "- catalog(dataset, lane, ticker, exchange, n_rows, first_date, last_date) "
        "-- present only if the data has been reindexed"
    )
    return "\n".join(lines)


def result_to_text(result: ToolResult, *, max_feed_rows: int = 40) -> str:
    """Render a tool result as compact text to feed back to the model."""
    if not result.ok:
        return f"ERROR: {result.error}"
    if not result.rows:
        return "(0 rows)"
    header = " | ".join(result.columns)
    body = [
        " | ".join("NULL" if v is None else str(v) for v in row)
        for row in result.rows[:max_feed_rows]
    ]
    note = f"({len(result.rows)} rows"
    note += ", truncated)" if len(result.rows) > max_feed_rows else ")"
    return "\n".join([header, *body, note])
