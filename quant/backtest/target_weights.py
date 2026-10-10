"""Engine for ETF allocation rules: a daily table of target weights, traded only when the target changes.

Extracted unchanged from scripts/research_regime.py (``simulate``, ``buy_hold``, and in phase 2 ``two_state`` and
``entry_index``, also used by research_mean_reversion); the per-ETF half-spreads, which were a module constant
there, are a parameter here. ``d`` is a ``quant.data.etf_panel.Data``. Costs: ``etf_order_cost``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.backtest.costs import START_EQUITY, etf_order_cost
from quant.data.calendar import month_end_mask
from quant.data.etf_panel import BENCH


def simulate_target_weights(w: pd.DataFrame, rets: pd.DataFrame, close: pd.DataFrame, start_i: int,
                            half_spread: dict, end_i: int | None = None, lag: int = 1, costs: bool = True,
                            equity: float = START_EQUITY) -> dict:
    """Portfolio that starts as ``equity`` cash at the close of session ``start_i`` and trades there to the target
    decided ``lag`` sessions earlier; afterwards it trades at the close of i whenever the target decided at i-lag
    differs from the last executed target. Holdings drift with returns between trades. Orders of $1 or less are
    not placed. Returns value, ret, held (weights earning each day's return), trades, orders, cost_frac."""
    cols = list(w.columns)
    W = w.values
    R = rets[cols].values
    P = close[cols].values
    end_i = len(w) - 1 if end_i is None else end_i
    h = np.zeros(len(cols))
    cash = equity
    last = None
    values, held, trades, orders, cost_frac = [], [], 0, 0, 0.0
    for i in range(start_i, end_i + 1):
        if i > start_i:
            r = R[i]
            if np.any(np.isnan(r[h > 0])):
                raise ValueError(f"missing return for a held asset on {w.index[i].date()}")
            h = h * (1 + np.nan_to_num(r))
        total = h.sum() + cash
        tgt = W[i - lag]
        if np.any(np.isnan(tgt)):
            raise ValueError(f"no target on {w.index[i - lag].date()}")
        if last is None or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            for k in range(len(cols)):
                if abs(delta[k]) > 1.0:
                    if costs:
                        c += etf_order_cost(abs(delta[k]), P[i, k], delta[k] < 0, half_spread[cols[k]])
                    orders += 1
            if last is not None:
                trades += 1
            h = new * (1 - c / total)
            cash = 0.0
            cost_frac += c / total
            last = tgt.copy()
        values.append(h.sum() + cash)
        held.append(h / h.sum() if h.sum() > 0 else h)
    idx = w.index[start_i:end_i + 1]
    v = pd.Series(values, index=idx)
    hw = pd.DataFrame(held, index=idx, columns=cols).shift(1).iloc[1:]    # weights earning each day's return
    return {"value": v, "ret": v.pct_change().iloc[1:], "held": hw, "trades": trades, "orders": orders,
            "cost_frac": cost_frac}


def buy_hold_weights(asset: str, sessions: pd.DatetimeIndex, rets: pd.DataFrame, close: pd.DataFrame, start_i: int,
                     half_spread: dict) -> dict:
    """100% ``asset`` from the close of ``start_i`` (one buy order)."""
    w = pd.DataFrame({asset: 1.0}, index=sessions)
    return simulate_target_weights(w, rets, close, start_i, half_spread)


def two_state(state: pd.Series, on: dict, off: dict, columns) -> pd.DataFrame:
    """Weights ``on`` where the state is 1, ``off`` where it is 0, NaN where it is undefined."""
    w = pd.DataFrame(np.nan, index=state.index, columns=list(columns))
    for k in columns:
        w.loc[state == 1.0, k] = on.get(k, 0.0)
        w.loc[state == 0.0, k] = off.get(k, 0.0)
    return w


def entry_index(w: pd.DataFrame, d, assets, monthly_rule: bool, bench: str = BENCH) -> int:
    """First session S where every used ETF and the benchmark have a close, the target decided at S-1 exists,
    and (monthly rules) S-1 is a month end."""
    have = d.adj[list(assets) + [bench]].notna().all(axis=1).values
    defined = w.notna().all(axis=1).values
    me = month_end_mask(w.index)
    for i in range(1, len(w)):
        if have[i] and defined[i - 1] and (me[i - 1] or not monthly_rule):
            return i
    raise ValueError("no entry possible")
