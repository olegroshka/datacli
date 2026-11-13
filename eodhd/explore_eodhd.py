"""Ad-hoc, entity-centric exploration of the EODHD datasets, backed by DuckDB.

Registers each dataset's per-lane parquets (and state sidecars) as unioned DuckDB
views -- each with a ``lane`` column, and each projecting the canonical schema
columns (see ``schema.py``) so evolving data still queries cleanly. Fast, targeted
queries instead of the full-scan ``qc``:

    describe <TICKER>          everything about one ticker, across datasets
    find <PATTERN>             where a ticker lives (lane / exchange / datasets)
    rows <TICKER> <dataset>    the actual rows for a ticker in a dataset
    coverage <TICKER>          do the datasets cover it equally?
    sql "<query>"              raw DuckDB over the registered views
    schema                     declared schema version + drift vs the on-disk data
    reindex                    (re)build the fast query catalog after new data

``reindex`` scans once into ``_datacli_index.parquet`` (dataset.lane.ticker ->
rows, first/last) so ``describe``/``find`` become instant; without it they scan
the views directly (still sub-second via predicate pushdown).

Catalog caveat: when the catalog exists, ``describe``/``find`` **prefer it** over
the live views, so tickers fetched *after* the last ``reindex`` are invisible to
them (``describe`` only falls back to a scan for a ticker the catalog has never
seen). Run ``reindex`` after every fetch. ``rows``/``coverage``/``sql`` always
read the live views.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # type: ignore[import-not-found]  # noqa: E402
import schema as sch  # type: ignore[import-not-found]  # noqa: E402
import _atomic
from _datadir import EODHD_RAW_ROOT  # type: ignore[import-not-found]  # noqa: E402
from eodhd_datasets import LANES  # type: ignore[import-not-found]  # noqa: E402

DATASETS = sch.datasets()
# Datasets whose rows are identified by (ticker, exchange). The ticker-centric
# verbs (describe / find / rows / coverage / reindex) only look at these; a
# day-keyed corpus like ``news`` is reachable through ``sql`` and ``schema``.
TICKER_DATASETS = tuple(
    kind
    for kind in DATASETS
    if any(
        ds.kind == kind and ds.ticker_keyed
        for lane in LANES.values()
        for ds in lane.datasets
    )
)
DATE_COL = {
    "prices": "date",
    "dividends": "ex_date",
    "splits": "ex_date",
    "fundamentals": "filing_date",
    "news": "date",
    "news_daily": "date",
    "issuer_map": "built_at",
    "news_issuer_daily": "date",
    "bars_1m": "date",
}
# Column that orders a dataset's rows newest-first in `rows` (defaults to DATE_COL).
ORDER_COL = {"bars_1m": "ts"}
# Datasets whose catalog entries come from the fetch-state sidecar instead of a
# scan (bars_1m holds ~600M rows): state column for rows / first / last date.
CATALOG_FROM_STATE = {"bars_1m": ("n_rows", "first_data_date", "latest_data_date")}
STATE_ASOF = {
    "prices": "latest_data_date",
    "dividends": "latest_data_date",
    "splits": "latest_data_date",
    "fundamentals": "latest_filing_date",
    "news": "date",
    "news_daily": None,
    "issuer_map": None,
    "news_issuer_daily": None,
    "bars_1m": "latest_data_date",
}
STATE_COVERAGE = {
    "prices": "coverage_through",
    "dividends": "coverage_through",
    "splits": "coverage_through",
    "fundamentals": None,
    "news": "date",
    "news_daily": None,
    "issuer_map": None,
    "news_issuer_daily": None,
    "bars_1m": "coverage_through",
}
DEFAULT_COLS = {
    "prices": ["date", "open", "high", "low", "close", "adjusted_close", "volume"],
    "dividends": [
        "ex_date",
        "dividend",
        "unadjusted_dividend",
        "currency",
        "period",
        "payment_date",
    ],
    "splits": ["ex_date", "split_factor", "numerator", "denominator", "split_ratio"],
    "fundamentals": ["statement", "date", "filing_date", "currency"],
    "news": ["published_at", "title", "source", "symbols", "polarity"],
    "news_daily": [
        "date",
        "n_articles",
        "share_of_day",
        "n_solo",
        "n_sources",
        "polarity_mean",
        "pos_share",
        "neg_share",
    ],
    "issuer_map": ["symbol", "issuer_id", "issuer_name", "primary_symbol", "evidence"],
    "bars_1m": ["ts", "date", "session", "open", "high", "low", "close", "volume"],
    "news_issuer_daily": [
        "date",
        "issuer_name",
        "n_articles",
        "share_of_day",
        "n_solo",
        "n_symbols",
        "polarity_mean",
        "pos_share",
        "neg_share",
    ],
}
INDEX_PATH = EODHD_RAW_ROOT / "_datacli_index.parquet"
META_PATH = EODHD_RAW_ROOT / "_datacli_meta.json"


def _console():
    return _render.make_console()


# --------------------------------------------------------------------------- #
# identity + views
# --------------------------------------------------------------------------- #
def parse_ticker(spec: str) -> tuple[str, str | None]:
    """``VAR.OL`` -> ("VAR", "OL"); ``VAR`` -> ("VAR", None)."""
    spec = spec.strip().upper()
    if "." in spec:
        ticker, exchange = spec.rsplit(".", 1)
        return ticker.strip(), exchange.strip()
    return spec, None


def _spec_for(lane: Any, kind: str) -> Any:
    return next((d for d in lane.datasets if d.kind == kind), None)


def _cols(con: Any, source_sql: str) -> list[str]:
    return [
        d[0] for d in con.execute(f"SELECT * FROM {source_sql} LIMIT 0").description
    ]


def _output_source(lane: Any, ds: Any) -> str | None:
    """``read_parquet(...)`` for a dataset's output, or ``None`` when absent.

    Partitioned outputs are read through their ``*.parquet`` glob; a partition
    directory with no files yet counts as absent.
    """
    root = lane.resolved_root()
    base = root / ds.output
    if not base.exists():
        return None
    if ds.partitioned and not any(base.glob("*.parquet")):
        return None
    pattern = Path(ds.output_glob(root)).as_posix()
    return f"read_parquet('{pattern}')"


def connect() -> Any:
    """In-memory DuckDB with a projected UNION view per dataset (+ ``*_state`` and,
    if built, a ``catalog``).

    Only datasets with data on disk get a view, so on an empty root the
    connection has no dataset views at all (``sql`` then explains which views
    exist). If ``_datacli_index.parquet`` is present it is exposed as ``catalog``
    and ``describe``/``find`` read *it* first -- it is a snapshot from the last
    ``reindex``, so run ``reindex`` after fetching new data.
    """
    import duckdb

    con = duckdb.connect()
    for kind in DATASETS:
        parts = []
        for lane in LANES.values():
            ds = _spec_for(lane, kind)
            if ds is None:
                continue
            src = _output_source(lane, ds)
            if src is None:
                continue
            cols = _cols(con, src)
            parts.append(f"SELECT {sch.projection(kind, cols, lane.name)} FROM {src}")
        if parts:
            con.execute(f"CREATE VIEW {kind} AS " + " UNION ALL BY NAME ".join(parts))
    for kind in DATASETS:
        parts = []
        for lane in LANES.values():
            ds = _spec_for(lane, kind)
            if ds is None or ds.state is None:
                continue
            path = lane.resolved_root() / ds.state
            if path.exists():
                parts.append(
                    f"SELECT *, '{lane.name}' AS lane "
                    f"FROM read_csv_auto('{path.as_posix()}', all_varchar=true)"
                )
        if parts:
            con.execute(
                f"CREATE VIEW {kind}_state AS " + " UNION ALL BY NAME ".join(parts)
            )
    if INDEX_PATH.exists():
        # A zero-column parquet (an old reindex over an empty root) would make
        # DuckDB refuse the view and take every verb down with it; skip it.
        try:
            con.execute(
                "CREATE VIEW catalog AS SELECT * FROM "
                f"read_parquet('{INDEX_PATH.as_posix()}')"
            )
        except Exception:
            pass
    register_score_views(con, EODHD_RAW_ROOT)
    register_intraday_views(con)
    # Macro views (macro / macro_country / macro_market) are layered on top when
    # that source has been fetched, so `sql` sees the same surface as the lab and
    # the MCP server. Best-effort: macro is optional and must never break eodhd.
    try:
        _repo_root = str(Path(__file__).resolve().parents[1])
        if _repo_root not in sys.path:
            sys.path.append(_repo_root)
        from macro import views as macro_views  # type: ignore[import-not-found]

        macro_views.register(con)
    except Exception:
        pass
    # Same for the FINRA views (finra_short_volume + its state sidecar).
    try:
        from finra import views as finra_views  # type: ignore[import-not-found]

        finra_views.register(con)
    except Exception:
        pass
    # ... the SEC views (Form 13F), once fetched ...
    try:
        from sec import views as sec_views  # type: ignore[import-not-found]

        sec_views.register(con)
    except Exception:
        pass
    # ... the borrow snapshots, once fetched ...
    try:
        from borrow import views as borrow_views  # type: ignore[import-not-found]

        borrow_views.register(con)
    except Exception:
        pass
    # ... the public short registers, once fetched ...
    try:
        from registers import views as registers_views  # type: ignore[import-not-found]

        registers_views.register(con)
    except Exception:
        pass
    # ... and the derived positioning views (the short ladder), once built.
    try:
        from positioning import views as positioning_views  # type: ignore[import-not-found]

        positioning_views.register(con)
    except Exception:
        pass
    return con


# --------------------------------------------------------------------------- #
# intraday lane: views + `bars` / `universe` verbs
# --------------------------------------------------------------------------- #
INTRADAY_DIR = EODHD_RAW_ROOT / "intraday"
INTRADAY_TICKERS = INTRADAY_DIR / "tickers_INTRADAY.parquet"
INTRADAY_MEMBERSHIP = INTRADAY_DIR / "universe_membership.parquet"
BAR_FREQS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
#: exchange -> timezone for the `local` column of `bars`
BAR_TZ = {
    "US": "America/New_York",
    "INDX": "America/New_York",
    "LSE": "Europe/London",
    "FOREX": "UTC",
}


def register_intraday_views(con: Any, root: Path | None = None) -> list[str]:
    """``bars_1m_adj`` (split-adjusted bars) and the universe views, when present.

    ``bars_1m_adj`` divides prices and multiplies volume by the product of the
    ``split_ratio`` of every split with ``ex_date`` after the bar's trading
    date (splits from every lane's ``splits`` view); ``split_adj`` carries the
    factor. Unadjusted bars stay in ``bars_1m``.
    """
    base = (root / "intraday") if root is not None else INTRADAY_DIR
    made: list[str] = []
    if _has_view(con, "bars_1m"):
        if _has_view(con, "splits"):
            segments = """
                WITH s AS (
                  SELECT upper(ticker) AS t, upper(exchange) AS e, CAST(ex_date AS VARCHAR) AS d,
                         any_value(split_ratio) AS f
                  FROM splits WHERE split_ratio > 0 AND split_ratio <> 1 GROUP BY 1, 2, 3),
                seg AS (
                  SELECT t, e, d AS until,
                         coalesce(lag(d) OVER (PARTITION BY t, e ORDER BY d), '') AS since,
                         exp(sum(ln(f)) OVER (PARTITION BY t, e ORDER BY d DESC
                                              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)) AS cum
                  FROM s)
                SELECT b.* REPLACE (b.open / coalesce(seg.cum, 1) AS open, b.high / coalesce(seg.cum, 1) AS high,
                                    b.low / coalesce(seg.cum, 1) AS low, b.close / coalesce(seg.cum, 1) AS close,
                                    b.volume * coalesce(seg.cum, 1) AS volume),
                       coalesce(seg.cum, 1.0) AS split_adj
                FROM bars_1m b LEFT JOIN seg
                  ON upper(b.ticker) = seg.t AND upper(b.exchange) = seg.e
                 AND b.date >= seg.since AND b.date < seg.until
            """
        else:
            segments = "SELECT *, 1.0 AS split_adj FROM bars_1m"
        con.execute(f"CREATE OR REPLACE VIEW bars_1m_adj AS {segments}")
        made.append("bars_1m_adj")
    for view, path in (
        ("intraday_universe", base / INTRADAY_TICKERS.name),
        ("intraday_membership", base / INTRADAY_MEMBERSHIP.name),
    ):
        if path.exists():
            con.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path.as_posix()}')"
            )
            made.append(view)
    return made


def bars(
    con: Any,
    spec: str,
    on: str | None,
    freq: str,
    session: str,
    adjusted: bool,
    head: int,
) -> int:
    """One trading day of a pair's bars, resampled from 1m."""
    if _no_data_hint(con):
        return 0
    console = _console()
    view = "bars_1m_adj" if adjusted else "bars_1m"
    if not _has_view(con, view):
        console.print(
            f"[yellow]no 1m bars yet[/yellow] -- refresh intraday --datasets bars_1m --run"
        )
        return 0
    if freq not in BAR_FREQS:
        console.print(
            f"[red]unknown --freq {freq}[/red]. Choose: {', '.join(BAR_FREQS)}"
        )
        return 2
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)
    if on is None:
        row = con.execute(
            f"SELECT max(date) FROM {view} WHERE {cond}", params
        ).fetchone()
        on = row[0] if row else None
        if on is None:
            console.print(f"[yellow]{spec} not present in bars_1m[/yellow]")
            return 0
    sess = "" if session == "all" else f" AND session = '{session}'"
    minutes = BAR_FREQS[freq]
    df = con.execute(
        f"""
        SELECT time_bucket(INTERVAL '{minutes} minutes', ts) AS ts,
               any_value(exchange) AS exchange, any_value(session) AS session,
               arg_min(open, ts) AS open, max(high) AS high, min(low) AS low,
               arg_max(close, ts) AS close, sum(volume) AS volume, count(*) AS n_1m
        FROM {view} WHERE {cond} AND date = ?{sess}
        GROUP BY 1 ORDER BY 1
        """,
        [*params, on],
    ).df()
    if df.empty:
        console.print(f"[yellow]{spec}: no {session} bars on {on}[/yellow]")
        return 0
    tz = BAR_TZ.get(str(df["exchange"].iloc[0]), "UTC")
    df.insert(
        1,
        "local",
        pd.to_datetime(df["ts"], utc=True).dt.tz_convert(tz).dt.strftime("%H:%M"),
    )
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.strftime("%Y-%m-%d %H:%M")
    shown = df.drop(columns=["exchange"]).head(head)
    title = (
        f"{spec} {freq} bars on {on} ({session}{', split-adjusted' if adjusted else ''}) "
        f"-- {len(df)} bar(s){f', first {head}' if len(df) > head else ''}"
    )
    console.print(_render.df_table(shown, title=title))
    return 0


