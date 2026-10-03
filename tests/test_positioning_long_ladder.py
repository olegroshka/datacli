"""positioning.long_ladder and sec.views effective views on a synthetic fund panel (DD-002 WP9).

Twenty simulated managers trade fifty securities over twelve quarters with
entries, exits, one split and amendments of every kind. The panel's truth
(trading flow over managers present in both quarters, level over all of
them, the remainder as drift) is computed here in plain Python, independently
of the SQL, and the ladder must reproduce it exactly.
"""

from __future__ import annotations

import random
import sys
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import long_ladder  # noqa: E402
from sec import views as sec_views  # noqa: E402

duckdb = pytest.importorskip("duckdb")

QUARTERS = [
    date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31),
    date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31),
    date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30), date(2024, 12, 31),
]
N_MANAGERS, N_SECURITIES = 20, 50
SPLIT_SECURITY, SPLIT_RATIO, SPLIT_EX = 7, 2.0, date(2023, 8, 15)  # inside quarter 6
GAP_SECURITY, GAP_QUARTER = 20, 5  # no prices in quarter 5
#: manager -> (first quarter index, last quarter index) of presence in the panel
PRESENCE = {m: (0, 11) for m in range(N_MANAGERS)}
PRESENCE.update({12: (4, 11), 13: (7, 11), 14: (0, 5), 15: (0, 8), 16: (3, 9)})
#: manager -> (any_hedge_funds, advises_private_funds); None = no ADV match
FLAGS: dict[int, tuple[bool | None, bool | None]] = {}
for _m in range(N_MANAGERS):
    FLAGS[_m] = (True, True) if _m < 6 else (None, True) if _m < 10 else (False, False) if _m < 14 else (None, None)
COHORT = {m for m, (hf, pf) in FLAGS.items() if (pf if hf is None else hf)}


def cusip(i: int) -> str:
    return f"CUS{i:06d}"


def ticker(i: int) -> str:
    return f"S{i:02d}"


def cik(m: int) -> str:
    return str(1000 + m)


@dataclass
class Panel:
    """What the ladder should see: true holdings per manager and quarter, in pre-split shares."""

    holdings: dict[tuple[int, int], dict[int, int]]  # (manager, quarter) -> {security: shares}
    submissions: pd.DataFrame = field(default_factory=pd.DataFrame)
    covers: pd.DataFrame = field(default_factory=pd.DataFrame)
    rows: pd.DataFrame = field(default_factory=pd.DataFrame)
    cohort: pd.DataFrame = field(default_factory=pd.DataFrame)
    prices: pd.DataFrame = field(default_factory=pd.DataFrame)
    splits: pd.DataFrame = field(default_factory=pd.DataFrame)
    cusip_map: pd.DataFrame = field(default_factory=pd.DataFrame)

    def present(self, m: int, q: int) -> bool:
        first, last = PRESENCE[m]
        return first <= q <= last

    def members(self, agg: str) -> set[int]:
        return set(range(N_MANAGERS)) if agg == "all" else COHORT

    def level(self, s: int, q: int, agg: str) -> float:
        return float(sum(self.holdings[(m, q)].get(s, 0) for m in self.members(agg) if self.present(m, q)))

    def flow(self, s: int, q: int, agg: str) -> float:
        if q == 0:
            return 0.0
        both = [m for m in self.members(agg) if self.present(m, q) and self.present(m, q - 1)]
        return float(sum(self.holdings[(m, q)].get(s, 0) - self.holdings[(m, q - 1)].get(s, 0) for m in both))

    def holders(self, s: int, q: int, agg: str) -> int:
        return sum(1 for m in self.members(agg) if self.present(m, q) and self.holdings[(m, q)].get(s, 0) > 0)


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_panel(seed: int = 5) -> Panel:
    rng = random.Random(seed)
    holdings: dict[tuple[int, int], dict[int, int]] = {}
    for m in range(N_MANAGERS):
        book: dict[int, int] = {}
        for q in range(len(QUARTERS)):
            if not (PRESENCE[m][0] <= q <= PRESENCE[m][1]):
                continue
            if not book:  # first filing: a fresh book of 15 to 25 names
                for s in rng.sample(range(N_SECURITIES), rng.randint(15, 25)):
                    book[s] = rng.randint(1_000, 50_000)
            else:  # trade: adjust, close, open
                for s in list(book):
                    r = rng.random()
                    if r < 0.15:
                        del book[s]
                    elif r < 0.6:
                        book[s] = max(100, book[s] + rng.randint(-8_000, 8_000))
                for s in rng.sample(range(N_SECURITIES), rng.randint(0, 3)):
                    book.setdefault(s, rng.randint(1_000, 20_000))
            holdings[(m, q)] = dict(book)
    holdings[(8, 9)].setdefault(11, 7_000)  # the name the addition amendment brings
    holdings[(3, 4)].setdefault(5, 9_000)  # the name the restatement corrects
    panel = Panel(holdings)
    _filings(panel, rng)
    _market(panel, rng)
    return panel


