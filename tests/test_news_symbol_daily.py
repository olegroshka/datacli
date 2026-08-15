from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import build_news_symbol_daily as bnd  # type: ignore  # noqa: E402
import eodhd_datasets as reg  # type: ignore  # noqa: E402
import fetch_eodhd_news as news  # type: ignore  # noqa: E402
import schema as sch  # type: ignore  # noqa: E402

FETCHED = "2026-08-15T10:00:00+00:00"


def _article(
    aid: str, when: str, symbols: list[str], pol: float, source: str = "https://x.com/a"
) -> dict:
    return {
        "date": when,
        "title": f"T {aid}",
        "content": "body " * 50,
        "link": f"{source}/{aid}",
        "symbols": symbols,
        "tags": [],
        "sentiment": {"polarity": pol, "neg": 0.0, "neu": 1.0, "pos": 0.0},
    }


def _write_day(articles_dir: Path, day: str, raw: list[dict]) -> Path:
    df = news.normalize_articles(raw, fetched_at=FETCHED)
    path = news.partition_path(day, articles_dir)
    news.write_partition(df, path)
    return path


def test_build_days_aggregates_per_symbol(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    adir = tmp_path / "articles"
    _write_day(
        adir,
        "2026-08-13",
        [
            _article("a1", "2026-08-13T09:00:00+00:00", ["AAPL.US", "MSFT.US"], 0.8),
            _article(
                "a2",
                "2026-08-13T15:00:00+00:00",
                ["AAPL.US"],
                -0.6,
                source="https://y.com/b",
            ),
            _article(
                "a3",
                "2026-08-13T16:00:00+00:00",
                ["AAPL.US", "B.US", "C.US", "D.US"],
                0.0,
            ),
            # re-publication of a1 later the same day: only the latest counts
            _article("a1", "2026-08-13T18:00:00+00:00", ["AAPL.US", "MSFT.US"], 0.8),
        ],
    )
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    df = bnd.build_days(
        con, (adir / "*.parquet").as_posix(), ["2026-08-13"], built_at=FETCHED
    )
    by = df.set_index("symbol")
    assert set(by.index) == {"AAPL.US", "MSFT.US", "B.US", "C.US", "D.US"}
    aapl = by.loc["AAPL.US"]
    assert aapl["ticker"] == "AAPL" and aapl["exchange"] == "US"
    assert aapl["n_articles"] == 3 and aapl["n_articles_day"] == 3
    assert aapl["share_of_day"] == pytest.approx(1.0)
    assert aapl["n_solo"] == 2  # a3 has 4 tags -> not solo
    assert aapl["n_sources"] == 2
    assert aapl["polarity_mean"] == pytest.approx((0.8 - 0.6 + 0.0) / 3)
    assert aapl["pos_share"] == pytest.approx(1 / 3) and aapl[
        "neg_share"
    ] == pytest.approx(1 / 3)
    # a1's 09:00 version is superseded by its 18:00 re-publication -> a2 at 15:00 is first
    assert str(aapl["first_published_at"])[:19] == "2026-08-13 15:00:00"
    assert str(aapl["last_published_at"])[:19] == "2026-08-13 18:00:00"
    assert by.loc["MSFT.US", "share_of_day"] == pytest.approx(1 / 3)
    assert bnd.build_days(
        con, (adir / "*.parquet").as_posix(), [], built_at=FETCHED
    ).empty


def test_days_to_rebuild_incremental_rules() -> None:
    parts = {"2026-08-11": 100.0, "2026-08-12": 100.0, "2026-08-13": 300.0}
    # no panel yet -> everything
    assert bnd.days_to_rebuild(parts, set(), None) == [
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
    ]
    # panel built at t=200: 13 is newer than the panel, 11/12 present and older -> only 13
    assert bnd.days_to_rebuild(
        parts, {"2026-08-11", "2026-08-12", "2026-08-13"}, 200.0
    ) == ["2026-08-13"]
    # a day missing from the panel is due even if its partition is old
    assert bnd.days_to_rebuild(parts, {"2026-08-11"}, 400.0) == [
        "2026-08-12",
        "2026-08-13",
    ]
    assert (
        bnd.days_to_rebuild(parts, {"2026-08-11", "2026-08-12", "2026-08-13"}, 400.0)
        == []
    )
    assert bnd.days_to_rebuild(parts, set(), 400.0, full=True, since="2026-08-12") == [
        "2026-08-12",
        "2026-08-13",
    ]


def test_build_end_to_end_incremental(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    adir = tmp_path / "articles"
    panel = tmp_path / "news_symbol_daily.parquet"
    _write_day(
        adir,
        "2026-08-12",
        [_article("b1", "2026-08-12T10:00:00+00:00", ["MSFT.US"], 0.5)],
    )
    _write_day(
        adir,
        "2026-08-13",
        [_article("a1", "2026-08-13T10:00:00+00:00", ["AAPL.US"], 0.5)],
    )
    out = bnd.build(articles_dir=adir, panel_path=panel)
    assert out["days_rebuilt"] == 2 and out["rows"] == 2
    back = pd.read_parquet(panel)
    assert set(back["symbol"]) == {"MSFT.US", "AAPL.US"}
    # nothing changed -> nothing rebuilt
    assert bnd.build(articles_dir=adir, panel_path=panel)["days_rebuilt"] == 0
    # a re-crawled day (newer partition) is rebuilt and replaces its rows
    time.sleep(0.05)
    p = _write_day(
        adir,
        "2026-08-13",
        [_article("a1", "2026-08-13T10:00:00+00:00", ["AAPL.US", "NVDA.US"], 0.5)],
    )
    os.utime(p, None)
    future = time.time() + 5
    os.utime(p, (future, future))
    out2 = bnd.build(articles_dir=adir, panel_path=panel)
    assert out2["days_rebuilt"] == 1
    back2 = pd.read_parquet(panel)
    assert set(back2["symbol"]) == {"MSFT.US", "AAPL.US", "NVDA.US"} and len(back2) == 3
    # empty articles dir -> no crash
    assert (
        bnd.build(articles_dir=tmp_path / "none", panel_path=tmp_path / "p.parquet")[
            "rows"
        ]
        == 0
    )


def test_registry_and_schema_wiring() -> None:
    ds = next(d for d in reg.LANES["news"].datasets if d.kind == "news_daily")
    assert (
        ds.ticker_keyed
        and ds.state is None
        and ds.output == "news_symbol_daily.parquet"
    )
    assert ds.fetcher == "build_news_symbol_daily.py"
    assert "news_daily" in sch.datasets()
    assert {"ticker", "exchange", "date", "share_of_day"} <= set(
        sch.canonical_columns("news_daily")
    )
    import cli  # type: ignore

    assert "news_daily" in cli.DEFAULT_KINDS and "news_daily" in cli.KNOWN_KINDS
    # refresh plans it after the news top-up, with no passthrough flags
    plan = cli.build_refresh_plan(
        ["news"],
        kinds=set(cli.DEFAULT_KINDS),
        with_universe=False,
        passthrough=["--to", "x"],
    )
    assert [(s.kind, s.script, s.args) for s in plan] == [
        (
            "news",
            "fetch_eodhd_news.py",
            ["--limit-days", str(reg.NEWS_REFRESH_MAX_DAYS)],
        ),
        ("news_daily", "build_news_symbol_daily.py", []),
        ("issuer_map", "build_issuer_map.py", []),
        ("news_issuer_daily", "build_news_issuer_daily.py", []),
    ]
