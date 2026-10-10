"""Development-period research (1999-03 .. 2014-12 only) for QQQ exposure timing: cash / 1x QQQ / 2x (QLD-like).

Rules studied (pre-registered in docs/research_ledger_qqq_timing.md, section 0, before the first run):

- T01 / T02 / T12: trend filter on P = QQQ total-return index (NDX price before 1999-03-10). The state turns "on"
  when P > MA(L) * (1 + b) and "off" when P < MA(L) * (1 - b); inside the band the previous state is kept.
  T01: on = 1x QQQ, off = cash.  T02: on = 2x, off = cash.  T12: on = 2x, off = 1x.
- VT: volatility target, exposure = min(2, target / realised vol over W sessions), traded only when the target
  is at least 0.25 away from the current exposure.
- VTT: VT while the MA(L) trend state (band 2%) is on, cash while it is off.
- M: month-end check (signal looked at only on the last session of each month), T01 / T02, band 0.

Signals use data up to and including the decision close t. Main execution: the close of the next session t+1
(the new position earns returns from session t+2). Variant: the close of t itself (not really doable by hand).

Exposure between 0 and 2 is held as sleeves: e <= 1 -> e in QQQ, 1 - e in cash (a T-bill ETF); e > 1 ->
(2 - e) in QQQ and (e - 1) in the 2x fund.  The 2x fund is synthetic: 2 * r_QQQ - (RF + spread) - 0.95%/yr,
with the spread calibrated on the real QLD 2006-06-22 .. 2014-12-31 only.

HARD RULE: no return, price or outcome dated after 2014-12-31 is used. Every vendor frame is read through
``load_dev_data``, which truncates at ``DEV_END`` immediately after parsing and asserts that no later row survives;
the guard result is printed on every run.

Raw data (local only, never committed): /Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw/
Outputs: output/research_only/qqq_timing_dev/ (returns and metrics only, no vendor price levels).

The data loader, the sleeve engine, the ETF cost model and the metrics are in quant (quant.data.index_history,
quant.backtest.exposure, quant.evaluation); the rule grid and the frozen one-shot stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study qqq_timing [--mode oneshot --test-end YYYY-MM-DD]
        (or scripts/research_qqq_timing.py)
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant.backtest import exposure
from quant.backtest.costs import (  # noqa: F401  (qt.* names read by tests)
    CASH_ETF_FEE, FEES_PER_SHARE, SELL_REG_FRAC, etf_order_cost,
)
from quant.backtest.exposure import HALF_SPREAD, synthetic_2x, weights_for  # noqa: F401
from quant.data import guards, index_history
from quant.data.guards import FutureDataError  # noqa: F401
from quant.data.index_history import DevData, QQQ_FIRST_CLOSE, NDX_LAST_BEFORE_QQQ
from quant.data.rates import fill_rf  # noqa: F401
from quant.data.sources import yahoo
from quant.evaluation.criteria import bonferroni_t, deflated_sharpe, timing_criteria as evaluate_criteria
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, exposure_metrics as metrics, monthly, yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.signals.technical import month_end_only, realised_vol, trend_state  # noqa: F401

DEV_END = "2014-12-31"
DEV_START = QQQ_FIRST_CLOSE       # QQQ's first close; strategies and the benchmark enter at this close
SECOND_START = "1986-10-01"       # second reference period on NDX price (after a 250-session warm-up)
SECOND_END = NDX_LAST_BEFORE_QQQ
QLD_CAL_START = "2006-06-22"      # first QLD close is 2006-06-21; first daily return 2006-06-22

CACHE = index_history.QQQ_RAW
TIINGO_QQQ = CACHE_ROOT / "holdout_2011_2019/qqq_tiingo_2010_2020.csv"
OUT = output_dir("qqq_timing_dev")
EXPOSURE_LEVELS = (0.0, 1.0, 2.0)


# ======================================================================== the date guard and data (quant.data)

def assert_dev_dates(dates, end: str = DEV_END) -> None:
    guards.assert_dev_dates(dates, end)


def truncate_dev(frame: pd.DataFrame, column: str = "date", end: str = DEV_END) -> pd.DataFrame:
    return guards.truncate_dev(frame, column, end)


def parse_chart(path, end: str = DEV_END) -> pd.DataFrame:
    return yahoo.parse_chart(path, end)


def load_dev_data(end: str = DEV_END) -> DevData:
    return index_history.load_dev_data(end, CACHE)


def order_cost(value: float, price: float, sell: bool) -> float:
    return etf_order_cost(value, price, sell, HALF_SPREAD)


def simulate(target: pd.Series, r1: pd.Series, r2: pd.Series, rc: pd.Series, price: pd.Series,
             start: str, end: str, lag: int = 1, continuous: bool = False, cash_is_etf: bool = True) -> dict:
    """``quant.backtest.exposure.simulate`` with the 1 bp ETF half-spread."""
    return exposure.simulate(target, r1, r2, rc, price, start, end, lag, continuous, cash_is_etf)


def calibrate_spread(data: DevData, start: str = QLD_CAL_START, end: str = DEV_END) -> dict:
    real = data.qld.loc[start:end]
    assert_dev_dates(real.index, DEV_END)
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


def data_checks(data: DevData) -> dict:
    q = data.qqq_raw
    adj_r = q["adjclose"].pct_change()
    cd_r = (q["close"] + q["dividend"]) / q["close"].shift(1) - 1
    d = (adj_r - cd_r).dropna()
    out = {"qqq_adj_vs_close_plus_div": {"max_abs_daily_diff": float(d.abs().max()),
                                         "cagr_adj": cagr_of(adj_r.dropna()), "cagr_close_div": cagr_of(cd_r.dropna()),
                                         "dividends_counted": int((q["dividend"] > 0).sum())}}
    if TIINGO_QQQ.exists():
        t = pd.read_csv(TIINGO_QQQ)
        t = truncate_dev(t, "date")
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
    rf = data.rf.loc[DEV_START:DEV_END]
    dt = data.dtb3.loc[DEV_START:DEV_END]
    out["rf_check"] = {"kf_rf_mean_annualised": float(rf.mean() * TRADING_DAYS), "dtb3_mean": float(dt.mean())}
    return out


# ======================================================================== signals (data up to t only)


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


# ======================================================================== main

def run(args) -> dict:
    data = load_dev_data(DEV_END)
    print("DATE GUARD: every vendor frame truncated at", DEV_END, "and asserted;",
          {k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)})
    assert_dev_dates(data.sessions)
    checks = data_checks(data)
    cal = calibrate_spread(data)
    spread = cal["spread_calibrated"]
    print(f"synthetic 2x: calibrated spread {spread:.4%}/yr on {cal['window']}, "
          f"TE {cal['calibrated']['tracking_error_daily_ann']:.2%}, corr {cal['calibrated']['corr']:.4f}")

    p = (1 + data.r1).cumprod()
    r1 = data.r1
    rc_etf = data.rf - CASH_ETF_FEE / TRADING_DAYS
    rc_zero = pd.Series(0.0, index=data.sessions)
    r2 = synthetic_2x(r1, data.rf, spread)
    r2_dear = synthetic_2x(r1, data.rf, spread + 0.01)

    periods = {"dev": (DEV_START, DEV_END), "second": (SECOND_START, SECOND_END)}
    const1 = pd.Series(1.0, index=data.sessions)
    const2 = pd.Series(2.0, index=data.sessions)
    bench = {k: simulate(const1, r1, r2, rc_etf, data.price, *v) for k, v in periods.items()}
    refs = {"QQQ_buyhold": const1, "2x_buyhold": const2, "1.5x_buyhold": pd.Series(1.5, index=data.sessions)}

    rows, monthly_keep, yearly_tab = [], {}, {}
    rules = rule_grid()
    assert len(rules) == 142, len(rules)
    for name, tgt in list(refs.items()) + [(r.name, r) for r in rules]:
        is_rule = isinstance(tgt, Rule)
        rule = tgt if is_rule else None
        target = target_exposure(rule, p, r1) if is_rule else tgt
        cont = rule.continuous if is_rule else (name == "1.5x_buyhold")
        row = {"config": name, "family": rule.family if is_rule else "REF", "counted": bool(is_rule),
               "length": rule.length if is_rule else None, "band": rule.band if is_rule else None,
               "vol_target": rule.target if is_rule else None, "vol_window": rule.window if is_rule else None}
        sim = simulate(target, r1, r2, rc_etf, data.price, *periods["dev"], continuous=cont)
        assert_dev_dates(sim["ret"].index)
        row.update(metrics(sim, bench["dev"], data.rf))
        sim2 = simulate(target, r1, r2, rc_etf, data.price, *periods["second"], continuous=cont)
        m2 = metrics(sim2, bench["second"], data.rf)
        for k in ("cagr", "excess_cagr_vs_qqq", "max_dd", "sharpe", "calmar", "switches_per_year", "t_monthly_excess"):
            row[f"second_{k}"] = m2[k]
        for tag, kw in [("sameday", dict(lag=0)), ("cash0", dict(cash_is_etf=False)), ("dear2x", {})]:
            rc = rc_zero if tag == "cash0" else rc_etf
            rr2 = r2_dear if tag == "dear2x" else r2
            s = simulate(target, r1, rr2, rc, data.price, *periods["dev"], continuous=cont, **kw)
            bm = bench["dev"] if tag != "cash0" else simulate(const1, r1, r2, rc_zero, data.price, *periods["dev"])
            mm = metrics(s, bm, data.rf)
            row[f"{tag}_cagr"], row[f"{tag}_excess"], row[f"{tag}_max_dd"] = mm["cagr"], mm["excess_cagr_vs_qqq"], mm["max_dd"]
        rows.append(row)
        monthly_keep[name] = monthly(sim["ret"])
        yearly_tab[name] = yearly(sim["ret"])
    grid = pd.DataFrame(rows)

    counted = grid[grid["counted"]]
    n = len(counted)
    hurdle = bonferroni_t(n)
    best = counted.sort_values("t_monthly_excess", ascending=False).iloc[0]
    dsr_best = deflated_sharpe(best["ir_monthly"], int(best["months"]), best["skew_mx"], best["kurt_mx"],
                               counted["ir_monthly"].values)
    grid["dsr"] = [deflated_sharpe(r.ir_monthly, int(r.months), r.skew_mx, r.kurt_mx, counted["ir_monthly"].values)["dsr"]
                   if r.counted else np.nan for r in grid.itertuples()]

    OUT.mkdir(parents=True, exist_ok=True)
    grid.to_csv(OUT / "grid.csv", index=False, float_format="%.6f")
    pd.DataFrame(yearly_tab).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(monthly_keep)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    summary = {"guard": data.guard, "data_checks": checks, "synthetic_2x_calibration": cal,
               "n_counted_configs": n, "bonferroni_t_one_sided_5pct": hurdle,
               "best_by_t": {"config": best["config"], "t": best["t_monthly_excess"], "dsr": dsr_best},
               "note": "development period only (1999-03-10 .. 2014-12-31); second period 1986-10 .. 1999-03 on NDX price"}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=float))
    print(f"{n} counted configs, Bonferroni one-sided 5% t = {hurdle:.2f}; best t {best['config']} "
          f"{best['t_monthly_excess']:.2f}, DSR {dsr_best['dsr']:.3f}")
    return summary


# ======================================================================== one-shot test (approved 2026-10-03)

ONESHOT_RULE = Rule("T01", 200, 0.02)          # frozen in docs/research_ledger_qqq_timing.md
ONESHOT_REPORT_ONLY = Rule("T02", 200, 0.02)
ONESHOT_ENTRY_CLOSE = "2014-12-31"             # enter at this close, so the first return is 2015-01-02
ONESHOT_FIRST_RETURN = "2015-01-02"
FROZEN_SPREAD = 0.0069599408878067825          # calibrated on QLD 2006-06-22 .. 2014-12-31 (dev run)
OUT_ONESHOT = output_dir("qqq_timing_oneshot")


def latest_complete_month_end(today: pd.Timestamp) -> pd.Timestamp:
    return (pd.Timestamp(today).normalize().replace(day=1) - pd.Timedelta(days=1))


def qld_tracking(data: DevData, spread: float, start: str, end: str) -> dict:
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


def run_oneshot(args) -> dict:
    end = pd.Timestamp(args.test_end) if args.test_end else latest_complete_month_end(pd.Timestamp.today())
    end_s = str(end.date())
    data = load_dev_data(end_s)
    last = data.sessions.max()
    if last > end or (end - last).days > 4:
        raise ValueError(f"last session {last.date()} does not close the month ending {end_s}")
    print("ONE-SHOT TEST: frames truncated at", end_s, {k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)})
    kf_last = data.guard["kf_rf"]["last"]
    cal = calibrate_spread(data, QLD_CAL_START, DEV_END)       # same dev window: must reproduce the frozen value
    if abs(cal["spread_calibrated"] - FROZEN_SPREAD) > 1e-6:
        raise ValueError(f"calibrated spread {cal['spread_calibrated']} differs from the frozen {FROZEN_SPREAD}")
    spread = FROZEN_SPREAD
    p = (1 + data.r1).cumprod()
    r1 = data.r1
    rc = data.rf - CASH_ETF_FEE / TRADING_DAYS
    r2 = synthetic_2x(r1, data.rf, spread)
    const1 = pd.Series(1.0, index=data.sessions)
    bench = simulate(const1, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
    assert str(bench["ret"].index[0].date()) == ONESHOT_FIRST_RETURN
    res = {"window": [ONESHOT_FIRST_RETURN, str(bench["ret"].index[-1].date())], "sessions": int(len(bench["ret"])),
           "kf_rf_last": kf_last, "rf_after_kf": "DTB3 / 252", "spread": spread}
    m_q = metrics(bench, bench, data.rf)
    res["QQQ_buyhold"] = m_q
    by_year = {"QQQ_buyhold": yearly(bench["ret"])}
    mon = {"QQQ_buyhold": monthly(bench["ret"])}
    for rule in (ONESHOT_RULE, ONESHOT_REPORT_ONLY):
        tgt = target_exposure(rule, p, r1)
        sim = simulate(tgt, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
        m = metrics(sim, bench, data.rf)
        m["state_at_entry_close"] = float(tgt.loc[ONESHOT_ENTRY_CLOSE])
        m["switches"] = int(sim["rebalances"])
        e = sim["exposure_held"]
        m["switch_dates_effective"] = [str(d.date()) for d in e.index[1:][e.values[1:] != e.values[:-1]]]
        res[rule.name] = m
        by_year[rule.name] = yearly(sim["ret"])
        mon[rule.name] = monthly(sim["ret"])
    res["criteria_" + ONESHOT_RULE.name] = evaluate_criteria(res[ONESHOT_RULE.name], m_q)
    res["qld_tracking_2015_on"] = qld_tracking(data, spread, ONESHOT_FIRST_RETURN, end_s)
    OUT_ONESHOT.mkdir(parents=True, exist_ok=True)
    (OUT_ONESHOT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(by_year).to_csv(OUT_ONESHOT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT_ONESHOT / "monthly_returns.csv", float_format="%.6f")
    crit = res["criteria_" + ONESHOT_RULE.name]
    print(json.dumps({"criteria": crit, **{k: {x: res[k][x] for x in ("cagr", "max_dd", "calmar")}
                                           for k in ("QQQ_buyhold", ONESHOT_RULE.name, ONESHOT_REPORT_ONLY.name)}},
                     indent=1, default=float))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["dev", "oneshot"], default="dev",
                    help="dev: 1999-2014 grid behind the 2014-12-31 guard; oneshot: the frozen rule on 2015-01..")
    ap.add_argument("--test-end", default=None, help="oneshot only: last date (default: latest complete month end)")
    args = ap.parse_args(argv)
    if args.mode == "oneshot":
        run_oneshot(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
