"""The Alpaca-checked corrections overlay over the US daily lanes (eodhd/price_fixes.py)."""

from __future__ import annotations

import datetime as dt
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import alpaca  # noqa: E402,F401  (puts eodhd/ on sys.path)
from alpaca import client  # noqa: E402
from alpaca.auth import Credentials  # noqa: E402

import price_fixes as pf  # type: ignore[import-not-found]  # noqa: E402
from daily_store import DailyStore  # type: ignore[import-not-found]  # noqa: E402

duckdb = pytest.importorskip("duckdb")

DAYS = pd.bdate_range("2024-01-02", periods=60)


def _wave(n: int, base: float = 10.0) -> np.ndarray:
    return base * (1 + 0.01 * np.sin(np.arange(n)))


def _eod(dates, close, adj=None, volume=None) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.DatetimeIndex(dates),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "adjusted_close": close if adj is None else np.asarray(adj, dtype=float),
            "volume": (
                np.full(len(close), 1000.0)
                if volume is None
                else np.asarray(volume, float)
            ),
        }
    )


def _bars(dates, close, volume=1000.0) -> pd.DataFrame:
    close = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": pd.DatetimeIndex(dates),
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": np.full(len(close), volume),
        }
    )


def _check(eod, raw, adj=None, **kwargs) -> pf.TickerCheck:
    return pf.check_ticker(
        eod, raw, raw if adj is None else adj, lane="us_common", ticker="T", **kwargs
    )


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #
def test_agreeing_sources_leave_the_ticker_alone() -> None:
    px = _wave(60)
    result = _check(_eod(DAYS, px), _bars(DAYS, px))
    assert result.fixes.empty
    assert result.summary["status"] == pf.CHECKED
    assert result.summary["identity"] == 1.0


def test_placeholders_after_the_last_trade_are_dropped() -> None:
    # QRVO: two bars without volume after the last session, the second at 18x
    px = _wave(60)
    eod_px = np.r_[px[:58], px[57], px[57] * 18]
    vol = np.r_[np.full(58, 1000.0), 0.0, 0.0]
    result = _check(_eod(DAYS, eod_px, volume=vol), _bars(DAYS[:58], px[:58]))
    fixes = result.fixes
    assert list(fixes["action"]) == [pf.DROP, pf.DROP]
    assert set(fixes["reason"]) == {pf.TAIL_PLACEHOLDER}
    assert list(fixes["date"]) == list(DAYS[58:])
    assert fixes["close"].isna().all() and list(fixes["orig_close"]) == list(
        eod_px[58:]
    )


def test_another_listing_after_the_last_trade_is_dropped_unless_a_split_explains_it() -> (
    None
):
    # WILC: the series carries on at ~300x (another listing, another currency unit)
    px = _wave(60)
    eod_px = np.r_[px[:50], px[49] * 300 * _wave(10, 1.0)]
    eod = _eod(DAYS, eod_px)
    raw = _bars(DAYS[:50], px[:50])
    result = _check(eod, raw)
    assert len(result.fixes) == 10
    assert set(result.fixes["reason"]) == {pf.TAIL_LISTING}

    split = [(DAYS[50], 1 / 300)]
    explained = _check(eod, raw, splits=split)
    assert explained.fixes.empty


def test_a_fall_after_the_last_trade_is_kept_as_a_possible_move_to_otc() -> None:
    px = _wave(60)
    eod_px = np.r_[px[:50], px[49] * 0.2 * _wave(10, 1.0)]  # -80%, still trading
    result = _check(_eod(DAYS, eod_px), _bars(DAYS[:50], px[:50]))
    assert result.fixes.empty


