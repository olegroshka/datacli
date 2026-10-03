"""positioning.ladder and positioning.basis: the DD-001 contract on hand-made and random paths."""

from __future__ import annotations

import random
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning.basis import NO_SPLITS, SplitFactor, interval_mean, last_close  # noqa: E402
from positioning.ladder import LONG, SHORT, Observation, run_ladder  # noqa: E402
from positioning.short_ladder import build_observations  # noqa: E402

D0 = date(2024, 1, 15)


def _obs(i: int, qty: float, lot: float, mark: float | None = None, missed: int = 0) -> Observation:
    return Observation(D0 + timedelta(days=14 * i), qty, lot, lot if mark is None else mark, missed)


def test_hand_computed_long_path() -> None:
    rows = run_ladder(
        [_obs(0, 100, 10.0), _obs(1, 150, 12.0), _obs(2, 80, 15.0, mark=16.0)], side=LONG
    )
    first, second, third = rows
    assert (first.inventory, first.flow, first.n_lots, first.seed_share) == (100, 0.0, 1, 1.0)
    assert first.wavg_age_days == 0 and first.reset
    assert second.n_lots == 2 and second.flow == 50
    assert second.cost_basis == pytest.approx((100 * 10 + 50 * 12) / 150)
    assert second.wavg_age_days == pytest.approx(100 * 14 / 150)
    assert second.seed_share == pytest.approx(100 / 150)
    # sell 70: all from the seed lot (FIFO), realised 70 * (15 - 10)
    assert third.realised == pytest.approx(350.0)
    assert third.n_lots == 2 and third.inventory == pytest.approx(80)
    assert third.cost_basis == pytest.approx((30 * 10 + 50 * 12) / 80)
    assert third.wavg_age_days == pytest.approx((30 * 28 + 50 * 14) / 80)
    assert third.profit_pct == pytest.approx((16 - third.cost_basis) / third.cost_basis)
    assert third.seed_share == pytest.approx(30 / 80)


def test_short_side_profits_when_price_falls() -> None:
    rows = run_ladder([_obs(0, 100, 20.0), _obs(1, 40, 15.0, mark=14.0)], side=SHORT)
    assert rows[1].realised == pytest.approx(60 * (20 - 15))
    assert rows[1].profit_pct == pytest.approx((20 - 14) / 20)
    long_rows = run_ladder([_obs(0, 100, 20.0), _obs(1, 40, 15.0, mark=14.0)], side=LONG)
    assert long_rows[1].realised == pytest.approx(-rows[1].realised)
    assert long_rows[1].profit_pct == pytest.approx(-rows[1].profit_pct)


def test_zero_inventory_masks_instead_of_zero_filling() -> None:
    rows = run_ladder([_obs(0, 50, 10.0), _obs(1, 0, 11.0), _obs(2, 20, 12.0)], side=LONG)
    empty = rows[1]
    assert empty.inventory == 0 and empty.n_lots == 0
    assert empty.wavg_age_days is None and empty.cost_basis is None
    assert empty.profit_pct is None and empty.seed_share is None
    assert empty.realised == pytest.approx(50.0)
    assert rows[2].wavg_age_days == 0 and rows[2].seed_share == 0.0 and not rows[2].reset


def test_gap_carries_forward_and_long_gap_resets() -> None:
    short_gap = run_ladder([_obs(0, 100, 10.0), _obs(3, 120, 11.0, missed=2)], side=LONG)
    assert short_gap[1].gap and not short_gap[1].reset and short_gap[1].n_lots == 2
    long_gap = run_ladder([_obs(0, 100, 10.0), _obs(4, 120, 11.0, missed=3)], side=LONG)
    assert long_gap[1].gap and long_gap[1].reset
    assert long_gap[1].n_lots == 1 and long_gap[1].seed_share == 1.0
    assert long_gap[1].flow == 0.0 and long_gap[1].cost_basis == 11.0


