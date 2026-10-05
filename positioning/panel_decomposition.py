"""A HARP-style decomposition of a positioning panel (DD-005; EVAL-008).

A slow panel ``x`` (dates by names: the log level of short interest, of a
register's disclosed level, or of a fund's position) is split, out of
sample on a rolling window, into three layers (HARP, *Global
Persistence, Local Residual Structure*; the reference implementation is
the sibling ``harp`` repository, ``scripts/table5_architectures.py``):

1. the name's own mean inside the window (a fixed effect) and the block's
   persistence ``rho_b`` (a pooled AR(1) per block; the pool when a block
   is too small): the stage-1 residual ``e_t = x_t - mean - rho_b (x_{t-1}
   - mean)``;
2. the block's low-rank residual structure: the PCA basis ``U`` (``K_B``
   components) of the window's stage-1 residuals, the common component
   ``c_t = U U' e_t`` and the idiosyncratic surprise ``eps_t = e_t - c_t``;
3. for the diagnostics only, HARP's forecasts of ``x_t`` from ``x_{t-1}``:
   ``G0`` (the pooled persistence), ``BA`` (the block's) and ``BA_M2``
   (``BA`` plus a ridge VAR on the block's residual scores), scored by
   HARP's out-of-sample R².

Everything is causal: the window ends before the date it decomposes, the
names' means and the bases come from the window, and a date's own value
enters only through ``e_t``. The signals are ``eps`` and ``c`` (and, on a
per-fund panel, ``eps`` aggregated to the issuer across funds); the
family test on them lives in :func:`ic_family`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev

HALFLIFE = 12  # HARP's EWM demeaning, in observations (used for the own-history baseline, not the decomposition)
K_B = 3
MIN_BLOCK = 25
MIN_WINDOW = 24
RIDGE_ALPHA = 1.0
POOL = "__pool__"
EXPECTED_SIGN = -1.0  # SPEC's G4: a growing short position is bearish


# --------------------------------------------------------------------------- #
# the two stages on one window (pure numpy)
# --------------------------------------------------------------------------- #
def fixed_effect_ar1(window: np.ndarray) -> tuple[float, np.ndarray]:
    """HARP's pooled AR(1) with per-name means on a ``(T, N)`` window without NaN: ``(rho, means)``."""
    means = window.mean(axis=0)
    tilde = window - means
    num = float(np.sum(tilde[1:] * tilde[:-1]))
    den = float(np.sum(tilde[:-1] ** 2))
    return (num / den if den > 1e-12 else 0.0), means


def block_ar1(window: np.ndarray, blocks: Sequence[Any], *, min_block: int = MIN_BLOCK) -> tuple[np.ndarray, np.ndarray, dict[Any, float]]:
    """Per name ``rho`` and mean, the block's own where it has ``min_block`` names, the pool's otherwise; and the block rhos."""
    rho_pool, means = fixed_effect_ar1(window)
    rho = np.full(window.shape[1], rho_pool)
    labels = np.asarray(blocks)
    rhos: dict[Any, float] = {POOL: rho_pool}
    for block in pd.unique(labels):
        idx = np.flatnonzero(labels == block)
        if len(idx) < min_block:
            continue
        rho_b, _ = fixed_effect_ar1(window[:, idx])
        rho[idx] = rho_b
        rhos[block] = rho_b
    return rho, means, rhos


def pca_basis(residuals: np.ndarray, k: int = K_B) -> np.ndarray:
    """The first ``k`` principal directions (``N_b x k``) of a ``(T, N_b)`` residual block (HARP's covariance eigenbasis)."""
    n = residuals.shape[1]
    ka = min(k, n - 2)
    if ka < 1:
        return np.zeros((n, 0))
    cov = residuals.T @ residuals / residuals.shape[0]
    eigvals, eigvecs = np.linalg.eigh(cov)
    order = np.argsort(eigvals)[::-1]
    return eigvecs[:, order][:, :ka]


def ridge_var(scores: np.ndarray, alpha: float = RIDGE_ALPHA) -> np.ndarray:
    """HARP's ridge VAR ``s_{t+1} = A s_t`` on the ``(T, k)`` scores (``A`` is ``k x k``)."""
    x, y = scores[:-1], scores[1:]
    k = x.shape[1]
    if k == 0 or len(x) < 2:
        return np.zeros((k, k))
    try:
        return np.linalg.solve(x.T @ x + alpha * np.eye(k), x.T @ y).T
    except np.linalg.LinAlgError:
        return np.eye(k) * 0.5