def test_an_interior_bar_without_volume_is_dropped_only_when_its_price_moved() -> None:
    px = _wave(60)
    eod_px = px.copy()
    eod_px[20] = px[19] * 2.0  # a placeholder at a new price
    eod_px[30] = px[29]  # a flat placeholder
    eod_px[40] = px[39] * 1.2  # a moderate move: a thin name's quote, not proof
    vol = np.full(60, 1000.0)
    vol[[20, 30, 40]] = 0.0
    alp_days = DAYS.delete([20, 30, 40])
    alp_px = np.delete(px, [20, 30, 40])
    result = _check(_eod(DAYS, eod_px, volume=vol), _bars(alp_days, alp_px))
    drops = result.fixes[result.fixes["action"] == pf.DROP]
    assert list(drops["date"]) == [DAYS[20]]
    assert set(drops["reason"]) == {pf.INTERIOR_PLACEHOLDER}


def test_a_wrong_close_takes_alpacas_bar_and_split_restated_volume() -> None:
    px = _wave(60, 28.0)
    eod_px = px.copy()
    eod_px[20] = 30.5  # stale price, no volume
    vol = np.full(60, 1000.0)
    vol[20] = 0.0
    adj = _bars(DAYS, px, volume=2000.0)  # split-restated volume
    result = _check(_eod(DAYS, eod_px, volume=vol), _bars(DAYS, px), adj)
    fixes = result.fixes
    assert len(fixes) == 1
    row = fixes.iloc[0]
    assert row["date"] == DAYS[20] and row["action"] == pf.REPLACE
    assert row["close"] == pytest.approx(px[20])
    assert row["adjusted_close"] == pytest.approx(px[20])
    assert row["volume"] == 2000.0
    assert row["orig_close"] == 30.5
    assert row["reason"] == "close,adjustment"


def test_a_moderate_close_gap_on_a_traded_vendor_bar_is_kept() -> None:
    # ACSI: a thin ETF; Alpaca's stale odd-lot print sits 8% under the vendor's close
    px = _wave(60, 38.0)
    alp = px.copy()
    alp[20:28] = px[19]  # Alpaca's close stuck for eight sessions
    result = _check(_eod(DAYS, px), _bars(DAYS, alp))
    assert result.fixes.empty
    # the same gap on a vendor bar without volume, against a session with too
    # few shares to count, is kept as well
    vol = np.full(60, 1000.0)
    vol[25] = 0.0
    thin = _check(_eod(DAYS, px, volume=vol), _bars(DAYS, alp, volume=100.0))
    assert thin.fixes.empty


def test_a_gross_close_gap_is_fixed_even_with_volume() -> None:
    # QUBX 2026-09-21: the vendor applied a 1:4 split to the close a day early
    px = _wave(60, 8.0)
    eod_px = px.copy()
    eod_px[30] = px[30] * 4
    result = _check(_eod(DAYS, eod_px, adj=eod_px), _bars(DAYS, px))
    fixed = result.fixes.set_index("date")
    assert fixed.loc[DAYS[30], "close"] == pytest.approx(px[30])
    assert fixed.loc[DAYS[30], "adjusted_close"] == pytest.approx(px[30])
    assert "close" in fixed.loc[DAYS[30], "reason"]


def test_a_split_missing_from_the_adjusted_history_is_rechained() -> None:
    # QUBX: a 1:4 reverse split on day 40; EODHD's adjusted closes before it are not scaled
    raw_px = np.r_[_wave(40, 6.0), _wave(20, 24.0)]
    alp_adj = np.r_[_wave(40, 6.0) * 4, _wave(20, 24.0)]
    eod = _eod(DAYS, raw_px, adj=raw_px)
    result = _check(eod, _bars(DAYS, raw_px), _bars(DAYS, alp_adj))
    fixes = result.fixes
    assert list(fixes["date"]) == list(DAYS[:40])
    assert set(fixes["reason"]) == {pf.ADJUSTMENT}
    assert np.allclose(fixes["adjusted_close"], alp_adj[:40])
    assert np.allclose(fixes["close"], raw_px[:40])  # the as-traded close stays
    assert result.summary["max_adjustment"] == pytest.approx(4.0)


def test_alpacas_larger_move_is_disputed_not_applied() -> None:
    px = _wave(60)
    alp_adj = np.r_[px[:30] / 3, px[30:]]  # Alpaca's own jump on day 30
    result = _check(_eod(DAYS, px), _bars(DAYS, px), _bars(DAYS, alp_adj))
    assert result.fixes.empty
    assert result.summary["disputed"] == 1


