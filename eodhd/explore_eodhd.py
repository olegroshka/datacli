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
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # type: ignore[import-not-found]  # noqa: E402
import schema as sch  # type: ignore[import-not-found]  # noqa: E402
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
}
STATE_ASOF = {
    "prices": "latest_data_date",
    "dividends": "latest_data_date",
    "splits": "latest_data_date",
    "fundamentals": "latest_filing_date",
    "news": "date",
}
STATE_COVERAGE = {
    "prices": "coverage_through",
    "dividends": "coverage_through",
    "splits": "coverage_through",
    "fundamentals": None,
    "news": "date",
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
    if built, a ``catalog``)."""
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
        con.execute(
            f"CREATE VIEW catalog AS SELECT * FROM read_parquet('{INDEX_PATH.as_posix()}')"
        )
    return con


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
    dcol = DATE_COL[dataset]
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


def run_sql(con: Any, query: str, limit: int) -> int:
    console = _console()
    try:
        df = con.execute(query).df()
    except Exception as exc:
        console.print(f"[red]{type(exc).__name__}: {exc}[/red]")
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
    catalog = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    EODHD_RAW_ROOT.mkdir(parents=True, exist_ok=True)
    catalog.to_parquet(INDEX_PATH, index=False)
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
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="explore", description="Ad-hoc EODHD data exploration (DuckDB)"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("describe").add_argument("ticker")
    sub.add_parser("find").add_argument("pattern")
    r = sub.add_parser("rows")
    r.add_argument("ticker")
    r.add_argument("dataset", choices=TICKER_DATASETS)
    r.add_argument("--head", type=int, default=20)
    r.add_argument("--cols", default=None, help="comma list, or '*' for all")
    sub.add_parser("coverage").add_argument("ticker")
    s = sub.add_parser("sql")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=50)
    sub.add_parser("schema")
    sub.add_parser("reindex")
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
    return 2


if __name__ == "__main__":
    sys.exit(main())
