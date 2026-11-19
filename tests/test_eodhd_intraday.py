"""The intraday lane: universe build, 1m fetcher (fake API), registry, CLI, scheduler, QC."""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = _REPO_ROOT / "eodhd"
for path in (_REPO_ROOT, _EODHD):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import build_intraday_universe as uni  # noqa: E402
import eodhd_datasets as reg  # noqa: E402
import fetch_eodhd_intraday as fx  # noqa: E402

T1 = uni.Tier("T1", 2, 3, "trade")
T2 = uni.Tier("T2", 4, 5, "breadth")


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #
def test_quarter_helpers() -> None:
    assert uni.quarter_starts(date(2016, 2, 10), date(2016, 11, 13)) == [
        date(2016, 1, 1),
        date(2016, 4, 1),
        date(2016, 7, 1),
        date(2016, 10, 1),
    ]
    assert uni.quarter_end(date(2016, 10, 1)) == date(2016, 12, 31)
    assert uni.quarter_end(date(2016, 1, 1)) == date(2016, 3, 31)


def _ranks(rows: list[tuple[date, list[str]]]) -> pd.DataFrame:
    out = []
    for q, order in rows:
        for i, t in enumerate(order, 1):
            out.append(
                {
                    "quarter_start": q,
                    "ticker": t,
                    "exchange": "US",
                    "adv_rank": i,
                    "adv_musd": 100.0 / i,
                }
            )
    return pd.DataFrame(out)


def test_assign_tiers_applies_hysteresis() -> None:
    q1, q2 = date(2020, 1, 1), date(2020, 4, 1)
    ranks = _ranks(
        [(q1, ["A", "B", "C", "D", "E", "F"]), (q2, ["C", "D", "B", "E", "A", "F"])]
    )
    got = uni.assign_tiers(ranks, [T1, T2])
    tiers = {(r.quarter_start, r.ticker): r.tier for r in got.itertuples()}
    assert {t for (q, t), tier in tiers.items() if q == q1 and tier == "T1"} == {
        "A",
        "B",
    }
    assert {t for (q, t), tier in tiers.items() if q == q1 and tier == "T2"} == {
        "C",
        "D",
    }
    # q2: C and D (ranks 1, 2 <= size 2) join T1 and incumbent B (rank 3 <=
    # keep 3) stays, so T1 floats to three; A fell to rank 5 (> keep 3) and
    # drops to T2 (rank 5 <= T2 keep 5), E joins T2 at rank 4 <= size 4.
    assert {t for (q, t), tier in tiers.items() if q == q2 and tier == "T1"} == {
        "B",
        "C",
        "D",
    }
    assert {t for (q, t), tier in tiers.items() if q == q2 and tier == "T2"} == {
        "A",
        "E",
    }
    assert "F" not in {t for (q, t) in tiers if q == q2}


def test_incumbents_never_crowd_out_a_name_ranked_within_size() -> None:
    # 2026Q4 regression: T2 incumbents ranked up to keep filled every slot, so
    # APH at rank 166 could not join. Here the four T2 incumbents of q1 sit at
    # ranks 4..7 in q2 (all <= keep 8) and NEW enters at rank 3 (<= size 6).
    q1, q2 = date(2020, 1, 1), date(2020, 4, 1)
    ranks = _ranks(
        [
            (q1, ["A", "B", "C", "D", "E", "F", "NEW"]),
            (q2, ["A", "B", "NEW", "C", "D", "E", "F"]),
        ]
    )
    tiers = [uni.Tier("T1", 2, 2, "trade"), uni.Tier("T2", 6, 8, "breadth")]
    got = uni.assign_tiers(ranks, tiers)
    q2_members = set(got[got["quarter_start"] == q2]["ticker"])
    assert "NEW" in q2_members
    assert {"C", "D", "E", "F"} <= q2_members  # every incumbent within keep stays
    assert len(q2_members) == 7  # the tier floats above size


def test_build_membership_drops_excluded_symbols_before_ranking() -> None:
    q1 = date(2020, 1, 1)
    ranks = _ranks([(q1, ["BAD", "A", "B", "C", "D"])])
    config = {"rules": {"tiers": [T1, T2]}, "exclude": {"tickers": ["BAD"]}}
    got = uni.build_membership(config, ranks, [q1])
    stocks = got[got["asset_type"] == "stock"].set_index("ticker")
    assert "BAD" not in stocks.index
    assert stocks.loc["A", "tier"] == "T1" and stocks.loc["A", "adv_rank"] == 1
    assert set(stocks.index) == {"A", "B", "C", "D"}


def test_curated_rows_from_the_shipped_toml() -> None:
    config = uni.load_config()
    rows = pd.DataFrame(uni.curated_rows(config))
    counts = rows["tier"].value_counts().to_dict()
    assert counts == {"context": 38, "index": 3, "fx": 4, "uk": 8}
    assert set(rows.loc[rows["tier"] == "uk", "isa_tradable"]) == {"yes"}
    assert set(rows.loc[rows["tier"] == "context", "exchange"]) == {"US"}
    assert not rows.duplicated(["ticker", "exchange"]).any()
    assert [t.name for t in config["rules"]["tiers"]] == ["T1", "T2"]


def test_fetch_targets_current_and_former_members() -> None:
    membership = pd.DataFrame(
        [
            {"quarter_start": date(2016, 1, 1), "ticker": "OLD", "exchange": "US"},
            {"quarter_start": date(2016, 4, 1), "ticker": "OLD", "exchange": "US"},
            {"quarter_start": date(2016, 4, 1), "ticker": "NEW", "exchange": "US"},
            {"quarter_start": date(2016, 7, 1), "ticker": "NEW", "exchange": "US"},
        ]
    )
    got = uni.fetch_targets(
        membership, history_start=date(2016, 1, 1), warmup_days=90
    ).set_index("ticker")
    assert (
        got.loc["OLD", "fetch_from"] == "2016-01-01"
    )  # warmup clipped at history start
    assert got.loc["OLD", "fetch_to"] == "2016-06-30" and not got.loc["OLD", "current"]
    assert got.loc["NEW", "fetch_from"] == "2016-01-02"  # 2016-04-01 minus 90 days
    assert got.loc["NEW", "fetch_to"] == "" and got.loc["NEW", "current"]
    listed = membership.assign(
        listed_from=["2016-03-01", "2016-03-01", "2015-01-01", "2015-01-01"]
    )
    got = uni.fetch_targets(
        listed, history_start=date(2016, 1, 1), warmup_days=90
    ).set_index("ticker")
    assert (
        got.loc["OLD", "fetch_from"] == "2016-03-01"
    )  # never before the first daily price
    assert got.loc["NEW", "fetch_from"] == "2016-01-02"


def _daily(tickers: dict[str, float], start: str, end: str) -> pd.DataFrame:
    """Flat daily series with dollar volume ``dv``; each ticker its own price
    (identical close series would be one company to the duplicate check)."""
    days = pd.bdate_range(start, end)
    rows = []
    for k, (t, dv) in enumerate(tickers.items()):
        close = 10.0 + k
        for d in days:
            rows.append(
                {
                    "ticker": t,
                    "exchange": "US",
                    "date": d.strftime("%Y-%m-%d"),
                    "close": close,
                    "volume": dv / close,
                }
            )
    return pd.DataFrame(rows)