def test_a_shift_under_one_percent_keeps_the_vendor_history() -> None:
    # SPWR: two unscaled days around a split; the sources' returns across it
    # differ by 0.3%, which must not move years of history
    alp_adj = _wave(60, 5.0)
    eod_adj = alp_adj.copy()
    eod_adj[:30] *= 1.003
    eod_adj[30:32] /= 35
    result = _check(_eod(DAYS, alp_adj, adj=eod_adj), _bars(DAYS, alp_adj))
    assert list(result.fixes["date"]) == [DAYS[30], DAYS[31]]
    assert np.allclose(result.fixes["adjusted_close"], alp_adj[30:32], rtol=0.01)


def test_another_security_or_too_little_overlap_fixes_nothing() -> None:
    px = _wave(60)
    other = _check(_eod(DAYS, px), _bars(DAYS, px * 3))
    assert other.summary["status"] == pf.OTHER_SECURITY and other.fixes.empty
    few = _check(_eod(DAYS, px), _bars(DAYS[:10], px[:10]))
    assert few.summary["status"] == pf.FEW_DAYS and few.fixes.empty
    none = _check(_eod(DAYS, px), _bars([], []))
    assert none.summary["status"] == pf.NO_BARS


def test_sub_cent_vendor_closes_leave_too_few_days_to_identify() -> None:
    # APM: the vendor's rounded adjusted history is 0.0001 for years; only a
    # handful of recent days are comparable, too few to trust the identity
    px = _wave(60, 5.0)
    eod_px = np.r_[np.full(50, 0.0001), px[50:]]
    result = _check(_eod(DAYS, eod_px), _bars(DAYS, px))
    assert result.summary["status"] == pf.FEW_DAYS and result.fixes.empty