def _reported(shares: int, s: int, period: date) -> int:
    """Raw shares as filed: post-split for the split security from the ex date."""
    return int(shares * SPLIT_RATIO) if s == SPLIT_SECURITY and period >= SPLIT_EX else shares


def _filings(panel: Panel, rng: random.Random) -> None:
    subs, covers, rows, cohort = [], [], [], []
    n = 0

    def file(m: int, q: int, book: dict[int, int], *, days: int, kind: str | None) -> None:
        nonlocal n
        n += 1
        acc = f"0000{m:03d}-{q:02d}-{n:06d}"
        period = QUARTERS[q]
        filed = period + timedelta(days=days)
        subs.append((acc, filed, "13F-HR/A" if kind else "13F-HR", cik(m), period))
        covers.append((acc, f"MANAGER {m}", kind is not None, kind))
        for s, shares in sorted(book.items()):
            rows.append((acc, cusip(s), float(_reported(shares, s, period)), "SH", None))
            if s % 17 == 0:  # noise the rule must ignore: a put on the same name, a bond
                rows.append((acc, cusip(s), 999.0, "SH", "Put"))
                rows.append((acc, "BOND" + cusip(s)[4:], 5000.0, "PRN", None))
        hf, pf = FLAGS[m]
        cohort.append((acc, cik(m), hf, pf))

    for (m, q), book in sorted(panel.holdings.items()):
        days = 40 + rng.randint(0, 5)
        if m == 3 and q == 4:  # original wrong on one name, restated 10 days later
            wrong = dict(book)
            wrong[5] = wrong.get(5, 0) + 500
            file(m, q, wrong, days=days, kind=None)
            file(m, q, book, days=days + 10, kind="RESTATEMENT")
        elif m == 8 and q == 9:  # original omits one name, added 10 days later
            assert 11 in book
            partial = {s: v for s, v in book.items() if s != 11}
            file(m, q, partial, days=days, kind=None)
            file(m, q, {11: book[11]}, days=days + 10, kind="NEW HOLDINGS")
        elif m == 1 and q == 2:  # a restatement filed 200 days late: outside the ladder's window
            file(m, q, book, days=days, kind=None)
            late = dict(book)
            late[2] = late.get(2, 0) * 3 + 300
            file(m, q, late, days=200, kind="RESTATEMENT")
        else:
            file(m, q, book, days=days, kind=None)
    panel.submissions = pd.DataFrame(subs, columns=["accession_number", "filing_date", "submission_type", "cik", "period"])
    panel.covers = pd.DataFrame(covers, columns=["accession_number", "manager_name", "is_amendment", "amendment_type"])
    panel.rows = pd.DataFrame(rows, columns=["accession_number", "cusip", "shares", "share_type", "put_call"])
    panel.cohort = pd.DataFrame(cohort, columns=["accession_number", "cik", "any_hedge_funds", "advises_private_funds"]).astype(
        {"any_hedge_funds": "boolean", "advises_private_funds": "boolean"}
    )


