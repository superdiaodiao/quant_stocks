"""Price-based signals, each using data up to and including the close of t.

Extracted unchanged from scripts/research_regime.py (``sma_state``, ``trailing_return``, ``trailing_compound``
(there ``trailing_rf``), ``vol_state``, ``inverse_vol_weights``), scripts/research_selective_t.py (``rsi_wilder``)
and scripts/research_megacap.py (``momentum_frames``). Window lengths are parameters without study defaults.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def sma_state(p: pd.Series, length: int) -> pd.Series:
    """1.0 if P_t > SMA(length)_t, 0.0 if not, NaN before the average exists (or where P is missing)."""
    ma = p.rolling(length, min_periods=length).mean()
    s = (p > ma).astype(float)
    return s.where(ma.notna() & p.notna())


def trailing_return(p: pd.Series, n: int) -> pd.Series:
    return p / p.shift(n) - 1


def trailing_compound(r: pd.Series, n: int) -> pd.Series:
    """Compounded return over the last n sessions ending at t (returns t-n+1 .. t), e.g. of the T-bill."""
    return np.exp(np.log1p(r).rolling(n, min_periods=n).sum()) - 1


def vol_state(r: pd.Series, window: int, med_len: int) -> pd.Series:
    """1.0 = calm (``window``-day vol below its own trailing ``med_len``-day median, both including t), 0.0 =
    turbulent, NaN before the median exists."""
    v = r.rolling(window, min_periods=window).std() * math.sqrt(TRADING_DAYS)
    med = v.rolling(med_len, min_periods=med_len).median()
    s = (v < med).astype(float)
    return s.where(med.notna())


def inverse_vol_weights(rets: pd.DataFrame, length: int) -> pd.DataFrame:
    """Weights proportional to 1 / (``length``-day vol); NaN rows until every column has a vol."""
    sig = rets.rolling(length, min_periods=length).std()
    inv = 1.0 / sig
    w = inv.div(inv.sum(axis=1), axis=0)
    return w.where(sig.notna().all(axis=1), np.nan)


def rsi_wilder(c: np.ndarray, n: int) -> np.ndarray:
    """Wilder's RSI(n) of closes ``c`` (NaN until n differences exist)."""
    d = np.diff(c, prepend=np.nan)
    up, dn = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    au, ad = up[1: n + 1].mean(), dn[1: n + 1].mean()
    for t in range(n, len(c)):
        if t > n:
            au = (au * (n - 1) + up[t]) / n
            ad = (ad * (n - 1) + dn[t]) / n
        out[t] = 100.0 if ad == 0 else 100.0 - 100.0 / (1 + au / ad)
    return out


def momentum_frames(idx: pd.DataFrame) -> dict:
    """Total-return momentum known at the close of each session (complete history required):
    ``m6`` = 126-session return, ``m12_1`` = return from t-252 to t-21."""
    return {"m6": idx / idx.shift(126) - 1, "m12_1": idx.shift(21) / idx.shift(252) - 1}