def test_build_end_to_end_on_synthetic_lanes(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "us_common").mkdir(parents=True)
    (raw / "us_extended").mkdir(parents=True)
    _daily({"AAA": 9e8, "BBB": 8e8, "CCC": 7e8}, "2015-01-01", "2016-12-31").to_parquet(
        raw / "us_common" / "prices_daily.parquet"
    )
    # us_extended: DDD is a common stock, EEE an ETF (filtered out), AAA duplicates us_common (ignored)
    _daily(
        {"DDD": 6e8, "EEE": 9.9e8, "AAA": 1e3}, "2015-01-01", "2016-12-31"
    ).to_parquet(raw / "us_extended" / "prices_daily.parquet")
    pd.DataFrame(
        {"Code": ["DDD", "EEE", "AAA"], "Type": ["Common Stock", "ETF", "Common Stock"]}
    ).to_parquet(raw / "us_extended" / "tickers_US_EXT.parquet")
    config = uni.load_config()
    config["rules"]["tiers"] = [
        uni.Tier("T1", 2, 2, "trade"),
        uni.Tier("T2", 3, 3, "breadth"),
    ]
    config["rules"]["history_start"] = "2016-01-01"
    out = tmp_path / "intraday"
    membership, current = uni.build(
        as_of=date(2016, 11, 13), config=config, raw_root=raw, out_dir=out
    )
    stocks = current[current["asset_type"] == "stock"].set_index("ticker")
    assert list(stocks.index) == ["AAA", "BBB", "CCC"]
    assert stocks.loc["AAA", "tier"] == "T1" and stocks.loc["CCC", "tier"] == "T2"
    assert stocks.loc["AAA", "adv_rank"] == 1 and stocks.loc[
        "AAA", "reason"
    ].startswith("adv rank 1")
    assert "EEE" not in set(
        membership["ticker"]
    )  # an ETF in us_extended is not a stock
    assert (current["tier"] == "context").sum() == 38
    assert (out / "tickers_INTRADAY.parquet").exists() and (
        out / "universe_membership.parquet"
    ).exists()
    saved = pd.read_parquet(out / "universe_membership.parquet")
    assert saved["quarter_start"].min() == "2016-01-01"
    assert set(saved.loc[saved["ticker"] == "AAA", "listed_from"]) == {"2015-01-01"}
    assert (
        saved.loc[saved["tier"] == "context", "listed_from"].isna().all()
    )  # no daily lane in this fixture


def test_ranks_undo_the_split_adjustment_of_daily_volume(tmp_path: Path) -> None:
    """EODHD daily volume is split-adjusted while close is raw, so a name that
    splits 10:1 *later* shows 10x its real dollar volume before the split."""
    prices = _daily({"BIG": 5e8, "SPLITTER": 1e8}, "2015-01-01", "2016-12-31")
    later = prices["ticker"].eq("SPLITTER") & (prices["date"] < "2016-09-01")
    prices.loc[later, "volume"] *= 10  # adjusted for the 2016-09-01 split
    prices["adjusted_close"] = prices["close"]
    prices.loc[later, "adjusted_close"] = (
        prices.loc[later, "close"] / 10
    )  # history re-adjusted
    prices.to_parquet(tmp_path / "prices.parquet")
    pd.DataFrame(
        [
            {
                "ticker": "SPLITTER",
                "exchange": "US",
                "ex_date": "2016-09-01",
                "split_factor": "10/1",
                "numerator": 10.0,
                "denominator": 1.0,
                "split_ratio": 10.0,
            },
            # a reverse split is ignored (vendor series for such names is already adjusted)
            {
                "ticker": "BIG",
                "exchange": "US",
                "ex_date": "2016-10-03",
                "split_factor": "1/100",
                "numerator": 1.0,
                "denominator": 100.0,
                "split_ratio": 0.01,
            },
        ]
    ).to_parquet(tmp_path / "splits.parquet")
    sources = [
        ("us_common", tmp_path / "prices.parquet", None, tmp_path / "splits.parquet")
    ]
    ranks = uni.load_quarter_ranks(
        sources, [date(2016, 7, 1)], min_price=5.0, min_days=200
    )
    got = ranks.set_index("ticker")
    assert got.loc["BIG", "adv_rank"] == 1 and got.loc["SPLITTER", "adv_rank"] == 2
    assert (
        round(got.loc["SPLITTER", "adv_musd"]) == 100
        and round(got.loc["BIG", "adv_musd"]) == 500
    )
    # without the splits file the lookahead comes back
    naive = uni.load_quarter_ranks(
        [("us_common", tmp_path / "prices.parquet", None, None)],
        [date(2016, 7, 1)],
        min_price=5.0,
        min_days=200,
    ).set_index("ticker")
    assert naive.loc["SPLITTER", "adv_rank"] == 1
    # a split the history was not re-adjusted for yet (bulk top-up) is not undone
    stale = _daily({"BIG": 5e8, "SPLITTER": 1e8}, "2015-01-01", "2016-12-31").assign(
        adjusted_close=10.0
    )
    stale.to_parquet(tmp_path / "prices.parquet")
    kept = uni.load_quarter_ranks(
        sources, [date(2016, 7, 1)], min_price=5.0, min_days=200
    ).set_index("ticker")
    assert round(kept.loc["SPLITTER", "adv_musd"]) == 100


# --------------------------------------------------------------------------- #
# bars
# --------------------------------------------------------------------------- #
def _ts(text: str) -> int:
    return int(datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp())


def _bar(text: str, close: float = 10.0, volume: float = 100.0) -> dict[str, Any]:
    return {
        "timestamp": _ts(text),
        "gmtoffset": 0,
        "datetime": text,
        "open": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "volume": volume,
    }


def test_bars_from_payload_tags_sessions_and_local_dates() -> None:
    payload = [
        _bar("2026-11-10 09:00:00"),  # 04:00 ET pre
        _bar("2026-11-10 14:30:00"),  # 09:30 ET rth
        _bar("2026-11-10 21:00:00"),  # 16:00 ET post (closing cross bar)
        _bar("2026-11-11 00:30:00"),  # 19:30 ET on 2026-11-10 -> previous local date
    ]
    bars = fx.bars_from_payload(payload, "AAPL", "US")
    assert list(bars["session"]) == ["pre", "rth", "post", "post"]
    assert list(bars["date"]) == ["2026-11-10"] * 4
    assert str(bars["ts"].dtype) == "datetime64[ms, UTC]"
    lse = fx.bars_from_payload(
        [_bar("2026-07-01 07:00:00"), _bar("2026-07-01 15:29:00")], "CSPX", "LSE"
    )
    assert list(lse["session"]) == ["rth", "rth"]  # BST: 08:00 and 16:29 London
    fxb = fx.bars_from_payload([_bar("2026-11-10 23:59:00")], "GBPUSD", "FOREX")
    assert list(fxb["session"]) == ["24h"] and list(fxb["date"]) == ["2026-11-10"]
    assert fx.bars_from_payload([], "X", "US").empty