def _market(panel: Panel, rng: random.Random) -> None:
    days = _weekdays(date(2021, 12, 1), date(2025, 1, 10))
    prices = []
    for s in range(N_SECURITIES):
        close = rng.uniform(20, 100)
        for d in days:
            close *= rng.uniform(0.985, 1.015)
            raw = close / SPLIT_RATIO if s == SPLIT_SECURITY and d >= SPLIT_EX else close
            prices.append((ticker(s), "us_common", d.isoformat(), raw))
    frame = pd.DataFrame(prices, columns=["ticker", "lane", "date", "close"])
    # one security goes unpriced for a quarter (a halt): the ladder must bridge the gap
    halted = (frame.ticker == ticker(GAP_SECURITY)) & (frame.date > QUARTERS[GAP_QUARTER - 1].isoformat()) & (frame.date <= QUARTERS[GAP_QUARTER].isoformat())
    panel.prices = frame[~halted].reset_index(drop=True)
    panel.splits = pd.DataFrame(
        [(ticker(SPLIT_SECURITY), "us_common", SPLIT_EX.isoformat(), SPLIT_RATIO)],
        columns=["ticker", "lane", "ex_date", "split_ratio"],
    )
    rows = [(cusip(s), ticker(s), date(2018, 8, 1), date(2026, 8, 31), 1500) for s in range(N_SECURITIES)]
    rows.append((cusip(3), "OLD03", date(2018, 8, 1), date(2019, 1, 1), 50))  # a rename: the old ticker loses
    panel.cusip_map = pd.DataFrame(rows, columns=["cusip", "eodhd_code", "first_seen", "last_seen", "n_days"])


def register(con, panel: Panel, *, upto_filed: date | None = None) -> None:
    """Fake source views; ``upto_filed`` keeps only what was filed or priced by that date."""
    subs, prices = panel.submissions, panel.prices
    if upto_filed is not None:
        subs = subs[subs["filing_date"] <= upto_filed]
        prices = prices[prices["date"] <= upto_filed.isoformat()]
    con.register("_subs", subs)
    con.register("_covers", panel.covers)
    con.register("_rows", panel.rows)
    con.register("_cohort", panel.cohort)
    con.register("_prices", prices)
    con.register("_splits", panel.splits)
    con.register("_map", panel.cusip_map)
    con.execute(f"CREATE OR REPLACE VIEW {sec_views.SUBMISSION_VIEW} AS SELECT * FROM _subs")
    con.execute(f"CREATE OR REPLACE VIEW {sec_views.COVER_VIEW} AS SELECT * FROM _covers")
    con.execute(
        f"CREATE OR REPLACE VIEW {sec_views.HOLDINGS_VIEW} AS "
        "SELECT r.*, s.cik, s.filing_date, s.period, s.submission_type, c.is_amendment, c.amendment_type "
        "FROM _rows r JOIN _subs s USING (accession_number) JOIN _covers c USING (accession_number)"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW {sec_views.COHORT_VIEW} AS "
        "SELECT c.accession_number, c.cik, c.any_hedge_funds, c.advises_private_funds FROM _cohort c "
        "JOIN _subs USING (accession_number)"
    )
    con.execute("CREATE OR REPLACE VIEW prices AS SELECT * FROM _prices")
    con.execute("CREATE OR REPLACE VIEW splits AS SELECT * FROM _splits")
    con.execute(f"CREATE OR REPLACE VIEW {long_ladder.MAP_VIEW} AS SELECT * FROM _map")
    sec_views.register_effective(con)


@pytest.fixture(scope="module")
def panel() -> Panel:
    return build_panel()


@pytest.fixture(scope="module")
def ladder(panel: Panel) -> pd.DataFrame:
    con = duckdb.connect()
    register(con, panel)
    return long_ladder.compute(con)


def _keyed(frame: pd.DataFrame) -> dict[tuple[str, date, str], pd.Series]:
    return {(r.cusip, r.period, r.aggregate): r for r in frame.itertuples(index=False)}


