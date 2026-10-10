"""Development-period research (2012-2016 only) for the weekly, industry-adjusted short-term reversal.

Rules studied (Dai, Medhat, Novy-Marx and Rizova, NBER w30917, industry-adjusted weekly reversal):

- Signal at each week end t (the Friday, or the last session of the week): the stock's total return over
  the sessions after the previous week end up to t, minus the compounded Ken French 49-industry value-weighted
  return of its FF49 industry over the same sessions.
- Eligible: top N by dv20 or dv50 rank (``weekly_universe_top300.csv.gz``, whose ranks already require a raw
  close >= $10), ``price_ge_10`` = Y, a close on t, an FF49 code, not a foreign filer, and no earnings event
  near the formation window: ``earnings_event_within_3_sessions`` = Y (D0 within 3 sessions of t) or
  ``earnings_nearest_d0_offset`` in [-7, +3] (the 5 formation sessions t-4..t widened by 3 sessions on
  each side), so an event early in the formation week is also excluded.
- Long the K most negative signals, short the K most positive, equal target weights, held one week.
  Entry (a) at the close of t (upper bound, not tradable by hand) or (b) at the close of the next session
  (Monday). Exit at the same point one week later; continuing names are not re-traded unless the position is
  more than 25% away from its target (no-trade band).
- Implementation on a $10,000 IBKR Pro Tiered account: QQQ for 100% of equity plus a long / short overlay of
  30% / 30% (and 50% / 50% for reference). Short proceeds pay for the long leg, so there is no margin loan.

HARD RULE: only data dated on or before 2016-12-31 is used for any return, price or outcome. Every frame is
read through ``load_dev_data``, which truncates at ``DEV_END`` and asserts that no later row survives; every
entry and exit date passes ``assert_dev_dates``. Holding weeks whose exit would fall after 2016-12-31 are
dropped (the next session after the last week end is not in the truncated calendar).

Outputs: output/research_only/reversal_dev_2012_2016/{summary.json, grid.csv, extra_tries.csv, weekly/*.csv}.
The IBKR cost model, the total-return index and the statistics are in quant (quant.backtest.costs,
quant.data.panel, quant.evaluation); the loader, the signal and the engine are quant.strategies.reversal_dev.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study reversal_dev   (or scripts/research_reversal_dev.py)
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from quant.backtest.costs import (
    CLEARING_FEE_PER_SHARE, EXCHANGE_FEE_PER_SHARE, FINRA_TAF_PER_SHARE_SOLD, HALF_SPREAD_BPS,
    HALF_SPREAD_UNRANKED_BPS, PASS_THROUGH_OF_COMMISSION, REBALANCE_BAND, SEC_FEE_PER_DOLLAR_SOLD,
)
from quant.data import version as dv
from quant.data.guards import assert_dev_dates
from quant.data.panel import TERMINAL_D5_STRESS, make_index
from quant.evaluation.criteria import bonferroni_t, weekly_deflated_sharpe as deflated_sharpe
from quant.evaluation.events import excess_by_year as by_year, weekly_overlay_metrics as metrics
from quant.paths import ROOT
from quant.reporting import write_json
from quant.strategies.reversal_dev import Config, build_signals, load_dev_data, schedule, simulate

DEV_END = "2016-12-31"
WARMUP_START = "2011-06-01"
EVAL_START = "2012-01-01"
JUDGED_YEARS = (2014, 2015, 2016)   # 2012 and 2013 exceed the 2% missing-slot rule (data report): reported only

# data version: REVERSAL_DATA_VERSION=v2 reads the v2 copies and writes <OUT>_v2 (docs/robustness_data_v2.md)
INPUTS = dv.INPUTS
CACHE = dv.CACHE
OUT = dv.versioned(ROOT / "output/research_only/reversal_dev_2012_2016")
# cost assumptions (IBKR Pro Tiered, 2012-2016): quant.backtest.costs (ibkr_order_cost, rank_half_spread)


# ======================================================================== main

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "weekly").mkdir(exist_ok=True)
    data = load_dev_data(INPUTS, CACHE, DEV_END, WARMUP_START)
    print(data.guard["assertion"])
    data_stress = None
    signals = build_signals(data)
    assert_dev_dates(signals["week_end"], DEV_END)
    week_ends = sorted(data.universe["week_end"].unique())
    idx = make_index(data.tr, data.close)
    sched = {(e, h): schedule(data, week_ends, e, h, EVAL_START, DEV_END) for e in ("friday", "monday")
             for h in (1, 2)}
    max_used = max(x for s in sched.values() for _, _, x in s)
    assert_dev_dates([max_used], DEV_END)
    print(f"date guard: latest exit used {max_used.date()} <= {DEV_END}: PASS")

    stress = load_dev_data(INPUTS, CACHE, DEV_END, WARMUP_START, terminal_awaiting=TERMINAL_D5_STRESS)
    idx_stress = make_index(stress.tr, stress.close)

    rows, ledger = [], []
    grid = [Config(rank_var=rv, n_universe=n, k=k, entry=en, overlay=ov)
            for rv in ("dv20", "dv50") for n in (100, 150, 200, 250) for k in (3, 5, 10, 20)
            for en in ("friday", "monday") for ov in (0.30, 0.50)]

    def one(cfg, tag):
        w = simulate(cfg, data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx, keep_detail=True)
        per = 52.0 / cfg.hold_weeks
        m = metrics(w, per)
        m_j = metrics(w[pd.to_datetime(w["entry"]).dt.year.isin(JUDGED_YEARS)], per)
        w2 = simulate(replace(cfg, spread_mult=2.0), data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx)
        w3 = simulate(replace(cfg, borrow_rate=0.02), data, signals, sched[(cfg.entry, cfg.hold_weeks)], idx)
        w4 = simulate(cfg, stress, signals, sched[(cfg.entry, cfg.hold_weeks)], idx_stress)
        row = {"config": cfg.name, "tag": tag, **{k: v for k, v in asdict(cfg).items()}, **m,
               "excess_net_ann_2014_2016": m_j.get("excess_net_ann"), "t_stat_2014_2016": m_j.get("t_stat"),
               "excess_ideal_ann_2014_2016": m_j.get("excess_ideal_ann"),
               "excess_net_ann_spread2x": metrics(w2, per)["excess_net_ann"],
               "excess_net_ann_borrow2pct": metrics(w3, per)["excess_net_ann"],
               "excess_net_ann_terminal_minus100": metrics(w4, per)["excess_net_ann"],
               "by_year_net": json.dumps(by_year(w))}
        w.to_csv(OUT / "weekly" / f"{cfg.name}.csv", index=False)
        return row

    for i, cfg in enumerate(grid, 1):
        rows.append(one(cfg, "grid"))
        print(f"[{i}/{len(grid)}] {cfg.name}: net {rows[-1]['excess_net_ann']:+.2%} t {rows[-1]['t_stat']:.2f} "
              f"ideal {rows[-1]['excess_ideal_ann']:+.2%}", flush=True)
    grid_df = pd.DataFrame(rows)
    grid_df.to_csv(OUT / "grid.csv", index=False)

    # extra feasibility tries, logged separately (all count toward the multiple-testing total)
    extras = []
    base_mon = grid_df[(grid_df["entry"] == "monday") & (grid_df["overlay"] == 0.30)]
    best = base_mon.sort_values("excess_ideal_ann", ascending=False).iloc[0]
    best_cfg = Config(rank_var=best["rank_var"], n_universe=int(best["n_universe"]), k=int(best["k"]),
                      entry="monday", overlay=0.30)
    extra_cfgs = []
    for acct in (25_000, 50_000, 100_000, 250_000):
        for k in (3, 5, 10, 20):
            extra_cfgs.append(replace(best_cfg, k=k, account=float(acct)))
    for k in (3, 5, 10, 20):
        extra_cfgs.append(replace(best_cfg, k=k, hold_weeks=2))
    for cfg in extra_cfgs:
        extras.append(one(cfg, "extra"))
        print(f"[extra] {cfg.name}: net {extras[-1]['excess_net_ann']:+.2%} t {extras[-1]['t_stat']:.2f}", flush=True)
    extra_df = pd.DataFrame(extras)
    extra_df.to_csv(OUT / "extra_tries.csv", index=False)

    all_df = pd.concat([grid_df, extra_df])
    trials = (all_df["sr_weekly"]).to_numpy()
    mon = grid_df[grid_df["entry"] == "monday"]
    top = mon.sort_values("ir", ascending=False).iloc[0]
    dsr = deflated_sharpe(top["sr_weekly"], int(top["weeks"]), top["skew"], top["kurt"], trials)
    n_tried = len(all_df)
    summary = {
        "date_guard": data.guard | {"latest_exit_used": str(max_used.date())},
        "configurations_tried": {"grid": len(grid_df), "extra": len(extra_df), "total": n_tried,
                                 "sensitivity_reruns_per_config": 3},
        "bonferroni_t_one_sided_5pct": float(bonferroni_t(n_tried)),
        "best_monday_by_ir": {k: (float(v) if isinstance(v, (np.floating, float, int, np.integer)) else v)
                              for k, v in top.items()},
        "deflated_sharpe_best_monday": dsr,
        "terminal_events_in_window": data.terminal_events["status"].value_counts().to_dict(),
        "cost_assumptions": {
            "commission": "IBKR Pro Tiered $0.0035/share, min $0.35/order, max 1% of trade value",
            "exchange_fee_per_share": EXCHANGE_FEE_PER_SHARE, "clearing_fee_per_share": CLEARING_FEE_PER_SHARE,
            "pass_through_fraction_of_commission": PASS_THROUGH_OF_COMMISSION,
            "sec_fee_per_dollar_sold": SEC_FEE_PER_DOLLAR_SOLD, "finra_taf_per_share_sold": FINRA_TAF_PER_SHARE_SOLD,
            "half_spread_bps_by_rank": HALF_SPREAD_BPS, "half_spread_unranked_bps": HALF_SPREAD_UNRANKED_BPS,
            "half_spread_floor": "half a 1-cent tick / price", "rebalance_band": REBALANCE_BAND,
            "borrow": "0.3%/yr general collateral (2%/yr sensitivity)",
            "shares": "long fractional, short whole shares (rounded to nearest; a name needing <1 share is skipped)",
        },
        "judged_years": JUDGED_YEARS,
    }
    write_json(OUT / "summary.json", summary)
    data.terminal_events.to_csv(OUT / "terminal_events_2012_2016.csv", index=False)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    args = parser.parse_args(argv)
    s = run(args)
    print(json.dumps(s["configurations_tried"]), "Bonferroni t:", round(s["bonferroni_t_one_sided_5pct"], 2))


if __name__ == "__main__":
    main()
