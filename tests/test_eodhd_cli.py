from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import cli  # type: ignore  # noqa: E402
import eodhd_datasets as reg  # type: ignore  # noqa: E402


def test_all_registered_datasets_have_existing_fetchers() -> None:
    # Every dataset and every universe step must point at a real script on disk;
    # a typo in the registry would otherwise only surface at refresh time.
    for lane in reg.LANES.values():
        if lane.universe_fetcher:
            assert (
                _SCRIPTS_EODHD / lane.universe_fetcher
            ).exists(), lane.universe_fetcher
        for ds in lane.datasets:
            assert ds.fetcher is not None, f"{lane.name}:{ds.kind} missing fetcher"
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


def test_step_display_and_argv() -> None:
    step = cli.Step(
        "us_common", "prices", "fetch_eodhd_us_prices.py", ["--to", "2026-07-06"]
    )
    assert step.display() == "python eodhd/fetch_eodhd_us_prices.py --to 2026-07-06"
    argv = step.argv()
    assert argv[0] == sys.executable
    assert argv[1].endswith("fetch_eodhd_us_prices.py")
    assert argv[-2:] == ["--to", "2026-07-06"]