def test_effective_view_restatement_replaces_addition_adds_late_restatement_shows(panel: Panel) -> None:
    con = duckdb.connect()
    register(con, panel)
    eff = con.execute(
        f"SELECT cusip, shares, filing_kind, effective_filing_date, n_effective_filings, filing_date "
        f"FROM {sec_views.HOLDINGS_EFFECTIVE_VIEW} WHERE cik = ? AND period = ? AND put_call IS NULL "
        "AND share_type = 'SH' ORDER BY cusip",
        [cik(3), QUARTERS[4]],
    ).df()
    assert set(eff["filing_kind"]) == {"restatement"} and set(eff["n_effective_filings"]) == {1}
    assert float(eff.loc[eff.cusip == cusip(5), "shares"].iloc[0]) == panel.holdings[(3, 4)][5]
    assert len(eff) == len(panel.holdings[(3, 4)])

    eff = con.execute(
        f"SELECT cusip, shares, filing_kind, effective_filing_date, filing_date, n_effective_filings "
        f"FROM {sec_views.HOLDINGS_EFFECTIVE_VIEW} WHERE cik = ? AND period = ? AND put_call IS NULL "
        "AND share_type = 'SH' ORDER BY cusip",
        [cik(8), QUARTERS[9]],
    ).df()
    assert len(eff) == len(panel.holdings[(8, 9)]) and set(eff["n_effective_filings"]) == {2}
    added = eff[eff.cusip == cusip(11)].iloc[0]
    assert added["filing_kind"] == "addition" and float(added["shares"]) == panel.holdings[(8, 9)][11]
    assert (eff["effective_filing_date"] == added["filing_date"]).all()  # the last filing that shaped the set
    assert (eff.loc[eff.cusip != cusip(11), "filing_date"] < added["filing_date"]).all()

    eff = con.execute(
        f"SELECT shares, filing_kind, effective_filing_date - period AS lag "
        f"FROM {sec_views.HOLDINGS_EFFECTIVE_VIEW} WHERE cik = ? AND period = ? AND cusip = ? AND put_call IS NULL",
        [cik(1), QUARTERS[2], cusip(2)],
    ).fetchall()
    assert len(eff) == 1 and eff[0][1] == "restatement" and eff[0][2] == 200
    assert eff[0][0] == panel.holdings[(1, 2)][2] * 3 + 300
    # the same rule with the ladder's cutoff keeps the original
    cut = con.execute(
        f"SELECT filing_kind FROM ({sec_views.effective_filings_sql(60)}) WHERE cik = ? AND period = ?",
        [cik(1), QUARTERS[2]],
    ).fetchall()
    assert cut == [("original",)]
    assert sec_views.HOLDINGS_EFFECTIVE_VIEW in sec_views.schema_snippet()


def test_drift_rule_yields_exactly_the_known_trading_flow(panel: Panel, ladder: pd.DataFrame) -> None:
    rows = _keyed(ladder)
    assert ladder.attrs["stats"]["unmapped"] == 0 and ladder.attrs["stats"]["unpriced"] == 0
    assert ladder.attrs["stats"]["unpriced_periods"] == 2  # the halted quarter, both aggregates
    checked = 0
    for agg in long_ladder.AGGREGATES:
        for s in range(N_SECURITIES):
            previous_level = None
            for q, period in enumerate(QUARTERS):
                level, flow = panel.level(s, q, agg), panel.flow(s, q, agg)
                key = (cusip(s), period, agg)
                if s == GAP_SECURITY and q == GAP_QUARTER:
                    assert key not in rows
                    continue
                if s == GAP_SECURITY and q == GAP_QUARTER + 1 and previous_level is not None:
                    row = rows[key]
                    assert row.gap and not row.reset and row.drift_shares == 0.0
                    assert row.flow == pytest.approx(level - previous_level, abs=1e-6)
                    assert row.inventory == pytest.approx(level, abs=1e-6)
                    previous_level = level
                    continue
                if level == 0 and flow == 0:
                    assert key not in rows or rows[key].inventory == 0
                    previous_level = level if key in rows else previous_level
                    continue
                row = rows[key]
                assert row.inventory == pytest.approx(level, abs=1e-6), key
                assert row.level_raw == pytest.approx(_reported(int(level), s, period), abs=1e-6), key
                if previous_level is None or row.reset:
                    assert row.flow == 0.0 and row.drift_shares == 0.0
                else:
                    assert row.flow == pytest.approx(flow, abs=1e-6), key
                    assert row.drift_shares == pytest.approx(level - previous_level - flow, abs=1e-6), key
                assert row.n_managers == panel.holders(s, q, agg)
                assert row.n_cohort_managers == panel.holders(s, q, "cohort")
                assert row.eodhd_code == ticker(s) and row.lane == "us_common"
                previous_level = level
                checked += 1
    assert checked > 800
    # entries and exits are drift, never flow: the quarters a manager enters or leaves carry it
    entries = {4: 12, 7: 13, 3: 16}
    for q, m in entries.items():
        book = panel.holdings[(m, q)]
        s = max(book, key=book.get)
        row = rows[(cusip(s), QUARTERS[q], "all")]
        assert row.drift_shares != 0 and abs(row.drift_shares) >= book[s] - abs(row.drift_shares - book[s])