def test_bars_frame_puts_each_bar_on_its_new_york_session() -> None:
    frame = pf.bars_frame(
        [
            {"t": "2026-11-20T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10},
            {"t": "2026-07-01T04:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.4, "v": 11},
        ]
    )
    assert list(frame["date"]) == [
        pd.Timestamp("2026-07-01"),
        pd.Timestamp("2026-11-20"),
    ]
    assert pf.bars_frame([]).empty


# --------------------------------------------------------------------------- #
# overlay and readers
# --------------------------------------------------------------------------- #
def _lane(root: Path, lane: str, rows: pd.DataFrame) -> None:
    (root / lane).mkdir(parents=True, exist_ok=True)
    frame = rows.copy()
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    frame["exchange"] = "US"
    frame.to_parquet(root / lane / pf.PRICES_FILE, index=False)


def _fixes(root: Path, rows: list[dict[str, Any]]) -> None:
    frame = pf.empty_fixes()
    frame = pd.concat([frame, pd.DataFrame(rows)], ignore_index=True)
    checks = pd.DataFrame(
        [
            {c: None for c in pf.CHECK_COLUMNS}
            | {"lane": r["lane"], "ticker": r["ticker"]}
            for r in rows
        ]
    ).drop_duplicates(["lane", "ticker"])
    pf.write_results(root, checks, frame)


def _three_days(root: Path) -> None:
    d = pd.DatetimeIndex(["2026-10-01", "2026-10-02", "2026-10-05"])
    rows = _eod(d, [10.0, 11.0, 2070.0]).assign(ticker="AAA")
    _lane(root, "us_common", rows)


def _fix(
    day: str, action: str, close: float | None, orig: float, **kw: Any
) -> dict[str, Any]:
    return {
        "lane": "us_common",
        "ticker": "AAA",
        "date": pd.Timestamp(day),
        "action": action,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adjusted_close": close,
        "volume": 5.0 if close else None,
        "orig_close": orig,
        "orig_adjusted_close": orig,
        "reason": "test",
        "source": pf.SOURCE,
        "checked_at": "now",
        **kw,
    }


def test_overlay_drops_and_replaces_only_the_rows_it_was_made_against(
    tmp_path: Path,
) -> None:
    _three_days(tmp_path)
    _fixes(
        tmp_path,
        [
            _fix("2026-10-01", pf.REPLACE, 9.0, 99.0),  # stale: EODHD's row is 10.0
            _fix("2026-10-02", pf.REPLACE, 12.0, 11.0),
            _fix("2026-10-05", pf.DROP, None, 2070.0),
        ],
    )
    src = f"read_parquet('{(tmp_path / 'us_common' / pf.PRICES_FILE).as_posix()}')"
    sql = pf.overlay_sql(src, "us_common", pf.fixes_path(tmp_path))
    got = (
        duckdb.connect()
        .execute(f"SELECT date, close, volume FROM {sql} ORDER BY date")
        .fetchall()
    )
    assert got == [("2026-10-01", 10.0, 1000.0), ("2026-10-02", 12.0, 5.0)]
    # another lane, or no fixes file: the source comes back unchanged
    assert pf.overlay_sql(src, "uk_eu", pf.fixes_path(tmp_path)) == src
    assert pf.overlay_sql(src, "us_common", tmp_path / "none.parquet") == src


def test_daily_store_reads_prices_with_the_fixes_unless_asked_not_to(
    tmp_path: Path,
) -> None:
    _three_days(tmp_path)
    _fixes(tmp_path, [_fix("2026-10-05", pf.DROP, None, 2070.0)])
    start, end = dt.date(2026, 10, 1), dt.date(2026, 10, 9)
    fixed = DailyStore(tmp_path).prices(start, end, columns=("close",))
    vendor = DailyStore(tmp_path, fixes=False).prices(start, end, columns=("close",))
    assert list(fixed["close"]) == [10.0, 11.0]
    assert list(vendor["close"]) == [10.0, 11.0, 2070.0]


def test_stale_fixes_are_found_and_merging_replaces_rechecked_pairs(
    tmp_path: Path,
) -> None:
    _three_days(tmp_path)
    rows = [
        _fix("2026-10-01", pf.REPLACE, 9.0, 99.0),
        _fix("2026-10-05", pf.DROP, None, 2070.0),
    ]
    rows.append({**_fix("2026-10-01", pf.REPLACE, 1.0, 1.0), "ticker": "BBB"})
    _fixes(tmp_path, rows)
    con = duckdb.connect()
    # AAA's replace was made against 99.0 (EODHD holds 10.0); BBB has no row at all
    assert set(pf.stale_tickers(con, tmp_path, "us_common")) == {("AAA",), ("BBB",)}
    checks = pd.DataFrame(
        [{c: None for c in pf.CHECK_COLUMNS} | {"lane": "us_common", "ticker": "AAA"}]
    )
    merged_checks, merged_fixes = pf.merge_results(tmp_path, checks, pf.empty_fixes())
    assert set(merged_fixes["ticker"]) == {"BBB"}  # AAA re-checked clean: its fixes go
    assert set(merged_checks["ticker"]) == {"AAA", "BBB"}


def test_a_second_run_merges_into_the_stored_overlay(tmp_path: Path) -> None:
    # the stored files hold dates; a run's rows hold timestamps: both must merge
    _three_days(tmp_path)
    _fixes(tmp_path, [_fix("2026-10-05", pf.DROP, None, 2070.0)])
    checks = pd.DataFrame(
        [
            {c: None for c in pf.CHECK_COLUMNS}
            | {
                "lane": "us_common",
                "ticker": "BBB",
                "alpaca_last": pd.Timestamp("2026-10-02"),
                "asof": dt.date(2026, 10, 2),
            }
        ]
    )
    new = pd.DataFrame(
        [{**_fix("2026-10-02", pf.REPLACE, 12.0, 11.0), "ticker": "BBB"}]
    )
    merged_checks, merged_fixes = pf.merge_results(tmp_path, checks, new)
    pf.write_results(tmp_path, merged_checks, merged_fixes)
    stored = pd.read_parquet(pf.fixes_path(tmp_path))
    assert sorted(stored["ticker"]) == ["AAA", "BBB"]


def test_recent_mode_selects_suspicious_bars_and_stale_fixes(tmp_path: Path) -> None:
    today = dt.date(2026, 10, 9)
    d = pd.bdate_range("2026-09-01", "2026-10-08")
    calm = _eod(d, _wave(len(d))).assign(ticker="CALM")
    px = _wave(len(d))
    px[-2:] *= 3  # a 3x move inside the window
    jumpy = _eod(d, px).assign(ticker="JUMP")
    _lane(tmp_path, "us_common", pd.concat([calm, jumpy]))
    con = duckdb.connect()
    pairs = pf.select_pairs(
        con, tmp_path, mode="recent", since=today - dt.timedelta(days=5)
    )
    assert pairs == [("us_common", "JUMP")]
    assert ("us_common", "CALM") in pf.select_pairs(con, tmp_path, mode="all")


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    return [
        {"t": f"{d:%Y-%m-%d}T05:00:00Z", "o": c, "h": c, "l": c, "c": c, "v": v}
        for d, c, v in zip(frame["date"], frame["close"], frame["volume"])
    ]


def test_plan_units_asks_retired_symbols_as_of_their_last_traded_day() -> None:
    today = dt.date(2026, 11, 23)
    units = pf.plan_units(
        {
            "NOW": (dt.date(2026, 11, 20), dt.date(2026, 11, 20)),
            "OLD": (dt.date(2026, 10, 6), dt.date(2026, 10, 2)),
        },
        today=today,
        retired_after_days=10,
        per_request=100,
    )
    assert [(u.tickers, u.asof, u.end) for u in units] == [
        (["NOW"], None, today),
        (["OLD"], dt.date(2026, 10, 2), dt.date(2026, 10, 6)),
    ]


def _store_for_run(root: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = pd.bdate_range("2026-08-03", periods=60)
    good = _eod(d, _wave(60)).assign(ticker="GOOD")
    old_px = np.r_[_wave(58), 10.0, 180.0]
    old = _eod(d, old_px, volume=np.r_[np.full(58, 1000.0), 0, 0]).assign(ticker="OLD")
    _lane(root, "us_common", good)
    _lane(root, "us_extended", old)
    return good, old


def test_run_checks_each_pair_and_finds_the_placeholders(tmp_path: Path) -> None:
    good, old = _store_for_run(tmp_path)
    calls: list[tuple[tuple[str, ...], dt.date | None, str]] = []

    def fetch(symbols, start, end, asof, adjustment):
        calls.append((tuple(symbols), asof, adjustment))
        out = {}
        if "GOOD" in symbols:
            out["GOOD"] = _records(good)
        if "OLD" in symbols:
            out["OLD"] = _records(old.iloc[:58])
        return out

    report = pf.run(
        tmp_path,
        [("us_common", "GOOD"), ("us_extended", "OLD")],
        fetch=fetch,
        symbol_of=lambda t: t,
        today=dt.date(2026, 11, 23),
    )
    assert {c["ticker"]: c["status"] for c in report.checks} == {
        "GOOD": pf.CHECKED,
        "OLD": pf.CHECKED,
    }
    fixes = report.fixes_frame()
    assert set(fixes["ticker"]) == {"OLD"} and set(fixes["action"]) == {pf.DROP}
    old_asof = {asof for symbols, asof, _a in calls if symbols == ("OLD",)}
    assert old_asof == {pd.Timestamp(old["date"].iloc[57]).date()}
    checks, merged = pf.merge_results(tmp_path, report.checks_frame(), fixes)
    pf.write_results(tmp_path, checks, merged)
    assert len(pd.read_parquet(pf.fixes_path(tmp_path))) == 2


def test_run_stops_in_a_quiet_window_and_survives_a_failed_request(
    tmp_path: Path,
) -> None:
    good, _old = _store_for_run(tmp_path)
    pairs = [("us_common", "GOOD"), ("us_extended", "OLD")]
    quiet = pf.run(
        tmp_path,
        pairs,
        fetch=lambda *a: pytest.fail("no request inside a quiet window"),
        symbol_of=lambda t: t,
        today=dt.date(2026, 11, 23),
        quiet=lambda: "15:30-16:15 America/New_York",
    )
    assert quiet.stopped and not quiet.checks

    def flaky(symbols, start, end, asof, adjustment):
        if "OLD" in symbols:
            raise RuntimeError("server error: try later")
        return {"GOOD": _records(good)}

    report = pf.run(
        tmp_path, pairs, fetch=flaky, symbol_of=lambda t: t, today=dt.date(2026, 11, 23)
    )
    assert [c["ticker"] for c in report.checks] == ["GOOD"]
    assert "OLD" in report.failed

    class Fatal(Exception):
        pass

    def denied(*_a):
        raise Fatal("key rejected")

    with pytest.raises(Fatal):
        pf.run(
            tmp_path,
            pairs,
            fetch=denied,
            symbol_of=lambda t: t,
            today=dt.date(2026, 11, 23),
            fatal=(Fatal,),
        )


# --------------------------------------------------------------------------- #
# Alpaca bars client and the scheduler contract
# --------------------------------------------------------------------------- #
class _Response:
    def __init__(self, payload: Any) -> None:
        self.status_code = 200
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, payloads: list[Any]) -> None:
        self.payloads = list(payloads)
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append({"url": url, **kwargs})
        return _Response(self.payloads.pop(0))


def test_bars_client_pages_and_sends_the_adjustment() -> None:
    bar = {"t": "2026-11-20T05:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}
    session = _Session(
        [
            {"bars": {"AAA": [bar]}, "next_page_token": "p2"},
            {"bars": {"AAA": [bar], "BBB": [bar]}, "next_page_token": None},
        ]
    )
    bars_client = client.BarsClient(
        Credentials("PKID", "SECRET"),
        data_url="https://data.example/v2/stocks",
        session=session,
        sleep=lambda s: None,
    )
    records, pages = bars_client.bars(
        ["AAA", "BBB"],
        dt.date(2026, 1, 1),
        dt.date(2026, 11, 20),
        adjustment="all",
        asof=dt.date(2026, 10, 2),
    )
    assert pages == 2 and len(records["AAA"]) == 2 and len(records["BBB"]) == 1
    first = session.calls[0]
    assert first["url"].endswith("/bars")
    assert (
        first["params"]["adjustment"] == "all"
        and first["params"]["timeframe"] == "1Day"
    )
    assert first["params"]["asof"] == "2026-10-02"
    assert session.calls[1]["params"]["page_token"] == "p2"
    with pytest.raises(ValueError):
        bars_client.bars(
            ["AAA"], dt.date(2026, 1, 1), dt.date(2026, 1, 2), adjustment="bogus"
        )


def test_scheduler_admits_fix_prices_with_an_exclusive_eodhd_root(
    tmp_path: Path,
) -> None:
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
    validated = registry.validate(
        "eodhd", "fix-prices", ["--days", "5", "--run"], context
    )
    modes = {c.resource_id: c.mode for c in validated.spec.resources}
    root_claims = [m for rid, m in modes.items() if "data" in rid and "toml" not in rid]
    assert root_claims == ["exclusive"]
    for bad in (
        ["--all", "--days", "3", "--run"],
        ["--days", "0", "--run"],
        ["lane", "--run"],
    ):
        with pytest.raises(CommandValidationError):
            registry.validate("eodhd", "fix-prices", bad, context)
    with pytest.raises(CommandValidationError):
        registry.validate(
            "eodhd", "fix-prices", ["--days", "5"], context
        )  # needs --run


def test_log_tolerances_are_what_the_docstring_says() -> None:
    assert pf.RULES.listing_jump == pytest.approx(math.log(2))
    assert pf.RULES.close_tolerance == 0.05 and pf.RULES.return_tolerance == 0.25