def test_merge_bars_splits_years_and_dedupes(tmp_path: Path) -> None:
    first = fx.bars_from_payload(
        [_bar("2025-12-31 15:00:00", 1.0), _bar("2026-01-02 15:00:00", 2.0)],
        "AAA",
        "US",
    )
    assert fx.merge_bars(tmp_path, first) == 2
    again = fx.bars_from_payload(
        [_bar("2026-01-02 15:00:00", 3.0), _bar("2026-01-02 15:01:00", 4.0)],
        "AAA",
        "US",
    )
    assert fx.merge_bars(tmp_path, again) == 1
    files = [p.name for p in fx.pair_files(tmp_path, "AAA", "US")]
    assert files == ["AAA.US_2025.parquet", "AAA.US_2026.parquet"]
    y26 = pd.read_parquet(tmp_path / "AAA.US_2026.parquet")
    assert list(y26["close"]) == [3.0, 4.0]  # last copy of a ts wins
    assert fx.count_rows(tmp_path, "AAA", "US") == 3


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _pair(t: str, **kw: Any) -> fx.PairState:
    return fx.PairState(
        ticker=t, exchange="US", fetch_from=kw.pop("fetch_from", date(2026, 1, 1)), **kw
    )


def test_chunks_respect_the_window_cap() -> None:
    down = fx.chunk_down(date(2026, 11, 13), date(2026, 1, 1))
    assert all((e - s).days + 1 <= fx.WINDOW_DAYS for s, e in down)
    assert down[0][1] == date(2026, 11, 13) and down[-1][0] == date(2026, 1, 1)
    up = fx.chunk_up(date(2026, 1, 1), date(2026, 11, 13))
    assert up[0][0] == date(2026, 1, 1) and up[-1][1] == date(2026, 11, 13)
    assert (
        sum((e - s).days + 1 for s, e in up)
        == (date(2026, 11, 13) - date(2026, 1, 1)).days + 1
    )


def test_plan_orders_topup_then_first_then_newest_history() -> None:
    to = date(2026, 11, 13)
    pairs = [
        _pair(
            "OLD", coverage_from=date(2026, 9, 1), coverage_through=date(2026, 11, 10)
        ),
        _pair("NEW"),
        _pair("GONE", status="not_found"),
        _pair(
            "FORMER",
            fetch_to=date(2026, 6, 30),
            coverage_from=date(2026, 1, 1),
            coverage_through=date(2026, 6, 30),
        ),
    ]
    windows = fx.plan_windows(pairs, to_date=to)
    phases = [w.phase for w in windows]
    assert phases[0] == "topup" and windows[0].ticker == "OLD"
    assert (
        windows[0].start == date(2026, 11, 7) and windows[0].end == to
    )  # 3-day overlap
    assert phases[1] == "first" and windows[1].ticker == "NEW" and windows[1].end == to
    backfill = windows[2:]
    assert all(w.phase == "backfill" for w in backfill)
    assert [w.end for w in backfill] == sorted((w.end for w in backfill), reverse=True)
    assert {w.ticker for w in windows} == {
        "OLD",
        "NEW",
    }  # not_found and complete former members skipped
    assert "GONE" in {
        w.ticker for w in fx.plan_windows(pairs, to_date=to, retry_not_found=True)
    }
    summary = fx.summarize_plan(windows, max_units=10)
    assert summary["units"] == 5 * len(windows) and summary["units_this_run"] == 10


# --------------------------------------------------------------------------- #
# execution against a fake API
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeApi:
    """Serves one bar per requested UTC day from ``first_day`` through
    ``last_day`` (the vendor's newest published session); 404 for ``missing``."""

    def __init__(
        self,
        first_day: dict[str, date],
        missing: set[str] = frozenset(),
        status: int = 200,
        last_day: dict[str, date] | None = None,
    ) -> None:
        self.first_day = first_day
        self.missing = missing
        self.status = status
        self.last_day = last_day if last_day is not None else {}
        self.calls: list[tuple[str, int, int]] = []

    def __call__(
        self, session: Any, url: str, *, params: dict[str, Any], **_: Any
    ) -> _Resp:
        symbol = url.rsplit("/", 1)[-1]
        self.calls.append((symbol, params["from"], params["to"]))
        if self.status != 200:
            return _Resp(self.status, {})
        if symbol in self.missing:
            return _Resp(404, "Ticker Not Found.")
        start = datetime.fromtimestamp(params["from"], tz=timezone.utc).date()
        end = datetime.fromtimestamp(params["to"], tz=timezone.utc).date()
        end = min(end, self.last_day.get(symbol, end))
        first = self.first_day.get(symbol, date(1900, 1, 1))
        rows = []
        day = max(start, first)
        while day <= end:
            rows.append(_bar(f"{day.isoformat()} 15:00:00"))
            day = day.fromordinal(day.toordinal() + 1)
        return _Resp(200, rows)


def _run(
    tmp_path: Path,
    targets: pd.DataFrame,
    api: _FakeApi,
    *,
    to: date,
    max_units: int = 0,
):
    state_path = tmp_path / "state.csv"
    previous = fx.load_state(state_path)
    pairs = fx.merge_targets(targets, previous)
    windows = fx.plan_windows(pairs.values(), to_date=to)
    result = fx.execute(
        windows,
        pairs,
        session=None,
        bars_dir=tmp_path / "bars",
        state_path=state_path,
        audit_path=tmp_path / "audit.csv",
        previous_state=previous,
        to_date=to,
        max_units=max_units,
        get=api,
    )
    return result, fx.load_state(state_path).set_index("ticker")


def _targets(*rows: tuple[str, str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ticker": t, "exchange": "US", "fetch_from": f, "fetch_to": to}
            for t, f, to in rows
        ]
    )


def test_execute_fills_history_resumes_and_stops_at_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fx, "EMPTY_STREAK_STOP", 2)
    to = date(2026, 11, 13)
    targets = _targets(
        ("AAA", "2025-06-01", ""), ("IPO", "2025-06-01", ""), ("BAD", "2025-06-01", "")
    )
    api = _FakeApi({"IPO.US": date(2026, 10, 1)}, missing={"BAD.US"})
    result, state = _run(tmp_path, targets, api, to=to, max_units=5 * 4)
    assert result.budget_stopped and result.units == 20
    assert state.loc["BAD", "status"] == "not_found"
    assert (
        state.loc["AAA", "status"] == "partial"
        and state.loc["AAA", "coverage_through"] == to.isoformat()
    )
    # second run resumes: no repeat of the newest windows, finishes AAA, stops IPO after 2 empty windows
    api.calls.clear()
    result, state = _run(tmp_path, targets, api, to=to)
    assert all(symbol != "BAD.US" for symbol, _, _ in api.calls)
    assert (
        state.loc["AAA", "status"] == "ok"
        and state.loc["AAA", "coverage_from"] == "2025-06-01"
    )
    assert state.loc["AAA", "first_data_date"] == "2025-06-01"
    assert (
        state.loc["IPO", "status"] == "ok"
        and state.loc["IPO", "first_data_date"] == "2026-10-01"
    )
    assert (
        state.loc["IPO", "coverage_from"] == "2025-06-01"
    )  # history start reached by the empty-streak rule
    ipo_backfills = [c for c in api.calls if c[0] == "IPO.US"]
    assert len(ipo_backfills) <= fx.EMPTY_STREAK_STOP + 1
    n_days = (to - date(2025, 6, 1)).days + 1
    assert int(state.loc["AAA", "n_rows"]) == n_days
    audit = pd.read_csv(tmp_path / "audit.csv")
    assert (
        set(audit["phase"]) <= {"topup", "first", "backfill"}
        and (audit["units"] == 5).all()
    )
    # a third run on the same day spends nothing; the next day is one top-up window per pair
    api.calls.clear()
    _run(tmp_path, targets, api, to=to)
    assert api.calls == []
    _run(tmp_path, targets, api, to=date(2026, 11, 14))
    assert sorted(c[0] for c in api.calls) == ["AAA.US", "IPO.US"]


