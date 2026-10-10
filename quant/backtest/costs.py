"""Trading costs on a $10,000 IBKR Pro Tiered account (docs/cost_model_ibkr_tiered.md).

Two models are in use and both are kept exactly, because frozen results were computed with them:

- ``etf_order_cost`` (scripts/research_qqq_timing.py, research_regime.py, research_calendar.py): a flat
  approximation for ETF switching - $0.0035/share (min $0.35, max 1% of value), $0.0005/share pass-through,
  0.3 bp regulatory fee on sells, plus a half-spread.
- ``ibkr_order_cost`` (scripts/research_reversal_dev.py): the published Tiered commission
  (src/research/ibkr_cost_calibration.py) plus closing-cross exchange and clearing fees, NYSE/FINRA pass-through,
  SEC fee and FINRA TAF on sells, and a half-spread. Used by every stock-level study.

Half-spread helpers: ``rank_half_spread`` (by dollar-volume rank bucket, floored at half a cent) and
``tick_half_spread`` (fixed bps, floored at half a cent).
"""
from __future__ import annotations

import numpy as np

from quant.data.synthetic import CASH_ETF_FEE, LEV_ER, NOMINAL_PRICE  # noqa: F401  (ETF leg assumptions)
from src.research.ibkr_cost_calibration import base_stock_commission_usd

START_EQUITY = 10_000.0

# ---------------------------------------------------------------- flat ETF model (qqq_timing / regime / calendar)
COMMISSION_PER_SHARE = 0.0035
COMMISSION_MIN = 0.35
COMMISSION_MAX_FRAC = 0.01
FEES_PER_SHARE = 0.0005           # exchange + clearing pass-through, order of magnitude
SELL_REG_FRAC = 0.3e-4            # SEC + FINRA TAF on sells, order of magnitude


def etf_order_cost(value: float, price: float, sell: bool, half_spread: float) -> float:
    """Cost in dollars of one order of ``value`` dollars at ``price``."""
    if value <= 0:
        return 0.0
    shares = value / price
    comm = min(max(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    return comm + FEES_PER_SHARE * shares + half_spread * value + (SELL_REG_FRAC * value if sell else 0.0)


# ---------------------------------------------------------------- IBKR Tiered stock model (reversal_dev)
EXCHANGE_FEE_PER_SHARE = 0.0010    # Nasdaq closing-cross (MOC/LOC) fee order of magnitude, passed through by Tiered
CLEARING_FEE_PER_SHARE = 0.0002    # NSCC/DTC clearing pass-through
PASS_THROUGH_OF_COMMISSION = 0.000175 + 0.00056   # NYSE + FINRA pass-through, fractions of the commission
SEC_FEE_PER_DOLLAR_SOLD = 22.1e-6  # SEC Section 31 fee, roughly $17-23 per $1M sold in 2012-2016; the top used
FINRA_TAF_PER_SHARE_SOLD = 0.000119
FINRA_TAF_MAX = 5.95
# Effective half-spread by dv rank bucket (large-cap Nasdaq names, 2012-2016), floored at half a cent tick.
HALF_SPREAD_BPS = ((50, 2.0), (100, 3.0), (150, 4.0), (200, 5.0), (250, 6.0), (300, 8.0))
HALF_SPREAD_UNRANKED_BPS = 10.0
REBALANCE_BAND = 0.25              # a continuing position is re-traded only when > 25% away from its target


def ibkr_order_cost(shares: float, price: float, sell: bool, hs: float) -> dict:
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


def ibkr_value_cost(value: float, price: float, sell: bool, hs: float) -> float:
    """``ibkr_order_cost`` total for an order given in dollars; 0 for a non-positive value or an unusable price
    (scripts/research_megacap.py ``order_cost``)."""
    if value <= 0 or not np.isfinite(price) or price <= 0:
        return 0.0
    return ibkr_order_cost(value / price, price, sell=sell, hs=hs)["total"]


def rank_half_spread(rank: float, price: float, mult: float = 1.0) -> float:
    """Half-spread (fraction of value) for a name of dollar-volume rank ``rank`` (NaN = unranked)."""
    bps = HALF_SPREAD_UNRANKED_BPS
    if rank == rank:
        for upper, b in HALF_SPREAD_BPS:
            if rank <= upper:
                bps = b
                break
    return max(bps * mult / 1e4 * 1.0, 0.005 / price * mult)


def ibkr_order_total(shares: float, price: float, sell: bool, hs: float) -> float:
    """``ibkr_order_cost`` total in dollars (the ``order_cost`` of research_spinoffs / research_ndx_recon)."""
    return ibkr_order_cost(shares, price, sell, hs)["total"]


def volume_tier_half_spread(dollar_volume: float, price: float, mult: float = 1.0) -> float:
    """Half-spread by the median raw dollar volume of the regular-way sessions so far: 50 / 25 / 10 / 5 bps below
    $2M / $10M / $50M / above (unknown volume: 50 bps), at least half a cent (research_spinoffs ``half_spread``,
    also research_ndx_recon)."""
    if not dollar_volume == dollar_volume or dollar_volume < 2e6:
        bps = 50.0
    elif dollar_volume < 1e7:
        bps = 25.0
    elif dollar_volume < 5e7:
        bps = 10.0
    else:
        bps = 5.0
    return max(bps / 1e4, 0.005 / price) * mult


def tick_half_spread(bps: float, price: float) -> float:
    """``bps`` basis points, but at least half a cent per share."""
    return max(bps / 1e4, 0.005 / price)
