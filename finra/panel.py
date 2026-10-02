"""Point-in-time positioning features from the FINRA short volume store.

The scoring panel (``scoring.panel_eval``) decides at the **close of trading
day T** (the session an article lands in) and measures the next session's
move, ``f1`` = close(T) to close(T+1). FINRA publishes day T's short volume at
18:00 ET on T, which is *after* that close. So the feature that is honestly
observable at the decision point is built from FINRA days **up to T-1**; this
module names those columns ``*_known``. The same windows ending at T are kept
as ``*_t`` for a decision taken after 18:00 ET (an entry at the next open),
and are labelled as such wherever they are reported.

Everything is trailing: rolling means over a symbol's own prior FINRA rows,
and the "abnormal" ratio is a short window minus a longer trailing baseline.
No full-sample statistic is used anywhere. The output is one row per
``(eodhd_code, date)`` for common symbols only.

Timeline (``T`` = FINRA trade date = the panel's trading day):

    FINRA(T-1) published 18:00 ET on T-1  ->  observable at close(T)   -> *_known
    FINRA(T)   published 18:00 ET on T    ->  observable after 18:00 ET -> *_t
    f1 = close(T) -> close(T+1)
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import pandas as pd

DEFAULT_WINDOWS: tuple[int, ...] = (1, 5, 20)
DEFAULT_BASELINE = 60
VIEW = "finra_short_volume"


def feature_columns(
    windows: Sequence[int] = DEFAULT_WINDOWS, baseline: int = DEFAULT_BASELINE
) -> list[str]:
    cols: list[str] = []
    for w in windows:
        cols += [f"sr{w}_known", f"sr{w}_t"]
    cols += [f"sr_abn_known", f"sr_abn_t", "n_rows_known"]
    return cols


def short_ratio_features(
    con: Any,
    *,
    start: date | str,
    end: date | str,
    windows: Sequence[int] = DEFAULT_WINDOWS,
    baseline: int = DEFAULT_BASELINE,
    short_window: int = 5,
    view: str = VIEW,
) -> pd.DataFrame:
    """Trailing short-ratio features per ``(eodhd_code, date)`` on ``[start, end]``.

    ``sr{w}_t`` is the mean short ratio over the ``w`` most recent FINRA rows of
    the symbol ending at that date; ``sr{w}_known`` is the same window ending at
    the previous FINRA row (what the close of that date can know).
    ``sr_abn_*`` is ``sr{short_window}`` minus the ``baseline``-row trailing
    mean. ``n_rows_known`` counts the prior rows inside the baseline window, so a
    thin history can be screened out by the caller.

    The windows are computed over the whole store so the first rows inside
    ``[start, end]`` already have history; only the output is clipped.
    """
    windows = tuple(windows)
    if short_window not in windows:
        raise ValueError(
            f"short_window {short_window} must be one of windows {windows}"
        )
    means_t = ", ".join(
        f"avg(short_ratio) OVER (PARTITION BY eodhd_code ORDER BY date "
        f"ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW) AS sr{w}_t"
        for w in windows
    )
    base_t = (
        f"avg(short_ratio) OVER (PARTITION BY eodhd_code ORDER BY date "
        f"ROWS BETWEEN {baseline - 1} PRECEDING AND CURRENT ROW) AS base_t, "
        f"count(short_ratio) OVER (PARTITION BY eodhd_code ORDER BY date "
        f"ROWS BETWEEN {baseline - 1} PRECEDING AND CURRENT ROW) AS n_rows_t"
    )
    known = ", ".join(
        f"lag(sr{w}_t) OVER (PARTITION BY eodhd_code ORDER BY date) AS sr{w}_known"
        for w in windows
    )
    sql = f"""
        WITH base AS (
          SELECT eodhd_code, date, short_ratio
          FROM {view}
          WHERE eodhd_code IS NOT NULL AND short_ratio IS NOT NULL
            AND security_kind = 'common'
        ), t AS (
          SELECT eodhd_code, date, {means_t}, {base_t}
          FROM base
        ), k AS (
          SELECT eodhd_code, date, {", ".join(f"sr{w}_t" for w in windows)},
                 sr{short_window}_t - base_t AS sr_abn_t,
                 {known},
                 lag(sr{short_window}_t - base_t)
                   OVER (PARTITION BY eodhd_code ORDER BY date) AS sr_abn_known,
                 lag(n_rows_t) OVER (PARTITION BY eodhd_code ORDER BY date) AS n_rows_known
          FROM t
        )
        SELECT * FROM k
        WHERE date BETWEEN DATE '{pd.Timestamp(start).date()}' AND DATE '{pd.Timestamp(end).date()}'
        ORDER BY eodhd_code, date
    """
    out = con.execute(sql).df()
    if out.empty:
        return out
    out["date"] = pd.to_datetime(out["date"])
    out["n_rows_known"] = out["n_rows_known"].fillna(0).astype(int)
    return out


def attach_positioning(
    panel: pd.DataFrame,
    features: pd.DataFrame,
    *,
    min_history: int = 20,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Join the scored panel (``symbol`` like ``AAPL.US``, ``trade_date``) to the features.

    The join key is the FINRA ``eodhd_code`` against the panel's ticker (the part
    before the exchange suffix) on the panel's **trading** day, so a weekend
    article meets the FINRA row of the Monday it prices on. Rows whose symbol has
    fewer than ``min_history`` prior FINRA rows are dropped and counted.
    """
    if panel.empty or features.empty:
        return pd.DataFrame(), {"panel_rows": int(len(panel)), "matched_rows": 0}
    p = panel.copy()
    p["eodhd_code"] = p["symbol"].astype(str).str.split(".").str[0].str.upper()
    p["trade_date"] = pd.to_datetime(p["trade_date"])
    f = features.rename(columns={"date": "trade_date"})
    merged = p.merge(f, on=["eodhd_code", "trade_date"], how="inner")
    thin = int((merged["n_rows_known"] < min_history).sum())
    merged = merged[merged["n_rows_known"] >= min_history]
    return merged, {
        "panel_rows": int(len(panel)),
        "matched_rows": int(len(merged)) + thin,
        "dropped_thin_history": thin,
        "rows": int(len(merged)),
        "match_share": round((len(merged) + thin) / max(len(panel), 1), 3),
        "n_days": int(merged["trade_date"].nunique()) if not merged.empty else 0,
    }