def universe(
    con: Any, lane: str, as_of: str | None, tier: str | None, changes: bool
) -> int:
    """Members of the intraday universe at a date, or the last quarter's changes."""
    console = _console()
    if lane != "intraday":
        console.print(f"[red]no universe verb for lane '{lane}'[/red] (only: intraday)")
        return 2
    if not _has_view(con, "intraday_membership"):
        console.print(
            "[yellow]no intraday universe yet[/yellow] -- python eodhd/build_intraday_universe.py"
        )
        return 0
    quarters = [
        q
        for (q,) in con.execute(
            "SELECT DISTINCT quarter_start FROM intraday_membership ORDER BY 1"
        ).fetchall()
    ]
    asof = as_of or quarters[-1]
    eligible = [q for q in quarters if q <= asof]
    if not eligible:
        console.print(
            f"[yellow]no membership on or before {asof}[/yellow] (first quarter {quarters[0]})"
        )
        return 0
    q = eligible[-1]
    tier_cond = " AND tier = ?" if tier else ""
    tier_params = [tier] if tier else []
    if changes:
        prev = eligible[-2] if len(eligible) > 1 else None
        if prev is None:
            console.print(
                f"[yellow]{q} is the first quarter; nothing to compare[/yellow]"
            )
            return 0
        df = con.execute(
            f"""
            WITH cur AS (SELECT ticker, exchange, tier, adv_rank FROM intraday_membership WHERE quarter_start = ?{tier_cond}),
                 old AS (SELECT ticker, exchange, tier, adv_rank FROM intraday_membership WHERE quarter_start = ?{tier_cond})
            SELECT coalesce(cur.ticker, old.ticker) AS ticker, coalesce(cur.exchange, old.exchange) AS exchange,
                   old.tier AS was, cur.tier AS now, cur.adv_rank AS rank_now, old.adv_rank AS rank_was
            FROM cur FULL OUTER JOIN old ON cur.ticker = old.ticker AND cur.exchange = old.exchange
            WHERE cur.tier IS DISTINCT FROM old.tier
            ORDER BY now NULLS LAST, rank_now NULLS LAST, ticker
            """,
            [q, *tier_params, prev, *tier_params],
        ).df()
        console.print(
            _render.df_table(
                df, title=f"intraday universe changes {prev} -> {q} ({len(df)})"
            )
        )
        return 0
    df = con.execute(
        f"""
        SELECT ticker, exchange, tier, "group", adv_rank, adv_musd, isa_tradable
        FROM intraday_membership WHERE quarter_start = ?{tier_cond}
        ORDER BY tier, adv_rank NULLS LAST, "group", ticker
        """,
        [q, *tier_params],
    ).df()
    counts = ", ".join(
        f"{k}={v}" for k, v in df["tier"].value_counts().sort_index().items()
    )
    console.print(
        _render.df_table(
            df, title=f"intraday universe for quarter {q} (as of {asof}): {counts}"
        )
    )
    return 0


