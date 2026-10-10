"""Concentrated mega-cap strategies on the frozen data version 1 panel (pre-registered in docs/research_ledger_megacap.md).

Rules (6 configurations, monthly; signal at the last session of a month, trade at the next session's close):
  M1 top 10 Nasdaq names by point-in-time market cap, equal weight
  M2 top 5, equal weight
  M3 among the top 20 by market cap, the 5 with the best 6-month (126-session) total return, equal weight
  M4 among the top 20, the 10 with the best 12-1 momentum (t-252 .. t-21), equal weight
  M5 M3, but 100% QQQ when QQQ closes below its 200-session SMA on the signal day
  M6 top 10 by market cap, cap-weighted (sanity: should track QQQ)

Market cap at signal session s: quant.data.market_cap (SEC shares -> public float -> Nasdaq company list -> dollar
volume proxy; data rule 0.2a). Rules, ranking and engine binding: quant.strategies.megacap.

Judged vs ONEQ total return in 2014-01..2019-12 and 2020-01..2026-08 (2012-2013 reported only). Costs: IBKR Tiered
($10k account, quant.backtest.costs). Delisted names keep their terminal return (quant.data.panel).

Usage:  PYTHONPATH=. .venv/bin/python -m quant study megacap [--check-ranks]   (or scripts/research_megacap.py)
"""
from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

from quant.data import version as dv
from quant.evaluation.criteria import HALVES_AB_LABELS, ab_verdict_on, bonferroni_t
from quant.evaluation.metrics import yearly
from quant.evaluation.periods import nav_window_metrics as window_metrics
from quant.paths import ROOT
from quant.signals.technical import momentum_frames
from quant.strategies.megacap import ONEQ_HS, QQQ_HS, RULES, build_targets, buy_hold, prepare, rank_check, simulate

# ======================================================================== frozen configuration

OUT = dv.versioned(ROOT / "output/research_only/megacap")
WINDOW = "megacap"
WINDOW_SPEC = {"perf_start": "2012-01-01", "perf_end": "2026-08-31", "price_start": "2011-06-01",
               "universe_start": "2012-01-01", "judged_from": "2014-01-01"}
PERIODS = {"2012-2013 (report only)": ("2012-01-01", "2013-12-31"),
           "H1 2014-2019": ("2014-01-01", "2019-12-31"),
           "H2 2020-2026-08": ("2020-01-01", "2026-08-31"),
           "Full 2014-2026-08": ("2014-01-01", "2026-08-31")}
JUDGED = ("H1 2014-2019", "H2 2020-2026-08")
FULL = "Full 2014-2026-08"
N_TRIALS = 6
assert len(RULES) == N_TRIALS


def criteria(per: dict) -> dict:
    return ab_verdict_on([per[p] for p in JUDGED], per[FULL], HALVES_AB_LABELS)


