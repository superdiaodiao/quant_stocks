"""Option math for the single-stock 0DTE simulator (qc/stock_0dte/sim.py imports it; a separate project file
because QuantConnect caps one file at 32,000 characters). Ledger: docs/research_ledger_stock_0dte.md, 0.3."""
import numpy as np
from scipy.special import ndtr


def bs(S, K, s, put):
    d1 = np.log(S / K) / s + 0.5 * s
    d2 = d1 - s
    if put:
        return K * ndtr(-d2) - S * ndtr(-d1)
    return S * ndtr(d1) - K * ndtr(d2)


def implied_s(S, K, mid, put):
    """sigma*sqrt(T) from Black-Scholes with r = q = 0 (vectorised bisection); nan where mid is not attainable."""
    K = np.asarray(K, float)
    mid = np.asarray(mid, float)
    lo = np.full(K.shape, 1e-6)
    hi = np.full(K.shape, 1.0)
    top = bs(S, K, hi, put)
    for _ in range(50):
        s = 0.5 * (lo + hi)
        up = bs(S, K, s, put) > mid
        hi = np.where(up, s, hi)
        lo = np.where(up, lo, s)
    s = 0.5 * (lo + hi)
    return np.where((mid > 0) & (mid < top), s, np.nan)


def abs_delta(S, K, s, put):
    d1 = np.log(S / K) / s + 0.5 * s
    return np.abs(ndtr(d1) - 1.0) if put else ndtr(d1)


def max_pain(strikes, oi_put, oi_call):
    """The listed strike that minimises what the day's open interest would be paid at expiry (None without OI)."""
    k = np.asarray(strikes, float)
    p = np.asarray(oi_put, float)
    c = np.asarray(oi_call, float)
    if len(k) == 0 or (p.sum() + c.sum()) <= 0:
        return None
    X = k[:, None]
    pay = (c[None, :] * np.maximum(X - k[None, :], 0.0)).sum(axis=1) + (p[None, :] * np.maximum(k[None, :] - X, 0.0)).sum(axis=1)
    return float(k[int(np.argmin(pay))])


def shares_at(legs, K, Sx):
    """Net shares per contract-share delivered to us if the legs in the money at Sx are exercised / assigned
    (OCC exercises 0.01 or more in the money): a call delivers +qn, a put -qn."""
    n = 0
    for r, k, qn in legs:
        if r == 1 and Sx - K[k] >= 0.01:
            n += qn
        elif r == 0 and K[k] - Sx >= 0.01:
            n -= qn
    return n
