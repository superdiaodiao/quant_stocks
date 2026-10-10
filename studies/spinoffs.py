"""Spin-off effect, 2012-2026 (docs/research_ledger_spinoffs.md; rules registered in its section 0).

Stages (each reads what the one before wrote):
  events      SEC event list (+ hand_review.csv, SEC shares outstanding) -> output/research_only/spinoffs/events.csv
              (quant.data.spinoff_events; ``finalize`` re-applies the hand review to the cached raw table)
  prices      price series per spun-off company / parent / benchmark ETF (data v2 panel, Yahoo, WIKI)
              -> research_cache/spinoffs/series/ (local only); coverage -> price_coverage.csv
  archive     archive.org Yahoo history pages for the series still missing -> price_coverage_archive.csv
  crosscheck  the event list against archived CSD ETF holdings -> csd_crosscheck.csv
  run         SP1-SP4 + SP1_d1 on H1 and H2, Nasdaq subset, BHAR diagnostics -> output/research_only/spinoffs/

Usage::

    PYTHONPATH=. python scripts/research_spinoffs.py events
    PYTHONPATH=. python scripts/research_spinoffs.py prices
    PYTHONPATH=. python scripts/research_spinoffs.py archive
    PYTHONPATH=. python scripts/research_spinoffs.py crosscheck
    PYTHONPATH=. python scripts/research_spinoffs.py run

Vendor prices never leave research_cache/; outputs carry returns, dates and SEC facts only. The event list and the
cross-check are quant.data.spinoff_events, the prices quant.data.spinoff_prices, the engine and the rule metrics
quant.strategies.spinoffs, the BHAR summary quant.evaluation.events.
"""
from __future__ import annotations

import argparse
import json

import pandas as pd

from quant.data import spinoff_events as evm
from quant.data.sources import sec_cache as sec
from quant.data.sources.daily_series import bench_frames, load_series
from quant.data.spinoff_prices import build_series, fill_from_archive
from quant.evaluation.events import bhar_summary
from quant.paths import CACHE_ROOT, ROOT
from quant.reporting import write_json
from quant.strategies.spinoffs import Config, attach_mcap, bhar_table, build_plan, metrics, segment_assets, simulate

OUT = ROOT / "output/research_only/spinoffs"
EVENTS = OUT / "events.csv"
SERIES = sec.CACHE / "series"
V2_PRICES = CACHE_ROOT / "reversal_2012_2026_v2/prices"
V2_INPUTS = ROOT / "output/research_only/reversal_2012_2026/inputs_v2"
FILL_BULK = CACHE_ROOT / "reversal_2012_2026_v2_fill/bulk_captures.csv.gz"
BENCH_ETFS = ("ONEQ", "QQQ", "IWM", "IJH", "SPY")

D5_RETURN = -0.55           # delisting with no terminal value (data plan section 0, D5)
BONFERRONI_T = 2.50         # two-sided 5% over 4 rules, reported only
HALVES = {"H1": ("2012-01-01", "2018-12-31"), "H2": ("2019-01-01", "2026-09-30")}
CONFIGS = [Config("SP1"), Config("SP2", months=24), Config("SP3", who="parent"), Config("SP4", min_mcap=1e9),
           Config("SP1_d1", entry_day=1)]
RULES = ("SP1", "SP2", "SP3", "SP4")


def load_events() -> pd.DataFrame:
    ev = pd.read_csv(EVENTS, dtype=str).fillna("")
    for c in ("spinco_cik", "parent_cik"):
        ev[c] = pd.to_numeric(ev[c], errors="coerce")
    return ev


