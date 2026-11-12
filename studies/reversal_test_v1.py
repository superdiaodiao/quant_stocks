"""One-shot test-period run (2017-2022) of the single frozen weekly reversal rule rev-ind-v1.

Pre-registered in docs/research_ledger_reversal_2012_2026.md ("rev-ind-v1 一次性检验"), committed with this file's
SHA-256 before any 2017-2022 number was computed. The rule is ``dv50_N100_K3_monday_ov30`` exactly as in the
development run (quant.strategies.reversal_dev, unchanged): no parameter, cost or data rule is changed here.

HARD RULE: only data dated on or before 2022-12-31 is read; the 2023-2026 holdout stays unread. Every frame goes
through ``load_dev_data`` with TEST_END, which truncates and asserts; entry and exit dates pass
``assert_dev_dates``. Holding weeks are those formed on or after 2017-01-01 whose exit is on or before 2022-12-31.

Primary data version: v2.1 (REVERSAL_DATA_VERSION=v2.1). A v1 run is reported beside it and decides nothing.
Running twice is refused: the primary result file is written once and never overwritten.

Outputs: output/research_only/reversal_test_2017_2022[_v2_1]/{summary.json, weekly.csv, terminal_events.csv}.

Usage:  REVERSAL_DATA_VERSION=v2.1 PYTHONPATH=. .venv/bin/python -m studies.reversal_test_v1
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace

import pandas as pd

from quant.data import version as dv
from quant.data.guards import assert_dev_dates
from quant.data.panel import TERMINAL_D5_STRESS, make_index
from quant.evaluation.events import excess_by_year as by_year, weekly_overlay_metrics as metrics
from quant.paths import ROOT
from quant.reporting import write_json
from quant.strategies.reversal_dev import Config, build_signals, load_dev_data, schedule, simulate

TEST_END = "2022-12-31"
WARMUP_START = "2016-06-01"     # development-period rows, only for the first formation weeks
EVAL_START = "2017-01-01"
HALVES = {"2017_2019": (2017, 2018, 2019), "2020_2022": (2020, 2021, 2022)}
RULE = Config(rank_var="dv50", n_universe=100, k=3, entry="monday", overlay=0.30)   # rev-ind-v1
DEV_TRIALS = 148
PASS_T = 2.0

OUT = dv.versioned(ROOT / "output/research_only/reversal_test_2017_2022")


def verdict(m: dict, halves: dict) -> dict:
    """The registered decision: net excess > 0, weekly t >= 2.0, and both halves net > 0."""
    checks = {"net_excess_positive": m["excess_net_ann"] > 0, "t_at_least_2": m["t_stat"] >= PASS_T,
              **{f"half_{k}_positive": v["excess_net_ann"] > 0 for k, v in halves.items()}}
    return {"checks": checks, "pass": all(checks.values())}


def run() -> dict:
    if (OUT / "summary.json").exists():
        raise SystemExit(f"{OUT / 'summary.json'} exists: the one-shot test has already run")
    data = load_dev_data(dv.INPUTS, dv.CACHE, TEST_END, WARMUP_START)
    print(data.guard["assertion"])
    signals = build_signals(data)
    assert_dev_dates(signals["week_end"], TEST_END)
    week_ends = sorted(data.universe["week_end"].unique())
    idx = make_index(data.tr, data.close)
    sched = schedule(data, week_ends, RULE.entry, RULE.hold_weeks, EVAL_START, TEST_END)
    first, last = sched[0][1], max(x for _, _, x in sched)
    print(f"date guard: entries {first.date()} .. exits {last.date()} <= {TEST_END}: PASS")

    w = simulate(RULE, data, signals, sched, idx, keep_detail=True)
    m = metrics(w)
    year = pd.to_datetime(w["entry"]).dt.year
    halves = {k: metrics(w[year.isin(ys)]) for k, ys in HALVES.items()}
    stress = load_dev_data(dv.INPUTS, dv.CACHE, TEST_END, WARMUP_START, terminal_awaiting=TERMINAL_D5_STRESS)
    best5 = w["excess_net"].nlargest(5).index
    summary = {
        "rule": "rev-ind-v1", "config": RULE.name, "config_fields": asdict(RULE),
        "data_version": dv.DATA_VERSION, "window": {"eval_start": EVAL_START, "end": TEST_END,
                                                    "first_entry": str(first.date()), "last_exit": str(last.date())},
        "date_guard": data.guard,
        "full": m, "halves": halves, "by_year_net": by_year(w),
        "sensitivity": {
            "spread_x2": metrics(simulate(replace(RULE, spread_mult=2.0), data, signals, sched, idx))["excess_net_ann"],
            "borrow_2pct": metrics(simulate(replace(RULE, borrow_rate=0.02), data, signals, sched, idx))["excess_net_ann"],
            "terminal_minus100": metrics(simulate(RULE, stress, signals, sched,
                                                  make_index(stress.tr, stress.close)))["excess_net_ann"],
            "without_best_5_weeks": metrics(w.drop(index=best5))["excess_net_ann"],
        },
        "development_trials": DEV_TRIALS,
        "verdict": verdict(m, halves),
    }
    OUT.mkdir(parents=True, exist_ok=True)
    w.to_csv(OUT / "weekly.csv", index=False)
    data.terminal_events.to_csv(OUT / "terminal_events.csv", index=False)
    write_json(OUT / "summary.json", summary)
    return summary


def main(argv=None):
    s = run()
    m = s["full"]
    print(json.dumps({"data": s["data_version"], "weeks": m["weeks"], "net_ann": round(m["excess_net_ann"], 4),
                      "t": round(m["t_stat"], 2), "halves": {k: round(v["excess_net_ann"], 4) for k, v in s["halves"].items()},
                      "pass": s["verdict"]["pass"]}))


if __name__ == "__main__":
    main()
