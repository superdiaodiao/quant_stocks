"""Sector ETF momentum rotation (part A) and moderate QQQ leverage (part B): the signals and target tables of
studies/sector_lev.py, and its crash-window risk report.

Extracted unchanged from studies/sector_lev.py in the slimming pass (``month_end_prices``, ``momentum``,
``top_n_weights``, ``faber_state``, ``to_daily``, ``part_a_targets``, ``exposure_weights``, ``part_b_targets``,
``risk_report``; ``window_stats`` / ``drawdown_episode`` went to quant.evaluation.equity). The part-B exposures,
the SMA length / band and the risk windows are arguments. Data: ``quant.data.sector_etfs``; engine:
``quant.backtest.flagged_weights``.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant.data.calendar import month_end_mask
from quant.data.sector_etfs import LEVERAGE_LEGS, SECTORS
from quant.evaluation.equity import drawdown_episode, equity0, window_stats
from quant.signals.technical import trend_state


# ======================================================================== Part A signals (month-end data up to t)

def month_end_prices(adj: pd.DataFrame) -> pd.DataFrame:
    return adj[month_end_mask(adj.index)]


def momentum(me: pd.DataFrame, kind: str) -> pd.DataFrame:
    """kind '12-1': M_1 / M_12 - 1; kind '6': M_0 / M_6 - 1 (month-end rows; NaN when the window start is missing)."""
    if kind == "12-1":
        return me.shift(1) / me.shift(12) - 1
    if kind == "6":
        return me / me.shift(6) - 1
    raise KeyError(kind)


def top_n_weights(mom: pd.DataFrame, n: int, need: int = 9) -> pd.DataFrame:
    """Equal weight on the n highest scores (ties by ticker); NaN rows until ``need`` ETFs can be ranked."""
    w = pd.DataFrame(np.nan, index=mom.index, columns=mom.columns)
    for dt, row in mom.iterrows():
        row = row.dropna()
        if len(row) < need:
            continue
        order = sorted(row.index, key=lambda t: (-row[t], t))[:n]
        w.loc[dt] = 0.0
        w.loc[dt, order] = 1.0 / n
    return w


def faber_state(me_spy: pd.Series, months: int = 10) -> pd.Series:
    sma = me_spy.rolling(months, min_periods=months).mean()
    return (me_spy > sma).astype(float).where(sma.notna())


def to_daily(w_me: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    """Month-end targets carried forward daily, plus the rebalance flag (True on month ends with a target)."""
    w = w_me.reindex(sessions).ffill()
    flag = pd.Series(False, index=sessions)
    flag.loc[w_me.dropna(how="all").index] = True
    return w, flag


LEGS_A = list(SECTORS) + ["IEFX"]


def part_a_targets(name: str, adj: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    me = month_end_prices(adj[list(SECTORS)])
    kind, n = {"A1": ("12-1", 3), "A2": ("12-1", 1), "A3": ("6", 3), "A4": ("12-1", 3)}[name]
    w = top_n_weights(momentum(me, kind), n)
    w["IEFX"] = 0.0
    w.loc[w[list(SECTORS)].isna().all(axis=1), "IEFX"] = np.nan
    if name == "A4":
        st = faber_state(month_end_prices(adj[["SPY"]])["SPY"]).reindex(w.index)
        off = st == 0.0
        w.loc[off & w["IEFX"].notna(), :] = 0.0
        w.loc[off & w["IEFX"].notna(), "IEFX"] = 1.0
        w.loc[st.isna(), :] = np.nan
    return to_daily(w[LEGS_A], adj.index)


# ======================================================================== Part B signals

LEGS_B = LEVERAGE_LEGS


def exposure_weights(e: float) -> dict:
    if e >= 1.0:
        return {"QQQ": 2.0 - e, "QLDX": e - 1.0, "CASH": 0.0}
    return {"QQQ": e, "QLDX": 0.0, "CASH": 1.0 - e}


def part_b_targets(name: str, p_qqq: pd.Series, exposure: dict, sma_len: int, band: float
                   ) -> tuple[pd.DataFrame, pd.Series]:
    """Rule ``name`` with (on, off) = ``exposure[name]``: on / off by the QQQ SMA(``sma_len``) trend state with a
    ``band`` (constant exposure when on == off); rebalance at each month end and on state changes."""
    on, off = exposure[name]
    idx = p_qqq.index
    me = pd.Series(month_end_mask(idx), index=idx)
    if on == off:
        st = pd.Series(1.0, index=idx)
    else:
        st = trend_state(p_qqq, sma_len, band)
    w = pd.DataFrame(np.nan, index=idx, columns=LEGS_B)
    for k in LEGS_B:
        w.loc[st == 1.0, k] = exposure_weights(on)[k]
        w.loc[st == 0.0, k] = exposure_weights(off)[k]
    change = st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()
    return w, (me | change) & st.notna()


# ======================================================================== risk report

def risk_report(r: pd.Series, risk_windows: dict, local_peak_from: dict) -> dict:
    """Window total return and the deepest drawdown episode with its trough in each of ``risk_windows`` (plus the
    episode measured from a local peak for the windows in ``local_peak_from``)."""
    v = equity0(r)
    out = {}
    for tag, (s, e) in risk_windows.items():
        w = window_stats(r, s, e)
        out[tag] = {"window_total": w["total"] if w else None, "episode": drawdown_episode(v, s, e)}
        if tag in local_peak_from:
            out[f"{tag}_from_local_peak"] = {"window_total": w["total"] if w else None,
                                             "episode": drawdown_episode(v, s, e, local_peak_from[tag])}
    return out
