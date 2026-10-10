"""Concentrated mega-cap rules M1-M6 (frozen in docs/research_ledger_megacap.md) and their engine bindings.

``RULES`` / ``Rule`` / ``build_targets`` were extracted unchanged from studies/megacap.py; ``simulate`` /
``buy_hold`` bind the monthly index-unit engine (quant.backtest.rebalance) to the study's $10,000 account, 25% band
and half-spreads, which used to be read from that module's globals (and were overridden temporarily by
research_ml_cross_section; the half-spread is now the ``qqq_hs`` argument). Used by studies/megacap.py,
megacap_oos2 / megacap_oos3, fundamentals, ml_cross_section, index_exclusion and short_overlay.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import costs
from quant.backtest.rebalance import buy_hold_level, simulate_index_units
from quant.data.market_cap import value_at as _value_at
from quant.strategies import registry

ACCOUNT = 10_000.0
ONEQ_HS = 2e-4
QQQ_HS = 1e-4


@dataclass(frozen=True)
class Rule:
    name: str
    pool: int                 # top-N by market cap
    hold: int                 # names held
    mom: str | None = None    # None, "m6" (126 sessions) or "m12_1"
    trend: bool = False       # 100% QQQ when QQQ < SMA200 at the signal
    capw: bool = False        # cap-weighted instead of equal weight


RULES = tuple(Rule(name, **params) for name, params in registry.MEGACAP.items())   # M1-M6, frozen in the registry


def simulate(targets: dict, sessions: pd.DatetimeIndex, idx: pd.DataFrame, close: pd.DataFrame,
             last_row: pd.Series, qqq_idx: pd.Series, qqq_close: pd.Series, account: float = ACCOUNT,
             band: float = costs.REBALANCE_BAND, end: str | None = None, qqq_hs: float = QQQ_HS) -> dict:
    """``simulate_index_units`` with the mega-cap study's account, band and QQQ half-spread."""
    return simulate_index_units(targets, sessions, idx, close, last_row, qqq_idx, qqq_close, account=account,
                                band=band, end=end, qqq_hs=qqq_hs)


def buy_hold(level: pd.Series, price: pd.Series, dates: pd.DatetimeIndex, hs: float, account: float = ACCOUNT):
    return buy_hold_level(level, price, dates, hs, account)


def build_targets(rule: Rule, ranked: pd.DataFrame, mom: dict, qqq_close: pd.Series) -> dict:
    """signal session -> list of (sid, weight, dv50_rank, mcap, mcap_src); 'QQQ' for the trend filter's QQQ leg.
    ``ranked``: candidates with mcap, one row per company. Signals with too few names are skipped."""
    sma = qqq_close.rolling(200, min_periods=200).mean()
    out = {}
    for s, g in ranked.groupby("s"):
        g = g[g["mcap"].notna()].sort_values(["mcap", "security_id"], ascending=[False, True]).head(rule.pool)
        if len(g) < rule.pool:
            continue
        if rule.mom is not None:
            m = mom[rule.mom]
            g = g.assign(mom=_value_at(m, g["security_id"], [s] * len(g)))
            if g["mom"].isna().all():
                continue
            g = g[g["mom"].notna()].sort_values(["mom", "security_id"], ascending=[False, True]).head(rule.hold)
            if len(g) < rule.hold:
                continue
        if rule.trend:
            if not np.isfinite(sma.loc[s]):
                continue
            if qqq_close.loc[s] < sma.loc[s]:
                out[s] = [("QQQ", 1.0, np.nan, np.nan, "qqq")]
                continue
        w = (g["mcap"] / g["mcap"].sum()).to_numpy() if rule.capw else np.full(len(g), 1.0 / len(g))
        out[s] = [(r.security_id, float(wi), float(r.dv50_rank), float(r.mcap), r.mcap_src)
                  for r, wi in zip(g.itertuples(), w)]
    return out
