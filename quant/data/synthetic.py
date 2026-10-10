"""Synthetic fund returns: a daily-rebalanced 2x of an index before the real fund lists, and the fee / price
assumptions of the synthetic legs (T-bill ETF, 2x fund).

Extracted unchanged from scripts/research_qqq_timing.py (``synthetic_2x``). The three constants are also
exported by quant.backtest.costs (from scripts/research_qqq_timing.py).
"""
from __future__ import annotations

import pandas as pd

TRADING_DAYS = 252
LEV_ER = 0.0095                   # QLD expense ratio, per year
CASH_ETF_FEE = 0.0010             # BIL / SGOV-like expense ratio, per year
NOMINAL_PRICE = 50.0              # share price assumed where no real close is used (QLD, T-bill ETF, NDX era)


def synthetic_2x(r1: pd.Series, rf: pd.Series, spread: float, er: float = LEV_ER) -> pd.Series:
    """Daily-rebalanced 2x: 2 * r - (rf_daily + spread / 252) - er / 252 (one unit of notional is borrowed)."""
    return 2.0 * r1 - (rf + spread / TRADING_DAYS) - er / TRADING_DAYS
