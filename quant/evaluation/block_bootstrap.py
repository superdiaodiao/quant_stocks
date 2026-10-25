"""Circular block bootstrap of monthly returns: the distribution of 10- and 20-year outcomes of several portfolios
resampled on the same calendar months (docs/research_ledger_risk_ladder.md 0.6).

A draw is a sequence of ``ceil(horizon / block)`` blocks of ``block`` consecutive months, each starting at a uniform
random month and wrapping from the last month to the first (circular, so every month is equally likely), cut to
``horizon`` months. The same draws are applied to every portfolio, so comparisons between portfolios are paired.
Drawdowns are measured on month-end values (shallower than daily drawdowns). Pure functions.
"""
from __future__ import annotations

import numpy as np


def circular_block_index(n: int, horizon: int, block: int, draws: int, rng: np.random.Generator) -> np.ndarray:
    """(draws, horizon) month positions in 0..n-1."""
    if block < 1 or n < 1:
        raise ValueError("block and n must be positive")
    nb = -(-horizon // block)
    starts = rng.integers(0, n, size=(draws, nb))
    idx = (starts[:, :, None] + np.arange(block)[None, None, :]) % n
    return idx.reshape(draws, nb * block)[:, :horizon]


def path_outcomes(r: np.ndarray, idx: np.ndarray) -> dict:
    """Per draw: annualised growth (CAGR), max drawdown from the start value 1 (month ends), worst 12-month return
    (NaN when the horizon is shorter than 12 months) and the terminal wealth multiple, for monthly returns ``r``."""
    x = np.asarray(r, float)[idx]
    if np.isnan(x).any():
        raise ValueError("NaN monthly return in the sample")
    W = np.cumprod(1.0 + x, axis=1)
    H = idx.shape[1]
    Wp = np.concatenate([np.ones((W.shape[0], 1)), W], axis=1)
    peak = np.maximum.accumulate(Wp, axis=1)
    dd = (Wp / peak - 1.0).min(axis=1)
    w12 = (Wp[:, 12:] / Wp[:, :-12] - 1.0).min(axis=1) if H >= 12 else np.full(W.shape[0], np.nan)
    end = W[:, -1]
    cagr = np.where(end > 0, np.abs(end) ** (12.0 / H) - 1.0, -1.0)
    return {"cagr": cagr, "max_dd": dd, "worst_12m": w12, "multiple": end}


def pct(x: np.ndarray, qs=(5, 25, 50, 75, 95)) -> dict:
    return {f"p{q}": float(np.percentile(x, q)) for q in qs}
