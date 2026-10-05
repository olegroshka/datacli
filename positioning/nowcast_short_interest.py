"""Nowcasting US short interest from daily short volume (DD-004 stage 2; EVAL-007).

FINRA publishes short interest twice a month (positions as of a settlement
date, published about nine days later) and daily short volume by venue the
next day. This module asks whether the daily short volume, the one public
ripple that is a *flow measurement* of the latent short inventory rather
than a price echo, explains the change the next print will show:

- **period**: the trade dates between two consecutive settlement dates,
  shifted back by the settlement lag (two trading days before 2024-05-28,
  one from then on), so that a period's trades are the ones that settle
  into its print;
- **target** ``y``: the change in the short position over the period in
  units of the symbol's 63-day median consolidated volume (days of volume,
  the denominator that exists for every priced name), winsorised;
- **state** (known at the previous print's publication): the previous
  level in the same units, the two previous changes, the reported days to
  cover, the log median volume and the period's length;
- **flow** (known at the period's end, before the print): cumulative short
  volume and venue volume over the median volume, the mean short ratio, the
  short-exempt share, the cumulative consolidated volume over the median
  volume and its abnormality, the period's return, and the last five days'
  short volume.

Two feature sets per model (``state``, ``state+flow``), linear and boosted,
split by time; the **flow increment** in test-years R² and in the mean
cross-sectional rank correlation with the realised change is the number.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Sequence

import numpy as np
import pandas as pd

from positioning import nowcast_register as nr

LANES: tuple[str, ...] = ("us_common", "us_extended")
LAG_BEFORE = 2
LAG_FROM = dt.date(2024, 5, 28)
LAG_AFTER = 1
ADV_WINDOW = 63
ADV_MIN_PERIODS = 20
LAST_DAYS = 5
FIT_TO = "2021-12-31"
VALIDATE_TO = "2023-12-31"
WINSOR = (0.005, 0.995)
STATE: tuple[str, ...] = ("level_adv", "d_prev1", "d_prev2", "dtc", "log_adv", "days")
FLOW: tuple[str, ...] = ("sv_adv", "tv_adv", "sr_mean", "exempt_share", "cvol_adv", "abn_cvol", "ret_period", "sv_last_adv")
FEATURE_SETS: dict[str, tuple[str, ...]] = {"state": STATE, "state+flow": STATE + FLOW}
MIN_DAYS_SHARE = 0.8
MIN_NAMES_IC = 200


def settlement_lag(settlement_date: dt.date | pd.Timestamp) -> int:
    """Trading days between a trade and its settlement on that date."""
    return LAG_AFTER if pd.Timestamp(settlement_date).date() >= LAG_FROM else LAG_BEFORE


def period_windows(settlement_dates: Sequence[Any], trading_days: Sequence[Any]) -> pd.DataFrame:
    """Per settlement date the trade-date window ``(start_excl, end]`` that settles into it, and the previous settlement date."""
    settles = pd.DatetimeIndex(pd.to_datetime(list(settlement_dates))).sort_values().unique()
    days = pd.DatetimeIndex(pd.to_datetime(list(trading_days))).sort_values().unique()

    def shifted(settle: pd.Timestamp) -> pd.Timestamp | None:
        i = days.searchsorted(settle, side="right") - 1 - settlement_lag(settle)
        return days[i] if i >= 0 else None

    rows = []
    for k in range(1, len(settles)):
        start, end = shifted(settles[k - 1]), shifted(settles[k])
        if start is None or end is None or end <= start:
            continue
        rows.append({"settlement_date": settles[k], "prev_settlement": settles[k - 1], "start_excl": start, "end": end})
    return pd.DataFrame(rows, columns=["settlement_date", "prev_settlement", "start_excl", "end"])


def assign_periods(dates: pd.Series, windows: pd.DataFrame) -> pd.Series:
    """The settlement date whose window holds each trade date (NaT outside every window)."""
    ends = pd.DatetimeIndex(windows["end"])
    starts = pd.DatetimeIndex(windows["start_excl"])
    settles = pd.DatetimeIndex(windows["settlement_date"])
    values = pd.to_datetime(dates)
    pos = ends.searchsorted(values.to_numpy(), side="left")
    inside = pos < len(ends)
    pos_safe = np.where(inside, pos, 0)
    ok = inside & (values.to_numpy() > starts[pos_safe].to_numpy())
    out = pd.Series(pd.NaT, index=dates.index, dtype="datetime64[ns]")
    out[ok] = settles[pos_safe[ok]]
    return out


def flow_features(daily: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Per ``(symbol, settlement_date)`` the flow block from daily rows (``date, symbol, short_volume, short_exempt_volume,
    total_volume, cvolume, adjusted_close, adv``; ``adv`` is the 63-day median consolidated volume as of the row's day)."""
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["settlement_date"] = assign_periods(frame["date"], windows)
    frame = frame[frame["settlement_date"].notna()].sort_values(["symbol", "settlement_date", "date"])
    by = frame.groupby(["symbol", "settlement_date"], sort=False)
    frame["_rank_from_end"] = by.cumcount(ascending=False)
    last = frame[frame["_rank_from_end"] < LAST_DAYS].groupby(["symbol", "settlement_date"])["short_volume"].sum().rename("sv_last")
    out = by.agg(
        sv=("short_volume", "sum"), exempt=("short_exempt_volume", "sum"), tv=("total_volume", "sum"), cvol=("cvolume", "sum"),
        days=("date", "size"), first_close=("adjusted_close", "first"), last_close=("adjusted_close", "last"), adv=("adv", "first"),
    ).join(last)
    window_days = windows.assign(window_days=0).set_index("settlement_date")["window_days"]
    out = out.reset_index()
    adv = out["adv"].where(out["adv"] > 0)
    out["sv_adv"] = out["sv"] / adv
    out["tv_adv"] = out["tv"] / adv
    out["sr_mean"] = out["sv"] / out["tv"].where(out["tv"] > 0)
    out["exempt_share"] = out["exempt"] / out["sv"].where(out["sv"] > 0)
    out["cvol_adv"] = out["cvol"] / adv
    out["abn_cvol"] = out["cvol"] / (adv * out["days"])
    out["ret_period"] = out["last_close"] / out["first_close"].where(out["first_close"] > 0) - 1.0
    out["sv_last_adv"] = out["sv_last"].fillna(0.0) / adv
    out["log_adv"] = np.log(adv)
    del window_days
    return out[["symbol", "settlement_date", "days", "adv", "log_adv", *FLOW]]