@dataclass(frozen=True)
class WindowFit:
    rho: np.ndarray  # per name
    means: np.ndarray  # per name
    rho_pool: float
    rhos: dict[Any, float]
    bases: dict[Any, tuple[np.ndarray, np.ndarray, np.ndarray]]  # block -> (name indices, U, A)


def fit_window(window: np.ndarray, blocks: Sequence[Any], *, k: int = K_B, min_block: int = MIN_BLOCK) -> WindowFit:
    """Both stages on a ``(T, N)`` window: the block persistence and, per block large enough, the residual basis and its VAR."""
    rho, means, rhos = block_ar1(window, blocks, min_block=min_block)
    residuals = window[1:] - (means + rho * (window[:-1] - means))
    labels = np.asarray(blocks)
    bases = {}
    for block in pd.unique(labels):
        idx = np.flatnonzero(labels == block)
        if len(idx) < min_block:
            continue
        u = pca_basis(residuals[:, idx], k)
        a = ridge_var(residuals[:, idx] @ u)
        bases[block] = (idx, u, a)
    return WindowFit(rho=rho, means=means, rho_pool=rhos[POOL], rhos=rhos, bases=bases)


def decompose_rows(fit: WindowFit, previous: np.ndarray, current: np.ndarray, previous_residual: np.ndarray | None = None) -> dict[str, np.ndarray]:
    """One date: ``e``, ``c``, ``eps`` and the three forecasts of ``current`` from ``previous`` (all per name)."""
    forecast_ba = fit.means + fit.rho * (previous - fit.means)
    forecast_g0 = fit.means + fit.rho_pool * (previous - fit.means)
    e = current - forecast_ba
    c = np.zeros_like(e)
    forecast_m2 = forecast_ba.copy()
    for _, (idx, u, a) in fit.bases.items():
        if u.shape[1] == 0:
            continue
        c[idx] = (e[idx] @ u) @ u.T
        if previous_residual is not None:
            forecast_m2[idx] = forecast_ba[idx] + (a @ (previous_residual[idx] @ u)) @ u.T
    return {"e": e, "c": c, "eps": e - c, "f_g0": forecast_g0, "f_ba": forecast_ba, "f_ba_m2": forecast_m2}


