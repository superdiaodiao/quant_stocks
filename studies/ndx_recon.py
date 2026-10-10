"""Nasdaq-100 reconstitution effect, 2012-2026 (docs/research_ledger_ndx_recon.md; rules registered in its section 0).

Stages (each reads what the one before wrote):
  events   Wikipedia "Historical components of the Nasdaq-100" change table (cached revision) + the hand table
           below -> output/research_only/ndx_recon/events_raw.csv (provisional announcement dates)
  verify   Nasdaq press releases on GlobeNewswire, found through archive.org's CDX index day by day around each
           expected announcement and read from archived copies (publication time in UTC -> ET)
           -> press_releases.csv, hand_check.csv; final event list -> events.csv (IDs, dates, sources only)
  prices   price series per event name (data v2 panel, Yahoo v8, Quandl WIKI) -> research_cache/ndx_recon/series/
           (local only); coverage -> price_coverage.csv
  run      event-time abnormal returns, R1 / R2 / R4 on H1 and H2, R3 diagnostic -> output/research_only/ndx_recon/

Usage::

    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py events
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py verify
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py prices
    REVERSAL_DATA_VERSION=v2 PYTHONPATH=. python scripts/research_ndx_recon.py run

Vendor prices never leave research_cache/; outputs carry returns, dates, tickers and source URLs only. The event
table and the prices are quant.data.ndx_events, the engine and the rule metrics quant.strategies.ndx_recon, the
CAR summary quant.evaluation.events.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quant.data.ndx_events import build_events_raw, build_prices, finalize, sessions_for_events, verify, wiki_text
from quant.data.sources.daily_series import bench_frames, load_series
from quant.evaluation.events import car_summary
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import write_json
from quant.strategies.ndx_recon import (
    ACCOUNT, Asset, build_plan, car_table, metrics, r1_eligible, r2_eligible, segment_assets, simulate,
)

OUT = ROOT / "output/research_only/ndx_recon"
SERIES = CACHE_ROOT / "ndx_recon/series"
V2_PRICES = CACHE_ROOT / "reversal_2012_2026_v2/prices"
V2_INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs_v2"

WIKI_REVID = 1378871158          # 2026-10-06T17:02:05Z, the revision read for this study
WINDOW = ("2012-01-01", "2026-09-30")

HOLD_R2 = 20                     # trading days after the effective date
POST_DAYS = 20
HALVES = {"H1": ("2012-01-01", "2018-12-31"), "H2": ("2019-01-01", "2026-09-30")}
BONFERRONI_T = 2.39              # two-sided 5% over the 3 judged rules (reported only)
RULES = ("R1", "R2", "R4")

# --------------------------------------------------------------------------------------------- hand table
# Corrections to the Wikipedia rows, decided from the press releases / SEC filings before any return was computed
# (ledger 1.2). Keys: (inclusion date as in Wikipedia, added ticker or "", removed ticker or "").
#   inclusion   corrected first day in (out of) the index
#   ann         announcement date (ET) when Wikipedia has none or a wrong one; replaced by a GlobeNewswire date
#               in ``verify`` when one is found
#   add_type / del_type  override of the classification
HAND = {
    ("February 16, 2016", "CSX", "KLAC"): {"inclusion": "2016-02-22",
                                           "note": "release title: CSX joins on February 22, 2016"},
    ("March 18, 2013", "KRFT", "STRZA"): {"ann": "", "note": "Wikipedia cites the 2012 Kraft Foods release"},
    ("November 18, 2013", "MAR", "GOLD"): {"ann": "", "note": "Wikipedia date is the access date"},
    ("July 2, 2015", "KHC", "KRFT"): {"add_type": "continuity", "del_type": "corporate",
                                      "note": "Kraft Foods merged into Kraft Heinz (same index slot)"},
    ("January 15, 2013", "STRZA", "LMCA"): {"add_type": "spin", "del_type": "corporate",
                                            "note": "Liberty Media spun off Starz; the old LMCA became Starz"},
    ("July 27, 2015", "BMRN", ""): {"ann": "", "note": "Wikipedia cites BioMarin's own release"},
    ("October 29, 2013", "VIP", "DELL"): {"ann": "", "note": "Wikipedia cites a news story of the delisting day"},
    ("July 17, 2023", "TTD", "ATVI"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("June 20, 2023", "ON", "RIVN"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("June 24, 2024", "ARM", "SIRI"): {"ann": "2024-06-13", "note": "Nasdaq release dated June 13, 2024"},
    ("November 18, 2024", "APP", "DLTR"): {"ann": "", "note": "Wikipedia cites a news story"},
    ("December 20, 2021", "PANW", "CERN"): {"del_type": "index",
                                            "note": "removed in the annual review (announced 2021-12-10); "
                                                    "the Oracle deal was announced later, on 2021-12-20"},
}

YAHOO_ALIAS = {   # ticker in the index table -> Yahoo symbol(s) holding the same security's history today
    "META": ["META"], "FB": ["META"], "GOOG": ["GOOG"], "VIP": ["VEON"], "TCOM": ["TCOM"], "NLOK": ["GEN"],
    "FI": ["FI", "FISV"], "WTW": ["WTW"], "SIRI": ["SIRI"], "KHC": ["KHC"], "LIN": ["LIN"], "BKNG": ["BKNG"],
    "MSTR": ["MSTR"], "CTSH": ["CTSH"], "LBTYA": ["LBTYA"], "LBTYK": ["LBTYK"], "KFT": ["MDLZ"], "RIMM": ["BB"],
    "CTRP": ["TCOM"], "WLTW": ["WTW"],
}


def load_events() -> pd.DataFrame:
    return pd.read_csv(OUT / "events.csv", dtype=str).fillna("")


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_") and k != "by_year"}


# ============================================================================== run

def run() -> dict:
    ev = load_events()
    q = load_series(SERIES, "etf_QQQ")
    sessions = pd.DatetimeIndex(q["date"][(q["date"] >= "2011-06-01") & (q["date"] <= "2026-10-02")])
    bench = bench_frames(SERIES, sessions, ("QQQ", "ONEQ"))
    assets = {}
    for e in ev.itertuples():
        df = load_series(SERIES, e.event_id)
        if df is not None:
            assets[e.event_id] = Asset(df, sessions)
    # diagnostics: event-time CARs (R3 = adds, post20)
    cars = car_table(ev, sessions, bench, assets, POST_DAYS)
    cars.to_csv(OUT / "car_events.csv", index=False, float_format="%.6f")
    summ = car_summary(cars)
    summ.to_csv(OUT / "car_summary.csv", index=False, float_format="%.6f")
    results, trades_all = [], []
    legs_of = {"R1": ("R1",), "R2": ("R2",), "R4": ("R1", "R2")}
    for half, (start, end) in HALVES.items():
        seg = sessions[(sessions >= start) & (sessions <= end)]
        bseg = {k: v.loc[seg] for k, v in bench.items()}
        oneq_ret, oneq_px = bseg["ONEQ"]["ret"].to_numpy(float), bseg["ONEQ"]["px"].to_numpy(float)
        seg_assets = segment_assets(assets, sessions, seg)
        for rule, legs in legs_of.items():
            for mult in (1.0, 2.0):
                plan = build_plan(ev, seg, start, end, legs, HOLD_R2)
                res = simulate(plan, seg_assets, seg, oneq_ret, oneq_px, spread_mult=mult)
                m = metrics(res, bseg, BONFERRONI_T)
                m.update({"rule": rule, "half": half, "spread_mult": mult, "planned": len(plan)})
                results.append(_clean(m))
                if mult == 1.0:
                    t = res["trades"].copy()
                    t["rule"], t["half"] = rule, half
                    trades_all.append(t)
                    pd.DataFrame({"nav": res["nav"], "oneq": (1 + bseg["ONEQ"]["ret"]).cumprod() * ACCOUNT,
                                  "exposure": res["exposure"]}).to_csv(
                        OUT / f"daily_nav_{rule}_{half}.csv", float_format="%.4f")
    res_df = pd.DataFrame(results)
    res_df.to_csv(OUT / "results.csv", index=False, float_format="%.6f")
    pd.concat(trades_all).to_csv(OUT / "trades.csv", index=False, float_format="%.6f")
    verdict = {}
    for rule in RULES:
        r = res_df[(res_df.rule == rule) & (res_df.spread_mult == 1.0)].set_index("half")
        verdict[rule] = {"H1": bool(r.loc["H1", "crit_pass"]), "H2": bool(r.loc["H2", "crit_pass"]),
                         "pass": bool(r.loc["H1", "crit_pass"] and r.loc["H2", "crit_pass"])}
    summary = {"events": int(len(ev)), "verdict": verdict,
               "r1_eligible": int(sum(r1_eligible(e) for e in ev.itertuples())),
               "r2_eligible": int(sum(r2_eligible(e) for e in ev.itertuples())),
               "with_prices": int(len(assets))}
    write_json(OUT / "summary.json", summary, default=None)
    print(json.dumps(summary, indent=2))
    cols = ["rule", "half", "spread_mult", "cagr", "bench_cagr", "excess_cagr", "t_excess_monthly", "max_dd",
            "bench_max_dd", "crit_A", "crit_B", "entries", "trade_mean_excess_vs_oneq", "trade_t_excess",
            "avg_exposure"]
    print(res_df[cols].to_string(float_format=lambda x: f"{x:.4f}"))
    return summary


# ============================================================================== CLI

def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["events", "verify", "prices", "run"])
    a = ap.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.stage == "events":
        ev = build_events_raw(wiki_text(WIKI_REVID), HAND, WINDOW)
        ev.to_csv(OUT / "events_raw.csv", index=False)
        print(ev.groupby(["side", "type"]).size())
        print(len(ev), "event rows")
    elif a.stage == "verify":
        ev = pd.read_csv(OUT / "events_raw.csv", dtype=str).fillna("")
        checks, rel = verify(ev)
        checks.to_csv(OUT / "hand_check.csv", index=False)
        rel.to_csv(OUT / "press_releases.csv", index=False)
        fin = finalize(ev, checks, sessions_for_events())
        fin.to_csv(OUT / "events.csv", index=False)
        print(fin.groupby(["side", "type"]).size())
    elif a.stage == "prices":
        cov = build_prices(load_events(), SERIES, V2_INPUTS, V2_PRICES, YAHOO_ALIAS)
        cov.to_csv(OUT / "price_coverage.csv", index=False)
        print(cov.status.value_counts())
    else:
        run()


if __name__ == "__main__":
    main()
