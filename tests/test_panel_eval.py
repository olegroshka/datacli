from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (str(_REPO_ROOT), str(_REPO_ROOT / "eodhd")):
    if p not in sys.path:
        sys.path.insert(0, p)

from scoring import panel_eval as pe  # noqa: E402


def test_lags_for_reads_the_overlap_out_of_the_horizon_name() -> None:
    assert pe._lags_for("f0") == 0
    assert pe._lags_for("f0_ex") == 0
    assert pe._lags_for("f1") == 0  # a 1-day return does not overlap the next
    assert pe._lags_for("f5_ex") == 4
    assert pe._lags_for("f20_ex") == 19
    assert pe._lags_for("weird") == 0


def test_t_stat_penalises_overlapping_windows() -> None:
    """The whole point of the Newey-West correction: a strongly autocorrelated
    series must not earn the standard error of an independent one."""
    # a smooth, highly autocorrelated series with a positive mean -- exactly the
    # shape of overlapping k-day forward returns
    x = pd.Series(np.sin(np.linspace(0, 6, 400)) * 0.01 + 0.002)
    naive = pe._t_stat(x, lags=0)
    corrected = pe._t_stat(x, lags=19)
    assert naive["mean_bps"] == corrected["mean_bps"]  # same estimate
    assert abs(corrected["t"]) < abs(naive["t"])  # ...bigger uncertainty
    assert corrected["nw_lags"] == 19 and naive["nw_lags"] == 0
    # an i.i.d. series should be barely affected either way
    rng = np.random.default_rng(0)
    noise = pd.Series(rng.normal(0.001, 0.01, 400))
    assert abs(pe._t_stat(noise, lags=5)["t"] - pe._t_stat(noise)["t"]) < 1.5


def test_t_stat_refuses_a_series_too_short_to_judge() -> None:
    assert pe._t_stat(pd.Series([0.01, 0.02])) == {"n_days": 2}


def test_t_stat_is_conservative_on_a_zero_variance_series() -> None:
    """A spread identical on every day has no sample variance, so its t is
    undefined. Report no evidence rather than infinite evidence -- real return
    series always vary, so this only ever fires on synthetic or degenerate input."""
    flat = pe._t_stat(pd.Series([0.01] * 50))
    assert flat["mean_bps"] == 100.0 and flat["t"] == 0.0 and flat["p"] == 1.0


