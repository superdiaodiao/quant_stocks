"""Exposure timing engine: cash / 1x / 2x sleeves of an index, as in the QQQ timing studies.

Extracted unchanged from scripts/research_qqq_timing.py (``weights_for``, ``simulate``; ``synthetic_2x`` is in quant.data.synthetic). The only
change: the half-spread of the ETF order-cost model, a module constant there that research_indicators overrode
temporarily, is the ``half_spread`` argument here.

Exposure between 0 and 2 is held as sleeves: e <= 1 -> e in the 1x fund, 1 - e in cash (a T-bill ETF); e > 1 ->
(2 - e) in the 1x fund and (e - 1) in the 2x fund. The 2x fund may be synthetic: 2 * r - (RF + spread) - fee.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.backtest.costs import NOMINAL_PRICE, START_EQUITY, etf_order_cost
from quant.data.synthetic import synthetic_2x  # noqa: F401  (re-exported)

TRADING_DAYS = 252
HALF_SPREAD = 1e-4                # 1 bp for QQQ, QLD and the T-bill ETF
VT_BAND = 0.25                    # a continuous target is traded only when >= 0.25 away from the held exposure


def weights_for(e: float) -> np.ndarray:
    """Exposure -> sleeve weights (QQQ, 2x fund, cash)."""
    e = float(min(max(e, 0.0), 2.0))
    if e <= 1.0:
        return np.array([e, 0.0, 1.0 - e])
    return np.array([2.0 - e, e - 1.0, 0.0])


def simulate(target: pd.Series, r1: pd.Series, r2: pd.Series, rc: pd.Series, price: pd.Series,
             start: str, end: str, lag: int = 1, continuous: bool = False, cash_is_etf: bool = True,
             half_spread: float = HALF_SPREAD) -> dict:
    """Run the sleeve portfolio.

    ``target`` is the exposure decided at each close t. With ``lag`` = 1 the trade happens at the close of t+1
    (so the new exposure earns from t+2); with ``lag`` = 0 at the close of t. The portfolio starts as cash at the
    close of ``start`` and trades there to the target decided ``lag`` sessions earlier.
    """
    idx = r1.index
    i0 = int(idx.searchsorted(pd.Timestamp(start)))
    i1 = int(idx.searchsorted(pd.Timestamp(end), side="right")) - 1
    tv = target.reindex(idx).values
    a1, a2, ac, pv = r1.values, r2.values, rc.values, price.values
    sleeves = np.array([0.0, 0.0, START_EQUITY])
    cur_state = None
    values, expo, orders, rebal, costs, cost_frac = [], [], 0, 0, 0.0, 0.0
    for i in range(i0, i1 + 1):
        if i > i0:
            sleeves = sleeves * (1 + np.array([a1[i], a2[i], ac[i]]))
        total = sleeves.sum()
        held_e = (sleeves[0] + 2 * sleeves[1]) / total
        desired = tv[i - lag] if i - lag >= 0 else np.nan
        if np.isnan(desired):
            desired = 0.0 if cur_state is None else cur_state
        if continuous:
            go = cur_state is None or abs(desired - held_e) >= VT_BAND - 1e-12 or (desired == 0.0 and held_e > 0)
        else:
            go = cur_state is None or desired != cur_state
        if go:
            new = weights_for(desired) * total
            delta = new - sleeves
            px = [pv[i] if not np.isnan(pv[i]) else NOMINAL_PRICE, NOMINAL_PRICE, NOMINAL_PRICE]
            c = 0.0
            for k in range(3):
                if k == 2 and not cash_is_etf:
                    continue
                if abs(delta[k]) > 1.0:
                    c += etf_order_cost(abs(delta[k]), px[k], delta[k] < 0, half_spread)
                    orders += 1
            if cur_state is not None:
                rebal += 1
            sleeves = new * (1 - c / total)
            costs += c
            cost_frac += c / total
            cur_state = desired
            held_e = desired
        values.append(sleeves.sum())
        expo.append(held_e)     # exposure held into the next session
    v = pd.Series(values, index=idx[i0:i1 + 1])
    e = pd.Series(expo, index=idx[i0:i1 + 1])
    return {"value": v, "ret": v.pct_change().iloc[1:], "exposure_held": e.shift(1).iloc[1:],
            "orders": orders, "rebalances": rebal, "costs": costs, "cost_frac": cost_frac}
