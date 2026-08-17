"""``score bench`` -- compare (model x schema) configurations on one fixed sample.

The problem this solves: before this existed we picked a model by inheritance
(the lab's SQL tier, a *coder* model) and judged a schema by eyeballing its
output distribution. Neither is a measurement. ``bench`` scores the **same**
articles with every configuration and scores the configurations themselves on
four axes that need no hand-labelled ground truth:

1. **validity** -- invalid share, errors, seconds per article. A config that
   cannot produce a valid record is disqualified whatever else it does.
2. **calibration** -- does it use the vocabulary and the scales, or collapse
   onto one value? (``other`` share, distinct values used, modal share,
   materiality base rate, ``horizon=n_a`` share).
3. **signal** -- do the labels sort *realised returns*? For each scored symbol
   we take the first trading day ``T >=`` publication and measure
   ``r0 = close(T)/close(T-1)-1`` (the session the news lands in, largely
   descriptive) and ``r1 = close(T+1)/close(T)-1`` (strictly forward), each also
   with the exchange's median move subtracted (``r0_ex``, ``r1_ex``) so a stock
   that merely drifted with the tape does not score as a correct call. A good
   sentiment orders r0 monotonically; a good materiality orders ``|r1|``.
4. **head-to-head** -- because every config sees identical articles, we can pair
   them: how often do two configs agree, and *on the articles where both commit
   to opposite directions*, whose sentiment sign matches the realised move? A
   direct accuracy proxy with no gold set.

Two traps this module exists to avoid, both found the hard way. **Abstention**: a
config answering "neutral" on half the corpus looks excellent on return spread
because it only commits on obvious news, and a naive head-to-head scores its
silence as defeat (llama3.1-8b: 54% neutral, "lost" 2-47). So ``neutral_share``
and ``sent_coverage`` sit next to every signal number and abstentions never
count as losses. **Base rate**: most articles precede a positive move, so
"always positive" scores well by accident -- hence ``sent_hit_rate`` is reported
against the majority-direction rate *on the rows the config committed to*
(``r0_base_committed``) as ``sent_edge_pp``. ``r0_pos_base_rate`` describes the
whole sample and is not the null: scoring a config's chosen rows against a base
rate drawn from rows it declined would reward selective abstention.

Sample and outputs live under ``<data-root>/news/bench/<run>/`` so the real
score sidecars are never touched.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from scoring import select as sel
from scoring import store
from scoring.backends import get_backend
from scoring.backends.base import Item
from scoring.schema import Schema, load_schema

log = logging.getLogger("scoring.bench")

#: Sentiment buckets used for the ordering metric.
SENT_BUCKETS = [-1.01, -0.45, -0.15, 0.15, 0.45, 1.01]
SENT_LABELS = ["strong_neg", "mild_neg", "neutral", "mild_pos", "strong_pos"]
#: |sentiment| at or below this counts as an abstention, not a direction call.
NEUTRAL_BAND = 0.05


@dataclass
class Config:
    """One thing to benchmark: a model id/tier plus a schema spec."""

    model: str
    schema_spec: str = "event"
    label: str | None = None

    @property
    def id(self) -> str:
        if self.label:
            return self.label
        model = self.model.split("/")[-1].replace(":", "_")
        return f"{model}__{self.schema_spec.replace('@', 'v')}"


@dataclass
class BenchResult:
    config: Config
    frame: pd.DataFrame  # article-level + per-symbol rows, as written by store
    seconds: float
    metrics: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# sample
# --------------------------------------------------------------------------- #
def build_sample(
    con: Any,
    *,
    n: int,
    since: str,
    until: str,
    max_symbols: int = 3,
    seed: int = 7,
    require_prices: bool = True,
) -> list[Item]:
    """A fixed, seeded article sample every config will score.

    Only articles with at least one target symbol are kept (per-symbol fields
    are half of what we are judging), and -- when ``require_prices`` -- only
    those whose symbol has a price bar on or after the publication day, so the
    signal metric is computable for every row.
    """
    sel.install_universe(con, sel.universe_symbols(con))
    days = sel.days_with_articles(con, since, until)
    if not days:
        return []
    per_day = max(1, int(n / max(len(days), 1)) + 1)
    items: list[Item] = []
    for day in days:
        got = sel.select_day(
            con,
            day,
            use_universe=True,
            max_symbols=max_symbols,
            sample=per_day,
            seed=seed,
        )
        items.extend(it for it in got if it.target_symbols)
    items.sort(key=lambda it: it.article_id)
    if require_prices:
        priced = _symbols_with_prices(con)
        items = [it for it in items if any(s in priced for s in it.target_symbols)]
    return items[:n]


def _symbols_with_prices(con: Any) -> set[str]:
    if not sel.has_view(con, "prices"):
        return set()
    rows = con.execute(
        "SELECT DISTINCT upper(ticker) || '.' || upper(exchange) FROM prices_state"
    ).fetchall()
    return {r[0] for r in rows}


def sample_frame(items: list[Item]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "article_id": [it.article_id for it in items],
            "date": [it.date for it in items],
            "symbols": [list(it.target_symbols) for it in items],
            "chars": [len(it.content or "") for it in items],
            "vendor_polarity": [it.vendor_polarity for it in items],
        }
    )


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def run_config(
    items: list[Item],
    config: Config,
    *,
    cfg: Any,
    chunk: int = 25,
    on_progress: Any = None,
) -> BenchResult:
    """Score the sample with one configuration."""
    schema = load_schema(config.schema_spec)
    backend = get_backend("llm", config=cfg, model=config.model)
    say = on_progress or (lambda m: log.info(m))
    t0 = time.perf_counter()
    results = []
    for start in range(0, len(items), chunk):
        batch = items[start : start + chunk]
        results.extend(backend.score(batch, schema))
        done = start + len(batch)
        say(
            f"{config.id}: {done}/{len(items)}  "
            f"ok={sum(1 for r in results if r.status == 'ok')} "
            f"invalid={sum(1 for r in results if r.status == 'invalid')} "
            f"err={sum(1 for r in results if r.status == 'error')}  "
            f"{(time.perf_counter() - t0) / max(done, 1):.1f}s/item"
        )
    elapsed = time.perf_counter() - t0
    frame = store.results_to_frame(results, schema, backend.id, store.now_iso())
    frame["config"] = config.id
    frame["model"] = backend.model
    frame["schema_spec"] = schema.key
    return BenchResult(config=config, frame=frame, seconds=elapsed)


# --------------------------------------------------------------------------- #
# returns
# --------------------------------------------------------------------------- #
#: Minimum stocks behind an exchange's daily mean before it is used as a market
#: proxy. A "market" of three names is noise, and subtracting it would add more
#: variance than the beta it removes.
MIN_MARKET_MEMBERS = 20


def price_reactions(con: Any, items: list[Item]) -> pd.DataFrame:
    """``(symbol, pub_date) -> r0, r1, r0_ex, r1_ex`` per sampled (article, symbol).

    ``r0`` spans the session the article lands in, ``r1`` the one after. The
    ``_ex`` columns subtract the median move of every stock on the same
    exchange over the same interval, leaving the stock-specific part. Company news
    is by nature stock-specific, so the market component is noise here: without
    the adjustment a stock that merely drifted up with the tape counts as a
    correct call. Left NULL when fewer than ``MIN_MARKET_MEMBERS`` stocks stand
    behind the mean.
    """
    rows = [
        {"article_id": it.article_id, "symbol": s, "pub_date": str(it.date)}
        for it in items
        for s in it.target_symbols
    ]
    cols = ["article_id", "symbol", "r0", "r1", "r0_ex", "r1_ex"]
    if not rows:
        return pd.DataFrame(columns=cols)
    pairs = pd.DataFrame(rows)
    pairs["ticker"] = pairs["symbol"].str.rsplit(".", n=1).str[0].str.upper()
    pairs["exchange"] = pairs["symbol"].str.rsplit(".", n=1).str[-1].str.upper()
    con.register("_bench_pairs", pairs)
    out = con.execute(f"""
        WITH bounds AS (
          SELECT cast(min(pub_date) AS DATE) - 10 AS lo,
                 cast(max(pub_date) AS DATE) + 10 AS hi FROM _bench_pairs
        ), px AS (
          SELECT upper(ticker) AS ticker, upper(exchange) AS exchange,
                 cast(date AS DATE) AS d, adjusted_close AS c,
                 lag(adjusted_close) OVER w AS c_prev,
                 lead(adjusted_close) OVER w AS c_next
          FROM prices
          WHERE adjusted_close IS NOT NULL
            AND cast(date AS DATE) BETWEEN (SELECT lo FROM bounds)
                                       AND (SELECT hi FROM bounds)
            AND upper(exchange) IN (SELECT DISTINCT exchange FROM _bench_pairs)
          WINDOW w AS (PARTITION BY upper(ticker), upper(exchange) ORDER BY cast(date AS DATE))
        ), mkt AS (
          -- Market proxy per exchange-session, over the same intervals as r0
          -- and r1 so the subtraction is like for like. The *median*, not the
          -- mean: the price store carries extreme bad ticks (daily returns up to
          -- +24,500% on US and +29,900% on INDX -- unadjusted splits and
          -- delisted stubs), which wreck an equal-weight mean. Measured over the
          -- sample window the per-day mean has sd 0.9-3.9% with worst-day values
          -- of 3-16%, while the median has sd 0.19-0.40%. Subtracting the mean
          -- *added* variance; the median removes it.
          SELECT exchange, d, count(*) AS n_mkt,
                 median(c / c_prev - 1) AS m0, median(c_next / c - 1) AS m1
          FROM px
          WHERE c_prev IS NOT NULL AND c_next IS NOT NULL AND c_prev > 0 AND c > 0
          GROUP BY exchange, d
        )
        SELECT p.article_id, p.symbol,
               px.c / px.c_prev - 1 AS r0,
               px.c_next / px.c - 1 AS r1,
               CASE WHEN mkt.n_mkt >= {MIN_MARKET_MEMBERS}
                    THEN (px.c / px.c_prev - 1) - mkt.m0 END AS r0_ex,
               CASE WHEN mkt.n_mkt >= {MIN_MARKET_MEMBERS}
                    THEN (px.c_next / px.c - 1) - mkt.m1 END AS r1_ex
        FROM _bench_pairs p
        JOIN px ON px.ticker = p.ticker AND px.exchange = p.exchange
        LEFT JOIN mkt ON mkt.exchange = px.exchange AND mkt.d = px.d
        WHERE px.d = (SELECT min(x.d) FROM px x
                      WHERE x.ticker = p.ticker AND x.exchange = p.exchange
                        AND x.d >= cast(p.pub_date AS DATE))
          AND px.c_prev IS NOT NULL AND px.c_next IS NOT NULL
        """).df()
    con.unregister("_bench_pairs")
    return out


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
def _spearman(a: pd.Series, b: pd.Series) -> float:
    if len(a) < 3:
        return 0.0
    return round(float(a.rank().corr(b.rank())), 3)


def config_metrics(
    frame: pd.DataFrame, reactions: pd.DataFrame, seconds: float
) -> dict[str, Any]:
    """Validity, calibration and signal for one config's output."""
    art = frame[frame["symbol"].isna()]
    ok = art[art["status"] == "ok"]
    n = max(len(art), 1)
    m: dict[str, Any] = {
        "n_articles": int(len(art)),
        "ok_share": round(float((art["status"] == "ok").mean()), 3),
        "invalid_share": round(float((art["status"] == "invalid").mean()), 3),
        "error_share": round(float((art["status"] == "error").mean()), 3),
        "s_per_item": round(seconds / n, 2),
    }
    if ok.empty:
        return m
    # calibration
    m["other_share"] = round(float((ok["event_type"] == "other").mean()), 3)
    m["n_event_classes"] = int(ok["event_type"].nunique())
    sent = ok["sentiment"].astype(float)
    m["sent_distinct"] = int(sent.nunique())
    m["sent_modal_share"] = round(float(sent.value_counts(normalize=True).iloc[0]), 3)
    m["sent_sd"] = round(float(sent.std()), 3)
    # Abstention rate. A config answering "neutral" on half the corpus is not
    # comparable to one that commits: its return spread is measured on an easier
    # subset. Read every signal number next to this and `sent_coverage`.
    m["neutral_share"] = round(float((sent.abs() <= NEUTRAL_BAND).mean()), 3)
    mat = ok["materiality"].astype(float)
    m["mat_modal_share"] = round(float(mat.value_counts(normalize=True).iloc[0]), 3)
    m["mat_low_share"] = round(float((mat <= 1).mean()), 3)
    if "horizon" in ok.columns:
        # v2 calls the no-signal bucket "n_a", v3 calls it "unclear"
        m["horizon_na_share"] = round(
            float(ok["horizon"].isin(["n_a", "unclear"]).mean()), 3
        )
    # signal: join per-symbol rows (subject role) to returns
    sym = frame[frame["symbol"].notna() & (frame["status"] == "ok")]
    if not sym.empty and not reactions.empty:
        art_vals = ok.set_index("article_id")[["sentiment", "materiality"]]
        j = sym.merge(reactions, on=["article_id", "symbol"], how="inner")
        j = j.join(art_vals, on="article_id", rsuffix="_art")
        subj = j[j["role"] == "subject"].dropna(subset=["r0", "r1", "sentiment"])
        m["n_signal_rows"] = int(len(subj))
        if len(subj) >= 30:
            subj = subj.copy()
            subj["bucket"] = pd.cut(
                subj["sentiment"].astype(float), bins=SENT_BUCKETS, labels=SENT_LABELS
            )
            g = subj.groupby("bucket", observed=True)["r0"].mean() * 10000
            if len(g) >= 3:
                vals = g.reset_index(drop=True)
                m["sent_r0_monotone"] = _spearman(pd.Series(range(len(vals))), vals)
                m["sent_r0_spread_bps"] = round(float(g.iloc[-1] - g.iloc[0]))
            # Directional hit rate among the rows where it commits, against the
            # base rate of a positive move: a config that always says "positive"
            # scores the base rate, which is not skill. sent_edge_pp is the gap.
            committed = subj[subj["sentiment"].astype(float).abs() > NEUTRAL_BAND]
            m["sent_coverage"] = round(float(len(committed) / max(len(subj), 1)), 3)
            m["r0_pos_base_rate"] = round(float((subj["r0"] > 0).mean()), 3)
            m["r1_pos_base_rate_all"] = round(float((subj["r1"] > 0).mean()), 3)
            if len(committed) >= 30:
                # Four horizons, measured identically. r0 is the session the
                # article lands in and is contaminated -- an article reporting
                # "shares fell 8%" makes the direction trivially inferable, so a
                # high r0 edge can be hindsight. r1 is strictly after publication,
                # the horizon that would matter for prediction. The *_ex variants
                # subtract the same-session mean move of the stock's exchange:
                # news is stock-specific, so market beta is noise in this test,
                # and a stock that merely rose with the tape should not score as a
                # hit. `_ex` is therefore the sharpest read available here.
                for horizon, prefix in (
                    ("r0", "sent_edge"),
                    ("r1", "sent_edge_r1"),
                    ("r0_ex", "sent_edge_r0ex"),
                    ("r1_ex", "sent_edge_r1ex"),
                ):
                    if horizon not in committed.columns:
                        continue
                    cm = committed.dropna(subset=[horizon])
                    if len(cm) < 30:
                        continue
                    # The null is evaluated on the *same rows the config committed
                    # to*: a config that abstains selectively (say, on days the
                    # market fell) would otherwise be scored against a base rate
                    # drawn from a population it declined. On 803 rows this moved
                    # the answer ~1.5pp in opposite directions per horizon.
                    base = float((cm[horizon] > 0).mean())
                    null = max(base, 1.0 - base)
                    hit = float(
                        (
                            (cm["sentiment"].astype(float) > 0) == (cm[horizon] > 0)
                        ).mean()
                    )
                    st = _edge_stats(hit, null, len(cm))
                    m[f"{horizon}_base_committed"] = round(base, 3)
                    m[f"{prefix}_hit"] = round(hit, 3)
                    m[f"{prefix}_pp"] = round((hit - null) * 100, 1)
                    m[f"{prefix}_n"] = int(len(cm))
                    m[f"{prefix}_z"] = st.get("edge_z")
                    m[f"{prefix}_p"] = st.get("edge_p")
                    if horizon == "r0":  # preserve the original metric names
                        m["sent_hit_rate"] = round(hit, 3)
                        m.update(_edge_stats(hit, null, len(cm)))
                    elif horizon == "r1":
                        m["sent_hit_rate_r1"] = round(hit, 3)
            up = subj[subj["direction"] == "up"]["r0"]
            dn = subj[subj["direction"] == "down"]["r0"]
            if len(up) >= 10 and len(dn) >= 10:
                m["dir_r0_spread_bps"] = round(float((up.mean() - dn.mean()) * 10000))
            mg = subj.groupby(subj["materiality"].astype(int), observed=True)[
                "r1"
            ].apply(lambda s: s.abs().mean() * 10000)
            if len(mg) >= 3:
                m["mat_absr1_monotone"] = _spearman(
                    pd.Series(mg.index, index=mg.index), mg
                )
                m["mat_absr1_spread_bps"] = round(float(mg.iloc[-1] - mg.iloc[0]))
    return m


