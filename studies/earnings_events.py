"""Large-cap earnings-event strategies on the frozen data version 1 (pre-registered in
docs/research_ledger_earnings_events.md, section 0, before any run).

Rules (top 250 by dv50 rank, at most 10 names, idle money in ONEQ, $10,000 IBKR Pro Tiered, whole shares, MOC fills):
- E1 announcement premium (Frazzini-Lamont 2007; Barber et al. 2013). The expected date is last year's actual
  announcement (8-K Item 2.02 D0) + 364 days. Buy at the close of expected day -5 (variant: -2), sell at the close of
  expected day +1, whatever the actual date turns out to be.
- E2 earnings-announcement-return drift (Brandt et al. 2008). EAR = stock total return D0-2 close .. D0+1 close minus
  ONEQ. Signal known at S = max(D0+1, the session of the 8-K acceptance); buy at S+1 if EAR >= the 90th percentile of
  the past 365 days of universe EARs; hold 60 sessions; 10 fixed slots (variant: monthly overlapping cohorts of 10).
- E3 = E2 slots, only names above their 200-day SMA at S.

Two halves: H1 2012-01-01..2018-12-31 (judged 2014-2017; 2012, 2013 and 2018 break the 2% rule), H2 2019-01-01..
2026-08-31. Fold 1: develop on H1 (pick primary vs variant by net IR), test on H2; fold 2 the other way round.
Pass (per fold, on the test half): A = CAGR > ONEQ and monthly excess t >= 2; B = max drawdown >= 10 pp shallower and
CAGR >= ONEQ - 3 pp. A rule passes only if both folds pass.

HARD DATE GUARD: each half is read through ``quant.data.panel.load_window`` (every price / return / universe /
terminal frame truncated to [warm-up start, half end] and asserted); earnings events are kept only when their 8-K
acceptance session is <= the half end; every signal uses events whose acceptance session is <= the signal session.

Events, candidates, engine, metrics and the fold selection: quant.strategies.earnings_events. The halves, the
configurations, the runs and the reports stay here.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study earnings_events   (or scripts/research_earnings_events.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

import pandas as pd

from quant.data import version as dv
from quant.data.panel import TERMINAL_D5_STRESS
from quant.paths import ROOT
from quant.reporting import public
from quant.strategies import canslim as cs
from quant.strategies.earnings_events import Config, Half, event_diagnostics, fold_verdicts

OUT = dv.versioned(ROOT / "output/research_only/earnings_events")
INPUTS = cs.INPUTS
ACCOUNT = 10_000.0
N_UNIVERSE = 250
BONFERRONI_T = 2.39   # two-sided 5% Bonferroni for 3 rules, reported only

HALVES = {
    "H1": {"perf_start": "2012-01-01", "perf_end": "2018-12-31", "price_start": "2011-06-01",
           "universe_start": "2012-01-01", "judged_from": "2014-01-01", "judged_to": "2017-12-31"},
    "H2": {"perf_start": "2019-01-01", "perf_end": "2026-08-31", "price_start": "2017-10-01",
           "universe_start": "2017-10-01", "judged_from": "2019-01-01", "judged_to": "2026-08-31"},
}
FOLDS = {"fold1": {"dev": "H1", "test": "H2"}, "fold2": {"dev": "H2", "test": "H1"}}

RULES = {"E1": ("E1_m5p1", "E1_m2p1"), "E2": ("E2_slot10", "E2_cohort"), "E3": ("E3_slot10_sma200",)}


CONFIGS = {
    "E1_m5p1": Config("E1_m5p1", "premium", lead=5, lag=1),
    "E1_m2p1": Config("E1_m2p1", "premium", lead=2, lag=1),
    "E2_slot10": Config("E2_slot10", "drift_slot"),
    "E2_cohort": Config("E2_cohort", "drift_cohort", k=30, weight=1 / 30),
    "E3_slot10_sma200": Config("E3_slot10_sma200", "drift_slot", sma200=True),
}


# ======================================================================== main

def _clean(m: dict) -> dict:
    return public(m, drop=("by_year",), convert=True)


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ("daily_nav", "trades"):
        (OUT / sub).mkdir(exist_ok=True)
    results, by_year, judged_all, diags, guards = [], [], {}, {}, {}
    for hname in ("H1", "H2"):
        h = Half(hname, HALVES[hname], INPUTS, BONFERRONI_T, account=ACCOUNT, n_universe=N_UNIVERSE)
        hs_ = Half(hname, HALVES[hname], INPUTS, BONFERRONI_T, terminal_awaiting=TERMINAL_D5_STRESS, account=ACCOUNT,
                   n_universe=N_UNIVERSE)
        guards[hname] = h.guard
        print(h.guard["assertion"], flush=True)
        print(f"  {hname}: events {len(h.ev)}, EAR rows {len(h.ear)}, with threshold {int(h.ear['thr'].notna().sum())}",
              flush=True)
        for name, cfg in CONFIGS.items():
            full, judged, extra = h.run(cfg)
            f2, j2, _ = h.run(replace(cfg, spread_mult=2.0))
            f3, j3, _ = hs_.run(cfg)
            judged_all[(hname, name)] = judged
            res = extra["res"]
            tick = h.memb.ticker
            tr = res["trades"]
            if len(tr):
                tr.assign(ticker=tr["security_id"].map(tick)).to_csv(OUT / "trades" / f"{hname}_{name}.csv", index=False)
            res["nav"].to_frame("nav").assign(oneq=h.oneq, qqq=h.qqq, exposure=res["exposure"],
                                              cost=res["cost"]).to_csv(OUT / "daily_nav" / f"{hname}_{name}.csv")
            for scope, m, m2, m3 in (("full", full, f2, f3), ("judged", judged, j2, j3)):
                results.append({"half": hname, "config": name, "scope": scope, **asdict(cfg), **_clean(m),
                                "excess_cagr_spread2x": m2["excess_cagr"], "pass_spread2x": m2["crit_pass"],
                                "excess_cagr_terminal_minus100": m3["excess_cagr"],
                                "pass_terminal_minus100": m3["crit_pass"]})
            for y, v in full["by_year"].items():
                ent = pd.to_datetime(tr["entry"]).dt.year if len(tr) else pd.Series(dtype=int)
                by_year.append({"half": hname, "config": name, "year": y, **v,
                                "judged_year": HALVES[hname]["judged_from"][:4] <= str(y) <= HALVES[hname]["judged_to"][:4],
                                "trades_entered": int((ent == y).sum())})
            # frictionless event-level diagnostics
            if cfg.kind == "premium":
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L, h.actual)
            elif cfg.kind == "drift_slot" and not cfg.sma200:
                allp = {}
                for r in h.ear.itertuples(index=False):
                    e = r.s_i + 1
                    if h.perf[0] <= e <= h.perf[-1]:
                        allp.setdefault(e, []).append((r.security_id, e + 60, 0, 0))
                diags[(hname, "E2_all_events")] = event_diagnostics(allp, h.sessions, h.Isig, h.cols, h.L)
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L)
            elif cfg.sma200:
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L)
            print(f"[{hname}] {name}: judged CAGR {judged['cagr']:+.1%} ONEQ {judged['bench_cagr']:+.1%} "
                  f"t_m {judged['t_excess_monthly']:.2f} IR {judged['ir']:.2f} DD {judged['max_dd']:.0%} "
                  f"(ONEQ {judged['bench_max_dd']:.0%}) trades {judged.get('closed_trades', 0)} "
                  f"pass {judged['crit_pass']} | full ex {full['excess_cagr']:+.1%}", flush=True)
        del h, hs_

    # fold selection and verdicts
    folds, verdict = fold_verdicts(judged_all, FOLDS, RULES)
    pd.DataFrame(results).to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    summary = {"date_guard": guards, "configs": {k: asdict(v) for k, v in CONFIGS.items()}, "folds": folds,
               "verdict": verdict,
               "event_diagnostics": {f"{a}:{b}": v for (a, b), v in diags.items()},
               "bonferroni_t_reported_only": BONFERRONI_T}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({"folds": folds, "verdict": verdict}, indent=1, default=str))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.parse_args(argv)
    run()


if __name__ == "__main__":
    main()
