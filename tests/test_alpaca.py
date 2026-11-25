"""alpaca: auth, client, parsing, universe, planning, store, refresh, views, CLI -- all offline."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from alpaca import auctions as au  # noqa: E402
from alpaca import cli, client, plan, qc, refresh, store, universe, views  # noqa: E402
from alpaca.auth import KEY_ID_VAR, SECRET_VAR, Credentials, available  # noqa: E402
from alpaca.config import ENV_ROOT, Settings, settings_from  # noqa: E402
from alpaca.errors import (  # noqa: E402
    ApiError,
    AuthError,
    CredentialsError,
    RateLimited,
)
from alpaca.quiet import QuietSchedule, QuietWindow  # noqa: E402

duckdb = pytest.importorskip("duckdb")

D = dt.date
NO_REGISTRY = {"registry": lambda name: ""}


def _print(code: str, price: float, size: int, ts: str, venue: str) -> dict[str, Any]:
    return {"c": code, "p": price, "s": size, "t": ts, "x": venue}


def _day(day: str, opens: list | None, closes: list | None) -> dict[str, Any]:
    return {"d": day, "o": opens, "c": closes}


def _aapl_day(day: str, scale: float = 1.0) -> dict[str, Any]:
    """A modern day: the listing exchange's cross plus its repeating report, and a small venue's report."""
    return _day(
        day,
        [
            _print("Q", 351.92, 1, f"{day}T14:30:00.143Z", "P"),
            _print("O", 352.05, int(488429 * scale), f"{day}T14:30:01.096Z", "Q"),
            _print("Q", 352.05, int(488429 * scale), f"{day}T14:30:01.097Z", "Q"),
        ],
        [
            _print("M", 352.27, 40, f"{day}T21:00:00.130Z", "P"),
            _print("6", 352.45, int(5378651 * scale), f"{day}T21:00:00.467Z", "Q"),
            _print("M", 352.45, int(5378651 * scale), f"{day}T21:00:00.468Z", "Q"),
        ],
    )


# --------------------------------------------------------------------------- #
# auth and config
# --------------------------------------------------------------------------- #
def test_credentials_need_both_variables_and_never_show_the_secret() -> None:
    creds = Credentials.from_env(
        {KEY_ID_VAR: "PKABCDEFGH", SECRET_VAR: "s3cr3t-value"}, **NO_REGISTRY
    )
    assert (
        "s3cr3t" not in repr(creds)
        and "s3cr3t" not in str(creds)
        and repr(creds).endswith("EFGH)")
    )
    assert creds.headers() == {
        "APCA-API-KEY-ID": "PKABCDEFGH",
        "APCA-API-SECRET-KEY": "s3cr3t-value",
    }
    with pytest.raises(CredentialsError, match=SECRET_VAR):
        Credentials.from_env({KEY_ID_VAR: "PKABCDEFGH"}, **NO_REGISTRY)
    assert not available({}, **NO_REGISTRY)


def test_settings_reject_unknown_keys_and_other_feeds() -> None:
    s = settings_from(
        {
            "history_start": "2018-01-01",
            "quiet_windows": ["15:00-16:30"],
            "data_root": "x",
        }
    )
    assert s.history_start == D(2018, 1, 1) and s.quiet_windows == ("15:00-16:30",)
    with pytest.raises(ValueError, match="unknown"):
        settings_from({"histroy_start": "2018-01-01"})
    with pytest.raises(ValueError, match="sip"):
        Settings(feed="iex")


def test_quiet_windows_apply_on_weekdays_in_new_york() -> None:
    schedule = QuietSchedule(["15:30-16:15"], "America/New_York")
    monday_1540_ny = dt.datetime(2026, 11, 23, 20, 40, tzinfo=dt.timezone.utc)
    assert str(schedule.active(monday_1540_ny)) == "15:30-16:15"
    assert schedule.active(monday_1540_ny + dt.timedelta(hours=1)) is None
    assert (
        schedule.active(dt.datetime(2026, 11, 22, 20, 40, tzinfo=dt.timezone.utc))
        is None
    )  # Sunday
    with pytest.raises(ValueError):
        QuietWindow.parse("16:15-15:30")
    with pytest.raises(ValueError):
        schedule.active(dt.datetime(2026, 11, 23, 15, 40))


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Session:
    def __init__(self, responses: list[_Response]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)


def _client(session: _Session, **kwargs: Any) -> client.AuctionsClient:
    creds = Credentials("PKID", "SECRET")
    return client.AuctionsClient(
        creds,
        data_url="https://data.example/v2/stocks/",
        session=session,
        sleep=lambda s: None,
        **kwargs,
    )


def test_client_merges_pages_and_sends_asof_and_headers() -> None:
    session = _Session(
        [
            _Response(
                200,
                {
                    "auctions": {"AAPL": [_aapl_day("2026-11-18")]},
                    "next_page_token": "tok",
                },
            ),
            _Response(
                200,
                {
                    "auctions": {"AAPL": [_aapl_day("2026-11-19")], "MSFT": []},
                    "next_page_token": None,
                },
            ),
        ]
    )
    records, pages = _client(session).auctions(
        ["AAPL", "MSFT"], D(2026, 11, 18), D(2026, 11, 19), asof=D(2025, 1, 2)
    )
    assert (
        pages == 2
        and [d["d"] for d in records["AAPL"]] == ["2026-11-18", "2026-11-19"]
        and "MSFT" not in records
    )
    first, second = session.calls
    assert first["url"] == "https://data.example/v2/stocks/auctions"
    assert (
        first["params"]["symbols"] == "AAPL,MSFT"
        and first["params"]["asof"] == "2025-01-02"
    )
    assert first["params"]["feed"] == "sip" and "page_token" not in first["params"]
    assert second["params"]["page_token"] == "tok"
    assert first["headers"]["APCA-API-KEY-ID"] == "PKID"


def test_client_maps_errors_without_leaking_the_key() -> None:
    with pytest.raises(AuthError) as caught:
        _client(_Session([_Response(403, {"message": "forbidden"})])).auctions(
            ["AAPL"], D(2026, 1, 2), D(2026, 1, 2)
        )
    assert (
        "SECRET" not in str(caught.value)
        and "forbidden" in str(caught.value)
        and caught.value.status == 403
    )
    with pytest.raises(ApiError, match="invalid symbol"):
        _client(_Session([_Response(422, {"message": "invalid symbol"})])).auctions(
            ["??"], D(2026, 1, 2), D(2026, 1, 2)
        )
    with pytest.raises(RateLimited):
        _client(_Session([_Response(429, {})] * 4)).auctions(
            ["AAPL"], D(2026, 1, 2), D(2026, 1, 2)
        )
    with pytest.raises(ApiError, match="not JSON"):
        _client(_Session([_Response(200, ValueError("bad"))])).auctions(
            ["AAPL"], D(2026, 1, 2), D(2026, 1, 2)
        )
    with pytest.raises(ApiError, match="did not end"):
        loop = [_Response(200, {"auctions": {}, "next_page_token": "again"})] * 3
        _client(_Session(loop), max_pages=2).auctions(
            ["AAPL"], D(2026, 1, 2), D(2026, 1, 2)
        )


def test_rate_limiter_spaces_requests_evenly() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    limiter = client.RateLimiter(120, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert slept == [0.5, 0.5]


# --------------------------------------------------------------------------- #
# parsing and the choice of the cross
# --------------------------------------------------------------------------- #
def test_parse_drops_days_outside_the_range_and_rejects_unrequested_symbols() -> None:
    late_report = _day(
        "2026-11-17",
        None,
        [_print("M", 586.76, 663604, "2026-11-18T00:00:00.003Z", "N")],
    )
    records = {"BRK.B": [late_report, _aapl_day("2026-11-18")]}
    frame = au.parse_records(
        records, {"BRK.B": "BRK-B"}, D(2026, 11, 18), D(2026, 11, 19)
    )
    assert (
        set(frame["date"]) == {D(2026, 11, 18)}
        and set(frame["ticker"]) == {"BRK-B"}
        and len(frame) == 6
    )
    assert str(frame["ts"].dtype) == "datetime64[ns, UTC]"
    with pytest.raises(au.RecordError, match="unrequested"):
        au.parse_records(
            {"XYZ": [_aapl_day("2026-11-18")]},
            {"AAPL": "AAPL"},
            D(2026, 11, 18),
            D(2026, 11, 18),
        )
    with pytest.raises(au.RecordError):
        au.parse_records(
            {"AAPL": [{"d": "2026-11-18", "c": [{"c": "6"}]}]},
            {"AAPL": "AAPL"},
            D(2026, 11, 18),
            D(2026, 11, 18),
        )


def test_select_crosses_takes_the_largest_cross_and_never_sums_reports() -> None:
    day = "2016-01-04"
    records = {
        "AAPL": [
            _aapl_day("2026-11-18"),
            _day(
                day,
                [
                    _print("O", 102.5, 883414, f"{day}T14:30:00.235Z", "T"),
                    _print("O", 102.4, 900, f"{day}T14:30:00.1Z", "P"),
                ],
                [_print("6", 105.35, 3440267, f"{day}T21:00:00.266Z", "T")],
            ),
            _day(
                "2026-11-19", None, [_print("M", 1.0, 5, "2026-11-19T21:00:00Z", "P")]
            ),
        ]
    }
    prints = au.parse_records(records, {"AAPL": "AAPL"}, D(2016, 1, 1), D(2026, 12, 31))
    daily = au.select_crosses(prints).set_index("date")
    modern = daily.loc[D(2026, 11, 18)]
    assert (
        modern["close_size"] == 5378651
        and modern["close_exchange"] == "Q"
        and modern["close_crosses"] == 1
    )
    assert (
        modern["open_price"] == 352.05
        and modern["open_size"] == 488429
        and modern["n_reports"] == 4
    )
    old = daily.loc[D(2016, 1, 4)]
    assert (
        old["open_size"] == 883414
        and old["open_exchange"] == "T"
        and old["open_crosses"] == 2
    )
    report_only = daily.loc[D(2026, 11, 19)]
    assert (
        pd.isna(report_only["close_price"])
        and pd.isna(report_only["close_size"])
        and report_only["n_reports"] == 1
    )
    assert list(au.select_crosses(au.empty_prints()).columns) == au.DAILY_COLUMNS


# --------------------------------------------------------------------------- #
# universe and planning
# --------------------------------------------------------------------------- #
def test_spans_spell_share_classes_and_ask_retired_symbols_as_of_their_last_price() -> (
    None
):
    members = pd.DataFrame(
        {
            "ticker": ["BRK-B", "MMC", "NEW", "GONE", "SPY"],
            "asset_type": ["stock"] * 4 + ["etf"],
        }
    )
    daily = pd.DataFrame(
        {
            "ticker": ["BRK-B", "MMC", "NEW", "SPY"],
            "first": [D(2005, 1, 3), D(2017, 1, 3), D(2026, 11, 1), D(2005, 1, 3)],
            "last": [D(2026, 11, 20), D(2026, 1, 13), D(2026, 11, 20), D(2026, 11, 20)],
        }
    )
    result = universe.spans_from_daily(
        members,
        daily,
        target=D(2026, 11, 20),
        history_start=D(2016, 1, 1),
        retired_after_days=10,
        symbol_map={"SPY": "SPY"},
    )
    spans = {s.ticker: s for s in result.spans}
    assert (
        spans["BRK-B"].symbol == "BRK.B"
        and spans["BRK-B"].start == D(2016, 1, 1)
        and spans["BRK-B"].asof is None
    )
    assert (
        spans["MMC"].asof == D(2026, 1, 13)
        and spans["MMC"].end == D(2026, 1, 13)
        and spans["MMC"].retired
    )
    assert spans["NEW"].start == D(2026, 11, 1) and spans["SPY"].asset_type == "etf"
    assert result.skipped == {"GONE": "no daily prices"}
    assert (
        universe.alpaca_symbol("BF-B") == "BF.B"
        and universe.alpaca_symbol("AAPL") == "AAPL"
    )


def _span(
    ticker: str, start: D, end: D, asof: D | None = None, symbol: str | None = None
) -> universe.SymbolSpan:
    return universe.SymbolSpan(ticker, symbol or ticker, "stock", start, end, asof)


def test_plan_tops_up_with_overlap_backfills_and_restarts_on_a_new_symbol() -> None:
    spans = [
        _span("AAPL", D(2016, 1, 1), D(2026, 11, 20)),
        _span("MSFT", D(2016, 1, 1), D(2026, 11, 20)),
        _span("OLD", D(2016, 1, 1), D(2020, 1, 31), asof=D(2020, 1, 31)),
        _span("REN", D(2016, 1, 1), D(2026, 11, 20), symbol="REN2"),
        _span("NEW", D(2016, 1, 1), D(2026, 11, 20)),
    ]
    covered = dict(
        coverage_from=D(2018, 1, 1), coverage_through=D(2026, 11, 18), status="partial"
    )
    states = {
        "AAPL": store.SymbolState("AAPL", symbol="AAPL", **covered),
        "MSFT": store.SymbolState(
            "MSFT",
            symbol="MSFT",
            coverage_from=D(2016, 1, 1),
            coverage_through=D(2026, 11, 18),
        ),
        "OLD": store.SymbolState(
            "OLD",
            symbol="OLD",
            asof=D(2020, 1, 31),
            coverage_from=D(2016, 1, 1),
            coverage_through=D(2020, 1, 31),
        ),
        "REN": store.SymbolState(
            "REN",
            symbol="REN",
            coverage_from=D(2016, 1, 1),
            coverage_through=D(2026, 11, 20),
        ),
    }
    tasks = plan.plan_tasks(spans, states, overlap_days=3)
    by = {(t.ticker, t.kind): t for t in tasks}
    assert by[("AAPL", "topup")].start == D(2026, 11, 15) and by[
        ("AAPL", "topup")
    ].end == D(2026, 11, 20)
    assert by[("AAPL", "backfill")].end == D(2017, 12, 31)
    assert ("MSFT", "topup") in by and ("MSFT", "backfill") not in by
    assert not any(t.ticker == "OLD" for t in tasks)  # retired and covered
    assert by[("REN", "first")].symbol == "REN2" and by[("NEW", "first")].start == D(
        2016, 1, 1
    )
    assert len(plan.plan_tasks(spans, states, overlap_days=3, full=True)) == len(spans)


def test_batches_group_identical_ranges_and_keep_one_ticker_per_symbol() -> None:
    tasks = [
        plan.FetchTask(
            f"T{i}", f"T{i}", D(2026, 11, 15), D(2026, 11, 20), None, "topup"
        )
        for i in range(5)
    ]
    tasks.append(
        plan.FetchTask("DUP", "T0", D(2026, 11, 15), D(2026, 11, 20), None, "topup")
    )
    tasks.append(
        plan.FetchTask(
            "OLD", "OLD", D(2016, 1, 1), D(2020, 1, 31), D(2020, 1, 31), "first"
        )
    )
    batches = plan.batch_tasks(tasks, per_request=4)
    assert [len(b.tasks) for b in batches] == [4, 2, 1]
    assert all(len(set(b.symbols)) == len(b.symbols) for b in batches)
    assert batches[-1].asof == D(2020, 1, 31)
    # one symbol over four years is about 1,070 weekdays: two pages at the observed density
    assert plan.estimate_pages(batches[-1], page_limit=10_000) == 2
    assert plan.estimate_pages(batches[0], page_limit=10_000) == 1  # a top-up
    big = plan.Batch(D(2016, 1, 1), D(2026, 12, 31), None, tuple(tasks[:4]))
    assert plan.estimate_pages(big, page_limit=1000) > 10


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def test_replace_range_is_idempotent_keeps_other_ranges_and_prune_clears(
    tmp_path: Path,
) -> None:
    target = store.AuctionStore(tmp_path)
    records = {"AAPL": [_aapl_day("2026-11-18"), _aapl_day("2026-11-19")]}
    frame = au.parse_records(
        records, {"AAPL": "AAPL"}, D(2026, 11, 18), D(2026, 11, 19)
    )
    first = target.replace_range("AAPL", frame, D(2026, 11, 18), D(2026, 11, 19))
    again = target.replace_range("AAPL", frame, D(2026, 11, 18), D(2026, 11, 19))
    assert first == again and first.n_days == 2 and first.n_prints == 12
    restated = au.parse_records(
        {"AAPL": [_aapl_day("2026-11-19", scale=2)]},
        {"AAPL": "AAPL"},
        D(2026, 11, 19),
        D(2026, 11, 19),
    )
    merged = target.replace_range("AAPL", restated, D(2026, 11, 19), D(2026, 11, 20))
    assert (
        merged.n_days == 1 and target.read_prints("AAPL")["size"].max() == 5378651 * 2
    )
    assert (
        target.stats("AAPL", D(2026, 11, 1), D(2026, 11, 30)).n_days == 2
    )  # the 18th is kept
    assert (
        target.replace_range(
            "AAPL", au.empty_prints(), D(2016, 1, 1), D(2016, 1, 2)
        ).n_prints
        == 0
    )
    assert target.stats("AAPL", D(2026, 11, 1), D(2026, 11, 30)).n_prints == 12
    assert target.prune("AAPL", [(D(2026, 11, 19), D(2026, 11, 30))]) == 6
    assert target.stats("AAPL", D(2026, 11, 1), D(2026, 11, 30)).first == D(
        2026, 11, 19
    )
    assert target.prune("AAPL", []) == 6 and not target.prints_path("AAPL").exists()
    with pytest.raises(ValueError, match="outside"):
        target.replace_range("AAPL", frame, D(2026, 11, 19), D(2026, 11, 19))


def test_state_round_trips_through_csv(tmp_path: Path) -> None:
    target = store.AuctionStore(tmp_path)
    state = store.SymbolState(
        "MMC",
        symbol="MMC",
        asset_type="stock",
        asof=D(2026, 1, 13),
        coverage_from=D(2017, 1, 3),
        n_prints=7,
        status="ok",
    )
    alias = store.SymbolState(
        "PSKY",
        symbol="CBS",
        asof=D(2019, 12, 4),
        unit="PSKY<CBS@2019-12-04",
        role="alias",
    )
    target.save_state({"MMC": state, alias.key: alias})
    assert target.load_state() == {"MMC": state, "PSKY<CBS@2019-12-04": alias}


# --------------------------------------------------------------------------- #
# refresh orchestration
# --------------------------------------------------------------------------- #
class _Universe:
    def __init__(self, spans: list[universe.SymbolSpan]) -> None:
        self.spans = spans

    def resolve(self, target: D) -> universe.UniverseResult:
        return universe.UniverseResult(tuple(self.spans), {"ZZZ": "no daily prices"})


class _FakeSource:
    """Answers with one modern day per weekday in range, for every requested symbol."""

    def __init__(self, fail: set[str] | None = None, auth: bool = False) -> None:
        self.calls: list[tuple] = []
        self.fail = fail or set()
        self.auth = auth

    def auctions(self, symbols, start, end, *, asof=None):
        self.calls.append((tuple(symbols), start, end, asof))
        if self.auth:
            raise AuthError("rejected", status=401)
        if self.fail & set(symbols):
            raise ApiError("boom", status=500)
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        weekdays = [d.isoformat() for d in days if d.weekday() < 5]
        return {s: [_aapl_day(d) for d in weekdays] for s in symbols}, 1


SUNDAY_EVENING = dt.datetime(2026, 11, 22, 20, 0, tzinfo=dt.timezone.utc)
MONDAY_1540_NEW_YORK = dt.datetime(2026, 11, 23, 20, 40, tzinfo=dt.timezone.utc)


def _refresh(
    tmp_path: Path,
    source: _FakeSource | None,
    *,
    run: bool,
    quiet: QuietSchedule | None = None,
    spans=None,
    now: dt.datetime = SUNDAY_EVENING,
):
    spans = spans or [
        _span("AAPL", D(2026, 11, 16), D(2026, 11, 20)),
        _span("BRK-B", D(2026, 11, 16), D(2026, 11, 20), symbol="BRK.B"),
    ]
    return refresh.refresh(
        _Universe(spans),
        store.AuctionStore(tmp_path),
        target=D(2026, 11, 20),
        run=run,
        overlap_days=3,
        per_request=100,
        page_limit=10_000,
        client_factory=(lambda: source) if source is not None else None,
        quiet=quiet,
        now=lambda: now,
    )


def test_refresh_dry_run_sends_nothing_and_a_run_stores_and_settles(
    tmp_path: Path,
) -> None:
    dry = _refresh(tmp_path, None, run=False)
    assert (
        len(dry.tasks) == 2
        and len(dry.batches) == 1
        and not (tmp_path / "auctions").exists()
    )
    source = _FakeSource()
    report = _refresh(tmp_path, source, run=True)
    assert len(source.calls) == 1 and report.daily_rows == 10 and not report.failed
    states = store.AuctionStore(tmp_path).load_state()
    assert (
        states["BRK-B"].status == "ok"
        and states["BRK-B"].symbol == "BRK.B"
        and states["BRK-B"].n_days == 5
    )
    assert store.AuctionStore(tmp_path).read_prints("BRK-B")[
        "ticker"
    ].unique().tolist() == ["BRK-B"]
    settled = _refresh(tmp_path, _FakeSource(), run=True)
    assert settled.in_sync and settled.daily_rows is None


def test_refresh_isolates_a_failed_batch_and_stops_on_auth_or_quiet(
    tmp_path: Path,
) -> None:
    spans = [
        _span("AAPL", D(2026, 11, 16), D(2026, 11, 20)),
        _span("OLD", D(2020, 1, 1), D(2020, 1, 31), asof=D(2020, 1, 31)),
    ]
    report = _refresh(tmp_path, _FakeSource(fail={"OLD"}), run=True, spans=spans)
    assert len(report.failed) == 1 and report.daily_rows == 5
    states = store.AuctionStore(tmp_path).load_state()
    assert (
        states["OLD"].status == "error"
        and "boom" in states["OLD"].detail
        and states["AAPL"].status == "ok"
    )
    with pytest.raises(AuthError):
        _refresh(tmp_path / "auth", _FakeSource(auth=True), run=True)
    quiet = QuietSchedule(["15:30-16:15"], "America/New_York")
    quiet_source = _FakeSource()
    stopped = _refresh(
        tmp_path / "quiet",
        quiet_source,
        run=True,
        quiet=quiet,
        now=MONDAY_1540_NEW_YORK,
    )
    assert (
        stopped.stopped == "quiet window 15:30-16:15 America/New_York"
        and not quiet_source.calls
    )
    weekend = _refresh(
        tmp_path / "weekend", _FakeSource(), run=True, quiet=quiet, now=SUNDAY_EVENING
    )
    assert not weekend.stopped and weekend.daily_rows == 10


def test_target_date_waits_for_the_evening_in_new_york() -> None:
    assert refresh.target_date(
        dt.datetime(2026, 11, 23, 23, 0, tzinfo=dt.timezone.utc)
    ) == D(
        2026, 11, 20
    )  # Mon 18:00 NY
    assert refresh.target_date(
        dt.datetime(2026, 11, 24, 2, 0, tzinfo=dt.timezone.utc)
    ) == D(
        2026, 11, 23
    )  # Mon 21:00 NY
    assert refresh.target_date(
        dt.datetime(2026, 11, 22, 12, 0, tzinfo=dt.timezone.utc)
    ) == D(
        2026, 11, 20
    )  # Sunday


# --------------------------------------------------------------------------- #
# views, qc and the CLI over a fixture eodhd root
# --------------------------------------------------------------------------- #
def _eodhd_root(root: Path) -> Path:
    (root / "intraday").mkdir(parents=True)
    pd.DataFrame(
        {
            "quarter_start": ["2026-10-01"] * 3,
            "ticker": ["AAPL", "BRK-B", "SPY"],
            "exchange": ["US"] * 3,
            "tier": ["T1", "T1", "context"],
            "role": ["trade", "trade", "signal"],
            "asset_type": ["stock", "stock", "etf"],
            "group": ["", "", "broad"],
        }
    ).to_parquet(root / "intraday" / "universe_membership.parquet")
    for lane, tickers in (("us_common", ["AAPL", "BRK-B"]), ("us_etf", ["SPY"])):
        (root / lane).mkdir()
        rows = [
            {
                "date": d,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "adjusted_close": 100.5,
                "volume": 1e6,
                "ticker": t,
                "exchange": "US",
            }
            for t in tickers
            for d in (
                "2026-11-16",
                "2026-11-17",
                "2026-11-18",
                "2026-11-19",
                "2026-11-20",
            )
        ]
        pd.DataFrame(rows).to_parquet(root / lane / "prices_daily.parquet")
    return root


def test_cli_plans_fetches_and_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    eodhd = _eodhd_root(tmp_path / "eodhd")
    monkeypatch.setenv("EODHD_DATA_ROOT", str(eodhd))
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "alpaca"))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setenv(KEY_ID_VAR, "PKID")
    monkeypatch.setenv(SECRET_VAR, "SECRET")
    monkeypatch.setattr(
        cli, "_now", lambda: dt.datetime(2026, 11, 22, 20, 0, tzinfo=dt.timezone.utc)
    )
    monkeypatch.setattr(
        cli.config, "load_settings", lambda: Settings(history_start=D(2026, 11, 16))
    )
    source = _FakeSource()
    monkeypatch.setattr(cli, "_client", lambda settings: source)
    assert cli.main(["fetch"]) == 0
    assert "nothing sent" in capsys.readouterr().out and not source.calls
    assert cli.main(["fetch", "--run"]) == 0
    out = capsys.readouterr().out
    assert "daily file rebuilt: 15" in out and len(source.calls) == 1
    assert source.calls[0][0] == ("AAPL", "BRK.B", "SPY")
    assert (
        cli.main(["fetch", "--run"]) == 0
        and "Everything in sync." in capsys.readouterr().out
    )
    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (
        payload["tickers"] == 3
        and payload["by_status"]["ok"] == 3
        and payload["key_configured"]
        and payload["daily_rows"] == 15
    )
    assert "SECRET" not in json.dumps(payload)
    assert cli.main(["qc", "--crosscheck"]) in (0, 1)
    out = capsys.readouterr().out
    assert (
        "eodhd daily open vs official opening cross: n 15" in out
        and "differ (> half a cent) 100%" in out
    )
    con = duckdb.connect()
    assert views.register(con, root=tmp_path / "alpaca") == {
        views.PRINTS_VIEW: True,
        views.DAILY_VIEW: True,
        views.STATE_VIEW: True,
        views.COVERAGE_VIEW: True,
    }
    assert con.execute(f"SELECT count(*) FROM {views.DAILY_VIEW}").fetchone()[0] == 15
    assert (
        con.execute(
            f"SELECT count(*) FROM {views.PRINTS_VIEW} WHERE condition IN ('O', '6')"
        ).fetchone()[0]
        == 30
    )
    assert cli.main(["fetch", "extra"]) == 2
    assert cli.main(["fetch", "--to", "nope"]) == 2
    assert cli.main(["nope"]) == 2 and cli.main(["--help"]) == 0
    monkeypatch.delenv(KEY_ID_VAR)
    monkeypatch.setattr(
        "alpaca.auth.read_user_env", lambda name, env=None, registry=None: ""
    )
    assert cli.main(["fetch", "--run", "--full"]) == 1
    assert "not set" in capsys.readouterr().out


def test_qc_flags_errors_and_missing_daily_file(tmp_path: Path) -> None:
    target = store.AuctionStore(tmp_path)
    assert qc.findings(target, target=D(2026, 11, 20))[0][1] == "empty"
    target.save_state(
        {"AAPL": store.SymbolState("AAPL", n_prints=3, status="error", detail="boom")}
    )
    checks = {c for _, c, _ in qc.findings(target, target=D(2026, 11, 20))}
    assert {"files", "fetch"} <= checks


def test_scheduler_admits_the_alpaca_family(tmp_path: Path, monkeypatch) -> None:
    from scheduler.commands import (
        CommandValidationError,
        ValidationContext,
        default_registry,
    )

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    context = ValidationContext.current(
        _REPO_ROOT, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    fetch = registry.validate(
        "alpaca", "fetch", ["--tickers", "AAPL,MSFT", "--run"], context
    )
    names = {b.name: b for b in fetch.bindings}
    claims = {c.resource_id: c.mode for c in fetch.spec.resources}
    assert Path(names["alpaca_data_root"].resolved_value).name == "alpaca"
    assert claims[names["alpaca_data_root"].resource_id] == "exclusive"
    assert claims[names["eodhd_data_root"].resource_id] == "shared"
    assert fetch.spec.network and fetch.spec.mutation
    assert registry.validate("alpaca", "status", ["--json"], context).spec.resources
    assert registry.validate("alpaca", "qc", ["--crosscheck"], context).spec.resources
    with pytest.raises(CommandValidationError, match="requires its own --run"):
        registry.validate("alpaca", "fetch", [], context)
    with pytest.raises(CommandValidationError, match="YYYY-MM-DD"):
        registry.validate("alpaca", "fetch", ["--to", "soon", "--run"], context)
    with pytest.raises(CommandValidationError):
        registry.validate("alpaca", "fetch", ["--ignore-quiet", "--run"], context)
    # the readiness preflight names a missing key pair, and only then
    monkeypatch.setattr(
        "alpaca.auth.read_user_env",
        lambda name, env=None, registry=None: (env or {}).get(name, ""),
    )
    codes = {
        f.code
        for f in registry.preflight(fetch.spec, "readiness", _execution(fetch, context))
    }
    assert "alpaca_key_missing" in codes
    keyed = ValidationContext.current(
        _REPO_ROOT,
        Path(sys.executable),
        config,
        environment={KEY_ID_VAR: "PKID", SECRET_VAR: "SECRET"},
    )
    fetch = registry.validate("alpaca", "fetch", ["--run"], keyed)
    codes = {
        f.code
        for f in registry.preflight(fetch.spec, "readiness", _execution(fetch, keyed))
    }
    assert "alpaca_key_missing" not in codes


def _execution(validated, context):
    from scheduler.commands import ExecutionContext

    return ExecutionContext(validation=context, bindings=validated.bindings)


# --------------------------------------------------------------------------- #
# predecessor securities: overrides, segments, the alias gate, coverage
# --------------------------------------------------------------------------- #
from alpaca import coverage as cov  # noqa: E402


def test_the_shipped_overrides_parse_and_every_alias_names_a_reason() -> None:
    overrides = universe.load_overrides()
    assert overrides.aliases and overrides.listings
    assert all(a.note for a in overrides.aliases)
    assert all(item.note for item in overrides.listings)
    assert [a.symbol for a in overrides.aliases_of("PSKY")] == ["CBS", "VIAC", "PARA"]


def test_overrides_reject_overlaps_typos_and_bad_dates() -> None:
    ok = universe.parse_overrides(
        {
            "alias": [
                {"ticker": "X", "symbol": "OLD", "until": "2020-01-31", "note": "n"}
            ],
            "listed": [{"ticker": "Y", "auctions_from": "2021-01-04"}],
        }
    )
    assert ok.aliases[0].asof == D(2020, 1, 31)
    assert ok.listing_of("Y").auctions_from == D(2021, 1, 4)
    with pytest.raises(ValueError, match="overlap"):
        universe.parse_overrides(
            {
                "alias": [
                    {"ticker": "X", "symbol": "A", "until": "2020-01-31"},
                    {"ticker": "X", "symbol": "B", "until": "2021-01-31"},
                ]
            }
        )
    with pytest.raises(ValueError, match="needs"):
        universe.parse_overrides(
            {"alias": [{"ticker": "X", "symbol": "A", "untill": "2020-01-31"}]}
        )
    with pytest.raises(ValueError, match="not a date"):
        universe.parse_overrides({"listed": [{"ticker": "Y", "auctions_from": "soon"}]})
    with pytest.raises(ValueError, match="unknown table"):
        universe.parse_overrides({"aliases": []})


def _alias(
    ticker: str, symbol: str, until: D, start: D | None = None, asof: D | None = None
) -> universe.Alias:
    return universe.Alias(ticker, symbol, until, asof or until, start)


def test_segments_give_the_main_symbol_the_one_interval_the_aliases_leave() -> None:
    aliases = [
        _alias("PSKY", "CBS", D(2019, 12, 4)),
        _alias(
            "PSKY", "VIAC", D(2022, 2, 16), start=D(2019, 12, 5), asof=D(2022, 2, 15)
        ),
        _alias("PSKY", "PARA", D(2025, 8, 6), start=D(2022, 2, 17)),
    ]
    units = universe.segment(
        "PSKY", "stock", D(2016, 1, 1), D(2026, 11, 20), ("PSKY", None), aliases
    )
    assert [(u.symbol, u.start, u.end, u.role) for u in units] == [
        ("CBS", D(2016, 1, 1), D(2019, 12, 4), "alias"),
        ("VIAC", D(2019, 12, 5), D(2022, 2, 16), "alias"),
        ("PARA", D(2022, 2, 17), D(2025, 8, 6), "alias"),
        ("PSKY", D(2025, 8, 7), D(2026, 11, 20), "main"),
    ]
    assert units[1].unit == "PSKY<VIAC@2022-02-15" and units[3].unit == "PSKY"
    both_ends = universe.segment(
        "VIAC",
        "stock",
        D(2017, 1, 3),
        D(2022, 5, 27),
        ("VIAC", D(2022, 5, 27)),
        [
            _alias("VIAC", "CBS", D(2019, 12, 4)),
            _alias(
                "VIAC",
                "PARA",
                D(2026, 12, 31),
                start=D(2022, 2, 17),
                asof=D(2022, 5, 27),
            ),
        ],
    )
    assert [(u.symbol, u.start, u.end) for u in both_ends] == [
        ("CBS", D(2017, 1, 3), D(2019, 12, 4)),
        ("VIAC", D(2019, 12, 5), D(2022, 2, 16)),
        ("PARA", D(2022, 2, 17), D(2022, 5, 27)),
    ]
    with pytest.raises(ValueError, match="split"):
        universe.segment(
            "X",
            "stock",
            D(2016, 1, 1),
            D(2020, 1, 1),
            ("X", None),
            [_alias("X", "M", D(2018, 6, 1), start=D(2018, 1, 1))],
        )
    before = universe.segment(
        "X",
        "stock",
        D(2016, 1, 1),
        D(2020, 1, 1),
        ("X", None),
        [_alias("X", "OLD", D(2015, 6, 1))],
    )
    assert [u.symbol for u in before] == [
        "X"
    ]  # an alias before the extent is clipped away


def test_listings_move_the_extent_and_aliases_become_units() -> None:
    members = pd.DataFrame(
        {"ticker": ["FLUT", "VMW"], "asset_type": ["stock", "stock"]}
    )
    daily = pd.DataFrame(
        {
            "ticker": ["FLUT", "VMW"],
            "first": [D(2017, 1, 24), D(2017, 1, 3)],
            "last": [D(2026, 11, 20), D(2023, 11, 21)],
        }
    )
    overrides = universe.Overrides(
        aliases=(_alias("VMW", "VMW", D(2023, 10, 29)),),
        listings=(universe.Listing("FLUT", D(2024, 1, 29)),),
    )
    result = universe.spans_from_daily(
        members,
        daily,
        target=D(2026, 11, 20),
        history_start=D(2016, 1, 1),
        retired_after_days=10,
        overrides=overrides,
    )
    assert result.extents == {
        "FLUT": (D(2024, 1, 29), D(2026, 11, 20)),
        "VMW": (D(2017, 1, 3), D(2023, 11, 21)),
    }
    vmw = [u for u in result.spans if u.ticker == "VMW"]
    assert [(u.unit, u.start, u.end, u.asof) for u in vmw] == [
        ("VMW<VMW@2023-10-29", D(2017, 1, 3), D(2023, 10, 29), D(2023, 10, 29)),
        ("VMW", D(2023, 10, 30), D(2023, 11, 21), D(2023, 11, 21)),
    ]


def test_coverage_counts_the_store_sessions_and_the_late_start() -> None:
    days = [D(2026, 11, d) for d in (16, 17, 18, 19, 20)]
    store_days = pd.DataFrame({"ticker": ["A"] * 5 + ["B"] * 5, "date": days * 2})
    daily = pd.DataFrame(
        {
            "ticker": ["A"] * 5 + ["B"] * 2,
            "date": days + days[3:],
            "close_price": [1.0] * 4 + [None] + [1.0, 1.0],
        }
    )
    extents = {name: (days[0], days[-1]) for name in ("A", "B", "C")}
    frame = cov.assess(
        daily, store_days, extents, min_coverage=0.75, max_gap_sessions=1
    ).set_index("ticker")
    assert frame.loc["A", "close_days"] == 4 and frame.loc["A", "late_sessions"] == 0
    assert frame.loc["A", "status"] == "ok"
    assert frame.loc["B", "late_sessions"] == 3 and frame.loc["B", "status"] == "thin"
    assert frame.loc["B", "first_close"] == days[3]
    assert (
        frame.loc["C", "expected_days"] == 0 and frame.loc["C", "status"] == "ok"
    )  # nothing priced


def test_verify_closes_compares_the_chosen_cross_with_the_store_close() -> None:
    prints = au.parse_records(
        {"X": [_aapl_day("2026-11-18"), _aapl_day("2026-11-19")]},
        {"X": "X"},
        D(2026, 11, 18),
        D(2026, 11, 19),
    )
    closes = pd.DataFrame(
        {"date": [D(2026, 11, 18), D(2026, 11, 19)], "close": [352.45, 300.0]}
    )
    match = cov.verify_closes(prints, closes, tolerance=0.002)
    assert (match.compared, match.matched, match.share) == (2, 1, 0.5)
    assert cov.verify_closes(au.empty_prints(), closes, tolerance=0.002).compared == 0


class _Closes:
    """The store's closes: ``level`` on every weekday (352.45 is the fixture's closing cross)."""

    def __init__(self, level: float = 352.45) -> None:
        self.level = level

    def closes(self, tickers, start, end):
        days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
        rows = [(t, d, self.level) for t in tickers for d in days if d.weekday() < 5]
        return pd.DataFrame(rows, columns=["ticker", "date", "close"])


class _AliasUniverse:
    def __init__(self, spans, extents):
        self.spans, self.extents = spans, extents

    def resolve(self, target):
        return universe.UniverseResult(tuple(self.spans), {}, self.extents)


PSKY_EXTENT = {"PSKY": (D(2026, 11, 16), D(2026, 11, 20))}
PSKY_MAIN = universe.SymbolSpan(
    "PSKY", "PSKY", "stock", D(2026, 11, 18), D(2026, 11, 20), None, "main"
)
PSKY_CBS = universe.SymbolSpan(
    "PSKY", "CBS", "stock", D(2026, 11, 16), D(2026, 11, 17), D(2019, 12, 4), "alias"
)


def _alias_refresh(tmp_path: Path, closes: _Closes, spans=None, **kwargs):
    return refresh.refresh(
        _AliasUniverse(spans or [PSKY_CBS, PSKY_MAIN], PSKY_EXTENT),
        store.AuctionStore(tmp_path),
        target=D(2026, 11, 20),
        run=True,
        overlap_days=3,
        per_request=100,
        page_limit=10_000,
        client_factory=lambda: _FakeSource(),
        closes=closes,
        now=lambda: SUNDAY_EVENING,
        **kwargs,
    )


def test_a_matching_alias_is_stored_under_the_ticker_and_coverage_is_written(
    tmp_path: Path,
) -> None:
    report = _alias_refresh(tmp_path, _Closes())
    assert not report.refused and report.daily_rows == 5
    target = store.AuctionStore(tmp_path)
    prints = target.read_prints("PSKY")
    assert set(prints.loc[prints["date"] <= D(2026, 11, 17), "symbol"]) == {"CBS"}
    assert set(prints.loc[prints["date"] >= D(2026, 11, 18), "symbol"]) == {"PSKY"}
    states = target.load_state()
    alias = states["PSKY<CBS@2019-12-04"]
    assert alias.role == "alias" and alias.n_days == 2 and alias.status == "ok"
    assert states["PSKY"].n_days == 3 and states["PSKY"].first_data == D(2026, 11, 18)
    assert report.coverage is not None and list(report.coverage["status"]) == ["ok"]
    assert target.read_coverage().loc[0, "close_days"] == 5
    assert not qc.coverage_findings(target)
    # a later segmentation without the alias prunes its prints, and the coverage turns thin
    later = _alias_refresh(
        tmp_path,
        _Closes(),
        spans=[PSKY_MAIN],
        full=True,
        policy=refresh.CoveragePolicy(max_gap_sessions=1),
    )
    assert later.pruned == 12 and later.thin() == ["PSKY"]
    assert "thin" in qc.coverage_findings(target)[0][2]


def test_a_span_that_moves_without_a_fetch_still_prunes_its_old_prints(
    tmp_path: Path,
) -> None:
    whole = _span("AAPL", D(2026, 11, 16), D(2026, 11, 20), asof=D(2026, 11, 20))
    _refresh(tmp_path, _FakeSource(), run=True, spans=[whole])
    # a [[listed]] start (or a store series that now ends earlier) shrinks the span
    later = _span("AAPL", D(2026, 11, 18), D(2026, 11, 20), asof=D(2026, 11, 20))
    source = _FakeSource()
    report = _refresh(tmp_path, source, run=True, spans=[later])
    assert not report.tasks and not source.calls  # covered: nothing to fetch
    assert report.pruned == 12 and report.daily_rows == 3
    kept = store.AuctionStore(tmp_path).read_prints("AAPL")
    assert kept["date"].min() == D(2026, 11, 18)
    assert _refresh(tmp_path, _FakeSource(), run=True, spans=[later]).pruned == 0


def test_a_ticker_limited_run_keeps_the_other_tickers_coverage(
    tmp_path: Path,
) -> None:
    _alias_refresh(tmp_path, _Closes())
    aapl = _span("AAPL", D(2026, 11, 16), D(2026, 11, 20))

    def limited(partial: bool) -> refresh.RefreshReport:
        return refresh.refresh(
            _AliasUniverse([aapl], {"AAPL": (aapl.start, aapl.end)}),
            store.AuctionStore(tmp_path),
            target=D(2026, 11, 20),
            run=True,
            overlap_days=3,
            per_request=100,
            page_limit=10_000,
            client_factory=lambda: _FakeSource(),
            closes=_Closes(),
            now=lambda: SUNDAY_EVENING,
            partial=partial,
        )

    report = limited(True)
    assert list(report.coverage["ticker"]) == ["AAPL", "PSKY"]
    assert list(store.AuctionStore(tmp_path).read_coverage()["ticker"]) == [
        "AAPL",
        "PSKY",
    ]
    limited(False)  # a whole-universe run writes exactly its tickers
    assert list(store.AuctionStore(tmp_path).read_coverage()["ticker"]) == ["AAPL"]


def test_an_alias_whose_closes_do_not_match_the_store_is_refused(
    tmp_path: Path,
) -> None:
    report = _alias_refresh(tmp_path, _Closes(level=10.0))
    assert report.refused == ["PSKY<CBS@2019-12-04"]
    target = store.AuctionStore(tmp_path)
    state = target.load_state()["PSKY<CBS@2019-12-04"]
    assert (
        state.status == "error"
        and "match the store's PSKY on 0 of 2 days" in state.detail
    )
    assert set(target.read_prints("PSKY")["symbol"]) == {
        "PSKY"
    }  # nothing of the alias stored
    assert "refused" in set(pd.read_csv(target.audit_path)["status"])


def test_coverage_flags_a_gap_in_the_middle_unless_it_is_reviewed() -> None:
    days = [D(2026, 11, d) for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]
    store_days = pd.DataFrame({"ticker": ["A"] * 10, "date": days})
    crossed = days[:3] + days[7:]  # four sessions in the middle without a closing cross
    daily = pd.DataFrame(
        {"ticker": ["A"] * len(crossed), "date": crossed, "close_price": 1.0}
    )
    extents = {"A": (days[0], days[-1])}
    plain = cov.assess(
        daily, store_days, extents, min_coverage=0.5, max_gap_sessions=3
    ).iloc[0]
    assert (
        plain["late_sessions"] == 0
        and plain["longest_gap"] == 4
        and plain["status"] == "thin"
    )
    assert (plain["gap_from"], plain["gap_to"]) == (days[3], days[6])
    reviewed = cov.assess(
        daily,
        store_days,
        extents,
        min_coverage=0.5,
        max_gap_sessions=3,
        gaps={"A": [(days[3], days[6])]},
    ).iloc[0]
    assert reviewed["longest_gap"] == 0 and reviewed["reviewed_gap_sessions"] == 4
    assert reviewed["expected_days"] == 6 and reviewed["status"] == "ok"


def test_gaps_parse_and_reach_the_universe_result() -> None:
    overrides = universe.parse_overrides(
        {
            "gap": [
                {
                    "ticker": "W",
                    "from": "2019-05-20",
                    "until": "2021-06-01",
                    "note": "OTC",
                }
            ]
        }
    )
    assert overrides.gaps_of("W") == [(D(2019, 5, 20), D(2021, 6, 1))]
    with pytest.raises(ValueError, match="after"):
        universe.parse_overrides(
            {"gap": [{"ticker": "W", "from": "2021-01-01", "until": "2020-01-01"}]}
        )
    members = pd.DataFrame({"ticker": ["W"], "asset_type": ["stock"]})
    daily = pd.DataFrame(
        {"ticker": ["W"], "first": [D(2016, 1, 4)], "last": [D(2026, 11, 20)]}
    )
    result = universe.spans_from_daily(
        members,
        daily,
        target=D(2026, 11, 20),
        history_start=D(2016, 1, 1),
        retired_after_days=10,
        overrides=overrides,
    )
    assert result.gaps == {"W": ((D(2019, 5, 20), D(2021, 6, 1)),)}
    assert universe.load_overrides().gaps_of(
        "WFRD"
    )  # the shipped file has the reviewed WFRD stretch
