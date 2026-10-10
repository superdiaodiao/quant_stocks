"""Classic technical indicators (pure, causal: each value uses data up to and including t).

Extracted unchanged from scripts/research_indicators.py (``sma``, ``ema``, ``wilder``, ``rsi``, ``macd``, ``kdj``,
``bollinger``, ``true_range``, ``atr``, ``donchian``); also used by research_mean_reversion.
"""
from __future__ import annotations

import numpy as np


def sma(x, n: int):
    return x.rolling(n, min_periods=n).mean()


def ema(x, n: int):
    """Exponential average with alpha = 2 / (n + 1), started on the first value (pandas adjust=False)."""
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(x, n: int):
    """Wilder smoothing (alpha = 1 / n), started on the first value."""
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close, n: int = 14):
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = wilder(up.iloc[1:], n), wilder(dn.iloc[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + au / ad)
    out = out.where(ad > 0, 100.0).where(au.notna())
    return out.reindex(close.index)


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return dif, dea, dif - dea


def kdj(high, low, close, n: int = 14, alpha: float = 1 / 3):
    """The owner's KDJ (src/strategy/common.calculate_kdj): RSV over n sessions, K = EMA(RSV), D = EMA(K), J = 3K - 2D."""
    hh, ll = high.rolling(n).max(), low.rolling(n).min()
    rsv = (close - ll) / (hh - ll) * 100
    k = rsv.ewm(alpha=alpha, adjust=False).mean()
    d = k.ewm(alpha=alpha, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def bollinger(close, n: int, k: float):
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return ma, ma + k * sd, ma - k * sd


def true_range(high, low, close):
    pc = close.shift(1)
    a, b, c = (high - low), (high - pc).abs(), (low - pc).abs()
    tr = np.maximum(np.maximum(a, b), c)
    return tr.where(pc.notna())


def atr(high, low, close, n: int):
    return wilder(true_range(high, low, close).iloc[1:], n).reindex(close.index)


def donchian(high, low, n: int):
    """Channel from the n sessions BEFORE t (today's own bar is not in it)."""
    return high.shift(1).rolling(n, min_periods=n).max(), low.shift(1).rolling(n, min_periods=n).min()
