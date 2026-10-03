"""The short ladder from FINRA short interest (INV-002 R2, DD-001).

Reads ``finra_short_interest``, ``prices`` and ``splits`` from a datacli
DuckDB connection and returns one row per ``(eodhd_code, settlement_date)``.
The schedule of expected observations is the set of settlement dates in the
store, so a symbol missing from a settlement date is counted as missed.

The vendor's splits table mixes share splits with price adjustments for
spin-offs and mergers (KB-003). Prices use every entry, so the basis price is
continuous across all of them; quantities use only the entries the short
interest reports themselves confirm (``confirm_quantity_splits``).

A symbol without prices of its own but with a successor in
``positioning_symbol_alias`` (a rename: the vendor keeps the history under
the new ticker, DD-002 WP13) is priced from the successor's closes and
splits; ``eodhd_code`` stays the reported spelling and ``priced_as`` names
the successor.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from itertools import combinations
from typing import Any, Sequence

import pandas as pd

from positioning import master
from positioning.basis import PricePath, SplitFactor, dedupe_splits
from positioning.ladder import DEFAULT_MAX_GAP, SHORT, Observation, run_ladder

SI_VIEW = "finra_short_interest"
#: Lanes that price US-listed securities; a symbol belongs to exactly one.
PRICE_LANES: tuple[str, ...] = ("us_common", "us_extended", "us_etf")
MATERIAL_LOG_RATIO = 0.3
#: Vendor placeholder closes (0.0001 and 999999.9999 appear on junk days) are not prices.
MIN_CLOSE = 0.0001
MAX_CLOSE = 999999.0


@dataclass(frozen=True)
class Report:
    """One short interest report: raw shares and FINRA's split flag."""

    settlement_date: date
    shares: float
    split_flag: bool = False


def confirm_quantity_splits(
    splits: Sequence[tuple[date, float]],
    reports: Sequence[Report],
    schedule: Sequence[date],
    *,
    max_gap: int = DEFAULT_MAX_GAP,
) -> list[tuple[date, float]]:
    """The splits that changed the reported share count (DD-001 section 1).

    Splits are grouped by the pair of reports they fall between: the first
    report on or after the ex date and the one before it. Among the group's
    candidates (every split when FINRA flagged the later report, only the
    material ones otherwise) the subset whose combined ratio is closest to
    the reported change is applied; a flagged report must apply at least one.
    So a lone flagged split always counts, a lone unflagged one counts when
    it is material and the count moved closer to the ratio than to no change,
    and a spin-off adjustment sharing a day with a real split is told apart
    from it. A group with no usable pair of reports (before the series, or
    across a gap that resets the ladder anyway) counts in full: it only
    rescales, it cannot create a flow.
    """
    position = {d: i for i, d in enumerate(schedule)}
    dates = [r.settlement_date for r in reports]
    groups: dict[int, list[tuple[date, float]]] = {}
    for ex_date, ratio in sorted(splits):
        k = next((i for i, d in enumerate(dates) if d >= ex_date), None)
        if k is not None:  # else: no report after it yet, nothing to rescale
            groups.setdefault(k, []).append((ex_date, ratio))
    confirmed: list[tuple[date, float]] = []
    for k, events in groups.items():
        after = reports[k]
        before = reports[k - 1] if k else None
        if before is None or before.shares <= 0 or after.shares <= 0:
            confirmed.extend(events)
            continue
        a, b = position.get(after.settlement_date), position.get(before.settlement_date)
        if a is not None and b is not None and a - b - 1 > max_gap:
            confirmed.extend(events)
            continue
        moved = math.log(after.shares / before.shares)
        candidates = [
            e for e in events
            if after.split_flag or abs(math.log(e[1])) > MATERIAL_LOG_RATIO
        ]
        best: tuple[tuple[date, float], ...] = ()
        best_error = math.inf if after.split_flag else abs(moved)
        for size in range(1, len(candidates) + 1):
            for subset in combinations(candidates, size):
                error = abs(moved - sum(math.log(r) for _, r in subset))
                if error < best_error:
                    best, best_error = subset, error
        confirmed.extend(best)
    return sorted(confirmed)


def build_observations(
    reports: Sequence[tuple[date, float]],
    closes: Sequence[tuple[date, float]],
    factor: SplitFactor,
    schedule: Sequence[date],
    *,
    quantity_factor: SplitFactor | None = None,
) -> tuple[list[Observation], int]:
    """Observations for one security from raw reports, raw closes and its splits.

    ``reports`` is ``(settlement_date, raw shares)`` sorted by date,
    ``closes`` is ``(date, raw close)`` sorted by date, ``schedule`` the sorted
    settlement dates the source published. ``factor`` rescales prices;
    ``quantity_factor`` rescales share counts and defaults to ``factor``. A
    report with no close in its interval cannot be priced; it is dropped and
    counted as missed, and the count of such reports is returned.
    """
    quantity_factor = factor if quantity_factor is None else quantity_factor
    basis_closes = PricePath([(d, factor.price(c, d)) for d, c in closes])
    position = {d: i for i, d in enumerate(schedule)}
    observations: list[Observation] = []
    unpriced = 0
    previous: date | None = None
    for obs_date, raw in reports:
        lot_price = basis_closes.interval_mean(previous, obs_date)
        mark = basis_closes.last_close(obs_date)
        if lot_price is None or mark is None:
            unpriced += 1
            continue
        missed = 0
        if previous is not None and obs_date in position and previous in position:
            missed = position[obs_date] - position[previous] - 1
        observations.append(
            Observation(
                obs_date, quantity_factor.quantity(raw, obs_date), lot_price, mark, missed
            )
        )
        previous = obs_date
    return observations, unpriced


