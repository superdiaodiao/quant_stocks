"""Execution conventions shared by the engines.

- Prices are rounded to the cent tick in the direction that never flatters the strategy (``ceil_cent`` for a
  sell limit, ``floor_cent`` for a buy limit).
- A resting limit fills only when the market trades through it by max(1 tick, 0.05%) (``through=True``); an open
  beyond the limit fills at the open. ``through=False`` is the optimistic touch-fill sensitivity.
- Signals at the close of a session trade at the close of the next session (``next_session``).

Extracted unchanged from scripts/research_intraday_t.py (``ceil_cent``, ``floor_cent``, ``fill_t1``, ``fill_t2``,
``buy_hold_oneq``) and scripts/research_megacap.py (the signal -> execution map in ``simulate``).
"""
from __future__ import annotations

import math

import pandas as pd

from quant.backtest.costs import START_EQUITY, ibkr_order_cost, tick_half_spread

TICK = 0.01
THROUGH_FRAC = 0.0005


def ceil_cent(x: float) -> float:
    return math.ceil(round(x / TICK, 6)) * TICK


def floor_cent(x: float) -> float:
    return math.floor(round(x / TICK, 6)) * TICK


def fill_sell_limit(open_: float, high: float, limit: float, through: bool) -> float | None:
    """Fill price of a sell limit resting through a session (None = not filled)."""
    if open_ >= limit:
        return open_
    need = limit + (max(TICK, THROUGH_FRAC * limit) if through else 0.0)
    return limit if high >= need - 1e-9 else None


def fill_buy_limit(open_: float, low: float, limit: float, through: bool) -> float | None:
    """Fill price of a buy limit resting through a session (None = not filled)."""
    if open_ <= limit:
        return open_
    need = limit - (max(TICK, THROUGH_FRAC * limit) if through else 0.0)
    return limit if low <= need + 1e-9 else None


def next_session(signals, sessions: pd.DatetimeIndex) -> dict:
    """{execution session: signal session}: each signal trades at the first session after it (signals with no
    later session are dropped)."""
    execs = {}
    for s in sorted(signals):
        later = sessions[sessions > s]
        if len(later):
            execs[later[0]] = s
    return execs


def buy_hold_returns(series, sessions: pd.DatetimeIndex, s_date: pd.Timestamp, e_date: pd.Timestamp,
                     half_spread_bps: float, costs: bool = True, equity: float = START_EQUITY) -> pd.Series:
    """Buy and hold of ``series`` (an ``OHLCSeries``): whole shares bought at the close before ``s_date`` with one
    IBKR order (half-spread ``half_spread_bps``, at least half a cent), daily total returns s_date..e_date on
    ``sessions``. The entry cost is charged in the first return."""
    r = series.tr.reindex(sessions)
    rr = r[(r.index >= s_date) & (r.index <= e_date)].copy()
    i = series.sessions.searchsorted(rr.index[0]) - 1
    px = series.close[i]
    shares = math.floor(equity / px)
    c = ibkr_order_cost(shares, px, False, tick_half_spread(half_spread_bps, px))["total"] \
        if costs and shares > 0 else 0.0
    rr.iloc[0] = (1 + rr.iloc[0]) * (equity - c) / equity - 1
    return rr
