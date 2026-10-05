"""Nowcasting the register from public ripples (DD-004, iteration 1; EVAL-006).

At the close of day ``d`` the regulator has not yet published the positions
dated ``d`` (they appear the next weekday). This module asks how much of
what will be published can be read from public daily data of ``d`` and
before, with the per-holder registers of WP18 as the truth:

- **state** (known by the close of ``d``): the issuer panel's row at ``d``
  (the panel is indexed by the publication day, so its row at ``d`` is the
  state published by ``d``): the disclosed level, the number of holders,
  the 21-day visible flow, the FIFO age and profit, the days since the last
  published change, and the market;
- **ripples** (known by the close of ``d``): the lane's daily bars at ``d``:
  returns over 1, 5 and 21 days, abnormal volume (the day's volume over its
  63-day median, and the 5-day mean of it), 21-day realised volatility, the
  day's range over the close, the pressure (the 5-day return's sign times
  the abnormal volume), and the market's median return that day;
- **targets** (published after the close of ``d``): from the funds' rows
  dated ``d``: ``t1`` an entry or an increase, ``t2`` a decrease or an exit,
  ``t3`` the aggregate change of the disclosed level in percent of capital,
  and ``t4`` the sign of the aggregate change over the next five issuer-days
  (up, flat within ``FLAT_BAND``, down).

Two models per target on the same split (fit to ``FIT_TO``, validate to
``VALIDATE_TO``, test after): the **state-only** persistence baseline and
**state plus ripples**; the number the iteration exists to produce is the
*ripple increment*, the test-years metric of the second minus the first.

The boosted model is XGBoost's histogram trees on the GPU when a CUDA
device is present (``boost_backend``), sklearn's on the CPU otherwise;
same depth, learning rate and iterations either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

MARKETS: tuple[str, ...] = ("uk", "fr", "nl", "se", "de")
MARKET_CODE: dict[str, int] = {m: i for i, m in enumerate(("uk", "fr", "nl", "se", "no", "ie", "de"))}
FIT_TO = "2019-12-31"
VALIDATE_TO = "2022-12-31"
ADV_WINDOW = 63
ADV_MIN_PERIODS = 20
FLOW_WINDOW = 21
VOL_WINDOW = 21
AHEAD_DAYS = 5
FLAT_BAND = 0.01
STATE: tuple[str, ...] = ("level", "n_holders", "flow_21", "age_days", "profit_pct", "days_since_event", "market_code")
RIPPLES: tuple[str, ...] = ("ret_1", "ret_5", "ret_21", "abn_vol", "abn_vol_5", "rvol_21", "range", "pressure", "mkt_ret")
TARGETS: tuple[str, ...] = ("t1", "t2", "t3", "t4")
BINARY: tuple[str, ...] = ("t1", "t2")
FEATURE_SETS: dict[str, tuple[str, ...]] = {"state": STATE, "state+ripples": STATE + RIPPLES}
#: Boosting iterations tried on the validation years; the better one is refit on fit plus validation years.
BOOST_ITERATIONS: tuple[int, ...] = (100, 300)
BOOST_DEPTH = 4
BOOST_LEARNING_RATE = 0.05
#: The smallest leaf: rows for sklearn; XGBoost's hessian weight (one per row in regression, p(1-p) per row in classification).
BOOST_MIN_LEAF_ROWS = 200
BOOST_MIN_CHILD_WEIGHT = {"regression": 200.0, "binary": 20.0, "multiclass": 20.0}


def boost_backend() -> str:
    """``xgboost-cuda`` when XGBoost and a CUDA device are usable, ``xgboost-cpu`` when only XGBoost is, else ``sklearn``."""
    try:
        import xgboost as xgb
    except ImportError:
        return "sklearn"
    try:
        xgb.XGBRegressor(n_estimators=1, device="cuda", tree_method="hist").fit(np.zeros((4, 1)), np.zeros(4))
        return "xgboost-cuda"
    except Exception:  # noqa: BLE001 (no CUDA device or driver)
        return "xgboost-cpu"


# --------------------------------------------------------------------------- #
# features and targets (pure)
# --------------------------------------------------------------------------- #
def ripple_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Per ``(date, ticker)`` the ripple block from one market's daily bars (``mkt_ret`` is the market's median 1-day return)."""
    frame = prices.sort_values(["ticker", "date"]).reset_index(drop=True).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    by = frame.groupby("ticker", sort=False)
    adj = frame["adjusted_close"].astype(float)
    frame["ret_1"] = by["adjusted_close"].pct_change(fill_method=None)
    frame["ret_5"] = adj / by["adjusted_close"].shift(5) - 1.0
    frame["ret_21"] = adj / by["adjusted_close"].shift(21) - 1.0
    volume = frame["volume"].astype(float)
    median_volume = by["volume"].transform(lambda s: s.rolling(ADV_WINDOW, min_periods=ADV_MIN_PERIODS).median())
    frame["abn_vol"] = (volume / median_volume.where(median_volume > 0)).astype(float)
    frame["abn_vol_5"] = frame.groupby("ticker", sort=False)["abn_vol"].transform(lambda s: s.rolling(5, min_periods=3).mean())
    frame["rvol_21"] = frame.groupby("ticker", sort=False)["ret_1"].transform(lambda s: s.rolling(VOL_WINDOW, min_periods=10).std())
    close = frame["close"].astype(float)
    frame["range"] = ((frame["high"].astype(float) - frame["low"].astype(float)) / close.where(close > 0)).astype(float)
    frame["pressure"] = np.sign(frame["ret_5"]) * frame["abn_vol"]
    frame["mkt_ret"] = frame.groupby("date")["ret_1"].transform("median")
    return frame[["date", "ticker", *RIPPLES]]


