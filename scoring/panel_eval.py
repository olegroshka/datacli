"""``score panel-eval`` -- does the scored corpus carry signal *as a panel*?

``score bench`` asks whether one model's call on one article predicts that
article's stock. That is the noisiest possible test, and it came back empty: a
+3.7pp edge on the publication session and nothing at all on the next one. This
module asks the question the other way round, which is how such a signal would
actually be used:

1. **Cross-section, not article.** Aggregate every article to a ``(date, symbol)``
   score, rank the names within each day, and measure the spread between the top
   and bottom buckets. Averaging over a day's cross-section cancels the
   idiosyncratic noise that swamps a single-article hit rate, so a signal far too
   weak to call one stock can still show up here.
2. **Magnitude, not direction.** ``materiality`` ordering ``|return|`` was clean on
   27k rows while direction was noise. Which way a stock moves is close to
   unforecastable; *how far* it moves is genuinely forecastable, and is the more
   honest target.
3. **Days, not one horizon.** Same-session, next-day, one week and one month.
   Post-announcement drift lives at weeks, not at a single close.

Every return is reported both raw and with the exchange's median move over the
same interval subtracted, because company news is stock-specific and the market
component is noise. Bad ticks are filtered explicitly and counted: the price store
carries daily returns into the thousands of percent (see roadmap item 6), and one
unadjusted split inside a bucket would otherwise dominate its mean.

The statistic for a bucket spread is a t over **days**, not over rows -- the daily
long-short returns are the independent observations, and treating each
``(article, symbol)`` row as independent would overstate significance several-fold.

Pure functions take DataFrames; the CLI wraps them with the connection.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import pandas as pd

log = logging.getLogger("scoring.panel_eval")

#: Horizons in trading days. 0 is the session the article lands in.
HORIZONS = (0, 1, 5, 20)
#: A one-day move beyond this is treated as a bad tick, not a return. The store
#: holds daily returns up to +24,500%, so this filter is doing real work.
MAX_ABS_DAILY = 0.50
#: Multi-day returns beyond this are dropped as clear data errors.
MAX_ABS_MULTIDAY = 2.00
#: Minimum stocks behind an exchange-session median before it is used as a proxy.
MIN_MARKET_MEMBERS = 20
#: Minimum names in a day's cross-section before it can be ranked.
MIN_CROSS_SECTION = 20
#: Impact fields a schema may carry, most-preferred first. v2/v3 have
#: `materiality`; v4 adds `expected_move`, which asks for the realised quantity
#: directly. Whichever are present get aggregated, so the two can be compared.
IMPACT_FIELDS = ("expected_move", "materiality")
#: Minimum distinct score values in a day before ranking means anything. A
#: saturated field (the vendor polarity is >=0.99 on half its rows) cannot order a
#: cross-section, and forcing it into buckets invents a spread.
MIN_DISTINCT_SCORES = 5


# --------------------------------------------------------------------------- #
# inputs
# --------------------------------------------------------------------------- #
def signal_panel(
    con: Any,
    view: str,
    *,
    backend: str | None = None,
    role: str = "subject",
) -> pd.DataFrame:
    """One row per ``(date, symbol)``: the day's aggregated score for that name.

    ``score_w`` weights each article's sentiment by ``materiality + 1`` -- the
    premise of the schema is that material news matters more, and the ``+1`` keeps
    immaterial articles contributing a little rather than dropping out entirely.
    ``score`` is the plain mean, kept so the weighting can be shown to earn its
    place rather than assumed.
    """
    where = ["s.symbol IS NOT NULL", "s.status = 'ok'", "s.sentiment IS NOT NULL"]
    params: list[Any] = []
    if backend:
        where.append("s.backend = ?")
        params.append(backend)
    if role:
        where.append("s.role = ?")
        params.append(role)
    # Impact fields vary by schema version: v2/v3 have `materiality`, v4 adds
    # `expected_move`. Pull whichever the view actually has so one panel supports
    # comparing them, rather than hard-coding the fields of one schema.
    have = {
        r[0]
        for r in con.execute(f"DESCRIBE {view}").fetchall()
    }
    impact = [f for f in IMPACT_FIELDS if f in have]
    extra_sel = "".join(f", any_value({f}) AS {f}" for f in impact)
    df = con.execute(
        f"""
        SELECT cast(s.date AS DATE) AS date, upper(s.symbol) AS symbol,
               s.article_id, s.sentiment, a.event_type{
            "".join(f", a.{f}" for f in impact)
        }
        FROM {view} s
        JOIN (
            SELECT article_id, any_value(event_type) AS event_type{extra_sel}
            FROM {view} WHERE symbol IS NULL AND status = 'ok'
            GROUP BY article_id
        ) a USING (article_id)
        WHERE {" AND ".join(where)}
        """,
        params,
    ).df()
    if df.empty:
        return df
    df["sentiment"] = pd.to_numeric(df["sentiment"], errors="coerce")
    for f in impact:
        df[f] = pd.to_numeric(df[f], errors="coerce").fillna(0.0)
    df = df.dropna(subset=["sentiment"])
    # Weight sentiment by whichever impact field is the schema's primary one, so
    # `score_w` means the same thing across versions.
    weight_on = impact[0] if impact else None
    df["w"] = (df[weight_on] + 1.0) if weight_on else 1.0
    df["sw"] = df["sentiment"] * df["w"]
    g = df.groupby(["date", "symbol"], observed=True)
    cols = {
        "n_articles": g["article_id"].nunique(),
        "score": g["sentiment"].mean(),
        "score_w": g["sw"].sum() / g["w"].sum(),
    }
    for f in impact:
        # `_max` is the one that matters: a name's day is characterised by its
        # most impactful article, not by the average of it with routine filings.
        cols[f"{f}_max"] = g[f].max()
        cols[f"{f}_mean"] = g[f].mean()
    out = pd.DataFrame(cols).reset_index()
    if "materiality_max" in out.columns:  # names the earlier tests and CLI use
        out["mat_max"] = out["materiality_max"]
        out["mat_mean"] = out["materiality_mean"]
    out["date"] = pd.to_datetime(out["date"])
    return out


def vendor_panel(con: Any, *, since: str | None = None) -> pd.DataFrame:
    """The same panel shape, built from the vendor polarity in ``news_daily``.

    This exists because it is the only signal available over the *whole* corpus.
    Our own scores cover 17 trading days, which cannot support a t over days; the
    vendor's cover ~2,800. It is a saturated VADER-style baseline, so it is not a
    ceiling on what our scores could do -- but it answers the prior question at
    proper scale: does aggregated news sentiment carry cross-sectional signal on
    this corpus *at all*? A flat result over 2,800 days is a far stronger finding
    than a flat result over 17, and a positive one would justify scoring more days
    with the better model.
    """
    where = "polarity_mean IS NOT NULL"
    params: list[Any] = []
    if since:
        where += " AND date >= ?"
        params.append(since)
    df = con.execute(
        f"""
        SELECT date, upper(symbol) AS symbol, n_articles, n_sources, share_of_day,
               polarity_mean AS score, polarity_mean AS score_w,
               pos_share, neg_share
        FROM news_daily WHERE {where}
        """,
        params,
    ).df()
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def attach_returns(
    con: Any,
    panel: pd.DataFrame,
    *,
    horizons: tuple[int, ...] = HORIZONS,
    max_abs_daily: float = MAX_ABS_DAILY,
    max_abs_multiday: float = MAX_ABS_MULTIDAY,
    min_market: int = MIN_MARKET_MEMBERS,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Join each panel row to the first trading day at or after its date, with
    forward returns at every horizon, raw and market-adjusted.

    Done entirely in DuckDB: ``prices`` holds 52M rows and materialising the
    return table in pandas would be tens of millions of rows for a panel that
    needs 1.5M. An ASOF join maps publication date to trading day in one pass --
    the correlated-subquery form of the same lookup cost ~34s *per day* on this
    store.

    ``f0`` is the session the article lands in; ``f{h}`` runs from ``close(T)`` to
    ``close(T+h)``. ``_ex`` subtracts the exchange's median move over the same
    interval, so a name that merely drifted with its market does not score.
    """
    if panel.empty:
        return pd.DataFrame(), {}
    fwd = [h for h in horizons if h > 0]
    lo = str(pd.to_datetime(panel["date"]).min().date())
    leads = ", ".join(f"lead(adjusted_close, {h}) OVER w AS c{h}" for h in fwd)
    rets = ", ".join(f"c{h} / c - 1 AS f{h}" for h in fwd)
    tick_filter = "".join(
        f" AND (f{h} IS NULL OR abs(f{h}) <= "
        f"{max_abs_daily if h == 1 else max_abs_multiday})"
        for h in fwd
    )
    med = ", ".join(f"median(f{h}) AS m{h}" for h in [0, *fwd])
    exc = ", ".join(f"c.f{h} - k.m{h} AS f{h}_ex" for h in [0, *fwd])
    cols = ", ".join(f"r.f{h}" for h in [0, *fwd]) + ", " + ", ".join(
        f"r.f{h}_ex" for h in [0, *fwd]
    )
    con.register("_panel", panel[["date", "symbol"]])
    sql = f"""
        WITH px AS (
          SELECT upper(ticker) || '.' || upper(exchange) AS symbol,
                 upper(exchange) AS x, cast(date AS DATE) AS d,
                 adjusted_close AS c,
                 lag(adjusted_close) OVER w AS c_prev,
                 {leads}
          FROM prices
          WHERE adjusted_close IS NOT NULL AND adjusted_close > 0
            AND cast(date AS DATE) >= DATE '{lo}' - INTERVAL 10 DAY
          WINDOW w AS (PARTITION BY upper(ticker), upper(exchange)
                       ORDER BY cast(date AS DATE))
        ), rr AS (
          SELECT symbol, x, d, c / c_prev - 1 AS f0, {rets}
          FROM px WHERE c_prev IS NOT NULL AND c_prev > 0
        ), clean AS (
          SELECT * FROM rr WHERE abs(f0) <= {max_abs_daily}{tick_filter}
        ), k AS (
          SELECT x, d, count(*) AS n_mkt, {med} FROM clean GROUP BY x, d
        ), r AS (
          SELECT c.*, {exc}, k.n_mkt
          FROM clean c JOIN k ON k.x = c.x AND k.d = c.d
          WHERE k.n_mkt >= {min_market}
        )
        SELECT p.date, p.symbol, r.d AS trade_date, {cols}
        FROM _panel p
        ASOF JOIN r ON p.symbol = r.symbol AND p.date <= r.d
    """
    joined = con.execute(sql).df()
    con.unregister("_panel")
    report = {
        "panel_rows": int(len(panel)),
        "matched_rows": int(len(joined)),
        "match_share": round(len(joined) / max(len(panel), 1), 3),
    }
    if joined.empty:
        return joined, report
    joined["date"] = pd.to_datetime(joined["date"])
    out = panel.merge(joined, on=["date", "symbol"], how="inner")
    report["joined_rows"] = int(len(out))
    report["n_days"] = int(out["trade_date"].nunique())
    return out, report


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #
def _t_stat(daily: pd.Series, *, lags: int = 0) -> dict[str, Any]:
    """Mean, t and two-sided p of a daily series -- days are the observations.

    ``lags`` must be set to the return horizon minus one whenever the series uses
    **overlapping** windows, which it does for every horizon past one day: a
    20-day forward return computed each day shares 19 of its 20 days with the next
    observation, so the daily values are strongly autocorrelated and a plain
    i.i.d. standard error is far too small. With 2,537 days of 20-day returns the
    naive t was 4.01; the effective number of independent observations is closer to
    127, and Newey-West with Bartlett weights is the standard correction. Ignoring
    this is the single easiest way to report a spurious result at this horizon.
    """
    d = daily.dropna()
    n = len(d)
    if n < 5:
        return {"n_days": n}
    x = d.to_numpy(dtype=float)
    mean = float(x.mean())
    dev = x - mean
    gamma0 = float((dev * dev).sum()) / n
    var = gamma0
    used = min(max(lags, 0), n - 1)
    for lag in range(1, used + 1):
        cov = float((dev[lag:] * dev[:-lag]).sum()) / n
        var += 2.0 * (1.0 - lag / (used + 1.0)) * cov
    var = max(var, 0.0)
    se = math.sqrt(var / n) if var > 0 else 0.0
    t = mean / se if se > 0 else 0.0
    return {
        "n_days": n,
        "mean_bps": round(mean * 10000, 1),
        "sd_bps": round(float(d.std(ddof=1)) * 10000, 1),
        "nw_lags": used,
        "t": round(t, 2),
        "p": round(math.erfc(abs(t) / math.sqrt(2.0)), 4),
    }