# --------------------------------------------------------------------------- #
# the rolling decomposition of a long panel
# --------------------------------------------------------------------------- #
def rolling_decomposition(
    panel: pd.DataFrame,
    *,
    window: int,
    step: int,
    blocks: pd.DataFrame | Callable[[pd.Timestamp], pd.Series] | None = None,
    k: int = K_B,
    min_block: int = MIN_BLOCK,
    fill: str = "mean",
) -> pd.DataFrame:
    """Decompose a wide panel (dates by names) on rolling windows of ``window`` rows, refitting every ``step`` rows.

    ``blocks`` maps names to blocks: a frame indexed by name with one column (static), a callable of the window's
    last date returning such a mapping, or None (one pool). ``fill`` is how a name's missing values inside a
    window are filled for the fit (``mean``: the name's window mean, HARP's choice; ``zero``: a missing position is
    zero). A date's output is NaN for the names missing on that date or the one before.
    """
    dates = panel.index
    rows: list[pd.DataFrame] = []
    t = window
    fit: WindowFit | None = None
    fitted_at = -1
    while t < len(dates):
        if fit is None or t - fitted_at >= step:
            train = panel.iloc[t - window:t]
            present = train.columns[train.notna().sum() >= MIN_WINDOW // 2]
            train = train[present]
            filled = train.fillna(train.mean()) if fill == "mean" else train.fillna(0.0)
            filled = filled.fillna(0.0)
            names = list(present)
            if blocks is None:
                labels = [POOL] * len(names)
            else:
                mapping = blocks(dates[t - 1]) if callable(blocks) else blocks.iloc[:, 0]
                labels = [mapping.get(n, POOL) if hasattr(mapping, "get") else POOL for n in names]
            fit = fit_window(filled.to_numpy(dtype=float), labels, k=k, min_block=min_block)
            fitted_at = t
            prev_residual: np.ndarray | None = None
            name_index = pd.Index(names)
            label_series = pd.Series(labels, index=name_index)
        previous = panel.iloc[t - 1].reindex(name_index)
        current = panel.iloc[t].reindex(name_index)
        prev_filled = previous.fillna(pd.Series(fit.means, index=name_index)).to_numpy(dtype=float)
        cur_filled = current.fillna(pd.Series(fit.means, index=name_index)).to_numpy(dtype=float)
        out = decompose_rows(fit, prev_filled, cur_filled, prev_residual)
        prev_residual = out["e"]
        ok = previous.notna().to_numpy() & current.notna().to_numpy()
        frame = pd.DataFrame({"date": dates[t], "name": name_index, "x": cur_filled, "block": label_series.to_numpy(),
                              "rho": fit.rho, **{key: np.where(ok, value, np.nan) for key, value in out.items()}})
        rows.append(frame[ok])
        t += 1
    if not rows:
        return pd.DataFrame(columns=["date", "name", "x", "block", "rho", "e", "c", "eps", "f_g0", "f_ba", "f_ba_m2"])
    return pd.concat(rows, ignore_index=True)


def own_history_zscore(panel: pd.DataFrame, *, halflife: int = 6, min_history: int = 6) -> pd.DataFrame:
    """EVAL-001/005's own-history EWM z-score of each name's series (the baseline the surprise must beat)."""
    return panel.apply(lambda s: ev.ewm_zscore(s, halflife=halflife, min_history=min_history))


def forecast_r2(decomposed: pd.DataFrame) -> pd.DataFrame:
    """HARP's out-of-sample R² of the three forecasts of ``x`` over the decomposed rows, per calendar year."""
    rows = []
    frame = decomposed.dropna(subset=["x", "f_g0"])
    for year, part in frame.groupby(frame["date"].dt.year):
        actual = part["x"].to_numpy()
        ss_tot = float(np.sum((actual - actual.mean()) ** 2))
        entry = {"year": int(year), "rows": len(part)}
        for arch in ("f_g0", "f_ba", "f_ba_m2"):
            ss_res = float(np.sum((actual - part[arch].to_numpy()) ** 2))
            entry[arch.replace("f_", "r2_")] = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        rows.append(entry)
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------- #
# the family
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FamilyRow:
    feature: str
    cleaning: str
    horizon: int
    n_dates: int
    mean_ic: float
    t_stat: float
    p_value: float
    significant: bool
    sign_as_spec: bool


def ic_family(
    frame: pd.DataFrame,
    *,
    features: Sequence[str],
    cleanings: Sequence[str] = ("none", "factors", "factors+level"),
    horizons: Sequence[int] = (10, 21),
    min_names: int,
    nw_lag: Callable[[int], int],
    family_size: int | None = None,
    expected_sign: float = EXPECTED_SIGN,
) -> pd.DataFrame:
    """Per-date Spearman IC of each feature (raw, given the factors, given the factors and the level) against ``ret_adj_<h>``."""
    size = family_size or len(features) * len(cleanings) * len(horizons)
    rows: list[FamilyRow] = []
    for feature in features:
        for cleaning in cleanings:
            if cleaning == "none":
                values = frame[feature]
            else:
                factors = ["reversal_21d", "momentum_12_1"] + (["level"] if cleaning == "factors+level" else [])
                values = _residualise(frame, feature, factors, min_names)
            part = frame[["date"]].assign(_x=values)
            for horizon in horizons:
                part["_r"] = frame[f"ret_adj_{horizon}"]
                ics = _ic(part, min_names)
                mean, t = ev.newey_west_t(ics, lag=nw_lag(horizon))
                p = ev.p_value(t, len(ics))
                rows.append(FamilyRow(feature, cleaning, horizon, len(ics), mean, t, p, p < 0.05 / size,
                                      math.copysign(1.0, mean) == expected_sign if math.isfinite(mean) else False))
    return pd.DataFrame([r.__dict__ for r in rows])


def _residualise(frame: pd.DataFrame, feature: str, factors: Sequence[str], min_names: int) -> pd.Series:
    out = pd.Series(np.nan, index=frame.index)
    cols = [feature, *factors]
    for _, group in frame.groupby("date", sort=False):
        g = group[cols].dropna()
        if len(g) < min_names:
            continue
        y = ev.winsorise(g[feature]).to_numpy(dtype=float)
        x = np.column_stack([np.ones(len(g)), *(ev.winsorise(g[f]).to_numpy(dtype=float) for f in factors)])
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        out.loc[g.index] = y - x @ beta
    return out


def _ic(frame: pd.DataFrame, min_names: int) -> pd.Series:
    ics = {}
    for date, group in frame.groupby("date", sort=True):
        g = group[["_x", "_r"]].dropna()
        if len(g) < min_names:
            continue
        ics[date] = g["_x"].rank().corr(g["_r"].rank())
    return pd.Series(ics, dtype=float)


def halves(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split at the median date."""
    cut = frame["date"].drop_duplicates().sort_values().iloc[len(frame["date"].unique()) // 2]
    return frame[frame["date"] < cut], frame[frame["date"] >= cut]