def register_score_views(con: Any, root: Path) -> list[str]:
    """Expose the news score / embedding sidecars written by ``score run``.

    ``news/scores/<schema>@<v>/<backend>/<day>.parquet`` -> one view per schema
    version, ``news_scores_<schema>_v<v>`` (all backends unioned; the ``backend``
    column tells them apart), plus ``news_scores_<schema>`` for the latest
    version. ``news/embeddings/<model>/<day>.parquet`` -> ``news_embeddings``.
    Returns the view names created. Best-effort: never breaks the connection.
    """
    created: list[str] = []
    scores = root / "news" / "scores"
    latest: dict[str, tuple[int, str]] = {}
    if scores.is_dir():
        for sdir in sorted(scores.iterdir()):
            if not sdir.is_dir() or "@" not in sdir.name:
                continue
            name, _, ver = sdir.name.partition("@")
            if not any(sdir.glob("*/*.parquet")):
                continue
            view = f"news_scores_{_safe(name)}_v{_safe(ver)}"
            pattern = (sdir / "*" / "*.parquet").as_posix()
            try:
                con.execute(
                    f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM "
                    f"read_parquet('{pattern}', union_by_name=true)"
                )
            except Exception:
                continue
            created.append(view)
            try:
                v = int(ver)
            except ValueError:
                v = 0
            if name not in latest or v > latest[name][0]:
                latest[name] = (v, view)
        for name, (_, view) in latest.items():
            alias = f"news_scores_{_safe(name)}"
            try:
                con.execute(f"CREATE OR REPLACE VIEW {alias} AS SELECT * FROM {view}")
                created.append(alias)
            except Exception:
                pass
    emb = root / "news" / "embeddings"
    if emb.is_dir() and any(emb.glob("*/*.parquet")):
        pattern = (emb / "*" / "*.parquet").as_posix()
        try:
            con.execute(
                "CREATE OR REPLACE VIEW news_embeddings AS SELECT * FROM "
                f"read_parquet('{pattern}', union_by_name=true)"
            )
            created.append("news_embeddings")
        except Exception:
            pass
    return created


