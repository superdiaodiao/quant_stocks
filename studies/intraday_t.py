"""Repeated intraday "T" trades (做T) on QQQ / QLD (pre-registered in docs/research_ledger_intraday_t.md, section 0).

Base: 75% of a $10,000 cash account in integer shares, 25% settled cash as the T reserve (= 1/3 of the base value);
month-end rebalance back to 75% when the base weight leaves 70-80%. Each session, with P = the previous real close:

- T1 (sell high, buy back at the close): limit sell q shares at L = ceil_cent(P * (1 + k)). Fills at the open when
  open >= L, else at L when high >= L + max($0.01, 0.05% * L). Filled shares are bought back at the close (MOC).
- T2 (buy the dip, sell at the close): limit buy q shares with the reserve at L = floor_cent(P * (1 - k)). Fills at
  the open when open <= L, else at L when low <= L - max($0.01, 0.05% * L). Sold at the close.
- T3: sell q shares at the open (MOO), buy them back at the close (MOC), every session.

k = 1% or 0.5 * ATR20% (mean true range of the 20 sessions up to t-1 over P). q = min(floor(B / 3), floor((cash - $5)
/ cap)) with cap = 1.10 * P for T1 / T3 and L for T2. These rules need only the day's open / high / low / close, never
the order in which the high and low happened.

Reference H75: the same base / reserve / rebalance with no T trades (the key comparison). H100: all-in integer shares,
buy and hold. Benchmark ONEQ: buy and hold. Costs: scripts/research_reversal_dev.order_cost (IBKR Tiered) with a
half-spread (QQQ 1 bp, QLD / ONEQ 2 bp, at least half a cent) on every order, limit fills and auctions included.

Yahoo OHLC are split-adjusted; real traded prices are rebuilt by multiplying back later splits, so share counts and
per-share commissions are real. Every vendor frame is truncated at ``END`` and asserted.
Raw data local only (research_cache/calendar/raw, research_cache/qqq_timing/raw). Outputs (returns, counts and
statistics only, no price levels): output/research_only/intraday_t/. The real-price OHLC, the limit fills, the
cost model and the metrics are in quant (quant.data.ohlc, quant.backtest, quant.evaluation); the rules and the
engine are quant.strategies.intraday_t.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study intraday_t   (or scripts/research_intraday_t.py)
"""
from __future__ import annotations

import argparse

import pandas as pd

from quant.backtest.execution import buy_hold_returns
from quant.data.rates import daily_rf
from quant.evaluation.criteria import ab_criteria, bonferroni_t
from quant.evaluation.events import metrics_vs_refs, trip_stats_usd, trips_by_year_usd
from quant.evaluation.metrics import TRADING_DAYS, core_metrics, monthly, yearly
from quant.paths import CACHE_ROOT, output_dir
from quant.reporting import write_json
from quant.strategies.intraday_t import HALF_SPREAD_BPS, ONEQ_FIRST_RETURN, Spec, load, name_of, period_bounds, simulate

END = "2026-09-30"
CAL_RAW = CACHE_ROOT / "calendar/raw"
QT_RAW = CACHE_ROOT / "qqq_timing/raw"
SOURCES = {"QQQ": CAL_RAW / "chart_QQQ.json", "QLD": QT_RAW / "chart_QLD.json", "ONEQ": CAL_RAW / "chart_ONEQ.json"}
OUT = output_dir("intraday_t")

BASES = ("QQQ", "QLD")
RULES = ("T1", "T2", "T3")
CONFIGS = (("T1", "k1"), ("T1", "atr"), ("T2", "k1"), ("T2", "atr"), ("T3", "none"))


def evaluate(r: pd.Series, h75: pd.Series, h100: pd.Series, oneq: pd.Series, rf: pd.Series) -> dict:
    return metrics_vs_refs(r, {"h75": h75, "h100": h100}, oneq, rf)


def judge(per: dict) -> dict:
    h1, h2, f = per["half1"], per["half2"], per["full"]
    adds = h1["excess_vs_h75"] > 0 and h2["excess_vs_h75"] > 0 and f["t_monthly_excess_vs_h75"] >= 2.0
    a, b = ab_criteria((h1, h2), f, cagr="excess_vs_oneq", bench=None)
    return {"adds_value_vs_h75": bool(adds), "A": bool(a), "B": bool(b), "pass_vs_oneq": bool(a or b)}


# ======================================================================== main

