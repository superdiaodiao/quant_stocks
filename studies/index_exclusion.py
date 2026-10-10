"""Cap-weighted Nasdaq replica minus predicted losers: a long-only index enhancement
(pre-registered in docs/research_ledger_index_exclusion.md, section 0).

Two base portfolios built monthly from the data-v2 U300 panel and its point-in-time market caps: B100 (the 100
largest non-financial names, cap-weighted, QQQ-like) and B300 (every U300 name with a market cap, cap-weighted).
Three loser signals reused from earlier studies (S-MISP mispricing composite of research_short_overlay, S-ML GBRT
walk-forward predictions of research_ml_cross_section, S-PROF profitability composite of research_fundamentals).
Variants: drop the worst 5 / 10 / 20 base members by signal and renormalise, or tilt weights by (1 - 0.5 x badness
percentile). $10k IBKR Tiered fractional-share simulation with the engine of research_megacap (next-close
execution, 25% rebalance band). Judged against ONEQ and, as the key diagnostic, against the matching base replica.
Portfolios, replica, statistics and verdict: quant.strategies.index_exclusion.

Usage:
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --check
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --register
  REVERSAL_DATA_VERSION=v2 PYTHONPATH=. .venv/bin/python scripts/research_index_exclusion.py --run
"""
from __future__ import annotations

import os

os.environ.setdefault("REVERSAL_DATA_VERSION", "v2")      # the study is defined on frozen data v2 only

import argparse  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

import pandas as pd  # noqa: E402

from quant.backtest.costs import REBALANCE_BAND  # noqa: E402
from quant.paths import CACHE_ROOT, ROOT  # noqa: E402
from quant.reporting import Preregistration, RunLog  # noqa: E402
from quant.strategies import index_exclusion as ix  # noqa: E402
from quant.strategies import short_overlay as so  # noqa: E402

# ======================================================================== frozen configuration

OUT = ROOT / "output/research_only/index_exclusion"
LEDGER = ROOT / "docs/research_ledger_index_exclusion.md"
FROZEN = OUT / "frozen_prereg.json"
PREREG = Preregistration(LEDGER, FROZEN, OUT)
prereg_block, prereg_hash, register = PREREG.block, PREREG.hash, PREREG.register
PRED = ROOT / "output/research_only/ml_cross_section/predictions.csv.gz"   # research_ml_cross_section output
ML_CACHE = CACHE_ROOT / "ml_cross_section_v2"                              # its feature panel

SIGNALS = ("S-MISP", "S-ML", "S-PROF")
BASES = ("B100", "B300")
VARIANTS = ("X5", "X10", "X20", "T50")
N_TRIALS = len(SIGNALS) * len(BASES) * len(VARIANTS)       # 24
ALPHA_ONE_SIDED = 0.05
TOPN = 100
LAMBDA = 0.5
ACCOUNT = 10_000.0
BAND = REBALANCE_BAND                                   # 0.25
EXECUTIONS = {  # name -> (account, band); 'ideal' is the cost-free target-weight replica
    "main": (ACCOUNT, BAND),
    "band0": (ACCOUNT, 0.0),
    "acct100k": (100_000.0, BAND),
    "ideal": (None, None),
}
FOLDS = {  # signal -> {fold: (window start, window end)}; first signal used
    "default": {"H1": ("2012-02-01", "2018-12-31"), "H2": ("2019-01-01", "2026-08-31")},
    "S-ML": {"H1": ("2016-01-04", "2020-12-31"), "H2": ("2021-01-01", "2026-08-31")},
}
FIRST_SIGNAL = {"default": "2012-01-31", "S-ML": "2015-12-31"}


def key_of(signal: str) -> str:
    return "S-ML" if signal == "S-ML" else "default"


def load():
    data, sigs, panel, oneq_lvl, oneq_px, end, qtr, qqq_lvl, scores = so.scored_panel(ML_CACHE, PRED)
    if pd.Series(data.sessions).max() > pd.Timestamp(end):
        raise RuntimeError("sessions beyond end")
    return data, scores, oneq_lvl, oneq_px, qqq_lvl


def excluded_x10(scores: pd.DataFrame) -> pd.DataFrame:
    return pd.concat([ix.excluded_names(scores, b, sig, 10, TOPN) for b in BASES for sig in SIGNALS],
                     ignore_index=True)