def _safe(text: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in text)


def _dataset_views(con: Any) -> list[str]:
    """Names of the dataset views that exist on this connection."""
    return [k for k in DATASETS if _has_view(con, k)]


def _no_data_hint(con: Any) -> bool:
    """If no dataset view exists at all, say so (with the first-fill pointer)
    and return ``True`` so the caller can stop early."""
    if _dataset_views(con):
        return False
    _console().print(
        f"[yellow]no data under {EODHD_RAW_ROOT}[/yellow] -- first fill: "
        "config set data-root <path>, then refresh <lane> --run (us_common/uk_eu: "
        "add --with-fundamentals); see eodhd/README.md 'First fill'"
    )
    return True


def _has_view(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def _where(
    ticker: str, exchange: str | None, prefix: str = ""
) -> tuple[str, list[Any]]:
    t = f"upper({prefix}ticker) = ?"
    if exchange:
        return f"{t} AND upper({prefix}exchange) = ?", [ticker, exchange]
    return t, [ticker]


def _raw_columns(con: Any, kind: str) -> list[str]:
    cols: list[str] = []
    for lane in LANES.values():
        ds = _spec_for(lane, kind)
        if ds is None:
            continue
        src = _output_source(lane, ds)
        if src is not None:
            for c in _cols(con, src):
                if c not in cols:
                    cols.append(c)
    return cols


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
def _dataset_summary(
    con: Any, kind: str, cond: str, params: list[Any]
) -> tuple[int, str | None, str | None, str | None]:
    """(rows, first, last, lane) for a ticker in a dataset -- from the catalog if
    built, else scanning the view."""
    if _has_view(con, "catalog"):
        row = con.execute(
            "SELECT sum(n_rows), min(first_date), max(last_date), any_value(lane) "
            f"FROM catalog WHERE dataset = ? AND {cond}",
            [kind, *params],
        ).fetchone()
        if row and row[0]:
            return int(row[0]), row[1], row[2], row[3]
        # fall through to scan if catalog has no entry (data may be newer)
    if not _has_view(con, kind):
        return 0, None, None, None
    dcol = DATE_COL[kind]
    row = con.execute(
        f"SELECT count(*), cast(min({dcol}) AS VARCHAR), cast(max({dcol}) AS VARCHAR), any_value(lane) "
        f"FROM {kind} WHERE {cond}",
        params,
    ).fetchone()
    n, dmin, dmax, lane = row or (0, None, None, None)
    return int(n or 0), dmin, dmax, lane


def describe(con: Any, spec: str) -> int:
    from rich.text import Text

    if _no_data_hint(con):
        return 0
    console = _console()
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)

    table = _render.boxed_table(title=str(spec))
    for col in (
        "dataset",
        "present",
        "rows",
        "first",
        "last(data)",
        "coverage",
        "state",
    ):
        table.add_column(col, justify="right" if col == "rows" else "left")

    lanes_seen: set[str] = set()
    cov_values: dict[str, str] = {}
    for kind in TICKER_DATASETS:
        n, dmin, dmax, lane = _dataset_summary(con, kind, cond, params)
        if lane:
            lanes_seen.add(lane)
        cov, _asof, status = _state_lookup(con, kind, cond, params)
        if kind != "fundamentals" and cov:
            cov_values[kind] = cov
        table.add_row(
            Text(kind, style=_render.KIND_HUE.get(kind, "")),
            _render.yesno_cell(bool(n)),
            f"{n:,}" if n else "-",
            dmin or "-",
            dmax or "-",
            cov or "-",
            _render.status_token(status),
        )

    where = ", ".join(sorted(lanes_seen)) if lanes_seen else "[red]not found[/red]"
    console.print(f"[bold]{spec}[/bold]  ->  lane(s): {where}")
    console.print(table)
    if cov_values and len(set(cov_values.values())) == 1:
        console.print(
            "coverage: prices/dividends/splits all queried through "
            f"[green]{next(iter(cov_values.values()))}[/green]  ->  uniform"
        )
    elif cov_values:
        detail = ", ".join(f"{k}={v}" for k, v in cov_values.items())
        console.print(f"coverage: [yellow]uneven[/yellow] ({detail})")
    return 0