def _panel(n_days: int, n_names: int, score_fn, ret_fn, n_articles_fn=None):
    rows = []
    for d in range(n_days):
        for s in range(n_names):
            rows.append(
                {
                    "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                    "symbol": f"S{s}.US",
                    "score": score_fn(d, s),
                    "score_w": score_fn(d, s),
                    "n_articles": (n_articles_fn or (lambda d, s: 1))(d, s),
                    "mat_max": s % 4,
                    "f1_ex": ret_fn(d, s),
                    "trade_date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                }
            )
    return pd.DataFrame(rows)


def test_cross_section_finds_a_planted_monotone_spread() -> None:
    # score ranks with the return every day, but the size of the spread varies
    # day to day -- a spread that is *identical* every day has zero sample
    # variance, which makes the t undefined rather than infinite
    df = _panel(
        60,
        40,
        lambda d, s: s / 40,
        lambda d, s: (s - 20) * 0.001 * (1 + 0.4 * math.sin(d)),
    )
    table, stats = pe.cross_section(df, horizon="f1_ex", score_col="score")
    assert stats["monotone"] == 1.0
    assert stats["mean_bps"] > 0 and stats["p"] < 0.01
    assert list(table["bucket"]) == [0, 1, 2, 3, 4]
    assert table["mean_bps"].is_monotonic_increasing


def test_cross_section_does_not_invent_a_spread_from_a_saturated_score() -> None:
    """The bug this guard exists for: the vendor polarity is >=0.99 on half its
    rows, and breaking those ties by row order manufactured a t of 4.87 out of a
    field with no dispersion. Tied scores must land in the same bucket."""
    # every name scores 1.0 except one; returns rise with the (arbitrary) row order
    df = _panel(
        60,
        40,
        lambda d, s: 0.2 if s == 0 else 1.0,
        lambda d, s: (s - 20) * 0.001,
    )
    table, stats = pe.cross_section(df, horizon="f1_ex", score_col="score")
    # only two distinct scores exist, so the guard refuses to rank at all
    assert stats.get("n_days_rankable", 0) == 0 or stats.get("n_days", 0) == 0

    # with a little genuine dispersion it ranks, but ties stay together
    df2 = _panel(
        60, 40, lambda d, s: min(s, 5) / 5.0, lambda d, s: (s - 20) * 0.001
    )
    t2, s2 = pe.cross_section(df2, horizon="f1_ex", score_col="score")
    top = t2[t2["bucket"] == t2["bucket"].max()]
    # the saturated top group is large because all the 1.0s share one rank
    assert int(top["n_rows"].iloc[0]) > 60 * 40 / 5


def test_cross_section_skips_days_with_too_thin_a_cross_section() -> None:
    df = _panel(30, 5, lambda d, s: s / 5, lambda d, s: (s - 2) * 0.001)
    _, stats = pe.cross_section(df, horizon="f1_ex", score_col="score", min_names=20)
    assert stats == {"n_days": 0}


def test_magnitude_orders_absolute_moves_by_materiality() -> None:
    # |return| grows with mat_max (= s % 4), sign alternates so direction is noise
    df = _panel(
        40,
        40,
        lambda d, s: 0.0,
        lambda d, s: (1 if s % 2 else -1)
        * (s % 4 + 1)
        * 0.01
        * (1 + 0.4 * math.sin(d)),
    )
    table, stats = pe.magnitude(df, horizon="f1_ex", field="mat_max")
    assert stats["monotone"] == 1.0
    assert stats["spread_bps"] > 0 and stats["p"] < 0.01
    assert table["mean_abs_bps"].is_monotonic_increasing


def test_intensity_buckets_by_article_count() -> None:
    df = _panel(
        40,
        40,
        lambda d, s: 0.0,
        lambda d, s: (1 if s % 2 else -1) * (s + 1) * 0.001 * (1 + 0.4 * math.sin(d)),
        n_articles_fn=lambda d, s: s + 1,
    )
    table, stats = pe.intensity(df, horizon="f1_ex")
    assert list(table["n_bucket"]) == ["1", "2", "3", "4-5", "6+"]
    assert stats["monotone"] == 1.0 and stats["spread_bps"] > 0


def test_empty_and_missing_columns_are_handled() -> None:
    empty = pd.DataFrame()
    t, s = pe.cross_section(empty)
    assert t.empty and s == {}
    t, s = pe.magnitude(empty)
    assert t.empty and s == {}
    t, s = pe.intensity(empty)
    assert t.empty and s == {}
    df = _panel(5, 5, lambda d, s: 0.0, lambda d, s: 0.0)
    t, s = pe.cross_section(df, horizon="f99_ex")  # horizon absent
    assert t.empty and s == {}


@pytest.mark.parametrize("field", ["mat_max", "mat_mean"])
def test_magnitude_accepts_either_materiality_column(field: str) -> None:
    df = _panel(40, 40, lambda d, s: 0.0, lambda d, s: (s % 4 + 1) * 0.01)
    df["mat_mean"] = df["mat_max"]
    table, stats = pe.magnitude(df, horizon="f1_ex", field=field)
    assert not table.empty and stats["monotone"] == 1.0


def test_calibration_reads_predicted_against_realised() -> None:
    """expected_move is stated in return units, so it can be checked against
    reality rather than only ranked. A field that realises exactly what it
    predicts must show ratio 1.0 in every bucket."""
    rows = []
    buckets = [0.0, 0.005, 0.02, 0.05, 0.10]
    for d in range(40):
        for i, b in enumerate(buckets):
            for k in range(10):
                rows.append(
                    {
                        "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                        "symbol": f"S{i}{k}.US",
                        "expected_move_max": b,
                        # realises exactly the predicted size, sign alternating
                        "f1_ex": (1 if k % 2 else -1) * b,
                    }
                )
    df = pd.DataFrame(rows)
    table, stats = pe.calibration(df, horizon="f1_ex", field="expected_move_max")
    assert list(table["predicted_bps"]) == [0.0, 50.0, 200.0, 500.0, 1000.0]
    assert list(table["realised_bps"]) == [0.0, 50.0, 200.0, 500.0, 1000.0]
    assert [r for r in table["ratio"][1:]] == [1.0, 1.0, 1.0, 1.0]
    assert stats["monotone"] == 1.0 and stats["slope"] == pytest.approx(1.0)
    assert stats["top_bucket_ratio"] == 1.0


def test_calibration_flags_a_field_that_is_ordered_but_overstated() -> None:
    """The realistic case: the ordering is right but the sizes are too big. That
    is still usable as a ranking, and the ratio is what says so."""
    rows = []
    for d in range(40):
        for i, b in enumerate([0.005, 0.02, 0.05]):
            for k in range(10):
                rows.append(
                    {
                        "date": pd.Timestamp("2024-01-01") + pd.Timedelta(days=d),
                        "symbol": f"S{i}{k}.US",
                        "expected_move_max": b,
                        "f1_ex": (1 if k % 2 else -1) * b * 0.4,  # realises 40%
                    }
                )
    _, stats = pe.calibration(pd.DataFrame(rows), field="expected_move_max")
    assert stats["monotone"] == 1.0  # ordering intact
    assert stats["slope"] == pytest.approx(0.4, abs=0.01)  # sizes overstated
    assert stats["top_bucket_ratio"] == pytest.approx(0.4, abs=0.01)


def test_calibration_needs_the_field_and_horizon_present() -> None:
    df = _panel(5, 5, lambda d, s: 0.0, lambda d, s: 0.01)
    t, s = pe.calibration(df, field="expected_move_max")  # field absent
    assert t.empty and s == {}
