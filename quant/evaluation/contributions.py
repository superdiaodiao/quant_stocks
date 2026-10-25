"""Monthly contributions: the account value of a fixed monthly saving plan run on a series of monthly returns, and
what it does to the drawdown experience (docs/research_ledger_risk_ladder.md 0.6).

Convention: ``initial`` dollars at the start, then after each month's return ``contrib`` dollars are added at the
month end (so the last contribution has not earned anything yet). The cost of investing a contribution is ignored.
Pure functions; arrays are (draws, months) or a single path (months,).
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def account_paths(r: np.ndarray, initial: float, contrib: float) -> tuple[np.ndarray, np.ndarray]:
    """(value, contributed) after each month for monthly returns ``r`` (shape (..., H)), both of shape (..., H).
    value_t = value_{t-1} (1 + r_t) + contrib, value_0 = initial; contributed_t = initial + t x contrib."""
    r = np.asarray(r, float)
    v = np.empty_like(r)
    cur = np.full(r.shape[:-1], float(initial))
    for t in range(r.shape[-1]):
        cur = cur * (1.0 + r[..., t]) + contrib
        v[..., t] = cur
    c = initial + contrib * np.arange(1, r.shape[-1] + 1, dtype=float)
    return v, np.broadcast_to(c, v.shape)


def irr_annual(final: np.ndarray, initial: float, contrib: float, months: int, lo: float = -0.5, hi: float = 0.5,
               iters: int = 80) -> np.ndarray:
    """Money-weighted annual return (IRR) of ``initial`` at 0, ``contrib`` at each month end 1..months and the value
    ``final`` at month ``months`` (bisection on the monthly rate; NaN when the value is not positive)."""
    final = np.asarray(final, float)
    a, b = np.full(final.shape, lo), np.full(final.shape, hi)

    def fv(x):
        g = (1.0 + x) ** months
        with np.errstate(divide="ignore", invalid="ignore"):
            ann = np.where(np.abs(x) > 1e-12, (g - 1.0) / x, float(months))
        return initial * g + contrib * ann

    for _ in range(iters):
        m = 0.5 * (a + b)
        up = fv(m) < final
        a = np.where(up, m, a)
        b = np.where(up, b, m)
    x = 0.5 * (a + b)
    out = (1.0 + x) ** 12 - 1.0
    return np.where(final > 0, out, np.nan)


def longest_run(mask: np.ndarray) -> np.ndarray:
    """Longest run of consecutive True along the last axis."""
    mask = np.asarray(mask, bool)
    best = np.zeros(mask.shape[:-1], int)
    cur = np.zeros(mask.shape[:-1], int)
    for t in range(mask.shape[-1]):
        cur = np.where(mask[..., t], cur + 1, 0)
        best = np.maximum(best, cur)
    return best


def contribution_stats(r: np.ndarray, initial: float, contrib: float) -> dict:
    """Per path (arrays over the leading axes): final value, contributed, final / contributed, IRR, the account's
    max drawdown (value incl. contributions, monthly), the worst value / contributed - 1, the months and the longest
    stretch with value below the money put in, and the max drawdown of the same returns without contributions."""
    r = np.asarray(r, float)
    v, c = account_paths(r, initial, contrib)
    H = r.shape[-1]
    peak = np.maximum.accumulate(np.concatenate([np.full(r.shape[:-1] + (1,), float(initial)), v], axis=-1), axis=-1)
    acct_dd = (v / peak[..., 1:] - 1.0).min(axis=-1)
    w = np.cumprod(1.0 + r, axis=-1)
    wpk = np.maximum.accumulate(np.concatenate([np.ones(r.shape[:-1] + (1,)), w], axis=-1), axis=-1)
    twr_dd = (w / wpk[..., 1:] - 1.0).min(axis=-1)
    below = v < c
    return {"final_value": v[..., -1], "contributed": c[..., -1], "final_over_contributed": v[..., -1] / c[..., -1],
            "irr": irr_annual(v[..., -1], initial, contrib, H), "account_max_dd": acct_dd,
            "worst_vs_contributed": (v / c - 1.0).min(axis=-1), "months_below_contributed": below.sum(axis=-1),
            "longest_below_contributed": longest_run(below), "twr_max_dd": twr_dd}


def path_contribution_metrics(r_monthly: pd.Series, initial: float, contrib: float) -> dict:
    """``contribution_stats`` of one historical path of monthly returns, as plain floats, with the month of the
    account's worst point versus the money put in."""
    s = contribution_stats(r_monthly.values, initial, contrib)
    v, c = account_paths(r_monthly.values, initial, contrib)
    out = {k: float(x) for k, x in s.items()}
    out["months"] = int(len(r_monthly))
    out["worst_vs_contributed_month"] = str(r_monthly.index[int(np.argmin(v / c))])
    return out
