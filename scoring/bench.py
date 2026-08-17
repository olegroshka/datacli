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
   descriptive) and ``r1 = close(T+1)/close(T)-1`` (strictly forward). A good
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
against ``r0_pos_base_rate`` as ``sent_edge_pp``.

Sample and outputs live under ``<data-root>/news/bench/<run>/`` so the real
score sidecars are never touched.
"""

from __future__ import annotations

import json
import logging
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
def price_reactions(con: Any, items: list[Item]) -> pd.DataFrame:
    """``(symbol, pub_date) -> r0, r1`` for every sampled (article, symbol)."""
    rows = [
        {"article_id": it.article_id, "symbol": s, "pub_date": str(it.date)}
        for it in items
        for s in it.target_symbols
    ]
    if not rows:
        return pd.DataFrame(columns=["article_id", "symbol", "r0", "r1"])
    pairs = pd.DataFrame(rows)
    pairs["ticker"] = pairs["symbol"].str.rsplit(".", n=1).str[0].str.upper()
    pairs["exchange"] = pairs["symbol"].str.rsplit(".", n=1).str[-1].str.upper()
    con.register("_bench_pairs", pairs)
    out = con.execute("""
        WITH px AS (
          SELECT upper(ticker) AS ticker, upper(exchange) AS exchange,
                 cast(date AS DATE) AS d, adjusted_close AS c,
                 lag(adjusted_close) OVER w AS c_prev,
                 lead(adjusted_close) OVER w AS c_next
          FROM prices WHERE adjusted_close IS NOT NULL
          WINDOW w AS (PARTITION BY upper(ticker), upper(exchange) ORDER BY cast(date AS DATE))
        )
        SELECT p.article_id, p.symbol,
               px.c / px.c_prev - 1 AS r0,
               px.c_next / px.c - 1 AS r1
        FROM _bench_pairs p
        JOIN px ON px.ticker = p.ticker AND px.exchange = p.exchange
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
            base = float((subj["r0"] > 0).mean())
            m["r0_pos_base_rate"] = round(base, 3)
            if len(committed) >= 30:
                hit = float(
                    (
                        (committed["sentiment"].astype(float) > 0)
                        == (committed["r0"] > 0)
                    ).mean()
                )
                m["sent_hit_rate"] = round(hit, 3)
                m["sent_edge_pp"] = round((hit - max(base, 1 - base)) * 100, 1)
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
