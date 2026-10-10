"""Re-scoring a saved rule NAV (or monthly return) series against benchmark NAVs (ONEQ, the estimated Nasdaq
Composite total return, QQQ), the conventions of studies/benchmark_composite.py.

Extracted unchanged from studies/benchmark_composite.py in the slimming pass (``cagr`` -> ``cagr_calendar``,
``bench_window``, ``summarise``, ``tables``). Conventions: CAGR over calendar time (days / 365.25, not sessions);
monthly / yearly returns from the last NAV of each period over the previous period's last NAV (the base point opens
the first period); monthly-excess t = ``quant.evaluation.stats.t_of_mean(x, 2)`` (its former local ``tstat``, the
same code).
"""
from __future__ import annotations

import math

import pandas as pd

from quant.evaluation.stats import t_of_mean


def cagr_calendar(growth: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    """CAGR of a total ``growth`` multiple over calendar time (years = days / 365.25)."""
    yrs = (end - start).days / 365.25
    return float(growth ** (1 / yrs) - 1)


def bench_window(level: pd.DataFrame, base: pd.Timestamp, end: pd.Timestamp, dates=None) -> pd.DataFrame:
    """Benchmark NAVs (base = 1 at the close of ``base``) on ``dates`` (default: all sessions in the window)."""
    lv = level.loc[(level.index >= base) & (level.index <= end), ["ONEQ", "IXIC_TR_est", "QQQ"]]
    if lv.index[0] != base or lv.index[-1] != end:
        raise ValueError(f"benchmark sessions do not match the rule window {base.date()}..{end.date()}")
    nav = lv / lv.iloc[0]
    if dates is not None:
        missing = pd.DatetimeIndex(dates).difference(nav.index)
        if len(missing):
            raise ValueError(f"benchmark missing rule dates: {list(missing[:5])}")
        nav = nav.reindex(dates)
    return nav


def summarise(name: str, label: str, rule_nav: pd.Series, bnav: pd.DataFrame, rule_mdd: float | None,
              bench_mdd: dict, monthly: pd.DataFrame, yearly: pd.DataFrame, extra: dict | None = None) -> dict:
    """CAGR (calendar years) of the rule and each benchmark, excess CAGRs, drawdowns, monthly excess mean / t / IR
    (t: ``stats.t_of_mean(x, 2)``) and the by-year table of one re-scored case."""
    start, end = rule_nav.index[0], rule_nav.index[-1]
    out = {"name": name, "label": label, "base_close": str(start.date()), "end": str(end.date()),
           "years": round((end - start).days / 365.25, 3)}
    out["cagr"] = {"rule": cagr_calendar(rule_nav.iloc[-1] / rule_nav.iloc[0], start, end)}
    for b in ("ONEQ", "IXIC_TR_est", "QQQ"):
        out["cagr"][b] = cagr_calendar(bnav[b].iloc[-1] / bnav[b].iloc[0], start, end)
    out["excess_cagr"] = {f"vs_{b}": out["cagr"]["rule"] - out["cagr"][b] for b in ("ONEQ", "IXIC_TR_est", "QQQ")}
    out["max_dd"] = {"rule": rule_mdd, **bench_mdd}
    mx_c = monthly["rule"] - monthly["ONEQ"]
    mx_q = monthly["rule"] - monthly["QQQ"]
    mx_i = monthly["rule"] - monthly["IXIC_TR_est"]
    out["months"] = int(len(monthly))
    out["monthly_excess_vs_ONEQ"] = {"mean": float(mx_c.mean()), "std": float(mx_c.std(ddof=1)), "t": t_of_mean(mx_c, 2),
                                     "ir_ann": float(mx_c.mean() / mx_c.std(ddof=1) * math.sqrt(12))}
    out["monthly_excess_vs_IXIC_TR_est"] = {"mean": float(mx_i.mean()), "t": t_of_mean(mx_i, 2)}
    out["monthly_excess_vs_QQQ"] = {"mean": float(mx_q.mean()), "t": t_of_mean(mx_q, 2)}
    oneq_minus_qqq = monthly["ONEQ"] - monthly["QQQ"]
    out["ONEQ_minus_QQQ_monthly_t"] = t_of_mean(oneq_minus_qqq, 2)
    out["by_year"] = {str(y): {k: round(float(v), 6) for k, v in row.items()} for y, row in yearly.iterrows()}
    if extra:
        out.update(extra)
    return out


def tables(rule: pd.Series, bnav: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Monthly and by-year returns from NAV series (first point is the base)."""
    nav = pd.concat([rule.rename("rule"), bnav], axis=1)
    def per(group):
        last = nav.groupby(group).last()
        prev = last.shift(1)
        prev.iloc[0] = nav.iloc[0]
        return last / prev - 1
    m = per(nav.index.to_period("M"))
    y = per(nav.index.year)
    # drop the base point if it is the only observation in its period
    if (nav.index.to_period("M") == nav.index[0].to_period("M")).sum() == 1:
        m = m.iloc[1:]
    if (nav.index.year == nav.index[0].year).sum() == 1:
        y = y.iloc[1:]
    for t in (m, y):
        t["excess_vs_ONEQ"] = t["rule"] - t["ONEQ"]
        t["excess_vs_QQQ"] = t["rule"] - t["QQQ"]
    return m, y
