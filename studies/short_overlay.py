"""Short-loser overlay on QQQ (pre-registered in docs/research_ledger_short_overlay.md, section 0).

A margin account holds QQQ (100% of equity, or 100% + k for the market-neutral variant) and shorts the N worst names
of a monthly "loser" ranking in the data-v2 U300 universe, total short notional k of equity. Four loser signals
(GBRT walk-forward scores of research_ml_cross_section, the profitability composite, a Stambaugh-Yu-Yuan style
mispricing composite, 12-1 momentum). Daily simulation with whole-share shorts, IBKR Tiered commissions and fees,
half-spreads, tiered borrow fees, IBKR margin-loan and credit-interest rules, Reg T initial / maintenance margin with
forced liquidation, and terminal values for delisted shorts. Judged against ONEQ total return on two folds.
Signals, engine, fold metrics and diagnostics: quant.strategies.short_overlay.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_short_overlay.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
from dataclasses import replace  # noqa: E402

import pandas as pd  # noqa: E402

from quant.evaluation.criteria import bonferroni_t_student  # noqa: E402
from quant.evaluation.stats import t_sf  # noqa: E402
from quant.paths import CACHE_ROOT, ROOT  # noqa: E402
from quant.reporting import Preregistration, RunLog  # noqa: E402
from quant.strategies import short_overlay as so  # noqa: E402
# names read as ``scripts.research_short_overlay.X`` by tests/test_forward_observation.py
from quant.strategies.short_overlay import (  # noqa: E402,F401
    ALPHA_ONE_SIDED, Cfg, KS, MODES, NS, N_TRIALS, SIGNALS, basket_diagnostics, borrow_rates, build_market,
    fold_bounds, folds_for, load_effr, misp_score, month_returns, schedule, simulate, tstat,
)

# ======================================================================== frozen configuration

OUT = ROOT / "output/research_only/short_overlay"
LEDGER = ROOT / "docs/research_ledger_short_overlay.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"
ML_CACHE = CACHE_ROOT / "ml_cross_section_v2"                 # the ml_cross_section feature panel
EFFR_CSV = CACHE_ROOT / "leverage_methods/raw/EFFR_nyfed.csv"  # NY Fed EFFR (downloaded by leverage_methods)

SENSITIVITIES = {
    "borrow_x2": {"borrow_mult": 2.0},
    "d5_minus100": {"idx": "d5_minus100"},
    "delist_0pct": {"idx": "delist_0pct"},
    "spread_x2": {"spread_mult": 2.0},
    "account_100k": {"account": 100_000.0},
    "account_1m": {"account": 1_000_000.0},
    "ideal": {"ideal": True},
}


def load():
    return so.load(ML_CACHE, PRED, EFFR_CSV)


# ======================================================================== commands

def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    cov = so.coverage(scores)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov.to_string())
    for sig in SIGNALS:
        sched = schedule(mk, scores, sig, 20)
        print(sig, "signals", len(sched), "first trade", mk.sessions[sched[0][0]].date(),
              "last trade", mk.sessions[sched[-1][0]].date())
    # where do the worst names sit: average borrow tier of the worst 20 (no returns)
    for sig in SIGNALS:
        w = scores.dropna(subset=[sig]).sort_values(["s", sig]).groupby("s").head(20)
        print(sig, "worst-20 borrow tier mix", w["borrow"].value_counts(normalize=True).round(3).to_dict(),
              "median dv50 rank", float(w["dv50_rank"].median()))
    print("EFFR range", float(mk.effr.min()), float(mk.effr.max()), "terminal events in market", len(mk.booked))


def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data, sigs, panel, scores, mk, oneq, qqq = load()
    log = RunLog()
    say = log.say

    say("DATE GUARD:", data.guard["assertion"])
    rows, months_ex, nav_out = [], {}, []
    scheds = {(sig, n): schedule(mk, scores, sig, n) for sig in SIGNALS for n in NS}
    variants = [("main", {})] + list(SENSITIVITIES.items())
    for sig in SIGNALS:
        for n in NS:
            sched = scheds[(sig, n)]
            for k in KS:
                for mode in MODES:
                    for vname, mods in variants:
                        cfg = replace(Cfg(signal=sig, n=n, k=k, mode=mode), **mods)
                        for fold, (a, b) in folds_for(sig).items():
                            start, end, entries = fold_bounds(mk, sched, a, b)
                            res = simulate(mk, entries, cfg, start, end)
                            m, ex = so.fold_metrics(res, oneq, qqq, cfg.account)
                            rows.append({"config": cfg.name, "signal": sig, "n": n, "k": k, "mode": mode,
                                         "variant": vname, "fold": fold, **m})
                            if vname == "main":
                                months_ex[(cfg.name, fold)] = ex
                                nv = res["nav"][["nav", "short", "n_short"]].copy()
                                nv["config"], nv["fold"] = cfg.name, fold
                                nav_out.append(nv.reset_index())
                say(f"{sig} N{n}: done ({time.time() - t0:.0f}s)")
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "summary.csv", index=False)
    pd.concat(nav_out, ignore_index=True).to_csv(OUT / "nav_daily.csv.gz", index=False)

    # ---- judgement
    main = summ[summ["variant"] == "main"]
    verdict = []
    for cfgname, g in main.groupby("config", sort=False):
        ex = pd.concat([months_ex[(cfgname, f)] for f in ("H1", "H2")])
        t_all = tstat(ex)
        hurdle = bonferroni_t_student(len(ex) - 1, N_TRIALS, ALPHA_ONE_SIDED)
        h1, h2 = g[g["fold"] == "H1"].iloc[0], g[g["fold"] == "H2"].iloc[0]
        both = bool(h1["pass"] and h2["pass"])
        verdict.append({"config": cfgname, "H1_pass": bool(h1["pass"]), "H2_pass": bool(h2["pass"]),
                        "H1_A": bool(h1["A"]), "H1_B": bool(h1["B"]), "H2_A": bool(h2["A"]), "H2_B": bool(h2["B"]),
                        "t_pooled_vs_oneq": t_all, "p_one_sided": t_sf(t_all, len(ex) - 1),
                        "bonferroni_t": hurdle, "nominal_pass": both, "pass": bool(both and t_all >= hurdle)})
    verdict = pd.DataFrame(verdict)
    verdict.to_csv(OUT / "verdict.csv", index=False)

    # ---- diagnostics: the loser basket alone
    legs, squeeze, names, feas, tick = so.loser_leg_report(mk, scheds, scores, so.ACCOUNT)
    legs = pd.DataFrame(legs)
    legs.to_csv(OUT / "short_leg.csv", index=False)
    pd.DataFrame(squeeze).to_csv(OUT / "worst_squeeze_months.csv", index=False)
    nm = pd.DataFrame(names)
    if len(nm):
        nm["ticker"] = [tick.get((s_, i), i) for s_, i in zip(nm["s"], nm["security_id"])]
        nm = nm.drop(columns=["s"]).sort_values("ret", ascending=False)
        nm.drop_duplicates(["t0", "security_id"]).head(30).to_csv(OUT / "worst_single_name_squeezes.csv", index=False)
    pd.DataFrame(feas).to_csv(OUT / "feasibility.csv", index=False)

    # ---- benchmarks per fold
    pd.DataFrame(so.fold_benchmarks(oneq, qqq)).to_csv(OUT / "benchmarks.csv", index=False)
    res = {"prereg_sha256": prereg_hash(), "n_trials": N_TRIALS, "verdict": "PASS" if verdict["pass"].any() else "FAIL",
           "n_pass": int(verdict["pass"].sum()), "n_nominal_pass": int(verdict["nominal_pass"].sum()),
           "bonferroni_t_typical": float(verdict["bonferroni_t"].median()),
           "guard": data.guard["assertion"], "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    say(json.dumps(res, default=str))
    log.write(OUT / "run_log.txt")


def main(argv=None):
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true")
    g.add_argument("--register", action="store_true")
    g.add_argument("--run", action="store_true")
    args = ap.parse_args(argv)
    if args.check:
        check(args)
    elif args.register:
        register()
    else:
        run(args)


if __name__ == "__main__":
    main()