def test_the_split_creates_no_flow_and_is_confirmed_by_the_aggregate(panel: Panel, ladder: pd.DataFrame) -> None:
    rows = _keyed(ladder)
    s, q = SPLIT_SECURITY, 6
    for agg in long_ladder.AGGREGATES:
        row = rows[(cusip(s), QUARTERS[q], agg)]
        assert row.flow == pytest.approx(panel.flow(s, q, agg), abs=1e-6)
        assert row.quantity_factor == SPLIT_RATIO and row.price_factor == SPLIT_RATIO
        assert rows[(cusip(s), QUARTERS[q - 1], agg)].quantity_factor == 1.0
        assert row.level_raw == pytest.approx(panel.level(s, q, agg) * SPLIT_RATIO)
    # without the basis the raw level jumps by the ratio
    assert rows[(cusip(s), QUARTERS[q], "all")].level_raw / rows[(cusip(s), QUARTERS[q - 1], "all")].level_raw > 1.5
    # a security without splits keeps factor 1 everywhere
    other = ladder[ladder.cusip == cusip(0)]
    assert (other["quantity_factor"] == 1.0).all() and (other["price_factor"] == 1.0).all()


def test_amendments_reach_the_ladder_restatement_replaces_addition_adds(panel: Panel, ladder: pd.DataFrame) -> None:
    rows = _keyed(ladder)
    # m3's wrong original (+500 on s05) was replaced: the level is the restated one
    assert rows[(cusip(5), QUARTERS[4], "all")].level_raw == pytest.approx(panel.level(5, 4, "all"))
    # m8's missing s11 was added
    assert rows[(cusip(11), QUARTERS[9], "all")].level_raw == pytest.approx(panel.level(11, 9, "all"))
    # m1's late restatement of s02 did not reach the ladder
    assert rows[(cusip(2), QUARTERS[2], "all")].level_raw == pytest.approx(panel.level(2, 2, "all"))


def test_prices_published_at_and_the_kernel_outputs(panel: Panel, ladder: pd.DataFrame) -> None:
    lag = (pd.to_datetime(ladder["published_at"]) - pd.to_datetime(ladder["period"])).dt.days
    assert lag.min() >= long_ladder.DEADLINE_DAYS and lag.max() <= long_ladder.LATE_DAYS
    # the amended quarters complete later than the plain ones
    by_period = ladder.groupby("period")["published_at"].first()
    assert by_period[QUARTERS[4]] > QUARTERS[4] + timedelta(days=46)
    assert by_period[QUARTERS[9]] > QUARTERS[9] + timedelta(days=46)
    assert by_period[QUARTERS[2]] <= QUARTERS[2] + timedelta(days=46)  # the late restatement is out
    # lot price = quarter mean basis close, mark = quarter-end basis close
    px = panel.prices[panel.prices.ticker == ticker(SPLIT_SECURITY)].copy()
    px["date"] = pd.to_datetime(px["date"]).dt.date
    px["basis"] = px["close"] * [SPLIT_RATIO if d >= SPLIT_EX else 1.0 for d in px["date"]]
    quarter = px[(px.date > QUARTERS[5]) & (px.date <= QUARTERS[6])]
    seed = ladder[(ladder.cusip == cusip(SPLIT_SECURITY)) & (ladder["aggregate"] == "all")].sort_values("period")
    row = seed[seed.period == QUARTERS[6]].iloc[0]
    assert row["cost_basis"] <= max(px.basis) and row["cost_basis"] >= min(px.basis)
    mark = quarter["basis"].iloc[-1]
    assert row["profit_pct"] == pytest.approx((mark - row["cost_basis"]) / row["cost_basis"])
    assert row["unrealised"] == pytest.approx(row["inventory"] * (mark - row["cost_basis"]))
    first = seed.iloc[0]
    assert first["reset"] and first["seed_share"] == 1.0 and first["wavg_age_days"] == 0
    assert (seed["wavg_age_days"].iloc[1:] > 0).all()
    assert (seed["seed_share"].diff().dropna() <= 1e-9).all()  # the seed only ever shrinks
    assert set(ladder.columns) == set(long_ladder.COLUMNS)
    assert not ladder.duplicated(list(long_ladder.KEY)).any()
    assert (pd.to_datetime(ladder["published_at"]) > pd.to_datetime(ladder["period"])).all()
    held = ladder[ladder.inventory > 0]
    assert held[["wavg_age_days", "cost_basis", "profit_pct"]].notna().all().all()
    empty = ladder[ladder.inventory == 0]
    assert empty[["wavg_age_days", "cost_basis", "profit_pct"]].isna().all().all()
    assert len(empty) > 0  # sold-out names keep a zero row


