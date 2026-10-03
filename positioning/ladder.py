"""FIFO lot ladder over an aggregate quantity series (DD-001 section 3).

The kernel knows nothing about sources, sides beyond a sign, or pandas. It
takes observations of one security and one side in date order and returns one
row per observation. Every row depends only on the observations up to it, so
appending data never changes earlier rows.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Iterable

LONG = 1
SHORT = -1
DEFAULT_MAX_GAP = 2
_EPS = 1e-9


@dataclass(frozen=True)
class Observation:
    """One reported quantity on the split-neutral basis.

    ``lot_price`` prices the flow of this observation (opened and closed
    lots); ``mark_price`` values what is left. ``missed_before`` is the number
    of scheduled observations absent since the previous one.
    """

    obs_date: date
    quantity: float
    lot_price: float
    mark_price: float
    missed_before: int = 0


@dataclass(frozen=True)
class LadderRow:
    obs_date: date
    inventory: float
    flow: float
    n_lots: int
    wavg_age_days: float | None
    cost_basis: float | None
    profit_pct: float | None
    unrealised: float
    realised: float
    seed_share: float | None
    gap: bool
    reset: bool


@dataclass
class _Lot:
    lot_date: date
    price: float
    remaining: float
    seed: bool


def run_ladder(
    observations: Iterable[Observation],
    *,
    side: int,
    max_gap: int = DEFAULT_MAX_GAP,
) -> list[LadderRow]:
    """Run the ladder; ``side`` is ``LONG`` or ``SHORT``.

    A first observation, or one after more than ``max_gap`` missed scheduled
    observations, starts from a single seed lot whose true age is unknown.
    """
    if side not in (LONG, SHORT):
        raise ValueError(f"side must be {LONG} or {SHORT}, got {side}")
    lots: deque[_Lot] = deque()
    rows: list[LadderRow] = []
    previous: Observation | None = None
    for obs in observations:
        _validate(obs, previous)
        reset = previous is None or obs.missed_before > max_gap
        realised = 0.0
        if reset:
            lots.clear()
            flow = 0.0
            if obs.quantity > _EPS:
                lots.append(_Lot(obs.obs_date, obs.lot_price, obs.quantity, True))
        else:
            assert previous is not None
            flow = obs.quantity - previous.quantity
            if flow > _EPS:
                lots.append(_Lot(obs.obs_date, obs.lot_price, flow, False))
            elif flow < -_EPS:
                realised = _close(lots, -flow, obs.lot_price, side)
        rows.append(_row(obs, lots, flow, realised, side, reset))
        previous = obs
    return rows


def _validate(obs: Observation, previous: Observation | None) -> None:
    if obs.quantity < 0:
        raise ValueError(f"{obs.obs_date}: negative quantity {obs.quantity}")
    if not obs.lot_price > 0 or not obs.mark_price > 0:
        raise ValueError(f"{obs.obs_date}: prices must be positive")
    if obs.missed_before < 0:
        raise ValueError(f"{obs.obs_date}: missed_before must not be negative")
    if previous is not None and obs.obs_date <= previous.obs_date:
        raise ValueError(
            f"observations must be strictly increasing in date: "
            f"{previous.obs_date} then {obs.obs_date}"
        )


def _close(lots: deque[_Lot], amount: float, exit_price: float, side: int) -> float:
    """Pop ``amount`` from the oldest lots; return the profit realised."""
    realised = 0.0
    while amount > _EPS and lots:
        lot = lots[0]
        closed = min(lot.remaining, amount)
        realised += closed * side * (exit_price - lot.price)
        lot.remaining -= closed
        amount -= closed
        if lot.remaining <= _EPS:
            lots.popleft()
    return realised


def _row(
    obs: Observation,
    lots: deque[_Lot],
    flow: float,
    realised: float,
    side: int,
    reset: bool,
) -> LadderRow:
    inventory = sum(lot.remaining for lot in lots)
    gap = obs.missed_before > 0
    if inventory <= _EPS:
        return LadderRow(
            obs.obs_date, 0.0, flow, 0, None, None, None, 0.0, realised, None, gap, reset
        )
    age = sum(lot.remaining * (obs.obs_date - lot.lot_date).days for lot in lots) / inventory
    cost = sum(lot.remaining * lot.price for lot in lots) / inventory
    seed = sum(lot.remaining for lot in lots if lot.seed) / inventory
    return LadderRow(
        obs_date=obs.obs_date,
        inventory=inventory,
        flow=flow,
        n_lots=len(lots),
        wavg_age_days=age,
        cost_basis=cost,
        profit_pct=side * (obs.mark_price - cost) / cost,
        unrealised=inventory * side * (obs.mark_price - cost),
        realised=realised,
        seed_share=seed,
        gap=gap,
        reset=reset,
    )
