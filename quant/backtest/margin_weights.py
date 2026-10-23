"""Constant-mix ETF portfolio with optional leverage from an IBKR margin loan (buffett study B3i,
docs/research_ledger_buffett.md section 0.6.3).

At the close of each execution session the holdings are set to ``weights x leverage x equity`` (equity = holdings +
cash); between executions they drift with the daily total returns. Negative cash is a margin loan charged
``(bm + spread) x calendar days / 360`` each day (``bm`` of the previous session, annual decimal); positive cash
earns nothing (a $10k IBKR account earns no interest on its first $10k). Maintenance: when equity at a close falls
below ``maint`` x the holdings' value, the holdings are cut to 1x equity at that close with an extra ``liq_slip``
on the value sold, and stay at 1x until the next execution. Orders: ``etf_order_cost`` per ETF and execution (orders
of $1 or less are skipped); the cost of a rebalance comes out of equity before the new holdings are
set, so the account holds exactly ``leverage`` x equity after it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.backtest.costs import START_EQUITY, etf_order_cost


def simulate_margin_weights(weights: dict, leverage: float, rets: pd.DataFrame, close: pd.DataFrame, bm: pd.Series,
                            executions, start: pd.Timestamp, end: pd.Timestamp, half_spread: dict,
                            spread: float = 0.015, maint: float = 0.25, liq_slip: float = 0.005,
                            equity: float = START_EQUITY) -> dict:
    """Daily simulation on the sessions of ``rets`` in [start, end]; the account starts as ``equity`` cash and buys
    the targets at the close of ``start``; ``executions`` are the later sessions at whose close it rebalances.
    Returns nav, ret, cost (dollars per session), interest (dollars per session), gross (holdings / equity),
    orders, liquidations (sessions)."""
    cols = list(weights)
    w = np.array([weights[c] for c in cols], float)
    sess = rets.index[(rets.index >= start) & (rets.index <= end)]
    R = rets[cols].reindex(sess).to_numpy()
    P = close[cols].reindex(sess).to_numpy()
    B = bm.reindex(sess).ffill().to_numpy()
    exe = set(pd.DatetimeIndex(list(executions)))
    h = np.zeros(len(cols))
    cash = equity
    nav, cost, inte, gross = (np.zeros(len(sess)) for _ in range(4))
    orders, liqs, cut = 0, [], False

    def trade(lev: float, i: int, extra: float = 0.0) -> float:
        """Holdings to w x lev x (equity - cost of this trade): the cost is priced on the trade to w x lev x equity
        and paid out of equity, so a 1x account never borrows to pay its commissions."""
        nonlocal h, cash, orders
        eq = h.sum() + cash
        delta = w * lev * eq - h
        c = 0.0
        for k in range(len(cols)):
            if abs(delta[k]) > 1.0:
                c += etf_order_cost(abs(delta[k]), P[i, k], delta[k] < 0, half_spread[cols[k]])
                if delta[k] < 0:
                    c += extra * abs(delta[k])
                orders += 1
        h = w * lev * (eq - c)
        cash = (1 - lev) * (eq - c)
        return c

    for i, d in enumerate(sess):
        if i > 0:
            r = R[i]
            if np.any(np.isnan(r[h != 0])):
                raise ValueError(f"missing return for a held ETF on {d.date()}")
            h = h * (1 + np.nan_to_num(r))
            if cash < 0:
                days = (d - sess[i - 1]).days
                it = cash * (B[i - 1] + spread) * days / 360.0
                cash += it
                inte[i] = -it
        eq = h.sum() + cash
        if i == 0 or d in exe:
            cost[i] += trade(leverage, i)
            cut = False
        elif not cut and h.sum() > 0 and eq < maint * h.sum():
            cost[i] += trade(1.0, i, extra=liq_slip)
            liqs.append(d)
            cut = True
        nav[i] = h.sum() + cash
        gross[i] = h.sum() / nav[i] if nav[i] > 0 else np.nan
    v = pd.Series(nav, index=sess)
    return {"nav": v, "ret": v.pct_change().iloc[1:], "cost": pd.Series(cost, index=sess),
            "interest": pd.Series(inte, index=sess), "gross": pd.Series(gross, index=sess), "orders": orders,
            "liquidations": [str(x.date()) for x in liqs]}
