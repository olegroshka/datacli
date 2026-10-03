"""The long ladder from SEC Form 13F holdings (DD-002 WP9, DD-001).

Reads the amendment-resolved 13F filings (``sec.views.effective_filings_sql``),
``sec_13f_holdings``, ``sec_13f_manager_cohort``, ``positioning_cusip_map``,
``prices`` and ``splits`` from a datacli DuckDB connection and returns one row
per ``(cusip, period, aggregate)``: the FIFO lot ladder of DD-001 run with
``side = LONG`` over the cohort's aggregate shares, quarter by quarter.

Two aggregates are built: ``all`` (every 13F filer) and ``cohort`` (filers
whose Form ADV says they run hedge funds, else advise private funds; NULL
flags are outside the cohort).

**Panel drift (S18).** The flow of a security between two consecutive
periods is the change in shares summed over the managers that filed for
*both* periods; a manager's first or last filing contributes no flow. The
level is the sum over every manager of the later period. The kernel trades
the flow and absorbs the rest (``drift_shares``) by scaling its lots, so the
inventory equals the level and no phantom lot is opened by a filer entering
or leaving (``positioning.ladder``).

**Lateness.** A filing dated more than ``LATE_DAYS`` after its period is
left out of the aggregate (96.8 percent of originals are inside; late
originals and most amendments arrive months to years later, KB-003), so a
period's ``published_at`` is ``period + 45`` days or the latest included
filing date, whichever is later, and never beyond ``period + LATE_DAYS``.

**Basis.** Prices use every vendor split entry; share counts only the entries
the ``all`` aggregate's level confirms, the rule the short ladder uses
(``short_ladder.confirm_quantity_splits``). The lot price of a period is the
mean basis close over the quarter, the mark the basis close at period end.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta
from typing import Any, Sequence

import pandas as pd
import pyarrow as pa

from positioning.basis import SplitFactor, dedupe_splits
from positioning.ladder import DEFAULT_MAX_GAP, LONG, Observation, run_ladder
from positioning.short_ladder import MAX_CLOSE, MIN_CLOSE, PRICE_LANES, Report, confirm_quantity_splits
from sec.views import COHORT_VIEW, HOLDINGS_VIEW, SUBMISSION_VIEW, effective_filings_sql

NAME = "long_ladder"
#: Filings later than this many days after their period are outside the aggregate.
LATE_DAYS = 60
#: The statutory deadline: the earliest a period's aggregate can be complete.
DEADLINE_DAYS = 45
AGGREGATES: tuple[str, ...] = ("all", "cohort")
MAP_VIEW = "positioning_cusip_map"
#: Closes are read from this date; the SEC data sets begin with the 2013Q2 period.
FIRST_PRICE_DATE = date(2013, 1, 1)

SCHEMA = pa.schema(
    [
        ("cusip", pa.string()),
        ("period", pa.date32()),
        ("published_at", pa.date32()),
        ("aggregate", pa.string()),
        ("eodhd_code", pa.string()),
        ("lane", pa.string()),
        ("inventory", pa.float64()),
        ("flow", pa.float64()),
        ("drift_shares", pa.float64()),
        ("n_lots", pa.int32()),
        ("wavg_age_days", pa.float64()),
        ("cost_basis", pa.float64()),
        ("profit_pct", pa.float64()),
        ("unrealised", pa.float64()),
        ("realised", pa.float64()),
        ("seed_share", pa.float64()),
        ("gap", pa.bool_()),
        ("reset", pa.bool_()),
        ("n_managers", pa.int32()),
        ("n_cohort_managers", pa.int32()),
        ("level_raw", pa.float64()),
        ("quantity_factor", pa.float64()),
        ("price_factor", pa.float64()),
    ]
)
COLUMNS = list(SCHEMA.names)
KEY = ("cusip", "period", "aggregate")


def _quarter_end_sql(col: str) -> str:
    return f"(month({col}) IN (3, 6, 9, 12) AND {col} = last_day({col}))"


def aggregate_sql(late_days: int | None = LATE_DAYS) -> str:
    """SQL for the per-period aggregates: one row per ``(cusip, period, aggregate)``.

    Columns: ``level_raw`` (sum of shares over the aggregate's managers),
    ``in_flow`` (shares at this period held by managers who also filed the
    previous period), ``out_flow`` (shares at the previous period held by
    managers who also filed this period), ``n_managers`` (holders in the
    aggregate), ``n_cohort_managers`` (holders in the cohort), ``published_at``.
    ``in_flow - out_flow`` is the flow once both are on one share basis.
    """
    member = {"all": "TRUE", "cohort": "cohort IS TRUE"}
    prev = {"all": "prev_all", "cohort": "prev_cohort"}
    nxt = {"all": "next_all", "cohort": "next_cohort"}
    parts = []
    for agg in AGGREGATES:
        parts.append(
            f"SELECT '{agg}' AS aggregate, cusip, period, "
            f"sum(shares) AS level_raw, count(*) AS n_managers, "
            f"count(*) FILTER (WHERE cohort IS TRUE) AS n_cohort_managers "
            f"FROM _joined WHERE {member[agg]} GROUP BY cusip, period"
        )
    levels = " UNION ALL ".join(parts)
    parts = []
    for agg in AGGREGATES:
        parts.append(
            f"SELECT '{agg}' AS aggregate, cusip, period, sum(shares) AS in_flow "
            f"FROM _joined WHERE {member[agg]} AND {prev[agg]} GROUP BY cusip, period"
        )
    inflows = " UNION ALL ".join(parts)
    parts = []
    for agg in AGGREGATES:
        parts.append(
            f"SELECT '{agg}' AS aggregate, j.cusip, s.period, sum(j.shares) AS out_flow "
            f"FROM _joined j JOIN _sched s ON s.k = j.k + 1 "
            f"WHERE {member[agg]} AND {nxt[agg]} GROUP BY j.cusip, s.period"
        )
    outflows = " UNION ALL ".join(parts)
    return f"""
WITH _eff AS ({effective_filings_sql(late_days)}),
_sched AS (
  SELECT period, row_number() OVER (ORDER BY period) AS k,
         greatest(period + {DEADLINE_DAYS}, max(filing_date)) AS published_at
  FROM _eff WHERE {_quarter_end_sql('period')} GROUP BY period),
_filers AS (
  SELECT e.cik, e.period, s.k,
         bool_or(coalesce(m.any_hedge_funds, m.advises_private_funds)) AS cohort
  FROM _eff e JOIN _sched s USING (period)
  LEFT JOIN {COHORT_VIEW} m USING (accession_number)
  GROUP BY e.cik, e.period, s.k),
_present AS (
  SELECT f.cik, f.period, f.k, f.cohort,
         p.cik IS NOT NULL AS prev_all, p.cohort IS TRUE AS prev_cohort,
         n.cik IS NOT NULL AS next_all, n.cohort IS TRUE AS next_cohort
  FROM _filers f
  LEFT JOIN _filers p ON p.cik = f.cik AND p.k = f.k - 1
  LEFT JOIN _filers n ON n.cik = f.cik AND n.k = f.k + 1),
_hold AS (
  SELECT h.cik, h.period, h.cusip, sum(h.shares) AS shares
  FROM {HOLDINGS_VIEW} h JOIN _eff e USING (accession_number)
  WHERE h.share_type = 'SH' AND h.put_call IS NULL AND h.shares > 0
    AND h.cusip IS NOT NULL AND h.cusip <> ''
  GROUP BY h.cik, h.period, h.cusip),
_joined AS (
  SELECT h.cik, h.period, h.cusip, h.shares, p.k, p.cohort,
         p.prev_all, p.prev_cohort, p.next_all, p.next_cohort
  FROM _hold h JOIN _present p USING (cik, period)),
_levels AS ({levels}),
_in AS ({inflows}),
_out AS ({outflows}),
_keys AS (
  SELECT aggregate, cusip, period FROM _levels
  UNION SELECT aggregate, cusip, period FROM _out)
SELECT k.aggregate, k.cusip, k.period, s.published_at,
       coalesce(l.level_raw, 0.0) AS level_raw,
       coalesce(i.in_flow, 0.0) AS in_flow, coalesce(o.out_flow, 0.0) AS out_flow,
       coalesce(l.n_managers, 0) AS n_managers, coalesce(l.n_cohort_managers, 0) AS n_cohort_managers
FROM _keys k
JOIN _sched s USING (period)
LEFT JOIN _levels l USING (aggregate, cusip, period)
LEFT JOIN _in i USING (aggregate, cusip, period)
LEFT JOIN _out o USING (aggregate, cusip, period)
"""


def aggregate_flows(con: Any, *, late_days: int | None = LATE_DAYS) -> pd.DataFrame:
    """The per-period aggregates (``aggregate_sql``) as a frame sorted by key."""
    frame = con.execute(aggregate_sql(late_days)).df()
    if frame.empty:
        return frame
    frame["period"] = pd.to_datetime(frame["period"]).dt.date
    frame["published_at"] = pd.to_datetime(frame["published_at"]).dt.date
    return frame.sort_values(["aggregate", "cusip", "period"], kind="stable").reset_index(drop=True)


def resolve_codes(con: Any, cusips: Sequence[str]) -> pd.DataFrame:
    """One ``eodhd_code`` per CUSIP from ``positioning_cusip_map``: the latest pairing wins.

    A CUSIP seen under several tickers over time is a rename; the ticker seen
    last is the one the vendor keeps the history under. Ties go to the pair
    seen on more days.
    """
    con.register("_ladder_cusips", pd.DataFrame({"cusip": list(cusips)}))
    try:
        frame = con.execute(f"""
            SELECT cusip, eodhd_code FROM (
              SELECT m.cusip, m.eodhd_code,
                     row_number() OVER (PARTITION BY m.cusip ORDER BY m.last_seen DESC, m.n_days DESC, m.eodhd_code) AS rn
              FROM {MAP_VIEW} m JOIN _ladder_cusips c USING (cusip)
              WHERE m.eodhd_code IS NOT NULL AND m.eodhd_code <> '')
            WHERE rn = 1
            """).df()
    finally:
        con.unregister("_ladder_cusips")
    return frame


def quarter_prices(
    con: Any,
    codes: Sequence[str],
    price_factors: dict[str, SplitFactor],
    *,
    lanes: Sequence[str] = PRICE_LANES,
    first_date: date = FIRST_PRICE_DATE,
) -> pd.DataFrame:
    """Per ``(eodhd_code, period)``: ``lane``, ``lot_price`` (mean basis close over the
    quarter) and ``mark_price`` (basis close at period end), on the price basis."""
    steps = [
        (code, ex_date, cumulative)
        for code, factor in price_factors.items()
        for ex_date, cumulative in zip(factor.ex_dates, factor.cumulative)
    ]
    con.register("_ladder_codes", pd.DataFrame({"ticker": list(codes)}))
    con.register(
        "_ladder_factors",
        pd.DataFrame(steps, columns=["ticker", "ex_date", "cumulative"]).astype(
            {"ticker": "string", "cumulative": "float64"}
        ),
    )
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    try:
        frame = con.execute(f"""
            WITH px AS (
              SELECT p.ticker, p.lane, CAST(p.date AS DATE) AS date, p.close
              FROM prices p JOIN _ladder_codes c USING (ticker)
              WHERE p.lane IN ({lane_list}) AND p.close > {MIN_CLOSE} AND p.close < {MAX_CLOSE}
                AND CAST(p.date AS DATE) >= DATE '{first_date.isoformat()}'),
            based AS (
              SELECT px.ticker, px.lane, px.date,
                     px.close * coalesce(f.cumulative, 1.0) AS basis_close,
                     last_day(date_trunc('quarter', px.date) + INTERVAL 2 MONTH) AS period
              FROM px ASOF LEFT JOIN _ladder_factors f
                ON f.ticker = px.ticker AND CAST(f.ex_date AS DATE) <= px.date)
            SELECT ticker AS eodhd_code, any_value(lane) AS lane, period,
                   avg(basis_close) AS lot_price,
                   arg_max(basis_close, date) AS mark_price
            FROM based GROUP BY ticker, period
            """).df()
    finally:
        con.unregister("_ladder_codes")
        con.unregister("_ladder_factors")
    if not frame.empty:
        frame["period"] = pd.to_datetime(frame["period"]).dt.date
    return frame


def _load_splits(con: Any, codes: Sequence[str], lanes: Sequence[str]) -> dict[str, list[tuple[date, float]]]:
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    con.register("_ladder_codes", pd.DataFrame({"ticker": list(codes)}))
    try:
        frame = con.execute(f"""
            SELECT s.ticker, CAST(s.ex_date AS DATE) AS ex_date, s.split_ratio
            FROM splits s JOIN _ladder_codes c USING (ticker)
            WHERE s.lane IN ({lane_list}) AND s.split_ratio > 0
            """).df()
    finally:
        con.unregister("_ladder_codes")
    out: dict[str, list[tuple[date, float]]] = {}
    for code, group in frame.groupby("ticker", sort=False):
        out[str(code)] = dedupe_splits(
            zip((pd.Timestamp(v).date() for v in group["ex_date"]), group["split_ratio"].astype(float))
        )
    return out


def compute(
    con: Any,
    *,
    late_days: int | None = LATE_DAYS,
    lanes: Sequence[str] = PRICE_LANES,
    max_gap: int = DEFAULT_MAX_GAP,
) -> pd.DataFrame:
    """Long ladder rows for every priced security and both aggregates.

    ``frame.attrs['stats']`` counts what was left out: securities with no
    ticker in the CUSIP map, with a ticker but no prices, and periods a
    priced security could not be priced in (missed observations).
    """
    flows = aggregate_flows(con, late_days=late_days)
    stats = {"securities": 0, "unmapped": 0, "unpriced": 0, "unpriced_periods": 0, "periods": 0}
    if flows.empty:
        frame = pd.DataFrame(columns=COLUMNS)
        frame.attrs["stats"] = stats
        return frame
    schedule = sorted(flows["period"].unique())
    stats["periods"] = len(schedule)
    position = {p: i for i, p in enumerate(schedule)}
    cusips = flows["cusip"].unique()
    stats["securities"] = int(len(cusips))
    codes = resolve_codes(con, cusips)
    code_of = dict(zip(codes["cusip"], codes["eodhd_code"]))
    stats["unmapped"] = int(len(cusips) - len(code_of))
    unique_codes = sorted(set(code_of.values()))
    splits_by = _load_splits(con, unique_codes, lanes)
    price_factors = {code: SplitFactor.from_splits(ev) for code, ev in splits_by.items()}
    prices = quarter_prices(con, unique_codes, price_factors, lanes=lanes)
    priced_codes = set(prices["eodhd_code"]) if not prices.empty else set()
    stats["unpriced"] = int(sum(1 for c in code_of.values() if c not in priced_codes))
    prices_by = {k: g for k, g in prices.groupby("eodhd_code", sort=False)} if not prices.empty else {}

    out: list[dict[str, Any]] = []
    for cusip, security in flows.groupby("cusip", sort=False):
        code = code_of.get(cusip)
        if code is None or code not in prices_by:
            continue
        px = prices_by[code]
        lane = str(px["lane"].iloc[0])
        lot_of = dict(zip(px["period"], px["lot_price"].astype(float)))
        mark_of = dict(zip(px["period"], px["mark_price"].astype(float)))
        events = splits_by.get(code, [])
        price_factor = price_factors.get(code, SplitFactor.from_splits([]))
        # the ``all`` aggregate's raw level confirms which entries changed share counts
        whole = security[security["aggregate"] == "all"].sort_values("period")
        reports = [Report(p, float(q)) for p, q in zip(whole["period"], whole["level_raw"])]
        quantity_factor = SplitFactor.from_splits(
            confirm_quantity_splits(events, reports, schedule, max_gap=max_gap)
        )
        for agg, group in security.groupby("aggregate", sort=False):
            group = group.sort_values("period")
            observations: list[Observation] = []
            meta: list[dict[str, Any]] = []
            previous: date | None = None
            for rec in group.itertuples(index=False):
                period = rec.period
                lot_price = lot_of.get(period)
                mark = mark_of.get(period)
                if lot_price is None or mark is None or not lot_price > 0 or not mark > 0:
                    stats["unpriced_periods"] += 1
                    continue
                fq = quantity_factor.at(period)
                missed = 0 if previous is None else position[period] - position[previous] - 1
                reset = previous is None or missed > max_gap
                if reset:
                    flow: float | None = None
                    lot_price = mark  # a first observation has no interval (DD-001 section 2)
                elif missed:
                    # the flow refers to the schedule's previous period, which the ladder
                    # did not observe (unpriced): the change is a trade, as across a short
                    # ladder gap (DD-001 section 3), and the row carries gap = true
                    flow = None
                else:
                    assert previous is not None
                    flow = float(rec.in_flow) / fq - float(rec.out_flow) / quantity_factor.at(previous)
                observations.append(
                    Observation(period, float(rec.level_raw) / fq, lot_price, mark, missed, flow)
                )
                meta.append(
                    {
                        "published_at": rec.published_at,
                        "n_managers": int(rec.n_managers),
                        "n_cohort_managers": int(rec.n_cohort_managers),
                        "level_raw": float(rec.level_raw),
                        "quantity_factor": fq,
                        "price_factor": price_factor.at(period),
                    }
                )
                previous = period
            for row, extra in zip(run_ladder(observations, side=LONG, max_gap=max_gap), meta):
                record = asdict(row)
                record["period"] = record.pop("obs_date")
                record["drift_shares"] = record.pop("drift")
                record.update(extra)
                record.update({"cusip": cusip, "aggregate": agg, "eodhd_code": code, "lane": lane})
                out.append(record)
    frame = pd.DataFrame(out, columns=COLUMNS)
    frame.attrs["stats"] = stats
    return frame


def source_latest(con: Any) -> date | None:
    """The newest period the 13F store has holdings reports for."""
    row = con.execute(
        f"SELECT max(period) FROM {SUBMISSION_VIEW} WHERE submission_type LIKE '13F-HR%'"
    ).fetchone()
    return None if row is None or row[0] is None else pd.Timestamp(row[0]).date()


def reconcile(stored: pd.DataFrame, con: Any, *, late_days: int | None = LATE_DAYS) -> tuple[int, int]:
    """``(rows compared, rows whose level_raw differs from the source)``: the qc oracle."""
    flows = aggregate_flows(con, late_days=late_days)
    if flows.empty or stored.empty:
        return 0, 0
    merged = stored[["cusip", "period", "aggregate", "level_raw"]].merge(
        flows[["cusip", "period", "aggregate", "level_raw"]].rename(columns={"level_raw": "source"}),
        on=["cusip", "period", "aggregate"],
        how="left",
    )
    diff = (merged["source"].isna()) | ((merged["source"] - merged["level_raw"]).abs() > 1e-6)
    return int(len(merged)), int(diff.sum())


def late_cutoff(period: date, late_days: int | None = LATE_DAYS) -> date | None:
    return None if late_days is None else period + timedelta(days=late_days)
