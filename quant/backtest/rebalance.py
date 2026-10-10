"""Engine for periodic stock portfolios on the point-in-time panel.

Positions are units of each name's total-return index (so dividends, splits and terminal returns are already in
the price path); 'QQQ' is a slot that uses the QQQ index. Extracted unchanged from scripts/research_megacap.py
(``simulate``, ``buy_hold``); the QQQ half-spread, a module constant there, is a parameter here.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.backtest.costs import REBALANCE_BAND, START_EQUITY, ibkr_value_cost, rank_half_spread
from quant.backtest.execution import next_session

QQQ_HS = 1e-4


def simulate_index_units(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame,
                         last_row: pd.Series, qqq_idx: pd.Series, qqq_close: pd.Series,
                         account: float = START_EQUITY, band: float = REBALANCE_BAND, end: str | None = None,
                         qqq_hs: float = QQQ_HS) -> dict:
    """Daily simulation. ``targets``: signal session -> list of (sid, weight, dv50_rank, mcap, mcap_src). A target
    decided at the close of signal session s is traded at the close of the next session. Sells first, then buys
    (scaled down when cash is short); a continuing position is traded only when it is more than ``band`` x its
    target value away from the target. Ended series are turned into cash (terminal return already booked).
    Half-spreads: by dv50 rank (``rank_half_spread``; names being sold out use rank 50), ``qqq_hs`` for QQQ."""
    if not targets:
        raise ValueError("no targets")
    execs = next_session(targets, sessions)
    start = min(execs)
    perf = sessions[(sessions >= start) & (sessions <= pd.Timestamp(end or sessions[-1]))]
    col = {c: i for i, c in enumerate(idx.columns)}
    I = idx.reindex(perf).to_numpy()
    P = close.reindex(perf).to_numpy()
    Q = qqq_idx.reindex(perf).to_numpy()
    QP = qqq_close.reindex(perf).to_numpy()
    lr = last_row
    cash = account
    pos: dict = {}           # sid -> units
    nav = np.zeros(len(perf))
    cost = np.zeros(len(perf))
    traded = np.zeros(len(perf))
    n_orders = np.zeros(len(perf), dtype=int)
    n_names = np.zeros(len(perf), dtype=int)

    def ix(sid, i):
        return Q[i] if sid == "QQQ" else I[i, col[sid]]

    def px(sid, i):
        return QP[i] if sid == "QQQ" else P[i, col[sid]]

    for i, d in enumerate(perf):
        for sid in [s for s in pos if s != "QQQ" and pd.notna(lr.get(s)) and d > lr[s]]:
            cash += pos.pop(sid) * I[i, col[sid]]
        if d in execs:
            tgt = targets[execs[d]]
            hs_of = {sid: (qqq_hs if sid == "QQQ" else rank_half_spread(rk, px(sid, i))) for sid, _, rk, _, _ in tgt}
            for sid in pos:
                if sid not in hs_of:
                    hs_of[sid] = qqq_hs if sid == "QQQ" else rank_half_spread(50, px(sid, i))
            total = cash + sum(u * ix(s, i) for s, u in pos.items())
            want = {sid: w * total for sid, w, _, _, _ in tgt if sid == "QQQ" or np.isfinite(ix(sid, i))}
            sells, buys = {}, {}
            for sid, u in pos.items():
                cur = u * ix(sid, i)
                tv = want.get(sid, 0.0)
                if tv == 0.0:
                    sells[sid] = cur
                elif abs(tv - cur) > band * tv:
                    (sells if tv < cur else buys)[sid] = abs(tv - cur)
            for sid, tv in want.items():
                if sid not in pos:
                    buys[sid] = tv
            for sid, v in sorted(sells.items()):
                k = ibkr_value_cost(v, px(sid, i), True, hs_of[sid])
                pos[sid] -= v / ix(sid, i)
                if pos[sid] <= 1e-12 or sid not in want:
                    pos.pop(sid)
                cash += v - k
                cost[i] += k
                traded[i] += v
                n_orders[i] += 1
            need = sum(buys.values())
            scale = min(1.0, cash / need) if need > 0 else 1.0
            for sid, v in sorted(buys.items()):
                amt = v * scale
                if amt < 1.0:
                    continue
                k = ibkr_value_cost(amt, px(sid, i), False, hs_of[sid])
                pos[sid] = pos.get(sid, 0.0) + (amt - k) / ix(sid, i)
                cash -= amt
                cost[i] += k
                traded[i] += amt
                n_orders[i] += 1
            cash = max(cash, 0.0) if abs(cash) < 1e-6 else cash
        nav[i] = cash + sum(u * ix(s, i) for s, u in pos.items())
        n_names[i] = len(pos)
    return {"nav": pd.Series(nav, index=perf), "cost": pd.Series(cost, index=perf),
            "traded": pd.Series(traded, index=perf), "orders": pd.Series(n_orders, index=perf),
            "names": pd.Series(n_names, index=perf), "start": perf[0]}


def buy_hold_level(level: pd.Series, price: pd.Series, dates: pd.DatetimeIndex, hs: float,
                   account: float = START_EQUITY) -> pd.Series:
    """Dollar value of ``account`` put into a total-return ``level`` at the first of ``dates`` (one buy order)."""
    lvl = level.reindex(dates)
    k = ibkr_value_cost(account, float(price.reindex(dates).iloc[0]), False, hs)
    return (account - k) * lvl / lvl.iloc[0]
