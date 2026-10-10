"""Target-weight engine with rebalance flags (the sector / leverage ETF studies).

Extracted unchanged from scripts/research_sector_lev.py (``entry_index``, ``simulate``, ``buy_hold``); also used by
research_voltarget. The leg half-spreads, a module dict there, are the ``half_spread`` argument (default: the
sector-lev legs, ``quant.data.sector_etfs.LEG_HALF_SPREAD``). ``d`` is a ``quant.data.sector_etfs.Data``.

$10,000 cash at the close of ``start_i`` is traded to the target decided ``lag`` sessions earlier; afterwards a
trade happens at the close of i when the decision at i-lag is a rebalance day or its target changed.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.backtest.costs import START_EQUITY, etf_order_cost
from quant.data.sector_etfs import LEG_HALF_SPREAD

TRADING_DAYS = 252


def entry_index(w: pd.DataFrame, flag: pd.Series, d, monthly_rule: bool, not_before=None) -> int:
    """First session S (>= not_before) with a QQQ close and a benchmark return, a target decided at S-1 and,
    for monthly rules, S-1 a decision day."""
    have = (d.close["QQQ"].notna() & d.rets["BENCH"].notna()).values
    defined = w.notna().all(axis=1).values
    fl = flag.values
    lo = 1 if not_before is None else max(1, int(w.index.searchsorted(pd.Timestamp(not_before))))
    for i in range(lo, len(w)):
        if have[i] and defined[i - 1] and (fl[i - 1] or not monthly_rule):
            return i
    raise ValueError("no entry possible")


def simulate(w: pd.DataFrame, flag: pd.Series, rets: pd.DataFrame, close: pd.DataFrame, start_i: int,
             end_i: int | None = None, lag: int = 1, costs: bool = True, extra_drag: dict | None = None,
             half_spread: dict = LEG_HALF_SPREAD) -> dict:
    """$10,000 cash at the close of ``start_i`` traded to the target decided ``lag`` sessions earlier; afterwards
    trades at the close of i when the decision at i-lag is a rebalance day or its target differs from the last
    executed one. Holdings drift between trades. ``extra_drag``: {leg: annual drag} for sensitivities."""
    cols = list(w.columns)
    W, F = w.values, flag.reindex(w.index).fillna(False).values
    R = rets[cols].values.copy()
    for k, a in (extra_drag or {}).items():
        R[:, cols.index(k)] -= a / TRADING_DAYS
    P = close[cols].values
    end_i = len(w) - 1 if end_i is None else end_i
    h = np.zeros(len(cols))
    cash = START_EQUITY
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
        if last is None or F[i - lag] or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            traded = False
            for k in range(len(cols)):
                if abs(delta[k]) > 1.0:
                    if costs:
                        c += etf_order_cost(abs(delta[k]), P[i, k], delta[k] < 0, half_spread[cols[k]])
                    orders += 1
                    traded = True
            if traded or last is None:
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
    hw = pd.DataFrame(held, index=idx, columns=cols).shift(1).iloc[1:]
    return {"value": v, "ret": v.pct_change().iloc[1:], "held": hw, "trades": trades, "orders": orders,
            "cost_frac": cost_frac}


def buy_hold(leg: str, d, start_i: int) -> dict:
    w = pd.DataFrame({leg: 1.0}, index=d.sessions)
    return simulate(w, pd.Series(False, index=d.sessions), d.rets, d.close, start_i)
