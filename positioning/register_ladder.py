"""The per-fund short ladder on the public registers (DD-003 WP18).

A register row is a holder's net short position in an issuer, in percent of
the share capital, on ``position_date``, visible to the public from
``published_from``. Per ``(market, holder, isin)`` the rows form a step
series; the DD-001 kernel runs on it with **percent of share capital as the
quantity unit** (the regulator's percentage is split-neutral by construction)
and the local close on the position date as the lot price, giving each
fund's position its FIFO age, cost and profit (SPEC's G6 and G7 with real
identity) and its flow (G4 with identity).

Rules fixed here:

- exact duplicate rows are dropped; of several rows for one holder, ISIN and
  position date the last in the file stands;
- a row below the publication threshold (``net_short_pct < 0.5``) is the
  last visible level of the position (the regulator discloses the crossing
  down once): the ladder takes it as a partial close and the issuer panel
  stops counting the holder from that row on;
- visibility is monotone: a row is visible from the latest ``published_from``
  of every row of the pair up to it in position-date order, so a late
  correction cannot leak backwards (``visible_from``);
- the first visible row is a seed lot: how long the fund was short below the
  threshold is unknown, and ``seed_share`` carries that forward;
- rows without a close on or before the position date are skipped (the
  issuer has no price history there); a pair with no priced row is left out.

The issuer panel (``issuer_panel``) is daily and point in time: on day ``d``
the holders are those whose latest visible row (``visible_from <= d``) is at
or above the threshold; ``level`` is their summed percent, ``age_days`` the
percent-weighted FIFO age aged to ``d``, ``profit_pct`` the percent-weighted
short profit marked at ``d``'s close, ``flow`` the summed percent change of
the rows that became visible on ``d``, ``entries`` and ``exits`` the holders
crossing the threshold on ``d``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Mapping

import numpy as np
import pandas as pd

from positioning.ladder import SHORT, Observation, run_ladder

THRESHOLD_PCT = 0.5
#: A publication more than this many days after the position date is late (flagged, not dropped).
LATE_DAYS = 5
_NO_GAP = 10**9

FUND_COLUMNS: tuple[str, ...] = (
    "market", "holder", "isin", "position_date", "published_from", "visible_from", "lag_days", "late",
    "pct", "below_threshold", "flow", "inventory", "n_lots", "wavg_age_days", "cost_basis", "profit_pct",
    "seed_share", "lot_price", "realised", "reset",
)
PANEL_COLUMNS: tuple[str, ...] = (
    "market", "isin", "date", "n_holders", "level", "flow", "entries", "exits", "age_days", "profit_pct", "close",
)


def prepare_rows(register: pd.DataFrame) -> pd.DataFrame:
    """Deduplicate, order and date the visibility of the register's rows."""
    frame = register.copy()
    frame = frame.drop_duplicates()
    frame["position_date"] = pd.to_datetime(frame["position_date"])
    frame["published_from"] = pd.to_datetime(frame["published_from"])
    frame = frame.drop_duplicates(["market", "holder", "isin", "position_date"], keep="last")
    frame = frame.sort_values(["market", "holder", "isin", "position_date"], kind="stable").reset_index(drop=True)
    frame["visible_from"] = frame.groupby(["market", "holder", "isin"], sort=False)["published_from"].cummax()
    frame["lag_days"] = (frame["published_from"] - frame["position_date"]).dt.days
    frame["late"] = frame["lag_days"] > LATE_DAYS
    frame["below_threshold"] = frame["net_short_pct"] < THRESHOLD_PCT
    return frame


def _asof_close(closes: pd.Series, day: pd.Timestamp) -> float | None:
    idx = closes.index.searchsorted(day, side="right") - 1
    if idx < 0:
        return None
    value = float(closes.iloc[idx])
    return value if value > 0 else None


def fund_ladder(pair: pd.DataFrame, closes: pd.Series) -> list[dict[str, Any]]:
    """The kernel over one holder's rows in one ISIN; ``closes`` is a date-indexed, sorted series."""
    observations: list[Observation] = []
    kept: list[pd.Series] = []
    for _, row in pair.iterrows():
        price = _asof_close(closes, row["position_date"])
        if price is None:
            continue
        observations.append(Observation(row["position_date"].date(), float(row["net_short_pct"]), price, price))
        kept.append(row)
    if not observations:
        return []
    ladder = run_ladder(observations, side=SHORT, max_gap=_NO_GAP)
    out: list[dict[str, Any]] = []
    for row, obs, lad in zip(kept, observations, ladder):
        out.append(
            {
                "market": row["market"], "holder": row["holder"], "isin": row["isin"],
                "position_date": row["position_date"], "published_from": row["published_from"],
                "visible_from": row["visible_from"], "lag_days": int(row["lag_days"]), "late": bool(row["late"]),
                "pct": float(row["net_short_pct"]), "below_threshold": bool(row["below_threshold"]),
                "flow": lad.flow, "inventory": lad.inventory, "n_lots": lad.n_lots, "wavg_age_days": lad.wavg_age_days,
                "cost_basis": lad.cost_basis, "profit_pct": lad.profit_pct, "seed_share": lad.seed_share,
                "lot_price": obs.lot_price, "realised": lad.realised, "reset": lad.reset,
            }
        )
    return out


