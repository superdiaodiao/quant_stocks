"""Dow Theory market timing: primary trend from swing highs / lows of the Dow Jones Industrial (^DJI) and
Transportation (^DJT) averages, with mutual confirmation. Bull = 100% QQQ, bear = 100% T-bill ETF.

Pre-registered in docs/research_ledger_dow_theory.md (section 0) before the first run.

Swing points (each average separately, closes only, data up to the decision close t):
- In an up-leg the highest close H is tracked. H is *confirmed* as a swing high on the first day t on which the
  reaction from H satisfies the family's condition; the leg then turns down, starting from the lowest close after H.
  Down-legs are symmetric. Confirmation is dated t (never back-filled to the day of H).
- Condition: decline from H to the lowest close since H >= pct; family R also requires t - index(H) >= min_days
  sessions and a retracement of at least ``retrace`` (1/3) of the preceding leg, which starts at the latest swing
  low before H (version 2: own pivots or pivots of a pct + min_days tracker without the retracement test; version 1,
  family R1, used only the tracker's own pivots and locked up in long trends -- see the ledger).
Break direction of one average: close above its last confirmed swing high -> up; below its last confirmed swing
low -> down; otherwise unchanged. Family P ("strict pattern") counts an up break only when the last swing low is
higher than the one before (higher low), a down break only after a lower high.
Primary trend: bull when both averages' break directions are up, bear when both are down, otherwise unchanged
(non-confirmation keeps the trend). Before the first signal the state is bull.

Execution, costs, metrics, the 2014-12-31 date guard and the one-shot pass criteria are the QQQ timing
study's, from quant (quant.data.index_history, quant.backtest.exposure, quant.evaluation); the swing detection, the
rules and the Dow loader are quant.strategies.dow_theory. Raw data stays local in research_cache/qqq_timing/raw/.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study dow_theory [--mode oneshot --test-end 2026-09-30]
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.backtest.costs import CASH_ETF_FEE
from quant.backtest.exposure import simulate
from quant.data.guards import assert_dev_dates
from quant.data.index_history import NDX_LAST_BEFORE_QQQ, QQQ_FIRST_CLOSE, QQQ_RAW, load_dev_data
from quant.evaluation.criteria import bonferroni_t, deflated_sharpe, timing_criteria
from quant.evaluation.metrics import TRADING_DAYS, exposure_metrics, monthly, yearly
from quant.paths import output_dir
from quant.reporting import write_json, write_period_tables
from quant.strategies.dow_theory import dow_checks, dow_state, load_dow, rule_grid, signal_list, target_on_sessions

DEV_END = "2014-12-31"              # the QQQ timing development end and start (docs/research_ledger_qqq_timing.md)
DEV_START = QQQ_FIRST_CLOSE
DJ_FILES = {"DJI": QQQ_RAW / "chart_%5EDJI.json", "DJT": QQQ_RAW / "chart_%5EDJT.json"}
SECOND_START = "1993-01-04"        # 1992 is the swing warm-up (Yahoo ^DJI / ^DJT start 1992-01-02)
SECOND_END = NDX_LAST_BEFORE_QQQ
OUT = output_dir("dow_theory")
FROZEN = "R_p5_D15"                # chosen before the development run (ledger section 0)
ONESHOT_ENTRY_CLOSE = "2014-12-31"
ONESHOT_FIRST_RETURN = "2015-01-02"
ONESHOT_END = "2026-09-30"         # the pre-registered one-shot window end


# ======================================================================== runs

def run_dev(args) -> dict:
    data = load_dev_data(DEV_END)
    closes, g = load_dow(DJ_FILES, DEV_END)
    guard = {**{k: v["last"] for k, v in data.guard.items() if isinstance(v, dict)}, **{k: v["last"] for k, v in g.items()}}
    print("DATE GUARD: every frame truncated at", DEV_END, "and asserted;", guard)
    checks = dow_checks(closes, data.sessions, "1992-01-02", DEV_END)
    r1 = data.r1
    rc_etf = data.rf - CASH_ETF_FEE / TRADING_DAYS
    rc_zero = pd.Series(0.0, index=data.sessions)
    r2 = pd.Series(0.0, index=data.sessions)      # no 2x sleeve in this study
    periods = {"dev": (DEV_START, DEV_END), "second": (SECOND_START, SECOND_END)}
    const1 = pd.Series(1.0, index=data.sessions)
    bench = {k: simulate(const1, r1, r2, rc_etf, data.price, *v) for k, v in periods.items()}
    bench_cash0 = simulate(const1, r1, r2, rc_zero, data.price, *periods["dev"])
    m_q = exposure_metrics(bench["dev"], bench["dev"], data.rf)
    m_q2 = exposure_metrics(bench["second"], bench["second"], data.rf)

    rows, yearly_tab, monthly_keep, signals = [], {"QQQ_buyhold": yearly(bench["dev"]["ret"])}, {}, {}
    rules = rule_grid()
    assert len(rules) == 18, len(rules)
    for rule in rules:
        state = dow_state(rule, closes)
        assert_dev_dates(state.index, DEV_END)
        tgt = target_on_sessions(state, data.sessions)
        row = {"config": rule.name, "family": rule.family, "pct": rule.pct, "min_days": rule.min_days,
               "retrace": rule.retrace, "averages": "+".join(rule.averages)}
        sim = simulate(tgt, r1, r2, rc_etf, data.price, *periods["dev"])
        assert_dev_dates(sim["ret"].index, DEV_END)
        m = exposure_metrics(sim, bench["dev"], data.rf)
        row.update(m)
        crit = timing_criteria(m, m_q)
        row.update({f"dev_{k}": v for k, v in crit.items()})
        sim2 = simulate(tgt, r1, r2, rc_etf, data.price, *periods["second"])
        m2 = exposure_metrics(sim2, bench["second"], data.rf)
        for k in ("cagr", "excess_cagr_vs_qqq", "max_dd", "calmar", "switches_per_year", "time_cash"):
            row[f"second_{k}"] = m2[k]
        s0 = simulate(tgt, r1, r2, rc_etf, data.price, *periods["dev"], lag=0)
        row["sameday_excess"] = exposure_metrics(s0, bench["dev"], data.rf)["excess_cagr_vs_qqq"]
        sc = simulate(tgt, r1, r2, rc_zero, data.price, *periods["dev"], cash_is_etf=False)
        row["cash0_excess"] = exposure_metrics(sc, bench_cash0, data.rf)["excess_cagr_vs_qqq"]
        rows.append(row)
        yearly_tab[rule.name] = yearly(sim["ret"])
        monthly_keep[rule.name] = monthly(sim["ret"])
        signals[rule.name] = signal_list(state, SECOND_START, DEV_END)
    grid = pd.DataFrame(rows)
    n = len(grid)
    hurdle = bonferroni_t(n)
    grid["dsr"] = [deflated_sharpe(r.ir_monthly, int(r.months), r.skew_mx, r.kurt_mx, grid["ir_monthly"].values)["dsr"]
                   for r in grid.itertuples()]
    out = OUT / "dev"
    out.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    write_period_tables(out, yearly_tab, monthly_keep)
    summary = {"guard": {**data.guard, "dow": g}, "dow_checks": checks, "n_configs": n,
               "bonferroni_t_one_sided_5pct": hurdle, "QQQ_buyhold_dev": m_q, "NDX_buyhold_second": m_q2,
               "signals_1993_2014": signals, "frozen_pre_registered": FROZEN}
    write_json(out / "summary.json", summary, default=float)
    print(f"{n} configs, Bonferroni one-sided 5% t = {hurdle:.2f}")
    cols = ["config", "cagr", "excess_cagr_vs_qqq", "max_dd", "calmar", "time_cash", "switches_per_year",
            "t_monthly_excess", "dsr", "second_excess_cagr_vs_qqq", "second_max_dd"]
    print(f"QQQ dev: cagr {m_q['cagr']:.4f} mdd {m_q['max_dd']:.4f} calmar {m_q['calmar']:.3f}; "
          f"NDX second: cagr {m_q2['cagr']:.4f} mdd {m_q2['max_dd']:.4f}")
    print(grid[cols].to_string(index=False, float_format=lambda v: f"{v:.3f}"))
    return summary


def run_oneshot(args) -> dict:
    end_s = args.test_end
    if end_s != ONESHOT_END:
        raise ValueError("the pre-registered one-shot window ends 2026-09-30")
    data = load_dev_data(end_s)
    closes, g = load_dow(DJ_FILES, end_s)
    last = data.sessions.max()
    if last > pd.Timestamp(end_s) or (pd.Timestamp(end_s) - last).days > 4:
        raise ValueError(f"last session {last.date()} does not close the month ending {end_s}")
    print("ONE-SHOT TEST: frames truncated at", end_s, {k: v["last"] for k, v in g.items()},
          "kf_rf last", data.guard["kf_rf"]["last"])
    rule = {r.name: r for r in rule_grid()}[FROZEN]
    r1 = data.r1
    rc = data.rf - CASH_ETF_FEE / TRADING_DAYS
    r2 = pd.Series(0.0, index=data.sessions)
    const1 = pd.Series(1.0, index=data.sessions)
    bench = simulate(const1, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
    assert str(bench["ret"].index[0].date()) == ONESHOT_FIRST_RETURN
    state = dow_state(rule, closes)
    tgt = target_on_sessions(state, data.sessions)
    sim = simulate(tgt, r1, r2, rc, data.price, ONESHOT_ENTRY_CLOSE, end_s)
    m_q = exposure_metrics(bench, bench, data.rf)
    m = exposure_metrics(sim, bench, data.rf)
    e = sim["exposure_held"]
    m["state_at_entry_close"] = float(tgt.loc[ONESHOT_ENTRY_CLOSE])
    m["switches"] = int(sim["rebalances"])
    chg = np.flatnonzero(e.values[1:] != e.values[:-1]) + 1
    m["switch_dates_effective"] = [f"{e.index[i].date()} {'QQQ' if e.values[i] > 0.5 else 'T-bill'}" for i in chg]
    m["signals"] = signal_list(state, "2014-12-01", end_s)
    res = {"rule": FROZEN, "window": [ONESHOT_FIRST_RETURN, str(bench["ret"].index[-1].date())],
           "sessions": int(len(bench["ret"])), "dow_guard": g, "kf_rf_last": data.guard["kf_rf"]["last"],
           "QQQ_buyhold": m_q, FROZEN: m, "criteria": timing_criteria(m, m_q)}
    out = OUT / "oneshot"
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "results.json", res, default=float)
    write_period_tables(out, {"QQQ_buyhold": yearly(bench["ret"]), FROZEN: yearly(sim["ret"])},
                        {"QQQ_buyhold": monthly(bench["ret"]), FROZEN: monthly(sim["ret"])})
    print(json.dumps({"criteria": res["criteria"],
                      **{k: {x: res[k][x] for x in ("cagr", "max_dd", "calmar", "vol", "sharpe", "time_cash",
                                                    "t_monthly_excess")} for k in ("QQQ_buyhold", FROZEN)}},
                     indent=1, default=float))
    print("switches:", m["switches"], m["switch_dates_effective"])
    print("signals:", m["signals"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--mode", choices=["dev", "oneshot"], default="dev")
    ap.add_argument("--test-end", default=None, help="oneshot only: 2026-09-30")
    args = ap.parse_args(argv)
    run_oneshot(args) if args.mode == "oneshot" else run_dev(args)


if __name__ == "__main__":
    main()
