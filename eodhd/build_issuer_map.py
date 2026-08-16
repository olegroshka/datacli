"""Build ``issuer_map.parquet``: every symbol line -> the issuer behind it.

Why: EODHD's news symbol tags are exchange-specific and US-biased -- an EU/UK
issuer's US line / ADR / Frankfurt mirrors collect most of the articles
(``SAP.US`` 248 vs ``SAP.XETRA`` 1 in a month). To count news per *company* we
need ``symbol -> issuer``. Two evidence sources, unioned with union-find:

1. **Vendor keys** from the cached fundamentals ``General`` block
   (``cache/fundamentals/<EXCH>/<TICKER>.json.gz`` in ``us_common`` / ``uk_eu``):
   ``LEI`` (issuer id), ``ISIN`` (same security on several exchanges),
   ``PrimaryTicker`` and the ``Listings`` array (cross-listings / ADRs).
2. **Corpus co-tagging**: the vendor tags every line of an issuer on its
   articles, so for a minor line ``b`` the conditional probability
   ``P(parent | b)`` of its main line is ~1.0 (measured: APC.F -> AAPL.US 0.999,
   SAP.XETRA -> SAP.US 1.0, HSBA.LSE -> HSBC.US 1.0). A link ``b -> a`` is
   accepted when ``a`` is the most co-tagged bigger symbol, ``P(a|b) >= --min-p``
   (0.90) and ``b`` has at least ``--min-n`` (30) tagged articles in the window.
   Guards against peer contamination (small caps whose few articles are market
   round-ups): index / forex / crypto / commodity exchanges, ETF-universe
   symbols and ``:``-symbols are excluded, and a co-tag link never merges two
   symbols whose vendor identity (LEI, else ISIN) is known and different.

Output (in the news lane): one row per symbol with ``issuer_id`` (LEI when
known, else the primary ISIN, else ``SYM:<primary symbol>``), ``issuer_name``,
``primary_symbol`` (the vendor's ``PrimaryTicker`` if in the component, else the
most-tagged member), ``evidence`` (``vendor`` / ``isin`` / ``lei`` / ``listing``
/ ``cotag`` / ``self``), the co-tag ``p`` and ``n``, the lane the symbol belongs
to (if any) and ``in_universe``. A QC summary is printed (and returned).

Usage:
    uv run python eodhd/build_issuer_map.py                 # cache + last 365 days of corpus
    uv run python eodhd/build_issuer_map.py --days 730 --min-p 0.9
    uv run python eodhd/build_issuer_map.py --no-cotag       # vendor keys only

Outputs:
    data/raw/eodhd/news/issuer_map.parquet
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import _atomic
from _datadir import EODHD_RAW_ROOT

RAW_DIR = EODHD_RAW_ROOT / "news"
ARTICLES_DIR = RAW_DIR / "articles"
MAP_PATH = RAW_DIR / "issuer_map.parquet"
CACHE_LANES = ("us_common", "uk_eu")

#: Symbol tags on these exchanges are not issuers.
NON_ISSUER_EXCHANGES = frozenset(
    {"INDX", "FOREX", "CC", "COMM", "BOND", "GBOND", "MONEY", "EUFUND", "IL"}
)
DEFAULT_MIN_P = 0.90
DEFAULT_MIN_N = 30

MAP_SCHEMA = pa.schema(
    [
        ("symbol", pa.string()),
        ("ticker", pa.string()),
        ("exchange", pa.string()),
        ("issuer_id", pa.string()),
        ("issuer_name", pa.string()),
        ("primary_symbol", pa.string()),
        ("evidence", pa.string()),
        ("cotag_p", pa.float64()),
        ("cotag_n", pa.int64()),
        ("n_articles", pa.int64()),
        ("lane", pa.string()),
        ("in_universe", pa.bool_()),
        ("name", pa.string()),
        ("isin", pa.string()),
        ("lei", pa.string()),
        ("built_at", pa.timestamp("us", tz="UTC")),
    ]
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("issuer_map")


# --------------------------------------------------------------------------- #
# vendor keys from the fundamentals cache
# --------------------------------------------------------------------------- #
def read_general(path: Path) -> dict[str, Any] | None:
    """The ``General`` block of one cached fundamentals payload (or ``None``)."""
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    general = payload.get("General") if isinstance(payload, dict) else None
    return general if isinstance(general, dict) else None


def vendor_records(
    root: Path = EODHD_RAW_ROOT, lanes: Iterable[str] = CACHE_LANES
) -> list[dict[str, Any]]:
    """One record per cached firm: symbol, name, isin, lei, primary, listings."""
    out: list[dict[str, Any]] = []
    for lane in lanes:
        base = root / lane / "cache" / "fundamentals"
        if not base.is_dir():
            continue
        for exch_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            for f in sorted(exch_dir.glob("*.json.gz")):
                g = read_general(f)
                if g is None:
                    continue
                symbol = f"{f.name[: -len('.json.gz')]}.{exch_dir.name}".upper()
                listings = g.get("Listings") or {}
                items = listings.values() if isinstance(listings, dict) else listings
                out.append(
                    {
                        "symbol": symbol,
                        "lane": lane,
                        "name": g.get("Name"),
                        "isin": _clean(g.get("ISIN")),
                        "lei": _clean(g.get("LEI")),
                        "primary": _clean_symbol(g.get("PrimaryTicker")),
                        "listings": [
                            _clean_symbol(f"{it.get('Code')}.{it.get('Exchange')}")
                            for it in items
                            if isinstance(it, dict)
                            and it.get("Code")
                            and it.get("Exchange")
                        ],
                    }
                )
    return out


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s.lower() not in ("none", "null", "nan", "n/a", "") else None


def _clean_symbol(v: Any) -> str | None:
    s = _clean(v)
    if not s or "." not in s:
        return None
    return s.upper()


# --------------------------------------------------------------------------- #
# union-find
# --------------------------------------------------------------------------- #
class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, x: str) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: str) -> str:
        self.add(x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def groups(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for x in list(self.parent):
            out.setdefault(self.find(x), []).append(x)
        return out


def link_vendor(records: list[dict[str, Any]], uf: UnionFind) -> dict[str, str]:
    """Union symbols by PrimaryTicker / Listings / shared LEI / shared ISIN.

    Returns ``{symbol: evidence}`` for symbols linked to something else.
    """
    evidence: dict[str, str] = {}
    by_lei: dict[str, list[str]] = {}
    by_isin: dict[str, list[str]] = {}
    for r in records:
        uf.add(r["symbol"])
        if r["primary"] and r["primary"] != r["symbol"]:
            uf.union(r["primary"], r["symbol"])
            evidence[r["symbol"]] = "vendor"
            evidence.setdefault(r["primary"], "vendor")
        for lst in r["listings"]:
            if lst and lst != r["symbol"]:
                uf.union(r["symbol"], lst)
                evidence.setdefault(lst, "listing")
                evidence.setdefault(r["symbol"], "vendor")
        if r["lei"]:
            by_lei.setdefault(r["lei"], []).append(r["symbol"])
        if r["isin"]:
            by_isin.setdefault(r["isin"], []).append(r["symbol"])
    for key, members in by_lei.items():
        for m in members[1:]:
            uf.union(members[0], m)
            evidence.setdefault(m, "lei")
            evidence.setdefault(members[0], "lei")
    for key, members in by_isin.items():
        for m in members[1:]:
            uf.union(members[0], m)
            evidence.setdefault(m, "isin")
            evidence.setdefault(members[0], "isin")
    return evidence


# --------------------------------------------------------------------------- #
# corpus co-tagging
# --------------------------------------------------------------------------- #
def cotag_candidates(
    con: Any,
    articles_glob: str,
    *,
    since: str,
    min_p: float = DEFAULT_MIN_P,
    min_n: int = DEFAULT_MIN_N,
) -> pd.DataFrame:
    """For every symbol ``b``: its best co-tagged bigger symbol ``a`` and ``P(a|b)``.

    Returns rows ``(sym, parent, p, n_b, n_a, n_both)`` for accepted links only,
    plus every symbol's article count in ``n_b`` via a second frame (see
    :func:`symbol_counts`).
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _tags AS
        SELECT DISTINCT article_id, upper(trim(s)) AS sym
        FROM read_parquet('{articles_glob}'), unnest(symbols) t(s)
        WHERE date >= DATE '{since}' AND s IS NOT NULL AND trim(s) <> ''
        """)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _cnt AS SELECT sym, count(*) AS n FROM _tags GROUP BY 1"
    )
    excluded = ", ".join(f"'{e}'" for e in sorted(NON_ISSUER_EXCHANGES))
    df = con.execute(
        f"""
        WITH pairs AS (
          SELECT b.sym AS sym, a.sym AS parent, count(*) AS n_both
          FROM _tags b JOIN _tags a USING (article_id)
          WHERE a.sym <> b.sym
          GROUP BY 1, 2
        ),
        scored AS (
          SELECT p.sym, p.parent, p.n_both, cb.n AS n_b, ca.n AS n_a,
                 p.n_both * 1.0 / cb.n AS p,
                 row_number() OVER (PARTITION BY p.sym ORDER BY p.n_both DESC, ca.n DESC, p.parent) AS rk
          FROM pairs p
          JOIN _cnt cb ON cb.sym = p.sym
          JOIN _cnt ca ON ca.sym = p.parent
          WHERE ca.n >= cb.n
            AND split_part(p.sym, '.', -1) NOT IN ({excluded})
            AND split_part(p.parent, '.', -1) NOT IN ({excluded})
        )
        SELECT sym, parent, p, n_b, n_a, n_both
        FROM scored WHERE rk = 1 AND p >= ? AND n_b >= ?
        ORDER BY sym
        """,
        [min_p, min_n],
    ).df()
    return df


def symbol_counts(con: Any) -> dict[str, int]:
    return {r[0]: int(r[1]) for r in con.execute("SELECT sym, n FROM _cnt").fetchall()}


def link_cotag(
    cands: pd.DataFrame,
    uf: UnionFind,
    evidence: dict[str, str],
    *,
    identity: dict[str, str] | None = None,
    exclude: set[str] | None = None,
) -> tuple[dict[str, tuple[float, int]], int]:
    """Union accepted co-tag links; returns ``({sym: (p, n_b)}, n_rejected)``.

    ``identity`` maps a symbol to its vendor identity (LEI, else ISIN); a link
    whose two sides carry *different* known identities is rejected -- that is
    the peer-contamination case (two real companies co-tagged in round-ups).
    ``exclude`` lists symbols that may be neither side (ETFs, junk).
    """
    identity = identity or {}
    exclude = exclude or set()
    # identity of each *live* component: seeded from every symbol's own identity
    comp: dict[str, str] = {}
    for sym, ident in identity.items():
        comp.setdefault(uf.find(sym), ident)
    out: dict[str, tuple[float, int]] = {}
    rejected = 0
    for row in cands.itertuples(index=False):
        a, b = str(row.parent), str(row.sym)
        if a in exclude or b in exclude or ":" in a or ":" in b:
            rejected += 1
            continue
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            out.setdefault(b, (float(row.p), int(row.n_b)))
            continue
        ia, ib = comp.get(ra), comp.get(rb)
        if ia and ib and ia != ib:
            rejected += 1
            continue
        uf.union(a, b)
        comp[uf.find(a)] = ia or ib  # type: ignore[assignment]
        if comp[uf.find(a)] is None:
            comp.pop(uf.find(a), None)
        evidence.setdefault(b, "cotag")
        evidence.setdefault(a, "cotag")
        out[b] = (float(row.p), int(row.n_b))
    return out, rejected


# --------------------------------------------------------------------------- #
# assemble
# --------------------------------------------------------------------------- #
def assemble(
    uf: UnionFind,
    records: list[dict[str, Any]],
    evidence: dict[str, str],
    cotag: dict[str, tuple[float, int]],
    counts: dict[str, int],
    universe: set[str],
    *,
    built_at: str,
) -> pd.DataFrame:
    meta = {r["symbol"]: r for r in records}
    rows: list[dict[str, Any]] = []
    for _root, members in uf.groups().items():
        members = sorted(set(members))
        leis = sorted({meta[m]["lei"] for m in members if m in meta and meta[m]["lei"]})
        primaries = [
            meta[m]["primary"]
            for m in members
            if m in meta and meta[m]["primary"] in members
        ]
        primary = (
            Counter(primaries).most_common(1)[0][0]
            if primaries
            else max(members, key=lambda m: (counts.get(m, 0), m in meta, -len(m)))
        )
        isin_primary = meta.get(primary, {}).get("isin") or next(
            (meta[m]["isin"] for m in members if m in meta and meta[m]["isin"]), None
        )
        issuer_id = (
            leis[0] if leis else (isin_primary if isin_primary else f"SYM:{primary}")
        )
        name = meta.get(primary, {}).get("name") or next(
            (meta[m]["name"] for m in members if m in meta and meta[m]["name"]), None
        )
        for m in members:
            ticker, _, exchange = m.rpartition(".")
            r = meta.get(m, {})
            p, n = cotag.get(m, (None, None))
            rows.append(
                {
                    "symbol": m,
                    "ticker": ticker or m,
                    "exchange": exchange or None,
                    "issuer_id": issuer_id,
                    "issuer_name": name,
                    "primary_symbol": primary,
                    "evidence": evidence.get(m, "self"),
                    "cotag_p": p,
                    "cotag_n": n,
                    "n_articles": counts.get(m, 0),
                    "lane": r.get("lane"),
                    "in_universe": m in universe,
                    "name": r.get("name"),
                    "isin": r.get("isin"),
                    "lei": r.get("lei"),
                    "built_at": built_at,
                }
            )
    df = pd.DataFrame(rows, columns=MAP_SCHEMA.names)
    return df.sort_values(["issuer_id", "symbol"]).reset_index(drop=True)


def qc_summary(df: pd.DataFrame, records: list[dict[str, Any]]) -> dict[str, Any]:
    """Facts about the map: sizes, collisions, universe coverage, spot checks."""
    out: dict[str, Any] = {}
    out["symbols"] = int(len(df))
    out["issuers"] = int(df["issuer_id"].nunique())
    sizes = df.groupby("issuer_id").size()
    out["members_per_issuer"] = {
        "p50": float(sizes.median()),
        "p90": float(sizes.quantile(0.9)),
        "max": int(sizes.max()),
    }
    out["evidence"] = df["evidence"].value_counts().to_dict()
    # collision: one component holding more than one LEI
    leis = df.dropna(subset=["lei"]).groupby("issuer_id")["lei"].nunique()
    out["lei_collisions"] = int((leis > 1).sum())
    for lane in CACHE_LANES:
        lane_syms = [r["symbol"] for r in records if r["lane"] == lane]
        sub = df[df["symbol"].isin(lane_syms)]
        multi = sub.groupby("issuer_id").size()
        has_us = df[
            df["symbol"].isin(lane_syms) | df["issuer_id"].isin(sub["issuer_id"])
        ]
        us_line = has_us[has_us["exchange"] == "US"].groupby("issuer_id").size().index
        out[f"{lane}_symbols"] = int(len(sub))
        out[f"{lane}_share_with_other_lines"] = (
            round(float((sub["issuer_id"].map(sizes) > 1).mean()), 3)
            if len(sub)
            else 0.0
        )
        out[f"{lane}_share_with_us_line"] = (
            round(float(sub["issuer_id"].isin(us_line).mean()), 3) if len(sub) else 0.0
        )
    return out


def spot_check(df: pd.DataFrame, symbols: Iterable[str]) -> pd.DataFrame:
    idx = df.set_index("symbol")
    rows = []
    for s in symbols:
        if s in idx.index:
            r = idx.loc[s]
            members = df[df["issuer_id"] == r["issuer_id"]]["symbol"].tolist()
            rows.append(
                {
                    "symbol": s,
                    "issuer_id": r["issuer_id"],
                    "issuer_name": r["issuer_name"],
                    "primary": r["primary_symbol"],
                    "n_members": len(members),
                    "members": ", ".join(members[:12]),
                }
            )
        else:
            rows.append(
                {
                    "symbol": s,
                    "issuer_id": None,
                    "issuer_name": None,
                    "primary": None,
                    "n_members": 0,
                    "members": "(not in map)",
                }
            )
    return pd.DataFrame(rows)


SPOT_SYMBOLS = (
    "SAP.XETRA",
    "HSBA.LSE",
    "ASML.AS",
    "SIE.XETRA",
    "NESN.SW",
    "MC.PA",
    "VOD.LSE",
    "AAPL.US",
    "APC.F",
)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def etf_symbols(root: Path = EODHD_RAW_ROOT) -> set[str]:
    """Every ticker.exchange in the ETF universes (never an issuer parent/child)."""
    out: set[str] = set()
    for lane, fname, code_col, exch_col, default in (
        ("us_etf", "tickers_US_ETF.parquet", "Code", None, "US"),
        ("uk_eu_etf", "tickers_UK_EU_ETF.parquet", "Code", "source_exchange", None),
    ):
        path = root / lane / fname
        if not path.exists():
            continue
        try:
            df = pd.read_parquet(path)
        except Exception:
            continue
        if code_col not in df.columns:
            continue
        exch = df[exch_col] if exch_col and exch_col in df.columns else default
        codes = df[code_col].astype(str).str.upper()
        if isinstance(exch, str):
            out |= {f"{c}.{exch}" for c in codes}
        else:
            out |= {
                f"{c}.{str(e).upper()}"
                for c, e in zip(codes, exch)
                if isinstance(e, str)
            }
    return out


def universe_symbols(root: Path = EODHD_RAW_ROOT) -> set[str]:
    """Every (ticker.exchange) with a price sidecar in any lane."""
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


def build(
    *,
    root: Path = EODHD_RAW_ROOT,
    articles_dir: Path = ARTICLES_DIR,
    map_path: Path = MAP_PATH,
    days: int = 365,
    min_p: float = DEFAULT_MIN_P,
    min_n: int = DEFAULT_MIN_N,
    cotag: bool = True,
    today: date | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    import duckdb

    t0 = time.perf_counter()
    records = vendor_records(root)
    log.info(
        "vendor keys: %d cached firms (%.0fs)", len(records), time.perf_counter() - t0
    )
    uf = UnionFind()
    evidence = link_vendor(records, uf)
    cotag_links: dict[str, tuple[float, int]] = {}
    counts: dict[str, int] = {}
    if cotag and articles_dir.is_dir() and any(articles_dir.glob("*.parquet")):
        con = duckdb.connect()
        since = ((today or date.today()) - timedelta(days=days)).isoformat()
        cands = cotag_candidates(
            con,
            (articles_dir / "*.parquet").as_posix(),
            since=since,
            min_p=min_p,
            min_n=min_n,
        )
        counts = symbol_counts(con)
        # vendor identity per symbol (LEI, else ISIN); link_cotag lifts it to components
        identity = {
            r["symbol"]: (r["lei"] or r["isin"])
            for r in records
            if (r["lei"] or r["isin"])
        }
        cotag_links, rejected = link_cotag(
            cands, uf, evidence, identity=identity, exclude=etf_symbols(root)
        )
        log.info(
            "co-tag links: %d accepted, %d rejected by guards (since %s, p>=%.2f, n>=%d; %.0fs)",
            len(cotag_links),
            rejected,
            since,
            min_p,
            min_n,
            time.perf_counter() - t0,
        )
    from datetime import datetime, timezone

    built_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    df = assemble(
        uf,
        records,
        evidence,
        cotag_links,
        counts,
        universe_symbols(root),
        built_at=built_at,
    )
    table = pa.Table.from_pandas(df, schema=MAP_SCHEMA, preserve_index=False)
    map_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic.write_table(table, map_path, compression="zstd")
    summary = qc_summary(df, records)
    log.info(
        "wrote %s: %d symbols, %d issuers (%.0fs)",
        map_path.name,
        summary["symbols"],
        summary["issuers"],
        time.perf_counter() - t0,
    )
    return df, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build issuer_map.parquet: symbol line -> issuer (vendor keys + corpus co-tagging)"
    )
    parser.add_argument(
        "--days",
        type=int,
        default=365,
        help="Corpus window for co-tagging (default 365)",
    )
    parser.add_argument(
        "--min-p",
        type=float,
        default=DEFAULT_MIN_P,
        help=f"Accept a co-tag link when P(parent|sym) >= this (default {DEFAULT_MIN_P})",
    )
    parser.add_argument(
        "--min-n",
        type=int,
        default=DEFAULT_MIN_N,
        help=f"...and the symbol has at least this many tagged articles (default {DEFAULT_MIN_N})",
    )
    parser.add_argument(
        "--no-cotag", action="store_true", help="Vendor keys only (no corpus links)"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df, summary = build(
        days=args.days, min_p=args.min_p, min_n=args.min_n, cotag=not args.no_cotag
    )
    print(json.dumps(summary, indent=2))
    print(spot_check(df, SPOT_SYMBOLS).to_string(index=False))


if __name__ == "__main__":
    main()
