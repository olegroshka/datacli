from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_news as news  # type: ignore  # noqa: E402

FETCHED_AT = "2026-08-15T10:00:00+00:00"


def _raw(link: str, *, when: str = "2026-08-13T16:13:00+00:00", **over) -> dict:
    item = {
        "date": when,
        "title": "Some headline",
        "content": "Full text of the article.",
        "link": link,
        "symbols": ["AAPL.US", "APC.F"],
        "tags": ["EARNINGS", "TECH"],
        "sentiment": {"polarity": 0.9, "neg": 0.02, "neu": 0.9, "pos": 0.08},
    }
    item.update(over)
    return item


# --------------------------------------------------------------------------- #
# identity / normalisation
# --------------------------------------------------------------------------- #
def test_article_id_is_stable_and_link_derived() -> None:
    a = news.article_id("https://x.test/a")
    assert a == news.article_id("  https://x.test/a ")
    assert len(a) == 16
    assert a != news.article_id("https://x.test/b")


def test_normalize_articles_shapes_schema_and_drops_linkless() -> None:
    raw = [
        _raw("https://finance.yahoo.com/news/a"),
        _raw("https://www.nasdaq.com/b", tags=None, symbols="MSFT.US", sentiment=None),
        {"date": "2026-08-13T00:00:00+00:00", "title": "no link"},
        "garbage",
    ]
    df = news.normalize_articles(raw, fetched_at=FETCHED_AT)
    assert list(df.columns) == news.ARTICLE_COLUMNS
    assert len(df) == 2
    first = df.iloc[0]
    assert first["source"] == "finance.yahoo.com"
    assert first["date"] == date(2026, 8, 13)
    assert first["published_at"].tzinfo is not None
    assert first["symbols"] == ["AAPL.US", "APC.F"]
    assert first["polarity"] == 0.9
    second = df.iloc[1]
    assert second["tags"] == []
    assert second["symbols"] == ["MSFT.US"]
    assert pd.isna(second["polarity"])


def test_normalize_articles_empty() -> None:
    df = news.normalize_articles([], fetched_at=FETCHED_AT)
    assert df.empty
    assert list(df.columns) == news.ARTICLE_COLUMNS


# --------------------------------------------------------------------------- #
# merge / partition round-trip
# --------------------------------------------------------------------------- #
def test_merge_articles_upserts_on_article_id_last_wins() -> None:
    old = news.normalize_articles(
        [_raw("https://x.test/a", title="old"), _raw("https://x.test/b")],
        fetched_at=FETCHED_AT,
    )
    new = news.normalize_articles(
        [_raw("https://x.test/a", title="new"), _raw("https://x.test/c")],
        fetched_at=FETCHED_AT,
    )
    merged = news.merge_articles(old, new)
    assert len(merged) == 3
    assert merged.set_index("link").loc["https://x.test/a", "title"] == "new"
    assert news.merge_articles(None, new).equals(
        new.sort_values(["published_at", "article_id"]).reset_index(drop=True)
    )


def test_partition_key_and_path(tmp_path: Path) -> None:
    assert news.partition_key(date(2026, 8, 13)) == "2026-08-13"
    assert news.partition_key("2021-01-05") == "2021-01-05"
    assert (
        news.partition_path("2026-08-13", tmp_path) == tmp_path / "2026-08-13.parquet"
    )


def test_normalize_articles_falls_back_to_crawl_day_for_bad_timestamp() -> None:
    df = news.normalize_articles(
        [_raw("https://x.test/a", when="not-a-date"), _raw("https://x.test/b")],
        fetched_at=FETCHED_AT,
        crawl_day=date(2026, 8, 13),
    )
    assert df["date"].tolist() == [date(2026, 8, 13), date(2026, 8, 13)]
    assert pd.isna(df.iloc[0]["published_at"])
    # without a crawl day the bad row keeps a null date (caller's problem)
    df2 = news.normalize_articles(
        [_raw("https://x.test/a", when="not-a-date")], fetched_at=FETCHED_AT
    )
    assert pd.isna(df2.iloc[0]["date"])


def test_write_partition_pins_schema_even_for_empty_lists(tmp_path: Path) -> None:
    """An all-empty tags month must still be list<string>, not list<null>, so a
    glob read across partitions has one schema."""
    df = news.normalize_articles(
        [_raw("https://x.test/a", tags=[]), _raw("https://x.test/b", tags=None)],
        fetched_at=FETCHED_AT,
    )
    path = tmp_path / "2026-08.parquet"
    news.write_partition(df, path)
    schema = pq.read_schema(path)
    assert schema.equals(news.ARTICLE_SCHEMA)
    back = news.read_partition(path)
    assert back is not None and len(back) == 2
    # round-trip: re-merge what we read back and rewrite without error
    again = news.merge_articles(back, df)
    news.write_partition(again, path)
    assert pq.read_schema(path).equals(news.ARTICLE_SCHEMA)
    assert news.read_partition(tmp_path / "missing.parquet") is None