def state_features(issuers: pd.DataFrame) -> pd.DataFrame:
    """Per ``(isin, date)`` the state block from the issuer panel (its row at ``d`` is what is published by ``d``)."""
    frame = issuers.sort_values(["isin", "date"]).reset_index(drop=True).copy()
    frame["date"] = pd.to_datetime(frame["date"])
    by = frame.groupby("isin", sort=False)
    frame["flow_21"] = by["flow"].transform(lambda s: s.rolling(FLOW_WINDOW, min_periods=1).sum())
    changed = (frame["flow"] != 0) | (frame["entries"] > 0) | (frame["exits"] > 0)
    last_change = frame["date"].where(changed)
    frame["_last_change"] = last_change.groupby(frame["isin"]).ffill()
    frame["days_since_event"] = (frame["date"] - frame["_last_change"]).dt.days.astype(float)
    frame["market_code"] = frame["market"].map(MARKET_CODE).astype(float)
    columns = ["isin", "date", "ticker", "market", "level", "n_holders", "flow_21", "age_days", "profit_pct", "days_since_event", "market_code"]
    return frame[columns].reset_index(drop=True)


def targets(funds: pd.DataFrame) -> pd.DataFrame:
    """Per ``(isin, position_date)`` the published change: ``t1`` entry or increase, ``t2`` decrease or exit, ``t3`` the aggregate change."""
    frame = funds.copy()
    frame["position_date"] = pd.to_datetime(frame["position_date"])
    reset = frame["reset"].astype(bool)
    flow = frame["flow"].astype(float)
    inventory = frame["inventory"].astype(float)
    frame["_up"] = ((~reset) & (flow > 0)) | (reset & (inventory > 0))
    frame["_down"] = (~reset) & ((flow < 0) | frame["below_threshold"].astype(bool))
    frame["_change"] = np.where(reset, inventory, flow)
    out = frame.groupby(["isin", "position_date"], sort=True).agg(t1=("_up", "any"), t2=("_down", "any"), t3=("_change", "sum")).reset_index()
    out["t1"] = out["t1"].astype(int)
    out["t2"] = out["t2"].astype(int)
    out["t3"] = out["t3"].astype(float)
    return out.rename(columns={"position_date": "date"})


