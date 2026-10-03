"""positioning.dataset / views / cli: the stored short ladder on a fake DuckDB surface."""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import cli, dataset, views  # noqa: E402
from positioning.config import ENV_ROOT  # noqa: E402

duckdb = pytest.importorskip("duckdb")


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


DAYS = _weekdays(date(2024, 1, 2), 80)
SCHEDULE = DAYS[9::10]  # eight settlement dates


def _source(con, reports: pd.DataFrame, *, split: bool = True) -> None:
    """Fake ``finra_short_interest``, ``prices`` and ``splits`` views."""
    prices = pd.DataFrame(
        [
            {"date": d.isoformat(), "close": 50.0 + i * 0.1, "ticker": t, "lane": "us_common"}
            for t in ("AAA", "BBB")
            for i, d in enumerate(DAYS)
        ]
    )
    if split:  # BBB splits 2:1 on DAYS[35]: raw closes halve from then on
        late = (prices.ticker == "BBB") & (prices.date >= DAYS[35].isoformat())
        prices.loc[late, "close"] /= 2
    splits = pd.DataFrame(
        [{"ticker": "BBB", "ex_date": DAYS[35].isoformat(), "split_ratio": 2.0, "lane": "us_common"}]
        if split
        else [],
        columns=["ticker", "ex_date", "split_ratio", "lane"],
    )
    con.register("_reports", reports)
    con.register("_prices", prices)
    con.register("_splits", splits)
    con.execute("CREATE OR REPLACE VIEW finra_short_interest AS SELECT * FROM _reports")
    con.execute("CREATE OR REPLACE VIEW prices AS SELECT * FROM _prices")
    con.execute("CREATE OR REPLACE VIEW splits AS SELECT * FROM _splits")


def _reports(upto: int = len(SCHEDULE), bump: float = 0.0) -> pd.DataFrame:
    rows = []
    for i, day in enumerate(SCHEDULE[:upto]):
        after_split = day >= DAYS[35]
        rows.append(("AAA", day, 1000.0 + 100 * i + (bump if i == 2 else 0.0), False))
        rows.append(("BBB", day, (2000.0 - 50 * i) * (2 if after_split else 1), after_split and SCHEDULE[i - 1] < DAYS[35]))
    frame = pd.DataFrame(rows, columns=["eodhd_code", "settlement_date", "short_position", "split_adjusted"])
    frame["published_at"] = [d + timedelta(days=11) for d in frame["settlement_date"]]
    frame["listed"] = True
    frame["security_kind"] = "common"
    return frame


def test_build_dry_run_writes_nothing_then_run_is_idempotent(tmp_path: Path) -> None:
    con = duckdb.connect()
    _source(con, _reports())
    plan = dataset.build(con, tmp_path, run=False)
    assert plan.partitions == 8 and len(plan.new) == 8 and plan.rows == 16 and plan.symbols == 2
    assert not dataset.store(tmp_path).days_on_disk()

    first = dataset.build(con, tmp_path, run=True)
    assert len(first.new) == 8 and not first.restated
    assert dataset.store(tmp_path).days_on_disk() == SCHEDULE
    again = dataset.build(con, tmp_path, run=True)
    assert again.unchanged == 8 and again.changed == 0
    assert dataset.qc(tmp_path, con) == []

    stored = dataset.store(tmp_path).read_range()
    bbb = stored[stored.eodhd_code == "BBB"].sort_values("settlement_date")
    assert (bbb["flow"].iloc[1:] == -50).all()  # the 2:1 split created no flow
    assert bbb["quantity_factor"].tolist() == [1.0] * 3 + [2.0] * 5
    status = dataset.status(tmp_path)
    assert status["partitions"] == 8 and status["rows"] == 16 and status["restated"] == 0


def test_appending_a_report_adds_one_partition_and_leaves_the_rest(tmp_path: Path) -> None:
    con = duckdb.connect()
    _source(con, _reports(upto=7))
    dataset.build(con, tmp_path, run=True)
    _source(con, _reports(upto=8))
    stale = [f for f in dataset.qc(tmp_path, con) if f.check == "stale"]
    assert len(stale) == 1
    report = dataset.build(con, tmp_path, run=True)
    assert report.new == (SCHEDULE[7],) and not report.restated and report.unchanged == 7


def test_a_revised_report_is_recorded_as_a_restatement(tmp_path: Path) -> None:
    con = duckdb.connect()
    _source(con, _reports())
    dataset.build(con, tmp_path, run=True)
    _source(con, _reports(bump=25.0))  # FINRA revises AAA's third report
    plan = dataset.build(con, tmp_path, run=False)
    assert not plan.new and plan.restated == tuple(SCHEDULE[2:])  # that date and all after it
    dataset.build(con, tmp_path, run=True)
    state = dataset.store(tmp_path).load_state()
    assert state[SCHEDULE[2].isoformat()].detail.startswith("restated")
    assert state[SCHEDULE[1].isoformat()].detail == ""
    assert any(f.check == "restated" and f.severity == "warn" for f in dataset.qc(tmp_path, con))


