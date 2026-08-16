from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_news as news  # type: ignore  # noqa: E402
import report_news_quality as rq  # type: ignore  # noqa: E402

FETCHED = "2026-08-15T10:00:00+00:00"


def _art(aid, when, symbols, content="body " * 60, pol=0.5, link=None):
    return {
        "date": when,
        "title": aid,
        "content": content,
        "link": link or f"https://x.com/{aid}",
        "symbols": symbols,
        "tags": ["T"],
        "sentiment": {"polarity": pol, "neg": 0, "neu": 1, "pos": 0},
    }


def _corpus(tmp_path: Path) -> tuple[Path, Path]:
    adir = tmp_path / "articles"
    # day 1: 300 articles, 60 tag NVDA (20% -> burst), one short, one junk symbol, one untagged
    d1 = [
        _art(
            f"a{i}",
            "2026-08-10T10:00:00+00:00",
            ["NVDA.US"] if i < 60 else [f"X{i % 40}.US"],
        )
        for i in range(300)
    ]
    d1[0]["content"] = "tiny"
    d1[1]["symbols"] = ["USDUSD.FOREX", "Y.US"]
    d1[2]["symbols"] = []
    news.write_partition(
        news.normalize_articles(d1, fetched_at=FETCHED),
        news.partition_path("2026-08-10", adir),
    )
    # day 3 (day 2 missing -> crawl gap); republication of a0 with same link on a later day
    d3 = [_art(f"b{i}", "2026-08-12T10:00:00+00:00", ["Z.US"]) for i in range(10)]
    d3.append(
        _art("a0", "2026-08-12T11:00:00+00:00", ["NVDA.US"], link="https://x.com/a0")
    )
    news.write_partition(
        news.normalize_articles(d3, fetched_at=FETCHED),
        news.partition_path("2026-08-12", adir),
    )
    state = tmp_path / "news_fetch_state.csv"
    pd.DataFrame(
        {
            "date": ["2026-08-10", "2026-08-12"],
            "status": ["ok", "http_500"],
            "detail": ["", "boom"],
        }
    ).to_csv(state, index=False)
    return adir, state


def test_report_and_flags(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    adir, state = _corpus(tmp_path)
    con = duckdb.connect()
    rep = rq.run_report(con, (adir / "*.parquet").as_posix(), state)
    t = rep["totals"]
    assert t["articles"] == 310 and t["days"] == 2 and t["republished_articles"] == 1
    assert t["empty_content"] == 1 and t["untagged"] == 1
    assert rep["missing_days"] == {"count": 1, "sample": ["2026-08-11"]}
    assert (
        rep["state"]["not_ok"] == 1
        and rep["state"]["sample"][0]["status"] == "http_500"
    )
    assert rep["junk_symbols"][0]["symbol"] == "USDUSD.FOREX"
    assert (
        rep["bursts"]
        and rep["bursts"][0]["symbol"] == "NVDA.US"
        and rep["bursts"][0]["share"] == pytest.approx(58 / 300, abs=1e-3)
    )
    assert [y["year"] for y in rep["by_year"]] == [2026]
    codes = [c for _, c, _ in rq.flags(rep)]
    assert codes[:2] == ["crawl_gap", "crawl_state"]
    assert {"junk_symbols", "tagging_burst", "republications"} <= set(codes)
    # a windowed report drops the earlier day
    rep2 = rq.run_report(
        con, (adir / "*.parquet").as_posix(), state, since="2026-08-12"
    )
    assert rep2["totals"]["articles"] == 11 and rep2["missing_days"]["count"] == 0


def test_qc_news_routes_to_the_hygiene_report(monkeypatch) -> None:
    import cli  # type: ignore

    calls: list = []
    monkeypatch.setattr(
        cli,
        "delegate",
        lambda script, argv, **kw: calls.append((script, argv, kw)) or 0,
    )
    assert cli.cmd_qc(["news", "--all"]) == 0
    assert calls[-1][0] == cli.NEWS_QC_SCRIPT and calls[-1][1] == ["--all"]
    assert cli.cmd_qc(["us_common"]) == 0
    assert calls[-1][0] == cli.QC_SCRIPT and calls[-1][1] == ["--lane", "us_common"]
    assert cli.cmd_qc(["news", "prices"]) == 2  # no dataset drill-down for news
