"""``score eval`` -- sanity and agreement checks over the score sidecars.

Reads the DuckDB views (``news_scores_<schema>`` joined to ``news``) and answers:

- **health**: how many articles scored, invalid/error share, seconds per item,
  by backend;
- **vs vendor**: our ``sentiment`` against EODHD's ``polarity`` for the same
  article -- Pearson / Spearman, and a 3x3 sign agreement table (neg / neutral /
  pos with a +-0.05 dead band). The vendor score is a saturated VADER-style
  baseline, so *low* agreement is not automatically bad -- the table shows where
  the two disagree;
- **distributions**: event_type, horizon, materiality, novelty, per-symbol
  role/direction -- the quickest way to see a model that collapses onto one
  value;
- **backend vs backend** (``--compare A B``): the same article scored by two
  backends -- sentiment correlation, event_type / direction agreement (raw and
  Cohen's kappa), materiality MAE.

Pure functions take DataFrames; the CLI wraps them with the connection.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

DEAD_BAND = 0.05
NUMERIC_FIELDS = ("sentiment", "confidence", "materiality")
CATEGORICAL_FIELDS = ("event_type", "horizon", "novelty")
SYMBOL_FIELDS = ("role", "direction")


def _sign(x: pd.Series, band: float = DEAD_BAND) -> pd.Series:
    return pd.cut(
        x.astype(float),
        bins=[-float("inf"), -band, band, float("inf")],
        labels=["neg", "neutral", "pos"],
    ).astype(str)


# --------------------------------------------------------------------------- #
# loaders (SQL kept here so the pure functions below stay testable)
# --------------------------------------------------------------------------- #
def load_scores(con: Any, view: str, *, backend: str | None = None) -> pd.DataFrame:
    """Article-level rows (symbol IS NULL) of one score view, joined to the
    vendor polarity from ``news``."""
    where = "s.symbol IS NULL"
    params: list[Any] = []
    if backend:
        where += " AND s.backend = ?"
        params.append(backend)
    return con.execute(
        f"""
        SELECT s.article_id, cast(s.date AS VARCHAR) AS date, s.backend, s.model, s.status,
               s.seconds, s.cached, s.prompt_tokens, s.completion_tokens, s.cost_usd,
               s.event_type, s.summary, s.sentiment, s.confidence, s.materiality,
               s.novelty, s.horizon,
               n.polarity AS vendor_polarity, n.source
        FROM {view} s
        LEFT JOIN (
            SELECT article_id, any_value(polarity) AS polarity, any_value(source) AS source
            FROM news GROUP BY article_id
        ) n USING (article_id)
        WHERE {where}
        """,
        params,
    ).df()


def load_symbol_scores(
    con: Any, view: str, *, backend: str | None = None
) -> pd.DataFrame:
    where = "s.symbol IS NOT NULL"
    params: list[Any] = []
    if backend:
        where += " AND s.backend = ?"
        params.append(backend)
    return con.execute(
        f"SELECT s.article_id, s.symbol, s.backend, s.role, s.direction, s.relevance, "
        f"s.sentiment FROM {view} s WHERE {where}",
        params,
    ).df()


# --------------------------------------------------------------------------- #
# pure metrics
# --------------------------------------------------------------------------- #
def health(df: pd.DataFrame) -> pd.DataFrame:
    """Per-backend counts, invalid/error share, s/item, cached share."""
    if df.empty:
        return pd.DataFrame()
    g = df.groupby("backend")
    out = pd.DataFrame(
        {
            "articles": g.size(),
            "ok": g["status"].apply(lambda s: int((s == "ok").sum())),
            "invalid": g["status"].apply(lambda s: int((s == "invalid").sum())),
            "error": g["status"].apply(lambda s: int((s == "error").sum())),
            "s_per_item": g["seconds"].mean().round(2),
            "tokens_in": g["prompt_tokens"].mean().round(0).astype(int),
            "days": g["date"].nunique(),
            "last_day": g["date"].max(),
        }
    )
    out["invalid_share"] = (out["invalid"] / out["articles"]).round(3)
    return out.reset_index()


def vs_vendor(df: pd.DataFrame) -> dict[str, Any]:
    """Our sentiment vs the vendor polarity on the same articles."""
    ok = df[
        (df["status"] == "ok") & df["sentiment"].notna() & df["vendor_polarity"].notna()
    ]
    n = int(len(ok))
    if n < 3:
        return {"n": n}
    ours = ok["sentiment"].astype(float)
    theirs = ok["vendor_polarity"].astype(float)
    table = pd.crosstab(_sign(ours), _sign(theirs), dropna=False)
    table = table.reindex(
        index=["neg", "neutral", "pos"], columns=["neg", "neutral", "pos"], fill_value=0
    )
    agree = int(sum(table.loc[k, k] for k in ("neg", "neutral", "pos")))
    return {
        "n": n,
        "pearson": round(float(ours.corr(theirs)), 3),
        "spearman": _spearman(ours, theirs),
        "sign_agreement": round(agree / n, 3),
        "sign_table": table,  # rows = ours, cols = vendor
        "ours": _describe(ours),
        "vendor": _describe(theirs),
    }


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """Spearman rho as Pearson on ranks (no scipy dependency)."""
    return round(float(a.rank().corr(b.rank())), 3)


def _describe(s: pd.Series) -> dict[str, float]:
    q = s.quantile([0.1, 0.5, 0.9])
    return {
        "mean": round(float(s.mean()), 3),
        "std": round(float(s.std()), 3),
        "p10": round(float(q.iloc[0]), 3),
        "p50": round(float(q.iloc[1]), 3),
        "p90": round(float(q.iloc[2]), 3),
        "share_pos": round(float((s > DEAD_BAND).mean()), 3),
        "share_neg": round(float((s < -DEAD_BAND).mean()), 3),
    }


def distributions(
    df: pd.DataFrame, symbols: pd.DataFrame | None = None
) -> dict[str, pd.DataFrame]:
    """Value counts (share) for the categorical fields; mean sentiment per event_type."""
    out: dict[str, pd.DataFrame] = {}
    ok = df[df["status"] == "ok"]
    if ok.empty:
        return out
    for col in CATEGORICAL_FIELDS:
        vc = ok[col].astype(str).value_counts(dropna=False)
        out[col] = pd.DataFrame(
            {"n": vc, "share": (vc / vc.sum()).round(3)}
        ).reset_index(names=col)
    out["materiality"] = (
        ok["materiality"]
        .value_counts()
        .sort_index()
        .rename_axis("materiality")
        .reset_index(name="n")
    )
    out["sentiment_by_event_type"] = (
        ok.groupby("event_type")["sentiment"]
        .agg(["count", "mean", "std"])
        .round(3)
        .sort_values("count", ascending=False)
        .reset_index()
    )
    if symbols is not None and not symbols.empty:
        for col in SYMBOL_FIELDS:
            vc = symbols[col].astype(str).value_counts(dropna=False)
            out[f"symbol_{col}"] = pd.DataFrame(
                {"n": vc, "share": (vc / vc.sum()).round(3)}
            ).reset_index(names=col)
    return out


def compare(
    a: pd.DataFrame,
    b: pd.DataFrame,
    sa: pd.DataFrame | None = None,
    sb: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Two backends on the same articles (join on article_id; symbols on article_id+symbol)."""
    ja = a[a["status"] == "ok"].set_index("article_id")
    jb = b[b["status"] == "ok"].set_index("article_id")
    both = ja.join(jb, how="inner", lsuffix="_a", rsuffix="_b")
    out: dict[str, Any] = {"n": int(len(both))}
    if len(both) >= 3:
        out["sentiment_pearson"] = round(
            float(
                both["sentiment_a"]
                .astype(float)
                .corr(both["sentiment_b"].astype(float))
            ),
            3,
        )
        out["sentiment_spearman"] = _spearman(
            both["sentiment_a"].astype(float), both["sentiment_b"].astype(float)
        )
        out["sentiment_sign_agreement"] = round(
            float((_sign(both["sentiment_a"]) == _sign(both["sentiment_b"])).mean()), 3
        )
        for col in ("event_type", "horizon"):
            out[f"{col}_agreement"] = round(
                float(
                    (
                        both[f"{col}_a"].astype(str) == both[f"{col}_b"].astype(str)
                    ).mean()
                ),
                3,
            )
            out[f"{col}_kappa"] = cohen_kappa(
                both[f"{col}_a"].astype(str), both[f"{col}_b"].astype(str)
            )
        out["materiality_mae"] = round(
            float(
                (
                    both["materiality_a"].astype(float)
                    - both["materiality_b"].astype(float)
                )
                .abs()
                .mean()
            ),
            3,
        )
    if sa is not None and sb is not None and not sa.empty and not sb.empty:
        ka = sa.set_index(["article_id", "symbol"])
        kb = sb.set_index(["article_id", "symbol"])
        sboth = ka.join(kb, how="inner", lsuffix="_a", rsuffix="_b")
        out["n_symbols"] = int(len(sboth))
        if len(sboth) >= 3:
            out["direction_agreement"] = round(
                float(
                    (
                        sboth["direction_a"].astype(str)
                        == sboth["direction_b"].astype(str)
                    ).mean()
                ),
                3,
            )
            out["direction_kappa"] = cohen_kappa(
                sboth["direction_a"].astype(str), sboth["direction_b"].astype(str)
            )
            out["role_agreement"] = round(
                float(
                    (sboth["role_a"].astype(str) == sboth["role_b"].astype(str)).mean()
                ),
                3,
            )
    return out


def cohen_kappa(x: pd.Series, y: pd.Series) -> float:
    """Cohen's kappa for two categorical series of equal length."""
    n = len(x)
    if n == 0:
        return 0.0
    po = float((x.values == y.values).mean())
    px = x.value_counts(normalize=True)
    py = y.value_counts(normalize=True)
    pe = float(
        sum(px.get(k, 0.0) * py.get(k, 0.0) for k in set(px.index) | set(py.index))
    )
    if pe >= 1.0:
        return 1.0
    return round((po - pe) / (1.0 - pe), 3)
