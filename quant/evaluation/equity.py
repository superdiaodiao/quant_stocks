"""Equity-curve metrics of the ETF leverage studies, from daily returns rebased to 1.0 at the entry close.

Extracted unchanged from scripts/research_sector_lev.py (``equity0``, ``longest_drawdown`` (here
``longest_underwater``: strict ``v < peak``, unlike ``metrics.longest_drawdown``), ``ulcer_index``, ``full_years``,
``period_metrics``); also used by research_voltarget and research_leverage_methods. The verdict on (full, H1, H2)
is ``quant.evaluation.criteria.ab_verdict``.

Added in the slimming pass, each from one study and unchanged (the docstrings state the conventions):
``window_stats`` / ``drawdown_episode`` (sector_lev crash windows), ``t_monthly_excess`` (voltarget's ``tstat_diff``:
no spread guard, unlike ``metrics.t_and_ir``) / ``mm_alpha`` (Moreira-Muir OLS alpha, unlike ``stats.ols_alpha``: no
NaN handling, key ``t_alpha``), ``drawdown_info`` / ``worst_window`` (leverage_methods value paths; the longest
underwater stretch there is in years).
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


# ---- crash windows and drawdown episodes (from studies/sector_lev.py in the slimming pass)

def window_stats(r: pd.Series, start: str, end: str) -> dict | None:
    """Total return and max drawdown (``equity0`` curve) of daily returns ``r`` over [start, end]; None below 20
    sessions."""
    x = r.loc[start:end]
    if len(x) < 20:
        return None
    return {"start": str(x.index[0].date()), "end": str(x.index[-1].date()), "total": float(np.prod(1 + x) - 1),
            "max_dd": max_drawdown(equity0(x))}


def drawdown_episode(v: pd.Series, start: str, end: str, peak_from: str | None = None) -> dict | None:
    """Deepest drawdown of the full curve whose trough falls in [start, end]: peak, trough, recovery.
    ``peak_from``: only peaks on/after this date count (a crash measured from its own local high)."""
    if peak_from is not None:
        v = v.loc[peak_from:]
    dd = v / v.cummax() - 1
    win = dd.loc[start:end]
    if len(win) < 20:
        return None
    trough = win.idxmin()
    peak_val = v.loc[:trough].max()
    peak = v.loc[:trough].idxmax()
    after = v.loc[trough:]
    rec = after[after >= peak_val]
    rec_dt = rec.index[0] if len(rec) else None
    yrs = lambda a, b: (b - a).days / 365.25
    return {"peak": str(peak.date()), "trough": str(trough.date()), "depth": float(win.min()),
            "recovered": str(rec_dt.date()) if rec_dt is not None else "not recovered",
            "years_underwater_peak_to_recovery": yrs(peak, rec_dt) if rec_dt is not None else
            f">{yrs(peak, v.index[-1]):.1f}",
            "years_trough_to_recovery": yrs(trough, rec_dt) if rec_dt is not None else f">{yrs(trough, v.index[-1]):.1f}"}


# ---- monthly excess t and the Moreira-Muir alpha (from studies/voltarget.py in the slimming pass)

def t_monthly_excess(a: pd.Series, b: pd.Series) -> float:
    """t of the mean monthly excess of daily returns ``a`` over ``b``: mean / sd(ddof=1) * sqrt(n) of
    ``monthly(a) - monthly(b)``, no spread guard (NaN, with a numpy warning, when there is no spread)."""
    x = monthly(a) - monthly(b)
    return float(x.mean() / x.std() * math.sqrt(len(x)))


def mm_alpha(r: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    """Moreira-Muir regression: monthly (rule - rf) on (QQQ - rf); annualised alpha (x 12), its OLS t
    (``t_alpha``) and beta. No NaN handling or minimum sample, unlike ``quant.evaluation.stats.ols_alpha``."""
    mrf = monthly(rf.reindex(r.index))
    y = (monthly(r) - mrf).values
    x = (monthly(q) - mrf).values
    X = np.column_stack([np.ones(len(x)), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ coef
    s2 = resid @ resid / (len(y) - 2)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(X.T @ X)))
    return {"alpha_ann": float(coef[0] * 12), "t_alpha": float(coef[0] / se[0]), "beta": float(coef[1])}


# ---- value-path drawdown details (from studies/leverage_methods.py in the slimming pass)

def drawdown_info(v: pd.Series) -> dict:
    """Max drawdown of a value path with its peak / trough / recovery dates, years from peak to recovery (or
    underwater so far) and the longest underwater stretch in years (strict ``v < peak`` sessions / 252)."""
    peak = v.cummax()
    dd = v / peak - 1
    trough = dd.idxmin()
    pk = v.loc[:trough].idxmax()
    after = v.loc[trough:]
    rec = after[after >= v.loc[pk]]
    yrs = lambda a, b: (b - a).days / 365.25
    under = (v < peak).values
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return {"max_dd": float(dd.min()), "dd_peak": str(pk.date()), "dd_trough": str(trough.date()),
            "dd_recovered": str(rec.index[0].date()) if len(rec) else "not recovered",
            "years_peak_to_recovery": yrs(pk, rec.index[0]) if len(rec) else float("nan"),
            "years_underwater_so_far": float("nan") if len(rec) else yrs(pk, v.index[-1]),
            "longest_underwater_years": best / TRADING_DAYS}


def worst_window(v: pd.Series, n: int) -> float:
    """Worst ``n``-session return of a value path (-1 where the start value is not positive; NaN if too short)."""
    x = v.values
    if len(x) <= n:
        return float("nan")
    with np.errstate(divide="ignore", invalid="ignore"):
        r = x[n:] / x[:-n] - 1
    r = np.where(x[:-n] > 0, r, -1.0)
    return float(np.nanmin(r))
