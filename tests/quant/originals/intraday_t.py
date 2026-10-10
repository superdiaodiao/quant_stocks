"""Frozen reference copy from scripts/research_intraday_t.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from pathlib import Path
import numpy as np
import pandas as pd
from tests.quant.originals.reversal_dev import order_cost


END = "2026-09-30"
START_EQUITY = 10_000.0
THROUGH_FRAC = 0.0005
TICK = 0.01
ATR_N = 20
HALF_SPREAD_BPS = {"QQQ": 1.0, "QLD": 2.0, "ONEQ": 2.0}


def hs_of(sym: str, price: float) -> float:
    return max(HALF_SPREAD_BPS[sym] / 1e4, 0.005 / price)


def cost(shares: float, price: float, sell: bool, sym: str, on: bool = True) -> float:
    if not on or shares <= 0:
        return 0.0
    return order_cost(shares, price, sell, hs_of(sym, price))["total"]


def ceil_cent(x: float) -> float:
    return math.ceil(round(x / TICK, 6)) * TICK


def floor_cent(x: float) -> float:
    return math.floor(round(x / TICK, 6)) * TICK


@dataclass
class Series_:
    sym: str
    sessions: pd.DatetimeIndex
    open: np.ndarray         # real (as-traded) prices
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    prev_close: np.ndarray   # previous close in today's share units (split on day t already applied)
    split: np.ndarray        # split ratio effective at the open of t (1.0 otherwise)
    div: np.ndarray          # real dividend per share (today's units), ex-date t
    atr_pct: np.ndarray      # mean true range of the 20 sessions up to t-1, over prev close (NaN when < 20)
    tr: pd.Series            # total return from adjclose
    checks: dict


def split_events(path: Path, end: str = END) -> dict:
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    out = {}
    for v in j.get("events", {}).get("splits", {}).values():
        d = pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")
        if d <= end:
            out[d] = float(v["numerator"]) / float(v["denominator"])
    return out


def real_factor(dates: pd.DatetimeIndex, splits: dict) -> np.ndarray:
    """F_t = product of split ratios with ex-date after t (real price = split-adjusted price * F_t)."""
    f = np.ones(len(dates))
    for d, ratio in splits.items():
        f[dates < pd.Timestamp(d)] *= ratio
    return f


def build(sym: str, df: pd.DataFrame, splits: dict) -> Series_:
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
    atr = trp.rolling(ATR_N, min_periods=ATR_N).mean().shift(1).to_numpy()   # through t-1
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
    return Series_(sym, dates, o * f, h * f, lo * f, rc, np.r_[np.nan, rc[:-1]] / split, split, div, atr, tr, checks)


def fill_t1(open_: float, high: float, limit: float, through: bool) -> float | None:
    if open_ >= limit:
        return open_
    need = limit + (max(TICK, THROUGH_FRAC * limit) if through else 0.0)
    return limit if high >= need - 1e-9 else None


def fill_t2(open_: float, low: float, limit: float, through: bool) -> float | None:
    if open_ <= limit:
        return open_
    need = limit - (max(TICK, THROUGH_FRAC * limit) if through else 0.0)
    return limit if low <= need + 1e-9 else None


def buy_hold_oneq(oneq: Series_, sessions: pd.DatetimeIndex, s_date: pd.Timestamp, e_date: pd.Timestamp,
                  costs: bool = True) -> pd.Series:
    """ONEQ buy and hold: bought at the close before s_date (one buy order), daily total returns s_date..e_date."""
    r = oneq.tr.reindex(sessions)
    rr = r[(r.index >= s_date) & (r.index <= e_date)].copy()
    i = oneq.sessions.searchsorted(rr.index[0]) - 1
    px = oneq.close[i]
    c = cost(math.floor(START_EQUITY / px), px, False, "ONEQ", costs)
    rr.iloc[0] = (1 + rr.iloc[0]) * (START_EQUITY - c) / START_EQUITY - 1
    return rr
