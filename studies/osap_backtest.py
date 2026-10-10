"""Long-only test of the profitability predictors picked after the OSAP screen (docs/research_ledger_osap.md 2.1).

P1 = GP          gross profit / total assets                 (Novy-Marx 2013)
P2 = OperProfRD  (operating income + D&A + R&D) / total assets (Ball, Gerakos, Linnainmaa & Nikolaev 2016)

Fundamentals: annual 10-K facts from SEC XBRL companyfacts, point in time by filing date (a fact is usable only on
signal dates strictly after the day it was filed; later restatements count only from their own filing date).
Universe (each month's last universe Friday t): Nasdaq common stocks with dv50 rank <= 300, raw close >= $10, a close
on t, SIC not 6000-6999, signal computable. Hold the top 10 (equal weight at entry), keep a holding while it stays in
the top 20, fill empty slots best-first; trades at the close of the session after t. IBKR Pro Tiered costs.
Signals, universe, schedule and engine: quant.strategies.osap.

Periods (no calibration, 0 variants): A = 2014-01-01 .. 2019-12-31, B = 2020-01-01 .. 2026-08-31.
Pass per period: net CAGR > ONEQ total-return CAGR and monthly-excess t >= 2; a candidate passes if both periods pass.

HARD DATE GUARD: ``quant.data.panel.load_window`` truncates every price / universe / QQQ / terminal frame to [warm-up start, period
end] and asserts it; companyfacts facts are cut to filed <= period end and asserted; every (signal date, stock) row
asserts filed < signal date.

    PYTHONPATH=. .venv/bin/python scripts/research_osap_backtest.py --register
    PYTHONPATH=. .venv/bin/python scripts/research_osap_backtest.py --run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

from quant.data import version as dv
from quant.data.panel import TERMINAL_D5_STRESS
from quant.paths import ROOT
from quant.reporting import Preregistration, public
from quant.strategies import canslim as cs
from quant.strategies import indicators as ind
from quant.strategies.osap import build_states, evaluate, passes, run_period

# ======================================================================== frozen configuration

OUT = dv.versioned(ROOT / "output/research_only/osap") / "backtest"
LEDGER = ROOT / "docs/research_ledger_osap.md"
FROZEN = ROOT / "output/research_only/osap/frozen_prereg.json"
CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/osap")   # raw OSAP files: version independent
STATES_CACHE = dv.versioned(CACHE / "fundamental_states.csv.gz")
PREREG = Preregistration(LEDGER, FROZEN, OUT, include_end=True)   # the block includes the END marker
prereg_block, prereg_hash = PREREG.block, PREREG.hash

PERIODS = {
    "A": {"perf_start": "2014-01-01", "perf_end": "2019-12-31", "price_start": "2012-06-01",
          "universe_start": "2013-06-01", "judged_from": "2014-01-01"},
    "B": {"perf_start": "2020-01-01", "perf_end": "2026-08-31", "price_start": "2018-06-01",
          "universe_start": "2019-06-01", "judged_from": "2020-01-01"},
}
CANDIDATES = {"P1": "gp", "P2": "opr"}
OUT_OF_SCREEN_START = "2025-01-01"

N_HOLD = 10
KEEP_RANK = 20
DV_RANK_MAX = 300
MAX_AGE_DAYS = 550
ACCOUNT = 10_000.0


# ======================================================================== register / run

def register():
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    if FROZEN.is_file():
        raise SystemExit(f"{FROZEN} exists; registration happens once")
    FROZEN.write_text(json.dumps({"ledger": str(LEDGER.relative_to(ROOT)), "prereg_sha256": prereg_hash(),
                                  "registered_at": pd.Timestamp.now("UTC").isoformat(),
                                  "candidates": CANDIDATES, "periods": PERIODS, "n_hold": N_HOLD,
                                  "keep_rank": KEEP_RANK}, indent=2))
    print("registered", FROZEN)


def load_states():
    uni = pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str, usecols=["cik"])
    return build_states(pd.to_numeric(uni["cik"], errors="coerce").dropna().astype(int).unique(), STATES_CACHE)


def run(force: bool = False) -> dict:
    if not FROZEN.is_file():
        raise SystemExit("run --register first")
    frozen = json.loads(FROZEN.read_text())
    if frozen["prereg_sha256"] != prereg_hash():
        raise SystemExit("the pre-registration block in the ledger changed after registration; refusing to run")
    results_path = OUT / "results.json"
    if results_path.is_file() and not force:
        raise SystemExit(f"{results_path} exists; each candidate/period runs once")
    OUT.mkdir(parents=True, exist_ok=True)
    states = load_states()
    summary = {"prereg_sha256": frozen["prereg_sha256"], "periods": {}, "candidates": {}}
    rules = dict(account=ACCOUNT, n_hold=N_HOLD, keep_rank=KEEP_RANK)
    for pname in PERIODS:
        base = run_period(pname, PERIODS[pname], states, DV_RANK_MAX, MAX_AGE_DAYS)
        stress = run_period(pname, PERIODS[pname], states, DV_RANK_MAX, MAX_AGE_DAYS, TERMINAL_D5_STRESS)
        win, rows, sched = base["win"], base["rows"], base["sched"]
        print(win.guard["assertion"])
        cov = rows.groupby("t").agg(n=("security_id", "size"), gp=("gp", lambda x: x.notna().mean()),
                                    opr=("opr", lambda x: x.notna().mean()))
        summary["periods"][pname] = {
            "guard": {k: v for k, v in win.guard.items() if k != "frames"},
            "guard_frames": win.guard["frames"],
            "signal_dates": len(cov), "first_signal": str(cov.index.min().date()),
            "last_signal": str(cov.index.max().date()),
            "avg_universe_names": float(cov["n"].mean()),
            "coverage_gp": float(cov["gp"].mean()), "coverage_opr": float(cov["opr"].mean()),
            "carried_signal_dates": int(rows.loc[rows["carried"], "t"].nunique()),
            "point_in_time_rows_checked": int(rows["filed"].notna().sum()),
            "max_fact_filed_used": str(rows["filed"].dropna().max()),
        }
        rows.to_csv(OUT / f"signals_{pname}.csv.gz", index=False)
        for cand, sig in CANDIDATES.items():
            m, res, oneq, qqq = evaluate(win, sched, rows, sig, **rules)
            m2, *_ = evaluate(win, sched, rows, sig, **rules, spread_mult=2.0)
            ms, *_ = evaluate(stress["win"], stress["sched"], stress["rows"], sig, **rules)
            r = public(m, convert=True, bools=False)
            r["pass"] = passes(m)
            r["excess_cagr_spread2x"] = m2["excess_cagr"]
            r["excess_cagr_terminal_minus100"] = ms["excess_cagr"]
            if pname == "B":
                sub = ind.stock_sub_metrics(res["nav"], oneq, qqq, OUT_OF_SCREEN_START, res["cost"], res["exposure"])
                r["out_of_screen_2025_2026"] = {k: sub[k] for k in ("start", "end", "cagr", "bench_cagr", "qqq_cagr",
                                                                   "excess_cagr", "t_excess_monthly", "max_dd",
                                                                   "bench_max_dd", "months")}
            d = OUT / f"{cand}_{pname}"
            d.mkdir(exist_ok=True)
            res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"],
                                              cost=res["cost"]).to_csv(d / "daily_nav.csv")
            tick = dict(zip(rows["security_id"], rows["ticker"]))
            res["trades"].assign(ticker=lambda x: x["sid"].map(tick)).to_csv(d / "trades.csv", index=False)
            m["_monthly_active"].to_csv(d / "monthly_active_vs_oneq.csv", header=["active"])
            (d / "metrics.json").write_text(json.dumps(r, indent=2, default=str))
            summary["candidates"].setdefault(cand, {})[pname] = r
            print(f"{cand} ({sig}) {pname}: CAGR {m['cagr']:+.1%} ONEQ {m['bench_cagr']:+.1%} QQQ {m['qqq_cagr']:+.1%} "
                  f"ex {m['excess_cagr']:+.1%} t_m {m['t_excess_monthly']:.2f} DD {m['max_dd']:.0%} "
                  f"(ONEQ {m['bench_max_dd']:.0%}) buys {m['n_buy']} cost/yr {m['cost_drag_per_year']:.2%} "
                  f"pass={r['pass']}", flush=True)
    for cand in CANDIDATES:
        a, b = summary["candidates"][cand]["A"]["pass"], summary["candidates"][cand]["B"]["pass"]
        summary["candidates"][cand]["verdict"] = "pass" if a and b else ("unstable" if a or b else "fail")
    results_path.write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--register", action="store_true")
    p.add_argument("--run", action="store_true")
    p.add_argument("--build-states", action="store_true", help="only build the point-in-time fundamentals cache")
    a = p.parse_args(argv)
    if a.register:
        register()
    if a.build_states:
        st = load_states()
        print(len(st), st["cik"].nunique())
    if a.run:
        s = run()
        print(json.dumps({c: v["verdict"] for c, v in s["candidates"].items()}))


if __name__ == "__main__":
    sys.exit(main())
