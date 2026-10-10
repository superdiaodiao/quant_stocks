"""Calendar and intraday anomalies on QQQ (pre-registered in docs/research_ledger_calendar.md, section 0).

Eight textbook rules, no parameters:

- C1 turn of the month: hold QQQ on the last session of each month and the first three of the next (days -1..+3).
- C2 "sell in May" (Halloween): hold QQQ November .. April, T-bills May .. October.
- C3 pre-holiday: hold QQQ only on the session right before a scheduled NYSE holiday.
- C4 overnight only: buy at the close, sell at the next open (earns close(t-1) -> open(t), dividends included).
- C5 intraday only: buy at the open, sell at the close (earns open(t) -> close(t)).
- C6 weekend / Monday effect: skip the first session of every calendar week (sell at the week's last close,
  buy back at the next week's first close); hold QQQ otherwise.
- C7 FOMC: hold QQQ on scheduled FOMC announcement days and the session before (days -1, 0).
- C8 C1 or C2 (union).

The idle state is a T-bill ETF (RF - 0.10%/yr, two orders per switch) for C1, C2, C3, C7, C8 and uninvested cash at
0% (one order per switch) for C4, C5, C6. Trades happen in the closing auction (and the opening auction for C4/C5)
at that day's close / open. Costs: IBKR Pro Tiered per order (scripts/research_qqq_timing.py constants), half-spread
1 bp for QQQ and the T-bill ETF, 2 bp for ONEQ. Every period starts fresh from $10,000.

Daily QQQ return split: total r = adjclose_t / adjclose_{t-1} - 1; intraday i = close_t / open_t - 1;
overnight o = (1 + r) / (1 + i) - 1 (so the ex-dividend amount sits in the overnight leg).

Every vendor frame is truncated at ``END`` immediately after parsing and asserted. Raw data local only:
research_cache/calendar/raw/ (Yahoo chart JSON for QQQ / ONEQ, Federal Reserve FOMC calendar pages).
Outputs: output/research_only/calendar/ (returns and metrics only, no vendor price levels).

The data, the calendars, the simulator and the per-period evaluation are quant.strategies.calendar; the FOMC dates
and the NYSE holiday calendar are quant.data.event_calendar. This file holds the frozen parameters and the run.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study calendar   (or scripts/research_calendar.py)
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from quant.data.event_calendar import scheduled_holidays
from quant.evaluation.criteria import bonferroni_t
from quant.evaluation.metrics import core_metrics, monthly, yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json, write_period_tables
from quant.strategies.calendar import (
    RuleSpec, Spreads, buy_hold, criteria, data_checks, effect_size, evaluate_period, load_data, period_bounds,
    segments,
)

END = "2026-09-30"
RAW = CACHE_ROOT / "calendar/raw"
OUT = output_dir("calendar")

SPREADS = Spreads(qqq=1e-4, bill=1e-4, oneq=2e-4)    # half-spreads: QQQ, the T-bill ETF, ONEQ

PERIODS = {
    "dev": ("1999-03-11", "2014-12-31"),
    "test": ("2015-01-02", "2026-09-30"),
    "full": ("1999-03-11", "2026-09-30"),
    # "half1" / "half2" are the full period split by session count (filled in at run time)
}
JUDGED = "test"

RULES = (
    RuleSpec("C1", "B", "turn of the month: hold days -1..+3, T-bills otherwise"),
    RuleSpec("C2", "B", "sell in May: hold Nov-Apr, T-bills May-Oct"),
    RuleSpec("C3", "B", "pre-holiday: hold the session before scheduled NYSE holidays"),
    RuleSpec("C4", "C", "overnight only: buy at the close, sell at the next open"),
    RuleSpec("C5", "C", "intraday only: buy at the open, sell at the close"),
    RuleSpec("C6", "C", "weekend effect: skip the first session of each week"),
    RuleSpec("C7", "B", "FOMC: hold the announcement day and the session before"),
    RuleSpec("C8", "B", "C1 or C2"),
)


# ======================================================================== main

def run(args=None) -> dict:
    d = load_data(END, RAW)
    print("DATE GUARD: every frame truncated at", END, "and asserted;", d.guard)
    checks = data_checks(d, RAW)
    print("DATA CHECKS:", {k: v for k, v in checks.items() if k != "holidays_per_year"})
    bounds = period_bounds(d, PERIODS)
    hol = scheduled_holidays(d.sessions)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(RULES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(RULES)), "judged_period": JUDGED,
           "periods": {k: [str(d.sessions[s].date()), str(d.sessions[e].date())] for k, (s, e) in bounds.items()},
           "rules": {}, "benchmarks": {}}
    rows, by_year, mon = [], {}, {}
    for spec in RULES:
        seg = segments(spec.name, d.sessions, hol)
        info = {"description": spec.description, "idle": spec.idle, "periods": {}}
        for pk, (s, e) in bounds.items():
            net = evaluate_period(spec.name, seg, d, s, e, spec.idle, True, spreads=SPREADS, end=END)
            gross = evaluate_period(spec.name, seg, d, s, e, spec.idle, False, spreads=SPREADS, end=END)
            cash0 = evaluate_period(spec.name, seg, d, s, e, "C", True, spreads=SPREADS, end=END)["metrics"]
            pr = {"net": net["metrics"], "gross": gross["metrics"], "criteria": criteria(net["metrics"]),
                  "gross_criteria": criteria(gross["metrics"]),
                  "sens_cash0": {k: cash0[k] for k in ("cagr", "max_dd", "excess_vs_oneq", "cost_drag_per_year")},
                  "effect": effect_size(spec.name, seg, d, s, e)}
            info["periods"][pk] = pr
            for tag in ("net", "gross"):
                rows.append({"rule": spec.name, "period": pk, "basis": tag, **pr[tag],
                             **{f"crit_{k}": v for k, v in pr["criteria" if tag == "net" else "gross_criteria"].items()}})
            if pk == "full":
                by_year[spec.name] = yearly(net["ret"])
                by_year[f"{spec.name}_gross"] = yearly(gross["ret"])
                mon[spec.name] = monthly(net["ret"])
        res["rules"][spec.name] = info
        t = info["periods"][JUDGED]
        print(f"{spec.name} test: CAGR {t['net']['cagr']:+.2%} (gross {t['gross']['cagr']:+.2%}; ONEQ "
              f"{t['net']['oneq_cagr']:+.2%}, QQQ {t['net']['qqq_cagr']:+.2%}) MDD {t['net']['max_dd']:.1%} "
              f"(ONEQ {t['net']['oneq_max_dd']:.1%}) t {t['net']['t_monthly_excess_vs_oneq']:+.2f} "
              f"cost {t['net']['cost_drag_per_year']:.2%}/yr pass={t['criteria']['pass']}")
    # benchmark rows
    for pk, (s, e) in bounds.items():
        q = buy_hold(d.r, d.close, SPREADS.qqq, s, e)
        first_oneq = int(np.argmax(d.oneq_r.notna().values))
        so = max(s, first_oneq)
        b = buy_hold(d.oneq_r, d.oneq_close, SPREADS.oneq, so, e)
        res["benchmarks"][pk] = {"QQQ": core_metrics(q, d.rf), "ONEQ": {"start": str(b.index[0].date()),
                                                                      **core_metrics(b, d.rf)}}
        if pk == "full":
            by_year["QQQ_buyhold"] = yearly(q)
            by_year["ONEQ_buyhold"] = yearly(b)
            mon["QQQ_buyhold"] = monthly(q)
            mon["ONEQ_buyhold"] = monthly(b)
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    write_period_tables(OUT, by_year, mon)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