def _clean(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


# ======================================================================== run

def run() -> dict:
    ev = attach_mcap(load_events(), SERIES)
    summary = {"halves": HALVES, "configs": [c.__dict__ for c in CONFIGS], "results": {}}
    sp = ev[ev.classification == "spin_off"]
    has = lambda who, c: c == c and (SERIES / f"{who}_{int(c)}.csv.gz").exists()  # noqa: E731
    sp_prices = sp.spinco_cik.apply(lambda c: has("spinco", c))
    summary["data"] = {
        "events": int(len(sp)), "events_by_half": {h: int(((sp.first_regular_way_date >= a) &
                                                          (sp.first_regular_way_date <= b)).sum())
                                                   for h, (a, b) in HALVES.items()},
        "nasdaq_events": int((sp.nasdaq_listed == "Y").sum()),
        "spinco_with_prices": int(sp_prices.sum()),
        "spinco_without_prices": sp[~sp_prices].event_id.tolist(),
        "spinco_without_prices_delist_reason": sp[~sp_prices].delist_reason.replace("", "none").value_counts().to_dict(),
        "parent_cik_known": int(sp.parent_cik.notna().sum()),
        "parent_with_prices": int(sp.parent_cik.apply(lambda c: has("parent", c)).sum()),
        "mcap_known": int((sp.mcap_usd != "").sum()),
        "mcap_ge_1b": int(sp.mcap_usd.apply(lambda x: x != "" and float(x) >= 1e9).sum()),
    }
    print(json.dumps(summary["data"], default=str)[:600], flush=True)
    all_sessions = pd.DatetimeIndex(load_series(SERIES, "etf_ONEQ")["date"])
    rows, by_year = [], []
    (OUT / "daily_nav").mkdir(parents=True, exist_ok=True)
    (OUT / "trades").mkdir(parents=True, exist_ok=True)
    for half, (start, end) in HALVES.items():
        sessions = all_sessions[(all_sessions >= pd.Timestamp(start)) & (all_sessions <= pd.Timestamp(end))]
        bench = bench_frames(SERIES, sessions, BENCH_ETFS)
        print(f"DATE GUARD {half}: sessions {sessions[0].date()}..{sessions[-1].date()}", flush=True)
        for d5, tag in ((D5_RETURN, ""), (-1.0, "_delist100")):
            assets = segment_assets(ev, sessions, d5, pd.Timestamp(HALVES["H2"][1]), SERIES, V2_INPUTS)
            for cfg in CONFIGS:
                for mult, stag in ((1.0, ""), (2.0, "_spread2")):
                    if tag and stag:
                        continue
                    plan = build_plan(ev, cfg, sessions, start, end)
                    res = simulate(plan, assets, sessions, bench["ONEQ"]["ret"].to_numpy(),
                                   bench["ONEQ"]["px"].to_numpy(), cfg.months, spread_mult=mult)
                    m = metrics(res, bench, BONFERRONI_T)
                    name = cfg.code + tag + stag
                    rows.append({"config": name, "half": half, **{k: v for k, v in _clean(m).items()
                                                                    if not isinstance(v, dict)}})
                    if not tag and not stag:
                        for y, v in m["by_year"].items():
                            by_year.append({"config": name, "half": half, "year": y, **v})
                        res["nav"].to_frame("nav").to_csv(OUT / "daily_nav" / f"{name}_{half}.csv")
                        res["trades"].to_csv(OUT / "trades" / f"{name}_{half}.csv", index=False)
            if not tag:   # Nasdaq-only SP1 (report only)
                plan = build_plan(ev, CONFIGS[0], sessions, start, end, nasdaq_only=True)
                res = simulate(plan, assets, sessions, bench["ONEQ"]["ret"].to_numpy(), bench["ONEQ"]["px"].to_numpy(), 12)
                m = metrics(res, bench, BONFERRONI_T)
                rows.append({"config": "SP1_nasdaq_only", "half": half,
                             **{k: v for k, v in _clean(m).items() if not isinstance(v, dict)}})
    res_df = pd.DataFrame(rows)
    res_df.to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    # verdicts
    verdict = {}
    for code in RULES:
        v = {}
        for half in HALVES:
            r = res_df[(res_df.config == code) & (res_df.half == half)].iloc[0]
            v[half] = {"pass": bool(r.crit_pass), "A": bool(r.crit_A), "B": bool(r.crit_B), "cagr": r.cagr,
                       "oneq_cagr": r.bench_cagr, "t": r.t_excess_monthly, "max_dd": r.max_dd,
                       "oneq_max_dd": r.bench_max_dd}
        v["rule"] = ("pass" if all(v[h]["pass"] for h in HALVES) else
                     "unstable" if any(v[h]["pass"] for h in HALVES) else "fail")
        verdict[code] = v
    summary["verdict"] = verdict
    # diagnostics on the full window
    sessions = all_sessions[(all_sessions >= pd.Timestamp(HALVES["H1"][0])) &
                            (all_sessions <= pd.Timestamp(HALVES["H2"][1]))]
    bench = bench_frames(SERIES, sessions, BENCH_ETFS)
    assets = segment_assets(ev, sessions, D5_RETURN, pd.Timestamp(HALVES["H2"][1]), SERIES, V2_INPUTS)
    bt = bhar_table(ev, sessions, bench, assets, HALVES["H1"][1])
    bt.to_csv(OUT / "bhar_events.csv", index=False)
    bs = bhar_summary(bt)
    bs.to_csv(OUT / "bhar_summary.csv", index=False)
    write_json(OUT / "summary.json", summary, newline=True)
    return summary


# ======================================================================== CLI

def stage_events(rebuild_raw: bool = True) -> pd.DataFrame:
    raw_path = sec.CACHE / "events_raw.csv"
    raw = evm.build_events() if rebuild_raw or not raw_path.exists() else pd.read_csv(raw_path, dtype=str)
    raw.to_csv(raw_path, index=False)
    ev = evm.finalize(raw)
    OUT.mkdir(parents=True, exist_ok=True)
    ev.to_csv(EVENTS, index=False)
    print(ev.classification.value_counts().to_string())
    return ev


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["events", "finalize", "prices", "archive", "run", "crosscheck"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offline", action="store_true", help="prices: cache only")
    a = ap.parse_args(argv)
    if a.stage == "events":
        stage_events(True)
    elif a.stage == "finalize":
        stage_events(False)
    elif a.stage == "archive":
        f = fill_from_archive(load_events(), SERIES, OUT, FILL_BULK, HALVES["H2"][1], limit=a.limit)
        print(f.groupby(["who", "status"]).size().to_string() if len(f) else "nothing to fill")
    elif a.stage == "crosscheck":
        c = evm.csd_crosscheck(load_events(), OUT)
        print(c.to_string())
    elif a.stage == "prices":
        st = build_series(load_events(), SERIES, V2_PRICES, OUT, offline=a.offline)
        print(st.groupby(["who", "status"]).size().to_string())
    else:
        s = run()
        print(json.dumps(s["verdict"], indent=1, default=str))


if __name__ == "__main__":
    main()