def compute(
    con: Any,
    codes: Sequence[str] | None = None,
    *,
    lanes: Sequence[str] = PRICE_LANES,
    max_gap: int = DEFAULT_MAX_GAP,
    view: str = SI_VIEW,
) -> pd.DataFrame:
    """Short ladder rows for ``codes`` (all listed common codes with prices when ``None``).

    ``lane`` on each row says where the symbol's prices come from, which is
    also what kind of security it is (``us_etf``: a fund; ``us_extended``:
    outside the qualifying common-stock universe, possibly delisted).
    """
    lane_list = ", ".join(f"'{lane}'" for lane in lanes)
    code_filter = ""
    if codes is not None:
        con.register("_ladder_codes", pd.DataFrame({"code": list(codes)}))
        code_filter = "AND eodhd_code IN (SELECT code FROM _ladder_codes)"
    reports = con.execute(f"""
        SELECT eodhd_code, settlement_date, published_at, short_position, split_adjusted
        FROM {view}
        WHERE listed AND security_kind = 'common' AND eodhd_code IS NOT NULL {code_filter}
        ORDER BY eodhd_code, settlement_date
        """).df()
    schedule = [
        r[0]
        for r in con.execute(
            f"SELECT DISTINCT settlement_date FROM {view} ORDER BY 1"
        ).fetchall()
    ]
    if codes is not None:
        con.unregister("_ladder_codes")
    if reports.empty:
        return pd.DataFrame()
    first = reports["settlement_date"].min()
    alias_of = master.aliases(con)
    wanted = pd.concat(
        [reports[["eodhd_code"]].drop_duplicates(), pd.DataFrame({"eodhd_code": list(alias_of.values())})]
    ).drop_duplicates()
    con.register("_ladder_si_codes", wanted)
    closes = con.execute(f"""
        SELECT ticker, lane, CAST(date AS DATE) AS date, close
        FROM prices
        WHERE lane IN ({lane_list}) AND close > {MIN_CLOSE} AND close < {MAX_CLOSE}
          AND ticker IN (SELECT eodhd_code FROM _ladder_si_codes)
          AND CAST(date AS DATE) >= DATE '{pd.Timestamp(first).date()}' - 40
        ORDER BY ticker, date
        """).df()
    splits = con.execute(f"""
        SELECT ticker, CAST(ex_date AS DATE) AS ex_date, split_ratio
        FROM splits
        WHERE lane IN ({lane_list}) AND split_ratio > 0
          AND ticker IN (SELECT eodhd_code FROM _ladder_si_codes)
        """).df()
    con.unregister("_ladder_si_codes")

    closes_by = {k: g for k, g in closes.groupby("ticker", sort=False)}
    splits_by = {k: g for k, g in splits.groupby("ticker", sort=False)}
    out: list[dict[str, Any]] = []
    for code, group in reports.groupby("eodhd_code", sort=False):
        priced_as = code
        px = closes_by.get(code)
        if px is None and code in alias_of:
            priced_as = alias_of[code]
            px = closes_by.get(priced_as)
        if px is None:
            continue
        lane = str(px["lane"].iloc[0])
        sp = splits_by.get(priced_as)
        events = dedupe_splits(
            [] if sp is None else zip(_dates(sp["ex_date"]), sp["split_ratio"])
        )
        settlement = _dates(group["settlement_date"])
        shares = group["short_position"].astype(float).tolist()
        flags = group["split_adjusted"].fillna(False).astype(bool).tolist()
        typed = [Report(d, s, f) for d, s, f in zip(settlement, shares, flags)]
        price_factor = SplitFactor.from_splits(events)
        quantity_factor = SplitFactor.from_splits(
            confirm_quantity_splits(events, typed, schedule, max_gap=max_gap)
        )
        published = dict(zip(settlement, _dates(group["published_at"])))
        observations, _ = build_observations(
            list(zip(settlement, shares)),
            list(zip(_dates(px["date"]), px["close"].astype(float))),
            price_factor,
            schedule,
            quantity_factor=quantity_factor,
        )
        for row in run_ladder(observations, side=SHORT, max_gap=max_gap):
            record = asdict(row)
            record["eodhd_code"] = code
            record["settlement_date"] = record.pop("obs_date")
            record["published_at"] = published[row.obs_date]
            record["lane"] = lane
            record["priced_as"] = priced_as
            record["quantity_factor"] = quantity_factor.at(row.obs_date)
            record["price_factor"] = price_factor.at(row.obs_date)
            out.append(record)
    frame = pd.DataFrame(out)
    if frame.empty:
        return frame
    lead = ["eodhd_code", "settlement_date", "published_at", "lane"]
    return frame[lead + [c for c in frame.columns if c not in lead]]


def _dates(values: Any) -> list[date]:
    return [pd.Timestamp(v).date() for v in values]
