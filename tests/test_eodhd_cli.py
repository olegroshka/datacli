from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import cli  # type: ignore  # noqa: E402
import eodhd_datasets as reg  # type: ignore  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _bootstrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan tests must not depend on what is on this machine's data root: by
    default every lane looks bootstrapped (coverage file present)."""
    monkeypatch.setattr(reg.LaneConfig, "bootstrap_missing", lambda self: False)


def test_all_registered_datasets_have_existing_fetchers() -> None:
    # Every dataset and every universe step must point at a real script on disk;
    # a typo in the registry would otherwise only surface at refresh time.
    for lane in reg.LANES.values():
        if lane.universe_fetcher:
            assert (
                _SCRIPTS_EODHD / lane.universe_fetcher
            ).exists(), lane.universe_fetcher
        for ds in lane.datasets:
            if ds.fetcher is None:  # locally derived sidecars (news scores) have none
                assert ds.state is None and ds.partitioned, f"{lane.name}:{ds.kind}"
                continue
            assert (_SCRIPTS_EODHD / ds.fetcher).exists(), ds.fetcher


def test_us_etf_carries_universe_provider_arg_but_uk_eu_etf_does_not() -> None:
    us_prices = next(d for d in reg.LANES["us_etf"].datasets if d.kind == "prices")
    uk_prices = next(d for d in reg.LANES["uk_eu_etf"].datasets if d.kind == "prices")
    assert us_prices.fetcher_args == ("--universe", "provider")
    assert uk_prices.fetcher_args == ()


def test_plan_us_etf_orders_universe_then_data_with_fixed_args() -> None:
    plan = cli.build_refresh_plan(
        ["us_etf"], kinds=set(cli.DEFAULT_KINDS), with_universe=True, passthrough=[]
    )
    assert [s.kind for s in plan] == ["universe", "prices", "dividends", "splits"]
    assert plan[0].script == "fetch_eodhd_us_etf_universe.py"
    assert plan[1].script == "fetch_eodhd_us_etf_prices.py"
    assert plan[1].args == ["--universe", "provider"]


def test_plan_us_common_has_no_universe_and_no_fundamentals_by_default() -> None:
    plan = cli.build_refresh_plan(
        ["us_common"], kinds=set(cli.DEFAULT_KINDS), with_universe=True, passthrough=[]
    )
    assert [s.kind for s in plan] == ["prices", "dividends", "splits"]
    assert plan[0].script == "fetch_eodhd_us_prices.py"


def test_plan_includes_fundamentals_when_kind_selected() -> None:
    plan = cli.build_refresh_plan(
        ["us_common"], kinds=set(cli.KNOWN_KINDS), with_universe=False, passthrough=[]
    )
    assert plan[-1].kind == "fundamentals"
    assert plan[-1].script == "fetch_eodhd_us_fundamentals.py"


def test_passthrough_appends_to_data_steps_not_universe() -> None:
    plan = cli.build_refresh_plan(
        ["us_etf"], kinds={"prices"}, with_universe=True, passthrough=["--full-refresh"]
    )
    universe = next(s for s in plan if s.kind == "universe")
    prices = next(s for s in plan if s.kind == "prices")
    assert universe.args == []
    assert prices.args == ["--universe", "provider", "--full-refresh"]


def test_fundamentals_only_selects_common_lanes_and_skips_universe() -> None:
    # `--datasets fundamentals` across all lanes must hit only the two lanes that
    # have a fundamentals dataset, and must not trigger ETF/index universe steps.
    plan = cli.build_refresh_plan(
        list(reg.LANES),
        kinds={"fundamentals"},
        with_universe=True,
        passthrough=[],
    )
    assert [(s.lane, s.kind) for s in plan] == [
        ("us_common", "fundamentals"),
        ("uk_eu", "fundamentals"),
    ]


def test_news_rides_default_refresh_capped_and_without_passthrough() -> None:
    # news is a default kind, but a routine refresh must be a bounded top-up:
    # the registry pins --limit-days, and ticker-style passthrough flags
    # (--full-refresh / --tickers) never reach the crawler, so `refresh --run`
    # can never turn into a 2,000-day backfill.
    assert "news" in cli.DEFAULT_KINDS
    plan = cli.build_refresh_plan(
        list(reg.LANES),
        kinds=set(cli.DEFAULT_KINDS),
        with_universe=True,
        passthrough=["--full-refresh", "--tickers", "AAPL"],
    )
    news_steps = [s for s in plan if s.lane == "news"]
    assert [(s.kind, s.script, s.args) for s in news_steps] == [
        (
            "news",
            "fetch_eodhd_news.py",
            ["--limit-days", str(reg.NEWS_REFRESH_MAX_DAYS)],
        ),
        ("news_daily", "build_news_symbol_daily.py", []),
    ]
    # ...and no universe step is invented for it
    assert all(s.kind != "universe" for s in news_steps)


def test_fast_path_extra_kinds_exclude_bulk_and_fundamentals() -> None:
    # `refresh --fast` delegates prices/dividends/splits to the bulk script and
    # then runs the remaining default kinds (news) as ordinary steps.
    kinds = set(cli.DEFAULT_KINDS) | {"fundamentals"}
    extra = cli.build_refresh_plan(
        list(reg.LANES),
        kinds=kinds - cli.INCREMENTAL_KINDS - {"fundamentals"},
        with_universe=False,
        passthrough=[],
    )
    assert [(s.lane, s.kind) for s in extra] == [
        ("news", "news"),
        ("news", "news_daily"),
    ]


def test_passthrough_not_applied_to_fundamentals() -> None:
    # fundamentals runs via its own --update mode and doesn't accept --to/etc, so
    # price/event passthrough must stop at the incremental fetchers.
    plan = cli.build_refresh_plan(
        ["us_common"],
        kinds={"prices", "fundamentals"},
        with_universe=False,
        passthrough=["--full-refresh"],
    )
    prices = next(s for s in plan if s.kind == "prices")
    fundamentals = next(s for s in plan if s.kind == "fundamentals")
    assert prices.args == ["--full-refresh"]
    assert fundamentals.args == ["--update"]  # fixed arg only, no passthrough


def _unbootstrapped(monkeypatch: pytest.MonkeyPatch, *lanes: str) -> None:
    monkeypatch.setattr(
        reg.LaneConfig, "bootstrap_missing", lambda self: self.name in lanes
    )


def test_first_fill_runs_fundamentals_before_prices_when_coverage_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On an empty root the per-ticker fetchers need coverage_summary.csv, which
    # only the fundamentals stage writes: the plan must reorder, not fail at #1.
    _unbootstrapped(monkeypatch, "us_common")
    plan = cli.build_refresh_plan(
        ["us_common"],
        kinds={"prices", "dividends", "splits", "fundamentals"},
        with_universe=True,
        passthrough=[],
    )
    assert [s.kind for s in plan] == ["fundamentals", "prices", "dividends", "splits"]
    notes = cli.bootstrap_notes(["us_common"], {"prices", "fundamentals"})
    assert len(notes) == 1 and "fundamentals runs first" in notes[0]


def test_first_fill_without_fundamentals_skips_per_ticker_steps_with_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _unbootstrapped(monkeypatch, "us_common", "uk_eu")
    plan = cli.build_refresh_plan(
        ["us_common", "uk_eu", "us_etf"],
        kinds=set(cli.DEFAULT_KINDS),
        with_universe=True,
        passthrough=[],
    )
    # common-stock lanes drop out entirely; the ETF lane (universe fetcher) is fine
    assert {s.lane for s in plan} == {"us_etf"}
    notes = cli.bootstrap_notes(
        ["us_common", "uk_eu", "us_etf"], set(cli.DEFAULT_KINDS)
    )
    assert [n.split(":")[0] for n in notes] == ["us_common", "uk_eu"]
    assert all("--datasets fundamentals --run" in n for n in notes)
    # lanes that are bootstrapped or select no per-ticker kind produce no note
    assert cli.bootstrap_notes(["us_common"], {"news"}) == []


def test_lane_universe_source_and_bootstrap_fields() -> None:
    assert reg.LANES["us_common"].bootstrap_file == "coverage_summary.csv"
    assert reg.LANES["uk_eu"].bootstrap_file == "coverage_summary.csv"
    assert reg.LANES["news"].bootstrap_file is None
    assert reg.LANES["news"].universe_source() == "(no universe)"
    assert "coverage_summary.csv" in reg.LANES["us_common"].universe_source()
    assert reg.LANES["us_etf"].universe_source() == "fetch_eodhd_us_etf_universe.py"


def test_refresh_rejects_fast_with_per_ticker_flags_and_bad_names(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.cmd_refresh(["--fast", "--tickers", "AAPL"])
    assert exc.value.code == 2
    assert "cannot be combined with --fast" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.cmd_refresh(["--days", "3"])  # --days without --fast
    # typos get the friendly did-you-mean, not an argparse dump
    assert cli.cmd_refresh(["us_comon"]) == 2
    out = capsys.readouterr().out
    assert "did you mean" in out and "us_common" in out
    assert cli.cmd_refresh(["--datasets", "prcies"]) == 2
    assert "prices" in capsys.readouterr().out


def test_config_keys_and_lifecycle_help() -> None:
    assert set(cli.CONFIG_KEYS) >= {"data-root", "sync-backend"}
    text = cli.top_help()
    assert "Lifecycle" in text and "first fill" in text and "reindex" in text
    assert "EODHD_API_KEY" in text


def test_step_display_and_argv() -> None:
    step = cli.Step(
        "us_common", "prices", "fetch_eodhd_us_prices.py", ["--to", "2026-07-06"]
    )
    assert step.display() == "python eodhd/fetch_eodhd_us_prices.py --to 2026-07-06"
    argv = step.argv()
    assert argv[0] == sys.executable
    assert argv[1].endswith("fetch_eodhd_us_prices.py")
    assert argv[-2:] == ["--to", "2026-07-06"]


def test_local_steps_are_marked() -> None:
    plan = cli.build_refresh_plan(
        ["news"], kinds=set(cli.DEFAULT_KINDS), with_universe=False, passthrough=[]
    )
    by_kind = {s.kind: s for s in plan}
    assert by_kind["news_daily"].local is True and by_kind["news"].local is False
    assert reg.LANES["news"].datasets[1].local is True  # news_daily spec