def test_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        run_ladder([_obs(0, 1, 1.0)], side=0)
    with pytest.raises(ValueError):
        run_ladder([_obs(0, -1, 1.0)], side=LONG)
    with pytest.raises(ValueError):
        run_ladder([_obs(0, 1, 0.0)], side=LONG)
    with pytest.raises(ValueError):
        run_ladder([_obs(1, 1, 1.0), _obs(0, 1, 1.0)], side=LONG)


def _random_path(rng: random.Random, n: int) -> list[Observation]:
    qty, price, out = rng.uniform(0, 1000), rng.uniform(5, 50), []
    for i in range(n):
        if i:
            qty = max(0.0, qty + rng.choice([-1, 1]) * rng.uniform(0, 400))
            if rng.random() < 0.1:
                qty = 0.0
            price *= rng.uniform(0.8, 1.25)
        out.append(_obs(i, qty, price, mark=price * rng.uniform(0.95, 1.05)))
    return out


@pytest.mark.parametrize("side", [LONG, SHORT])
def test_accounting_identity_on_random_paths(side: int) -> None:
    rng = random.Random(319)
    for _ in range(200):
        path = _random_path(rng, rng.randint(2, 40))
        rows = run_ladder(path, side=side)
        for row, obs in zip(rows, path):
            assert row.inventory == pytest.approx(obs.quantity, abs=1e-6)
        carried = sum(
            path[i - 1].quantity * (path[i].lot_price - path[i - 1].lot_price)
            for i in range(1, len(path))
        )
        final_mark = path[-1].quantity * (path[-1].mark_price - path[-1].lot_price)
        total = sum(r.realised for r in rows) + rows[-1].unrealised
        assert total == pytest.approx(side * (carried + final_mark), rel=1e-9, abs=1e-6)


def test_prefix_run_equals_prefix_of_full_run() -> None:
    rng = random.Random(7)
    path = _random_path(rng, 30)
    full = run_ladder(path, side=SHORT)
    for cut in (1, 5, 17, 29):
        assert run_ladder(path[:cut], side=SHORT) == full[:cut]


def test_age_grows_one_interval_per_step_when_nothing_trades() -> None:
    rows = run_ladder([_obs(i, 100, 10.0) for i in range(5)], side=LONG)
    assert [r.wavg_age_days for r in rows] == [0, 14, 28, 42, 56]
    assert all(r.n_lots == 1 and r.realised == 0 for r in rows)


# --------------------------------------------------------------------------- #
# split-neutral basis
# --------------------------------------------------------------------------- #
def test_split_factor_is_inclusive_of_the_ex_date() -> None:
    factor = SplitFactor.from_splits([(date(2024, 6, 10), 10.0), (date(2021, 7, 20), 4.0)])
    assert factor.at(date(2021, 7, 19)) == 1.0
    assert factor.at(date(2021, 7, 20)) == 4.0
    assert factor.at(date(2024, 6, 9)) == 4.0
    assert factor.at(date(2024, 6, 10)) == 40.0
    assert NO_SPLITS.at(date(2024, 1, 1)) == 1.0
    # the NVDA figure from KB-003: 315,033,985 shares after both splits, in pre-2021 units
    assert factor.quantity(315_033_985, date(2024, 6, 14)) == pytest.approx(7_875_849.6, rel=1e-6)
    with pytest.raises(ValueError):
        SplitFactor.from_splits([(date(2024, 1, 1), 0.0)])


