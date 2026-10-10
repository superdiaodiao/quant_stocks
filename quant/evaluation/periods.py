"""Metrics of a strategy against ONEQ (and QQQ) over a judged period, and the two-fold split.

- ``halves``: the two-fold split of a return index (first half / second half by session count).
- ``return_period_metrics``: from aligned daily returns (scripts/research_regime.py ``period_metrics``).
- ``nav_window_metrics``: from dollar NAV series over a calendar window, rebased at the last session before it
  (scripts/research_megacap.py ``window_metrics``).
- ``segment_metrics``: from dollar NAV series over [start, end] rebased at the last close before start, with
  session-count years (252 a year), Sharpe / Sortino / alpha on excess over a daily risk-free rate and the trade
  statistics of an engine result (studies/sec_alt.py ``period_metrics``); its underwater run is
  ``longest_underwater_sessions``.

All extracted unchanged; the key names are the ones the ledgers quote.
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


def longest_underwater_sessions(v: pd.Series) -> int:
    """Longest run of consecutive sessions with the value strictly below its running peak (no tolerance, unlike
    ``metrics.longest_drawdown``)."""
    under = (v / v.cummax() - 1) < 0
    best = run = 0
    for u in under.to_numpy():
        run = run + 1 if u else 0
        best = max(best, run)
    return int(best)


def segment_metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, rf: pd.Series, start: str, end: str,
                    res: dict | None = None, *, account_start: str, account: float) -> dict:
    """Metrics for sessions in [start, end]; the base is the last close before ``start`` (the series must have
    one). Years = sessions / 252. With an engine result ``res`` (nav, cost, exposure, n_positions, traded_day,
    trades with entry / ret / pnl) also trade statistics, cost drag and turnover; the segment whose base is the
    ``account_start`` close (where ``account`` dollars start) owns the trades made at that close."""
    def seg(s):
        s0 = s[s.index < pd.Timestamp(start)].iloc[-1:]
        return pd.concat([s0, s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]])
    v, b, q = seg(nav), seg(oneq), seg(qqq)
    r, rb, rq = v.pct_change().iloc[1:], b.pct_change().iloc[1:], q.pct_change().iloc[1:]
    f = rf.reindex(r.index).fillna(0.0)
    yrs = len(r) / 252.0
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / yrs) - 1
    bc = (b.iloc[-1] / b.iloc[0]) ** (1 / yrs) - 1
    qc = (q.iloc[-1] / q.iloc[0]) ** (1 / yrs) - 1
    dd, bdd, qdd = max_drawdown(v), max_drawdown(b), max_drawdown(q)
    ex = r - f
    down = ex[ex < 0]
    a = r - rb
    m, mb = monthly(r), monthly(rb)
    mx = m - mb
    y, yb = yearly(r), yearly(rb)
    beta = float(np.cov(r, rb, ddof=1)[0, 1] / rb.var(ddof=1))
    alpha = float(((r - f) - beta * (rb - f)).mean() * 252)
    out = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
           "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "excess_vs_oneq": cagr - bc, "excess_vs_qqq": cagr - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
           "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100,
           "longest_dd_sessions": longest_underwater_sessions(v),
           "oneq_longest_dd_sessions": longest_underwater_sessions(b),
           "sharpe": float(ex.mean() / ex.std(ddof=1) * math.sqrt(252)) if ex.std() > 0 else float("nan"),
           "sortino": float(ex.mean() * 252 / (math.sqrt((down ** 2).sum() / len(ex)) * math.sqrt(252)))
           if len(down) else float("nan"),
           "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
           "ir": float(a.mean() / a.std(ddof=1) * math.sqrt(252)) if a.std() > 0 else float("nan"),
           "beta_vs_oneq": beta, "alpha_vs_oneq_ann": alpha,
           "months": int(len(mx)), "monthly_excess_mean": float(mx.mean()),
           "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std(ddof=1) * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
           "years_beating_oneq": f"{int((y > yb).sum())}/{len(y)}", "share_years_beating_oneq": float((y > yb).mean()),
           "worst_year": float(y.min()), "worst_year_label": int(y.idxmin()), "worst_month": float(m.min()),
           "worst_month_label": str(m.idxmin())}
    if res is not None:
        # the segment whose base is the START close also owns the trades made at that close
        lo = pd.Timestamp(account_start) if v.index[0] == pd.Timestamp(account_start) else pd.Timestamp(start)
        mask = (res["nav"].index >= lo) & (res["nav"].index <= pd.Timestamp(end))
        t = res["trades"]
        if len(t):
            t = t[(t["entry"] >= lo) & (t["entry"] <= pd.Timestamp(end))]
        gains, losses = t["pnl"][t["pnl"] > 0].sum() if len(t) else 0, -t["pnl"][t["pnl"] < 0].sum() if len(t) else 0
        navm = res["nav"][mask]
        prev = res["nav"].shift(1).fillna(account)[mask]
        out.update({"trades": int(len(t)), "win_rate": float((t["ret"] > 0).mean()) if len(t) else float("nan"),
                    "profit_factor": float(gains / losses) if losses > 0 else float("nan"),
                    "avg_trade_ret": float(t["ret"].mean()) if len(t) else float("nan"),
                    "cost_drag_per_year": float((res["cost"][mask] / prev).sum() / yrs),
                    "avg_exposure": float(res["exposure"][mask].mean()),
                    "avg_positions": float(res["n_positions"][mask].mean()),
                    "time_in_cash_share": float((res["exposure"][mask] < 0.05).mean())})
        # turnover: one-way traded value / 2 / average NAV per year, on this segment
        out["turnover_per_year"] = float(res["traded_day"][mask].sum()
                                         / 2 / navm.mean() / yrs)
    return out
