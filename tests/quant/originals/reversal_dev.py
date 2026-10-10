"""Frozen reference copy from scripts/research_reversal_dev.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
import math
from statistics import NormalDist
import numpy as np
import pandas as pd
from src.research.ibkr_cost_calibration import base_stock_commission_usd  # noqa: E402
from scripts import study_data_version as dv  # noqa: E402


NORM = NormalDist()
# ---------------------------------------------------------------- cost assumptions (IBKR Pro Tiered, 2012-2016)
EXCHANGE_FEE_PER_SHARE = 0.0010    # Nasdaq closing-cross (MOC/LOC) fee order of magnitude, passed through by Tiered
CLEARING_FEE_PER_SHARE = 0.0002    # NSCC/DTC clearing pass-through
PASS_THROUGH_OF_COMMISSION = 0.000175 + 0.00056   # NYSE + FINRA pass-through, fractions of the commission
SEC_FEE_PER_DOLLAR_SOLD = 22.1e-6  # SEC Section 31 fee, roughly $17-23 per $1M sold in 2012-2016; the top used
FINRA_TAF_PER_SHARE_SOLD = 0.000119
FINRA_TAF_MAX = 5.95
# Effective half-spread by dv rank bucket (large-cap Nasdaq names, 2012-2016), floored at half a cent tick.
HALF_SPREAD_BPS = ((50, 2.0), (100, 3.0), (150, 4.0), (200, 5.0), (250, 6.0), (300, 8.0))
HALF_SPREAD_UNRANKED_BPS = 10.0
REBALANCE_BAND = 0.25
TERMINAL_D5 = -0.55


def half_spread(rank: float, price: float, mult: float = 1.0) -> float:
    bps = HALF_SPREAD_UNRANKED_BPS
    if rank == rank:
        for upper, b in HALF_SPREAD_BPS:
            if rank <= upper:
                bps = b
                break
    return max(bps * mult / 1e4 * 1.0, 0.005 / price * mult)


def order_cost(shares: float, price: float, sell: bool, hs: float) -> dict:
    """Cost of one order: IBKR Tiered commission (min $0.35, max 1%), pass-through fees, regulatory fees on
    sells (including short sales), and the half-spread ``hs`` (a fraction of trade value)."""
    shares = abs(float(shares))
    if shares == 0:
        return {"commission": 0.0, "fees": 0.0, "spread": 0.0, "total": 0.0}
    value = shares * price
    commission = base_stock_commission_usd(shares, price, pricing_plan="tiered")
    fees = shares * (EXCHANGE_FEE_PER_SHARE + CLEARING_FEE_PER_SHARE) + commission * PASS_THROUGH_OF_COMMISSION
    if sell:
        fees += value * SEC_FEE_PER_DOLLAR_SOLD + min(shares * FINRA_TAF_PER_SHARE_SOLD, FINRA_TAF_MAX)
    spread = value * hs
    return {"commission": commission, "fees": fees, "spread": spread, "total": commission + fees + spread}


def make_index(tr: pd.DataFrame, close: pd.DataFrame | None = None) -> pd.DataFrame:
    """Cumulative total-return index; flat after the last booked value (cash), NaN before the first row
    (the first price row when ``close`` is given, else the first return)."""
    started = (close if close is not None else tr).notna().cumsum() > 0
    return (1 + tr.fillna(0.0)).cumprod().where(started)


def max_drawdown(r: pd.Series) -> float:
    w = (1 + r).cumprod()
    return float((w / w.cummax() - 1).min()) if len(w) else float("nan")


def deflated_sharpe(sr: float, n_obs: int, skew: float, kurt: float, sr_trials: np.ndarray) -> dict:
    """Bailey and Lopez de Prado (2014) deflated Sharpe ratio, per-period Sharpe ratios."""
    n_trials = len(sr_trials)
    var = float(np.var(sr_trials, ddof=1))
    g = 0.5772156649
    sr0 = math.sqrt(var) * ((1 - g) * NORM.inv_cdf(1 - 1 / n_trials) + g * NORM.inv_cdf(1 - 1 / (n_trials * math.e)))
    denom = math.sqrt(max(1 - skew * sr + (kurt - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(n_obs - 1) / denom
    return {"n_trials": n_trials, "sr0_weekly": sr0, "dsr": float(NORM.cdf(z)), "z": z}