def test_covered_through_holds_recent_windows_to_their_newest_bar() -> None:
    to = date(2026, 11, 19)
    settled = to - timedelta(days=fx.VENDOR_LAG_DAYS)
    assert fx.covered_through(to, to, to) == to
    assert fx.covered_through(to, date(2026, 11, 17), to) == date(2026, 11, 17)
    assert fx.covered_through(to, None, to) == settled  # halted: still advances
    assert fx.covered_through(to, date(2026, 10, 1), to) == settled
    old = date(2026, 11, 10)  # a settled window is held whatever it returned
    assert fx.covered_through(old, None, to) == old


def test_a_session_the_vendor_publishes_late_is_asked_for_again(
    tmp_path: Path,
) -> None:
    targets = _targets(
        ("AAA", "2026-11-01", ""), ("LAG", "2026-11-01", ""), ("DEAD", "2026-11-01", "")
    )
    api = _FakeApi(
        {},
        last_day={
            "LAG.US": date(2026, 11, 17),  # 11-18 not published yet
            "DEAD.US": date(2026, 11, 6),  # stopped trading
        },
    )
    _, state = _run(tmp_path, targets, api, to=date(2026, 11, 19))
    assert state.loc["AAA", "coverage_through"] == "2026-11-19"
    assert state.loc["LAG", "coverage_through"] == "2026-11-17"
    assert state.loc["DEAD", "coverage_through"] == "2026-11-15"
    # next day the vendor has caught up: LAG's top-up re-reads 11-18 and fills it
    api.last_day["LAG.US"] = date(2026, 11, 20)
    api.calls.clear()
    _, state = _run(tmp_path, targets, api, to=date(2026, 11, 20))
    lag = [c for c in api.calls if c[0] == "LAG.US"]
    assert len(lag) == 1
    assert datetime.fromtimestamp(lag[0][1], tz=timezone.utc).date() <= date(
        2026, 11, 18
    )
    assert state.loc["LAG", "coverage_through"] == "2026-11-20"
    assert state.loc["LAG", "latest_data_date"] == "2026-11-20"
    bars = pd.read_parquet(tmp_path / "bars" / "LAG.US_2026.parquet")
    assert "2026-11-18" in set(bars["date"])
    # a dead member stays one short top-up window a run
    assert len([c for c in api.calls if c[0] == "DEAD.US"]) == 1


def test_a_retired_pair_without_recent_bars_settles(tmp_path: Path) -> None:
    targets = _targets(("OLD", "2026-10-01", "2026-11-17"))
    api = _FakeApi({}, last_day={"OLD.US": date(2026, 11, 13)})
    _, state = _run(tmp_path, targets, api, to=date(2026, 11, 19))
    assert state.loc["OLD", "coverage_through"] == "2026-11-15"
    _, state = _run(tmp_path, targets, api, to=date(2026, 11, 22))
    assert state.loc["OLD", "coverage_through"] == "2026-11-17"  # its fetch_to
    api.calls.clear()
    _run(tmp_path, targets, api, to=date(2026, 11, 23))
    assert api.calls == []


def test_execute_stops_at_the_time_limit(tmp_path: Path) -> None:
    targets = _targets(("AAA", "2025-01-01", ""))
    pairs = fx.merge_targets(targets, fx.load_state(tmp_path / "s.csv"))
    windows = fx.plan_windows(pairs.values(), to_date=date(2026, 11, 13))
    now = iter(range(0, 10_000, 30))  # each check advances the clock 30 s
    result = fx.execute(
        windows,
        pairs,
        session=None,
        bars_dir=tmp_path / "b",
        state_path=tmp_path / "s.csv",
        audit_path=tmp_path / "a.csv",
        previous_state=fx.load_state(tmp_path / "s.csv"),
        to_date=date(2026, 11, 13),
        max_minutes=1.5,
        get=_FakeApi({}),
        clock=lambda: float(next(now)),
    )
    assert (
        result.time_stopped and result.requests == 2
    )  # checks at 30 s and 60 s pass, 90 s stops


def test_rounds_keep_pairs_sequential_and_current_first() -> None:
    to = date(2026, 11, 13)
    pairs = [
        _pair("AAA", fetch_from=date(2025, 1, 1)),
        _pair("BBB", fetch_from=date(2026, 1, 1)),
        _pair(
            "CCC",
            fetch_from=date(2026, 1, 1),
            coverage_from=date(2026, 6, 1),
            coverage_through=date(2026, 11, 1),
        ),
    ]
    rounds = fx.execution_rounds(fx.plan_windows(pairs, to_date=to))
    assert all(
        len({w.key for w in r}) == len(r) for r in rounds
    )  # one window per pair per round
    assert {w.phase for w in rounds[0]} == {"topup", "first"} and len(rounds[0]) == 3
    backfill_rounds = [r for r in rounds if r[0].phase == "backfill"]
    aaa = [w for r in backfill_rounds for w in r if w.ticker == "AAA"]
    assert [w.end for w in aaa] == sorted((w.end for w in aaa), reverse=True)


def test_parallel_execution_matches_sequential(tmp_path: Path) -> None:
    to = date(2026, 11, 13)
    targets = _targets(*[(t, "2024-01-01", "") for t in ("AAA", "BBB", "CCC", "DDD")])
    _, seq = _run(
        tmp_path / "seq", targets, _FakeApi({"DDD.US": date(2026, 3, 1)}), to=to
    )
    state_path = tmp_path / "par" / "state.csv"
    pairs = fx.merge_targets(targets, fx.load_state(state_path))
    fx.execute(
        fx.plan_windows(pairs.values(), to_date=to),
        pairs,
        session=None,
        bars_dir=tmp_path / "par" / "bars",
        state_path=state_path,
        audit_path=tmp_path / "par" / "audit.csv",
        previous_state=fx.load_state(state_path),
        to_date=to,
        workers=4,
        get=_FakeApi({"DDD.US": date(2026, 3, 1)}),
    )
    par = fx.load_state(state_path).set_index("ticker")
    cols = [
        "coverage_from",
        "coverage_through",
        "first_data_date",
        "latest_data_date",
        "n_rows",
        "status",
    ]
    assert par[cols].sort_index().equals(seq[cols].sort_index())


def test_execute_aborts_on_auth_failure(tmp_path: Path) -> None:
    with pytest.raises(fx.AuthError):
        _run(
            tmp_path,
            _targets(("AAA", "2026-10-01", "")),
            _FakeApi({}, status=401),
            to=date(2026, 11, 13),
        )