# ======================================================================== run

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    data, ranked, oneq_lvl, oneq_px, end = prepare(WINDOW, WINDOW_SPEC)
    top = rank_check(ranked)
    top.to_csv(OUT / "top20_by_month.csv", index=False)
    src = top.groupby(top["s"].str[:4])["src"].value_counts(normalize=True).unstack(fill_value=0).round(3)
    src.to_csv(OUT / "mcap_source_share_top20.csv")
    print("market-cap source share among each month's top 20:\n", src.to_string())
    dec = top[top["s"].str[5:7] == "12"]
    print("December top 10:")
    for s, g in dec.groupby("s"):
        print(" ", s, ", ".join(f"{t}({m:.0f}{'' if x == 'sec_shares' else '*'})" for t, m, x in
                                zip(g.ticker[:10], g.mcap_bn[:10], g.src[:10])))
    if args.check_ranks:
        return {}
    mom = momentum_frames(data.sig_idx)
    ranked_x = ranked.copy()          # sensitivity (0.2a): names with no market-cap source are not ranked
    ranked_x.loc[ranked_x["mcap_src"] == "dollar_volume", "mcap"] = np.nan
    results, summary, by_year, holds, navs = {}, [], [], [], {}
    for rule in RULES:
        tg = build_targets(rule, ranked, mom, data.qqq_close)
        tgx = build_targets(rule, ranked_x, mom, data.qqq_close)
        simx = simulate(tgx, data.sessions, data.perf_idx, data.close,
                        data.last_row, data.qqq_perf_idx, data.qqq_close)
        sim = simulate(tg, data.sessions, data.perf_idx, data.close,
                       data.last_row, data.qqq_perf_idx, data.qqq_close)
        sim0 = simulate(tg, data.sessions, data.perf_idx, data.close,
                        data.last_row, data.qqq_perf_idx, data.qqq_close, band=0.0)
        dates = sim["nav"].index
        oneq = buy_hold(oneq_lvl, oneq_px, dates, ONEQ_HS)
        qqq = buy_hold(data.qqq_perf_idx, data.qqq_close, dates, QQQ_HS)
        per = {}
        for pname, (a, b) in PERIODS.items():
            m = window_metrics(sim["nav"], oneq, qqq, a, b, sim["cost"], sim["traded"], sim["orders"])
            m0 = window_metrics(sim0["nav"], oneq, qqq, a, b)
            mx = window_metrics(simx["nav"], oneq.reindex(simx["nav"].index), qqq.reindex(simx["nav"].index), a, b)
            if m is None:
                continue
            m["cagr_band0_sensitivity"] = m0["cagr"] if m0 else np.nan
            m["cagr_no_dv_fallback_sensitivity"] = mx["cagr"] if mx else np.nan
            m["t_no_dv_fallback_sensitivity"] = mx["t_monthly_excess_vs_oneq"] if mx else np.nan
            m["max_dd_no_dv_fallback_sensitivity"] = mx["max_dd"] if mx else np.nan
            per[pname] = m
            summary.append({"rule": rule.name, "period": pname, **m})
        crit = criteria(per)
        crit_x = criteria({p: {"cagr": v["cagr_no_dv_fallback_sensitivity"], "oneq_cagr": v["oneq_cagr"],
                               "t_monthly_excess_vs_oneq": v["t_no_dv_fallback_sensitivity"],
                               "dd_shallower_than_oneq_pp": (abs(v["oneq_max_dd"])
                                                             - abs(v["max_dd_no_dv_fallback_sensitivity"])) * 100}
                           for p, v in per.items()})
        n_dv = sum(1 for lst in tg.values() for x in lst if str(x[4]).startswith("dollar_volume"))
        results[rule.name] = {"criteria_no_dv_fallback_sensitivity": crit_x, "picks_using_dollar_volume_mcap": n_dv,
                              "rule": rule.__dict__, "start": str(sim["start"].date()), "periods": per, "criteria": crit,
                              "avg_names_held": float(sim["names"].mean()),
                              "share_months_in_qqq": float(np.mean([t[0][0] == "QQQ" for t in tg.values()]))}
        r = sim["nav"].pct_change().dropna()
        for y, v in yearly(r).items():
            by_year.append({"rule": rule.name, "year": y, "strategy": v,
                            "oneq": yearly(oneq.pct_change().dropna()).get(y),
                            "qqq": yearly(qqq.pct_change().dropna()).get(y)})
        for s, lst in tg.items():
            for k, (sid, w, rk, mc, sr) in enumerate(lst, 1):
                holds.append({"rule": rule.name, "signal": s.date().isoformat(), "slot": k, "security_id": sid,
                              "weight": round(w, 4), "mcap_bn": round(mc / 1e9, 1) if np.isfinite(mc) else None,
                              "mcap_src": sr})
        navs[rule.name] = sim["nav"]
        navs[f"ONEQ_from_{rule.name}"] = oneq
        navs[f"QQQ_from_{rule.name}"] = qqq
        f = per[FULL]
        print(f"{rule.name}: start {sim['start'].date()} | " + " | ".join(
            f"{p.split()[0]} CAGR {per[p]['cagr']:+.1%} ONEQ {per[p]['oneq_cagr']:+.1%} DD {per[p]['max_dd']:.0%}"
            f" (ONEQ {per[p]['oneq_max_dd']:.0%})" for p in JUDGED) +
            f" | full t {f['t_monthly_excess_vs_oneq']:.2f} | pass {crit['pass']}")
    # benchmark rows (ONEQ and QQQ themselves, from M1's start)
    for name in ("ONEQ", "QQQ"):
        s_ = navs[f"{name}_from_M1"]
        for pname, (a, b) in PERIODS.items():
            m = window_metrics(s_, navs["ONEQ_from_M1"], navs["QQQ_from_M1"], a, b)
            if m:
                summary.append({"rule": f"{name} buy-hold", "period": pname, **m})
    qper = {p: window_metrics(navs["QQQ_from_M1"], navs["ONEQ_from_M1"], navs["QQQ_from_M1"], a, b)
            for p, (a, b) in PERIODS.items()}
    qqq_reference = criteria(qper)
    print("reference (not a trial): QQQ buy-hold judged by the same criteria vs ONEQ:", qqq_reference,
          f"full t {qper[FULL]['t_monthly_excess_vs_oneq']:.2f}")
    tick = ranked.drop_duplicates("security_id").set_index("security_id")["ticker"]
    hd = pd.DataFrame(holds)
    hd["ticker"] = hd["security_id"].map(tick).fillna(hd["security_id"])
    hd.to_csv(OUT / "holdings_by_signal.csv", index=False)
    pd.DataFrame(summary).to_csv(OUT / "summary.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    pd.DataFrame(navs).to_csv(OUT / "nav_daily.csv")
    out = {"window": data.spec, "guard": data.guard["assertion"], "n_trials": N_TRIALS,
           "bonferroni_t_one_sided_5pct": bonferroni_t(N_TRIALS),
           "mcap_source_share_top20_by_year": json.loads(src.to_json(orient="index")),
           "qqq_buyhold_reference_criteria": qqq_reference, "results": results, "terminal_events": int(len(data.terminal_events))}
    (OUT / "results.json").write_text(json.dumps(out, indent=1, default=lambda x: x if not isinstance(x, float)
                                                 else round(x, 6)) + "\n")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check-ranks", action="store_true", help="only build and print the market-cap ranking")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