def test_views_register_only_with_data(tmp_path: Path) -> None:
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)[views.VIEW] is False
    _source(con, _reports())
    dataset.build(con, tmp_path, run=True)
    assert views.register(con, root=tmp_path)[views.VIEW] is True
    row = con.execute(
        f"SELECT count(*), count(DISTINCT eodhd_code), min(published_at - settlement_date) FROM {views.VIEW}"
    ).fetchone()
    assert row == (16, 2, 11)
    profit, log_profit, cost = con.execute(
        f"SELECT profit_pct, short_profit_log, cost_basis FROM {views.VIEW} "
        "WHERE eodhd_code = 'AAA' ORDER BY settlement_date DESC LIMIT 1"
    ).fetchone()
    close = 50.0 + 79 * 0.1
    assert profit == pytest.approx((cost - close) / cost)
    assert log_profit == pytest.approx(__import__("math").log(cost / close))
    assert con.execute(f"SELECT count(*) FROM {views.STATE_VIEW}").fetchone()[0] == 8
    assert views.VIEW in views.schema_snippet()


def test_cli_dry_run_run_status_and_qc(tmp_path: Path, monkeypatch, capsys) -> None:
    con = duckdb.connect()
    _source(con, _reports())
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setattr(cli, "_connect", lambda: con)

    assert cli.main(["build"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert not dataset.store(tmp_path).days_on_disk()
    assert cli.main(["build", "--run"]) == 0
    assert len(dataset.store(tmp_path).days_on_disk()) == 8
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert payload["datasets"][0]["partitions"] == 8 and payload["root_source"] == "env"
    assert cli.main(["qc"]) == 0
    assert cli.main(["build", "extra"]) == 2
    assert cli.main(["biuld"]) == 2
    assert cli.main(["build", "--rum"]) == 2
    assert cli.main(["--help"]) == 0


def test_scheduler_admits_the_positioning_family(tmp_path: Path) -> None:
    from scheduler.commands import (
        CommandValidationError,
        ValidationContext,
        default_registry,
    )

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    context = ValidationContext.current(_REPO_ROOT, Path(sys.executable), config, environment={})
    registry = default_registry()
    build = registry.validate("positioning", "build", ["--run"], context)
    names = {b.name: b for b in build.bindings}
    claims = {c.resource_id: c.mode for c in build.spec.resources}
    assert Path(names["positioning_data_root"].resolved_value).name == "positioning"
    assert claims[names["positioning_data_root"].resource_id] == "exclusive"
    assert claims[names["eodhd_data_root"].resource_id] == "shared"
    assert claims[names["finra_data_root"].resource_id] == "shared"
    status = registry.validate("positioning", "status", ["--json"], context)
    status_claims = {c.resource_id: c.mode for c in status.spec.resources}
    assert status_claims[names["positioning_data_root"].resource_id] == "shared"
    assert registry.validate("positioning", "qc", [], context).spec.resources
    with pytest.raises(CommandValidationError, match="requires its own --run"):
        registry.validate("positioning", "build", [], context)
    with pytest.raises(CommandValidationError):
        registry.validate("positioning", "build", ["--run", "--full"], context)
    with pytest.raises(CommandValidationError, match="flags only"):
        registry.validate("positioning", "qc", ["x"], context)


# --------------------------------------------------------------------------- #
# the long ladder as a stored dataset (DD-002 WP9 step 5)
# --------------------------------------------------------------------------- #
def _long_source(con) -> None:
    from test_positioning_long_ladder import build_panel, register

    register(con, build_panel())


def test_long_ladder_build_qc_views_and_restatement(tmp_path: Path) -> None:
    from test_positioning_long_ladder import QUARTERS

    con = duckdb.connect()
    _long_source(con)
    spec = dataset.LONG_LADDER
    plan = dataset.build(con, tmp_path, run=False, dataset="long_ladder")
    assert plan.partitions == len(QUARTERS) and len(plan.new) == len(QUARTERS) and plan.stats["unmapped"] == 0
    assert not dataset.store(tmp_path, spec).days_on_disk()
    first = dataset.build(con, tmp_path, run=True, dataset=spec)
    assert dataset.store(tmp_path, spec).days_on_disk() == QUARTERS and not first.restated
    assert dataset.build(con, tmp_path, run=True, dataset=spec).unchanged == len(QUARTERS)
    assert dataset.qc(tmp_path, con, "long_ladder") == []
    assert dataset.qc(tmp_path, None, "long_ladder") == []
    # the short ladder store is untouched and reports itself empty
    assert dataset.qc(tmp_path)[0].check == "empty" and dataset.status(tmp_path)["present"] is False
    status = dataset.status(tmp_path, "long_ladder")
    assert status["dataset"] == "long_ladder" and status["partitions"] == len(QUARTERS)
    # views
    registered = views.register(con, root=tmp_path)
    assert registered[views.LONG_VIEW] is True and registered[views.VIEW] is False
    n, aggs, lag = con.execute(
        f"SELECT count(*), count(DISTINCT aggregate), min(published_at - period) FROM {views.LONG_VIEW}"
    ).fetchone()
    assert n == first.rows and aggs == 2 and lag == 45
    profit, log_profit = con.execute(
        f"SELECT profit_pct, long_profit_log FROM {views.LONG_VIEW} WHERE inventory > 0 LIMIT 1"
    ).fetchone()
    assert log_profit == pytest.approx(__import__("math").log(1 + profit))
    assert con.execute(f"SELECT count(*) FROM {views.LONG_STATE_VIEW}").fetchone()[0] == len(QUARTERS)
    assert views.LONG_VIEW in views.schema_snippet() and views.LONG_VIEW not in views.schema_snippet(long_ladder=False)
    # a late-arriving amendment inside the window restates that period and the ones after
    con.execute("CREATE OR REPLACE VIEW _rows2 AS SELECT * FROM _rows")
    con.execute(
        "CREATE OR REPLACE VIEW sec_13f_holdings AS "
        "SELECT r.*, s.cik, s.filing_date, s.period, s.submission_type, c.is_amendment, c.amendment_type "
        "FROM (SELECT accession_number, cusip, CASE WHEN cusip = 'CUS000000' THEN shares + 1 ELSE shares END AS shares, "
        "share_type, put_call FROM _rows) r JOIN _subs s USING (accession_number) JOIN _covers c USING (accession_number)"
    )
    plan = dataset.build(con, tmp_path, run=False, dataset=spec)
    assert not plan.new and plan.restated and plan.restated[0] == QUARTERS[0]
    with pytest.raises(ValueError, match="unknown positioning dataset"):
        dataset.build(con, tmp_path, run=False, dataset="mid_ladder")


def test_cli_long_ladder_dataset_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    con = duckdb.connect()
    _long_source(con)
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("DATACLI_LOCKS_HELD", "1")
    monkeypatch.setattr(cli, "_connect", lambda: con)
    assert cli.main(["build", "--dataset", "long_ladder"]) == 0
    out = capsys.readouterr().out
    assert "long_ladder plan (dry run)" in out and "left out" in out
    assert cli.main(["build", "--dataset", "long_ladder", "--run"]) == 0
    assert len(dataset.store(tmp_path, dataset.LONG_LADDER).days_on_disk()) == 12
    capsys.readouterr()
    assert cli.main(["status", "--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert [d["dataset"] for d in payload["datasets"]] == ["short_ladder", "long_ladder"]
    assert payload["datasets"][1]["partitions"] == 12 and payload["datasets"][0]["present"] is False
    assert cli.main(["qc", "--dataset", "long_ladder"]) == 0
    assert "long_ladder: no findings" in capsys.readouterr().out
    assert cli.main(["qc"]) == 0  # the short ladder: empty is a warning, not an error
    assert cli.main(["build", "--dataset", "mid_ladder"]) == 2
    assert cli.main(["qc", "extra"]) == 2


def test_scheduler_admits_the_dataset_flag_and_claims_the_sec_root(tmp_path: Path) -> None:
    from scheduler.commands import CommandValidationError, ValidationContext, default_registry

    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(f'[eodhd]\ndata_root = "{data.as_posix()}"\n', encoding="utf-8")
    context = ValidationContext.current(_REPO_ROOT, Path(sys.executable), config, environment={})
    registry = default_registry()
    build = registry.validate("positioning", "build", ["--dataset", "long_ladder", "--run"], context)
    names = {b.name: b for b in build.bindings}
    claims = {c.resource_id: c.mode for c in build.spec.resources}
    assert Path(names["sec_data_root"].resolved_value).name == "sec"
    assert claims[names["sec_data_root"].resource_id] == "shared"
    assert claims[names["positioning_data_root"].resource_id] == "exclusive"
    assert registry.validate("positioning", "qc", ["--dataset", "long_ladder"], context).spec.resources
    with pytest.raises(CommandValidationError, match="unknown positioning dataset"):
        registry.validate("positioning", "build", ["--dataset", "mid_ladder", "--run"], context)
    with pytest.raises(CommandValidationError, match="unknown positioning dataset"):
        registry.validate("positioning", "qc", ["--dataset", "x"], context)
