"""Volatility targeting on QQQ (Moreira-Muir style): the exposure rules V1-V4b of studies/voltarget.py, their
QQQ / 2x-leg / T-bill weights, the monthly and daily-band target tables and the comparison with a constant exposure.

Extracted unchanged from studies/voltarget.py in the slimming pass (``expanding_vol``, ``daily_exposure``,
``weights_from_exposure``, ``targets``, ``banded_targets``, ``held_exposure``, ``constant_targets``,
``vs_constant``, ``exposure_stats``). The frozen volatility target, windows and SMA band are ``VolParams``, given by
the study. Data: ``quant.data.sector_etfs``; engine: ``quant.backtest.flagged_weights``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.data import sector_etfs as etfs
from quant.data.calendar import month_end_mask
from quant.evaluation.equity import equity0, t_monthly_excess
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown
from quant.signals.technical import realised_vol, trend_state

LEGS = etfs.LEVERAGE_LEGS                 # QQQ, QLDX (2x leg), CASH (T-bill ETF)


@dataclass(frozen=True)
class VolParams:
    target_vol: float                     # V1 / V2 / V4 volatility target
    vol_short: int                        # realised-vol window (vol20)
    vol_quarter: int                      # V1b window (vol63)
    vol_lr_min: int                       # minimum returns of the expanding (long-run) vol of V3
    sma_len: int                          # V4 trend state: SMA length and band
    sma_band: float


# ======================================================================== signals (data up to and including t)

def expanding_vol(r: pd.Series, min_obs: int) -> pd.Series:
    """Std of every daily return from the first one up to and including t, annualised."""
    return r.expanding(min_periods=min_obs).std() * math.sqrt(TRADING_DAYS)


def daily_exposure(name: str, r_qqq: pd.Series, p_qqq: pd.Series, p: VolParams) -> tuple[pd.Series, pd.Series]:
    """Exposure the rule would choose at each close t, and the trend-state change flag (V4 only, else all False)."""
    v20 = realised_vol(r_qqq, p.vol_short)
    no_change = pd.Series(False, index=r_qqq.index)
    if name == "V1":
        return (p.target_vol / v20).clip(upper=1.0), no_change
    if name == "V1b":
        return (p.target_vol / realised_vol(r_qqq, p.vol_quarter)).clip(upper=1.0), no_change
    if name in ("V2", "V2b"):
        return (p.target_vol / v20).clip(upper=1.5 if name == "V2" else 2.0), no_change
    if name in ("V3", "V3b"):
        return (expanding_vol(r_qqq, p.vol_lr_min) / v20).clip(upper=1.5 if name == "V3" else 1.0), no_change
    if name in ("V4", "V4b"):
        st = trend_state(p_qqq, p.sma_len, p.sma_band)
        on = (p.target_vol / v20).clip(upper=1.5)
        off = (p.target_vol / v20).clip(upper=1.0) if name == "V4" else pd.Series(0.0, index=r_qqq.index).where(v20.notna())
        e = on.where(st == 1.0, off).where(st.notna() & v20.notna())
        change = st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()
        return e, change
    raise KeyError(name)


def weights_from_exposure(e: pd.Series) -> pd.DataFrame:
    w = pd.DataFrame(np.nan, index=e.index, columns=LEGS)
    ok = e.notna()
    ev = e[ok].values
    w.loc[ok, "QQQ"] = np.where(ev >= 1.0, 2.0 - ev, ev)
    w.loc[ok, "QLDX"] = np.where(ev >= 1.0, ev - 1.0, 0.0)
    w.loc[ok, "CASH"] = np.where(ev >= 1.0, 0.0, 1.0 - ev)
    return w


def targets(name: str, d: etfs.Data, p: VolParams) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Monthly decisions (plus V4 state changes) carried forward; returns (weights, rebalance flag, exposure)."""
    e, change = daily_exposure(name, d.rets["QQQ"], d.p_qqq, p)
    me = pd.Series(month_end_mask(e.index), index=e.index)
    decide = (me | change) & e.notna()
    e_t = e.where(decide).ffill()
    return weights_from_exposure(e_t), decide, e_t


def banded_targets(name: str, d: etfs.Data, p: VolParams, band: float) -> tuple[pd.DataFrame, pd.Series, pd.Series]:
    """Report-only: decide every day, act only when |e - last acted e| > band * last acted e (or on a state change)."""
    e, change = daily_exposure(name, d.rets["QQQ"], d.p_qqq, p)
    ev, ch = e.values, change.values
    out = np.full(len(e), np.nan)
    last = np.nan
    for i in range(len(e)):
        if np.isnan(ev[i]):
            if not np.isnan(last):
                out[i] = last
            continue
        if np.isnan(last) or ch[i] or abs(ev[i] - last) > band * last:
            last = ev[i]
        out[i] = last
    e_t = pd.Series(out, index=e.index)
    return weights_from_exposure(e_t), pd.Series(False, index=e.index), e_t


def held_exposure(held: pd.DataFrame) -> pd.Series:
    return held["QQQ"] + 2.0 * held["QLDX"]


def constant_targets(e_bar: float, index: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    w = weights_from_exposure(pd.Series(e_bar, index=index))
    return w, pd.Series(month_end_mask(index), index=index)


# ======================================================================== constant-exposure comparison

def vs_constant(r: pd.Series, c: pd.Series, i1, i2) -> dict:
    out = {}
    for tag, ix in (("full", r.index), ("half1", i1), ("half2", i2)):
        rr, cc = r.loc[ix], c.loc[ix]
        out[tag] = {"cagr": cagr_of(rr), "const_cagr": cagr_of(cc), "cagr_minus_const": cagr_of(rr) - cagr_of(cc),
                    "max_dd": max_drawdown(equity0(rr)), "const_max_dd": max_drawdown(equity0(cc)),
                    "t_monthly_excess_vs_const": t_monthly_excess(rr, cc)}
    f = out["full"]
    out["timing_adds_value"] = bool(f["t_monthly_excess_vs_const"] >= 2.0 and out["half1"]["cagr_minus_const"] > 0
                                    and out["half2"]["cagr_minus_const"] > 0)
    return out


def exposure_stats(e_held: pd.Series, e_tgt: pd.Series, cap: float | None) -> dict:
    out = {"mean": float(e_held.mean()), "min": float(e_held.min()), "median": float(e_held.median()),
           "max": float(e_held.max()), "target_mean": float(e_tgt.mean())}
    if cap is not None:
        out["share_target_at_cap"] = float((e_tgt >= cap - 1e-12).mean())
    out["share_target_zero"] = float((e_tgt <= 1e-12).mean())
    return out
