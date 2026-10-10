"""Performance metrics of daily return / value series.

Extracted unchanged from scripts/research_qqq_timing.py (``cagr_of``, ``max_drawdown``, ``monthly``, ``yearly``,
``cagr_months``), scripts/research_calendar.py (``longest_drawdown``, ``t_and_ir``, ``core_metrics``,
``relative_metrics``), scripts/research_megacap.py (``longest_drawdown_days``) and
scripts/research_reversal_dev.py (its ``max_drawdown`` of returns, here ``max_drawdown_of_returns``).

Conventions: 252 sessions a year; ``max_drawdown`` takes a VALUE series (prepend 1.0 to include the start);
monthly / yearly returns compound the daily returns inside each calendar month / year.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def cagr_of(r: pd.Series, periods: float = TRADING_DAYS) -> float:
    """Compound annual growth of a daily return series (NaNs dropped)."""
    r = pd.Series(r).dropna()
    return float(np.prod(1 + r.values) ** (periods / len(r)) - 1) if len(r) else float("nan")


def max_drawdown(value: pd.Series) -> float:
    """Largest peak-to-trough fall of a value series (negative fraction)."""
    return float((value / value.cummax() - 1).min())


def max_drawdown_of_returns(r: pd.Series) -> float:
    """``max_drawdown`` of the value path (1 + r).cumprod() (NaN for an empty series)."""
    w = (1 + r).cumprod()
    return float((w / w.cummax() - 1).min()) if len(w) else float("nan")


def monthly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.to_period("M")).prod() - 1


def yearly(r: pd.Series) -> pd.Series:
    return (1 + r).groupby(r.index.year).prod() - 1


def cagr_months(m: pd.Series) -> float:
    return float(np.prod(1 + m.values) ** (12 / len(m)) - 1)


def longest_drawdown(value: pd.Series) -> int:
    """Longest stretch (sessions) from a peak until the value is back at that peak (or the end)."""
    peak = value.cummax().values
    under = value.values < peak - 1e-12
    best = run = 0
    for u in under:
        run = run + 1 if u else 0
        best = max(best, run)
    return int(best)


def longest_drawdown_days(v: pd.Series) -> int:
    """Longest calendar-day span from a peak until the value first regains it (or the end, if never)."""
    peak_val, peak_day, longest = -np.inf, None, 0
    for d, x in v.items():
        if x >= peak_val:
            if peak_day is not None:
                longest = max(longest, (d - peak_day).days)
            peak_val, peak_day = x, d
    if peak_day is not None and v.iloc[-1] < peak_val:
        longest = max(longest, (v.index[-1] - peak_day).days)
    return int(longest)


def t_and_ir(x: pd.Series) -> tuple[float, float]:
    """(t-statistic of the mean, annualised IR) of a MONTHLY excess-return series; (0, 0) when it has no spread."""
    sd = x.std()
    if not sd > 0:
        return 0.0, 0.0
    return float(x.mean() / sd * math.sqrt(len(x))), float(x.mean() / sd * math.sqrt(12))


def core_metrics(r: pd.Series, rf: pd.Series) -> dict:
    """CAGR, vol, max drawdown (and its length in sessions), Sharpe / Sortino on excess over ``rf``, Calmar, worst
    year and month of a daily return series."""
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    ex = r - rf.reindex(r.index)
    down = np.sqrt(np.mean(np.minimum(ex.values, 0.0) ** 2))
    cagr, dd = cagr_of(r), max_drawdown(v0)
    y, m = yearly(r), monthly(r)
    return {"cagr": cagr, "vol": float(r.std() * math.sqrt(TRADING_DAYS)), "max_dd": dd,
            "longest_dd_sessions": longest_drawdown(v0), "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "sortino": float(ex.mean() / down * math.sqrt(TRADING_DAYS)) if down > 0 else float("nan"),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "worst_year": float(y.min()), "worst_year_which": int(y.idxmin()),
            "worst_month": float(m.min()), "worst_month_which": str(m.idxmin())}


def relative_metrics(r: pd.Series, b: pd.Series, rf: pd.Series, tag: str) -> dict:
    """Strategy vs a buy-and-hold benchmark on the benchmark's index (keys carry ``tag``, e.g. 'oneq')."""
    r = r.reindex(b.index)
    mr, mb = monthly(r), monthly(b)
    mx = mr - mb
    t, ir = t_and_ir(mx)
    rfm = monthly(rf.reindex(b.index))
    y_, x_ = (mr - rfm).values, (mb - rfm).values
    beta = float(np.cov(y_, x_, ddof=1)[0, 1] / np.var(x_, ddof=1))
    alpha = float((y_.mean() - beta * x_.mean()) * 12)
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    vb = pd.concat([pd.Series([1.0]), (1 + b).cumprod().reset_index(drop=True)])
    dd, bdd = max_drawdown(v0), max_drawdown(vb)
    yr, yb = yearly(r), yearly(b)
    return {f"{tag}_window_start": str(b.index[0].date()), f"cagr_in_{tag}_window": cagr_of(r),
            f"{tag}_cagr": cagr_of(b), f"excess_vs_{tag}": cagr_of(r) - cagr_of(b),
            f"max_dd_in_{tag}_window": dd, f"{tag}_max_dd": bdd, f"dd_shallower_than_{tag}_pp": (abs(bdd) - abs(dd)) * 100,
            f"t_monthly_excess_vs_{tag}": t, f"ir_vs_{tag}": ir, f"beta_vs_{tag}": beta, f"alpha_vs_{tag}": alpha,
            f"share_years_beating_{tag}": float((yr > yb).mean()), f"years_{tag}": int(len(yr)), f"months_{tag}": int(len(mx))}


