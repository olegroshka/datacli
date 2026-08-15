from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import build_issuer_map as bim  # type: ignore  # noqa: E402
import build_news_issuer_daily as bnid  # type: ignore  # noqa: E402
import build_news_symbol_daily as bnd  # type: ignore  # noqa: E402
import eodhd_datasets as reg  # type: ignore  # noqa: E402
import fetch_eodhd_news as news  # type: ignore  # noqa: E402

FETCHED = "2026-08-15T10:00:00+00:00"


def _cache_firm(root: Path, lane: str, exch: str, code: str, general: dict) -> None:
    d = root / lane / "cache" / "fundamentals" / exch
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / f"{code}.json.gz", "wt", encoding="utf-8") as fh:
        json.dump({"General": general}, fh)


def _fake_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    _cache_firm(
        root,
        "us_common",
        "US",
        "AAPL",
        {
            "Name": "Apple Inc.",
            "ISIN": "US0378331005",
            "LEI": "LEI-AAPL",
            "PrimaryTicker": "AAPL.US",
            "Listings": {"0": {"Code": "0R2V", "Exchange": "LSE", "Name": "Apple"}},
        },
    )
    _cache_firm(
        root,
        "uk_eu",
        "XETRA",
        "SAP",
        {
            "Name": "SAP SE",
            "ISIN": "DE0007164600",
            "LEI": "LEI-SAP",
            "PrimaryTicker": "SAP.F",
            "Listings": {},
        },
    )
    _cache_firm(
        root,
        "uk_eu",
        "LSE",
        "HSBA",
        {
            "Name": "HSBC Holdings PLC",
            "ISIN": "GB0005405286",
            "LEI": "LEI-HSBC",
            "PrimaryTicker": "HSBA.LSE",
            "Listings": {"0": {"Code": "HSBC", "Exchange": "US", "Name": "HSBC ADR"}},
        },
    )
    _cache_firm(
        root,
        "uk_eu",
        "LSE",
        "EMG",
        {
            "Name": "Man Group PLC",
            "ISIN": "JE00BJ1DLW90",
            "LEI": "LEI-MAN",
            "PrimaryTicker": "EMG.LSE",
        },
    )
    _cache_firm(
        root,
        "uk_eu",
        "LSE",
        "BSIF",
        {
            "Name": "Bluefield Solar",
            "ISIN": "GG00B4LR8T09",
            "LEI": "LEI-BSIF",
            "PrimaryTicker": "BSIF.LSE",
        },
    )
    # same ISIN listed twice (two exchanges) -> same issuer
    _cache_firm(
        root,
        "uk_eu",
        "ST",
        "NDA-SE",
        {"Name": "Nordea", "ISIN": "FI4000297767", "LEI": None, "PrimaryTicker": None},
    )
    _cache_firm(
        root,
        "uk_eu",
        "HE",
        "NDA-FI",
        {"Name": "Nordea", "ISIN": "FI4000297767", "LEI": None, "PrimaryTicker": None},
    )
    # price universe sidecars
    for lane, rows in (
        ("us_common", [("AAPL", "US")]),
        ("uk_eu", [("SAP", "XETRA"), ("HSBA", "LSE"), ("EMG", "LSE"), ("BSIF", "LSE")]),
    ):
        (root / lane).mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows, columns=["ticker", "exchange"]).to_csv(
            root / lane / "prices_fetch_state.csv", index=False
        )
    return root


def _article(aid: str, when: str, symbols: list[str], pol: float = 0.5) -> dict:
    return {
        "date": when,
        "title": f"T {aid}",
        "content": "body " * 50,
        "link": f"https://x.com/{aid}",
        "symbols": symbols,
        "tags": [],
        "sentiment": {"polarity": pol, "neg": 0.0, "neu": 1.0, "pos": 0.0},
    }


