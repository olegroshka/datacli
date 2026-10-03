"""positioning.holdings (DD-002 WP10): G1's members against hand computations on the synthetic panel."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import holdings  # noqa: E402
from test_positioning_long_ladder import (  # noqa: E402
    N_MANAGERS,
    N_SECURITIES,
    QUARTERS,
    Panel,
    _reported,
    build_panel,
    cusip,
    register,
    value,
)

duckdb = pytest.importorskip("duckdb")


@pytest.fixture(scope="module")
def panel() -> Panel:
    return build_panel()


@pytest.fixture(scope="module")
def inputs(panel: Panel) -> pd.DataFrame:
    con = duckdb.connect()
    register(con, panel)
    return holdings.compute(con)


def _hand(panel: Panel, s: int, q: int, agg: str) -> dict[str, float] | None:
    """The four measures from the panel's truth, written out the long way."""
    period = QUARTERS[q]
    managers = [m for m in panel.members(agg) if panel.present(m, q)]
    holders = [m for m in managers if panel.holdings[(m, q)].get(s, 0) > 0]
    if not holders:
        return None
    shares = [float(_reported(panel.holdings[(m, q)][s], s, period)) for m in holders]
    weight = 0.0
    top = 0
    for m in holders:
        book = panel.holdings[(m, q)]
        total = sum(value(x, n) for x, n in book.items())
        weight += value(s, book[s]) / total
        ranked = sorted(book, key=lambda x: (-value(x, book[x]), cusip(x)))
        if s in ranked[: holdings.TOP_N]:
            top += 1
    return {
        "n_holders": len(holders),
        "level_raw": sum(shares),
        "value_usd": sum(value(s, panel.holdings[(m, q)][s]) for m in holders),
        "long_fund_weight": weight,
        "long_conc": sum(x * x for x in shares) / sum(shares) ** 2,
        "best_ideas_count": top,
    }


def test_the_four_measures_equal_hand_computations(panel: Panel, inputs: pd.DataFrame) -> None:
    rows = {(r.cusip, r.period, r.aggregate): r for r in inputs.itertuples(index=False)}
    checked = 0
    for agg in holdings.AGGREGATES:
        for q, period in enumerate(QUARTERS):
            counts = {}
            for s in range(N_SECURITIES):
                expected = _hand(panel, s, q, agg)
                key = (cusip(s), period, agg)
                if expected is None:
                    assert key not in rows
                    continue
                row = rows[key]
                for field, want in expected.items():
                    assert getattr(row, field) == pytest.approx(want, abs=1e-9), (key, field)
                counts[s] = expected["best_ideas_count"]
                checked += 1
            total = sum(counts.values())
            for s, n in counts.items():
                assert rows[(cusip(s), period, agg)].best_ideas == pytest.approx(n / total if total else 0.0)
    assert checked > 800
    assert set(inputs.columns) == set(holdings.COLUMNS)
    assert not inputs.duplicated(list(holdings.KEY)).any()
    assert inputs.attrs["stats"]["periods"] == len(QUARTERS)


def test_acceptance_invariants_and_checks(panel: Panel, inputs: pd.DataFrame) -> None:
    sums = inputs.groupby(["aggregate", "period"])["best_ideas"].sum()
    assert (sums - 1).abs().max() < 1e-9  # L1-normalised per period and aggregate
    single = inputs[inputs.n_holders == 1]
    assert len(single) and (single["long_conc"] - 1).abs().max() < 1e-12
    many = inputs[inputs.n_holders > 1]
    assert (many["long_conc"] < 1).all() and (many["long_conc"] >= 1 / many["n_holders"] - 1e-12).all()
    assert (inputs["long_fund_weight"] <= inputs["n_holders"]).all() and (inputs["long_fund_weight"] > 0).all()
    # every manager has at most TOP_N best ideas per quarter
    per_quarter = inputs[inputs["aggregate"] == "all"].groupby("period")["best_ideas_count"].sum()
    present = {q: sum(1 for m in range(N_MANAGERS) if panel.present(m, q)) for q in range(len(QUARTERS))}
    for q, period in enumerate(QUARTERS):
        assert per_quarter[period] <= holdings.TOP_N * present[q]
    lag = (pd.to_datetime(inputs["published_at"]) - pd.to_datetime(inputs["period"])).dt.days
    assert lag.min() >= 45 and lag.max() <= holdings.LATE_DAYS
    assert holdings.checks(inputs) == []
    broken = inputs.copy()
    broken.loc[broken.index[0], "best_ideas"] += 0.5
    broken.loc[broken[broken.n_holders == 1].index[:1], "long_conc"] = 0.5
    checks = {c for _, c, _ in holdings.checks(broken)}
    assert checks == {"best_ideas_not_normalised", "single_holder_conc"}
    assert holdings.checks(pd.DataFrame(columns=holdings.COLUMNS)) == []


def test_cohort_rows_are_a_subset_and_units_do_not_matter(panel: Panel, inputs: pd.DataFrame) -> None:
    con = duckdb.connect()
    register(con, panel)
    # one manager reports in thousands: weights inside its book are unchanged
    con.execute(
        "CREATE OR REPLACE VIEW sec_13f_holdings AS "
        "SELECT r.accession_number, r.cusip, r.shares, "
        "CASE WHEN s.cik = '1002' THEN r.value_usd / 1000 ELSE r.value_usd END AS value_usd, "
        "r.share_type, r.put_call, s.cik, s.filing_date, s.period, s.submission_type, c.is_amendment, c.amendment_type "
        "FROM _rows r JOIN _subs s USING (accession_number) JOIN _covers c USING (accession_number)"
    )
    again = holdings.compute(con)
    pd.testing.assert_series_equal(again["long_fund_weight"], inputs["long_fund_weight"])
    pd.testing.assert_series_equal(again["best_ideas"], inputs["best_ideas"])
    assert (again["value_usd"] <= inputs["value_usd"]).all()
    all_rows = inputs[inputs["aggregate"] == "all"]
    cohort = inputs[inputs["aggregate"] == "cohort"]
    merged = all_rows.merge(cohort, on=["cusip", "period"], suffixes=("_all", "_cohort"))
    assert len(merged) == len(cohort) and (merged["n_holders_cohort"] <= merged["n_holders_all"]).all()
    assert (merged["level_raw_cohort"] <= merged["level_raw_all"] + 1e-9).all()