def _two_sided_p(z: float) -> float:
    """Two-sided p-value for a standard-normal z. ``erfc`` makes this exact for
    the normal, so no scipy dependency is needed for the sample sizes here."""
    return float(math.erfc(abs(z) / math.sqrt(2.0)))


def _edge_stats(hit: float, null: float, n: int) -> dict[str, Any]:
    """Is a directional hit rate distinguishable from the trivial strategy?

    ``null`` is the rate a config gets for free by always calling the majority
    direction, so the question is not "is hit > 0.5" but "is hit > null". Returns
    the standard error in percentage points alongside z and p, because an edge
    quoted without its SE invites reading noise as a result -- at n=220 the SE is
    ~3.4pp, which is most of the spread we saw while screening.
    """
    if n <= 0 or not 0.0 < null < 1.0:
        return {}
    se = math.sqrt(null * (1.0 - null) / n)
    z = (hit - null) / se if se > 0 else 0.0
    return {
        "n_committed": int(n),
        "edge_se_pp": round(se * 100, 2),
        "edge_z": round(z, 2),
        "edge_p": round(_two_sided_p(z), 4),
    }


def paired_sign_test(
    a: pd.DataFrame, b: pd.DataFrame, reactions: pd.DataFrame, horizon: str = "r1"
) -> dict[str, Any]:
    """McNemar test of two configs' directional calls on the same articles.

    Both configs saw the same articles, so the comparison should be paired. Rows
    where they agree on the sign are concordant -- both right or both wrong -- and
    carry no information about which is better; only the rows where exactly one is
    right do, which is precisely McNemar's discordant set.

    The practical catch, and the reason this is worth reporting explicitly: if two
    configs agree on sign on ~97% of articles, the discordant set is tiny and *no*
    sample size available here can separate their directional skill. That is a
    finding about model choice rather than a defect of the test -- when
    ``sign_agree`` is high, pick on coverage, validity and cost instead.
    """
    aa = a[(a["symbol"].isna()) & (a["status"] == "ok")].set_index("article_id")
    bb = b[(b["symbol"].isna()) & (b["status"] == "ok")].set_index("article_id")
    both = aa.join(bb, how="inner", lsuffix="_a", rsuffix="_b")
    out: dict[str, Any] = {"horizon": horizon, "n_both": int(len(both))}
    if both.empty or reactions.empty or horizon not in reactions.columns:
        return out
    r = reactions.groupby("article_id")[horizon].mean()
    cmp_ = both.join(r, how="inner").dropna(subset=[horizon])

    def _sign(x: float) -> int:
        return 1 if x > NEUTRAL_BAND else (-1 if x < -NEUTRAL_BAND else 0)

    sign_a = cmp_["sentiment_a"].astype(float).apply(_sign)
    sign_b = cmp_["sentiment_b"].astype(float).apply(_sign)
    real = cmp_[horizon].apply(lambda x: 1 if x > 0 else -1)
    # both must commit for the pair to be informative
    paired = (sign_a != 0) & (sign_b != 0)
    out["n_both_committed"] = int(paired.sum())
    if paired.sum() < 20:
        return out
    out["sign_agree"] = round(float((sign_a == sign_b)[paired].mean()), 3)
    b_only = int(((sign_a == real) & (sign_b != real) & paired).sum())
    c_only = int(((sign_b == real) & (sign_a != real) & paired).sum())
    out["a_only_right"] = b_only
    out["b_only_right"] = c_only
    out["n_discordant"] = b_only + c_only
    if b_only + c_only >= 10:
        z = (b_only - c_only) / math.sqrt(b_only + c_only)
        out["mcnemar_z"] = round(z, 2)
        out["mcnemar_p"] = round(_two_sided_p(z), 4)
    return out


