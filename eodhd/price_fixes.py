"""Corrections to the US daily prices from a second source: Alpaca's SIP daily bars.

EODHD stays the source of the US daily lanes (``us_common``, ``us_etf``,
``us_extended``) and its files are never rewritten here. ``eodhd fix-prices``
compares a ticker's EODHD history with Alpaca's consolidated-tape daily bars
(as traded and split-and-dividend adjusted, from 2016) and writes the bars it
can show are wrong to an overlay beside the lanes::

    <eodhd root>/_fixes/us_daily_prices.parquet   one row per corrected (lane, ticker, date)
    <eodhd root>/_fixes/us_daily_checks.parquet   one row per checked (lane, ticker)

The readers (the ``prices`` view of ``explore_eodhd``, ``daily_store`` and the
ISA export) apply the overlay through :func:`overlay_sql`; ``prices_vendor``
keeps the plain EODHD rows. A fix carries the EODHD values it was made against
and applies only while the EODHD row still holds them, so a vendor
re-delivery switches it off (the next run re-checks that ticker) instead of
stacking two corrections. Deleting ``_fixes`` returns every reader to plain
EODHD.

Rules, per (lane, ticker), over the days both sources have:

1. identity: EODHD's close must agree with Alpaca's (as traded, or adjusted
   for the names whose history the vendor delivers adjusted) within 2% on 80%
   of the common days, else the two symbols name different securities and
   nothing is fixed;
2. tail: bars after Alpaca's last bar are dropped when they have no volume
   (placeholders after a delisting), or all of them when the first traded one
   sits more than 2x above Alpaca's last close with no split to explain it
   (another listing filed under the ticker; a fall is kept: it can be the
   real move to OTC trading);
3. interior: a bar Alpaca does not have, with no volume and a price moved
   more than 1.5x from the bar before, is dropped;
4. close: a common day whose close is more than 1.5x off Alpaca's takes
   Alpaca's bar (open, high, low, close; split-restated volume). A smaller gap
   proves nothing: Alpaca's close is the last trade, the vendor's the official
   close, and on a thin name they differ by several percent;
5. adjustment: between consecutive common days on which both sources print
   the same close, EODHD's adjusted return must agree with Alpaca's within
   0.25 in log terms. Where it does not and Alpaca's move is the smaller one
   (a split applied on the wrong day or not at all), Alpaca's return replaces
   it and the adjusted closes are re-chained from the last clean day, so the
   history before the event moves by the missing factor (moves under 1% are
   left alone). Where Alpaca's move is the larger one, or the closes differ,
   EODHD is kept and the day is counted as ``disputed``.

An Alpaca bar without volume is a placeholder (the last price carried over a
halt or a spell off the tape) and counts as no bar. Finally, do no harm: a
ticker's fixes are kept only if the corrected adjusted series has no more
moves beyond 1.5x, and not clearly more total movement, than the vendor's
(else the drops alone, else nothing; counted as ``rejected``).

Bars Alpaca cannot speak to (before 2016, OTC names, days it has no bar for
that carry volume) are left as EODHD delivered them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

US_LANES: tuple[str, ...] = ("us_common", "us_etf", "us_extended")
PRICES_FILE = "prices_daily.parquet"
SPLITS_FILE = "splits_history.parquet"
FIXES_DIR = "_fixes"
FIXES_FILE = "us_daily_prices.parquet"
CHECKS_FILE = "us_daily_checks.parquet"
SOURCE = "alpaca_sip"
PRICE_COLUMNS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
)
DROP, REPLACE = "drop", "replace"

# check statuses
CHECKED = "checked"
NO_BARS = "no_alpaca_bars"
FEW_DAYS = "too_few_common_days"
OTHER_SECURITY = "identity_mismatch"
UNSUPPORTED = "unsupported_symbol"

#: what Alpaca accepts as a symbol (BRK.B); anything else would fail the whole request
VALID_SYMBOL = re.compile(r"[A-Z][A-Z0-9.]{0,15}")

# fix reasons
TAIL_PLACEHOLDER = "placeholder_after_last_trade"
TAIL_LISTING = "other_listing_after_last_trade"
INTERIOR_PLACEHOLDER = "placeholder_moved_price"
CLOSE = "close"
ADJUSTMENT = "adjustment"

FIX_COLUMNS: tuple[str, ...] = (
    "lane",
    "ticker",
    "date",
    "action",
    *PRICE_COLUMNS,
    "orig_close",
    "orig_adjusted_close",
    "reason",
    "source",
    "checked_at",
)
CHECK_COLUMNS: tuple[str, ...] = (
    "lane",
    "ticker",
    "symbol",
    "asof",
    "status",
    "basis",
    "identity",
    "common_days",
    "alpaca_first",
    "alpaca_last",
    "eodhd_last",
    "drops",
    "closes",
    "adjustments",
    "disputed",
    "rejected",
    "max_adjustment",
    "checked_at",
)
NEW_YORK = "America/New_York"


# --------------------------------------------------------------------------- #
# paths and the overlay
# --------------------------------------------------------------------------- #
def fixes_path(root: Path | str) -> Path:
    return Path(root) / FIXES_DIR / FIXES_FILE


def checks_path(root: Path | str) -> Path:
    return Path(root) / FIXES_DIR / CHECKS_FILE


def overlay_sql(source: str, lane: str, fixes: Path | str | None) -> str:
    """``source`` (a FROM-able price relation of ``lane``) with the fixes applied.

    Same columns as ``source``: a ``drop`` fix removes its row, a ``replace``
    fix swaps the six price columns. A fix applies only to the row it was made
    against (same close and adjusted close). Without a fixes file, or for a
    lane the fixes do not cover, ``source`` comes back unchanged.
    """
    if fixes is None or lane not in US_LANES or not Path(fixes).exists():
        return source
    path = Path(fixes).as_posix()
    swaps = ", ".join(
        f"CASE WHEN _f.action = '{REPLACE}' THEN _f.{c} ELSE _p.{c} END AS {c}"
        for c in PRICE_COLUMNS
    )
    return (
        f"(SELECT _p.* REPLACE ({swaps}) FROM {source} AS _p "
        f"LEFT JOIN (SELECT * FROM read_parquet('{path}') WHERE lane = '{lane}') AS _f "
        "ON _f.ticker = _p.ticker AND _f.date = TRY_CAST(_p.date AS DATE) "
        "AND _f.orig_close IS NOT DISTINCT FROM _p.close "
        "AND _f.orig_adjusted_close IS NOT DISTINCT FROM _p.adjusted_close "
        f"WHERE _f.action IS DISTINCT FROM '{DROP}')"
    )


# --------------------------------------------------------------------------- #
# the comparison (pure)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rules:
    """Thresholds of the rules in the module docstring."""

    history_start: dt.date = dt.date(2016, 1, 1)
    identity_tolerance: float = 0.02
    identity_min_share: float = 0.8
    min_common_days: int = 20
    close_tolerance: float = 0.05  # log; a vendor bar without volume this far off
    # a close or a volume-less bar's move this far off (log) is wrong on its face
    gross_move: float = math.log(1.5)
    return_tolerance: float = 0.25  # log
    # adjusted returns are compared only between sessions this close (calendar
    # days) with no vendor trading day between them
    max_gap_days: int = 10
    listing_jump: float = math.log(2.0)
    # the other-listing rule needs a last Alpaca close at least this high
    min_listing_price: float = 1.0
    split_match: float = 0.1  # log distance at which a split explains a jump
    split_window_days: int = 7
    # adjusted closes below this are rounded too coarsely to compare returns
    min_adjusted_price: float = 0.10
    # closes below this are not compared (four-decimal rounding)
    min_close: float = 0.01
    # a re-chained adjusted close moving less than this (log) keeps EODHD's value:
    # two sources' returns across an event rarely cancel exactly, and a 0.3%
    # shift of years of history is noise, not a fix
    min_adjustment: float = 0.01
    # do no harm: fixes may not add total log movement beyond this (absolute,
    # or as a share of the vendor's), nor any move beyond 1.5x
    movement_slack: float = 0.25
    movement_slack_share: float = 0.02


RULES = Rules()


@dataclass
class TickerCheck:
    fixes: pd.DataFrame
    summary: dict[str, Any]


def empty_fixes() -> pd.DataFrame:
    frame = pd.DataFrame({c: pd.Series(dtype="float64") for c in FIX_COLUMNS})
    for c in ("lane", "ticker", "action", "reason", "source", "checked_at"):
        frame[c] = frame[c].astype("object")
    frame["date"] = pd.Series(dtype="datetime64[ns]")
    return frame


def bars_frame(records: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Alpaca bar records -> ``date`` (the New York session), ``open .. volume``."""
    if not records:
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume"]
        ).astype({"date": "datetime64[ns]"})
    frame = pd.DataFrame.from_records(records)
    stamp = pd.to_datetime(frame["t"], utc=True).dt.tz_convert(NEW_YORK)
    out = pd.DataFrame(
        {
            "date": stamp.dt.tz_localize(None).dt.normalize(),
            "open": frame["o"].astype(float),
            "high": frame["h"].astype(float),
            "low": frame["l"].astype(float),
            "close": frame["c"].astype(float),
            "volume": frame["v"].astype(float),
        }
    )
    return out.drop_duplicates("date", keep="last").sort_values(
        "date", ignore_index=True
    )