def _state_lookup(
    con: Any, kind: str, cond: str, params: list[Any]
) -> tuple[str | None, str | None, str | None]:
    sname = f"{kind}_state"
    if not _has_view(con, sname):
        return None, None, None
    cov_col = STATE_COVERAGE[kind]
    asof_col = STATE_ASOF[kind]
    cov_expr = f"any_value({cov_col})" if cov_col else "NULL"
    row = con.execute(
        f"SELECT {cov_expr}, any_value({asof_col}), any_value(status) FROM {sname} WHERE {cond}",
        params,
    ).fetchone()
    return (row[0], row[1], row[2]) if row else (None, None, None)


def find(con: Any, pattern: str) -> int:
    if _no_data_hint(con):
        return 0
    console = _console()
    like = f"%{pattern.strip().upper()}%"
    rows: dict[tuple[str, str, str], set[str]] = {}
    if _has_view(con, "catalog"):
        for dataset, ticker, exchange, lane in con.execute(
            "SELECT DISTINCT dataset, upper(ticker), upper(exchange), lane "
            "FROM catalog WHERE upper(ticker) LIKE ?",
            [like],
        ).fetchall():
            rows.setdefault((ticker, exchange, lane), set()).add(dataset)
    else:
        for kind in TICKER_DATASETS:
            view = (
                f"{kind}_state"
                if _has_view(con, f"{kind}_state")
                else (kind if _has_view(con, kind) else None)
            )
            if view is None:
                continue
            for ticker, exchange, lane in con.execute(
                f"SELECT DISTINCT upper(ticker), upper(exchange), lane FROM {view} WHERE upper(ticker) LIKE ?",
                [like],
            ).fetchall():
                rows.setdefault((ticker, exchange, lane), set()).add(kind)
    if not rows:
        console.print(f"[yellow]no ticker matches[/yellow] '{pattern}'")
        return 0
    from rich.text import Text

    table = _render.boxed_table(title=f"matches for '{pattern}' ({len(rows)})")
    for col in ("ticker", "exchange", "lane", "datasets"):
        table.add_column(col)
    for (ticker, exchange, lane), kinds in sorted(rows.items()):
        datasets = Text()
        for i, kind in enumerate(sorted(kinds)):
            if i:
                datasets.append(" ")
            datasets.append(kind, style=_render.KIND_HUE.get(kind, ""))
        table.add_row(Text(ticker, style="bold"), exchange, lane, datasets)
    console.print(table)
    return 0


