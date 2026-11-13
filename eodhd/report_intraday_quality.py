"""Quality report for the ``intraday`` lane's 1m bars (``qc intraday``).

Reads the bar partitions (trailing ``--days``, default 30, or ``--all``), the
fetch state and, for cross-checks, the daily ``prices`` view. Reports:

- **duplicates**: more than one bar per (ticker, exchange, ts);
- **OHLC sanity**: low above open/close or high below them by more than
  ``OHLC_TOLERANCE`` (0.1%), or prices <= 0;
- **thin sessions**: regular-hours bar count per pair and day below
  ``THIN_SHARE`` of the expected 390 (US, INDX) or 510 (LSE). Market-wide short
  days (half days) are excluded per exchange: a day counts as short on an
  exchange when its median pair has fewer than ``THIN_SHARE`` of that
  exchange's typical (median) day's bars (only where the typical day is itself
  near the nominal length; not the thin LSE ETFs). Pair-days without any
  regular-hours bar (weekend dates of the 24-hour index quotes) are skipped;
  missing days are the ``holes`` check's job;
- **gaps**: regular-hours gaps over ``GAP_MINUTES`` between consecutive bars,
  T1 names only;
- **close check**: the last regular-hours close against the unadjusted daily
  close; a ratio off by more than ``CLOSE_TOLERANCE`` means a missed split or
  a bad bar;
- **holes**: trading days inside a pair's bar span where the daily lanes
  have a price but there are no 1m bars at all (vendor holes such as TSLA
  2023-08 .. 2024-04);
- **volume reconciliation**: (regular-hours volume + the first post-close
  bar, which carries the closing cross) / daily volume, as quantiles per
  exchange, with daily volume un-split-adjusted from the `splits` view (EODHD
  daily volume is split-adjusted, close is not) -- information, not a failure
  (design doc section 11);
- **state**: status counts and pairs whose history is still to fill.

Design: docs/INTRADAY_1M_DESIGN.md, section 8.

Usage:
    uv run python eodhd/report_intraday_quality.py [--days N | --all] [--json]
    uv run python eodhd/cli.py qc intraday
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # noqa: E402
from _datadir import EODHD_RAW_ROOT  # noqa: E402

RAW_DIR = EODHD_RAW_ROOT / "intraday"
BARS_DIR = RAW_DIR / "bars_1m"
STATE_PATH = RAW_DIR / "bars_1m_fetch_state.csv"
TICKERS_PATH = RAW_DIR / "tickers_INTRADAY.parquet"

EXPECTED_RTH = {"US": 390, "INDX": 390, "LSE": 510}
THIN_SHARE = 0.8
GAP_MINUTES = 5
CLOSE_TOLERANCE = 0.02
#: Relative slack for the OHLC check: the LSE feed rounds open/close and high/low
#: differently (about 2,700 VUSA bars off by 0.01%), which is noise, not a bad bar.
OHLC_TOLERANCE = 0.001
#: A pair with at least this many missing trading days inside its span is flagged.
HOLE_MIN_DAYS = 5
DEFAULT_DAYS = 30


def _expected_case() -> str:
    parts = " ".join(f"WHEN '{k}' THEN {v}" for k, v in EXPECTED_RTH.items())
    return f"CASE exchange {parts} ELSE NULL END"


def run_report(
    con: Any,
    bars_glob: str,
    state_path: Path,
    *,
    since: str | None,
    t1: list[tuple[str, str]] | None = None,
    has_prices: bool = False,
) -> dict[str, Any]:
    """Compute the facts (pure SQL over the bars; ``prices`` view optional)."""
    where = f"WHERE date >= '{since}'" if since else ""
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW _b AS SELECT * FROM read_parquet('{bars_glob}') {where}"
    )
    out: dict[str, Any] = {"since": since}
    n, pairs, first, last = con.execute(
        "SELECT count(*), count(DISTINCT ticker || '.' || exchange), min(date), max(date) FROM _b"
    ).fetchone()
    out["totals"] = {
        "bars": int(n),
        "pairs": int(pairs),
        "first_day": first,
        "last_day": last,
    }
    out["duplicates"] = int(
        con.execute(
            "SELECT count(*) - count(DISTINCT (ticker, exchange, ts)) FROM _b"
        ).fetchone()[0]
    )
    out["ohlc_bad"] = int(
        con.execute(
            f"SELECT count(*) FROM _b WHERE low > least(open, close) * (1 + {OHLC_TOLERANCE}) "
            f"OR high < greatest(open, close) * (1 - {OHLC_TOLERANCE}) "
            "OR least(open, high, low, close) <= 0"
        ).fetchone()[0]
    )
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _day AS
        SELECT ticker, exchange, date,
               count(*) FILTER (WHERE session = 'rth') AS n_rth,
               arg_max(close, ts) FILTER (WHERE session = 'rth') AS last_rth_close,
               sum(volume) FILTER (WHERE session = 'rth') AS v_rth,
               arg_min(volume, ts) FILTER (WHERE session = 'post') AS v_close_bar,
               {_expected_case()} AS expected
        FROM _b GROUP BY ticker, exchange, date
        """)
    # A short day (half day) is judged per exchange against that exchange's own
    # typical day. Only exchanges whose typical day is near the nominal session
    # count: in a thin market (the LSE ETFs rarely fill 510 minutes) bar counts
    # say nothing about the calendar. Pair-days with no regular-hours bar at all
    # (weekend and holiday dates of the 24-hour index quotes) are not sessions.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _short AS
        WITH dm AS (SELECT exchange, date, median(n_rth) AS m, any_value(expected) AS expected
                    FROM _day WHERE expected IS NOT NULL AND n_rth > 0 GROUP BY 1, 2),
             em AS (SELECT exchange, median(m) AS typical FROM dm GROUP BY 1)
        SELECT dm.exchange, dm.date FROM dm JOIN em USING (exchange)
        WHERE dm.m < {THIN_SHARE} * em.typical AND em.typical >= {THIN_SHARE} * dm.expected
        """)
    short = con.execute(
        "SELECT exchange, date FROM _short ORDER BY date, exchange"
    ).fetchall()
    out["short_days"] = sorted({d for _, d in short})
    out["short_days_by_exchange"] = {
        e: n
        for e, n in con.execute(
            "SELECT exchange, count(*) FROM _short GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    thin = con.execute(f"""
        SELECT ticker || '.' || exchange AS pair, count(*) AS days,
               count(*) FILTER (WHERE n_rth < {THIN_SHARE} * expected) AS thin_days
        FROM _day d WHERE expected IS NOT NULL AND n_rth > 0
          AND NOT EXISTS (SELECT 1 FROM _short s WHERE s.exchange = d.exchange AND s.date = d.date)
        GROUP BY 1 HAVING thin_days > 0 ORDER BY thin_days * 1.0 / days DESC, pair LIMIT 15
        """).fetchall()
    out["thin"] = [{"pair": p, "days": int(d), "thin_days": int(t)} for p, d, t in thin]
    out["gaps"] = []
    if t1:
        con.execute(
            "CREATE OR REPLACE TEMP TABLE _t1 AS SELECT * FROM (SELECT unnest(?::VARCHAR[]) AS pair)",
            [[f"{t}.{e}" for t, e in t1]],
        )
        gaps = con.execute(f"""
            WITH r AS (
              SELECT ticker || '.' || exchange AS pair, date, ts,
                     lag(ts) OVER (PARTITION BY ticker, exchange, date ORDER BY ts) AS prev
              FROM _b WHERE session = 'rth' AND ticker || '.' || exchange IN (SELECT pair FROM _t1))
            SELECT pair, count(*) AS gaps, max(date_diff('minute', prev, ts)) AS worst
            FROM r WHERE prev IS NOT NULL AND date_diff('minute', prev, ts) > {GAP_MINUTES}
            GROUP BY 1 ORDER BY gaps DESC, pair LIMIT 15
            """).fetchall()
        out["gaps"] = [
            {"pair": p, "gaps": int(g), "worst_minutes": int(w)} for p, g, w in gaps
        ]
    out["close_mismatch"] = []
    out["volume_ratio"] = []
    out["holes"] = []
    if has_prices:
        holes = con.execute("""
            WITH span AS (SELECT ticker, exchange, min(date) AS lo, max(date) AS hi FROM _day GROUP BY 1, 2),
                 px AS (SELECT DISTINCT upper(ticker) AS ticker, upper(exchange) AS exchange,
                               CAST(date AS VARCHAR) AS date FROM prices)
            SELECT s.ticker || '.' || s.exchange AS pair, count(*) AS n, min(px.date), max(px.date)
            FROM span s
            JOIN px ON px.ticker = upper(s.ticker) AND px.exchange = upper(s.exchange)
                   AND px.date BETWEEN s.lo AND s.hi
            LEFT JOIN _day d ON d.ticker = s.ticker AND d.exchange = s.exchange AND d.date = px.date
            WHERE d.date IS NULL
            GROUP BY 1 ORDER BY n DESC, pair LIMIT 15
            """).fetchall()
        out["holes"] = [
            {"pair": p, "missing_days": int(n), "first": a, "last": b}
            for p, n, a, b in holes
        ]
        con.execute("""
            CREATE OR REPLACE TEMP TABLE _x AS
            SELECT d.*, p.close AS daily_close, p.volume AS daily_volume
            FROM _day d JOIN (SELECT upper(ticker) AS ticker, upper(exchange) AS exchange, CAST(date AS VARCHAR) AS date,
                                     any_value(close) AS close, any_value(volume) AS volume
                              FROM prices GROUP BY 1, 2, 3) p
              ON upper(d.ticker) = p.ticker AND upper(d.exchange) = p.exchange AND d.date = p.date
            WHERE d.last_rth_close IS NOT NULL AND p.close > 0
            """)
        # EODHD daily volume is split-adjusted (close is raw); undo the splits
        # that came after each day so it compares with the raw 1m volume.
        has_splits = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = 'splits'"
        ).fetchone()[0]
        if has_splits:
            con.execute("""
                UPDATE _x SET daily_volume = daily_volume / f.factor
                FROM (SELECT x.ticker, x.exchange, x.date, exp(sum(ln(s.split_ratio))) AS factor
                      FROM _x x JOIN (SELECT DISTINCT upper(ticker) AS ticker, upper(exchange) AS exchange,
                                             CAST(ex_date AS VARCHAR) AS ex_date, split_ratio
                                      FROM splits WHERE split_ratio > 0 AND split_ratio <> 1) s
                        ON s.ticker = upper(x.ticker) AND s.exchange = upper(x.exchange) AND s.ex_date > x.date
                      GROUP BY 1, 2, 3) f
                WHERE _x.ticker = f.ticker AND _x.exchange = f.exchange AND _x.date = f.date
                """)
        mism = con.execute(f"""
            SELECT ticker || '.' || exchange, date, last_rth_close, daily_close,
                   round(last_rth_close / daily_close, 4) AS ratio
            FROM _x WHERE abs(last_rth_close / daily_close - 1) > {CLOSE_TOLERANCE}
            ORDER BY abs(ln(last_rth_close / daily_close)) DESC LIMIT 15
            """).fetchall()
        out["close_mismatch"] = [
            {
                "pair": p,
                "date": d,
                "bar_close": float(b),
                "daily_close": float(c),
                "ratio": float(r),
            }
            for p, d, b, c, r in mism
        ]
        out["close_mismatch_count"] = int(
            con.execute(
                f"SELECT count(*) FROM _x WHERE abs(last_rth_close / daily_close - 1) > {CLOSE_TOLERANCE}"
            ).fetchone()[0]
        )
        vol = con.execute("""
            SELECT exchange, count(*) AS n,
                   quantile_cont(r, 0.1), quantile_cont(r, 0.5), quantile_cont(r, 0.9)
            FROM (SELECT exchange, (coalesce(v_rth, 0) + coalesce(v_close_bar, 0)) / daily_volume AS r
                  FROM _x WHERE daily_volume > 0 AND expected IS NOT NULL)
            GROUP BY 1 ORDER BY 1
            """).fetchall()
        out["volume_ratio"] = [
            {
                "exchange": e,
                "pair_days": int(n),
                "p10": round(a, 3),
                "p50": round(b, 3),
                "p90": round(c, 3),
            }
            for e, n, a, b, c in vol
        ]
    out["state"] = {"present": state_path.exists(), "status": {}, "partial": 0}
    if state_path.exists():
        rows = con.execute(
            f"SELECT coalesce(nullif(status, ''), '(none)'), count(*) FROM read_csv_auto('{state_path.as_posix()}', all_varchar=true) GROUP BY 1"
        ).fetchall()
        out["state"]["status"] = {s: int(k) for s, k in rows}
        out["state"]["partial"] = out["state"]["status"].get("partial", 0)
    return out


def flags(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    if report["duplicates"]:
        out.append(
            (
                "error",
                "duplicates",
                f"{report['duplicates']:,} duplicate (pair, ts) bars",
            )
        )
    if report["ohlc_bad"]:
        out.append(("warning", "ohlc", f"{report['ohlc_bad']:,} bars fail OHLC sanity"))
    if report.get("close_mismatch_count"):
        worst = report["close_mismatch"][0]
        out.append(
            (
                "warning",
                "close_check",
                f"{report['close_mismatch_count']:,} pair-days where the last regular-hours close is more than "
                f"{CLOSE_TOLERANCE:.0%} off the daily close, worst {worst['pair']} on {worst['date']} "
                f"(ratio {worst['ratio']}) -- a missed split or a bad bar",
            )
        )
    big_holes = [
        h for h in report.get("holes", []) if h["missing_days"] >= HOLE_MIN_DAYS
    ]
    if big_holes:
        worst = big_holes[0]
        out.append(
            (
                "warning",
                "holes",
                f"{len(big_holes)} pair(s) miss 1m bars on {HOLE_MIN_DAYS}+ trading days inside their span, worst "
                f"{worst['pair']} ({worst['missing_days']} days between {worst['first']} and {worst['last']}) "
                "-- vendor holes; refetching does not fill them",
            )
        )
    if report["thin"]:
        worst = report["thin"][0]
        out.append(
            (
                "warning",
                "thin_sessions",
                f"{len(report['thin'])} pair(s) with regular sessions below {THIN_SHARE:.0%} of the expected bars, "
                f"worst {worst['pair']} ({worst['thin_days']} of {worst['days']} days)",
            )
        )
    if report["gaps"]:
        worst = report["gaps"][0]
        out.append(
            (
                "info",
                "gaps",
                f"T1 regular-hours gaps over {GAP_MINUTES} min in {len(report['gaps'])} pair(s), "
                f"most in {worst['pair']} ({worst['gaps']}, longest {worst['worst_minutes']} min)",
            )
        )
    for v in report["volume_ratio"]:
        out.append(
            (
                "info",
                "volume_vs_daily",
                f"{v['exchange']}: (regular hours + closing bar) / daily volume p10 {v['p10']} / p50 {v['p50']} / "
                f"p90 {v['p90']} over {v['pair_days']:,} pair-days -- vendor 1m volume is not daily volume",
            )
        )
    status = report["state"]["status"]
    bad = {k: v for k, v in status.items() if k in {"error", "not_found"}}
    if bad:
        out.append(
            (
                "warning",
                "fetch_state",
                ", ".join(f"{v} {k}" for k, v in sorted(bad.items()))
                + " -- check last_error in bars_1m_fetch_state.csv (fix the toml for not_found symbols)",
            )
        )
    if report["state"]["partial"]:
        out.append(
            (
                "info",
                "backfill",
                f"{report['state']['partial']} pair(s) still filling history (refresh continues newest-first)",
            )
        )
    if report["short_days"]:
        out.append(
            (
                "info",
                "short_days",
                f"{len(report['short_days'])} market-wide short day(s) excluded ("
                + ", ".join(
                    f"{e} {n}"
                    for e, n in report.get("short_days_by_exchange", {}).items()
                )
                + "): "
                + ", ".join(report["short_days"][:6]),
            )
        )
    return out


def print_report(
    console: Any, report: dict[str, Any], flag_rows: list[tuple[str, str, str]]
) -> None:
    from rich.text import Text

    t = report["totals"]
    head = Text("QC · intraday bars_1m", style="bold")
    errs = sum(1 for f in flag_rows if f[0] == "error")
    warns = sum(1 for f in flag_rows if f[0] == "warning")
    head.append(
        f"   ✗ {errs} errors · ⚠ {warns} warnings   since {report['since'] or 'all'}",
        style="dim",
    )
    console.print(head)
    table = _render.minimal_table()
    for col in ("bars", "pairs", "first", "last", "duplicates", "ohlc bad", "state"):
        table.add_column(col, justify="right", no_wrap=True)
    status = (
        ", ".join(f"{k}={v}" for k, v in sorted(report["state"]["status"].items()))
        or "-"
    )
    table.add_row(
        f"{t['bars']:,}",
        f"{t['pairs']:,}",
        str(t["first_day"]),
        str(t["last_day"]),
        f"{report['duplicates']:,}",
        f"{report['ohlc_bad']:,}",
        status,
    )
    console.print(table)
    if flag_rows:
        ft = _render.minimal_table(title="flags")
        ft.add_column("", no_wrap=True)
        ft.add_column("issue", no_wrap=True)
        ft.add_column("detail", overflow="fold")
        for sev, code, detail in flag_rows:
            glyph, style = _render.SEVERITY.get(sev, ("•", ""))
            ft.add_row(Text(glyph, style=style), code, detail)
        console.print(ft)
    else:
        console.print(Text("no flags", style="green"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=os.environ.get("DATACLI_PROG") or None,
        description="Quality report for the intraday lane's 1m bars (duplicates, OHLC, thin sessions, gaps, "
        "close and volume against daily data, fetch state)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"Trailing days of bars (default {DEFAULT_DAYS}).",
    )
    parser.add_argument(
        "--all", action="store_true", help="Every bar (slow on a full backfill)."
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the raw report as JSON."
    )
    parser.add_argument("--no-color", dest="no_color", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = _render.make_console(no_color=True if args.no_color else None)
    if not BARS_DIR.is_dir() or not any(BARS_DIR.glob("*.parquet")):
        console.print(
            f"[yellow]no 1m bars under {BARS_DIR}[/yellow] -- fetch first: refresh intraday --datasets bars_1m --run"
        )
        return 0
    import explore_eodhd
    import pandas as pd

    con = explore_eodhd.connect()
    since = (
        None
        if args.all
        else (datetime.now(timezone.utc).date() - timedelta(days=args.days)).isoformat()
    )
    t1: list[tuple[str, str]] = []
    if TICKERS_PATH.exists():
        cur = pd.read_parquet(TICKERS_PATH)
        t1 = [
            (r.ticker, r.exchange)
            for r in cur[cur["tier"] == "T1"].itertuples(index=False)
        ]
    report = run_report(
        con,
        (BARS_DIR / "*.parquet").as_posix(),
        STATE_PATH,
        since=since,
        t1=t1,
        has_prices=explore_eodhd._has_view(con, "prices"),
    )
    flag_rows = flags(report)
    if args.json:
        print(json.dumps({**report, "flags": flag_rows}, indent=2, default=str))
    else:
        print_report(console, report, flag_rows)
    return 1 if any(f[0] == "error" for f in flag_rows) else 0


if __name__ == "__main__":
    sys.exit(main())