def head_to_head(
    a: pd.DataFrame, b: pd.DataFrame, reactions: pd.DataFrame
) -> dict[str, Any]:
    """Paired comparison of two configs on the articles both scored ok.

    ``sign_win_rate`` looks only at articles where the two disagree on the sign
    of sentiment and asks whose sign matches the realised ``r0`` -- a direct
    accuracy proxy that needs no labels.
    """
    aa = a[(a["symbol"].isna()) & (a["status"] == "ok")].set_index("article_id")
    bb = b[(b["symbol"].isna()) & (b["status"] == "ok")].set_index("article_id")
    both = aa.join(bb, how="inner", lsuffix="_a", rsuffix="_b")
    out: dict[str, Any] = {"n_both": int(len(both))}
    if both.empty:
        return out
    out["event_agree"] = round(
        float((both["event_type_a"] == both["event_type_b"]).mean()), 3
    )
    sa, sb = both["sentiment_a"].astype(float), both["sentiment_b"].astype(float)
    out["sent_corr"] = round(float(sa.corr(sb)), 3)
    out["sent_mae"] = round(float((sa - sb).abs().mean()), 3)
    out["mat_agree"] = round(
        float((both["materiality_a"] == both["materiality_b"]).mean()), 3
    )
    # disagreement head-to-head against realised r0 (mean over the article's symbols)
    if not reactions.empty:
        r = reactions.groupby("article_id")["r0"].mean()
        cmp_ = both.join(r, how="inner")

        def _sign(x: float) -> int:
            return 1 if x > NEUTRAL_BAND else (-1 if x < -NEUTRAL_BAND else 0)

        sign_a = cmp_["sentiment_a"].astype(float).apply(_sign)
        sign_b = cmp_["sentiment_b"].astype(float).apply(_sign)
        real = cmp_["r0"].apply(lambda x: 1 if x > 0 else -1)
        out["neutral_a"] = round(float((sign_a == 0).mean()), 3)
        out["neutral_b"] = round(float((sign_b == 0).mean()), 3)
        out["n_disagree"] = int((sign_a != sign_b).sum())
        # Only *committed* disagreements decide the head-to-head: if one side
        # abstains it is not wrong, it is silent, and counting that as a loss
        # merely measures which config abstains more (llama3.1-8b answered
        # "neutral" on 54% of a 259-article sample and "lost" 2-47 that way).
        committed = (sign_a != sign_b) & (sign_a != 0) & (sign_b != 0)
        out["n_committed_disagree"] = int(committed.sum())
        if committed.sum() >= 20:
            a_right = int(((sign_a == real) & committed).sum())
            b_right = int(((sign_b == real) & committed).sum())
            total = a_right + b_right
            out["a_wins"] = a_right
            out["b_wins"] = b_right
            out["a_win_rate"] = round(float(a_right / total), 3) if total else None
    return out


def scorecard(results: list[BenchResult]) -> pd.DataFrame:
    """One row per config, the columns that decide the choice."""
    rows = []
    for r in results:
        rows.append(
            {
                "config": r.config.id,
                "model": r.config.model,
                "schema": r.config.schema_spec,
                **r.metrics,
            }
        )
    df = pd.DataFrame(rows)
    return df