def exposure_metrics(sim: dict, bench: dict, rf: pd.Series) -> dict:
    """Metrics of an exposure-timing run (``quant.backtest.exposure.simulate`` output) vs a buy-and-hold run:
    return, risk, time spent at each exposure level, trading, monthly excess t / IR, drop-best-months checks
    (scripts/research_qqq_timing.py ``metrics``)."""
    r, b, v = sim["ret"], bench["ret"], sim["value"]
    years = len(r) / TRADING_DAYS
    cagr = cagr_of(r)
    bcagr = cagr_of(b)
    ex = r - rf.reindex(r.index)
    mdd = max_drawdown(v)
    e = sim["exposure_held"]
    m, mb = monthly(r), monthly(b)
    mx = m - mb
    out = {
        "cagr": cagr, "excess_cagr_vs_qqq": cagr - bcagr, "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
        "max_dd": mdd, "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
        "calmar": cagr / abs(mdd) if mdd < 0 else float("nan"),
        "time_cash": float((e < 0.05).mean()), "time_between_0_1": float(((e >= 0.05) & (e < 0.95)).mean()),
        "time_1x": float(((e >= 0.95) & (e <= 1.05)).mean()), "time_between_1_2": float(((e > 1.05) & (e < 1.95)).mean()),
        "time_2x": float((e >= 1.95).mean()), "avg_exposure": float(e.mean()),
        "switches_per_year": sim["rebalances"] / years, "orders_per_year": sim["orders"] / years,
        "cost_drag_per_year": sim["cost_frac"] / years,
        "worst_year": float(yearly(r).min()), "worst_year_which": int(yearly(r).idxmin()),
        "months": int(len(mx)), "mean_monthly_excess": float(mx.mean()),
        "t_monthly_excess": float(mx.mean() / mx.std() * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
        "ir_monthly": float(mx.mean() / mx.std()) if mx.std() > 0 else 0.0,
        "skew_mx": float(mx.skew()), "kurt_mx": float(mx.kurt() + 3.0),
    }
    # drop-best-months: strategy's own best months removed, and the biggest-excess months replaced by QQQ's
    for k in (3, 6):
        keep = m.drop(m.nlargest(k).index)
        out[f"cagr_drop_best{k}"] = cagr_months(keep)
        swapped = m.copy()
        top = mx.nlargest(k).index
        swapped.loc[top] = mb.loc[top]
        out[f"excess_drop_best{k}_excess_months"] = cagr_months(swapped) - cagr_months(mb)
    return out