def _corpus(root: Path) -> Path:
    adir = root / "news" / "articles"
    raw = []
    # 40 Apple articles tagging AAPL.US and its Frankfurt mirror APC.F -> APC.F is a child of AAPL.US
    for i in range(40):
        raw.append(
            _article(f"ap{i}", "2026-08-13T10:00:00+00:00", ["AAPL.US", "APC.F"], 0.8)
        )
    # SAP.US alone 30x, plus 32 articles tagging both SAP.XETRA and SAP.US -> SAP.XETRA child of SAP.US
    for i in range(30):
        raw.append(_article(f"su{i}", "2026-08-13T11:00:00+00:00", ["SAP.US"], 0.5))
    for i in range(32):
        raw.append(
            _article(
                f"sx{i}", "2026-08-13T12:00:00+00:00", ["SAP.XETRA", "SAP.US"], 0.5
            )
        )
    # peer contamination: 35 UK round-ups tag both BSIF.LSE and EMG.LSE (different LEIs) -> must NOT merge
    for i in range(35):
        raw.append(
            _article(
                f"uk{i}", "2026-08-13T13:00:00+00:00", ["EMG.LSE", "BSIF.LSE"], -0.2
            )
        )
    for i in range(50):
        raw.append(_article(f"em{i}", "2026-08-13T14:00:00+00:00", ["EMG.LSE"], 0.1))
    df = news.normalize_articles(raw, fetched_at=FETCHED)
    news.write_partition(df, news.partition_path("2026-08-13", adir))
    return adir


def test_union_find_and_vendor_links() -> None:
    uf = bim.UnionFind()
    recs = [
        {
            "symbol": "A.US",
            "lane": "us_common",
            "name": "A",
            "isin": "I1",
            "lei": "L1",
            "primary": "A.US",
            "listings": ["A.LSE"],
        },
        {
            "symbol": "B.XETRA",
            "lane": "uk_eu",
            "name": "B",
            "isin": "I2",
            "lei": "L2",
            "primary": "B.F",
            "listings": [],
        },
        {
            "symbol": "C.ST",
            "lane": "uk_eu",
            "name": "C",
            "isin": "I3",
            "lei": None,
            "primary": None,
            "listings": [],
        },
        {
            "symbol": "C.HE",
            "lane": "uk_eu",
            "name": "C",
            "isin": "I3",
            "lei": None,
            "primary": None,
            "listings": [],
        },
    ]
    ev = bim.link_vendor(recs, uf)
    groups = {frozenset(g) for g in uf.groups().values()}
    assert frozenset({"A.US", "A.LSE"}) in groups
    assert frozenset({"B.XETRA", "B.F"}) in groups
    assert frozenset({"C.ST", "C.HE"}) in groups
    assert (
        ev["A.LSE"] == "listing" and ev["B.XETRA"] == "vendor" and ev["C.HE"] == "isin"
    )


