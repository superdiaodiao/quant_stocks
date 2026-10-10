"""Equity-curve metrics of the ETF leverage studies, from daily returns rebased to 1.0 at the entry close.

Extracted unchanged from scripts/research_sector_lev.py (``equity0``, ``longest_drawdown`` (here
``longest_underwater``: strict ``v < peak``, unlike ``metrics.longest_drawdown``), ``ulcer_index``, ``full_years``,
``period_metrics``); also used by research_voltarget and research_leverage_methods. The verdict on (full, H1, H2)
is ``quant.evaluation.criteria.ab_verdict``.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly


def equity0(r: pd.Series) -> pd.Series:
    """Equity curve starting at 1.0 on the entry close (index: entry date + return dates)."""
    v = (1 + r).cumprod()
    start = r.index[0] - pd.Timedelta(days=1)
    return pd.concat([pd.Series([1.0], index=[start]), v])


def longest_underwater(v: pd.Series) -> int:
    """Longest stretch (sessions) from a peak until the curve regains it (or the end of the data)."""
    peak = v.cummax()
    under = (v < peak).values
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return int(best)


def ulcer_index(v: pd.Series) -> float:
    dd = (v / v.cummax() - 1) * 100
    return float(np.sqrt((dd ** 2).mean()))


def full_years(r: pd.Series, min_sessions: int = 240) -> list:
    n = r.groupby(r.index.year).size()
    return [int(y) for y, k in n.items() if k >= min_sessions]


def period_metrics(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    v, vb, vq = equity0(r), equity0(b), equity0(q)
    rfr = rf.reindex(r.index)
    ex = r - rfr
    down = np.sqrt((np.minimum(ex, 0) ** 2).mean())
    cagr, bc, qc = cagr_of(r), cagr_of(b), cagr_of(q)
    dd, bdd, qdd = max_drawdown(v), max_drawdown(vb), max_drawdown(vq)
    mr, mb, mq, mrf = monthly(r), monthly(b), monthly(q), monthly(rfr)
    mx, mxq = mr - mb, mr - mq
    y, xb = (mr - mrf).values, (mb - mrf).values
    beta = float(np.cov(y, xb, ddof=1)[0, 1] / np.var(xb, ddof=1)) if len(y) > 2 else float("nan")
    alpha_m = float(y.mean() - beta * xb.mean())
    yr, yb = yearly(r), yearly(b)
    fy = full_years(r)
    yrs = yr.loc[fy] if fy else yr
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
            "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "cagr_minus_oneq": cagr - bc, "cagr_minus_qqq": cagr - qc,
            "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
            "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
            "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100, "dd_shallower_than_qqq_pp": (abs(qdd) - abs(dd)) * 100,
            "longest_dd_sessions": longest_underwater(v), "oneq_longest_dd_sessions": longest_underwater(vb),
            "ulcer": ulcer_index(v), "oneq_ulcer": ulcer_index(vb),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "oneq_sharpe": float((b - rfr).mean() / (b - rfr).std() * math.sqrt(TRADING_DAYS)),
            "sortino": float(ex.mean() / down * math.sqrt(TRADING_DAYS)) if down > 0 else float("nan"),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "oneq_calmar": bc / abs(bdd) if bdd < 0 else float("nan"),
            "months": int(len(mx)),
            "ir_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(12)),
            "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(len(mx))),
            "t_monthly_excess_vs_qqq": float(mxq.mean() / mxq.std() * math.sqrt(len(mxq))),
            "beta_vs_oneq": beta, "alpha_ann_vs_oneq": alpha_m * 12,
            "full_years": len(fy), "share_years_beating_oneq": float((yr.loc[fy] > yb.loc[fy]).mean()) if fy else float("nan"),
            "worst_year": int(yrs.idxmin()), "worst_year_ret": float(yrs.min()),
            "worst_month": str(mr.idxmin()), "worst_month_ret": float(mr.min())}