def fund_ladders(rows: pd.DataFrame, closes_by_isin: Mapping[str, pd.Series]) -> pd.DataFrame:
    """Every pair's ladder; pairs whose ISIN has no close series are left out."""
    out: list[dict[str, Any]] = []
    for (_, _, isin), pair in rows.groupby(["market", "holder", "isin"], sort=False):
        closes = closes_by_isin.get(isin)
        if closes is None or closes.empty:
            continue
        out.extend(fund_ladder(pair, closes))
    return pd.DataFrame(out, columns=list(FUND_COLUMNS))


def _issuer_events(fund_rows: pd.DataFrame) -> pd.DataFrame:
    """The issuer's state after each visible row, in visibility order (one row per visibility date)."""
    rows = fund_rows.sort_values(["visible_from", "position_date"], kind="stable")
    state: dict[str, dict[str, float]] = {}
    events: list[dict[str, Any]] = []
    for day, group in rows.groupby("visible_from", sort=True):
        flow = entries = exits = 0.0
        for _, r in group.iterrows():
            was = state.get(r["holder"])
            visible = not r["below_threshold"] and r["inventory"] > 0
            if visible:
                age_offset = float(r["wavg_age_days"]) - r["position_date"].toordinal()
                state[r["holder"]] = {"pct": r["inventory"], "age_offset": age_offset, "inv_cost": 1.0 / float(r["cost_basis"])}
                if was is None:
                    entries += 1
            elif was is not None:
                del state[r["holder"]]
                exits += 1
            flow += float(r["flow"]) if not r["reset"] else float(r["inventory"])
        total = sum(s["pct"] for s in state.values())
        events.append(
            {
                "date": day, "n_holders": len(state), "level": total, "flow": flow, "entries": entries, "exits": exits,
                "age_offset": (sum(s["pct"] * s["age_offset"] for s in state.values()) / total) if total > 0 else np.nan,
                "inv_cost": (sum(s["pct"] * s["inv_cost"] for s in state.values()) / total) if total > 0 else np.nan,
            }
        )
    return pd.DataFrame(events)


def issuer_panel(fund_rows: pd.DataFrame, dates: pd.DatetimeIndex, closes_by_isin: Mapping[str, pd.Series]) -> pd.DataFrame:
    """The daily point-in-time issuer panel over ``dates`` (the market's trading days)."""
    dates = pd.DatetimeIndex(dates).sort_values()
    out: list[pd.DataFrame] = []
    for (market, isin), group in fund_rows.groupby(["market", "isin"], sort=False):
        events = _issuer_events(group)
        if events.empty:
            continue
        first = events["date"].min()
        index = dates[dates >= first]
        if index.empty:
            continue
        by_day = events.set_index("date")
        # events on non-trading days roll to the next trading day
        positions = index.searchsorted(by_day.index.to_numpy(), side="left")
        keep = positions < len(index)
        by_day = by_day[keep]
        by_day.index = index[positions[keep]]
        flows = by_day[["flow", "entries", "exits"]].groupby(level=0).sum().reindex(index).fillna(0.0)
        state = by_day[["n_holders", "level", "age_offset", "inv_cost"]].groupby(level=0).last().reindex(index).ffill()
        closes = closes_by_isin.get(isin)
        close = closes.reindex(index).ffill() if closes is not None else pd.Series(np.nan, index=index)
        ordinal = pd.Series([d.toordinal() for d in index], index=index, dtype=float)
        panel = pd.DataFrame(
            {
                "market": market, "isin": isin, "date": index,
                "n_holders": state["n_holders"].fillna(0).astype(int).to_numpy(),
                "level": state["level"].fillna(0.0).to_numpy(),
                "flow": flows["flow"].to_numpy(), "entries": flows["entries"].astype(int).to_numpy(), "exits": flows["exits"].astype(int).to_numpy(),
                "age_days": (state["age_offset"] + ordinal).where(state["level"] > 0).to_numpy(),
                "profit_pct": (1.0 - close * state["inv_cost"]).where(state["level"] > 0).to_numpy(),
                "close": close.to_numpy(),
            }
        )
        out.append(panel)
    if not out:
        return pd.DataFrame(columns=list(PANEL_COLUMNS))
    return pd.concat(out, ignore_index=True)[list(PANEL_COLUMNS)]