def test_load_targets_from_membership_and_explicit_tickers(tmp_path: Path) -> None:
    membership = pd.DataFrame(
        [
            {"quarter_start": "2026-10-01", "ticker": "AAA", "exchange": "US"},
            {"quarter_start": "2026-10-01", "ticker": "SWDA", "exchange": "LSE"},
        ]
    )
    path = tmp_path / "m.parquet"
    membership.to_parquet(path)
    config = uni.load_config()
    assert len(fx.load_targets(path, config, tickers=[], limit=0)) == 2
    got = fx.load_targets(path, config, tickers=["swda.lse", "ZZZ"], limit=0)
    assert {(r.ticker, r.exchange) for r in got.itertuples()} == {
        ("SWDA", "LSE"),
        ("ZZZ", "US"),
    }
    with pytest.raises(FileNotFoundError):
        fx.load_targets(tmp_path / "missing.parquet", config, tickers=[], limit=0)


# --------------------------------------------------------------------------- #
# registry, CLI, scheduler
# --------------------------------------------------------------------------- #
def test_lane_is_registered() -> None:
    lane = reg.LANES["intraday"]
    (ds,) = lane.datasets
    assert ds.kind == "bars_1m" and ds.partitioned and ds.ticker_keyed
    assert ds.fetcher == "fetch_eodhd_intraday.py"
    assert ds.fetcher_args == (
        "--max-units",
        str(reg.INTRADAY_REFRESH_MAX_UNITS),
        "--max-minutes",
        str(reg.INTRADAY_REFRESH_MAX_MINUTES),
    )
    assert ds.dry_run_arg == "--dry-run"
    assert lane.universe_fetcher == "build_intraday_universe.py" and lane.universe_local
    assert (
        lane.universe_code_column == "ticker"
        and lane.universe_exchange_column == "exchange"
    )


def test_refresh_plan_is_opt_in_and_takes_passthrough() -> None:
    import cli

    assert "bars_1m" in cli.KNOWN_KINDS and "bars_1m" not in cli.DEFAULT_KINDS
    assert "bars_1m" not in cli.INCREMENTAL_KINDS
    assert not cli.build_refresh_plan(
        ["intraday"], kinds=set(cli.DEFAULT_KINDS), with_universe=True, passthrough=[]
    )
    steps = cli.build_refresh_plan(
        ["intraday"],
        kinds={"bars_1m"},
        with_universe=True,
        passthrough=["--tickers", "NVDA.US"],
    )
    assert [(s.kind, s.local) for s in steps] == [
        ("universe", True),
        ("bars_1m", False),
    ]
    assert steps[1].args == [
        *reg.LANES["intraday"].datasets[0].fetcher_args,
        "--tickers",
        "NVDA.US",
    ]
    assert steps[1].dry_run_arg == "--dry-run"


def test_scheduler_lists_match_the_registry() -> None:
    import cli

    from scheduler import commands

    assert commands.EODHD_LANES == set(reg.LANES)
    assert commands.EODHD_KINDS == set(cli.KNOWN_KINDS)


def test_scheduler_admits_the_intraday_refresh() -> None:
    from scheduler import commands

    registry = commands.default_registry()
    capability = next(c for c in commands.CAPABILITIES if c.identity == "eodhd refresh")
    registry._validate_arguments(
        capability, ("intraday", "--datasets", "bars_1m", "--run"), allow_dry_run=False
    )
    with pytest.raises(commands.CommandValidationError):
        registry._validate_arguments(
            capability,
            ("intraday", "--datasets", "bars_5m", "--run"),
            allow_dry_run=False,
        )


# --------------------------------------------------------------------------- #
# status, catalog, QC
# --------------------------------------------------------------------------- #
def _write_state(path: Path, rows: list[dict[str, str]]) -> None:
    pd.DataFrame(rows, columns=fx.STATE_COLUMNS).fillna("").to_csv(path, index=False)


def test_status_record_for_bars(tmp_path: Path) -> None:
    import status_eodhd as st

    bars = fx.bars_from_payload(
        [_bar("2026-11-12 15:00:00"), _bar("2026-11-12 15:01:00")], "AAA", "US"
    )
    fx.merge_bars(tmp_path / "bars_1m", bars)
    _write_state(
        tmp_path / "bars_1m_fetch_state.csv",
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "fetch_from": "2016-01-01",
                "coverage_from": "2016-01-01",
                "coverage_through": "2026-11-13",
                "latest_data_date": "2026-11-12",
                "first_data_date": "2016-01-04",
                "n_rows": "2",
                "fetched_at": "2026-11-13T06:00:00Z",
                "status": "ok",
            }
        ],
    )
    lane = reg.LaneConfig(
        name="intraday",
        region="US",
        asset_class="intraday",
        datasets=(reg.bars_1m_spec(),),
        root=tmp_path,
    )
    rec = st.collect_dataset(
        lane,
        reg.bars_1m_spec(),
        as_of_ts=pd.Timestamp("2026-11-13"),
        stale_days=7,
        deep=False,
    )
    assert rec["rows"] == 2 and rec["pairs"] == 1 and rec["last_data"] == "2026-11-12"
    assert rec["stale"] is False and rec["status"] == {"ok": 1}


def test_reindex_takes_bars_from_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    duckdb = pytest.importorskip("duckdb")
    import explore_eodhd as ex

    monkeypatch.setattr(ex, "INDEX_PATH", tmp_path / "_datacli_index.parquet")
    monkeypatch.setattr(ex, "META_PATH", tmp_path / "_datacli_meta.json")
    monkeypatch.setattr(ex, "EODHD_RAW_ROOT", tmp_path)
    con = duckdb.connect()
    con.execute(
        "CREATE VIEW bars_1m_state AS SELECT * FROM (VALUES "
        "('AAA','US','1200','2016-01-04','2026-11-12','intraday'),"
        "('NEW','US','','','','intraday')) t(ticker,exchange,n_rows,first_data_date,latest_data_date,lane)"
    )
    assert ex.cmd_reindex(con) == 0
    catalog = pd.read_parquet(tmp_path / "_datacli_index.parquet")
    assert catalog.to_dict("records") == [
        {
            "dataset": "bars_1m",
            "lane": "intraday",
            "ticker": "AAA",
            "exchange": "US",
            "n_rows": 1200,
            "first_date": "2016-01-04",
            "last_date": "2026-11-12",
        }
    ]