def test_cohort_aggregate_is_the_flagged_managers_only(panel: Panel, ladder: pd.DataFrame) -> None:
    all_rows = ladder[ladder["aggregate"] == "all"]
    cohort_rows = ladder[ladder["aggregate"] == "cohort"]
    assert cohort_rows["inventory"].sum() < all_rows["inventory"].sum()
    assert (cohort_rows["n_managers"] == cohort_rows["n_cohort_managers"]).all()
    merged = all_rows.merge(cohort_rows, on=["cusip", "period"], suffixes=("_all", "_cohort"))
    assert (merged["n_cohort_managers_all"] == merged["n_managers_cohort"]).all()
    assert (merged["inventory_cohort"] <= merged["inventory_all"] + 1e-6).all()


def test_prefix_run_equals_prefix_of_full_run(panel: Panel, ladder: pd.DataFrame) -> None:
    con = duckdb.connect()
    cut = QUARTERS[7] + timedelta(days=long_ladder.LATE_DAYS)
    register(con, panel, upto_filed=cut)
    prefix = long_ladder.compute(con)
    full = ladder[ladder.period <= QUARTERS[7]]
    key = list(long_ladder.KEY)
    a = prefix.sort_values(key).reset_index(drop=True)
    b = full.sort_values(key).reset_index(drop=True)
    assert len(a) == len(b) > 0
    pd.testing.assert_frame_equal(a, b, check_dtype=False)


def test_reconciliation_matches_the_source(panel: Panel, ladder: pd.DataFrame) -> None:
    con = duckdb.connect()
    register(con, panel)
    compared, differing = long_ladder.reconcile(ladder, con)
    assert compared == len(ladder) and differing == 0
    tampered = ladder.copy()
    tampered.loc[tampered.index[:3], "level_raw"] += 1
    assert long_ladder.reconcile(tampered, con) == (len(ladder), 3)
    assert long_ladder.source_latest(con) == QUARTERS[-1]


def test_unmapped_and_unpriced_securities_are_counted_not_invented(panel: Panel) -> None:
    con = duckdb.connect()
    register(con, panel)
    con.execute(f"CREATE OR REPLACE VIEW {long_ladder.MAP_VIEW} AS SELECT * FROM _map WHERE cusip <> '{cusip(0)}'")
    con.execute(f"CREATE OR REPLACE VIEW prices AS SELECT * FROM _prices WHERE ticker <> '{ticker(1)}'")
    frame = long_ladder.compute(con)
    assert frame.attrs["stats"]["unmapped"] == 1 and frame.attrs["stats"]["unpriced"] == 1
    assert not set(frame["cusip"]) & {cusip(0), cusip(1)}
    assert long_ladder.compute(duckdb.connect().execute("CREATE VIEW x AS SELECT 1") and _empty()).empty


def _empty():
    con = duckdb.connect()
    register(con, build_panel())
    con.execute(f"CREATE OR REPLACE VIEW {sec_views.SUBMISSION_VIEW} AS SELECT * FROM _subs WHERE 1 = 0")
    return con