def test_interval_mean_and_last_close() -> None:
    closes = [(date(2024, 1, d), float(d)) for d in (2, 3, 4, 5, 8)]
    assert interval_mean(closes, date(2024, 1, 3), date(2024, 1, 8)) == pytest.approx((4 + 5 + 8) / 3)
    assert interval_mean(closes, None, date(2024, 1, 6)) == 5.0
    assert interval_mean(closes, date(2024, 1, 8), date(2024, 1, 20)) is None
    assert last_close(closes, date(2024, 1, 1)) is None
    assert last_close(closes, date(2024, 1, 7)) == 5.0


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def test_a_split_produces_no_flow_and_leaves_the_ladder_unchanged() -> None:
    """ADR-001 D7: the same economic path with and without a split gives the same ladder."""
    rng = random.Random(11)
    days = _weekdays(date(2024, 1, 2), 120)
    true_close, price = [], 100.0
    for _ in days:
        price *= rng.uniform(0.97, 1.03)
        true_close.append(price)
    schedule = days[9::10]
    true_qty = [1000.0]
    for _ in schedule[1:]:
        true_qty.append(max(0.0, true_qty[-1] + rng.uniform(-300, 300)))

    ex_date, ratio = days[55], 4.0
    raw_close = [c / ratio if d >= ex_date else c for d, c in zip(days, true_close)]
    raw_qty = [q * ratio if d >= ex_date else q for d, q in zip(schedule, true_qty)]

    plain, _ = build_observations(
        list(zip(schedule, true_qty)), list(zip(days, true_close)), NO_SPLITS, schedule
    )
    split, _ = build_observations(
        list(zip(schedule, raw_qty)),
        list(zip(days, raw_close)),
        SplitFactor.from_splits([(ex_date, ratio)]),
        schedule,
    )
    a, b = run_ladder(plain, side=SHORT), run_ladder(split, side=SHORT)
    assert len(a) == len(b) == len(schedule)
    for x, y in zip(a, b):
        assert y.inventory == pytest.approx(x.inventory)
        assert y.flow == pytest.approx(x.flow, abs=1e-6)
        assert y.n_lots == x.n_lots
        assert y.wavg_age_days == pytest.approx(x.wavg_age_days)
        assert y.profit_pct == pytest.approx(x.profit_pct)
        assert y.realised == pytest.approx(x.realised, abs=1e-6)

    # without the basis the split shows up as a phantom flow of the split ratio
    naive, _ = build_observations(
        list(zip(schedule, raw_qty)), list(zip(days, raw_close)), NO_SPLITS, schedule
    )
    k = next(i for i, d in enumerate(schedule) if d >= ex_date)
    assert run_ladder(naive, side=SHORT)[k].flow > 2 * true_qty[k - 1]


def test_build_observations_counts_missed_and_unpriced_reports() -> None:
    days = _weekdays(date(2024, 1, 2), 60)
    closes = [(d, 10.0) for d in days[20:]]  # no prices for the first 20 days
    schedule = days[9::10]
    reports = [(schedule[0], 5.0), (schedule[2], 6.0), (schedule[5], 7.0)]
    observations, unpriced = build_observations(reports, closes, NO_SPLITS, schedule)
    assert unpriced == 1
    assert [o.obs_date for o in observations] == [schedule[2], schedule[5]]
    assert [o.missed_before for o in observations] == [0, 2]


# --------------------------------------------------------------------------- #
# vendor split hygiene (KB-003: spin-off adjustments and duplicates in `splits`)
# --------------------------------------------------------------------------- #
def test_dedupe_splits_drops_the_same_event_listed_twice() -> None:
    from positioning.basis import dedupe_splits

    events = [
        (date(2024, 11, 13), 1.91),
        (date(2024, 11, 12), 1.91),
        (date(2022, 8, 30), 0.928571),
        (date(2022, 8, 30), 0.928),
        (date(2021, 7, 20), 4.0),
        (date(2024, 6, 10), 10.0),
    ]
    assert dedupe_splits(events) == [
        (date(2021, 7, 20), 4.0),
        (date(2022, 8, 30), 0.928),
        (date(2024, 6, 10), 10.0),
        (date(2024, 11, 12), 1.91),
    ]
    # two different events on one day are both real and compound (ENOV 2022-04-05)
    same_day = [(date(2022, 4, 5), 1 / 3), (date(2022, 4, 5), 0.581)]
    assert dedupe_splits(same_day) == sorted(same_day)
    assert SplitFactor.from_splits(same_day).at(date(2022, 4, 5)) == pytest.approx(0.581 / 3)


