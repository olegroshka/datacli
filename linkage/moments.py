"""Pairwise-complete second moments of two matrices with missing values. Pure.

For ``X`` (T x N) and ``Y`` (T x K), each with NaN where a value is missing,
:func:`pairwise` returns, for every column pair ``(i, k)``, the number of rows
where both are present and the covariance and both variances over exactly
those rows. Everything is a handful of matrix products, so a month-end
snapshot of thousands of names against dozens of ETFs takes milliseconds.

Population moments are used; betas and correlations do not depend on the
normalisation.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Pairwise:
    n: np.ndarray  # (N, K) shared rows
    cov: np.ndarray
    var_x: np.ndarray  # variance of x over the shared rows
    var_y: np.ndarray

    def beta(self) -> np.ndarray:
        """Slope of x on y."""
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(self.var_y > 0, self.cov / self.var_y, np.nan)

    def corr(self) -> np.ndarray:
        with np.errstate(divide="ignore", invalid="ignore"):
            denom = np.sqrt(self.var_x * self.var_y)
            return np.where(denom > 0, np.clip(self.cov / denom, -1.0, 1.0), np.nan)


def pairwise(x: np.ndarray, y: np.ndarray) -> Pairwise:
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    if x.ndim != 2 or y.ndim != 2 or x.shape[0] != y.shape[0]:
        raise ValueError(
            f"need two 2-d arrays with the same rows, got {x.shape} and {y.shape}"
        )
    vx = (~np.isnan(x)).astype(np.float64)
    vy = (~np.isnan(y)).astype(np.float64)
    x0 = np.nan_to_num(x, nan=0.0)
    y0 = np.nan_to_num(y, nan=0.0)
    n = vx.T @ vy
    sx = x0.T @ vy
    sy = vx.T @ y0
    sxy = x0.T @ y0
    sxx = (x0 * x0).T @ vy
    syy = vx.T @ (y0 * y0)
    with np.errstate(divide="ignore", invalid="ignore"):
        mx, my = sx / n, sy / n
        cov = sxy / n - mx * my
        var_x = np.maximum(sxx / n - mx * mx, 0.0)
        var_y = np.maximum(syy / n - my * my, 0.0)
    empty = n == 0
    for arr in (cov, var_x, var_y):
        arr[empty] = np.nan
    return Pairwise(n=n.astype(np.int64), cov=cov, var_x=var_x, var_y=var_y)


def market_residuals(r: np.ndarray, market: np.ndarray, min_obs: int) -> np.ndarray:
    """Each column of ``r`` net of its own OLS fit on ``market`` (intercept and slope).

    Fitted on the rows where both are present; a column with fewer than
    ``min_obs`` such rows is all NaN.
    """
    r = np.asarray(r, dtype=np.float64)
    m = np.asarray(market, dtype=np.float64).reshape(-1, 1)
    moments = pairwise(r, m)
    beta = moments.beta()[:, 0]
    valid = (~np.isnan(r)) & (~np.isnan(m))
    count = valid.sum(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_r = np.where(valid, r, 0.0).sum(axis=0) / count
        mean_m = (np.where(valid, m, 0.0)).sum(axis=0) / count
    alpha = mean_r - beta * mean_m
    resid = r - alpha - beta * m
    resid[~valid] = np.nan
    resid[:, count < min_obs] = np.nan
    return resid