# ======================================================================== commands

def check(args):
    OUT.mkdir(parents=True, exist_ok=True)
    data, scores, oneq, oneq_px, qqq = load()
    cov = ix.coverage(scores, BASES, SIGNALS, TOPN)
    cov.to_csv(OUT / "check_coverage.csv", index=False)
    pd.set_option("display.width", 250)
    print(cov.to_string())
    ex = excluded_x10(scores)
    ex.to_csv(OUT / "check_excluded_X10.csv", index=False)
    print(ex.groupby(["base", "signal"])["excluded_capw"].describe().to_string())
    for s in ("2014-12-31", "2020-12-31", "2026-07-31"):
        print(ex[ex["s"] == s][["base", "signal", "names", "excluded_capw"]].to_string())
    for b in BASES:
        m = ix.base_members(scores, b, TOPN)
        last = m[m["s"] == m["s"].max()]
        print(b, "last month top-10 weight", round(float(last["mcap"].head(10).sum() / last["mcap"].sum()), 3),
              "n", len(last))


def run(args):
    if not FROZEN.exists():
        raise SystemExit("not registered")
    if json.loads(FROZEN.read_text())["sha256"] != prereg_hash():
        raise SystemExit("pre-registration text changed since --register")
    t0 = time.time()
    OUT.mkdir(parents=True, exist_ok=True)
    data, scores, oneq_lvl, oneq_px, qqq_lvl = load()
    log = RunLog()
    say = log.say

    say("DATE GUARD:", data.guard["assertion"])
    members = {b: ix.base_members(scores, b, TOPN) for b in BASES}
    # ---- targets: bases from both first-signal dates, variants per signal
    tgts = {}
    for key, first in FIRST_SIGNAL.items():
        for b in BASES:
            tgts[(b, "BASE", key)] = ix.make_targets(members[b], None, "BASE", LAMBDA, first)
    for sig in SIGNALS:
        key = key_of(sig)
        for b in BASES:
            for v in VARIANTS:
                tgts[(b, f"{sig}_{v}", key)] = ix.make_targets(members[b], sig, v, LAMBDA, FIRST_SIGNAL[key])
    # ---- simulations
    sims = {}
    for ex in EXECUTIONS:
        for k_, tg in tgts.items():
            sims[(ex,) + k_] = ix.run_sim(tg, data, qqq_lvl, *EXECUTIONS[ex], ACCOUNT)
        say(f"execution {ex}: {len(tgts)} portfolios ({time.time() - t0:.0f}s)")

    rows, ver_rows, years_rows, val_rows, nav_out = ix.evaluate(sims, data, oneq_lvl, oneq_px, qqq_lvl, FOLDS,
                                                                N_TRIALS, ALPHA_ONE_SIDED)
    summ = pd.DataFrame(rows)
    summ.to_csv(OUT / "summary.csv", index=False)
    ver = pd.DataFrame(ver_rows)
    ver.to_csv(OUT / "verdict.csv", index=False)
    pd.DataFrame(years_rows).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(val_rows).to_csv(OUT / "replica_validation.csv", index=False)
    pd.concat(nav_out, ignore_index=True).to_csv(OUT / "nav_daily.csv.gz", index=False)
    excluded_x10(scores).to_csv(OUT / "excluded_X10.csv", index=False)
    main = ver[(ver["execution"] == "main") & ver["trial"]]
    res = {"prereg_sha256": prereg_hash(), "n_trials": N_TRIALS,
           "verdict": "PASS" if main["pass"].any() else "FAIL", "n_pass": int(main["pass"].sum()),
           "n_cond1": int(main["cond1_vs_oneq"].sum()), "n_cond2": int(main["cond2_exclusion_alpha"].sum()),
           "n_nominal_cond2": int(main["nominal_cond2_t2"].sum()),
           "max_t_vs_base": float(main["t_pooled_vs_base"].max()),
           "bonferroni_t_typical": float(main["bonferroni_t"].median()),
           "guard": data.guard["assertion"], "runtime_s": round(time.time() - t0, 1)}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    pd.set_option("display.width", 250)
    say(main[["base", "portfolio", "H1_pass", "H2_pass", "t_pooled_vs_oneq", "H1_ex_base_ann", "H2_ex_base_ann",
              "t_pooled_vs_base", "pass"]].to_string())
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
