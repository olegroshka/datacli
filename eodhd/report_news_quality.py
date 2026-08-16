"""Corpus hygiene report for the ``news`` lane (``qc news``).

The price QC engine audits ticker-keyed lanes; the news corpus needs its own
checks. This report reads the article partitions and the crawl state and
reports, with thresholds, the things that bite downstream models:

- **crawl gaps**: days between the first and last partition with no partition,
  and state rows that are not ``ok``;
- **empty content** share (articles with no/short text);
- **untagged** share (no symbol tags at all) and **junk symbol tags**
  (``USDUSD.FOREX``, symbols with ``:``);
- **re-publications** (same ``article_id`` in more than one day partition);
- **tagging bursts**: (day, symbol) pairs whose share of the day's articles is
  implausibly high (the AAPL 30 % day) -- counts must be normalised;
- **volume by year** and **top sources** -- the vendor's coverage is not
  stationary (2024 dip).

Usage:
    uv run python eodhd/report_news_quality.py [--since YYYY-MM-DD | --all] [--json]
    uv run python eodhd/cli.py qc news            # trailing 365 days
    uv run python eodhd/cli.py qc news --all      # whole corpus (minutes)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # noqa: E402
from _datadir import EODHD_RAW_ROOT  # noqa: E402

RAW_DIR = EODHD_RAW_ROOT / "news"
ARTICLES_DIR = RAW_DIR / "articles"
STATE_PATH = RAW_DIR / "news_fetch_state.csv"

#: A (day, symbol) tagged on more than this share of the day's articles is a burst.
BURST_SHARE = 0.15
#: Content shorter than this many characters counts as empty.
MIN_CHARS = 200
JUNK_SYMBOL_PATTERNS = ("USDUSD.FOREX",)


def run_report(
    con: Any,
    articles_glob: str,
    state_path: Path,
    *,
    since: str | None = None,
) -> dict[str, Any]:
    """Compute the hygiene facts. Pure w.r.t. output; SQL over the corpus."""
    where = f"WHERE date >= DATE '{since}'" if since else ""
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW _news AS SELECT * FROM read_parquet('{articles_glob}') {where}"
    )
    out: dict[str, Any] = {}
    row = con.execute(f"""
        SELECT count(*) AS rows, count(DISTINCT article_id) AS articles,
               min(date) AS first_day, max(date) AS last_day, count(DISTINCT date) AS days,
               sum(CASE WHEN content IS NULL OR length(content) < {MIN_CHARS} THEN 1 ELSE 0 END) AS empty_content,
               sum(CASE WHEN len(symbols) = 0 THEN 1 ELSE 0 END) AS untagged,
               sum(CASE WHEN len(tags) = 0 THEN 1 ELSE 0 END) AS no_topic_tags,
               round(avg(length(content))) AS avg_chars
        FROM _news
        """).fetchone()
    keys = [
        "rows",
        "articles",
        "first_day",
        "last_day",
        "days",
        "empty_content",
        "untagged",
        "no_topic_tags",
        "avg_chars",
    ]
    out["totals"] = {
        k: (
            str(v)
            if k in ("first_day", "last_day")
            else (int(v) if v is not None else 0)
        )
        for k, v in zip(keys, row)
    }
    n = max(out["totals"]["articles"], 1)
    out["totals"]["empty_share"] = round(out["totals"]["empty_content"] / n, 4)
    out["totals"]["untagged_share"] = round(out["totals"]["untagged"] / n, 4)
    out["totals"]["republished_articles"] = int(
        con.execute(
            "SELECT count(*) FROM (SELECT article_id FROM _news GROUP BY 1 HAVING count(DISTINCT date) > 1)"
        ).fetchone()[0]
    )
    # crawl gaps: calendar days without a partition between first and last
    days = [
        str(r[0])
        for r in con.execute("SELECT DISTINCT date FROM _news ORDER BY 1").fetchall()
    ]
    if days:
        import datetime as _dt

        d0, d1 = _dt.date.fromisoformat(days[0]), _dt.date.fromisoformat(days[-1])
        have = set(days)
        missing = [
            (d0 + _dt.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)
        ]
        missing = [d for d in missing if d not in have]
    else:
        missing = []
    out["missing_days"] = {"count": len(missing), "sample": missing[:10]}
    # crawl state
    state_bad: list[dict[str, Any]] = []
    if state_path.exists():
        import pandas as pd

        st = pd.read_csv(state_path, dtype=str)
        bad = st[st["status"] != "ok"] if "status" in st.columns else st.iloc[0:0]
        state_bad = (
            bad[["date", "status", "detail"]].head(20).to_dict(orient="records")
            if len(bad)
            else []
        )
        out["state"] = {
            "days": int(len(st)),
            "not_ok": int(len(bad)),
            "sample": state_bad,
        }
    else:
        out["state"] = {"days": 0, "not_ok": 0, "sample": []}
    # junk symbol tags
    junk_where = " OR ".join(
        [f"upper(s) = '{j}'" for j in JUNK_SYMBOL_PATTERNS] + ["s LIKE '%:%'"]
    )
    junk = con.execute(f"""
        SELECT upper(s) AS sym, count(*) AS n FROM _news, unnest(symbols) t(s)
        WHERE {junk_where} GROUP BY 1 ORDER BY 2 DESC LIMIT 15
        """).fetchall()
    out["junk_symbols"] = [{"symbol": s, "n": int(k)} for s, k in junk]
    # tagging bursts
    bursts = con.execute(f"""
        WITH tot AS (SELECT date, count(DISTINCT article_id) AS n_day FROM _news GROUP BY 1),
             per AS (SELECT date, upper(s) AS sym, count(DISTINCT article_id) AS n
                     FROM _news, unnest(symbols) t(s) GROUP BY 1, 2)
        SELECT cast(per.date AS VARCHAR), sym, n, n_day, round(n * 1.0 / n_day, 3) AS share
        FROM per JOIN tot USING (date)
        WHERE split_part(sym, '.', -1) NOT IN ('INDX', 'FOREX', 'CC', 'COMM')
          AND n * 1.0 / n_day > {BURST_SHARE} AND n_day >= 200
        ORDER BY share DESC LIMIT 15
        """).fetchall()
    out["bursts"] = [
        {"date": d, "symbol": s, "n": int(n), "n_day": int(nd), "share": float(sh)}
        for d, s, n, nd, sh in bursts
    ]
    out["by_year"] = [
        {"year": int(y), "articles": int(a), "per_day": int(p)}
        for y, a, p in con.execute(
            "SELECT year(date), count(DISTINCT article_id), round(count(DISTINCT article_id) / count(DISTINCT date)) FROM _news GROUP BY 1 ORDER BY 1"
        ).fetchall()
    ]
    out["top_sources"] = [
        {"source": s or "(none)", "articles": int(a), "share": round(int(a) / n, 3)}
        for s, a in con.execute(
            "SELECT source, count(*) FROM _news GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        ).fetchall()
    ]
    return out


def flags(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``(severity, code, detail)`` triage lines derived from the report."""
    out: list[tuple[str, str, str]] = []
    t = report["totals"]
    if report["missing_days"]["count"]:
        out.append(
            (
                "error",
                "crawl_gap",
                f"{report['missing_days']['count']} day(s) without a partition, e.g. {', '.join(report['missing_days']['sample'][:5])} -- run fetch_eodhd_news.py --from <first missing>",
            )
        )
    if report["state"]["not_ok"]:
        out.append(
            (
                "error",
                "crawl_state",
                f"{report['state']['not_ok']} state row(s) not ok -- re-run fetch_eodhd_news.py (they are re-crawled automatically)",
            )
        )
    if t["empty_share"] > 0.01:
        out.append(
            (
                "warning",
                "empty_content",
                f"{t['empty_share']:.1%} of articles have < {MIN_CHARS} chars",
            )
        )
    if t["untagged_share"] > 0.20:
        out.append(
            (
                "warning",
                "untagged",
                f"{t['untagged_share']:.1%} of articles carry no symbol tag",
            )
        )
    if report["junk_symbols"]:
        top = ", ".join(
            f"{j['symbol']} ({j['n']:,})" for j in report["junk_symbols"][:3]
        )
        out.append(
            (
                "warning",
                "junk_symbols",
                f"junk symbol tags present: {top} -- filter them in symbol-level features",
            )
        )
    if report["bursts"]:
        b = report["bursts"][0]
        out.append(
            (
                "warning",
                "tagging_burst",
                f"{len(report['bursts'])} (day, symbol) burst(s) > {BURST_SHARE:.0%} of the day, worst {b['symbol']} on {b['date']} ({b['share']:.0%}) -- normalise counts by share_of_day",
            )
        )
    years = report["by_year"]
    if len(years) >= 3:
        med = sorted(y["per_day"] for y in years)[len(years) // 2]
        thin = [y for y in years if y["per_day"] < 0.65 * med]
        if thin:
            out.append(
                (
                    "info",
                    "volume_dip",
                    "thin year(s): "
                    + ", ".join(
                        f"{y['year']} ({y['per_day']}/day vs median {med})"
                        for y in thin
                    ),
                )
            )
    if t["republished_articles"]:
        out.append(
            (
                "info",
                "republications",
                f"{t['republished_articles']:,} article_ids appear in more than one day partition (vendor re-publications; take the latest published_at per article_id for one row per article)",
            )
        )
    return out


def print_report(
    console: Any, report: dict[str, Any], flag_rows: list[tuple[str, str, str]]
) -> None:
    from rich.text import Text

    t = report["totals"]
    head = Text("QC · news", style="bold")
    errs = sum(1 for f in flag_rows if f[0] == "error")
    warns = sum(1 for f in flag_rows if f[0] == "warning")
    head.append(f"   ✗ {errs} errors · ⚠ {warns} warnings", style="dim")
    console.print(head)
    table = _render.minimal_table()
    for col in (
        "articles",
        "days",
        "first",
        "last",
        "avg chars",
        "empty",
        "untagged",
        "republished",
        "missing days",
        "state not ok",
    ):
        table.add_column(col, justify="right", no_wrap=True)
    table.add_row(
        f"{t['articles']:,}",
        f"{t['days']:,}",
        t["first_day"],
        t["last_day"],
        f"{t['avg_chars']:,}",
        f"{t['empty_share']:.2%}",
        f"{t['untagged_share']:.1%}",
        f"{t['republished_articles']:,}",
        f"{report['missing_days']['count']:,}",
        f"{report['state']['not_ok']:,}",
    )
    console.print(table)
    if report["by_year"]:
        yt = _render.minimal_table(title="articles per day by year")
        for col in ("year", "articles", "per day"):
            yt.add_column(col, justify="right", no_wrap=True)
        for y in report["by_year"]:
            yt.add_row(str(y["year"]), f"{y['articles']:,}", f"{y['per_day']:,}")
        console.print(yt)
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
        description="Corpus hygiene report for the news lane (crawl gaps, empty/untagged shares, junk symbols, re-publications, tagging bursts, volume by year, sources)",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only consider articles on/after this day (default: the trailing 365 days)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="The whole corpus (slower: minutes on 4M+ articles)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Print the raw report as JSON"
    )
    parser.add_argument("--no-color", dest="no_color", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    console = _render.make_console(no_color=True if args.no_color else None)
    if not ARTICLES_DIR.is_dir() or not any(ARTICLES_DIR.glob("*.parquet")):
        console.print(
            f"[yellow]no news corpus under {ARTICLES_DIR}[/yellow] -- crawl first: fetch_eodhd_news.py"
        )
        return 0
    import duckdb

    con = duckdb.connect()
    since = args.since
    if since is None and not args.all:
        from datetime import date, timedelta

        since = (date.today() - timedelta(days=365)).isoformat()
    report = run_report(
        con, (ARTICLES_DIR / "*.parquet").as_posix(), STATE_PATH, since=since
    )
    if not args.json:
        console.print(
            f"[dim]window: {since or 'whole corpus'} .. today"
            + ("" if args.all or args.since else "  (--all for the whole corpus)")
            + "[/dim]"
        )
    if args.json:
        print(json.dumps({**report, "flags": flags(report)}, indent=2, default=str))
        return 0
    print_report(console, report, flags(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