def _lags_for(horizon: str) -> int:
    """Overlap in a forward-return series named ``f{h}`` or ``f{h}_ex``."""
    digits = "".join(ch for ch in horizon.split("_")[0] if ch.isdigit())
    return max(int(digits) - 1, 0) if digits else 0


def cross_section(
    df: pd.DataFrame,
    *,
    horizon: str = "f1_ex",
    score_col: str = "score_w",
    n_buckets: int = 5,
    min_names: int = MIN_CROSS_SECTION,
    min_distinct: int = MIN_DISTINCT_SCORES,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rank each day's names by score; return per-bucket means and the spread.

    The long-short statistic is a t over days: each day contributes one
    observation, because the names within a day share that day's market shock and
    are not independent draws.
    """
    if df.empty or horizon not in df.columns or score_col not in df.columns:
        return pd.DataFrame(), {}
    d = df.dropna(subset=[horizon, score_col]).copy()
    # only days with a wide enough cross-section to rank meaningfully
    sizes = d.groupby("date")["symbol"].nunique()
    d = d[d["date"].isin(sizes[sizes >= min_names].index)]
    if d.empty:
        return pd.DataFrame(), {"n_days": 0}

    # Ties must share a bucket. Ranking with method="first" breaks ties by row
    # order -- which is alphabetical by symbol here -- and then qcut splits a
    # single saturated score across several buckets, manufacturing a spread out of
    # nothing. The vendor polarity is >=0.99 on half its rows, and that bug alone
    # produced a t of 4.87 on a field with no dispersion. "average" gives tied
    # scores one rank, and duplicates="drop" then merges the empty edges.
    def _bucket(s: pd.Series) -> pd.Series:
        if s.nunique() < min_distinct:
            return pd.Series([pd.NA] * len(s), index=s.index)
        try:
            return pd.qcut(
                s.rank(method="average"), n_buckets, labels=False, duplicates="drop"
            )
        except ValueError:
            return pd.Series([pd.NA] * len(s), index=s.index)

    d["bucket"] = d.groupby("date")[score_col].transform(_bucket)
    d = d.dropna(subset=["bucket"])
    if d.empty:
        return pd.DataFrame(), {"n_days": 0}
    d["bucket"] = d["bucket"].astype(int)
    n_actual = int(d.groupby("date")["bucket"].nunique().median())
    table = (
        d.groupby("bucket")
        .agg(
            n_rows=(horizon, "size"),
            mean_score=(score_col, "mean"),
            mean_bps=(horizon, lambda s: round(float(s.mean()) * 10000, 1)),
        )
        .reset_index()
    )
    per_day = d.groupby(["date", "bucket"])[horizon].mean().unstack("bucket")
    # Use the buckets that actually materialised, not the ones requested. A
    # saturated score collapses under duplicates="drop" -- v4's sentiment is 64.8%
    # exactly neutral, which yields 2 buckets from a request for 5 -- and asking
    # for bucket `n_buckets - 1` then finds nothing and returns silently.
    present = [c for c in per_day.columns if pd.notna(c)]
    top, bot = (max(present), min(present)) if present else (n_buckets - 1, 0)
    stats: dict[str, Any] = {
        "horizon": horizon,
        "score": score_col,
        "n_buckets": n_buckets,
        "buckets_realised": n_actual,
        "n_rows": int(len(d)),
        "n_days_rankable": int(d["date"].nunique()),
        "days_dropped_saturated": int(sizes[sizes >= min_names].shape[0] - d["date"].nunique()),
    }
    # Same fix as magnitude(): the top-minus-bottom spread needs both extreme
    # buckets present on a day, which a saturated score rarely manages -- v5's
    # sentiment paired them on 6 days of 58. The per-day rank correlation between
    # the score and the SIGNED return uses the whole cross-section every day.
    stats.update(_daily_rank_corr(d, score_col, horizon, signed=True))
    if top in per_day.columns and bot in per_day.columns and top != bot:
        ext = _t_stat(per_day[top] - per_day[bot], lags=_lags_for(horizon))
        stats.update({f"ext_{k}": v for k, v in ext.items()})
        stats.update(ext)  # keep the flat names the earlier tests and CLI read
        stats["monotone"] = _spearman_int(table["bucket"], table["mean_bps"])
    elif top == bot:
        stats["note"] = "score too saturated to rank: one bucket"
    return table, stats


def _spearman_int(a: pd.Series, b: pd.Series) -> float:
    x = pd.Series(a).reset_index(drop=True).rank()
    y = pd.Series(b).reset_index(drop=True).rank()
    if x.nunique() < 2 or y.nunique() < 2:
        return float("nan")
    return round(float(x.corr(y)), 3)


def magnitude(
    df: pd.DataFrame, *, horizon: str = "f1_ex", field: str = "mat_max"
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Does ``materiality`` order the *size* of the move, regardless of direction?

    Direction is close to unforecastable; magnitude is the target with a real
    chance. Reported as mean ``|return|`` per materiality level.
    """
    if df.empty or horizon not in df.columns or field not in df.columns:
        return pd.DataFrame(), {}
    d = df.dropna(subset=[horizon, field]).copy()
    if d.empty:
        return pd.DataFrame(), {}
    d["abs_ret"] = d[horizon].abs()
    # Do NOT round. `materiality` happens to be 0-3, but `expected_move` is a
    # decimal return (0.0, 0.005, 0.02, 0.05, 0.10) and rounding collapsed every
    # one of those to 0 -- one level, so the whole test silently returned nothing.
    d[field] = pd.to_numeric(d[field], errors="coerce")
    table = (
        d.groupby(field, observed=True)
        .agg(
            n_rows=("abs_ret", "size"),
            mean_abs_bps=("abs_ret", lambda s: round(float(s.mean()) * 10000, 1)),
            median_abs_bps=("abs_ret", lambda s: round(float(s.median()) * 10000, 1)),
        )
        .reset_index()
    )
    stats: dict[str, Any] = {"horizon": horizon, "field": field,
                             "n_rows": int(len(d))}
    if len(table) >= 3:
        stats["monotone"] = _spearman_int(
            table[field].astype(float), table["mean_abs_bps"]
        )
        stats["spread_bps"] = round(
            float(table["mean_abs_bps"].iloc[-1] - table["mean_abs_bps"].iloc[0]), 1
        )
        # Primary statistic: the per-day rank correlation between the field and
        # |return|, then a t over days. This uses EVERY row and EVERY level each
        # day, which the extremes-only spread below does not -- and that matters
        # enormously once the top level is rare. Sampling 150 articles/day left
        # `materiality = 3` on 15 of 8,005 rows, so only 12 of 63 days had both
        # extremes present and the spread statistic was reduced to 11
        # observations of a very noisy difference. The rank correlation kept all
        # 63 days.
        stats.update(_daily_rank_corr(d, field, horizon))
        # Secondary: top level vs bottom level. Kept because it is the number in
        # return units that a reader can interpret, but it is the fragile one.
        lo, hi = table[field].iloc[0], table[field].iloc[-1]
        per_day = (
            d[d[field].isin([lo, hi])]
            .groupby(["date", field])["abs_ret"]
            .mean()
            .unstack(field)
        )
        if hi in per_day.columns and lo in per_day.columns:
            ext = _t_stat(per_day[hi] - per_day[lo], lags=_lags_for(horizon))
            stats.update({f"ext_{k}": v for k, v in ext.items()})
    return table, stats


def _daily_rank_corr(
    d: pd.DataFrame,
    field: str,
    horizon: str,
    *,
    min_rows: int = 20,
    min_levels: int = 2,
    signed: bool = False,
) -> dict[str, Any]:
    """Mean per-day Spearman of ``field`` against ``|return|``, with a t over days.

    Each day contributes one correlation, computed over that day's whole
    cross-section, so days are still the independent observations but no day is
    discarded merely because its top level happened to be empty. It also answers
    the question we actually care about -- does the field *order* the size of the
    move -- without depending on how the levels are spaced or labelled.
    """
    # Group by the TRADING day, not the publication day: a Saturday and a Sunday
    # article both price on the Monday, so counting them as separate observations
    # would overstate the number of independent days.
    key = "trade_date" if "trade_date" in d.columns else "date"
    vals = []
    for _, g in d.groupby(key):
        if len(g) < min_rows or g[field].nunique() < min_levels:
            continue
        y = g[horizon] if signed else g[horizon].abs()
        c = _spearman_int(g[field], y)
        if c == c:  # not NaN
            vals.append(c)
    if len(vals) < 5:
        return {"corr_n_days": len(vals)}
    s = pd.Series(vals)
    out = _t_stat(s, lags=_lags_for(horizon))
    return {
        "corr_n_days": out.get("n_days"),
        "corr_mean": round(float(s.mean()), 4),
        "corr_t": out.get("t"),
        "corr_p": out.get("p"),
    }


def calibration(
    df: pd.DataFrame, *, horizon: str = "f1_ex", field: str = "expected_move_max"
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Is a predicted move *size* right, not merely correctly ordered?

    ``expected_move`` is stated in the same units as the thing we measure -- a
    decimal return -- so unlike ``materiality`` it can be checked against reality
    directly rather than only ranked. That distinction matters for what the field
    can be used for:

    * **ordered but miscalibrated** is still useful as a ranking (bucket names are
      arbitrary, sort by it and pick the top);
    * **calibrated** is usable as a forecast -- feed it straight into position
      sizing or an options screen.

    The realised comparison is ``mean |return|``, and the ratio column is what to
    read. Note the expected floor: even news implying nothing sits on top of a
    stock's ordinary volatility (~1%/day here), so the ``none`` bucket cannot
    realise zero. A well-calibrated *upper* bucket is the informative part.
    """
    if df.empty or horizon not in df.columns or field not in df.columns:
        return pd.DataFrame(), {}
    d = df.dropna(subset=[horizon, field]).copy()
    if d.empty:
        return pd.DataFrame(), {}
    d["abs_ret"] = d[horizon].abs()
    table = (
        d.groupby(field, observed=True)
        .agg(
            n_rows=("abs_ret", "size"),
            predicted_bps=(field, lambda s: round(float(s.iloc[0]) * 10000, 1)),
            realised_bps=("abs_ret", lambda s: round(float(s.mean()) * 10000, 1)),
            realised_median_bps=("abs_ret", lambda s: round(float(s.median()) * 10000, 1)),
        )
        .reset_index(drop=True)
    )
    table["ratio"] = [
        None if not p else round(r / p, 2)
        for p, r in zip(table["predicted_bps"], table["realised_bps"])
    ]
    stats: dict[str, Any] = {"horizon": horizon, "field": field, "n_rows": int(len(d))}
    if len(table) >= 3:
        stats["monotone"] = _spearman_int(table["predicted_bps"], table["realised_bps"])
        # calibration slope over the buckets that predict a non-zero move
        nz = table[table["predicted_bps"] > 0]
        if len(nz) >= 2:
            stats["slope"] = round(
                float(
                    (nz["realised_bps"].iloc[-1] - nz["realised_bps"].iloc[0])
                    / (nz["predicted_bps"].iloc[-1] - nz["predicted_bps"].iloc[0])
                ),
                3,
            )
            stats["top_bucket_ratio"] = table["ratio"].iloc[-1]
    return table, stats


def intensity(
    df: pd.DataFrame, *, horizon: str = "f1_ex", buckets: tuple[int, ...] = (1, 2, 3, 5)
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Mean ``|return|`` by how many articles the name drew that day.

    News intensity needs no model at all -- it is a free baseline that any scored
    feature has to beat to justify its cost. Reported with the same day-clustered,
    overlap-corrected statistic as everything else.
    """
    if df.empty or horizon not in df.columns:
        return pd.DataFrame(), {}
    d = df.dropna(subset=[horizon]).copy()
    edges = [0, *buckets, 10**9]
    labels = [f"{a + 1}" if a + 1 == b else f"{a + 1}-{b}" for a, b in zip(edges, edges[1:])]
    labels[-1] = f"{edges[-2] + 1}+"
    d["n_bucket"] = pd.cut(
        d["n_articles"], bins=edges, labels=labels, include_lowest=False
    )
    table = (
        d.groupby("n_bucket", observed=True)
        .agg(
            n_rows=(horizon, "size"),
            mean_abs_bps=(horizon, lambda s: round(float(s.abs().mean()) * 10000, 1)),
            mean_bps=(horizon, lambda s: round(float(s.mean()) * 10000, 1)),
        )
        .reset_index()
    )
    stats: dict[str, Any] = {"horizon": horizon, "n_rows": int(len(d))}
    if len(table) >= 3:
        # Measured with exactly the same statistic as the model fields, so the
        # free baseline and the paid one are compared like for like.
        stats.update(_daily_rank_corr(d, "n_articles", horizon))
        stats["monotone"] = _spearman_int(
            pd.Series(range(len(table))), table["mean_abs_bps"]
        )
        stats["spread_bps"] = round(
            float(table["mean_abs_bps"].iloc[-1] - table["mean_abs_bps"].iloc[0]), 1
        )
        d["abs_ret"] = d[horizon].abs()
        lo, hi = table["n_bucket"].iloc[0], table["n_bucket"].iloc[-1]
        per_day = (
            d[d["n_bucket"].isin([lo, hi])]
            .groupby(["date", "n_bucket"], observed=True)["abs_ret"]
            .mean()
            .unstack("n_bucket")
        )
        if hi in per_day.columns and lo in per_day.columns:
            ext = _t_stat(per_day[hi] - per_day[lo], lags=_lags_for(horizon))
            stats.update({f"ext_{k}": v for k, v in ext.items()})
    return table, stats
