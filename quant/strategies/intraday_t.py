"""Repeated intraday "T" trades (做T) on QQQ / QLD around a held base (docs/research_ledger_intraday_t.md section 0):
the T1 / T2 / T3 rules, the cash-account engine with the month-end base rebalance and the judged periods.

Moved unchanged from studies/intraday_t.py in the slimming pass (``Spec``, ``simulate``, ``hs_of``, ``cost``, ``load``,
``period_bounds``, ``name_of`` and the frozen account / rule parameters). The Yahoo chart files and the data end are
arguments; the real-price OHLC, the limit fills and the cost model are quant.data.ohlc / quant.backtest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost as order_cost, tick_half_spread
from quant.backtest.execution import ceil_cent, fill_buy_limit as fill_t2, fill_sell_limit as fill_t1, floor_cent
from quant.data.guards import assert_dev_dates
from quant.data.ohlc import OHLCSeries as Series_, build_real_ohlc as build
from quant.data.sources.yahoo import parse_ohlc, split_events

START_EQUITY = 10_000.0
BASE_FRAC = 0.75
BAND = (0.70, 0.80)
T_FRACTION = 3                     # q = floor(B / 3)
CASH_BUFFER = 5.0
CAP_MULT = 1.10
K_FIXED = 0.01
ATR_MULT = 0.5
ATR_N = 20
HALF_SPREAD_BPS = {"QQQ": 1.0, "QLD": 2.0, "ONEQ": 2.0}
ONEQ_FIRST_RETURN = "2003-10-02"


def hs_of(sym: str, price: float) -> float:
    return tick_half_spread(HALF_SPREAD_BPS[sym], price)


def cost(shares: float, price: float, sell: bool, sym: str, on: bool = True) -> float:
    if not on or shares <= 0:
        return 0.0
    return order_cost(shares, price, sell, hs_of(sym, price))["total"]


def load(sym: str, source: Path, end: str) -> Series_:
    """Real-price OHLC of ``sym`` from the Yahoo chart file ``source``, truncated at ``end`` and asserted; a
    missing open / high / low is an error."""
    df = parse_ohlc(source, end)
    for k in ("open", "high", "low"):
        if df[k].isna().any():
            raise ValueError(f"{sym} {k} missing on {df.loc[df[k].isna(), 'date'].head().tolist()}")
    assert_dev_dates(df["date"], end)
    return build(sym, df, split_events(source, end))


# ======================================================================== simulation

@dataclass
class Spec:
    rule: str | None          # "T1" / "T2" / "T3" / None (hold)
    k_mode: str = "none"      # "k1" / "atr" / "none"
    base_frac: float = BASE_FRAC
    reserve: bool = True      # cash-account model: q limited by settled cash
    rebalance: bool = True
    costs: bool = True
    through: bool = True


def simulate(d: Series_, s: int, e: int, spec: Spec) -> dict:
    """$10,000 at the close of s-1; sessions s..e. Returns daily values, trip records and counters."""
    sym, on = d.sym, spec.costs
    p0 = d.close[s - 1]
    b = int(math.floor(spec.base_frac * (START_EQUITY - 5.0) / (p0 * (1 + hs_of(sym, p0)))))
    cash = START_EQUITY - b * p0 - cost(b, p0, False, sym, on)
    cost_total = cost(b, p0, False, sym, on)
    cost_frac = cost_total / START_EQUITY        # sum of each cost over the account value before it
    values, trips, orders, gfv_days, day_trade = [START_EQUITY], [], 1, 0, []
    month = d.sessions.to_period("M")
    for t in range(s, e + 1):
        if d.split[t] != 1.0:
            b = int(round(b * d.split[t]))
        cash += b * d.div[t]
        p, o, h, lo, c = d.prev_close[t], d.open[t], d.high[t], d.low[t], d.close[t]
        traded = False
        if spec.rule is not None:
            k = K_FIXED if spec.k_mode == "k1" else ATR_MULT * d.atr_pct[t] if spec.k_mode == "atr" else 0.0
            if spec.rule in ("T1", "T3"):
                q = b // T_FRACTION
                if spec.reserve:
                    q = min(q, int(max(0.0, math.floor((cash - CASH_BUFFER) / (CAP_MULT * p)))))
                px = None
                if q > 0 and np.isfinite(k):
                    px = o if spec.rule == "T3" else fill_t1(o, h, ceil_cent(p * (1 + k)), spec.through)
                if px is not None:
                    settled = cash
                    c_s, c_b = cost(q, px, True, sym, on), cost(q, c, False, sym, on)
                    cash += q * px - c_s - q * c - c_b
                    if spec.reserve and settled < q * c + c_b:
                        gfv_days += 1
                    trips.append((d.sessions[t], q, q * (px - c), c_s + c_b, q * px))
                    cost_total += c_s + c_b
                    cost_frac += (c_s + c_b) / values[-1]
                    orders += 2
                    traded = True
            elif spec.rule == "T2":
                lim = floor_cent(p * (1 - k)) if np.isfinite(k) else float("nan")
                q = b // T_FRACTION
                if spec.reserve and np.isfinite(lim):
                    q = min(q, int(max(0.0, math.floor((cash - CASH_BUFFER) / lim))))
                px = fill_t2(o, lo, lim, spec.through) if q > 0 and np.isfinite(lim) else None
                if px is not None:
                    c_b, c_s = cost(q, px, False, sym, on), cost(q, c, True, sym, on)
                    cash += q * c - c_s - q * px - c_b
                    trips.append((d.sessions[t], q, q * (c - px), c_s + c_b, q * px))
                    cost_total += c_s + c_b
                    cost_frac += (c_s + c_b) / values[-1]
                    orders += 2
                    traded = True
        day_trade.append(traded)
        v = b * c + cash
        if spec.rebalance and t < e and month[t + 1] != month[t]:
            w = b * c / v
            if not (BAND[0] <= w <= BAND[1]):
                nb = int(math.floor(spec.base_frac * (v - 5.0) / (c * (1 + hs_of(sym, c)))))
                dq = nb - b
                cc = cost(abs(dq), c, dq < 0, sym, on)
                cash -= dq * c + cc
                cost_total += cc
                cost_frac += cc / v
                orders += 1
                b = nb
                v = b * c + cash
        values.append(v)
    val = pd.Series(values, index=d.sessions[s - 1: e + 1])
    tdf = pd.DataFrame(trips, columns=["date", "q", "gross", "cost", "notional"])
    dt_ = pd.Series(day_trade, index=d.sessions[s: e + 1])
    pdt = (dt_.astype(int).rolling(5, min_periods=1).sum() > 3)
    return {"value": val, "ret": val.pct_change().iloc[1:], "trips": tdf, "orders": orders, "cost_total": cost_total, "cost_frac": cost_frac,
            "gfv_days": gfv_days, "pdt_breach_share": float(pdt.mean()), "final_base": b}


# ======================================================================== periods

def period_bounds(d: Series_, end: str) -> dict:
    """full / half1 / half2 (equal session counts) from the first session with an ATR20 to ``end``."""
    s = int(np.argmax(np.isfinite(d.atr_pct)))
    e = int(d.sessions.searchsorted(pd.Timestamp(end), side="right")) - 1
    mid = s + (e - s + 1) // 2
    return {"full": (s, e), "half1": (s, mid - 1), "half2": (mid, e)}


def name_of(rule: str, k_mode: str) -> str:
    return rule if k_mode == "none" else f"{rule}-{'1%' if k_mode == 'k1' else 'ATR'}"