def rows(con: Any, spec: str, dataset: str, head: int, cols: str | None) -> int:
    if _no_data_hint(con):
        return 0
    console = _console()
    if dataset not in TICKER_DATASETS:
        console.print(
            f"[red]unknown dataset '{dataset}'[/red]. "
            f"Choose: {', '.join(TICKER_DATASETS)}"
        )
        return 2
    if not _has_view(con, dataset):
        console.print(f"[yellow]no {dataset} data[/yellow]")
        return 0
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)
    dcol = ORDER_COL.get(dataset, DATE_COL[dataset])
    if cols == "*":
        sel = "*"
    elif cols:
        sel = ", ".join(c.strip() for c in cols.split(","))
    else:
        sel = ", ".join(DEFAULT_COLS[dataset])
    df = con.execute(
        f"SELECT {sel} FROM {dataset} WHERE {cond} ORDER BY {dcol} DESC LIMIT ?",
        [*params, head],
    ).df()
    if df.empty:
        console.print(f"[yellow]{spec} not present in {dataset}[/yellow]")
        return 0
    hue = _render.KIND_HUE.get(dataset, "cyan")
    console.print(
        _render.df_table(
            df, title=f"{spec} in [{hue}]{dataset}[/{hue}] (latest {len(df)})"
        )
    )
    return 0


def coverage(con: Any, spec: str) -> int:
    from rich.text import Text

    if _no_data_hint(con):
        return 0
    console = _console()
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)
    table = _render.boxed_table(title=f"coverage: {spec}")
    for col in ("dataset", "coverage_through", "last_data", "status"):
        table.add_column(col)
    cov_values: dict[str, str] = {}
    for kind in TICKER_DATASETS:
        cov, asof, status = _state_lookup(con, kind, cond, params)
        if cov is None and asof is None and status is None:
            continue
        if kind != "fundamentals" and cov:
            cov_values[kind] = cov
        table.add_row(
            Text(kind, style=_render.KIND_HUE.get(kind, "")),
            cov or "-",
            asof or "-",
            _render.status_token(status),
        )
    console.print(table)
    if not cov_values:
        console.print("[yellow]no state coverage found[/yellow]")
    elif len(set(cov_values.values())) == 1:
        console.print(
            f"-> uniform: all queried through [green]{next(iter(cov_values.values()))}[/green]"
        )
    else:
        console.print("-> [yellow]uneven coverage[/yellow] across datasets")
    return 0


def _existing_views(con: Any) -> list[str]:
    """Names of the tables/views registered on this connection (sorted)."""
    try:
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() ORDER BY table_name"
        ).fetchall()
    except Exception:
        return []
    return [str(r[0]) for r in rows]


def _is_missing_relation(exc: BaseException) -> bool:
    """True when DuckDB failed because a referenced table/view is not registered.

    Matches DuckDB's ``CatalogException`` (by class name, so this stays free of a
    hard duckdb import) and, defensively, any error text saying something ``does
    not exist``.
    """
    if type(exc).__name__ == "CatalogException":
        return True
    return "does not exist" in str(exc)


