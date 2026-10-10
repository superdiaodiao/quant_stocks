"""Sector ETF momentum rotation and moderate QQQ leverage (pre-registered in docs/research_ledger_sector_lev.md, section 0).

Part A, sector rotation (decided at each month end, traded at the next session's close, equal weight):
- A1  top 3 by 12-1 month momentum (M_1 / M_12 - 1) among XLK XLF XLE XLV XLI XLY XLP XLU XLB (+ SMH, SOXX once listed)
- A2  top 1 by the same momentum
- A3  top 3 by 6-month momentum (M_0 / M_6 - 1)
- A4  A1 while SPY's month-end close > its 10-month SMA, else IEF (T-bill ETF before IEF lists on 2002-07-30)

Part B, moderate leverage (QQQ + a 2x leg: synthetic before 2006-06-22, real QLD after; cash = T-bill ETF):
- B1  constant 1.25x (75% QQQ + 25% 2x leg), rebalanced at each month end
- B2  constant 1.5x
- B3  QQQ SMA200 with a 2% band on -> 1.5x, off -> 1.0x (trade on state change and at each month end)
- B4  on -> 1.25x, off -> 0.5x (50% QQQ + 50% T-bill ETF)

Execution: the target decided at the close of t (data up to t) is traded at the close of t+1. Benchmark: ONEQ total
return, spliced with the Nasdaq Composite price index before ONEQ lists (2003-10-01); QQQ is reported.
Costs: IBKR Pro Tiered, $10,000 start. The data / legs, the flagged target-weight engine and the equity metrics
are in quant (quant.data.sector_etfs, quant.backtest.flagged_weights, quant.evaluation.equity); the part A / B
signals and the risk report are quant.strategies.sector_lev.

Every vendor frame is truncated at ``END`` immediately after parsing and asserted. Raw data local only:
research_cache/sector_lev/raw/. Outputs: output/research_only/sector_lev/ (returns and metrics only).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study sector_lev   (or scripts/research_sector_lev.py)
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

import pandas as pd

from quant.backtest.flagged_weights import buy_hold, entry_index, simulate
from quant.data.guards import assert_dev_dates
from quant.data.sector_etfs import ONEQ_START, Data, data_checks, load_data
from quant.evaluation.criteria import ab_verdict as evaluate, bonferroni_t
from quant.evaluation.equity import period_metrics, window_stats
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, max_drawdown, monthly, yearly
from quant.paths import output_dir
from quant.reporting import write_json, write_period_tables
from quant.strategies.sector_lev import part_a_targets, part_b_targets, risk_report

END = "2026-09-30"
OUT = output_dir("sector_lev")

HALF_SPLIT = "2015-01-01"
CRASH_WINDOWS = {"1999_2002": ("1999-01-01", "2002-12-31"), "2007_2009": ("2007-01-01", "2009-12-31")}
RISK_WINDOWS = {"2000_2002": ("2000-01-01", "2002-12-31"), "2008": ("2008-01-01", "2008-12-31")}
LOCAL_PEAK_FROM = {"2008": "2007-01-01"}      # report-only: the 2008 crash measured from its 2007 high
SMA_LEN, BAND = 200, 0.02
B_EXPOSURE = {"B1": (1.25, 1.25), "B2": (1.5, 1.5), "B3": (1.5, 1.0), "B4": (1.25, 0.5)}    # (on, off)


@dataclass(frozen=True)
class Spec:
    name: str
    part: str
    description: str


SPECS = (
    Spec("A1", "A", "top 3 of 9 SPDRs + SMH/SOXX by 12-1 month momentum, equal weight, monthly"),
    Spec("A2", "A", "top 1 by 12-1 month momentum, monthly"),
    Spec("A3", "A", "top 3 by 6-month momentum, equal weight, monthly"),
    Spec("A4", "A", "A1 while SPY > 10-month SMA, else IEF (T-bill ETF before 2002-07-30)"),
    Spec("B1", "B", "constant 1.25x QQQ (75% QQQ + 25% 2x leg), monthly rebalance"),
    Spec("B2", "B", "constant 1.5x QQQ (50% QQQ + 50% 2x leg), monthly rebalance"),
    Spec("B3", "B", "QQQ SMA200 band 2%: on 1.5x, off 1.0x"),
    Spec("B4", "B", "QQQ SMA200 band 2%: on 1.25x, off 0.5x (half T-bill ETF)"),
)
SPEC = {s.name: s for s in SPECS}


def targets(name: str, d: Data) -> tuple[pd.DataFrame, pd.Series]:
    if SPEC[name].part == "A":
        return part_a_targets(name, d.adj)
    return part_b_targets(name, d.p_qqq, B_EXPOSURE, SMA_LEN, BAND)


def split(r: pd.Series) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    return r.index[r.index < HALF_SPLIT], r.index[r.index >= HALF_SPLIT]


# ======================================================================== main

def run_spec(spec: Spec, d: Data, not_before=None) -> dict:
    w, flag = targets(spec.name, d)
    s_i = entry_index(w, flag, d, spec.part == "A", not_before)
    sim = simulate(w, flag, d.rets, d.close, s_i)
    b, q = buy_hold("BENCH", d, s_i)["ret"], buy_hold("QQQ", d, s_i)["ret"]
    r = sim["ret"]
    assert r.index.equals(b.index) and r.index.equals(q.index)
    assert_dev_dates(r.index, END)
    i1, i2 = split(r)
    full = period_metrics(r, b, q, d.rf)
    h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
    h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
    return {"w": w, "flag": flag, "s_i": s_i, "sim": sim, "r": r, "b": b, "q": q, "full": full, "half1": h1,
            "half2": h2, "criteria": evaluate(full, h1, h2)}


def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;",
          {k: v["last"] for k, v in d.guard.items() if isinstance(v, dict)})
    checks = data_checks(d, END)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(SPECS),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(SPECS)), "configs": {}}
    rows, by_year, mon, crash_rows, risk_rows = [], {}, {}, [], []
    refs_done, starts = set(), []
    for spec in SPECS:
        m = run_spec(spec, d)
        m2 = run_spec(spec, d, not_before=ONEQ_START)
        starts.append(m["s_i"])
        crit = {"spliced": m["criteria"], "oneq_only": m2["criteria"],
                "pass": bool(m["criteria"]["pass"] and m2["criteria"]["pass"])}
        sim, r = m["sim"], m["r"]
        years = len(r) / TRADING_DAYS
        info = {"description": spec.description, "entry_close": str(d.sessions[m["s_i"]].date()),
                "full": m["full"], "half1": m["half1"], "half2": m["half2"],
                "oneq_only": {"entry_close": str(d.sessions[m2["s_i"]].date()), "full": m2["full"],
                              "half1": m2["half1"], "half2": m2["half2"]},
                "criteria": crit, "rebalances": sim["trades"], "rebalances_per_year": sim["trades"] / years,
                "orders": sim["orders"], "cost_drag_per_year": sim["cost_frac"] / years,
                "avg_weight": {k: float(v) for k, v in sim["held"].mean().items() if v > 0.005},
                "crash_windows": {}, "risk": risk_report(r, RISK_WINDOWS, LOCAL_PEAK_FROM)}
        for tag, (s, e) in CRASH_WINDOWS.items():
            info["crash_windows"][tag] = {"strategy": window_stats(r, s, e), "oneq": window_stats(m["b"], s, e),
                                          "qqq": window_stats(m["q"], s, e)}
            st = info["crash_windows"][tag]
            if st["strategy"]:
                crash_rows.append({"config": spec.name, "window": tag, "start": st["strategy"]["start"],
                                   "end": st["strategy"]["end"], "total": st["strategy"]["total"],
                                   "max_dd": st["strategy"]["max_dd"], "oneq_total": st["oneq"]["total"],
                                   "oneq_max_dd": st["oneq"]["max_dd"], "qqq_total": st["qqq"]["total"],
                                   "qqq_max_dd": st["qqq"]["max_dd"]})
        for who, series in (("strategy", r), ("ONEQ_spliced", m["b"]), ("QQQ", m["q"])):
            if who != "strategy" and (who, m["s_i"]) in refs_done:
                continue
            refs_done.add((who, m["s_i"]))
            rep = info["risk"] if who == "strategy" else risk_report(series, RISK_WINDOWS, LOCAL_PEAK_FROM)
            for tag, x in rep.items():
                ep = x["episode"] or {}
                risk_rows.append({"config": spec.name if who == "strategy" else f"{who}_from_{d.sessions[m['s_i']].date()}",
                                  "window": tag, "window_total": x["window_total"], **ep})
        sens = {}
        w, flag, s_i = m["w"], m["flag"], m["s_i"]
        variants = [("same_close", dict(lag=0)), ("no_costs", dict(costs=False))]
        if spec.part == "B":
            variants.append(("spread_plus_1pct", dict(extra_drag={"QLDX": 0.01})))
        for tag, kw in variants:
            s2 = simulate(w, flag, d.rets, d.close, s_i, **kw)
            sens[tag] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                         "cagr_minus_oneq": cagr_of(s2["ret"]) - m["full"]["oneq_cagr"]}
        info["sensitivities"] = sens
        res["configs"][spec.name] = info
        for tag, p in [("full", m["full"]), ("half1", m["half1"]), ("half2", m["half2"]),
                       ("oneq_full", m2["full"]), ("oneq_half1", m2["half1"]), ("oneq_half2", m2["half2"])]:
            rows.append({"config": spec.name, "period": tag, **p, "pass": crit["pass"] if tag == "full" else None})
        by_year[spec.name] = yearly(r)
        mon[spec.name] = monthly(r)
        f = m["full"]
        print(f"{spec.name} {f['start']}..{f['end']} CAGR {f['cagr']:+.2%} (ONEQ {f['oneq_cagr']:+.2%}, QQQ "
              f"{f['qqq_cagr']:+.2%}) MDD {f['max_dd']:.1%} (ONEQ {f['oneq_max_dd']:.1%}) t {f['t_monthly_excess_vs_oneq']:+.2f}"
              f" | H1 {m['half1']['cagr_minus_oneq']:+.2%} H2 {m['half2']['cagr_minus_oneq']:+.2%} pass={crit['pass']}")
    s0 = min(starts)
    for leg, tag in (("BENCH", "ONEQ_spliced_buyhold"), ("QQQ", "QQQ_buyhold")):
        rr = buy_hold(leg, d, s0)["ret"]
        by_year[tag] = yearly(rr)
        mon[tag] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, by_year, mon)
    pd.DataFrame(crash_rows).to_csv(OUT / "crash_windows.csv", index=False, float_format="%.6f")
    pd.DataFrame(risk_rows).to_csv(OUT / "leverage_risk.csv", index=False, float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
