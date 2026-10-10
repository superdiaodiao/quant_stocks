"""QQQ exposure timing (cash / 1x QQQ / 2x): the rule family, the rule grid, the synthetic-2x calibration and the
data checks of studies/qqq_timing.py.

Extracted unchanged from studies/qqq_timing.py in the slimming pass (``Rule``, ``target_exposure``, ``rule_grid``,
``calibrate_spread``, ``data_checks``, ``qld_tracking``, ``latest_complete_month_end``). The study's date guard and
paths are arguments: ``guard_end`` / ``dev_start`` / ``dev_end`` (the 2014-12-31 development end) and the Tiingo
cross-check file. The data are ``quant.data.index_history.DevData``; the engine is ``quant.backtest.exposure``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates, truncate_dev
from quant.data.index_history import DevData
from quant.data.synthetic import synthetic_2x
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, monthly
from quant.signals.technical import month_end_only, realised_vol, trend_state


# ======================================================================== data checks and the 2x calibration

def calibrate_spread(data: DevData, start: str, end: str, guard_end: str) -> dict:
    """Spread of the synthetic 2x fund (bisection) that matches the real QLD's CAGR over [start, end]."""
    real = data.qld.loc[start:end]
    assert_dev_dates(real.index, guard_end)
    r1 = data.r1.reindex(real.index)
    rf = data.rf.reindex(real.index)
    target = cagr_of(real)
    lo, hi = -0.05, 0.10
    for _ in range(80):
        mid = (lo + hi) / 2
        if cagr_of(synthetic_2x(r1, rf, mid)) > target:
            lo = mid
        else:
            hi = mid
    s = (lo + hi) / 2
    out = {"window": [str(real.index[0].date()), str(real.index[-1].date())], "sessions": int(len(real)),
           "spread_calibrated": s}
    for tag, sp in [("spread_0", 0.0), ("calibrated", s)]:
        syn = synthetic_2x(r1, rf, sp)
        d = syn - real
        out[tag] = {"cagr_gap_syn_minus_real": cagr_of(syn) - target,
                    "tracking_error_daily_ann": float(d.std() * math.sqrt(TRADING_DAYS)),
                    "tracking_error_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
                    "corr": float(np.corrcoef(syn, real)[0, 1]),
                    "mean_daily_diff_bp": float(d.mean() * 1e4), "max_abs_daily_diff": float(d.abs().max())}
    # tracking by calendar year (cagr gaps only; both legs are dev-period data)
    yrs = {}
    syn = synthetic_2x(r1, rf, s)
    for y in sorted(set(real.index.year)):
        m = real.index.year == y
        yrs[int(y)] = {"gap": float(np.prod(1 + syn[m]) - np.prod(1 + real[m])),
                       "te": float((syn[m] - real[m]).std() * math.sqrt(TRADING_DAYS))}
    out["by_year"] = yrs
    return out


def data_checks(data: DevData, tiingo_qqq: Path, dev_start: str, dev_end: str) -> dict:
    """QQQ adjusted close vs close + dividends, Yahoo vs Tiingo (when the file exists), NDX before 1999, RF."""
    q = data.qqq_raw
    adj_r = q["adjclose"].pct_change()
    cd_r = (q["close"] + q["dividend"]) / q["close"].shift(1) - 1
    d = (adj_r - cd_r).dropna()
    out = {"qqq_adj_vs_close_plus_div": {"max_abs_daily_diff": float(d.abs().max()),
                                         "cagr_adj": cagr_of(adj_r.dropna()), "cagr_close_div": cagr_of(cd_r.dropna()),
                                         "dividends_counted": int((q["dividend"] > 0).sum())}}
    if tiingo_qqq.exists():
        t = pd.read_csv(tiingo_qqq)
        t = truncate_dev(t, "date", dev_end)
        tr = pd.Series(t["adjClose"].values, index=pd.DatetimeIndex(t["date"])).pct_change().dropna()
        yr = pd.Series(q["adjclose"].values, index=pd.DatetimeIndex(q["date"])).pct_change()
        j = pd.concat([tr, yr], axis=1, join="inner").dropna()
        out["qqq_yahoo_vs_tiingo_2010_2014"] = {"sessions": int(len(j)),
                                                "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max()),
                                                "cagr_gap": cagr_of(j.iloc[:, 1]) - cagr_of(j.iloc[:, 0])}
    ndx = data.r1.loc[:"1999-03-09"]
    per_year = ndx.groupby(ndx.index.year).size()
    out["ndx_pre1999"] = {"sessions_per_year_min": int(per_year.iloc[1:-1].min()),
                          "sessions_per_year_max": int(per_year.iloc[1:-1].max()),
                          "abs_daily_moves_over_10pct": int((ndx.abs() > 0.10).sum()),
                          "zero_return_days": int((ndx == 0).sum())}
    rf = data.rf.loc[dev_start:dev_end]
    dt = data.dtb3.loc[dev_start:dev_end]
    out["rf_check"] = {"kf_rf_mean_annualised": float(rf.mean() * TRADING_DAYS), "dtb3_mean": float(dt.mean())}
    return out