def run_sql(con: Any, query: str, limit: int) -> int:
    from rich.markup import escape

    console = _console()
    try:
        df = con.execute(query).df()
    except Exception as exc:
        if _is_missing_relation(exc):
            # Typical on a fresh root: the dataset views only exist for data on
            # disk, so `SELECT ... FROM prices` fails before anything is fetched.
            text = str(exc).strip()
            first_line = text.splitlines()[0] if text else "relation does not exist"
            console.print(f"[red]{escape(first_line)}[/red]")
            views = _existing_views(con)
            if views:
                console.print(
                    "views on this connection: "
                    + ", ".join(f"[cyan]{escape(v)}[/cyan]" for v in views)
                )
            else:
                console.print("[dim]no dataset views on this connection[/dim]")
                console.print(
                    "[dim]no data yet? see 'First fill' in eodhd/README.md "
                    "(config set data-root, then refresh <lane> "
                    "--with-fundamentals --run)[/dim]"
                )
            return 1
        console.print(f"[red]{type(exc).__name__}: {escape(str(exc))}[/red]")
        return 1
    caption = None
    if len(df) > limit:
        caption = f"showing first {limit} of {len(df):,} rows"
        df = df.head(limit)
    if df.empty:
        console.print("[dim](no rows)[/dim]")
    else:
        console.print(_render.df_table(df, caption=caption))
    return 0


