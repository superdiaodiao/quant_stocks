"""A broad "T" grid (做T 大网格) around a held base with honest out-of-sample selection, pre-registered in
docs/research_ledger_t_grid.md (section 0). Not QuantConnect.

Grid per base (61,440 configurations): direction (S sell-high / B buy-dip / C both) x trigger measure (D10, D20, D50,
RSI2, RSI14, R1, R5, Z1) x trigger level (expanding own-history quantiles 80/90/95/98, mirrored 20/10/5/2) x exit
(SMA, TARGET 2 sigma, TRAIL 2 sigma, OPP = measure back to its median, TIME) x max hold (5/10/20/40) x fraction of the
base (1/5, 1/3, 1/2, 1) x cash reserve (0 = margin account, 10/25/50% cash account) x regime (ALL / UP = close >
SMA200). Bases: QQQ, U18 (18 large caps, Yahoo OHLC from the selective_t cache), T10 (monthly point-in-time top-10 by
market cap from the megacap v2 study, v2 panel closes, close-only execution).

The simulator is vectorised over configurations and accounts (numpy arrays of shape (accounts, configs)); one Python
loop over sessions. Account / execution / cost rules are those of quant.strategies.selective_t (tested equal on
the S1-like special case). The grid run accumulates per calendar year the sufficient statistics of each
configuration's daily basket returns (sum R, R^2, R*M, log(1+R), exposure, trips ...), from which every window
statistic used for selection is computed. Selected configurations are re-run exactly (fresh $10k accounts on the
test window, per-account matched exposure).

Usage: PYTHONPATH=. .venv/bin/python scripts/research_t_grid.py [--workers 12] [--smoke]
Outputs (returns / statistics only, no prices): output/research_only/t_grid/. Local derived caches (not in Git):
research_cache/t_grid/. The engine is quant.strategies.t_grid, the grid run / selection / surface tables are
quant.strategies.t_grid_analysis; the configuration, the verdicts and the outputs stay here.
"""
from __future__ import annotations

import argparse
import time

import numpy as np
import pandas as pd

from quant.evaluation.criteria import bonferroni_t
from quant.evaluation.metrics import relative_metrics
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import json_default, write_json
from quant.strategies.t_grid import END, base_rows, cfg_arrays, grid, load_all, oneq_for, simulate_grid
from quant.strategies.t_grid_analysis import (
    N_GRID, bh_count, half_dates, heat_tables, load_sims, marginals, multiple_testing, run_grid_all, stability,
    surface_frame, surface_summary, two_fold, walk_forward,
)

CACHE = CACHE_ROOT / "t_grid"
OUT = ROOT / "output/research_only/t_grid"
# inputs: the calendar study's QQQ / ONEQ charts, the selective_t study's 18 stock charts, the v2 panel, and the
# megacap v2 study's point-in-time top-20 list (T10)
SOURCES = {"QQQ": CACHE_ROOT / "calendar/raw/chart_QQQ.json", "ONEQ": CACHE_ROOT / "calendar/raw/chart_ONEQ.json"}
RAW = CACHE_ROOT / "selective_t/raw"
PANEL = CACHE_ROOT / "reversal_2012_2026_v2/prices"
TOP20 = ROOT / "output/research_only/megacap_v2/top20_by_month.csv"

BASES = ("QQQ", "U18", "T10")
COSTS = {"base": (True, 1.0), "zero": (False, 1.0), "x2": (True, 2.0)}


# ======================================================================== main analysis

