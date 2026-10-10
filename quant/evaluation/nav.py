"""Metrics of the dollar-NAV series of the stock-portfolio engines (livermore, indicators, oneil, earnings_events,
stops, walk_forward, canslim), judged against a buy-and-hold benchmark NAV started with the same account.

Moved unchanged from the studies in the slimming pass; each function keeps the conventions the ledgers were written
with, so several similar-looking functions coexist (their keys, anchoring and floating-point operation order differ):

- ``nav_vs_bench_metrics`` / ``nav_vs_bench_sub_metrics`` (were ``strategies.livermore.perf_metrics`` /
  ``sub_metrics``): the account value is anchored 7 days before the first close (first week / month keep their
  return); daily Sharpe without the risk-free rate; benchmark keys ``qqq_*``; weekly (W-FRI) and month-end active
  returns; by-year values rounded to 4 places.
- ``nav_vs_oneq_metrics`` / ``nav_vs_oneq_sub_metrics`` (were ``strategies.indicators.stock_metrics`` /
  ``stock_sub_metrics``): ``nav_vs_bench_metrics`` against ONEQ (keys ``bench_*``) plus QQQ's CAGR / drawdown.
- ``trade_stats``: closed-trade statistics (livermore and the engines that reuse it with ``tranches=1``).
- ``oneq_nav_metrics`` / ``oneq_nav_sub_window`` / ``oneq_nav_vs_base`` (were ``studies/stops.py`` ``metrics`` /
  ``sub_window`` / ``vs_base``): the stops ledger's metric set (0.6): ulcer index, longest drawdown in calendar days, calendar-month excess, polyfit beta.
  Its helpers ``drawdown``, ``longest_drawdown_days_open``, ``ulcer_index_pct``, ``sharpe_daily``, ``sortino_daily``,
  ``cagr_of_returns``, ``mdd_of_returns``, ``calmar_of_returns``, ``anchored``, ``trade_metrics`` keep their own
  operation order (``ulcer_index_pct`` is not ``quant.evaluation.equity.ulcer_index``; ``longest_drawdown_days_open``
  also says whether the longest drawdown is unrecovered, unlike ``metrics.longest_drawdown_days``).
- ``return_vs_bench_metrics`` / ``yearly_vs_bench`` (were ``studies/walk_forward.py`` ``ret_metrics`` / ``by_year``):
  CAGR / drawdown / Sharpe over the risk-free rate / monthly excess t, and calendar-year returns, of a DAILY RETURN
  series and its benchmark on the same days.
- ``weekly``: last value of each W-FRI week (was ``strategies.canslim.weekly``).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.evaluation.metrics import cagr_of, max_drawdown_of_returns, monthly


def weekly(series: pd.Series) -> pd.Series:
    return series.resample("W-FRI").last().dropna()


# ======================================================================== livermore / indicators convention

def nav_vs_bench_metrics(nav: pd.Series, bench: pd.Series, account: float, cost: pd.Series, traded: float,
                         exposure: pd.Series) -> dict:
    # the starting value sits 7 days before the first close so that the first week and month keep their return
    anchor = nav.index[0] - pd.Timedelta(days=7)
    nav0 = pd.concat([pd.Series([account], index=[anchor]), nav])
    bn0 = pd.concat([pd.Series([account], index=[anchor]), bench])
    r, b = nav0.pct_change().dropna(), bn0.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / account) ** (1 / years) - 1
    bcagr = (bench.iloc[-1] / account) ** (1 / years) - 1
    a = r - b
    wa = (weekly(nav0).pct_change() - weekly(bn0).pct_change()).dropna()
    mn, mb = nav0.resample("ME").last().dropna(), bn0.resample("ME").last().dropna()
    ma = (mn.pct_change() - mb.pct_change()).dropna()
    sd = a.std(ddof=1)
    out = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "cagr": cagr, "qqq_cagr": bcagr, "excess_cagr": cagr - bcagr,
        "vol": r.std(ddof=1) * math.sqrt(252), "max_dd": max_drawdown_of_returns(r), "qqq_max_dd": max_drawdown_of_returns(b),
        "sharpe": r.mean() / r.std(ddof=1) * math.sqrt(252) if r.std() > 0 else np.nan,
        "qqq_sharpe": b.mean() / b.std(ddof=1) * math.sqrt(252),
        "ir": a.mean() / sd * math.sqrt(252) if sd > 0 else np.nan,
        "t_excess_weekly": wa.mean() / wa.std(ddof=1) * math.sqrt(len(wa)) if wa.std() > 0 else np.nan,
        "t_excess_monthly": ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)) if ma.std() > 0 else np.nan,
        "ir_weekly_per_period": wa.mean() / wa.std(ddof=1) if wa.std() > 0 else np.nan,
        "weekly_active_skew": float(wa.skew()), "weekly_active_kurt": float(wa.kurt() + 3), "weeks": len(wa),
        "months": len(ma), "tracking_error": sd * math.sqrt(252),
        "turnover_one_way_per_year": traded / 2 / nav.mean() / years,
        "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
        "cost_usd_total": float(cost.sum()),
        "time_in_stocks": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
        "active_weekly_mean_ann": wa.mean() * 52,
    }
    for n in (5, 10):
        out[f"active_ann_drop_best{n}w"] = wa.drop(wa.nlargest(n).index).mean() * 52
    by = {}
    for y in sorted(set(nav.index.year)):
        m = nav.index.year == y
        prev_n = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        prev_b = bench[bench.index.year < y].iloc[-1] if (bench.index.year < y).any() else account
        sy, by_ = nav[m].iloc[-1] / prev_n - 1, bench[m].iloc[-1] / prev_b - 1
        by[int(y)] = {"strategy": round(float(sy), 4), "qqq": round(float(by_), 4), "excess": round(float(sy - by_), 4)}
    out["by_year"] = by
    out["years_beating_qqq"] = sum(v["excess"] > 0 for v in by.values())
    out["_monthly_active"] = ma
    return out


def nav_vs_bench_sub_metrics(nav: pd.Series, bench: pd.Series, start: str, cost: pd.Series, exposure: pd.Series,
                             account: float = 10_000.0) -> dict:
    """Metrics on a sub-window, both series rebased to ``account`` at the last close before ``start``."""
    before = nav.index[nav.index < pd.Timestamp(start)]
    if not len(before):
        return nav_vs_bench_metrics(nav, bench, account, cost, 0.0, exposure)
    t0 = before[-1]
    keep = nav.index > t0
    n = nav[keep] / nav.loc[t0] * account
    b = bench[keep] / bench.loc[t0] * account
    return nav_vs_bench_metrics(n, b, account, cost[keep], 0.0, exposure[keep])


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"closed_trades": 0}
    w, l_ = trades[trades["ret"] > 0], trades[trades["ret"] <= 0]
    return {"closed_trades": int(len(trades)), "win_rate": float((trades["ret"] > 0).mean()),
            "avg_trade_ret": float(trades["ret"].mean()), "avg_win": float(w["ret"].mean()) if len(w) else np.nan,
            "avg_loss": float(l_["ret"].mean()) if len(l_) else np.nan,
            "median_days": float(trades["days"].median()) if "days" in trades else np.nan,
            "share_pyramided": float((trades["tranches"] > 1).mean()), "best_trade": float(trades["ret"].max()),
            "worst_trade": float(trades["ret"].min())}


def nav_vs_oneq_metrics(nav, oneq, qqq, account, cost, traded, exposure) -> dict:
    m = nav_vs_bench_metrics(nav, oneq, account, cost, traded, exposure)
    m["bench_cagr"], m["bench_max_dd"] = m.pop("qqq_cagr"), m.pop("qqq_max_dd")
    m["bench_sharpe"] = m.pop("qqq_sharpe")
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    m["qqq_cagr"] = (qqq.iloc[-1] / account) ** (1 / years) - 1
    r = pd.concat([pd.Series([account], index=[qqq.index[0] - pd.Timedelta(days=7)]), qqq]).pct_change().dropna()
    m["qqq_max_dd"] = max_drawdown_of_returns(r)
    for y, v in m["by_year"].items():
        v["oneq"] = v.pop("qqq")
    m["years_beating_bench"] = m.pop("years_beating_qqq")
    return m


def nav_vs_oneq_sub_metrics(nav, oneq, qqq, start, cost, exposure, account=10_000.0) -> dict:
    before = nav.index[nav.index < pd.Timestamp(start)]
    if not len(before):
        return nav_vs_oneq_metrics(nav, oneq, qqq, account, cost, 0.0, exposure)
    t0 = before[-1]
    keep = nav.index > t0
    f = lambda s: s[keep] / s.loc[t0] * account  # noqa: E731
    return nav_vs_oneq_metrics(f(nav), f(oneq), f(qqq), account, cost[keep], 0.0, exposure[keep])


# ======================================================================== stops ledger convention (0.6)

def drawdown(nav0: pd.Series) -> pd.Series:
    return nav0 / nav0.cummax() - 1


def longest_drawdown_days_open(nav0: pd.Series) -> tuple[int, bool]:
    """Longest peak-to-recovery span in calendar days; an unrecovered final drawdown counts to the last date.
    Returns (days, the longest one is still unrecovered)."""
    peak_v, peak_d = -np.inf, None
    best, open_best = 0, False
    in_dd = False
    for d, v in nav0.items():
        if v >= peak_v:
            if in_dd:
                span = (d - peak_d).days
                if span > best:
                    best, open_best = span, False
            peak_v, peak_d, in_dd = v, d, False
        else:
            in_dd = True
    if in_dd:
        span = (nav0.index[-1] - peak_d).days
        if span > best:
            best, open_best = span, True
    return int(best), bool(open_best)


def ulcer_index_pct(nav0: pd.Series) -> float:
    """sqrt(mean((100 x drawdown)^2)), in percent."""
    return float(np.sqrt(np.mean((100 * drawdown(nav0)) ** 2)))


def sharpe_daily(r: pd.Series) -> float:
    sd = r.std(ddof=1)
    return float(r.mean() / sd * math.sqrt(252)) if sd > 0 else float("nan")


def sortino_daily(r: pd.Series) -> float:
    dd = math.sqrt(float(np.mean(np.minimum(r.to_numpy(), 0.0) ** 2)))
    return float(r.mean() / dd * math.sqrt(252)) if dd > 0 else float("nan")


def cagr_of_returns(r: pd.Series) -> float:
    if len(r) == 0:
        return float("nan")
    return float(np.prod(1 + r.to_numpy()) ** (252 / len(r)) - 1)


def mdd_of_returns(r: pd.Series) -> float:
    v = np.concatenate([[1.0], np.cumprod(1 + r.to_numpy())])
    return float((v / np.maximum.accumulate(v) - 1).min())


def calmar_of_returns(r: pd.Series) -> float:
    m = mdd_of_returns(r)
    return cagr_of_returns(r) / abs(m) if m < 0 else float("inf")


def anchored(s: pd.Series, account: float) -> pd.Series:
    """Prepend the starting value 7 days before the first close (first day / week / month keep their return)."""
    return pd.concat([pd.Series([account], index=[s.index[0] - pd.Timedelta(days=7)]), s])


def trade_metrics(trades: pd.DataFrame) -> dict:
    if trades is None or trades.empty:
        return {"closed_trades": 0, "win_rate": np.nan, "avg_win": np.nan, "avg_loss": np.nan,
                "win_loss_ratio": np.nan, "profit_factor": np.nan, "avg_hold_days": np.nan}
    r = trades["ret"].astype(float)
    w, l_ = r[r > 0], r[r <= 0]
    days = trades["days"].astype(float).dropna() if "days" in trades else pd.Series(dtype=float)
    return {"closed_trades": int(len(r)), "win_rate": float((r > 0).mean()),
            "avg_win": float(w.mean()) if len(w) else np.nan, "avg_loss": float(l_.mean()) if len(l_) else np.nan,
            "win_loss_ratio": float(w.mean() / abs(l_.mean())) if len(w) and len(l_) and l_.mean() < 0 else np.nan,
            "profit_factor": float(w.sum() / abs(l_.sum())) if len(l_) and l_.sum() < 0 else np.inf,
            "avg_hold_days": float(days.mean()) if len(days) else np.nan}


def oneq_nav_metrics(nav: pd.Series, oneq: pd.Series, qqq: pd.Series, cost: pd.Series, traded: float,
                     exposure: pd.Series, trades: pd.DataFrame | None, n_buy: int, n_stop: int,
                     account: float = 10_000.0) -> dict:
    """The full pre-registered metric set (stops ledger 0.6) for one daily NAV against ONEQ (and QQQ)."""
    nav0, b0, q0 = anchored(nav, account), anchored(oneq, account), anchored(qqq, account)
    r, b = nav0.pct_change().dropna(), b0.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cg = (nav.iloc[-1] / account) ** (1 / years) - 1
    bc = (oneq.iloc[-1] / account) ** (1 / years) - 1
    qc = (qqq.iloc[-1] / account) ** (1 / years) - 1
    mdd = float(drawdown(nav0).min())
    ldd, ldd_open = longest_drawdown_days_open(nav0)
    mr = nav0.resample("ME").last().pct_change().dropna()
    mb = b0.resample("ME").last().pct_change().dropna()
    mr, mb = mr.align(mb, join="inner")
    ma = mr - mb
    sd = ma.std(ddof=1)
    beta, a_m = np.polyfit(mb.to_numpy(), mr.to_numpy(), 1) if len(mr) > 2 else (np.nan, np.nan)
    by = {}
    for y in sorted(set(nav.index.year)):
        m = nav.index.year == y
        if m.sum() == 1 and nav.index[0].year == y:      # only the starting close (e.g. 2017-12-29): no return
            continue
        pn = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        pb = oneq[oneq.index.year < y].iloc[-1] if (oneq.index.year < y).any() else account
        pq = qqq[qqq.index.year < y].iloc[-1] if (qqq.index.year < y).any() else account
        by[int(y)] = {"strategy": float(nav[m].iloc[-1] / pn - 1), "oneq": float(oneq[m].iloc[-1] / pb - 1),
                      "qqq": float(qqq[m].iloc[-1] / pq - 1)}
    yr = pd.Series({y: v["strategy"] for y, v in by.items()})
    out = {"start": str(nav.index[0].date()), "end": str(nav.index[-1].date()), "years": years,
           "cagr": cg, "oneq_cagr": bc, "qqq_cagr": qc, "excess_oneq": cg - bc, "excess_qqq": cg - qc,
           "vol": float(r.std(ddof=1) * math.sqrt(252)), "max_dd": mdd,
           "oneq_max_dd": float(drawdown(b0).min()), "qqq_max_dd": float(drawdown(q0).min()),
           "longest_dd_days": ldd, "longest_dd_unrecovered": ldd_open, "ulcer": ulcer_index_pct(nav0),
           "sharpe": sharpe_daily(r), "sortino": sortino_daily(r), "calmar": cg / abs(mdd) if mdd < 0 else np.inf,
           "oneq_sharpe": sharpe_daily(b),
           "ir_oneq": float(ma.mean() / sd * math.sqrt(12)) if sd > 0 else np.nan,
           "t_monthly_excess": float(ma.mean() / sd * math.sqrt(len(ma))) if sd > 0 else np.nan,
           "months": int(len(ma)), "beta_oneq": float(beta), "alpha_oneq": float(a_m * 12),
           "share_years_beating_oneq": float(np.mean([v["strategy"] > v["oneq"] for v in by.values()])),
           "worst_year": float(yr.min()), "worst_year_which": int(yr.idxmin()), "worst_month": float(mr.min()),
           "buys": int(n_buy), "stop_exits": int(n_stop),
           "time_in_market": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
           "turnover_one_way_per_year": float(traded / 2 / nav.mean() / years),
           "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
           "cost_usd_total": float(cost.sum())}
    out.update(trade_metrics(trades))
    out["by_year"] = by
    out["_monthly_excess"] = ma
    out["_monthly_ret"] = mr
    return out


def oneq_nav_sub_window(res: dict, oneq: pd.Series, qqq: pd.Series, start: str, end: str,
                        account: float = 10_000.0) -> dict:
    """CAGR / max DD / t of a sub-window, all series rebased at the last close before ``start``."""
    nav = res["nav"]
    t0 = nav.index[nav.index < pd.Timestamp(start)][-1]
    keep = (nav.index > t0) & (nav.index <= pd.Timestamp(end))
    f = lambda s: s[keep] / s.loc[t0] * account  # noqa: E731
    tr = res["trades"]
    if tr is not None and len(tr):
        tr = tr[(pd.to_datetime(tr["exit"]) > t0) & (pd.to_datetime(tr["exit"]) <= pd.Timestamp(end))]
    m = oneq_nav_metrics(f(nav), f(oneq), f(qqq), res["cost"][keep], 0.0, res["exposure"][keep], tr, 0, 0)
    return {k: m[k] for k in ("start", "end", "cagr", "oneq_cagr", "qqq_cagr", "max_dd", "oneq_max_dd", "sharpe",
                              "t_monthly_excess")}


def oneq_nav_vs_base(m_wf: dict, m_base: dict) -> dict:
    """Two ``oneq_nav_metrics`` results compared: CAGR / Sharpe / drawdown differences and the t of the monthly
    return differences (0 without spread) (was ``studies/stops.py`` ``vs_base``)."""
    d = (m_wf["_monthly_ret"] - m_base["_monthly_ret"]).dropna()
    t = float(d.mean() / d.std(ddof=1) * math.sqrt(len(d))) if d.std(ddof=1) > 0 else 0.0
    return {"cagr_diff": m_wf["cagr"] - m_base["cagr"], "sharpe_diff": m_wf["sharpe"] - m_base["sharpe"],
            "max_dd_diff": m_wf["max_dd"] - m_base["max_dd"], "t_monthly_diff": t,
            "beats_base": bool(m_wf["cagr"] > m_base["cagr"] and m_wf["sharpe"] > m_base["sharpe"])}


# ======================================================================== walk-forward convention (daily returns)

def return_vs_bench_metrics(r: pd.Series, b: pd.Series, rf: pd.Series) -> dict:
    """CAGR / max DD / monthly-excess t of a daily return series and its benchmark over the same days (rebased)."""
    r, b = r.dropna(), b.reindex(r.index).fillna(0.0)
    v, bv = (1 + r).cumprod(), (1 + b).cumprod()
    v0, bv0 = pd.concat([pd.Series([1.0]), v]), pd.concat([pd.Series([1.0]), bv])
    mx = monthly(r) - monthly(b)
    ex = r - rf.reindex(r.index).fillna(0.0)
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()),
            "cagr": cagr_of(r), "bench_cagr": cagr_of(b), "excess_cagr": cagr_of(r) - cagr_of(b),
            "max_dd": float((v0 / v0.cummax() - 1).min()), "bench_max_dd": float((bv0 / bv0.cummax() - 1).min()),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(252)) if ex.std() > 0 else float("nan"),
            "t_monthly_excess": float(mx.mean() / mx.std() * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
            "months": int(len(mx))}


def yearly_vs_bench(r: pd.Series, b: pd.Series) -> dict:
    """{year: {strategy, oneq, excess}} of compounded daily returns ``r`` and benchmark ``b`` on r's days (was
    ``studies/walk_forward.py`` ``by_year``)."""
    ys = (1 + r).groupby(r.index.year).prod() - 1
    yb = (1 + b.reindex(r.index).fillna(0.0)).groupby(r.index.year).prod() - 1
    return {int(y): {"strategy": float(ys[y]), "oneq": float(yb[y]), "excess": float(ys[y] - yb[y])} for y in ys.index}