def ahead_sign(frame: pd.DataFrame, *, days: int = AHEAD_DAYS, band: float = FLAT_BAND) -> pd.Series:
    """``t4``: the sign of the aggregate change over the next ``days`` issuer-days (0 flat, 1 up, 2 down; NaN at the end)."""
    frame = frame.sort_values(["isin", "date"])
    change = frame["t3"].astype(float)
    reverse_cumsum = change.groupby(frame["isin"]).transform(lambda s: s[::-1].cumsum()[::-1])
    ahead = reverse_cumsum.groupby(frame["isin"]).shift(-1) - reverse_cumsum.groupby(frame["isin"]).shift(-(days + 1)).fillna(0.0)
    enough = frame.groupby("isin")["date"].transform(lambda s: pd.Series(np.arange(len(s))[::-1], index=s.index)) >= days
    sign = pd.Series(np.where(ahead > band, 1.0, np.where(ahead < -band, 2.0, 0.0)), index=frame.index)
    return sign.where(enough).reindex(frame.index)


def assemble(issuers: pd.DataFrame, funds: pd.DataFrame, prices: pd.DataFrame) -> pd.DataFrame:
    """One market's issuer-days with the state at ``d``, the ripples at ``d`` and the targets dated ``d``."""
    state = state_features(issuers)
    ripples = ripple_features(prices)
    frame = state.merge(ripples, on=["date", "ticker"], how="left")
    frame = frame.merge(targets(funds), on=["isin", "date"], how="left")
    for column in ("t1", "t2"):
        frame[column] = frame[column].fillna(0).astype(int)
    frame["t3"] = frame["t3"].fillna(0.0).astype(float)
    frame = frame.sort_values(["isin", "date"]).reset_index(drop=True)
    frame["t4"] = ahead_sign(frame).to_numpy()
    return frame


def split(frame: pd.DataFrame, *, fit_to: str = FIT_TO, validate_to: str = VALIDATE_TO) -> dict[str, pd.Series]:
    """Boolean masks ``fit``, ``validate``, ``test`` by date."""
    date = pd.to_datetime(frame["date"])
    fit = date <= pd.Timestamp(fit_to)
    validate = (date > pd.Timestamp(fit_to)) & (date <= pd.Timestamp(validate_to))
    test = date > pd.Timestamp(validate_to)
    return {"fit": fit, "validate": validate, "test": test}


# --------------------------------------------------------------------------- #
# models and the increment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Score:
    target: str
    model: str
    features: str
    years: str
    metric: str
    value: float
    rows: int
    positives: float


def _kind(target: str) -> str:
    return "binary" if target in BINARY else ("regression" if target == "t3" else "multiclass")