def _eod_frame(eod: pd.DataFrame) -> pd.DataFrame:
    frame = eod.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame = frame.sort_values("date", kind="stable").drop_duplicates(
        "date", keep="first"
    )
    for c in PRICE_COLUMNS:
        frame[c] = pd.to_numeric(frame[c], errors="coerce").astype(float)
    return frame.reset_index(drop=True)


def _log_ratio(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.log(a / b)
    out[~((a > 0) & (b > 0))] = np.nan
    return out


def _share_within(
    a: np.ndarray, b: np.ndarray, tolerance: float, floor: float
) -> tuple[float, int]:
    """Share of the comparable days (both at or above ``floor``) within ``tolerance``, and their count."""
    valid = (a >= floor) & (b >= floor)
    if not valid.any():
        return 0.0, 0
    share = float((np.abs(_log_ratio(a[valid], b[valid])) <= tolerance).mean())
    return share, int(valid.sum())


def _roughness(adjusted: np.ndarray) -> tuple[int, float]:
    """(moves beyond 1.5x either way, total absolute log movement) of a price series."""
    values = adjusted[np.isfinite(adjusted) & (adjusted > 0)]
    if len(values) < 2:
        return 0, 0.0
    steps = np.abs(np.diff(np.log(values)))
    return int((steps > math.log(1.5)).sum()), float(steps.sum())


def _adds_moves(vendor: np.ndarray, fixed: np.ndarray, kept: np.ndarray) -> bool:
    """Whether the fixes put a move beyond 1.5x on a day the vendor has none."""
    rows = kept & np.isfinite(vendor) & (vendor > 0) & np.isfinite(fixed) & (fixed > 0)
    if rows.sum() < 2:
        return False
    step_v = np.abs(np.diff(np.log(vendor[rows])))
    step_f = np.abs(np.diff(np.log(fixed[rows])))
    big = math.log(1.5)
    return bool(((step_f > big) & (step_v <= big)).any())


def _worse(fixed: tuple[int, float], vendor: tuple[int, float], rules: "Rules") -> bool:
    jumps_f, total_f = fixed
    jumps_v, total_v = vendor
    slack = max(rules.movement_slack, rules.movement_slack_share * total_v)
    return jumps_f > jumps_v or total_f > total_v + slack


def _other_listing(
    tail: pd.DataFrame,
    ref_close: float,
    splits: Sequence[tuple[pd.Timestamp, float]],
    rules: Rules,
) -> bool:
    """Whether the bars after Alpaca's last one belong to another listing.

    Only an upward jump counts: a security that leaves the consolidated tape
    for OTC trading often falls hard on the way, and that fall is real (keeping
    it errs against a backtest, dropping it would flatter one).
    """
    traded = tail[(tail["volume"].fillna(0) > 0) & (tail["close"] > 0)]
    # a penny security (a warrant) doubles in a day on its own (PUCKW)
    if traded.empty or not ref_close >= rules.min_listing_price:
        return False
    first = traded.iloc[0]
    if not first["close"] > 0:
        return False
    jump = math.log(first["close"] / ref_close)
    if jump <= rules.listing_jump:
        return False
    level = math.log(float(traded["close"].median()) / ref_close)
    if level <= rules.listing_jump:
        return False
    for ex_date, ratio in splits:
        if (
            ratio > 0
            and abs((pd.Timestamp(ex_date) - first["date"]).days)
            <= rules.split_window_days
            and abs(jump + math.log(ratio)) < rules.split_match
        ):
            return False
    return True


def check_ticker(
    eod: pd.DataFrame,
    raw: pd.DataFrame,
    adj: pd.DataFrame,
    *,
    lane: str,
    ticker: str,
    rules: Rules = RULES,
    splits: Sequence[tuple[pd.Timestamp, float]] = (),
    checked_at: str = "",
) -> TickerCheck:
    """Compare one (lane, ticker) EODHD history with Alpaca's bars; return its fixes.

    ``eod`` has ``date`` and the six price columns (its whole history: an
    adjustment fix moves every earlier adjusted close, before 2016 too).
    ``raw`` and ``adj`` are :func:`bars_frame` frames of Alpaca's as-traded and
    ``all``-adjusted bars. ``splits`` are EODHD's ``(ex_date, ratio)`` of the
    ticker, used to tell a split from another listing.
    """
    e = _eod_frame(eod)
    summary: dict[str, Any] = {
        "lane": lane,
        "ticker": ticker,
        "status": NO_BARS,
        "basis": None,
        "identity": None,
        "common_days": 0,
        "alpaca_first": None,
        "alpaca_last": None,
        "eodhd_last": e["date"].max() if len(e) else None,
        "drops": 0,
        "closes": 0,
        "adjustments": 0,
        "disputed": 0,
        "rejected": 0,
        "max_adjustment": 0.0,
        "checked_at": checked_at,
    }
    # A session without volume is a placeholder (the last price carried over a
    # halt or a spell off the tape: SMCI's OTC months in 2019 show 15.65 every
    # day), not a print, so it is no evidence against the vendor.
    raw = raw[raw["volume"].fillna(0) > 0]
    adj = adj[adj["volume"].fillna(0) > 0]
    if e.empty or raw.empty or adj.empty:
        return TickerCheck(empty_fixes(), summary)
    a_raw = raw.set_index("date")
    a_adj = adj.set_index("date")
    a_first, a_last = a_adj.index.min(), a_adj.index.max()
    summary.update(alpaca_first=a_first, alpaca_last=a_last)

    dates = e["date"]
    both = (dates.isin(a_adj.index) & dates.isin(a_raw.index)).to_numpy()
    summary["common_days"] = int(both.sum())
    if both.sum() < rules.min_common_days:
        summary["status"] = FEW_DAYS
        return TickerCheck(empty_fixes(), summary)

    close = e["close"].to_numpy(float)
    raw_close = a_raw["close"].reindex(dates).to_numpy(float)
    adj_close = a_adj["close"].reindex(dates).to_numpy(float)
    share_raw = _share_within(
        close[both], raw_close[both], rules.identity_tolerance, rules.min_close
    )
    share_adj = _share_within(
        close[both], adj_close[both], rules.identity_tolerance, rules.min_close
    )
    (share_raw, n_raw), (share_adj, n_adj) = share_raw, share_adj
    basis = "raw" if share_raw >= share_adj else "adjusted"
    identity, compared = (share_raw, n_raw) if basis == "raw" else (share_adj, n_adj)
    summary.update(basis=basis, identity=round(identity, 4))
    if compared < rules.min_common_days:
        # sub-cent vendor closes (rounded adjusted history) leave too few days to
        # tell the security apart
        summary["status"] = FEW_DAYS
        return TickerCheck(empty_fixes(), summary)
    if identity < rules.identity_min_share:
        summary["status"] = OTHER_SECURITY
        return TickerCheck(empty_fixes(), summary)
    summary["status"] = CHECKED
    bars = a_raw if basis == "raw" else a_adj
    basis_close = raw_close if basis == "raw" else adj_close

    # ----- drops --------------------------------------------------------- #
    n = len(e)
    reason = np.full(n, "", dtype=object)
    volume = e["volume"].fillna(0.0).to_numpy(float)
    after = (dates > a_last).to_numpy()
    if after.any():
        ref = float(bars["close"].reindex([a_last]).iloc[0])
        if not ref > 0:
            ref = float(a_adj["close"].loc[a_last])
        if _other_listing(e[after], ref, splits, rules):
            reason[after] = TAIL_LISTING
        else:
            reason[after & (volume <= 0)] = TAIL_PLACEHOLDER
    inside = ((dates >= a_first) & (dates <= a_last)).to_numpy()
    previous = np.r_[np.nan, close[:-1]]
    moved = (
        np.abs(np.nan_to_num(_log_ratio(close, previous), nan=0.0)) > rules.gross_move
    )
    placeholder = ~both & inside & (volume <= 0) & moved & (reason == "")
    reason[placeholder] = INTERIOR_PLACEHOLDER
    dropped = reason != ""

    # ----- closes -------------------------------------------------------- #
    # Only a gross gap is proof, and only where the vendor's own series breaks:
    # Alpaca's daily close is the session's last trade, the vendor's (and
    # Yahoo's) the official close, and on a thin name the last trade can be a
    # stray print far from it (GRN, SBET; IFMK 2017-02-14 at 7%). A vendor error
    # jumps away from the vendor's last close that agreed with Alpaca while
    # Alpaca's close stays near it (WILC 8948, QUBX's split a day early).
    comparable = both & ~dropped & (basis_close >= rules.min_close)
    off = np.abs(_log_ratio(close, basis_close))
    agrees = comparable & (off <= rules.close_tolerance)
    ref = pd.Series(np.where(agrees, close, np.nan)).ffill().shift(1).to_numpy()
    has_ref = np.isfinite(ref)
    vendor_jump = np.abs(np.nan_to_num(_log_ratio(close, ref), nan=np.inf))
    alpaca_jump = np.abs(np.nan_to_num(_log_ratio(basis_close, ref), nan=np.inf))
    bad_close = (
        comparable
        & has_ref
        & ~(off <= rules.gross_move)  # NaN (no vendor close) counts
        & (vendor_jump > rules.gross_move)
        & (alpaca_jump <= rules.gross_move)
    )

    # ----- adjustment chain --------------------------------------------- #
    adj_e = e["adjusted_close"].to_numpy(float)
    keep = ~dropped & np.isfinite(adj_e) & (adj_e > 0)
    k = np.flatnonzero(keep)
    new_adj = adj_e.copy()
    adj_changed = np.zeros(n, dtype=bool)
    disputed = 0
    if len(k):
        level_e = np.log(adj_e[k])
        alp = adj_close[k]
        level_a = np.full(len(k), np.nan)
        ok_a = alp >= rules.min_adjusted_price
        level_a[ok_a] = np.log(alp[ok_a])
        has = np.isfinite(level_a) & both[k]
        bad_k = bad_close[k]
        # both sources print the same close: a disagreement is the adjustment's
        agree_k = (np.abs(np.nan_to_num(off, nan=np.inf)) <= rules.close_tolerance)[k]
        step = np.diff(level_e, prepend=level_e[0])
        mismatch = np.zeros(len(k), dtype=bool)
        pos = np.flatnonzero(has)
        if len(pos) >= 2:
            i, j = pos[:-1], pos[1:]
            ret_e = level_e[j] - level_e[i]
            ret_a = level_a[j] - level_a[i]
            # Compare neighbouring sessions only: across a spell Alpaca has no
            # prints for (DCTH's OTC months), the vendor's series is the real one.
            k_dates = dates.to_numpy()[k]
            traded = np.cumsum(volume[k] > 0)
            gap_days = (k_dates[j] - k_dates[i]) / np.timedelta64(1, "D")
            near = (gap_days <= rules.max_gap_days) & (traded[j - 1] - traded[i] == 0)
            forced = (bad_k[i] | bad_k[j]) & near
            differs = near & (np.abs(ret_e - ret_a) > rules.return_tolerance)
            smaller = np.abs(ret_a) < np.abs(ret_e)
            same_prints = agree_k[i] & agree_k[j]
            take = forced | (differs & smaller & same_prints)
            disputed = int((differs & ~forced & ~(smaller & same_prints)).sum())
            for ii, jj, ra in zip(i[take], j[take], ret_a[take]):
                step[ii + 1 : jj] = 0.0
                step[jj] = ra
                mismatch[jj] = True
        clean = np.flatnonzero(has & ~mismatch & ~bad_k)
        if len(clean):
            anchor = clean[-1]
        elif len(pos):
            anchor = pos[-1]
        else:
            anchor = len(k) - 1
        cumulative = np.cumsum(step)
        level = level_e[anchor] + (cumulative - cumulative[anchor])
        moved_k = np.abs(level - level_e) > rules.min_adjustment
        new_adj[k] = np.where(moved_k, np.exp(level), adj_e[k])
        adj_changed[k] = moved_k
        if moved_k.any():
            summary["max_adjustment"] = float(
                np.exp(np.abs(level - level_e)[moved_k].max())
            )
    summary["disputed"] = disputed

    # ----- do no harm ---------------------------------------------------- #
    # Every rule exists to remove a false move. A set of fixes that leaves the
    # adjusted series with more big moves, or clearly more total movement, than
    # the vendor's is wrong somewhere: fall back to the drops alone, else to nothing.
    vendor = _roughness(adj_e)
    proposed = int((dropped | bad_close | adj_changed).sum())
    if _worse(_roughness(new_adj[~dropped]), vendor, rules) or _adds_moves(
        adj_e, new_adj, ~dropped
    ):
        bad_close = np.zeros(n, dtype=bool)
        adj_changed = np.zeros(n, dtype=bool)
        new_adj = adj_e.copy()
        if _worse(_roughness(adj_e[~dropped]), vendor, rules):
            dropped = np.zeros(n, dtype=bool)
        summary["rejected"] = proposed - int(dropped.sum())
        summary["max_adjustment"] = 0.0

    # ----- fix rows ------------------------------------------------------ #
    replace = ~dropped & (bad_close | adj_changed)
    summary.update(
        drops=int(dropped.sum()),
        closes=int((bad_close & ~dropped).sum()),
        adjustments=int((adj_changed & ~dropped).sum()),
    )
    if not (dropped.any() or replace.any()):
        return TickerCheck(empty_fixes(), summary)
    rows = e.loc[dropped | replace, ["date", *PRICE_COLUMNS]].copy()
    sel = np.flatnonzero(dropped | replace)
    rows["orig_close"] = rows["close"]
    rows["orig_adjusted_close"] = rows["adjusted_close"]
    is_drop = dropped[sel]
    is_close = bad_close[sel] & ~is_drop
    take_bars = bars.reindex(rows["date"])
    take_volume = a_adj["volume"].reindex(rows["date"]).to_numpy(float)
    for c in ("open", "high", "low", "close"):
        rows[c] = np.where(
            is_close, take_bars[c].to_numpy(float), rows[c].to_numpy(float)
        )
    rows["volume"] = np.where(is_close, take_volume, rows["volume"].to_numpy(float))
    rows["adjusted_close"] = new_adj[sel]
    for c in PRICE_COLUMNS:
        rows.loc[is_drop, c] = np.nan
    reasons = []
    for idx, row_drop in zip(sel, is_drop):
        if row_drop:
            reasons.append(reason[idx])
            continue
        parts = []
        if bad_close[idx]:
            parts.append(CLOSE)
        if adj_changed[idx]:
            parts.append(ADJUSTMENT)
        reasons.append(",".join(parts))
    rows["action"] = np.where(is_drop, DROP, REPLACE)
    rows["reason"] = reasons
    rows["lane"] = lane
    rows["ticker"] = ticker
    rows["source"] = SOURCE
    rows["checked_at"] = checked_at
    return TickerCheck(rows.loc[:, list(FIX_COLUMNS)].reset_index(drop=True), summary)


# --------------------------------------------------------------------------- #
# store: selection, loading, writing
# --------------------------------------------------------------------------- #
def lane_path(root: Path, lane: str, file: str = PRICES_FILE) -> Path:
    return Path(root) / lane / file


def _lanes_present(root: Path) -> list[str]:
    return [lane for lane in US_LANES if lane_path(root, lane).exists()]


def select_pairs(
    con: Any,
    root: Path,
    *,
    mode: str,
    tickers: Sequence[str] = (),
    since: dt.date | None = None,
    rules: Rules = RULES,
) -> list[tuple[str, str]]:
    """(lane, ticker) pairs to check.

    ``all``: every pair with a bar from ``rules.history_start``; ``tickers``:
    the named tickers in every lane that has them; ``recent``: pairs with a
    suspicious bar from ``since`` (an adjusted move beyond 1.5x either way,
    or a bar without volume whose close moved) plus pairs with a stale fix.
    """
    pairs: set[tuple[str, str]] = set()
    for lane in _lanes_present(root):
        src = f"read_parquet('{lane_path(root, lane).as_posix()}')"
        if mode == "all":
            rows = con.execute(
                f"SELECT DISTINCT ticker FROM {src} WHERE TRY_CAST(date AS DATE) >= $start",
                {"start": rules.history_start},
            ).fetchall()
        elif mode == "tickers":
            rows = con.execute(
                f"SELECT DISTINCT ticker FROM {src} WHERE ticker = ANY($t)",
                {"t": list(tickers)},
            ).fetchall()
        elif mode == "recent":
            if since is None:
                raise ValueError("recent mode needs since")
            lookback = since - dt.timedelta(days=14)
            rows = con.execute(
                f"""
                WITH p AS (
                  SELECT ticker, TRY_CAST(date AS DATE) AS d, close, adjusted_close AS a,
                         coalesce(volume, 0) AS v
                  FROM {src} WHERE TRY_CAST(date AS DATE) >= $lookback
                ), r AS (
                  SELECT *, lag(a) OVER w AS pa, lag(close) OVER w AS pc
                  FROM p WINDOW w AS (PARTITION BY ticker ORDER BY d)
                )
                SELECT DISTINCT ticker FROM r WHERE d >= $since AND (
                  (a > 0 AND pa > 0 AND abs(ln(a / pa)) > ln(1.5))
                  OR (v <= 0 AND close > 0 AND pc > 0 AND abs(ln(close / pc)) > $tol)
                )""",
                {"lookback": lookback, "since": since, "tol": rules.close_tolerance},
            ).fetchall()
            rows += stale_tickers(con, root, lane)
        else:
            raise ValueError(f"unknown mode: {mode}")
        pairs.update((lane, str(r[0])) for r in rows)
    return sorted(pairs, key=lambda p: (p[1], p[0]))


def stale_tickers(con: Any, root: Path, lane: str) -> list[tuple[str]]:
    """Tickers of ``lane`` with a fix whose EODHD row has changed or gone."""
    path = fixes_path(root)
    if not path.exists() or not lane_path(root, lane).exists():
        return []
    return con.execute(
        f"""
        SELECT DISTINCT f.ticker FROM read_parquet('{path.as_posix()}') f
        LEFT JOIN read_parquet('{lane_path(root, lane).as_posix()}') p
          ON p.ticker = f.ticker AND TRY_CAST(p.date AS DATE) = f.date
         AND p.close IS NOT DISTINCT FROM f.orig_close
         AND p.adjusted_close IS NOT DISTINCT FROM f.orig_adjusted_close
        WHERE f.lane = $lane AND p.ticker IS NULL""",
        {"lane": lane},
    ).fetchall()


def load_eod(
    con: Any, root: Path, lanes: Iterable[str], tickers: Sequence[str]
) -> dict[tuple[str, str], pd.DataFrame]:
    """Each (lane, ticker)'s whole EODHD history, as delivered."""
    out: dict[tuple[str, str], pd.DataFrame] = {}
    for lane in lanes:
        path = lane_path(root, lane)
        if not path.exists():
            continue
        frame = con.execute(
            f"SELECT ticker, TRY_CAST(date AS DATE) AS date, "
            f"{', '.join(PRICE_COLUMNS)} FROM read_parquet('{path.as_posix()}') "
            "WHERE ticker = ANY($t) ORDER BY ticker, date",
            {"t": list(tickers)},
        ).df()
        for ticker, group in frame.groupby("ticker", sort=False):
            out[(lane, str(ticker))] = group.drop(columns="ticker").reset_index(
                drop=True
            )
    return out


def load_splits(
    con: Any, root: Path, tickers: Sequence[str]
) -> dict[str, list[tuple[pd.Timestamp, float]]]:
    out: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for lane in US_LANES:
        path = lane_path(root, lane, SPLITS_FILE)
        if not path.exists():
            continue
        rows = con.execute(
            f"SELECT ticker, TRY_CAST(ex_date AS DATE), numerator, denominator "
            f"FROM read_parquet('{path.as_posix()}') WHERE ticker = ANY($t)",
            {"t": list(tickers)},
        ).fetchall()
        for ticker, ex_date, num, den in rows:
            if ex_date is None or not num or not den:
                continue
            out.setdefault(str(ticker), []).append(
                (pd.Timestamp(ex_date), float(num) / float(den))
            )
    return out


def _sort_fixes(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["lane", "ticker", "date"], ignore_index=True)


CHECK_DATES: tuple[str, ...] = ("alpaca_first", "alpaca_last", "eodhd_last", "asof")


def _with_stamps(frame: Any, columns: Sequence[str]) -> Any:
    """``frame`` with its date columns as timestamps (stored files hold dates)."""
    if frame is None:
        return None
    frame = frame.copy()
    for c in columns:
        if c in frame.columns:
            frame[c] = pd.to_datetime(frame[c])
    return frame


def merge_results(
    root: Path, checks: pd.DataFrame, fixes: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The stored checks and fixes with every re-checked (lane, ticker) replaced."""
    keys = set(zip(checks["lane"], checks["ticker"]))
    old_checks = _with_stamps(
        pd.read_parquet(checks_path(root)) if checks_path(root).exists() else None,
        CHECK_DATES,
    )
    old_fixes = _with_stamps(
        pd.read_parquet(fixes_path(root)) if fixes_path(root).exists() else None,
        ("date",),
    )
    checks = _with_stamps(checks, CHECK_DATES)
    fixes = _with_stamps(fixes, ("date",))

    def _keep(frame: pd.DataFrame | None) -> pd.DataFrame | None:
        if frame is None or frame.empty:
            return None
        mask = [
            (lane, t) not in keys for lane, t in zip(frame["lane"], frame["ticker"])
        ]
        return frame.loc[mask]

    parts_c = [p for p in (_keep(old_checks), checks) if p is not None and not p.empty]
    parts_f = [p for p in (_keep(old_fixes), fixes) if p is not None and not p.empty]
    all_checks = (
        pd.concat(parts_c, ignore_index=True)
        if parts_c
        else pd.DataFrame(columns=list(CHECK_COLUMNS))
    )
    all_fixes = pd.concat(parts_f, ignore_index=True) if parts_f else empty_fixes()
    all_fixes = all_fixes.drop_duplicates(["lane", "ticker", "date"], keep="last")
    return (
        all_checks.sort_values(["lane", "ticker"], ignore_index=True),
        _sort_fixes(all_fixes),
    )


def _dates(values: pd.Series) -> pd.Series:
    stamps = pd.to_datetime(values)
    return pd.Series(
        [x.date() if pd.notna(x) else None for x in stamps],
        index=values.index,
        dtype="object",
    )


def write_results(root: Path, checks: pd.DataFrame, fixes: pd.DataFrame) -> None:
    import _atomic  # type: ignore[import-not-found]

    checks_path(root).parent.mkdir(parents=True, exist_ok=True)
    fixes = fixes.copy()
    fixes["date"] = _dates(fixes["date"])
    checks = checks.copy()
    for c in ("alpaca_first", "alpaca_last", "eodhd_last", "asof"):
        checks[c] = _dates(checks[c])
    _atomic.to_parquet(fixes.loc[:, list(FIX_COLUMNS)], fixes_path(root), index=False)
    _atomic.to_parquet(
        checks.loc[:, list(CHECK_COLUMNS)], checks_path(root), index=False
    )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
@dataclass
class FetchUnit:
    """Tickers asked of Alpaca in one request pair (same ``asof`` and end)."""

    tickers: list[str]
    asof: dt.date | None
    end: dt.date


@dataclass
class Report:
    pairs: int = 0
    checks: list[dict[str, Any]] = field(default_factory=list)
    fixes: list[pd.DataFrame] = field(default_factory=list)
    requests: int = 0
    stopped: str | None = None
    failed: dict[str, str] = field(default_factory=dict)

    def fixes_frame(self) -> pd.DataFrame:
        parts = [f for f in self.fixes if not f.empty]
        return (
            _sort_fixes(pd.concat(parts, ignore_index=True)) if parts else empty_fixes()
        )

    def checks_frame(self) -> pd.DataFrame:
        if not self.checks:
            return pd.DataFrame(columns=list(CHECK_COLUMNS))
        return pd.DataFrame(self.checks).loc[:, list(CHECK_COLUMNS)]


def _unchecked(
    lane: str, ticker: str, symbol: str, status: str, checked_at: str
) -> dict[str, Any]:
    row: dict[str, Any] = {c: None for c in CHECK_COLUMNS}
    row.update(
        lane=lane,
        ticker=ticker,
        symbol=symbol,
        status=status,
        checked_at=checked_at,
        common_days=0,
        drops=0,
        closes=0,
        adjustments=0,
        disputed=0,
        rejected=0,
        max_adjustment=0.0,
    )
    return row


def plan_units(
    spans: Mapping[str, tuple[dt.date, dt.date]],
    *,
    today: dt.date,
    retired_after_days: int,
    per_request: int,
) -> list[FetchUnit]:
    """Group tickers into requests: current ones by plain symbol, retired ones by ``asof``.

    ``spans`` maps a ticker to (last bar, last traded bar). A ticker without a
    bar for ``retired_after_days`` is retired: Alpaca maps a retired symbol
    only ``asof`` a day it was in use, its last traded day here.
    """
    current: list[str] = []
    retired: dict[dt.date, list[tuple[str, dt.date]]] = {}
    cutoff = today - dt.timedelta(days=retired_after_days)
    for ticker, (last, last_traded) in sorted(spans.items()):
        if last >= cutoff:
            current.append(ticker)
        else:
            retired.setdefault(last_traded or last, []).append((ticker, last))
    units = [
        FetchUnit(current[i : i + per_request], None, today)
        for i in range(0, len(current), per_request)
    ]
    for asof, named in sorted(retired.items()):
        for i in range(0, len(named), per_request):
            part = named[i : i + per_request]
            # asked as of the last traded day, through the last bar (placeholders included)
            end = max(last for _t, last in part)
            units.append(FetchUnit([t for t, _l in part], asof, end))
    return units


class _Stopped(Exception):
    pass


def run(
    root: Path,
    pairs: Sequence[tuple[str, str]],
    *,
    fetch: Callable[
        [Sequence[str], dt.date, dt.date, dt.date | None, str],
        dict[str, list[dict[str, Any]]],
    ],
    symbol_of: Callable[[str], str],
    rules: Rules = RULES,
    today: dt.date,
    retired_after_days: int = 10,
    per_request: int = 100,
    chunk: int = 400,
    quiet: Callable[[], str | None] = lambda: None,
    progress: Callable[[str], None] = lambda _m: None,
    checked_at: str = "",
    fatal: tuple[type[BaseException], ...] = (),
) -> Report:
    """Check ``pairs``: load their EODHD rows, ask Alpaca, apply :func:`check_ticker`.

    ``fetch(symbols, start, end, asof, adjustment)`` returns Alpaca bar records
    by symbol; ``quiet()`` names an active quiet window (the run stops before
    the next request and keeps what it has checked). A failed request leaves
    its tickers unchecked (``Report.failed``) unless its error is ``fatal``.
    """
    import duckdb

    report = Report(pairs=len(pairs))
    by_ticker: dict[str, list[str]] = {}
    for lane, ticker in pairs:
        by_ticker.setdefault(ticker, []).append(lane)
    names = sorted(by_ticker)
    con = duckdb.connect()
    for start in range(0, len(names), chunk):
        batch = names[start : start + chunk]
        lanes = sorted({lane for t in batch for lane in by_ticker[t]})
        eod = load_eod(con, root, lanes, batch)
        splits = load_splits(con, root, batch)
        spans: dict[str, tuple[dt.date, dt.date]] = {}
        for t in batch:
            frames = [eod[(lane, t)] for lane in by_ticker[t] if (lane, t) in eod]
            if not frames:
                continue
            both = pd.concat(frames)
            last = pd.Timestamp(both["date"].max()).date()
            traded = both.loc[both["volume"].fillna(0) > 0, "date"]
            last_traded = pd.Timestamp(traded.max()).date() if len(traded) else last
            if last < rules.history_start:
                continue
            if not VALID_SYMBOL.fullmatch(symbol_of(t)):
                for lane in by_ticker[t]:
                    report.checks.append(
                        _unchecked(lane, t, symbol_of(t), UNSUPPORTED, checked_at)
                    )
                continue
            spans[t] = (last, last_traded)
        units = plan_units(
            spans,
            today=today,
            retired_after_days=retired_after_days,
            per_request=per_request,
        )
        try:
            for unit in units:
                symbols = {t: symbol_of(t) for t in unit.tickers}
                bars: dict[str, dict[str, list[dict[str, Any]]]] = {}
                for adjustment in ("raw", "all"):
                    wanted = (
                        list(symbols.values())
                        if adjustment == "raw"
                        else [s for s in symbols.values() if bars["raw"].get(s)]
                    )
                    if not wanted:
                        bars[adjustment] = {}
                        continue
                    window = quiet()
                    if window:
                        raise _Stopped(window)
                    try:
                        bars[adjustment] = fetch(
                            wanted, rules.history_start, unit.end, unit.asof, adjustment
                        )
                    except fatal:
                        raise
                    except Exception as exc:  # one request's failure is not the run's
                        bars = {}
                        for t in unit.tickers:
                            report.failed[t] = f"{type(exc).__name__}: {exc}"[:200]
                        break
                    finally:
                        report.requests += 1
                if not bars or "all" not in bars:
                    continue
                for t in unit.tickers:
                    raw = bars_frame(bars["raw"].get(symbols[t], []))
                    adj = bars_frame(bars["all"].get(symbols[t], []))
                    for lane in by_ticker[t]:
                        frame = eod.get((lane, t))
                        if frame is None:
                            continue
                        try:
                            result = check_ticker(
                                frame,
                                raw,
                                adj,
                                lane=lane,
                                ticker=t,
                                rules=rules,
                                splits=splits.get(t, ()),
                                checked_at=checked_at,
                            )
                        except (
                            Exception
                        ) as exc:  # one pair's data must not end the sweep
                            report.failed[f"{lane}:{t}"] = (
                                f"check: {type(exc).__name__}: {exc}"[:200]
                            )
                            continue
                        result.summary.update(symbol=symbols[t], asof=unit.asof)
                        report.checks.append(result.summary)
                        if not result.fixes.empty:
                            report.fixes.append(result.fixes)
        except _Stopped as stop:
            report.stopped = f"quiet window {stop}"
            return report
        fixed = sum(len(f) for f in report.fixes)
        progress(
            f"{min(start + chunk, len(names)):,}/{len(names):,} tickers  "
            f"{report.requests:,} requests  {fixed:,} fix rows"
        )
    return report


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _alpaca_fetcher(
    settings: Any,
) -> tuple[Callable[..., Any], Callable[[], str | None], Any]:
    import alpaca  # noqa: F401  (paths)
    from alpaca.auth import Credentials
    from alpaca.client import BarsClient, RateLimiter
    from alpaca.quiet import QuietSchedule

    client = BarsClient(
        Credentials.from_env(),
        data_url=settings.data_url,
        page_limit=settings.page_limit,
        max_pages=settings.max_pages,
        timeout=settings.timeout_seconds,
        limiter=RateLimiter(settings.max_requests_per_minute),
    )
    schedule = QuietSchedule(settings.quiet_windows, settings.quiet_timezone)

    def fetch(symbols, start, end, asof, adjustment):  # type: ignore[no-untyped-def]
        records, _pages = client.bars(
            symbols, start, end, adjustment=adjustment, asof=asof
        )
        return records

    def quiet() -> str | None:
        window = schedule.active(dt.datetime.now(dt.timezone.utc))
        return f"{window} {schedule.timezone}" if window else None

    return fetch, quiet, client


def summarize(checks: pd.DataFrame, fixes: pd.DataFrame) -> list[str]:
    lines = []
    if not checks.empty:
        by_status = checks["status"].value_counts()
        lines.append(
            "checked pairs: " + ", ".join(f"{k} {v:,}" for k, v in by_status.items())
        )
        lines.append(
            f"disputed days (EODHD kept): {int(checks['disputed'].fillna(0).sum()):,}"
        )
        if "rejected" in checks.columns:
            lines.append(
                "fixes rejected by the do-no-harm check: "
                f"{int(checks['rejected'].fillna(0).sum()):,}"
            )
    if fixes.empty:
        lines.append("fixes: none")
        return lines
    reasons = fixes["reason"].str.split(",").explode().value_counts()
    lines.append(
        f"fix rows: {len(fixes):,} over {fixes[['lane', 'ticker']].drop_duplicates().shape[0]:,} pairs ("
        + ", ".join(f"{k} {v:,}" for k, v in reasons.items())
        + ")"
    )
    per_lane = fixes.groupby("lane").size()
    lines.append("by lane: " + ", ".join(f"{k} {v:,}" for k, v in per_lane.items()))
    return lines


def status_lines(root: Path) -> list[str]:
    import duckdb

    if not fixes_path(root).exists():
        return [f"no fixes under {root / FIXES_DIR}"]
    checks = (
        pd.read_parquet(checks_path(root))
        if checks_path(root).exists()
        else pd.DataFrame(columns=list(CHECK_COLUMNS))
    )
    fixes = pd.read_parquet(fixes_path(root))
    lines = [f"fixes: {fixes_path(root)}"]
    if len(checks):
        lines.append(f"last check: {checks['checked_at'].max()}")
    lines += summarize(checks, fixes)
    con = duckdb.connect()
    stale = sum(len(stale_tickers(con, root, lane)) for lane in US_LANES)
    lines.append(
        f"pairs with a stale fix (EODHD row changed; re-checked by the next run): {stale:,}"
    )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="eodhd fix-prices",
        description=(
            "Check the US daily prices against Alpaca's SIP daily bars and keep the bars "
            "it shows are wrong in an overlay (<eodhd root>/_fixes). EODHD's files are "
            "never changed. Dry run unless --run."
        ),
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--all", action="store_true", help="every US pair with bars from 2016"
    )
    scope.add_argument(
        "--tickers", help="comma-separated tickers (every US lane that has them)"
    )
    scope.add_argument(
        "--days",
        type=int,
        default=10,
        help="pairs with a suspicious bar in the last N days, plus stale fixes (default 10)",
    )
    scope.add_argument(
        "--status", action="store_true", help="summarize the stored fixes (offline)"
    )
    parser.add_argument("--run", action="store_true", help="write the overlay")
    parser.add_argument("--limit", type=int, help="check at most N tickers (a trial)")
    parser.add_argument(
        "--out", help="dry run: write the proposed checks and fixes here"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    import duckdb

    import alpaca  # noqa: F401  (paths)
    from _datadir import EODHD_RAW_ROOT  # type: ignore[import-not-found]
    from alpaca import config as alpaca_config
    from alpaca.universe import alpaca_symbol

    root = Path(EODHD_RAW_ROOT)
    if args.status:
        print("\n".join(status_lines(root)))
        return 0
    if args.days is not None and args.days <= 0:
        parser.error("--days must be positive")

    settings = alpaca_config.load_settings()
    rules = Rules(history_start=settings.history_start)
    today = dt.datetime.now(dt.timezone.utc).date()
    con = duckdb.connect()
    if args.all:
        pairs = select_pairs(con, root, mode="all", rules=rules)
    elif args.tickers:
        names = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        pairs = select_pairs(con, root, mode="tickers", tickers=names, rules=rules)
    else:
        pairs = select_pairs(
            con,
            root,
            mode="recent",
            since=today - dt.timedelta(days=args.days),
            rules=rules,
        )
    if args.limit:
        keep = set(sorted({t for _l, t in pairs})[: args.limit])
        pairs = [p for p in pairs if p[1] in keep]
    tickers = len({t for _l, t in pairs})
    print(
        f"{'RUN' if args.run else 'DRY RUN'}: {len(pairs):,} pairs, {tickers:,} tickers -> Alpaca {settings.data_url} (sip)"
    )
    if not pairs:
        print("nothing to check")
        return 0

    from alpaca.errors import AuthError

    fetch, quiet, client = _alpaca_fetcher(settings)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = time.monotonic()
    report = run(
        root,
        pairs,
        fetch=fetch,
        symbol_of=lambda t: alpaca_symbol(t, settings.symbol_map),
        rules=rules,
        today=today,
        retired_after_days=settings.retired_after_days,
        per_request=settings.symbols_per_request,
        quiet=quiet,
        progress=lambda m: print("  " + m, flush=True),
        checked_at=stamp,
        fatal=(AuthError,),
    )
    checks, fixes = report.checks_frame(), report.fixes_frame()
    print(
        f"{client.requests_sent:,} Alpaca requests in {time.monotonic() - started:,.0f}s"
    )
    if report.stopped:
        print(f"stopped early: {report.stopped}; {len(checks):,} pairs checked")
    if report.failed:
        sample = ", ".join(sorted(report.failed)[:8])
        print(
            f"{len(report.failed):,} tickers left unchecked by failed requests (e.g. {sample}): "
            f"{next(iter(report.failed.values()))}"
        )
    print("\n".join(summarize(checks, fixes)))
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        checks.to_parquet(out / CHECKS_FILE, index=False)
        fixes.to_parquet(out / FIXES_FILE, index=False)
        print(f"proposed checks and fixes -> {out}")
    if not args.run:
        print("dry run: nothing written (re-run with --run)")
        return 0
    if checks.empty:
        return 0
    all_checks, all_fixes = merge_results(root, checks, fixes)
    write_results(root, all_checks, all_fixes)
    print(
        f"wrote {fixes_path(root)}: {len(all_fixes):,} fix rows over {all_checks.shape[0]:,} checked pairs"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