def test_qc_report_flags(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    import report_intraday_quality as qc

    bars_dir = tmp_path / "bars"
    full = [
        _bar(f"2026-11-10 {14 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}:00", 10.0, 10.0)
        for m in range(390)
    ]
    full.append(_bar("2026-11-10 21:00:00", 10.0, 500.0))  # closing-cross bar
    fx.merge_bars(bars_dir, fx.bars_from_payload(full, "AAA", "US"))
    fx.merge_bars(
        bars_dir, fx.bars_from_payload(full, "CCC", "US")
    )  # keeps the day from looking market-wide short
    fx.merge_bars(
        bars_dir,
        fx.bars_from_payload([_bar("2026-11-12 15:00:00", 10.0, 1.0)], "AAA", "US"),
    )
    thin = [
        _bar(f"2026-11-10 {14 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}:00", 20.0)
        for m in range(0, 390, 10)
    ]
    fx.merge_bars(bars_dir, fx.bars_from_payload(thin, "BBB", "US"))
    con = duckdb.connect()
    con.execute(
        "CREATE VIEW prices AS SELECT * FROM (VALUES ('AAA','US','2026-11-10',10.0,4400.0),"
        "('AAA','US','2026-11-11',10.0,100.0),('AAA','US','2026-11-12',10.0,1.0),"
        "('BBB','US','2026-11-10',40.0,1000.0)) t(ticker,exchange,date,close,volume)"
    )
    _write_state(
        tmp_path / "state.csv",
        [
            {"ticker": "AAA", "exchange": "US", "status": "ok"},
            {"ticker": "BBB", "exchange": "US", "status": "partial"},
        ],
    )
    report = qc.run_report(
        con,
        (bars_dir / "*.parquet").as_posix(),
        tmp_path / "state.csv",
        since=None,
        t1=[("AAA", "US"), ("BBB", "US")],
        has_prices=True,
    )
    assert report["holes"] == [
        {
            "pair": "AAA.US",
            "missing_days": 1,
            "first": "2026-11-11",
            "last": "2026-11-11",
        }
    ]
    assert (
        report["totals"]["bars"] == 2 * 391 + 39 + 1
        and report["duplicates"] == 0
        and report["ohlc_bad"] == 0
    )
    # BBB is thin on its only day; AAA's one-bar day (2026-11-12) is thin too --
    # two days of data are no basis for calling it a market-wide short day
    assert [t["pair"] for t in report["thin"]] == ["BBB.US", "AAA.US"]
    assert (
        report["gaps"][0]["pair"] == "BBB.US"
        and report["gaps"][0]["worst_minutes"] == 10
    )
    assert (
        report["close_mismatch_count"] == 1
        and report["close_mismatch"][0]["ratio"] == 0.5
    )
    (vol,) = report["volume_ratio"]
    assert vol["exchange"] == "US" and vol["pair_days"] == 3
    codes = {code for _, code, _ in qc.flags(report)}
    assert {
        "thin_sessions",
        "close_check",
        "gaps",
        "volume_vs_daily",
        "backfill",
    } <= codes


def test_qc_volume_ratio_undoes_later_splits(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    import report_intraday_quality as qc

    bars_dir = tmp_path / "bars"
    day = [
        _bar(f"2026-11-10 {14 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}:00", 10.0, 10.0)
        for m in range(390)
    ]
    fx.merge_bars(
        bars_dir, fx.bars_from_payload(day, "AAA", "US")
    )  # 3,900 shares in rth
    con = duckdb.connect()
    # daily volume 7,800 = 3,900 traded x the 2:1 split that came later
    con.execute(
        "CREATE VIEW prices AS SELECT * FROM (VALUES ('AAA','US','2026-11-10',10.0,7800.0))"
        " t(ticker,exchange,date,close,volume)"
    )
    con.execute(
        "CREATE VIEW splits AS SELECT * FROM (VALUES ('AAA','US','2026-11-20',2.0),('AAA','US','2026-11-01',5.0))"
        " t(ticker,exchange,ex_date,split_ratio)"
    )
    report = qc.run_report(
        con,
        (bars_dir / "*.parquet").as_posix(),
        tmp_path / "none.csv",
        since=None,
        has_prices=True,
    )
    (vol,) = report["volume_ratio"]
    assert vol["p50"] == 1.0  # only the split after the day counts


def test_qc_short_days_are_judged_per_exchange(tmp_path: Path) -> None:
    """A thin market (LSE ETFs) must not turn every day into a short day, and a
    US half day must not hide LSE thinness (or vice versa)."""
    duckdb = pytest.importorskip("duckdb")
    import report_intraday_quality as qc

    bars_dir = tmp_path / "bars"

    def us_day(day: str, minutes: int) -> list[dict[str, Any]]:
        return [
            _bar(f"{day} {14 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}:00")
            for m in range(minutes)
        ]

    us = (
        us_day("2026-11-24", 390)
        + us_day("2026-11-25", 390)
        + us_day("2026-11-27", 210)
    )
    for ticker in ("AAA", "BBB"):
        fx.merge_bars(bars_dir, fx.bars_from_payload(us, ticker, "US"))
    # an LSE ETF trading about 100 of 510 minutes on each of three normal days
    lse = [
        _bar(f"{day} {8 + m // 12:02d}:{(m % 12) * 5:02d}:00")
        for day in ("2026-11-24", "2026-11-25", "2026-11-27")
        for m in range(100)
    ]
    # rounding noise (high a hair under close) is tolerated; a real break is not
    lse[0] = {**lse[0], "close": 10.2, "high": 10.1999}
    lse[1] = {**lse[1], "close": 10.5, "high": 10.1}
    fx.merge_bars(bars_dir, fx.bars_from_payload(lse, "CSPX", "LSE"))
    report = qc.run_report(
        duckdb.connect(),
        (bars_dir / "*.parquet").as_posix(),
        tmp_path / "none.csv",
        since=None,
    )
    assert report["ohlc_bad"] == 1
    assert report["short_days_by_exchange"] == {"US": 1}
    assert report["short_days"] == ["2026-11-27"]
    # the LSE pair is still reported thin on every day; the US half day is not
    assert report["thin"] == [{"pair": "CSPX.LSE", "days": 3, "thin_days": 3}]


# --------------------------------------------------------------------------- #
# cut 2: views and verbs
# --------------------------------------------------------------------------- #
def _bars_view(
    con: Any, tmp_path: Path, payloads: dict[str, list[dict[str, Any]]]
) -> None:
    bars_dir = tmp_path / "bars_1m"
    for spec, payload in payloads.items():
        t, e = spec.split(".")
        fx.merge_bars(bars_dir, fx.bars_from_payload(payload, t, e))
    con.execute(
        f"CREATE VIEW bars_1m AS SELECT *, 'intraday' AS lane FROM read_parquet('{(bars_dir / '*.parquet').as_posix()}')"
    )


def test_bars_1m_adj_divides_by_later_splits(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    import explore_eodhd as ex

    con = duckdb.connect()
    _bars_view(
        con,
        tmp_path,
        {
            "NVDA.US": [
                _bar("2021-07-19 15:00:00", 800.0, 10.0),
                _bar("2024-06-07 15:00:00", 1200.0, 10.0),
                _bar("2024-06-10 15:00:00", 120.0, 10.0),
            ]
        },
    )
    con.execute(
        "CREATE VIEW splits AS SELECT * FROM (VALUES ('NVDA','US','2021-07-20',4.0),('NVDA','US','2024-06-10',10.0)) "
        "t(ticker,exchange,ex_date,split_ratio)"
    )
    assert "bars_1m_adj" in ex.register_intraday_views(con, root=tmp_path)
    got = [
        (d, round(c, 6), round(v, 6), round(f, 6))
        for d, c, v, f in con.execute(
            "SELECT date, close, volume, split_adj FROM bars_1m_adj ORDER BY ts"
        ).fetchall()
    ]
    assert got == [
        ("2021-07-19", 20.0, 400.0, 40.0),
        ("2024-06-07", 120.0, 100.0, 10.0),
        ("2024-06-10", 120.0, 10.0, 1.0),
    ]


def test_bars_and_universe_verbs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    duckdb = pytest.importorskip("duckdb")
    import explore_eodhd as ex

    con = duckdb.connect()
    day = [
        _bar(
            f"2026-11-10 {14 + (30 + m) // 60:02d}:{(30 + m) % 60:02d}:00",
            10.0 + m,
            1.0,
        )
        for m in range(30)
    ]
    day.append(_bar("2026-11-10 13:00:00", 9.0, 1.0))  # pre-market, excluded by default
    _bars_view(con, tmp_path, {"AAA.US": day})
    (tmp_path / "intraday").mkdir()
    pd.DataFrame(
        [
            {
                "quarter_start": "2026-07-01",
                "ticker": "AAA",
                "exchange": "US",
                "tier": "T2",
                "group": "T2",
                "adv_rank": 150,
                "adv_musd": 500.0,
                "isa_tradable": "unverified",
            },
            {
                "quarter_start": "2026-10-01",
                "ticker": "AAA",
                "exchange": "US",
                "tier": "T1",
                "group": "T1",
                "adv_rank": 90,
                "adv_musd": 900.0,
                "isa_tradable": "unverified",
            },
            {
                "quarter_start": "2026-10-01",
                "ticker": "SPY",
                "exchange": "US",
                "tier": "context",
                "group": "broad",
                "adv_rank": None,
                "adv_musd": None,
                "isa_tradable": "no",
            },
        ]
    ).to_parquet(tmp_path / "intraday" / "universe_membership.parquet")
    ex.register_intraday_views(con, root=tmp_path)
    assert ex.bars(con, "AAA.US", None, "15m", "rth", False, 100) == 0
    out = capsys.readouterr().out
    assert "2 bar(s)" in out and "09:30" in out and "09:45" in out
    assert ex.bars(con, "AAA.US", "2026-11-10", "1h", "all", False, 100) == 0
    assert (
        "2 bar(s)" in capsys.readouterr().out
    )  # the 08:00 ET pre bar + the 09:00 ET bucket
    assert ex.bars(con, "AAA.US", "2026-11-10", "7m", "rth", False, 100) == 2
    assert ex.universe(con, "intraday", None, None, False) == 0
    out = capsys.readouterr().out
    assert "T1=1" in out and "context=1" in out
    assert ex.universe(con, "intraday", None, None, True) == 0
    out = capsys.readouterr().out
    assert "2026-07-01 -> 2026-10-01" in out and "SPY" in out
    assert ex.universe(con, "intraday", "2026-08-15", "T2", False) == 0
    assert "T2=1" in capsys.readouterr().out


def test_a_vendor_hole_does_not_end_the_backfill(tmp_path: Path) -> None:
    class HoleApi(_FakeApi):
        def __call__(
            self, session: Any, url: str, *, params: dict[str, Any], **kw: Any
        ) -> _Resp:
            start = datetime.fromtimestamp(params["from"], tz=timezone.utc).date()
            if (
                date(2023, 8, 1) <= start <= date(2024, 4, 30)
            ):  # TSLA-style empty stretch
                self.calls.append((url, params["from"], params["to"]))
                return _Resp(200, [])
            return super().__call__(session, url, params=params, **kw)

    _, state = _run(
        tmp_path,
        _targets(("TSLA", "2022-01-01", "")),
        HoleApi({}),
        to=date(2026, 11, 13),
    )
    assert (
        state.loc["TSLA", "first_data_date"] == "2022-01-01"
        and state.loc["TSLA", "status"] == "ok"
    )


# --------------------------------------------------------------------------- #
# renamed symbols, duplicates, masks, half days (2026-11-13 follow-up)
# --------------------------------------------------------------------------- #
def _series(ticker: str, start: str, end: str, close: float, dv: float) -> pd.DataFrame:
    days = pd.bdate_range(start, end)
    return pd.DataFrame(
        {
            "ticker": ticker,
            "exchange": "US",
            "date": days.strftime("%Y-%m-%d"),
            "close": close,
            "volume": dv / close,
        }
    )


def test_one_company_under_several_symbols_ranks_once(tmp_path: Path) -> None:
    """OLD and NEW are one daily series (a rename filed twice); OLD stops trading
    at the end of 2016. GONE stopped before the quarter. OTHER is unrelated."""
    prices = pd.concat(
        [
            _series("OLD", "2015-01-01", "2016-12-30", 20.0, 9e8),
            _series("NEW", "2015-01-01", "2017-12-29", 20.0, 9e8),
            _series("OTHER", "2015-01-01", "2017-12-29", 30.0, 5e8),
            _series("GONE", "2015-01-01", "2016-06-01", 40.0, 7e8),
        ]
    )
    prices.to_parquet(tmp_path / "prices.parquet")
    sources = [("us_common", tmp_path / "prices.parquet", None, None)]
    quarters = [date(2016, 7, 1), date(2017, 7, 1)]
    ranks = uni.load_quarter_ranks(sources, quarters, min_price=5.0, min_days=200)
    q1 = ranks[ranks["quarter_start"] == date(2016, 7, 1)]
    # the symbol in use (the daily history that ends first) labels the company;
    # GONE has no price in the 10 days before the quarter
    assert list(q1["ticker"]) == ["OLD", "OTHER"]
    assert list(q1["adv_rank"]) == [1, 2]
    assert set(ranks["company"]) == {"NEW", "OTHER"}  # OLD and NEW: one company id
    q2 = ranks[ranks["quarter_start"] == date(2017, 7, 1)]
    assert list(q2["ticker"]) == ["NEW", "OTHER"]  # OLD stopped trading
    # 1m coverage overrides the end-date rule: the vendor filed the bars under NEW
    covered = uni.load_quarter_ranks(
        sources,
        quarters,
        min_price=5.0,
        min_days=200,
        coverage={"NEW": (date(2015, 1, 2), date(2017, 12, 29))},
    )
    first = covered[covered["quarter_start"] == date(2016, 7, 1)]
    assert list(first["ticker"]) == ["NEW", "OTHER"]


def test_a_relabelled_company_keeps_its_tier() -> None:
    ranks = pd.DataFrame(
        [
            (date(2016, 1, 1), "OLD", "CO", 2),
            (date(2016, 1, 1), "AAA", "AAA", 1),
            (date(2016, 1, 1), "BBB", "BBB", 3),
            (date(2016, 4, 1), "NEW", "CO", 2),  # same company, new label
            (date(2016, 4, 1), "AAA", "AAA", 1),
            (date(2016, 4, 1), "BBB", "BBB", 3),
        ],
        columns=["quarter_start", "ticker", "company", "adv_rank"],
    )
    tiers = [uni.Tier("T1", 1, 1, "trade"), uni.Tier("T2", 2, 3, "breadth")]
    out = uni.assign_tiers(ranks, tiers)
    q2 = out[out["quarter_start"] == date(2016, 4, 1)].set_index("ticker")
    assert set(q2.index) == {"AAA", "NEW"} and q2.loc["NEW", "tier"] == "T2"
    # the hysteresis case: in q2 the company ranks 3 (> size 2, <= keep 3) under
    # its new label, so only its incumbency as CO keeps it
    swapped = ranks.assign(
        adv_rank=[1, 2, 3, 1, 3, 2],
        ticker=["AAA", "OLD", "BBB", "AAA", "NEW", "BBB"],
        company=["AAA", "CO", "BBB", "AAA", "CO", "BBB"],
    )
    kept = uni.assign_tiers(swapped, tiers)
    q2_kept = set(kept[kept["quarter_start"] == date(2016, 4, 1)]["ticker"])
    assert q2_kept == {"AAA", "BBB", "NEW"}
    unlinked = uni.assign_tiers(swapped.drop(columns="company"), tiers)
    assert "NEW" not in set(
        unlinked[unlinked["quarter_start"] == date(2016, 4, 1)]["ticker"]
    )


def test_early_close_calendar() -> None:
    assert fx.early_close("US", date(2025, 11, 28)) == fx.time(13, 0)
    assert fx.early_close("US", date(2024, 12, 24)) == fx.time(13, 0)
    assert (
        fx.early_close("US", date(2021, 12, 24)) is None
    )  # Friday: Christmas observed
    assert fx.early_close("US", date(2025, 7, 3)) == fx.time(13, 0)
    assert fx.early_close("US", date(2020, 7, 3)) is None  # Friday: the holiday
    assert fx.early_close("US", date(2025, 11, 27)) is None  # Thanksgiving itself
    assert fx.early_close("LSE", date(2025, 12, 31)) == fx.time(12, 30)
    assert fx.early_close("FOREX", date(2025, 12, 24)) is None
    payload = [
        _bar("2025-11-28 17:59:00"),  # 12:59 ET on a half day
        _bar("2025-11-28 18:00:00"),  # 13:00 ET: after the early close
        _bar("2025-11-26 20:59:00"),  # 15:59 ET on a normal day
    ]
    bars = fx.bars_from_payload(payload, "AAA", "US")
    assert list(bars["session"]) == ["rth", "rth", "post"]  # sorted by ts


def test_retag_sessions_rewrites_stored_half_days(tmp_path: Path) -> None:
    bars = fx.bars_from_payload(
        [_bar("2025-11-28 18:30:00"), _bar("2025-11-26 18:30:00")], "AAA", "US"
    )
    bars["session"] = "rth"  # as tagged before the calendar existed
    fx.merge_bars(tmp_path, bars)
    assert fx.retag_sessions(tmp_path) == (1, 1)
    stored = pd.read_parquet(tmp_path / "AAA.US_2025.parquet")
    assert list(stored["session"]) == ["rth", "post"]
    assert fx.retag_sessions(tmp_path) == (0, 0)  # idempotent


def test_corrections_add_alias_rows_and_mask_history() -> None:
    config = {
        "alias": [
            {
                "ticker": "BKR",
                "source": "BHGE",
                "from": "2017-07-05",
                "until": "2019-10-17",
            },
            {
                "ticker": "GONE",
                "source": "OLD",
                "until": "2015-01-01",
            },  # before the span
        ],
        "mask": [
            {"ticker": "FI", "until": "2023-06-06"},
            {"ticker": "MT", "until": "2020-06-30"},
        ],
    }
    targets = _targets(
        ("BKR", "2016-01-01", "2018-06-30"),
        ("GONE", "2016-01-01", ""),
        ("FI", "2019-07-01", ""),
        ("MT", "2016-01-01", "2019-12-31"),  # entirely masked: dropped
    )
    out = fx.apply_corrections(targets, config).set_index(["ticker", "source"])
    assert out.loc[("BKR", "BHGE"), "fetch_from"] == "2017-07-05"
    assert out.loc[("BKR", "BHGE"), "fetch_to"] == "2018-06-30"
    assert out.loc[("BKR", ""), "fetch_from"] == "2016-01-01"
    assert ("GONE", "OLD") not in out.index
    assert out.loc[("FI", ""), "fetch_from"] == "2023-06-07"
    assert "MT" not in set(out.index.get_level_values(0))


def test_alias_windows_request_the_old_symbol_and_store_under_the_member(
    tmp_path: Path,
) -> None:
    to = date(2018, 3, 31)
    targets = fx.apply_corrections(
        _targets(("BKR", "2017-11-01", "")),
        {"alias": [{"ticker": "BKR", "source": "BHGE", "until": "2018-01-31"}]},
    )
    # BKR.US has nothing before 2018-02 except one bad bar the alias must replace
    stale = fx.bars_from_payload([_bar("2018-01-10 15:00:00", 99.0)], "BKR", "US")
    fx.merge_bars(tmp_path / "bars", stale)
    api = _FakeApi({"BKR.US": date(2018, 2, 1)})
    result, _ = _run(tmp_path, targets, api, to=to)
    assert result.failed == 0
    assert {c[0] for c in api.calls} == {"BKR.US", "BHGE.US"}
    stored = pd.concat(
        pd.read_parquet(p) for p in fx.pair_files(tmp_path / "bars", "BKR", "US")
    )
    assert set(stored["ticker"]) == {"BKR"}
    assert 99.0 not in set(stored["close"])  # replaced by the alias window
    state = fx.load_state(tmp_path / "state.csv").set_index(["ticker", "source"])
    alias_rows = int(state.loc[("BKR", "BHGE"), "n_rows"])
    own_rows = int(state.loc[("BKR", ""), "n_rows"])
    assert alias_rows == (date(2018, 1, 31) - date(2017, 11, 1)).days + 1
    assert alias_rows + own_rows == fx.count_rows(tmp_path / "bars", "BKR", "US")
    audit = pd.read_csv(tmp_path / "audit.csv", dtype=str, keep_default_na=False)
    assert set(audit.loc[audit["source"] == "BHGE", "ticker"]) == {"BKR"}


def test_purge_masked_deletes_the_other_security(tmp_path: Path) -> None:
    bars = fx.bars_from_payload(
        [_bar("2021-09-30 15:00:00", 1.0), _bar("2023-06-07 15:00:00", 115.0)],
        "FI",
        "US",
    )
    fx.merge_bars(tmp_path, bars)
    row = {c: "" for c in fx.STATE_COLUMNS}
    row.update(
        ticker="FI",
        exchange="US",
        n_rows="2",
        first_data_date="2021-09-30",
        latest_data_date="2023-06-07",
    )
    state = pd.DataFrame([row])
    config = {"mask": [{"ticker": "FI", "until": "2023-06-06"}]}
    fixed, gone = fx.purge_masked(tmp_path, state, config)
    assert gone == 1
    assert fixed.loc[0, "first_data_date"] == "2023-06-07"
    assert fixed.loc[0, "n_rows"] == "1"
    assert [p.name for p in fx.pair_files(tmp_path, "FI", "US")] == [
        "FI.US_2023.parquet"
    ]
    assert fx.purge_masked(tmp_path, fixed, config)[1] == 0


def test_audit_header_gains_new_columns(tmp_path: Path) -> None:
    path = tmp_path / "audit.csv"
    path.write_text(
        "requested_at,ticker,exchange,phase,from_date,to_date,http_status,rows,units,error\n"
        "2026-01-01T00:00:00Z,AAA,US,first,2026-01-01,2026-01-02,200,1,5,\n"
    )
    fx._upgrade_audit(path)
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert list(frame.columns) == fx.AUDIT_COLUMNS and frame.loc[0, "source"] == ""
