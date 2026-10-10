"""Small statistics helpers without a scipy dependency.

``t_sf`` (one-sided Student-t tail probability via the regularised incomplete beta function, Numerical Recipes'
continued fraction) was extracted unchanged from scripts/research_fundamentals.py; also used by research_ml_cross_section,
research_short_overlay and research_index_exclusion. The t-statistic, OLS-alpha and rank-correlation helpers below were
collected from the studies in the slimming pass (docs/architecture.md section 12); each docstring states its
convention.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd


def _betacf(a, b, x, itmax=300, eps=3e-14):
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1.0 + aa / c if abs(1.0 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1.0) < eps:
            break
    return h


def _betai(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lbeta) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta) * _betacf(b, a, 1 - x) / b


def t_sf(t: float, df: int) -> float:
    """One-sided upper-tail p-value of Student's t."""
    if not np.isfinite(t):
        return np.nan
    x = df / (df + t * t)
    p = 0.5 * _betai(df / 2.0, 0.5, x)
    return p if t > 0 else 1.0 - p


# ---- t-statistics of a mean. The studies use three conventions (minimum sample, NaN handling, return shape and
# the order of the floating-point operations differ), so each has its own name; none is a copy of another.

def t_of_mean(x, min_n: int = 3) -> float:
    """mean / sd(ddof=1) * sqrt(n) of the non-NaN values; NaN with fewer than ``min_n`` values or no spread
    (``min_n=3``: strategies.short_overlay, index_exclusion; ``min_n=2``: benchmark_composite)."""
    x = pd.Series(x).dropna()
    if len(x) < min_n or not x.std(ddof=1) > 0:
        return np.nan
    return float(x.mean() / x.std(ddof=1) * math.sqrt(len(x)))


def mean_t_n(x) -> tuple[float, float, int]:
    """(mean, t, n) of the non-NaN values with t = mean / sd(ddof=1) * sqrt(n); t is NaN for n < 2 or no spread
    (osap_screen)."""
    x = pd.Series(x).dropna()
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else np.nan, np.nan, n)
    sd = x.std(ddof=1)
    return float(x.mean()), float(x.mean() / sd * math.sqrt(n)) if sd > 0 else np.nan, n


def mean_t_n_se(x) -> tuple[float, float, int]:
    """(mean, t, n) with t = mean / (sd(ddof=1) / sqrt(n)) and no spread check; t is NaN for n < 2 (ndx_recon's
    event CARs)."""
    x = x.dropna()
    n = len(x)
    if n < 2:
        return (float(x.mean()) if n else np.nan, np.nan, n)
    return float(x.mean()), float(x.mean() / (x.std(ddof=1) / math.sqrt(n))), n


def one_sided_p_normal(t: float) -> float:
    """Upper-tail normal p-value of ``t`` (NaN for a non-finite t)."""
    return 1 - NormalDist().cdf(t) if np.isfinite(t) else np.nan


def ols_alpha(y: pd.Series, x: pd.Series) -> dict:
    """OLS of monthly ``y`` on ``x`` (rows with a NaN dropped): annualised alpha (x 12), its t, beta and n; NaN with
    fewer than 6 rows."""
    d = pd.DataFrame({"y": y, "x": x}).dropna()
    if len(d) < 6:
        return {"alpha_ann": np.nan, "alpha_t": np.nan, "beta": np.nan, "n": len(d)}
    X = np.column_stack([np.ones(len(d)), d["x"].to_numpy()])
    b, *_ = np.linalg.lstsq(X, d["y"].to_numpy(), rcond=None)
    e = d["y"].to_numpy() - X @ b
    s2 = (e @ e) / (len(d) - 2)
    cov = s2 * np.linalg.inv(X.T @ X)
    return {"alpha_ann": float(b[0] * 12), "alpha_t": float(b[0] / math.sqrt(cov[0, 0])), "beta": float(b[1]),
            "n": int(len(d))}


# ---- rank correlation: plain (grid_check) and over the pairs where both values are finite (t_grid)

def spearman(x: pd.Series, y: pd.Series) -> float:
    """Pearson correlation of the ranks of two aligned series."""
    return float(np.corrcoef(x.rank(), y.rank())[0, 1])


def spearman_finite(a: np.ndarray, b: np.ndarray) -> float:
    """``spearman`` of two arrays over the positions where both are finite."""
    m = np.isfinite(a) & np.isfinite(b)
    ra = pd.Series(a[m]).rank().to_numpy()
    rb = pd.Series(b[m]).rank().to_numpy()
    return float(np.corrcoef(ra, rb)[0, 1])
