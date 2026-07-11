"""Ad-hoc, entity-centric exploration of the EODHD datasets, backed by DuckDB.

Registers each dataset's per-lane parquets (and state sidecars) as unioned DuckDB
views -- each with a ``lane`` column -- and exposes fast, targeted queries instead
of the full-scan ``qc``:

    describe <TICKER>          everything about one ticker, across datasets
    find <PATTERN>             where a ticker lives (lane / exchange / datasets)
    rows <TICKER> <dataset>    the actual rows for a ticker in a dataset
    coverage <TICKER>          do the datasets cover it equally?
    sql "<query>"              raw DuckDB over the registered views

Views (each a UNION across the lanes that have the dataset): ``prices``,
``dividends``, ``splits``, ``fundamentals``, plus ``*_state`` for the sidecars.
DuckDB reads parquet lazily with predicate pushdown, so single-ticker queries are
sub-second even on the 12M-row tables.

Usage:
    python eodhd/explore_eodhd.py describe VAR.OL
    python eodhd/explore_eodhd.py rows VAR.OL dividends
    python eodhd/explore_eodhd.py sql "select lane, count(*) from dividends group by 1"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eodhd_datasets import LANES  # type: ignore[import-not-found]  # noqa: E402

DATASETS = ("prices", "dividends", "splits", "fundamentals")
# date column in each dataset's parquet
DATE_COL = {
    "prices": "date",
    "dividends": "ex_date",
    "splits": "ex_date",
    "fundamentals": "filing_date",
}
# per-kind state columns (fundamentals has no query "coverage")
STATE_ASOF = {
    "prices": "latest_data_date",
    "dividends": "latest_data_date",
    "splits": "latest_data_date",
    "fundamentals": "latest_filing_date",
}
STATE_COVERAGE = {
    "prices": "coverage_through",
    "dividends": "coverage_through",
    "splits": "coverage_through",
    "fundamentals": None,
}


def _console():
    from rich.console import Console

    return Console()


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


def connect() -> Any:
    """In-memory DuckDB with a UNION view per dataset (+ ``*_state``) from the registry."""
    import duckdb

    con = duckdb.connect()
    for kind in DATASETS:
        parts = []
        for lane in LANES.values():
            ds = _spec_for(lane, kind)
            if ds is None:
                continue
            path = lane.resolved_root() / ds.output
            if path.exists():
                parts.append(
                    f"SELECT *, '{lane.name}' AS lane "
                    f"FROM read_parquet('{path.as_posix()}')"
                )
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
    return con


def _has_view(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def _where(ticker: str, exchange: str | None) -> tuple[str, list[Any]]:
    if exchange:
        return "upper(ticker) = ? AND upper(exchange) = ?", [ticker, exchange]
    return "upper(ticker) = ?", [ticker]


# --------------------------------------------------------------------------- #
# verbs
# --------------------------------------------------------------------------- #
def describe(con: Any, spec: str) -> int:
    from rich.table import Table

    console = _console()
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)

    table = Table(title=f"{spec}", title_style="bold")
    for col in (
        "dataset",
        "present",
        "rows",
        "first",
        "last(data)",
        "coverage",
        "state",
    ):
        table.add_column(col)

    lanes_seen: set[str] = set()
    cov_values: dict[str, str] = {}
    for kind in DATASETS:
        if not _has_view(con, kind):
            continue
        dcol = DATE_COL[kind]
        row = con.execute(
            f"SELECT count(*), cast(min({dcol}) AS VARCHAR), cast(max({dcol}) AS VARCHAR), "
            f"any_value(lane) FROM {kind} WHERE {cond}",
            params,
        ).fetchone()
        n, dmin, dmax, lane = row or (0, None, None, None)
        if lane:
            lanes_seen.add(lane)
        cov, asof, status = _state_lookup(con, kind, cond, params)
        if kind != "fundamentals" and cov:
            cov_values[kind] = cov
        table.add_row(
            kind,
            "yes" if n else "[dim]no[/dim]",
            f"{n:,}" if n else "-",
            dmin or "-",
            dmax or "-",
            cov or "-",
            status or "-",
        )

    where = ", ".join(sorted(lanes_seen)) if lanes_seen else "[red]not found[/red]"
    console.print(f"[bold]{spec}[/bold]  ->  lane(s): {where}")
    console.print(table)
    if len(set(cov_values.values())) == 1 and cov_values:
        console.print(
            f"coverage: prices/dividends/splits all queried through "
            f"[green]{next(iter(cov_values.values()))}[/green]  ->  uniform ✓"
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
        f"SELECT {cov_expr}, any_value({asof_col}), any_value(status) "
        f"FROM {sname} WHERE {cond}",
        params,
    ).fetchone()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def find(con: Any, pattern: str) -> int:
    from rich.table import Table

    console = _console()
    like = f"%{pattern.strip().upper()}%"
    rows: dict[tuple[str, str, str], set[str]] = {}
    for kind in DATASETS:
        sname = f"{kind}_state"
        view = (
            sname if _has_view(con, sname) else (kind if _has_view(con, kind) else None)
        )
        if view is None:
            continue
        for ticker, exchange, lane in con.execute(
            f"SELECT DISTINCT upper(ticker), upper(exchange), lane "
            f"FROM {view} WHERE upper(ticker) LIKE ?",
            [like],
        ).fetchall():
            rows.setdefault((ticker, exchange, lane), set()).add(kind)
    if not rows:
        console.print(f"[yellow]no ticker matches[/yellow] '{pattern}'")
        return 0
    table = Table(title=f"matches for '{pattern}' ({len(rows)})", title_style="bold")
    for col in ("ticker", "exchange", "lane", "datasets"):
        table.add_column(col)
    for (ticker, exchange, lane), kinds in sorted(rows.items()):
        table.add_row(ticker, exchange, lane, " ".join(sorted(kinds)))
    console.print(table)
    return 0


def rows(con: Any, spec: str, dataset: str, head: int) -> int:
    console = _console()
    if dataset not in DATASETS:
        console.print(
            f"[red]unknown dataset '{dataset}'[/red]. Choose: {', '.join(DATASETS)}"
        )
        return 2
    if not _has_view(con, dataset):
        console.print(f"[yellow]no {dataset} data[/yellow]")
        return 0
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)
    dcol = DATE_COL[dataset]
    df = con.execute(
        f"SELECT * FROM {dataset} WHERE {cond} ORDER BY {dcol} DESC LIMIT ?",
        [*params, head],
    ).df()
    if df.empty:
        console.print(f"[yellow]{spec} not present in {dataset}[/yellow]")
        return 0
    console.print(f"[bold]{spec}[/bold] in [cyan]{dataset}[/cyan] (latest {len(df)}):")
    console.print(df.to_string(index=False))
    return 0


def coverage(con: Any, spec: str) -> int:
    from rich.table import Table

    console = _console()
    ticker, exchange = parse_ticker(spec)
    cond, params = _where(ticker, exchange)
    table = Table(title=f"coverage: {spec}", title_style="bold")
    for col in ("dataset", "coverage_through", "last_data", "status"):
        table.add_column(col)
    cov_values: dict[str, str] = {}
    for kind in DATASETS:
        cov, asof, status = _state_lookup(con, kind, cond, params)
        if cov is None and asof is None and status is None:
            continue
        if kind != "fundamentals" and cov:
            cov_values[kind] = cov
        table.add_row(kind, cov or "-", asof or "-", status or "-")
    console.print(table)
    if not cov_values:
        console.print("[yellow]no state coverage found[/yellow]")
    elif len(set(cov_values.values())) == 1:
        console.print(
            f"-> uniform: all queried through [green]{next(iter(cov_values.values()))}[/green] ✓"
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
    if len(df) > limit:
        console.print(f"[dim](showing first {limit} of {len(df)} rows)[/dim]")
        df = df.head(limit)
    console.print(df.to_string(index=False) if not df.empty else "[dim](no rows)[/dim]")
    return 0


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="explore", description="Ad-hoc EODHD data exploration (DuckDB)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("describe", help="everything about a ticker across datasets")
    d.add_argument("ticker")

    f = sub.add_parser("find", help="locate a ticker (lane/exchange/datasets)")
    f.add_argument("pattern")

    r = sub.add_parser("rows", help="the actual rows for a ticker in a dataset")
    r.add_argument("ticker")
    r.add_argument("dataset", choices=DATASETS)
    r.add_argument("--head", type=int, default=20)

    c = sub.add_parser("coverage", help="do the datasets cover the ticker equally?")
    c.add_argument("ticker")

    s = sub.add_parser("sql", help="raw DuckDB over the registered views")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=50)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        import duckdb  # noqa: F401
    except Exception:
        print("duckdb is required: uv sync (adds duckdb).", file=sys.stderr)
        return 1
    con = connect()
    if args.command == "describe":
        return describe(con, args.ticker)
    if args.command == "find":
        return find(con, args.pattern)
    if args.command == "rows":
        return rows(con, args.ticker, args.dataset, args.head)
    if args.command == "coverage":
        return coverage(con, args.ticker)
    if args.command == "sql":
        return run_sql(con, args.query, args.limit)
    return 2


if __name__ == "__main__":
    sys.exit(main())
