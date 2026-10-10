"""Walk-forward parameter re-selection ("adapt to the recent market") on the fine grids of research_grid_check.py.

Pre-registered in docs/research_ledger_indicators.md section 9 BEFORE any walk-forward number was computed.
THIS IS A WEAK TEST: every year used here was seen before (sections 2-3, 5, 7) and the grid itself is post-hoc.

Every January 1 of year Y, pick the grid point with the best Sharpe (secondary: CAGR) over the trailing L years
(L in {1, 3, 5}; primary L = 3, Sharpe) using only returns dated before Y, then trade it during Y. The picked points
are stitched into ONE continuous simulation (positions carry across years; at the switch close the portfolio moves to
what the new parameters would hold; all costs, including switching trades, are charged).

Settings and grids, data, costs and simulators all reuse quant.strategies.indicators (incl. the grids of research_grid_check):
  index  ^IXIC OHLC signals, hold ONEQ (^IXIC price return before 2003-10-02), T-bill ETF when out;
         stand-alone runs from 1999-03-10; walk-forward 2002-01 .. 2026-09-30
  stock  weekly dollar-volume top 300, close >= $10, up to 5 names, $10k IBKR Tiered; one continuous window
         (prices from 2011-06, universe and performance from 2012-01, to the last price day 2026-08-31);
         walk-forward 2018-01 .. 2026-08-31
Decision timing: the pick for Y uses data through the last close of Y-1; decisions from that close on use Y's
parameters; every decision fills at the next close (as in section 0.3).

Picking, stand-alone and stitched runs: quant.strategies.walk_forward; metrics: quant.evaluation.nav
(``return_vs_bench_metrics``, ``yearly_vs_bench``) and ``criteria.ab_oneq_halves``. Settings, runs and reports here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study walk_forward [--setting index|stock|both]
        (or scripts/research_walk_forward.py)
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.data import guards, panel
from quant.data import version as dv
from quant.evaluation.criteria import ab_oneq_halves as judge
from quant.evaluation.metrics import cagr_of
from quant.evaluation.nav import return_vs_bench_metrics as ret_metrics
from quant.evaluation.nav import yearly_vs_bench
from quant.paths import CACHE_ROOT, ROOT
from quant.strategies import indicators as ri
from quant.strategies.indicators import OWNER_REF
from quant.strategies.walk_forward import (
    all_picks, check_engine_matches_reference, index_standalone, repick_index_truncated, repick_stock_truncated,
    stitch_index_target, stitched_stock, stock_standalone, switch_dates,
)

OUT = dv.versioned(ROOT / "output/research_only/walk_forward")
RAW = CACHE_ROOT / "indicators/raw"     # ^IXIC / ^VIX charts of the indicators study
FutureDataError = ri.FutureDataError

LOOKBACKS = (1, 3, 5)
METRICS = ("sharpe", "cagr")
PRIMARY = {"L": 3, "metric": "sharpe"}
KINDS = ("ma", "donchian")

INDEX_DATA_START, INDEX_END = ri.INDEX_DEV_START, ri.INDEX_TEST_END       # 1999-03-10 .. 2026-09-30
INDEX_WF_YEARS = range(2002, 2027)
INDEX_HALVES = (("2002-01-01", "2013-12-31"), ("2014-01-01", "2026-09-30"))
STOCK_WINDOW = {"perf_start": "2012-01-01", "perf_end": "2026-09-30", "price_start": "2011-06-01",
                "universe_start": "2012-01-01", "judged_from": "2018-01-01"}
STOCK_WF_YEARS = range(2018, 2027)
STOCK_HALVES = (("2018-01-01", "2021-12-31"), ("2022-01-01", "2026-08-31"))
ACCOUNT = 10_000.0
BONFERRONI_4 = 2.2414          # one-sided 5% / 4 primary tests (reported only)


def pick_table(setting: str, kind: str, L: int, m: str, by: dict) -> list[dict]:
    return [{"setting": setting, "kind": kind, "L": L, "metric": m, "year": y,
             "params": f"{p['key'][0]}/{p['key'][1]}", "trailing_value": p["value"],
             "window_first": p["window_first"], "window_last": p["window_last"]} for y, p in by.items()]


# ======================================================================== index setting

def run_index(verify_truncated: bool = True) -> tuple[list, list, dict]:
    data = ri.load_index_data(INDEX_END, RAW)
    for v in data.guard.values():
        if isinstance(v, dict):
            assert pd.Timestamp(v["last"]) <= pd.Timestamp(INDEX_END)
    rets, tgts = index_standalone(data, INDEX_DATA_START, INDEX_END, KINDS)
    picks = all_picks(rets, INDEX_WF_YEARS, data.rf, LOOKBACKS, METRICS)
    checked = repick_index_truncated(picks, INDEX_WF_YEARS, RAW, INDEX_DATA_START, KINDS) if verify_truncated else []

    sess = data.r1["COMP"].index
    start = str(sess[sess < pd.Timestamp(f"{INDEX_WF_YEARS[0]}-01-01")][-1].date())   # last close of 2001 (cash)
    bench = ri.index_sim(pd.Series(1.0, index=sess), data, "COMP", start, INDEX_END)
    qqq_r = data.qqq_r.reindex(bench["ret"].index).fillna(0.0)
    rows, pick_rows, detail = [], [], {}
    for (kind, L, m), by in picks.items():
        tgt, lab = stitch_index_target(tgts[kind], by, sess)
        sim = ri.index_sim(tgt, data, "COMP", start, INDEX_END)
        guards.assert_dev_dates(sim["ret"].index, INDEX_END)
        assert sim["ret"].index[0] >= pd.Timestamp(f"{INDEX_WF_YEARS[0]}-01-01")
        r = sim["ret"]
        full = ret_metrics(r, bench["ret"], data.rf)
        mm = ri.index_metrics(sim, bench, data.rf)
        assert abs(mm["cagr"] - full["cagr"]) < 1e-9
        halves = [ret_metrics(r[(r.index >= a) & (r.index <= b)], bench["ret"], data.rf) for a, b in INDEX_HALVES]
        j = judge(full, halves)
        n_switch = sum(by[y]["key"] != by[y - 1]["key"] for y in list(INDEX_WF_YEARS)[1:])
        fixed = {k: ret_metrics(rr[rr.index > pd.Timestamp(start)], bench["ret"], data.rf)["cagr"]
                 for k, rr in rets[kind].items()}
        rank = int(sum(v > full["cagr"] for v in fixed.values()) + 1)
        owner = OWNER_REF[kind]
        rows.append({"setting": "index", "kind": kind, "L": L, "metric": m,
                     "primary": L == PRIMARY["L"] and m == PRIMARY["metric"],
                     **{k: full[k] for k in ("start", "end", "cagr", "bench_cagr", "excess_cagr", "max_dd",
                                             "bench_max_dd", "sharpe", "t_monthly_excess", "months")},
                     "qqq_cagr": cagr_of(qqq_r),
                     "h1_cagr": halves[0]["cagr"], "h1_bench": halves[0]["bench_cagr"],
                     "h1_t": halves[0]["t_monthly_excess"],
                     "h2_cagr": halves[1]["cagr"], "h2_bench": halves[1]["bench_cagr"],
                     "h2_t": halves[1]["t_monthly_excess"],
                     "switches_param": n_switch, "trades_per_year": mm["trades_per_year"],
                     "cost_drag_per_year": mm["cost_drag_per_year"],
                     "rank_among_fixed_points_cagr": rank, "n_points": len(fixed),
                     "fixed_best_cagr_hindsight": max(fixed.values()),
                     "fixed_median_cagr": float(np.median(list(fixed.values()))),
                     "owner_fixed_cagr": fixed[owner], **j})
        pick_rows += pick_table("index", kind, L, m, by)
        detail[f"index_{kind}_L{L}_{m}"] = {"by_year": yearly_vs_bench(r, bench["ret"]),
                                            "halves": halves, "full": full, "judge": j}
    return rows, pick_rows, {"detail": detail, "truncated_recheck_years": checked}


# ======================================================================== stock setting

def stock_metrics_wf(res: dict, runner) -> tuple[dict, list[dict], dict]:
    nav = res["nav"]
    oneq, qqq = runner.benchmarks(res["dates"], ACCOUNT)
    wf_start = f"{STOCK_WF_YEARS[0]}-01-01"
    full = ri.stock_sub_metrics(nav, oneq, qqq, wf_start, res["cost"], res["exposure"])
    halves = []
    for a, b in STOCK_HALVES:
        keep = nav.index <= pd.Timestamp(b)
        halves.append(ri.stock_sub_metrics(nav[keep], oneq[keep], qqq[keep], a, res["cost"][keep],
                                           res["exposure"][keep]))
    return full, halves, oneq


def run_stock(years=STOCK_WF_YEARS) -> tuple[list, list, dict]:
    data = panel.load_window("wf", dict(STOCK_WINDOW))
    print(data.guard["assertion"])
    assert data.spec["effective_end"] <= "2026-08-31"
    runner = ri.StockRunner(data, RAW)
    rets, hold = stock_standalone(data, runner, years, KINDS, ACCOUNT)
    picks = all_picks(rets, years, None, LOOKBACKS, METRICS)
    rows, pick_rows, detail = [], [], {}
    sw0 = switch_dates(data, years)[0]
    for (kind, L, m), by in picks.items():
        res = stitched_stock(data, runner, kind, by, hold[kind])
        full, halves, oneq = stock_metrics_wf(res, runner)
        fj = {"cagr": full["cagr"], "bench_cagr": full["bench_cagr"], "max_dd": full["max_dd"],
              "bench_max_dd": full["bench_max_dd"], "t_monthly_excess": full["t_excess_monthly"]}
        j = judge(fj, [{"cagr": h["cagr"], "bench_cagr": h["bench_cagr"]} for h in halves])
        n_switch = sum(by[y]["key"] != by[y - 1]["key"] for y in list(years)[1:])
        fixed = {}
        for key, rr in rets[kind].items():
            x = rr[rr.index > sw0]
            fixed[key] = float(np.prod(1 + x.values) ** (365.25 / (x.index[-1] - sw0).days) - 1)
        rank = int(sum(v > full["cagr"] for v in fixed.values()) + 1)
        yrs = (res["nav"].index[-1] - res["nav"].index[0]).days / 365.25
        rows.append({"setting": "stock", "kind": kind, "L": L, "metric": m,
                     "primary": L == PRIMARY["L"] and m == PRIMARY["metric"],
                     "start": full["start"], "end": full["end"], "cagr": full["cagr"],
                     "bench_cagr": full["bench_cagr"], "excess_cagr": full["excess_cagr"], "max_dd": full["max_dd"],
                     "bench_max_dd": full["bench_max_dd"], "sharpe": full["sharpe"],
                     "t_monthly_excess": full["t_excess_monthly"], "months": full["months"],
                     "qqq_cagr": full["qqq_cagr"],
                     "h1_cagr": halves[0]["cagr"], "h1_bench": halves[0]["bench_cagr"],
                     "h1_t": halves[0]["t_excess_monthly"],
                     "h2_cagr": halves[1]["cagr"], "h2_bench": halves[1]["bench_cagr"],
                     "h2_t": halves[1]["t_excess_monthly"],
                     "switches_param": n_switch, "buys": res["counts"]["buy"],
                     "switch_sells": res["counts"]["switch_sell"], "switch_buys": res["counts"]["switch_buy"],
                     "switch_cost_usd": res["switch_cost"], "cost_usd_total": float(res["cost"].sum()),
                     "trades_per_year": (res["counts"]["buy"] + res["counts"]["sell"]) / yrs,
                     "cost_drag_per_year": full["cost_drag_per_year"],
                     "rank_among_fixed_points_cagr": rank, "n_points": len(fixed),
                     "fixed_best_cagr_hindsight": max(fixed.values()),
                     "fixed_median_cagr": float(np.median(list(fixed.values()))),
                     "owner_fixed_cagr": fixed[OWNER_REF[kind]], **j})
        pick_rows += pick_table("stock", kind, L, m, by)
        detail[f"stock_{kind}_L{L}_{m}"] = {"by_year": {int(y): {**v} for y, v in full["by_year"].items()},
                                            "judge": j, "counts": res["counts"]}
        print(f"stock {kind} L={L} {m}: CAGR {full['cagr']:+.3f} vs ONEQ {full['bench_cagr']:+.3f}")
    return rows, pick_rows, {"detail": detail, "picks": picks, "guard": data.guard["assertion"]}


# ======================================================================== main

def main(argv=None):
    p = argparse.ArgumentParser(description="walk-forward parameter re-selection (pre-registered, ledger section 9)")
    p.add_argument("--setting", choices=["index", "stock", "both"], default="both")
    p.add_argument("--no-index-verify", action="store_true")
    p.add_argument("--verify-stock-truncated", action="store_true")
    p.add_argument("--verify-stock-only", action="store_true",
                   help="re-pick every stock year from truncated windows and compare with picks_both.csv")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.verify_stock_only:
        pk = pd.read_csv(OUT / "picks_both.csv")
        pk = pk[pk["setting"] == "stock"]
        picks = {}
        for (kind, L, m), g in pk.groupby(["kind", "L", "metric"]):
            picks[(kind, int(L), m)] = {int(r.year): {"key": tuple(int(x) for x in r.params.split("/"))}
                                        for r in g.itertuples()}
        ok = repick_stock_truncated(picks, STOCK_WF_YEARS, STOCK_WINDOW, RAW, KINDS, ACCOUNT)
        (OUT / "stock_truncated_recheck.json").write_text(json.dumps(
            {"years_identical": ok, "configurations": len(picks)}, indent=2))
        return
    rows, prow, meta = [], [], {}
    if a.setting in ("index", "both"):
        r, pr, m = run_index(not a.no_index_verify)
        rows += r
        prow += pr
        meta["index"] = m
    if a.setting in ("stock", "both"):
        meta["engine_check"] = check_engine_matches_reference(RAW, KINDS)
        r, pr, m = run_stock()
        rows += r
        prow += pr
        if a.verify_stock_truncated:
            m["truncated_recheck_years"] = repick_stock_truncated(m["picks"], STOCK_WF_YEARS, STOCK_WINDOW, RAW, KINDS,
                                                                  ACCOUNT)
        m.pop("picks")
        meta["stock"] = m
    res = pd.DataFrame(rows)
    picks = pd.DataFrame(prow)
    tag = a.setting
    res.to_csv(OUT / f"results_{tag}.csv", index=False, float_format="%.6f")
    picks.to_csv(OUT / f"picks_{tag}.csv", index=False, float_format="%.6f")
    (OUT / f"detail_{tag}.json").write_text(json.dumps(meta, indent=1, default=str))
    (OUT / "README.json").write_text(json.dumps({
        "status": "pre-registered in docs/research_ledger_indicators.md section 9; weak test (years seen before, "
                  "grid post-hoc)",
        "primary": PRIMARY, "lookbacks": LOOKBACKS, "metrics": METRICS,
        "index": {"walk_forward": [f"{INDEX_WF_YEARS[0]}-01", INDEX_END], "halves": INDEX_HALVES},
        "stock": {"window": STOCK_WINDOW, "walk_forward": [f"{STOCK_WF_YEARS[0]}-01", "2026-08-31"],
                  "halves": STOCK_HALVES},
        "bonferroni_t_4_primary_one_sided_5pct": BONFERRONI_4}, indent=2))
    pd.set_option("display.width", 250)
    cols = ["setting", "kind", "L", "metric", "primary", "cagr", "bench_cagr", "qqq_cagr", "max_dd", "bench_max_dd",
            "t_monthly_excess", "h1_cagr", "h1_bench", "h2_cagr", "h2_bench", "switches_param",
            "rank_among_fixed_points_cagr", "A", "B", "pass"]
    print(res[cols].to_string(float_format=lambda v: f"{v:+.3f}"))
    prim = picks.merge(res.loc[res["primary"], ["setting", "kind", "L", "metric"]])
    print(prim.pivot_table(index="year", columns=["setting", "kind"], values="params", aggfunc="first").to_string())


if __name__ == "__main__":
    main()