# --------------------------------------------------------------------------- #
# schema + reindex
# --------------------------------------------------------------------------- #
def _load_meta() -> dict[str, Any]:
    if META_PATH.exists():
        try:
            return json.loads(META_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def cmd_schema(con: Any) -> int:
    from rich.text import Text

    console = _console()
    meta = _load_meta()
    if meta:
        caption = (
            f"on-disk index: schema v{meta.get('schema_version', '?')}, "
            f"indexed {meta.get('indexed_at', '?')}"
        )
    else:
        caption = "no index yet -- run `reindex`"

    table = _render.boxed_table(
        title=f"schema  (declared SCHEMA_VERSION {sch.SCHEMA_VERSION})",
        caption=caption,
    )
    table.add_column("dataset")
    table.add_column("canonical", justify="right")
    table.add_column("missing")
    table.add_column("extra", justify="right")
    for kind in DATASETS:
        raw = _raw_columns(con, kind)
        if not raw:
            table.add_row(
                Text(kind, style=_render.KIND_HUE.get(kind, "")),
                Text("no data", style="dim"),
                "",
                "",
            )
            continue
        d = sch.diff(kind, raw)
        canon = sch.canonical_columns(kind)
        complete = not d["missing"]
        table.add_row(
            Text(kind, style=_render.KIND_HUE.get(kind, "")),
            Text(
                f"{len(d['present'])}/{len(canon)}",
                style="green" if complete else "yellow",
            ),
            (
                Text("-", style="dim")
                if complete
                else Text(", ".join(d["missing"]), style="yellow")
            ),
            Text(f"+{len(d['extra'])}" if d["extra"] else "-", style="dim"),
        )
    console.print(table)
    return 0


def cmd_reindex(con: Any) -> int:
    console = _console()
    frames = []
    for kind in TICKER_DATASETS:
        if kind in CATALOG_FROM_STATE:
            if _has_view(con, f"{kind}_state"):
                n_col, first_col, last_col = CATALOG_FROM_STATE[kind]
                frames.append(
                    con.execute(
                        f"SELECT '{kind}' AS dataset, lane, upper(ticker) AS ticker, "
                        f"upper(exchange) AS exchange, "
                        f"CAST(nullif({n_col}, '') AS BIGINT) AS n_rows, "
                        f"nullif({first_col}, '') AS first_date, "
                        f"nullif({last_col}, '') AS last_date "
                        f"FROM {kind}_state WHERE coalesce(nullif({n_col}, ''), '0') <> '0'"
                    ).df()
                )
            continue
        if not _has_view(con, kind):
            continue
        dcol = DATE_COL[kind]
        frames.append(
            con.execute(
                f"SELECT '{kind}' AS dataset, lane, upper(ticker) AS ticker, upper(exchange) AS exchange, "
                f"count(*) AS n_rows, cast(min({dcol}) AS VARCHAR) AS first_date, "
                f"cast(max({dcol}) AS VARCHAR) AS last_date "
                f"FROM {kind} GROUP BY lane, upper(ticker), upper(exchange)"
            ).df()
        )
    if not frames:
        # Nothing to index. Writing a zero-column parquet here would make
        # DuckDB refuse the `catalog` view later, so leave the root untouched.
        console.print(
            "[yellow]nothing to index yet[/yellow] -- no ticker-keyed data under "
            f"{EODHD_RAW_ROOT}. First fill: config set data-root <path>, then "
            "refresh <lane> --run (us_common/uk_eu: add --with-fundamentals); "
            "see eodhd/README.md 'First fill'."
        )
        return 0
    catalog = pd.concat(frames, ignore_index=True)
    EODHD_RAW_ROOT.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(catalog, INDEX_PATH, index=False)
    meta = {
        "schema_version": sch.SCHEMA_VERSION,
        "indexed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data_root": str(EODHD_RAW_ROOT),
        "n_entries": int(len(catalog)),
        "datasets": (
            sorted(catalog["dataset"].unique().tolist()) if not catalog.empty else []
        ),
    }
    META_PATH.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    console.print(
        f"reindexed: [bold]{len(catalog):,}[/bold] (dataset.lane.ticker) entries "
        f"-> {INDEX_PATH.name} (schema v{sch.SCHEMA_VERSION})"
    )
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
#: One-liner per verb; doubles as the subcommand ``help`` and ``description``.
VERB_HELP: dict[str, str] = {
    "describe": "Everything about one ticker, across datasets",
    "find": "Locate a ticker (lane / exchange / datasets)",
    "rows": "Show the actual rows for a ticker in a dataset (ticker-keyed "
    "datasets only; query `news` via sql)",
    "coverage": "Do the datasets cover a ticker equally?",
    "sql": "Raw DuckDB query over the dataset views (unguarded)",
    "schema": "Declared schema version + drift vs on-disk data",
    "reindex": "(Re)build the fast query catalog after new data — describe/find "
    "read the catalog, so run this after every fetch",
    "bars": "One trading day of a pair's 1m bars, resampled (intraday lane)",
    "universe": "Members of a lane's universe at a date, or a quarter's changes (intraday)",
}


def _sub_prog(prog: str, verb: str) -> str:
    """``usage:`` program name for a verb's sub-parser.

    ``eodhd/cli.py`` may hand us either the bare launcher (``eodhd``) or the
    launcher plus verb (``eodhd rows``) in ``DATACLI_PROG``; both should render
    as ``usage: eodhd rows ...`` rather than ``eodhd rows rows``.
    """
    return prog if prog.split()[-1:] == [verb] else f"{prog} {verb}"


def build_parser() -> argparse.ArgumentParser:
    prog = os.environ.get("DATACLI_PROG") or "explore"
    p = argparse.ArgumentParser(
        prog=prog,
        description="Ad-hoc EODHD data exploration (DuckDB). Only datasets with "
        "data on disk are queryable; describe/find read the reindex catalog "
        "when it exists.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add(verb: str) -> argparse.ArgumentParser:
        return sub.add_parser(
            verb,
            prog=_sub_prog(prog, verb),
            help=VERB_HELP[verb],
            description=VERB_HELP[verb],
        )

    add("describe").add_argument("ticker", help="TICKER or TICKER.EXCHANGE")
    add("find").add_argument("pattern", help="case-insensitive ticker substring")
    r = add("rows")
    r.add_argument("ticker", help="TICKER or TICKER.EXCHANGE")
    r.add_argument("dataset", choices=TICKER_DATASETS)
    r.add_argument("--head", type=int, default=20, help="latest N rows, default 20")
    r.add_argument(
        "--cols",
        default=None,
        help="columns to show: comma list, or '*' for all (default: the "
        "dataset's key columns)",
    )
    add("coverage").add_argument("ticker", help="TICKER or TICKER.EXCHANGE")
    s = add("sql")
    s.add_argument("query", help="a DuckDB SQL statement over the dataset views")
    s.add_argument(
        "--limit",
        type=int,
        default=50,
        help="max rows to print (the query itself is not limited), default 50",
    )
    add("schema")
    add("reindex")
    b = add("bars")
    b.add_argument("ticker", help="TICKER or TICKER.EXCHANGE")
    b.add_argument(
        "--on", default=None, help="trading date YYYY-MM-DD (default: the latest)"
    )
    b.add_argument(
        "--freq", default="15m", choices=tuple(BAR_FREQS), help="bar size, default 15m"
    )
    b.add_argument(
        "--session",
        default="rth",
        choices=("rth", "pre", "post", "24h", "all"),
        help="default rth",
    )
    b.add_argument(
        "--adjusted", action="store_true", help="split-adjusted prices (bars_1m_adj)"
    )
    b.add_argument(
        "--head", type=int, default=100, help="max bars to print, default 100"
    )
    u = add("universe")
    u.add_argument("lane", nargs="?", default="intraday", choices=("intraday",))
    u.add_argument(
        "--as-of", default=None, help="date YYYY-MM-DD (default: the latest quarter)"
    )
    u.add_argument("--tier", default=None, help="T1, T2, context, index, fx or uk")
    u.add_argument(
        "--changes", action="store_true", help="joiners/leavers vs the previous quarter"
    )
    return p


def main(argv: list[str] | None = None, con: Any = None) -> int:
    args = build_parser().parse_args(argv)
    if con is None:
        try:
            import duckdb  # noqa: F401
        except Exception:
            print("duckdb is required: uv sync.", file=sys.stderr)
            return 1
        con = connect()
    if args.command == "describe":
        return describe(con, args.ticker)
    if args.command == "find":
        return find(con, args.pattern)
    if args.command == "rows":
        return rows(con, args.ticker, args.dataset, args.head, args.cols)
    if args.command == "coverage":
        return coverage(con, args.ticker)
    if args.command == "sql":
        return run_sql(con, args.query, args.limit)
    if args.command == "schema":
        return cmd_schema(con)
    if args.command == "reindex":
        return cmd_reindex(con)
    if args.command == "bars":
        return bars(
            con, args.ticker, args.on, args.freq, args.session, args.adjusted, args.head
        )
    if args.command == "universe":
        return universe(con, args.lane, args.as_of, args.tier, args.changes)
    return 2


if __name__ == "__main__":
    sys.exit(main())