def test_confirm_quantity_splits_separates_share_splits_from_price_adjustments() -> None:
    from positioning.short_ladder import Report, confirm_quantity_splits

    schedule = [date(2024, 5, 31), date(2024, 6, 14), date(2024, 6, 28), date(2024, 7, 15)]
    split = [(date(2024, 6, 10), 10.0)]
    # flagged by FINRA: confirmed even though short interest also halved
    flagged = [Report(schedule[0], 100.0), Report(schedule[1], 500.0, True)]
    assert confirm_quantity_splits(split, flagged, schedule) == split
    # FINRA missed the flag, but the count moved by the ratio (PIPR 2026-03, KB-003)
    unflagged = [Report(schedule[0], 100.0), Report(schedule[1], 950.0)]
    assert confirm_quantity_splits(split, unflagged, schedule) == split
    # a spin-off adjustment: material ratio, share count unchanged, no flag (MTCH 2020-07)
    spin = [(date(2024, 6, 10), 3.5)]
    steady = [Report(schedule[0], 100.0), Report(schedule[1], 104.0)]
    assert confirm_quantity_splits(spin, steady, schedule) == []
    # a small unflagged ratio is never applied on fit alone (GE 1.04, 2019-02)
    small = [(date(2024, 6, 10), 1.04)]
    assert confirm_quantity_splits(small, steady, schedule) == []
    assert confirm_quantity_splits(small, [steady[0], Report(schedule[1], 104.0, True)], schedule) == small
    # before the first report, or across a resetting gap: only a rescale, so it counts
    assert confirm_quantity_splits(spin, [Report(schedule[1], 104.0)], schedule) == spin
    gappy = [Report(schedule[0], 100.0), Report(schedule[3], 104.0)]
    assert confirm_quantity_splits(spin, gappy, schedule, max_gap=1) == spin
    # no report on or after the ex date yet
    assert confirm_quantity_splits(spin, [Report(schedule[0], 100.0)], schedule) == []
    # a reverse split and a spin-off adjustment on one day: only the split moved the count
    both = [(date(2024, 6, 10), 1 / 3), (date(2024, 6, 10), 0.581)]
    third = [Report(schedule[0], 300.0), Report(schedule[1], 110.0, True)]
    assert confirm_quantity_splits(both, third, schedule) == [both[0]]


def test_spin_off_adjustment_keeps_price_continuous_and_quantity_untouched() -> None:
    days = _weekdays(date(2024, 1, 2), 40)
    ex_date, ratio = days[20], 1.25
    raw_close = [100.0 if d < ex_date else 80.0 for d in days]  # 20 percent spun off
    schedule = days[9::10]
    reports = [(d, 1000.0) for d in schedule]
    observations, _ = build_observations(
        reports,
        list(zip(days, raw_close)),
        SplitFactor.from_splits([(ex_date, ratio)]),
        schedule,
        quantity_factor=NO_SPLITS,
    )
    rows = run_ladder(observations, side=SHORT)
    assert all(r.flow == 0 for r in rows)
    assert all(r.profit_pct == pytest.approx(0.0) for r in rows)


def test_interval_mean_survives_a_vendor_spike_before_tiny_prices() -> None:
    from positioning.basis import PricePath

    days = _weekdays(date(2024, 1, 2), 30)
    closes = [(days[0], 999999.9999 * 1e6)] + [(d, 1e-9) for d in days[1:]]
    path = PricePath(closes)
    assert path.interval_mean(days[10], days[20]) == pytest.approx(1e-9)
    assert path.interval_mean(days[10], days[20]) > 0