def analyse(data: dict, sims: dict, bases, costs, rf: pd.Series) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    res = {"end": END, "n_grid_per_base": N_GRID, "n_trials_total": N_GRID * len(bases),
           "bonferroni_t_per_base": bonferroni_t(N_GRID), "bonferroni_t_total": bonferroni_t(N_GRID * len(bases)),
           "bonferroni_t_6_selector_runs": bonferroni_t(6), "bases": {}}
    pooled_p = []
    for base in bases:
        t0 = time.time()
        sim = sims[(base, "base")]
        hd = half_dates(data, base)
        df, extra = surface_frame(data, sims, base, costs)
        df.to_csv(OUT / f"surface_{base}.csv.gz", index=False, float_format="%.6g", compression="gzip")
        col = "full_excess_cagr_hmatch"
        ht = heat_tables(df, col)
        with open(OUT / f"heat_{base}.txt", "w") as fh:
            fh.write(f"# {base}: median / share>0 of full-window CAGR excess vs H_match (basket-level w), by pairs of axes\n")
            for k, v in ht.items():
                fh.write(f"\n## {k}\n{(v * (100 if 'median' in k else 1)).round(3).to_string()}\n")
        marg = marginals(df, col)
        marg.to_csv(OUT / f"marginals_{base}.csv", index=False, float_format="%.6g")
        b = {"windows": {k: list(v) for k, v in hd.items()}}
        summ = surface_summary(df, extra, costs, col)
        b["surface"] = summ
        b["stability"] = stability(df, col)
        b["multiple_testing"], p = multiple_testing(data, base, df, hd, rf)
        pooled_p.append(p)
        sel_info, folds = two_fold(data, base, df, hd, costs, COSTS, rf)
        b["selection"] = sel_info
        b["two_fold"] = {f: {c: v for c, v in d.items() if not c.startswith("_")} for f, d in folds.items()}
        wf = walk_forward(data, base, sim, costs, COSTS, rf)
        b["walk_forward"] = wf
        pd.DataFrame([{"year": y["year"], "selector": w, **y} for w in ("A", "P") for y in wf[w]["base"]["years"]]).to_csv(
            OUT / f"walk_forward_{base}.csv", index=False, float_format="%.6f")
        # ---------------- verdicts
        verdict = {}
        for which in ("A", "P"):
            f1, f2 = folds["fold1"]["base"][which], folds["fold2"]["base"][which]
            x1, x2 = folds["fold1"]["x2"][which], folds["fold2"]["x2"][which]
            wm = wf[which]["base"]["m"]
            a_ = f1["excess_vs_hmatch"] > 0 and f2["excess_vs_hmatch"] > 0
            b_ = wm["excess_vs_hmatch"] > 0 and wm["t_monthly_excess_vs_hmatch"] >= 2.0
            c_ = x1["excess_vs_hmatch"] > 0 and x2["excess_vs_hmatch"] > 0
            v = {"a_two_fold": bool(a_), "b_walk_forward": bool(b_), "c_double_spread": bool(c_),
                 "survives": bool(a_ and b_ and c_)}
            if base == "QQQ":
                h1r = folds["fold2"]["_series"][which][0].dropna()
                h2r = folds["fold1"]["_series"][which][0].dropna()
                cat = pd.concat([h1r, h2r])
                oq = oneq_for(data, cat.index)
                rm_all = relative_metrics(cat, oq, rf, "oneq")
                m1 = relative_metrics(h1r, oq[oq.index <= h1r.index[-1]], rf, "oneq")
                m2 = relative_metrics(h2r, oq[oq.index >= h2r.index[0]], rf, "oneq")
                A = m1["excess_vs_oneq"] > 0 and m2["excess_vs_oneq"] > 0 and rm_all["t_monthly_excess_vs_oneq"] >= 2
                B = all(m["dd_shallower_than_oneq_pp"] >= 10 and m["excess_vs_oneq"] >= -0.03 for m in (m1, m2))
                v["oneq_cross_fitted"] = {
                    "half1": {k: m1[k] for k in ("excess_vs_oneq", "dd_shallower_than_oneq_pp", "oneq_window_start")},
                    "half2": {k: m2[k] for k in ("excess_vs_oneq", "dd_shallower_than_oneq_pp")},
                    "t_monthly_concat": rm_all["t_monthly_excess_vs_oneq"], "A": bool(A), "B": bool(B)}
                v["worth_forward_watch"] = bool(v["survives"] and (A or B))
            verdict[which] = v
        b["verdict"] = verdict
        res["bases"][base] = b
        print(f"{base}: analysed in {time.time() - t0:.0f}s; surface full share>H_match "
              f"{summ['full']['share_beating_hmatch']:.1%}, median {summ['full']['median_excess_cagr_hmatch']:+.2%}; "
              f"verdict {verdict}", flush=True)
        write_json(OUT / "results.json", res, default=json_default)
    if pooled_p:
        allp = np.concatenate(pooled_p)
        res["pooled_bh_rejections_q05"] = bh_count(allp)
    write_json(OUT / "results.json", res, default=json_default)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--chunk", type=int, default=5120)
    ap.add_argument("--bases", default=",".join(BASES))
    ap.add_argument("--costs", default=",".join(COSTS))
    ap.add_argument("--smoke", action="store_true", help="tiny data / grid subset for timing (results not used)")
    ap.add_argument("--analyse-only", action="store_true", help="reuse the cached accumulators")
    a = ap.parse_args(argv)
    bases = a.bases.split(",")
    costs = a.costs.split(",")
    t0 = time.time()
    data = load_all(SOURCES, RAW, PANEL, TOP20, smoke=a.smoke)
    OUT.mkdir(parents=True, exist_ok=True)
    if not a.smoke:
        pd.DataFrame(data["pc_rows"]).to_csv(OUT / "data_checks_u18.csv", index=False, float_format="%.6g")
        pd.DataFrame(data["t10_checks"]).to_csv(OUT / "data_checks_t10.csv", index=False)
        sp = pd.DataFrame(data["t10_spells"], columns=["slot", "security_id", "first_i", "last_i"])
        sp["first"] = data["t10_master"][sp["first_i"]].strftime("%Y-%m-%d")
        sp["last"] = data["t10_master"][sp["last_i"]].strftime("%Y-%m-%d")
        sp.drop(columns=["first_i", "last_i"]).to_csv(OUT / "t10_spells.csv", index=False)
    print(f"data loaded in {time.time() - t0:.0f}s", flush=True)
    if a.smoke:
        n = 2048
        rows = base_rows(data, "U18")
        codes = cfg_arrays(grid().sample(n, random_state=0))
        t1 = time.time()
        simulate_grid(rows, codes)
        print(f"smoke: U18 {rows.c.shape} x {n} configs: {time.time() - t1:.1f}s")
        return
    if a.analyse_only:
        sims = load_sims(bases, costs, data, CACHE)
    else:
        sims = run_grid_all(data, bases, costs, a.workers, a.chunk, N_GRID, CACHE, COSTS)
    print(f"grid done at {time.time() - t0:.0f}s", flush=True)
    analyse(data, sims, bases, costs, data["rf"])
    print(f"all done in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