def assemble(short_interest: pd.DataFrame, flows: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Per ``(symbol, settlement_date)`` the target and the state block joined to the flow block.

    ``short_interest`` has ``symbol, settlement_date, short_position, days_to_cover``; the target needs the previous
    print to be the calendar's previous settlement date (no gap), and a flow block with at least ``MIN_DAYS_SHARE`` of
    the window's trading days.
    """
    si = short_interest.copy()
    si["settlement_date"] = pd.to_datetime(si["settlement_date"])
    si = si.sort_values(["symbol", "settlement_date"]).reset_index(drop=True)
    by = si.groupby("symbol", sort=False)
    si["prev_settlement"] = by["settlement_date"].shift(1)
    si["prev_position"] = by["short_position"].shift(1)
    si["dtc"] = by["days_to_cover"].shift(1)
    frame = si.merge(flows, on=["symbol", "settlement_date"], how="inner")
    frame = frame.merge(windows[["settlement_date", "prev_settlement"]].rename(columns={"prev_settlement": "calendar_prev"}), on="settlement_date", how="left")
    consecutive = frame["prev_settlement"] == frame["calendar_prev"]
    adv = frame["adv"].where(frame["adv"] > 0)
    frame["y"] = ((frame["short_position"] - frame["prev_position"]) / adv).where(consecutive)
    frame["level_adv"] = (frame["prev_position"] / adv).where(consecutive)
    frame = frame.sort_values(["symbol", "settlement_date"]).reset_index(drop=True)
    by = frame.groupby("symbol", sort=False)
    frame["d_prev1"] = by["y"].shift(1)
    frame["d_prev2"] = by["y"].shift(2)
    full_days = frame.groupby("settlement_date")["days"].transform("max")
    enough = frame["days"] >= MIN_DAYS_SHARE * full_days
    frame = frame[frame["y"].notna() & enough].reset_index(drop=True)
    frame["date"] = frame["settlement_date"]
    return frame


def winsorise(values: pd.Series, *, reference: pd.Series | None = None, limits: tuple[float, float] = WINSOR) -> pd.Series:
    """Clip at the reference sample's quantiles (the fit years' when scoring later years)."""
    ref = values if reference is None else reference
    lo, hi = ref.quantile(limits[0]), ref.quantile(limits[1])
    return values.clip(lower=lo, upper=hi)


def rank_ic(frame: pd.DataFrame, prediction: np.ndarray, *, min_names: int = MIN_NAMES_IC) -> float:
    """Mean over settlement dates of the cross-sectional Spearman correlation between the prediction and ``y``."""
    part = frame[["date", "y"]].copy()
    part["p"] = prediction
    values = []
    for _, group in part.groupby("date", sort=True):
        if len(group) < min_names:
            continue
        values.append(group["p"].rank().corr(group["y"].rank()))
    return float(np.nanmean(values)) if values else float("nan")


def fit_score(
    frame: pd.DataFrame,
    *,
    features: Sequence[str],
    features_label: str,
    model: str,
    masks: dict[str, pd.Series],
    iterations: Sequence[int] = nr.BOOST_ITERATIONS,
    min_names: int = MIN_NAMES_IC,
) -> list[nr.Score]:
    """Ridge or boosted regression of the winsorised ``y``; R² and the mean rank IC on the validation and test years."""
    fit, validate, test = (frame.loc[masks[k]] for k in ("fit", "validate", "test"))
    y_fit = winsorise(fit["y"])
    columns = list(features)
    scores: list[nr.Score] = []
    best_n, best_r2, best_pred = iterations[0], -np.inf, None
    for n in iterations if model == "boost" else iterations[:1]:
        estimator = nr.make_model("regression", model, iterations=n)
        estimator.fit(fit[columns], y_fit.to_numpy())
        pred = np.asarray(estimator.predict(validate[columns]), dtype=float)
        r2 = nr.metric_values("regression", winsorise(validate["y"], reference=fit["y"]).to_numpy(), pred)["r2"]
        if r2 > best_r2:
            best_n, best_r2, best_pred = n, r2, pred
    scores.append(nr.Score("y", model, features_label, "validate", "r2", best_r2, len(validate), float("nan")))
    scores.append(nr.Score("y", model, features_label, "validate", "rank_ic", rank_ic(validate, best_pred, min_names=min_names), len(validate), float("nan")))
    both = pd.concat([fit, validate])
    estimator = nr.make_model("regression", model, iterations=best_n)
    estimator.fit(both[columns], winsorise(both["y"]).to_numpy())
    pred = np.asarray(estimator.predict(test[columns]), dtype=float)
    r2 = nr.metric_values("regression", winsorise(test["y"], reference=both["y"]).to_numpy(), pred)["r2"]
    scores.append(nr.Score("y", model, features_label, "test", "r2", r2, len(test), float("nan")))
    scores.append(nr.Score("y", model, features_label, "test", "rank_ic", rank_ic(test, pred, min_names=min_names), len(test), float("nan")))
    return scores


def run(
    frame: pd.DataFrame,
    *,
    models: Sequence[str] = ("linear", "boost"),
    fit_to: str = FIT_TO,
    validate_to: str = VALIDATE_TO,
    min_names: int = MIN_NAMES_IC,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The score table and the flow increments (``state+flow`` minus ``state``)."""
    masks = nr.split(frame, fit_to=fit_to, validate_to=validate_to)
    scores: list[nr.Score] = []
    for model in models:
        for label, features in FEATURE_SETS.items():
            scores.extend(fit_score(frame, features=features, features_label=label, model=model, masks=masks, min_names=min_names))
    table = pd.DataFrame([s.__dict__ for s in scores])
    wide = table.pivot_table(index=["target", "model", "years", "metric"], columns="features", values="value", aggfunc="first", dropna=False)
    wide["increment"] = wide["state+flow"] - wide["state"]
    return table, wide.reset_index()
