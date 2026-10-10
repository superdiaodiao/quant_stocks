"""Re-score already-saved research results against a Nasdaq Composite total-return benchmark.

No rule is changed and no strategy is re-run: every rule return series is read from the files the original
research scripts saved under output/research_only/. Only the benchmark changes.

Benchmarks (raw Yahoo chart JSON, kept local under research_cache/benchmarks/):
  * ONEQ adjusted close  -- Fidelity Nasdaq Composite Index ETF, total return net of its ~0.21% fee (primary).
  * ^IXIC close + yield  -- Composite price index plus an estimated dividend yield (cross-check). The yield for
                            calendar year y = ONEQ cash dividends paid in y / mean ONEQ (split-adjusted) close in y,
                            plus the 0.21% expense ratio added back (fund distributions are net of fees), spread
                            evenly over the sessions of that year.
  * QQQ adjusted close   -- the benchmark the research originally used, for reference.

The benchmark loader is quant.data.composite_benchmark and the re-scoring conventions quant.evaluation.rescore;
this file reads the saved rule series (one function per file layout) and re-applies the criteria post hoc.

Usage: PYTHONPATH=. .venv/bin/python -m quant study benchmark_composite   (or scripts/research_benchmark_composite.py)
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.composite_benchmark import load_benchmarks
from quant.evaluation.metrics import max_drawdown as max_dd
from quant.evaluation.rescore import bench_window, cagr_calendar as cagr, summarise, tables
from quant.evaluation.stats import t_of_mean
from quant.paths import CACHE_ROOT, OUTPUT_ROOT, ROOT
from quant.reporting import write_json

RES = OUTPUT_ROOT
OUT = RES / "benchmark_composite"
RAW = CACHE_ROOT / "benchmarks"
ONEQ_ER = 0.0021


# ----------------------------------------------------------------------------- cases

def daily_nav_case(name, label, path, level, rebase=None, end=None, rule_col="nav"):
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date")
    nav = df[rule_col]
    if rebase is not None:
        nav = nav[nav.index >= pd.Timestamp(rebase)]
    if end is not None:
        nav = nav[nav.index <= pd.Timestamp(end)]
    nav = nav / nav.iloc[0] * 1.0
    bnav = bench_window(level, nav.index[0], nav.index[-1], nav.index)
    full = bench_window(level, nav.index[0], nav.index[-1])
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    # cross-check our QQQ against the QQQ series the research script saved
    saved_q = df["qqq"].reindex(nav.index)
    saved_q = saved_q / saved_q.iloc[0]
    q_gap = float(cagr(saved_q.iloc[-1], nav.index[0], nav.index[-1]) - cagr(bnav["QQQ"].iloc[-1], nav.index[0], nav.index[-1]))
    m, y = tables(nav, bnav)
    s = summarise(name, label, nav, bnav, max_dd(nav), bmdd, m, y,
                  {"check_saved_qqq_cagr_minus_yahoo_qqq_cagr": q_gap, "rule_source": str(path.relative_to(ROOT))})
    return s, m, y


def monthly_case(name, label, folder, col, level, base, end):
    mon = pd.read_csv(folder / "monthly_returns.csv")
    res = json.loads((folder / "results.json").read_text())
    base, end = pd.Timestamp(base), pd.Timestamp(end)
    full = bench_window(level, base, end)
    me = full.iloc[1:].groupby(full.index[1:].to_period("M")).tail(1)
    bm = me / pd.concat([full.iloc[[0]], me.iloc[:-1]]).values - 1
    bm.index = bm.index.to_period("M").astype(str)
    mon = mon.set_index("date")
    if list(mon.index) != list(bm.index):
        raise ValueError("month labels differ")
    # rule NAV at month ends (base = 1 at the entry close)
    rnav = pd.concat([pd.Series([1.0], index=[base]),
                      pd.Series((1 + mon[col]).cumprod().values, index=me.index)])
    bnav = pd.concat([full.iloc[[0]], me])
    m = pd.DataFrame({"rule": mon[col].values}, index=me.index.to_period("M"))
    for b in bm.columns:
        m[b] = bm[b].values
    yy = (1 + m).groupby(m.index.year).prod() - 1
    for t in (m, yy):
        t["excess_vs_ONEQ"] = t["rule"] - t["ONEQ"]
        t["excess_vs_QQQ"] = t["rule"] - t["QQQ"]
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    q_saved = mon["QQQ_buyhold"]
    s = summarise(name, label, rnav, bnav, float(res[col]["max_dd"]), bmdd, m, yy,
                  {"rule_max_dd_note": "daily max drawdown as saved in results.json by the original one-shot run",
                   "check_saved_qqq_monthly_max_abs_diff": float((q_saved.values - bm["QQQ"].values).__abs__().max()),
                   "saved_cagr_daily_252": float(res[col]["cagr"]),
                   "rule_source": str((folder / "monthly_returns.csv").relative_to(ROOT))})
    return s, m, yy


def reversal_case(level):
    path = RES / "reversal_dev_2012_2016" / "weekly" / "dv50_N100_K3_monday_ov30.csv"
    w = pd.read_csv(path, parse_dates=["entry", "exit"])
    if not (w["exit"].iloc[:-1].values == w["entry"].iloc[1:].values).all():
        raise ValueError("weekly holding periods are not contiguous")
    r = w["qqq"] + w["excess_net"]
    chk = (w["nav"].shift(-1) / w["nav"] - 1).iloc[:-1]
    if float((chk - r.iloc[:-1]).abs().max()) > 1e-9:
        raise ValueError("weekly return does not reproduce the saved NAV")
    dates = pd.DatetimeIndex([w["entry"].iloc[0]] + list(w["exit"]))
    nav = pd.Series(np.r_[1.0, (1 + r).cumprod().values], index=dates)
    bnav = bench_window(level, dates[0], dates[-1], dates)
    full = bench_window(level, dates[0], dates[-1])
    bmdd = {b: max_dd(full[b]) for b in full.columns}
    saved_q = pd.Series(np.r_[1.0, (1 + w["qqq"]).cumprod().values], index=dates)
    q_gap = float(cagr(saved_q.iloc[-1], dates[0], dates[-1]) - cagr(bnav["QQQ"].iloc[-1], dates[0], dates[-1]))
    m, y = tables(nav, bnav)
    s = summarise("reversal_rev_ind_v1", "DEV ONLY (in-sample, never tested): rev-ind-v1 = dv50_N100_K3_monday_ov30, "
                  "100% QQQ + 30/30 overlay, 2012-2016", nav, bnav, max_dd(nav), bmdd, m, y,
                  {"rule_max_dd_note": "on weekly (Monday-close) NAV; benchmark drawdowns are daily",
                   "rule_max_dd_weekly_benchmarks": {b: max_dd(bnav[b]) for b in bnav.columns},
                   "overlay_excess_cagr_vs_qqq_from_ledger": 0.101,
                   "check_saved_qqq_cagr_minus_yahoo_qqq_cagr": q_gap,
                   "month_assignment": "each weekly holding period is assigned to the month of its exit date",
                   "rule_source": str(path.relative_to(ROOT))})
    return s, m, y


def criteria_recheck(c: dict, out_dir: Path) -> dict:
    """The pre-registered pass rules, re-applied with ONEQ in place of QQQ. Post hoc: information only."""
    out = {"note": "post hoc: benchmark changed after the results were seen; does not replace the original verdicts"}
    for k in ("qqq200_T01_L200_b2", "dow_R_p5_D15"):
        s = c[k]
        calmar_r = s["cagr"]["rule"] / abs(s["max_dd"]["rule"])
        calmar_b = s["cagr"]["ONEQ"] / abs(s["max_dd"]["ONEQ"])
        dd_pts = s["max_dd"]["rule"] - s["max_dd"]["ONEQ"]
        gap = s["cagr"]["rule"] - s["cagr"]["ONEQ"]
        r = {"c1_maxdd_shallower_pts": dd_pts, "c1_pass_ge_10pp": dd_pts >= 0.10,
             "c2_calmar_rule": calmar_r, "c2_calmar_ONEQ": calmar_b, "c2_pass": calmar_r > calmar_b,
             "c3_cagr_gap": gap, "c3_pass_shortfall_le_3pp": gap >= -0.03}
        r["pass_all_three"] = bool(r["c1_pass_ge_10pp"] and r["c2_pass"] and r["c3_pass_shortfall_le_3pp"])
        out[k] = r
    t1, t2 = c["livermore20_test1"], c["livermore20_test2_judged"]
    mx = pd.concat([pd.read_csv(out_dir / f"{n}_monthly.csv", index_col=0).eval("rule - ONEQ")
                    for n in ("livermore20_test1", "livermore20_test2_judged")])
    a = {"A1_test1_excess": t1["excess_cagr"]["vs_ONEQ"], "A2_test2_excess": t2["excess_cagr"]["vs_ONEQ"],
         "A3_combined_months": int(len(mx)), "A3_combined_t": t_of_mean(mx, 2)}
    a["pass_A"] = bool(a["A1_test1_excess"] > 0 and a["A2_test2_excess"] > 0 and a["A3_combined_t"] >= 2)
    for n, s in (("test1", t1), ("test2", t2)):
        a[f"B_{n}_dd_shallower_pts"] = s["max_dd"]["rule"] - s["max_dd"]["ONEQ"]
    out["livermore20"] = a
    return out


def main(argv=None):
    level, yld = load_benchmarks(RAW, ONEQ_ER)
    OUT.mkdir(parents=True, exist_ok=True)
    cases = [
        monthly_case("qqq200_T01_L200_b2", "ONE-SHOT TEST: QQQ 200-day rule T01_L200_b2, 2015-01-02..2026-09-30",
                     RES / "qqq_timing_oneshot", "T01_L200_b2", level, "2014-12-31", "2026-09-30"),
        monthly_case("dow_R_p5_D15", "ONE-SHOT TEST: Dow theory R_p5_D15, 2015-01-02..2026-09-30",
                     RES / "dow_theory" / "oneshot", "R_p5_D15", level, "2014-12-31", "2026-09-30"),
        daily_nav_case("livermore20_test1", "ONE-SHOT TEST 1: Livermore #20, 2023-01-03..2026-08-31",
                       RES / "livermore" / "oneshot_test1" / "daily_nav.csv", level),
        daily_nav_case("livermore20_test2_judged", "ONE-SHOT TEST 2 (judged part): Livermore #20, rebased at the "
                       "2013-12-31 close, 2014-01-02..2016-12-30",
                       RES / "livermore" / "oneshot_test2" / "daily_nav.csv", level, rebase="2013-12-31"),
        daily_nav_case("canslim215_dev", "DEV ONLY (in-sample, never tested): CAN SLIM #215, 2017-2022",
                       RES / "canslim_dev_2017_2022" / "best_active_daily_nav.csv", level),
        reversal_case(level),
    ]
    summary = {"benchmarks": {
        "ONEQ": "Yahoo adjusted close of ONEQ (dividends reinvested, net of ~0.21%/yr fee) -- primary",
        "IXIC_TR_est": "^IXIC close + estimated dividend yield (ONEQ distributions / mean ONEQ close per calendar "
                       "year + 0.21% fee added back), spread evenly over that year's sessions -- cross-check",
        "QQQ": "Yahoo adjusted close of QQQ -- the original benchmark",
        "raw_files": str(RAW),
        "estimated_composite_yield_by_year": {str(k): round(v, 5) for k, v in yld.items()}},
        "cases": {}}
    for s, m, y in cases:
        summary["cases"][s["name"]] = s
        mm = m.copy()
        mm.index = mm.index.astype(str)
        mm.to_csv(OUT / f"{s['name']}_monthly.csv", float_format="%.6f")
        mm.index.name, y.index.name = "month", "year"
        y.to_csv(OUT / f"{s['name']}_by_year.csv", float_format="%.6f")
    summary["criteria_recheck_post_hoc"] = criteria_recheck(summary["cases"], OUT)
    write_json(OUT / "summary.json", summary, default=float)
    print(json.dumps(summary["criteria_recheck_post_hoc"], indent=1, default=float))
    rows = []
    for s in summary["cases"].values():
        rows.append({"case": s["name"], "start_base": s["base_close"], "end": s["end"],
                     "cagr_rule": s["cagr"]["rule"], "cagr_ONEQ": s["cagr"]["ONEQ"],
                     "cagr_IXIC_TR_est": s["cagr"]["IXIC_TR_est"], "cagr_QQQ": s["cagr"]["QQQ"],
                     "excess_vs_ONEQ": s["excess_cagr"]["vs_ONEQ"], "excess_vs_IXIC_TR_est": s["excess_cagr"]["vs_IXIC_TR_est"],
                     "excess_vs_QQQ": s["excess_cagr"]["vs_QQQ"],
                     "mdd_rule": s["max_dd"]["rule"], "mdd_ONEQ": s["max_dd"]["ONEQ"], "mdd_QQQ": s["max_dd"]["QQQ"],
                     "months": s["months"], "t_monthly_vs_ONEQ": s["monthly_excess_vs_ONEQ"]["t"],
                     "t_monthly_vs_IXIC_TR_est": s["monthly_excess_vs_IXIC_TR_est"]["t"],
                     "t_monthly_vs_QQQ": s["monthly_excess_vs_QQQ"]["t"]})
    tab = pd.DataFrame(rows)
    tab.to_csv(OUT / "comparison.csv", index=False, float_format="%.6f")
    pd.set_option("display.width", 250)
    print(tab.round(4).to_string())
    for s in summary["cases"].values():
        print(s["name"], {k: v for k, v in s.items() if k.startswith("check")})


if __name__ == "__main__":
    main()