# --------------------------------------------------------------------------- #
# planning / state
# --------------------------------------------------------------------------- #
def _state(rows: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"date": d, "status": s, "fetched_at": FETCHED_AT} for d, s in rows]
    )


def test_plan_days_skips_ok_days_but_recrawls_overlap() -> None:
    lookup = news.build_state_lookup(
        _state(
            [
                ("2026-08-10", "ok"),
                ("2026-08-11", "http_500"),
                ("2026-08-12", "ok"),
                ("2026-08-13", "ok"),
                ("2026-08-14", "ok"),
            ]
        )
    )
    days = news.plan_days(
        from_date="2026-08-09",
        to_date="2026-08-15",
        state_lookup=lookup,
        overlap_days=2,
        full_refresh=False,
    )
    # newest first: a --limit-days cap always takes the freshest pending days
    assert [d.isoformat() for d in days] == [
        "2026-08-15",  # overlap window: to - 2 .. to
        "2026-08-14",
        "2026-08-13",
        "2026-08-11",  # failed
        "2026-08-09",  # never crawled
    ]


def test_plan_days_full_refresh_and_empty_window() -> None:
    lookup = news.build_state_lookup(_state([("2026-08-10", "ok")]))
    days = news.plan_days(
        from_date="2026-08-10",
        to_date="2026-08-11",
        state_lookup=lookup,
        overlap_days=0,
        full_refresh=True,
    )
    assert [d.isoformat() for d in days] == ["2026-08-11", "2026-08-10"]
    assert (
        news.plan_days(
            from_date="2026-08-12",
            to_date="2026-08-11",
            state_lookup={},
            overlap_days=2,
            full_refresh=False,
        )
        == []
    )


def test_build_state_lookup_handles_missing() -> None:
    assert news.build_state_lookup(None) == {}
    assert news.build_state_lookup(pd.DataFrame()) == {}
    assert news.build_state_lookup(pd.DataFrame({"foo": [1]})) == {}


def test_merge_state_rows_last_write_wins_sorted() -> None:
    existing = _state([("2026-08-12", "http_500"), ("2026-08-10", "ok")])
    merged = news.merge_state_rows(
        existing,
        [
            {"date": "2026-08-12", "status": "ok", "pages": 3},
            {"date": "2026-08-11", "status": "ok", "pages": 3},
        ],
    )
    assert merged["date"].tolist() == ["2026-08-10", "2026-08-11", "2026-08-12"]
    assert merged.set_index("date").loc["2026-08-12", "status"] == "ok"
    assert set(news.STATE_COLUMNS).issubset(merged.columns)


# --------------------------------------------------------------------------- #
# crawl loop (HTTP stubbed)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status_code: int, payload=None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _Session:
    """Serves pages from a dict of offset -> response; records calls."""

    def __init__(self, pages: dict[int, _Resp]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        return self.pages.get(params["offset"], _Resp(200, []))


def test_fetch_day_paginates_until_short_page(monkeypatch) -> None:
    monkeypatch.setattr(news, "DELAY", 0)
    session = _Session(
        {
            0: _Resp(200, [_raw(f"https://x.test/{i}") for i in range(3)]),
            3: _Resp(200, [_raw(f"https://x.test/{i}") for i in range(3, 5)]),
        }
    )
    rows, pages, status, detail = news.fetch_day(
        session, date(2026, 8, 13), page_size=3, max_pages=10
    )
    assert (len(rows), pages, status, detail) == (5, 2, "ok", "")
    assert [c["offset"] for c in session.calls] == [0, 3]
    assert session.calls[0]["from"] == session.calls[0]["to"] == "2026-08-13"


def test_fetch_day_reports_empty_and_failure(monkeypatch) -> None:
    monkeypatch.setattr(news, "DELAY", 0)
    monkeypatch.setattr(news, "RETRY_SLEEP", 0)
    rows, pages, status, _ = news.fetch_day(_Session({}), date(2026, 8, 13))
    assert (rows, pages, status) == ([], 1, "empty")

    failing = _Session(
        {
            0: _Resp(200, [_raw(f"https://x.test/{i}") for i in range(2)]),
            2: _Resp(403, None, "forbidden"),
        }
    )
    rows, pages, status, detail = news.fetch_day(
        failing, date(2026, 8, 13), page_size=2
    )
    assert len(rows) == 2 and pages == 1
    assert status == "http_403" and detail == "forbidden"


def test_get_page_retries_on_429(monkeypatch) -> None:
    monkeypatch.setattr(news, "RETRY_SLEEP", 0)

    class _Flaky(_Session):
        def __init__(self) -> None:
            super().__init__({})
            self.n = 0

        def get(self, url, params=None, timeout=None):
            self.n += 1
            if self.n < 3:
                return _Resp(429, None, "slow down")
            return _Resp(200, [_raw("https://x.test/a")])

    session = _Flaky()
    data, status, _ = news._get_page(
        session, date(2026, 8, 13), offset=0, page_size=1000
    )
    assert status == "ok" and data is not None and len(data) == 1
    assert session.n == 3