def run(args=None) -> dict:
    data = {s: load(s, SOURCES[s], END) for s in ("QQQ", "QLD", "ONEQ")}
    for s in BASES:
        oq = data["ONEQ"].sessions
        extra = oq[oq >= data[s].sessions[0]].difference(data[s].sessions)
        if len(extra):
            raise ValueError(f"ONEQ sessions not in the {s} calendar: {list(extra[:5])}")
    print("DATA CHECKS:", {k: v.checks for k, v in data.items()})
    res = {"end": END, "data_checks": {k: v.checks for k, v in data.items()}, "n_trials": len(CONFIGS) * len(BASES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(CONFIGS) * len(BASES)), "bases": {}}
    rows, by_year, trips_year, mon = [], {}, [], {}
    for base in BASES:
        d = data[base]
        rf = daily_rf(d.sessions, END)
        bounds = period_bounds(d, END)
        binfo = {"periods": {k: [str(d.sessions[s].date()), str(d.sessions[e].date())] for k, (s, e) in bounds.items()},
                 "configs": {}, "references": {}}
        refs = {}
        for pk, (s, e) in bounds.items():
            for costs in (True, False):
                h75 = simulate(d, s, e, Spec(None, costs=costs))
                h100 = simulate(d, s, e, Spec(None, base_frac=1.0, reserve=False, rebalance=False, costs=costs))
                so = max(d.sessions[s], pd.Timestamp(ONEQ_FIRST_RETURN))
                oq = buy_hold_returns(data["ONEQ"], d.sessions, so, d.sessions[e], HALF_SPREAD_BPS["ONEQ"], costs)
                refs[(pk, costs)] = (h75, h100, oq)
            h75, h100, oq = refs[(pk, True)]
            binfo["references"][pk] = {
                "H75": evaluate(h75["ret"], h75["ret"], h100["ret"], oq, rf),
                "H100": evaluate(h100["ret"], h75["ret"], h100["ret"], oq, rf),
                "ONEQ": {"start": str(oq.index[0].date()), **core_metrics(oq, rf)}}
            if pk == "full":
                by_year[f"{base}_H75"] = yearly(h75["ret"])
                by_year[f"{base}_H100"] = yearly(h100["ret"])
                by_year[f"{base}_ONEQ"] = yearly(oq)
                mon[f"{base}_H75"], mon[f"{base}_H100"] = monthly(h75["ret"]), monthly(h100["ret"])
        for pk in bounds:
            ref = binfo["references"][pk]
            rows.append({"base": base, "config": "H75", "variant": "net", "period": pk, **ref["H75"]})
            rows.append({"base": base, "config": "H100", "variant": "net", "period": pk, **ref["H100"]})
        for rule, km in CONFIGS:
            nm = name_of(rule, km)
            info = {"periods": {}}
            for pk, (s, e) in bounds.items():
                years = (e - s + 1) / TRADING_DAYS
                pr = {}
                variants = {
                    "net": (Spec(rule, km), True),
                    "gross": (Spec(rule, km, costs=False), False),
                    "touch_fill": (Spec(rule, km, through=False), True),
                    "margin_100": (Spec(rule, km, base_frac=1.0, reserve=False, rebalance=False), True),
                }
                for vn, (spec, costs) in variants.items():
                    sim = simulate(d, s, e, spec)
                    h75, h100, oq = refs[(pk, costs)]
                    m = evaluate(sim["ret"], h75["ret"], h100["ret"], oq, rf)
                    m.update(trip_stats_usd(sim["trips"], years))
                    m.update({"orders_per_year": sim["orders"] / years,
                              "cost_drag_per_year": sim["cost_frac"] / years,
                              "gfv_risk_days": sim["gfv_days"], "pdt_breach_share": sim["pdt_breach_share"]})
                    pr[vn] = m
                    rows.append({"base": base, "config": nm, "variant": vn, "period": pk, **m})
                    if pk == "full" and vn == "net":
                        by_year[f"{base}_{nm}"] = yearly(sim["ret"])
                        mon[f"{base}_{nm}"] = monthly(sim["ret"])
                        ty = trips_by_year_usd(sim["trips"], base, nm)
                        if ty is not None:
                            trips_year.append(ty)
                info["periods"][pk] = pr
            info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in bounds})
            info["judgement_margin_100_report_only"] = {
                "adds_value_vs_h100": bool(info["periods"]["half1"]["margin_100"]["excess_vs_h100"] > 0
                                           and info["periods"]["half2"]["margin_100"]["excess_vs_h100"] > 0
                                           and info["periods"]["full"]["margin_100"]["t_monthly_excess_vs_h100"] >= 2)}
            binfo["configs"][nm] = info
            f, h1, h2 = (info["periods"][k]["net"] for k in ("full", "half1", "half2"))
            print(f"{base} {nm}: trips/yr {f['trips_per_year']:.0f}, gross/net per trip {f['gross_per_trip_bp']:+.1f}/"
                  f"{f['net_per_trip_bp']:+.1f} bp; vs H75 half1 {h1['excess_vs_h75']:+.2%} half2 "
                  f"{h2['excess_vs_h75']:+.2%} t {f['t_monthly_excess_vs_h75']:+.2f}; CAGR {f['cagr']:+.2%} "
                  f"MDD {f['max_dd']:.1%}; {info['judgement']}")
        res["bases"][base] = binfo
    OUT.mkdir(parents=True, exist_ok=True)
    write_json(OUT / "results.json", res, default=float)
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    if trips_year:
        pd.concat(trips_year).to_csv(OUT / "trips_by_year.csv", index=False, float_format="%.4f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