def test_build_issuer_map_end_to_end_with_guards(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    root = _fake_root(tmp_path)
    adir = _corpus(root)
    from datetime import date

    df, summary = bim.build(
        root=root,
        articles_dir=adir,
        map_path=root / "news" / "issuer_map.parquet",
        days=30,
        min_p=0.9,
        min_n=30,
        today=date(2026, 8, 15),
    )
    by = df.set_index("symbol")
    # vendor: AAPL <-> 0R2V.LSE (listing); HSBA <-> HSBC.US (listing); SAP.XETRA <-> SAP.F (primary); Nordea by ISIN
    assert by.loc["0R2V.LSE", "issuer_id"] == "LEI-AAPL"
    assert (
        by.loc["HSBC.US", "issuer_id"] == "LEI-HSBC"
        and by.loc["HSBC.US", "evidence"] == "listing"
    )
    assert (
        by.loc["NDA-SE.ST", "issuer_id"]
        == by.loc["NDA-FI.HE", "issuer_id"]
        == "FI4000297767"
    )
    # co-tag: APC.F -> AAPL.US, SAP.XETRA/SAP.US joined via SAP.US parent (p = 1.0)
    assert (
        by.loc["APC.F", "issuer_id"] == "LEI-AAPL"
        and by.loc["APC.F", "evidence"] == "cotag"
    )
    assert (
        by.loc["APC.F", "cotag_p"] == pytest.approx(1.0)
        and by.loc["APC.F", "cotag_n"] == 40
    )
    assert (
        by.loc["SAP.US", "issuer_id"] == "LEI-SAP"
        and by.loc["SAP.XETRA", "issuer_id"] == "LEI-SAP"
    )
    # peer contamination blocked: BSIF and Man Group keep different issuers
    assert (
        by.loc["BSIF.LSE", "issuer_id"] == "LEI-BSIF"
        and by.loc["EMG.LSE", "issuer_id"] == "LEI-MAN"
    )
    assert summary["lei_collisions"] == 0
    assert (
        by.loc["AAPL.US", "in_universe"]
        and by.loc["AAPL.US", "primary_symbol"] == "AAPL.US"
    )
    assert by.loc["SAP.XETRA", "issuer_name"] == "SAP SE"
    spot = bim.spot_check(df, ["SAP.XETRA", "NOPE.US"])
    assert spot.iloc[0]["n_members"] >= 3 and spot.iloc[1]["members"] == "(not in map)"


def test_news_issuer_daily_counts_once_per_issuer(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    root = _fake_root(tmp_path)
    adir = _corpus(root)
    from datetime import date

    map_path = root / "news" / "issuer_map.parquet"
    bim.build(
        root=root,
        articles_dir=adir,
        map_path=map_path,
        days=30,
        today=date(2026, 8, 15),
    )
    panel = root / "news" / "news_issuer_daily.parquet"
    out = bnid.build(root=root, articles_dir=adir, map_path=map_path, panel_path=panel)
    assert out["days_rebuilt"] == 1
    df = pd.read_parquet(panel).set_index(["ticker", "exchange"])
    # SAP.XETRA row carries the issuer's whole flow: 30 SAP.US-only + 32 both = 62 articles, counted once each
    sap = df.loc[("SAP", "XETRA")]
    assert (
        sap["n_articles"] == 62
        and sap["issuer_id"] == "LEI-SAP"
        and sap["n_symbols"] == 2
    )
    # Apple: 40 articles, each tagging 2 lines of the issuer -> still 40
    aapl = df.loc[("AAPL", "US")]
    assert aapl["n_articles"] == 40 and aapl["polarity_mean"] == pytest.approx(0.8)
    # Man Group: 35 round-ups + 50 own = 85; Bluefield 35 (not merged)
    assert (
        df.loc[("EMG", "LSE"), "n_articles"] == 85
        and df.loc[("BSIF", "LSE"), "n_articles"] == 35
    )
    # symbol panel for comparison: SAP.XETRA alone sees only 32
    bnd.build(articles_dir=adir, panel_path=root / "news" / "news_symbol_daily.parquet")
    sym = pd.read_parquet(root / "news" / "news_symbol_daily.parquet").set_index(
        "symbol"
    )
    assert sym.loc["SAP.XETRA", "n_articles"] == 32
    # incremental: nothing new -> nothing rebuilt; a newer map forces a full rebuild
    assert (
        bnid.build(root=root, articles_dir=adir, map_path=map_path, panel_path=panel)[
            "days_rebuilt"
        ]
        == 0
    )
    import os, time

    future = time.time() + 5
    os.utime(map_path, (future, future))
    assert (
        bnid.build(root=root, articles_dir=adir, map_path=map_path, panel_path=panel)[
            "days_rebuilt"
        ]
        == 1
    )


def test_registry_wiring_issuer() -> None:
    kinds = [d.kind for d in reg.LANES["news"].datasets]
    assert kinds[:4] == ["news", "news_daily", "issuer_map", "news_issuer_daily"]
    im = next(d for d in reg.LANES["news"].datasets if d.kind == "issuer_map")
    nid = next(d for d in reg.LANES["news"].datasets if d.kind == "news_issuer_daily")
    assert im.local and not im.ticker_keyed and im.fetcher == "build_issuer_map.py"
    assert (
        nid.local and nid.ticker_keyed and nid.fetcher == "build_news_issuer_daily.py"
    )
    import cli  # type: ignore
    import schema as sch  # type: ignore

    assert {"issuer_map", "news_issuer_daily"} <= set(cli.DEFAULT_KINDS)
    assert {"issuer_map", "news_issuer_daily"} <= set(sch.datasets())
