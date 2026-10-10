"""Metrics of a strategy against ONEQ (and QQQ) over a judged period, and the two-fold split.

- ``halves``: the two-fold split of a return index (first half / second half by session count).
- ``return_period_metrics``: from aligned daily returns (scripts/research_regime.py ``period_metrics``).
- ``nav_window_metrics``: from dollar NAV series over a calendar window, rebased at the last session before it
  (scripts/research_megacap.py ``window_metrics``).

Both extracted unchanged; the key names are the ones the ledgers quote.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.metrics import TRADING_DAYS, cagr_of, longest_drawdown_days, max_drawdown, monthly, yearly


def halves(index: pd.DatetimeIndex) -> tuple:
    n = len(index)
    return index[: n // 2], index[n // 2:]


def return_period_metrics(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    """Strategy returns ``r`` vs ONEQ ``b`` and QQQ ``q`` (same index); Sharpe on excess over ``rf``."""
    v = (1 + r).cumprod()
    vb, vq = (1 + b).cumprod(), (1 + q).cumprod()
    v0 = pd.concat([pd.Series([1.0]), v.reset_index(drop=True)])
    vb0 = pd.concat([pd.Series([1.0]), vb.reset_index(drop=True)])
    vq0 = pd.concat([pd.Series([1.0]), vq.reset_index(drop=True)])
    ex = r - rf.reindex(r.index)
    mx = monthly(r) - monthly(b)
    cagr, bc, qc = cagr_of(r), cagr_of(b), cagr_of(q)
    dd, bdd, qdd = max_drawdown(v0), max_drawdown(vb0), max_drawdown(vq0)
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
            "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "cagr_minus_oneq": cagr - bc, "cagr_minus_qqq": cagr - qc,
            "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
            "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100, "dd_shallower_than_qqq_pp": (abs(qdd) - abs(dd)) * 100,
            "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "months": int(len(mx)), "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(len(mx)))}


def nav_window_metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, a: str, b: str,
                       cost: pd.Series | None = None, traded: pd.Series | None = None,
                       orders: pd.Series | None = None) -> dict | None:
    """Metrics of ``nav`` over [a, b] vs the ONEQ and QQQ value series, rebased at the last session before a (or
    the first session when the series starts inside the window). None when fewer than 40 sessions remain.
    With ``cost`` / ``traded`` / ``orders`` (dollars per session) also cost drag, turnover and order count."""
    a_, b_ = pd.Timestamp(a), pd.Timestamp(b)
    before = nav.index[nav.index < a_]
    t0 = before[-1] if len(before) else nav.index[0]
    keep = (nav.index >= t0) & (nav.index <= b_)
    if keep.sum() < 40:
        return None
    v, vb, vq = nav[keep], oneq.reindex(nav.index)[keep], qqq.reindex(nav.index)[keep]
    r, rb, rq = v.pct_change().iloc[1:], vb.pct_change().iloc[1:], vq.pct_change().iloc[1:]
    years = (v.index[-1] - v.index[0]).days / 365.25
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / years) - 1
    bc = (vb.iloc[-1] / vb.iloc[0]) ** (1 / years) - 1
    qc = (vq.iloc[-1] / vq.iloc[0]) ** (1 / years) - 1
    dd, bdd, qdd = max_drawdown(v), max_drawdown(vb), max_drawdown(vq)
    act = r - rb
    mx = monthly(r) - monthly(rb)
    mq = monthly(r) - monthly(rq)
    actq = r - rq
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    alpha = float((r.mean() - beta * rb.mean()) * 252)
    down = np.sqrt((np.minimum(r, 0) ** 2).mean()) * math.sqrt(252)
    ys, yb = yearly(r), yearly(rb)
    m = monthly(r)
    out = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "years": round(years, 2),
           "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "excess_vs_oneq": cagr - bc, "excess_vs_qqq": cagr - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
           "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100,
           "longest_dd_days": longest_drawdown_days(v), "oneq_longest_dd_days": longest_drawdown_days(vb),
           "sharpe": float(r.mean() / r.std(ddof=1) * math.sqrt(252)),
           "oneq_sharpe": float(rb.mean() / rb.std(ddof=1) * math.sqrt(252)),
           "sortino": float(r.mean() * 252 / down) if down > 0 else np.nan,
           "calmar": cagr / abs(dd) if dd < 0 else np.nan,
           "ir": float(act.mean() / act.std(ddof=1) * math.sqrt(252)) if act.std() > 0 else np.nan,
           "tracking_error": float(act.std(ddof=1) * math.sqrt(252)),
           "beta_vs_oneq": beta, "alpha_vs_oneq_ann": alpha,
           "months": int(len(mx)), "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std(ddof=1) * math.sqrt(len(mx)))
           if mx.std() > 0 else np.nan,
           "t_monthly_excess_vs_qqq": float(mq.mean() / mq.std(ddof=1) * math.sqrt(len(mq))) if mq.std() > 0 else np.nan,
           "ir_vs_qqq": float(actq.mean() / actq.std(ddof=1) * math.sqrt(252)) if actq.std() > 0 else np.nan,
           "years_beating_qqq": int((ys > yearly(rq)).sum()),
           "years_beating_oneq": int((ys > yb).sum()), "n_years": int(len(ys)),
           "share_years_beating_oneq": float((ys > yb).mean()),
           "worst_year": float(ys.min()), "worst_year_label": int(ys.idxmin()),
           "worst_month": float(m.min()), "worst_month_label": str(m.idxmin())}
    if cost is not None:
        sel = (cost.index > t0) & (cost.index <= b_)
        prev = nav.shift(1).reindex(cost.index)[sel]
        out["cost_drag_per_year"] = float((cost[sel] / prev).sum() / years)
        out["cost_usd"] = float(cost[sel].sum())
        out["turnover_one_way_per_year"] = float(traded[sel].sum() / 2 / v.mean() / years)
        out["orders"] = int(orders[sel].sum())
    return out