def make_model(kind: str, model: str, *, iterations: int = 300, categorical: Sequence[int] = (), backend: str | None = None) -> Any:
    """``linear`` (imputed, scaled logistic or ridge) or ``boost`` (histogram gradient boosting of modest depth, GPU when available)."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression, Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if model == "linear":
        head = Ridge(alpha=1.0) if kind == "regression" else LogisticRegression(max_iter=500, C=1.0)
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), head)
    if model != "boost":
        raise ValueError(f"unknown model {model!r}")
    backend = backend or boost_backend()
    if backend.startswith("xgboost"):
        import xgboost as xgb

        params: dict[str, Any] = dict(
            n_estimators=iterations, max_depth=BOOST_DEPTH, learning_rate=BOOST_LEARNING_RATE, min_child_weight=BOOST_MIN_CHILD_WEIGHT[kind],
            tree_method="hist", device="cuda" if backend == "xgboost-cuda" else "cpu", n_jobs=8,
        )
        if kind == "regression":
            return xgb.XGBRegressor(**params)
        if kind == "binary":
            return xgb.XGBClassifier(objective="binary:logistic", **params)
        return xgb.XGBClassifier(objective="multi:softprob", num_class=3, **params)
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor

    params = dict(max_depth=BOOST_DEPTH, learning_rate=BOOST_LEARNING_RATE, max_iter=iterations, min_samples_leaf=BOOST_MIN_LEAF_ROWS, early_stopping=False)
    if categorical:
        params["categorical_features"] = list(categorical)
    return HistGradientBoostingRegressor(**params) if kind == "regression" else HistGradientBoostingClassifier(**params)


def metric_values(kind: str, y: np.ndarray, prediction: np.ndarray) -> dict[str, float]:
    from sklearn.metrics import average_precision_score, r2_score, roc_auc_score

    if kind == "binary":
        return {"auc": float(roc_auc_score(y, prediction)), "ap": float(average_precision_score(y, prediction))}
    if kind == "regression":
        return {"r2": float(r2_score(y, prediction))}
    return {"macro_auc": float(roc_auc_score(y, prediction, multi_class="ovr", average="macro"))}


def _predict(kind: str, model: Any, x: pd.DataFrame) -> np.ndarray:
    if kind == "regression":
        return np.asarray(model.predict(x), dtype=float)
    proba = np.asarray(model.predict_proba(x), dtype=float)
    return proba[:, 1] if kind == "binary" else proba


def _rows(frame: pd.DataFrame, target: str, mask: pd.Series) -> pd.DataFrame:
    return frame.loc[mask & frame[target].notna()]


def fit_score(
    frame: pd.DataFrame,
    target: str,
    *,
    features: Sequence[str],
    features_label: str,
    model: str,
    masks: dict[str, pd.Series],
    iterations: Sequence[int] = BOOST_ITERATIONS,
) -> list[Score]:
    """Fit on the fit years, score the validation years (choosing the boosting iterations there), refit on both, score the test years."""
    kind = _kind(target)
    categorical = [list(features).index("market_code")] if (model == "boost" and "market_code" in features) else []
    fit, validate, test = (_rows(frame, target, masks[k]) for k in ("fit", "validate", "test"))
    if kind != "regression":
        fit, validate, test = (part.assign(**{target: part[target].astype(int)}) for part in (fit, validate, test))
    columns = list(features)
    scores: list[Score] = []
    best_iterations, best_value, best_metrics = iterations[0], -np.inf, {}
    for n in iterations if model == "boost" else iterations[:1]:
        estimator = make_model(kind, model, iterations=n, categorical=categorical)
        estimator.fit(fit[columns], fit[target].to_numpy())
        metrics = metric_values(kind, validate[target].to_numpy(), _predict(kind, estimator, validate[columns]))
        lead = next(iter(metrics.values()))
        if lead > best_value:
            best_iterations, best_value, best_metrics = n, lead, metrics
    positives_validate = float(validate[target].astype(float).gt(0).mean()) if kind != "regression" else float("nan")
    for name, value in best_metrics.items():
        scores.append(Score(target, model, features_label, "validate", name, value, len(validate), positives_validate))
    both = pd.concat([fit, validate])
    estimator = make_model(kind, model, iterations=best_iterations, categorical=categorical)
    estimator.fit(both[columns], both[target].to_numpy())
    metrics = metric_values(kind, test[target].to_numpy(), _predict(kind, estimator, test[columns]))
    positives_test = float(test[target].astype(float).gt(0).mean()) if kind != "regression" else float("nan")
    for name, value in metrics.items():
        scores.append(Score(target, model, features_label, "test", name, value, len(test), positives_test))
    return scores


def ripple_increments(scores: Sequence[Score]) -> pd.DataFrame:
    """Per target, model, years and metric: state-plus-ripples minus state-only."""
    table = pd.DataFrame([s.__dict__ for s in scores])
    wide = table.pivot_table(index=["target", "model", "years", "metric"], columns="features", values="value", aggfunc="first")
    wide["increment"] = wide["state+ripples"] - wide["state"]
    return wide.reset_index()


def run(
    frame: pd.DataFrame,
    *,
    targets_to_run: Sequence[str] = TARGETS,
    models: Sequence[str] = ("linear", "boost"),
    fit_to: str = FIT_TO,
    validate_to: str = VALIDATE_TO,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The score table and the increments for the assembled frame."""
    masks = split(frame, fit_to=fit_to, validate_to=validate_to)
    scores: list[Score] = []
    for target in targets_to_run:
        for model in models:
            for label, features in FEATURE_SETS.items():
                scores.extend(fit_score(frame, target, features=features, features_label=label, model=model, masks=masks))
    table = pd.DataFrame([s.__dict__ for s in scores])
    return table, ripple_increments(scores)


def markdown(table: pd.DataFrame) -> str:
    """A GitHub table of a frame (floats to four decimals)."""
    columns = list(table.columns)
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    for _, row in table.iterrows():
        cells = [f"{v:.4f}" if isinstance(v, float) and not np.isnan(v) else ("" if isinstance(v, float) else str(v)) for v in row.tolist()]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
