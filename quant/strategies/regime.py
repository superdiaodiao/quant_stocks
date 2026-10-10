"""Regime-switching allocation across long-only ETFs: the rule signals and target-weight tables of studies/regime.py
and its data checks.

Extracted unchanged from studies/regime.py in the slimming pass (``monthly_only``, ``RuleSpec``,
``target_weights``, ``data_checks``). The window lengths, formerly the study's module defaults of its signal
wrappers, are ``RegimeParams``; the signals are quant.signals.technical; the engine is
``quant.backtest.target_weights``; the data are ``quant.data.etf_panel``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.target_weights import two_state
from quant.data.calendar import month_end_mask
from quant.data.etf_panel import Data
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.metrics import cagr_of
from quant.signals.technical import inverse_vol_weights, sma_state, trailing_compound, trailing_return, vol_state


@dataclass(frozen=True)
class RegimeParams:
    sma_len: int                          # R1 / R4 trend: SMA length
    mom_len: int                          # R2 / R2b: 12-month momentum in sessions
    vol_len: int                          # R3: realised-vol window
    vol_med_len: int                      # R3: trailing median of that vol
    rp_len: int                           # R5: inverse-vol window


# ======================================================================== data checks

def data_checks(d: Data, prior_qqq: Path, end: str) -> dict:
    """Adjusted close vs close + dividends per ETF, BIL vs the Ken French RF, QQQ vs an earlier cache (if present)."""
    out = {}
    for t, df in d.raw.items():
        a = df["adjclose"].pct_change()
        c = (df["close"] + df["dividend"]) / df["close"].shift(1) - 1
        diff = (a - c).dropna()
        first = pd.Timestamp(df["date"].iloc[0])
        listed = d.sessions[d.sessions >= first]
        out[t] = {"first": df["date"].iloc[0], "missing_sessions_after_listing": int(d.adj[t].reindex(listed).isna().sum()),
                  "max_abs_adj_vs_close_div": float(diff.abs().max()), "dividends": int((df["dividend"] > 0).sum()),
                  "max_abs_daily_return": float(a.abs().max())}
    bil = d.rets["BIL"].dropna()
    rf = d.rf.reindex(bil.index)
    out["BIL_vs_KF_rf_annualised"] = {"bil": cagr_of(bil), "kf_rf": cagr_of(rf), "window": [str(bil.index[0].date()),
                                                                                          str(bil.index[-1].date())]}
    if prior_qqq.exists():
        p = parse_chart(prior_qqq, end)
        pr = pd.Series(p["adjclose"].values, index=pd.DatetimeIndex(p["date"])).pct_change()
        j = pd.concat([pr, d.rets["QQQ"]], axis=1, join="inner").dropna()
        out["QQQ_vs_prior_cache"] = {"sessions": int(len(j)), "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())}
    return out


# ======================================================================== signals (data up to and including t)

def monthly_only(w: pd.DataFrame) -> pd.DataFrame:
    """Keep the month-end decisions and carry them until the next month end."""
    me = month_end_mask(w.index)
    keep = np.repeat(me[:, None], w.shape[1], axis=1)
    return w.where(keep, np.nan).ffill()


@dataclass(frozen=True)
class RuleSpec:
    name: str
    assets: tuple          # every ETF the rule can hold
    monthly: bool
    description: str


def target_weights(name: str, d: Data, cols: tuple, p: RegimeParams) -> pd.DataFrame:
    """Target weights of rule ``name`` over the ETFs ``cols`` decided at each close t (NaN rows = no decision
    possible yet)."""
    a = d.adj
    if name in ("R1", "R1b"):
        alt = "IEF" if name == "R1" else "USMV"
        return two_state(sma_state(a["QQQ"], p.sma_len), {"QQQ": 1.0}, {alt: 1.0}, cols)
    if name == "R2":
        q, tb = trailing_return(a["QQQ"], p.mom_len), trailing_compound(d.rf, p.mom_len)
        st = (q > tb).astype(float).where(q.notna() & tb.notna())
        return monthly_only(two_state(st, {"QQQ": 1.0}, {"IEF": 1.0}, cols))
    if name == "R2b":
        q, s, tb = (trailing_return(a["QQQ"], p.mom_len), trailing_return(a["SPY"], p.mom_len),
                    trailing_compound(d.rf, p.mom_len))
        ok = q.notna() & s.notna() & tb.notna()
        w = pd.DataFrame(np.nan, index=a.index, columns=list(cols))
        best_q = q >= s
        best_ret = q.where(best_q, s)
        w.loc[ok, :] = 0.0
        w.loc[ok & best_q & (best_ret > tb), "QQQ"] = 1.0
        w.loc[ok & ~best_q & (best_ret > tb), "SPY"] = 1.0
        w.loc[ok & ~(best_ret > tb), "IEF"] = 1.0
        return monthly_only(w)
    if name in ("R3", "R3b"):
        st = vol_state(d.rets["QQQ"], p.vol_len, p.vol_med_len)
        off = {"USMV": 1.0} if name == "R3" else {"QQQ": 0.5, "IEF": 0.5}
        return two_state(st, {"QQQ": 1.0}, off, cols)
    if name == "R4":
        return two_state(sma_state(a["SPY"], p.sma_len), {"MTUM": 1.0}, {"USMV": 1.0}, cols)
    if name == "R5":
        return monthly_only(inverse_vol_weights(d.rets[["QQQ", "IEF", "GLD"]], p.rp_len))
    raise KeyError(name)
