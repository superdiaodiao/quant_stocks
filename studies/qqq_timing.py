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
quant.backtest.exposure, quant.evaluation); the rules, the rule grid, the 2x calibration and the data checks are
quant.strategies.qqq_timing. This file holds the frozen parameters and the two runs (dev grid, one-shot test).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study qqq_timing [--mode oneshot --test-end YYYY-MM-DD]
        (or scripts/research_qqq_timing.py)
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.backtest.costs import CASH_ETF_FEE
from quant.backtest.exposure import simulate, synthetic_2x
from quant.data import index_history
from quant.data.guards import assert_dev_dates
from quant.data.index_history import QQQ_FIRST_CLOSE, NDX_LAST_BEFORE_QQQ
from quant.evaluation.criteria import bonferroni_t, deflated_sharpe, timing_criteria
from quant.evaluation.metrics import TRADING_DAYS, exposure_metrics as metrics, monthly, yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json, write_period_tables
from quant.strategies.qqq_timing import (
    Rule, calibrate_spread, data_checks, latest_complete_month_end, qld_tracking, rule_grid, target_exposure,
)

DEV_END = "2014-12-31"
DEV_START = QQQ_FIRST_CLOSE       # QQQ's first close; strategies and the benchmark enter at this close
SECOND_START = "1986-10-01"       # second reference period on NDX price (after a 250-session warm-up)
SECOND_END = NDX_LAST_BEFORE_QQQ
QLD_CAL_START = "2006-06-22"      # first QLD close is 2006-06-21; first daily return 2006-06-22

CACHE = index_history.QQQ_RAW
TIINGO_QQQ = CACHE_ROOT / "holdout_2011_2019/qqq_tiingo_2010_2020.csv"
OUT = output_dir("qqq_timing_dev")

# one-shot test (approved 2026-10-03)
ONESHOT_RULE = Rule("T01", 200, 0.02)          # frozen in docs/research_ledger_qqq_timing.md
ONESHOT_REPORT_ONLY = Rule("T02", 200, 0.02)
ONESHOT_ENTRY_CLOSE = "2014-12-31"             # enter at this close, so the first return is 2015-01-02
ONESHOT_FIRST_RETURN = "2015-01-02"
FROZEN_SPREAD = 0.0069599408878067825          # calibrated on QLD 2006-06-22 .. 2014-12-31 (dev run)
OUT_ONESHOT = output_dir("qqq_timing_oneshot")


# ======================================================================== development run

def run(args) -> dict:
    data = index_history.load_dev_data(DEV_END, CACHE)
    print("DATE GUARD: every vendor frame truncated at", DEV_END, "and asserted;",
          {k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)})
    assert_dev_dates(data.sessions, DEV_END)
    checks = data_checks(data, TIINGO_QQQ, DEV_START, DEV_END)
    cal = calibrate_spread(data, QLD_CAL_START, DEV_END, DEV_END)
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
        assert_dev_dates(sim["ret"].index, DEV_END)
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
    write_period_tables(OUT, yearly_tab, monthly_keep)
    summary = {"guard": data.guard, "data_checks": checks, "synthetic_2x_calibration": cal,
               "n_counted_configs": n, "bonferroni_t_one_sided_5pct": hurdle,
               "best_by_t": {"config": best["config"], "t": best["t_monthly_excess"], "dsr": dsr_best},
               "note": "development period only (1999-03-10 .. 2014-12-31); second period 1986-10 .. 1999-03 on NDX price"}
    write_json(OUT / "summary.json", summary, default=float)
    print(f"{n} counted configs, Bonferroni one-sided 5% t = {hurdle:.2f}; best t {best['config']} "
          f"{best['t_monthly_excess']:.2f}, DSR {dsr_best['dsr']:.3f}")
    return summary


# ======================================================================== one-shot test

def run_oneshot(args) -> dict:
    end = pd.Timestamp(args.test_end) if args.test_end else latest_complete_month_end(pd.Timestamp.today())
    end_s = str(end.date())
    data = index_history.load_dev_data(end_s, CACHE)
    last = data.sessions.max()
    if last > end or (end - last).days > 4:
        raise ValueError(f"last session {last.date()} does not close the month ending {end_s}")
    print("ONE-SHOT TEST: frames truncated at", end_s, {k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)})
    kf_last = data.guard["kf_rf"]["last"]
    cal = calibrate_spread(data, QLD_CAL_START, DEV_END, DEV_END)   # same dev window: must reproduce the frozen value
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
    res["criteria_" + ONESHOT_RULE.name] = timing_criteria(res[ONESHOT_RULE.name], m_q)
    res["qld_tracking_2015_on"] = qld_tracking(data, spread, ONESHOT_FIRST_RETURN, end_s)
    OUT_ONESHOT.mkdir(parents=True, exist_ok=True)
    write_json(OUT_ONESHOT / "results.json", res, default=float)
    write_period_tables(OUT_ONESHOT, by_year, mon)
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