def qld_tracking(data: DevData, spread: float, start: str, end: str) -> dict:
    """Synthetic 2x (at ``spread``) vs the real QLD over [start, end]."""
    real = data.qld.loc[start:end]
    syn = synthetic_2x(data.r1.reindex(real.index), data.rf.reindex(real.index), spread)
    d = syn - real
    by = {int(y): float(np.prod(1 + syn[real.index.year == y]) - np.prod(1 + real[real.index.year == y]))
          for y in sorted(set(real.index.year))}
    return {"window": [str(real.index[0].date()), str(real.index[-1].date())], "sessions": int(len(real)),
            "cagr_real": cagr_of(real), "cagr_synthetic": cagr_of(syn), "cagr_gap_syn_minus_real": cagr_of(syn) - cagr_of(real),
            "tracking_error_daily_ann": float(d.std() * math.sqrt(TRADING_DAYS)),
            "tracking_error_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
            "corr": float(np.corrcoef(syn, real)[0, 1]), "gap_by_year": by}


def latest_complete_month_end(today: pd.Timestamp) -> pd.Timestamp:
    return (pd.Timestamp(today).normalize().replace(day=1) - pd.Timedelta(days=1))


# ======================================================================== rules (data up to t only)

@dataclass(frozen=True)
class Rule:
    family: str            # T01 T02 T12 VT VTT M01 M02
    length: int = 0
    band: float = 0.0
    target: float = 0.0
    window: int = 0

    @property
    def name(self) -> str:
        if self.family in ("T01", "T02", "T12", "M01", "M02"):
            return f"{self.family}_L{self.length}_b{int(round(self.band * 100))}"
        if self.family == "VT":
            return f"VT_t{int(self.target * 100)}_w{self.window}"
        return f"VTT_t{int(self.target * 100)}_w{self.window}_L{self.length}"

    @property
    def continuous(self) -> bool:
        return self.family in ("VT", "VTT")


def target_exposure(rule: Rule, p: pd.Series, r1: pd.Series) -> pd.Series:
    """Desired exposure decided at each close t (only data up to t)."""
    if rule.family in ("T01", "T02", "T12", "M01", "M02"):
        st = trend_state(p, rule.length, rule.band)
        if rule.family.startswith("M"):
            st = month_end_only(st)
        on, off = {"T01": (1, 0), "T02": (2, 0), "T12": (2, 1), "M01": (1, 0), "M02": (2, 0)}[rule.family]
        return st.map({1.0: float(on), 0.0: float(off)})
    vt = (rule.target / realised_vol(r1, rule.window)).clip(upper=2.0)
    if rule.family == "VT":
        return vt
    st = trend_state(p, rule.length, 0.02)
    return vt.where(st != 0.0, 0.0).where(st.notna())


def rule_grid() -> list[Rule]:
    """The 142 pre-registered rules (docs/research_ledger_qqq_timing.md, section 0)."""
    rules = []
    for fam in ("T01", "T02", "T12"):
        for L in (75, 100, 112, 125, 150, 188, 200, 250):
            for b in (0.0, 0.01, 0.02, 0.03, 0.05):
                rules.append(Rule(fam, L, b))
    for t in (0.15, 0.20, 0.25):
        for w in (20, 60):
            rules.append(Rule("VT", target=t, window=w))
    for t in (0.15, 0.20, 0.25):
        for w in (20, 60):
            for L in (150, 200):
                rules.append(Rule("VTT", L, 0.02, t, w))
    for fam in ("M01", "M02"):
        for L in (150, 200):
            rules.append(Rule(fam, L, 0.0))
    return rules
