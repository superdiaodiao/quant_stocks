"""Daily OHLC rebuilt at real (as-traded) prices, for engines that count whole shares and rest limit orders.

Yahoo OHLC are split-adjusted; real prices multiply back the splits after each date, so share counts and the
half-cent spread floor use the prices that actually traded. Extracted unchanged from scripts/research_intraday_t.py
(``Series_``, ``real_factor``, ``build``); the class keeps every field of ``Series_``, so either can be passed to
code written for the other.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

ATR_N = 20


@dataclass
class OHLCSeries:
    sym: str
    sessions: pd.DatetimeIndex
    open: np.ndarray         # real (as-traded) prices
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    prev_close: np.ndarray   # previous close in today's share units (split on day t already applied)
    split: np.ndarray        # split ratio effective at the open of t (1.0 otherwise)
    div: np.ndarray          # real dividend per share (today's units), ex-date t
    atr_pct: np.ndarray      # mean true range of the ``atr_n`` sessions up to t-1, over prev close (NaN when fewer)
    tr: pd.Series            # total return from adjclose
    checks: dict


def real_factor(dates: pd.DatetimeIndex, splits: dict) -> np.ndarray:
    """F_t = product of split ratios with ex-date after t (real price = split-adjusted price * F_t)."""
    f = np.ones(len(dates))
    for d, ratio in splits.items():
        f[dates < pd.Timestamp(d)] *= ratio
    return f


def build_real_ohlc(sym: str, df: pd.DataFrame, splits: dict, atr_n: int = ATR_N) -> OHLCSeries:
    """``df``: date, open, high, low, close (split-adjusted), adjclose, dividend; ``splits``: {date: ratio}."""
    dates = pd.DatetimeIndex(df["date"])
    f = real_factor(dates, splits)
    o, h, lo, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    split = np.ones(len(dates))
    for d, ratio in splits.items():
        i = dates.searchsorted(pd.Timestamp(d))
        if i < len(dates) and dates[i] == pd.Timestamp(d):
            split[i] = ratio
    prev_adj = np.r_[np.nan, c[:-1]]
    tr_ = np.maximum(h, prev_adj) - np.minimum(lo, prev_adj)
    trp = pd.Series(tr_ / prev_adj)
    atr = trp.rolling(atr_n, min_periods=atr_n).mean().shift(1).to_numpy()   # through t-1
    adj = pd.Series(df["adjclose"].to_numpy(float), index=dates)
    rc = c * f
    div = df["dividend"].to_numpy(float) * f
    cd = (rc + div) / (np.r_[np.nan, rc[:-1]] / split) - 1
    tr = adj.pct_change()
    checks = {
        "first": str(dates[0].date()), "last": str(dates[-1].date()), "rows": int(len(dates)),
        "ohlc_missing_or_nonpositive": int(((~np.isfinite(np.c_[o, h, lo, c])) | (np.c_[o, h, lo, c] <= 0)).any(1).sum()),
        "open_or_close_outside_high_low": int(((o > h * (1 + 1e-6)) | (o < lo * (1 - 1e-6))
                                               | (c > h * (1 + 1e-6)) | (c < lo * (1 - 1e-6))).sum()),
        "splits": splits, "dividends": int((div > 0).sum()),
        "max_abs_real_close_plus_div_vs_adjclose": float(np.nanmax(np.abs(cd[1:] - tr.values[1:]))),
        "first_real_close": float(rc[0]), "median_atr20_pct": float(np.nanmedian(atr)),
    }
    return OHLCSeries(sym, dates, o * f, h * f, lo * f, rc, np.r_[np.nan, rc[:-1]] / split, split, div, atr, tr,
                      checks)
