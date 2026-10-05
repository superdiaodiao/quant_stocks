"""Long-only test of the profitability predictors picked after the OSAP screen (docs/research_ledger_osap.md 2.1).

P1 = GP          gross profit / total assets                 (Novy-Marx 2013)
P2 = OperProfRD  (operating income + D&A + R&D) / total assets (Ball, Gerakos, Linnainmaa & Nikolaev 2016)

Fundamentals: annual 10-K facts from SEC XBRL companyfacts, point in time by filing date (a fact is usable only on
signal dates strictly after the day it was filed; later restatements count only from their own filing date).
Universe (each month's last universe Friday t): Nasdaq common stocks with dv50 rank <= 300, raw close >= $10, a close
on t, SIC not 6000-6999, signal computable. Hold the top 10 (equal weight at entry), keep a holding while it stays in
the top 20, fill empty slots best-first; trades at the close of the session after t. IBKR Pro Tiered costs.

Periods (no calibration, 0 variants): A = 2014-01-01 .. 2019-12-31, B = 2020-01-01 .. 2026-08-31.
Pass per period: net CAGR > ONEQ total-return CAGR and monthly-excess t >= 2; a candidate passes if both periods pass.

HARD DATE GUARD: ``lv.load_window`` truncates every price / universe / QQQ / terminal frame to [warm-up start, period
end] and asserts it; companyfacts facts are cut to filed <= period end and asserted; every (signal date, stock) row
asserts filed < signal date.

    PYTHONPATH=. .venv/bin/python scripts/research_osap_backtest.py --register
    PYTHONPATH=. .venv/bin/python scripts/research_osap_backtest.py --run
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_canslim_dev as cs  # noqa: E402  (companyfacts paths, date guard, QQQ benchmark)
from scripts import research_indicators as ind  # noqa: E402  (ONEQ on sessions, metrics vs ONEQ)
from scripts import research_livermore as lv  # noqa: E402  (window loader)
from scripts import research_reversal_dev as rev  # noqa: E402  (IBKR order cost, half spread, terminal values)

from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT = dv.versioned(ROOT / "output/research_only/osap") / "backtest"
LEDGER = ROOT / "docs/research_ledger_osap.md"
FROZEN = ROOT / "output/research_only/osap/frozen_prereg.json"
CACHE = Path("/Users/bytedance/code/quant_stocks/research_cache/osap")   # raw OSAP files: version independent
DateGuardError = cs.DateGuardError

PERIODS = {
    "A": {"perf_start": "2014-01-01", "perf_end": "2019-12-31", "price_start": "2012-06-01",
          "universe_start": "2013-06-01", "judged_from": "2014-01-01"},
    "B": {"perf_start": "2020-01-01", "perf_end": "2026-08-31", "price_start": "2018-06-01",
          "universe_start": "2019-06-01", "judged_from": "2020-01-01"},
}
for _k, _v in PERIODS.items():
    lv.WINDOWS[f"osap_{_k}"] = dict(_v)
CANDIDATES = {"P1": "gp", "P2": "opr"}
OUT_OF_SCREEN_START = "2025-01-01"

N_HOLD = 10
KEEP_RANK = 20
DV_RANK_MAX = 300
MAX_AGE_DAYS = 550
ACCOUNT = 10_000.0

REVENUE = ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet", "SalesRevenueGoodsNet")
COGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization", "CostOfServices")
GROSS = ("GrossProfit",)
OPINC = ("OperatingIncomeLoss",)
DA = ("DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet")
DA_PARTS = ("Depreciation", "AmortizationOfIntangibleAssets")
RD = ("ResearchAndDevelopmentExpense", "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost")
ASSETS = ("Assets",)
GROUPS = {"rev": REVENUE, "cogs": COGS, "gross": GROSS, "opinc": OPINC, "da": DA, "da_part": DA_PARTS, "rd": RD,
          "assets": ASSETS}


# ======================================================================== fundamentals (point in time)

def extract_annual_facts(cik: int) -> pd.DataFrame:
    """Annual-duration (340-390 days) facts of the income-statement concepts and Assets instants, 10-K forms only."""
    cols = ["cik", "group", "concept", "prio", "end", "val", "filed"]
    p = cs._cf_path(cik)
    if p is None:
        return pd.DataFrame(columns=cols)
    env = json.loads(gzip.decompress(p.read_bytes()))
    gaap = env.get("payload", env).get("facts", {}).get("us-gaap", {})
    rows = []
    for group, concepts in GROUPS.items():
        for prio, concept in enumerate(concepts):
            for unit, facts in gaap.get(concept, {}).get("units", {}).items():
                if unit != "USD":
                    continue
                for f in facts:
                    if f.get("val") is None or not str(f.get("form", "")).startswith("10-K"):
                        continue
                    if group == "assets":
                        if "start" in f:
                            continue
                    else:
                        if "start" not in f:
                            continue
                        dur = (pd.Timestamp(f["end"]) - pd.Timestamp(f["start"])).days
                        if not 340 <= dur <= 390:
                            continue
                    rows.append((int(cik), group, concept, prio, f["end"], float(f["val"]), f["filed"]))
    return pd.DataFrame(rows, columns=cols)


def fy_values(known: dict, fy_end: str) -> dict | None:
    """Signal inputs for the fiscal year ending ``fy_end``; ``known`` = {(group, end): {concept: val}} holds only facts
    already filed. Within a group the first concept in its priority list wins.

    gp  = gross profit / assets (GrossProfit, else revenue - cost of revenue; NaN when neither exists or when gross
          profit exceeds revenue, a tagging error)
    opr = (operating income + D&A + R&D) / assets, the XBRL counterpart of Compustat revt - cogs - (xsga - xrd)
          (= OIBDP + xrd); missing D&A or R&D count as 0, missing operating income or a numerator above revenue
          (a units / tagging error) -> NaN
    """
    def pick(group, end):
        vals = known.get((group, end))
        if not vals:
            return None
        return min(vals.items(), key=lambda kv: GROUPS[group].index(kv[0]))[1]

    rev_ = pick("rev", fy_end)
    fe = pd.Timestamp(fy_end)
    a_ends = sorted((abs((pd.Timestamp(e) - fe).days), e) for (g, e) in known if g == "assets"
                    and abs((pd.Timestamp(e) - fe).days) <= 7)
    assets = pick("assets", a_ends[0][1]) if a_ends else None
    if rev_ is None or rev_ <= 0 or assets is None or assets <= 0:
        return None
    gross = pick("gross", fy_end)
    if gross is None:
        cogs = pick("cogs", fy_end)
        gross = None if cogs is None else rev_ - cogs
    if gross is not None and gross > rev_ * 1.0001:
        gross = None
    opinc = pick("opinc", fy_end)
    da = pick("da", fy_end)
    if da is None:
        da = float(sum(known.get(("da_part", fy_end), {}).values()))
    rd = pick("rd", fy_end) or 0.0
    num = None if opinc is None else opinc + da + rd
    if num is not None and num > rev_ * 1.0001:      # cannot exceed revenue: a units / tagging error
        num = None
    if gross is None and num is None:
        return None
    return {"fy_end": fy_end, "rev": rev_, "gross": gross, "opinc": opinc, "da": da, "rd": rd, "assets": assets,
            "gp": np.nan if gross is None else gross / assets,
            "opr": np.nan if num is None else num / assets}


def fundamental_states(facts: pd.DataFrame) -> pd.DataFrame:
    """One row per filing date with, for each signal, its value in the latest fiscal year where it is computable
    from facts filed up to that date (usable strictly later). For one (concept, period) the most recently filed value
    wins, so restatements apply from their own filing date."""
    cols = ["cik", "filed", "gp", "gp_fy_end", "opr", "opr_fy_end"]
    if facts.empty:
        return pd.DataFrame(columns=cols)
    known: dict = {}
    rows = []
    cik = int(facts["cik"].iloc[0])
    for filed, g in facts.sort_values("filed", kind="stable").groupby("filed", sort=True):
        for r in g.itertuples():
            known.setdefault((r.group, r.end), {})[r.concept] = r.val
        row = {"cik": cik, "filed": filed}
        for e in sorted({e for (gr, e) in known if gr == "rev"}, reverse=True):
            v = fy_values(known, e)
            if v is None:
                continue
            for sig in ("gp", "opr"):
                if sig not in row and np.isfinite(v[sig]):
                    row[sig], row[f"{sig}_fy_end"] = v[sig], e
            if "gp" in row and "opr" in row:
                break
        if "gp" in row or "opr" in row:
            rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def build_states(ciks, cache: Path | None = dv.versioned(CACHE / "fundamental_states.csv.gz")) -> pd.DataFrame:
    if cache is not None and cache.is_file():
        return pd.read_csv(cache, dtype={"filed": str, "gp_fy_end": str, "opr_fy_end": str})
    parts = [fundamental_states(extract_annual_facts(int(c))) for c in sorted(set(int(x) for x in ciks))]
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(cache, index=False)
    return out


def asof_states(rows: pd.DataFrame, states: pd.DataFrame, end: str) -> pd.DataFrame:
    """For each (signal date t, cik) row, the latest state filed strictly before t; facts filed after ``end`` are
    dropped first (date guard) and the result is asserted point in time."""
    st = states[states["filed"].astype(str) <= end].copy()
    cs.assert_window(st["filed"], None, end, "fundamental filings")
    st["usable_from"] = pd.to_datetime(st["filed"]) + pd.Timedelta(days=1)
    st["cik_i"] = st["cik"].astype(int)
    st = st.sort_values("usable_from")
    left = rows.copy()
    left["cik_i"] = pd.to_numeric(left["cik"], errors="coerce")
    left = left[left["cik_i"].notna()].copy()
    left["cik_i"] = left["cik_i"].astype(int)
    left = left.sort_values("t")
    m = pd.merge_asof(left, st.drop(columns=["cik"]), left_on="t", right_on="usable_from", by="cik_i",
                      direction="backward")
    used = m.dropna(subset=["filed"])
    if not (pd.to_datetime(used["filed"]) < used["t"]).all():
        raise DateGuardError("fundamental fact used on or before its filing date")
    for c in ("gp", "opr"):
        age = (m["t"] - pd.to_datetime(m[f"{c}_fy_end"])).dt.days
        m.loc[~(age <= MAX_AGE_DAYS), c] = np.nan
    return m


# ======================================================================== universe and schedule

def monthly_universe(win: lv.WinData) -> pd.DataFrame:
    """Rows (t, security_id, ticker, cik, dv50_rank) for each month's last universe Friday; weeks after the last
    universe file week reuse that week's membership (flagged ``carried``)."""
    uni = win.universe
    sic = pd.to_numeric(uni["sic"], errors="coerce")
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & uni["dv50_rank"].notna() & \
        (uni["dv50_rank"] <= DV_RANK_MAX) & ~sic.between(6000, 6999) & uni["security_id"].isin(win.close.columns)
    u = uni.loc[ok, ["week_end", "security_id", "ticker", "cik", "dv50_rank"]].copy()
    u["carried"] = False
    last = u["week_end"].max()
    end = pd.Timestamp(win.spec["effective_end"])
    extra = [u[u["week_end"] == last].assign(week_end=f, carried=True)
             for f in pd.date_range(last + pd.Timedelta(days=7), end, freq="W-FRI")
             if (win.sessions <= f).any() and win.sessions[win.sessions <= f][-1] > last]
    if extra:
        u = pd.concat([u] + extra, ignore_index=True)
    month_last = u.groupby(u["week_end"].dt.to_period("M"))["week_end"].transform("max")
    u = u[u["week_end"] == month_last].rename(columns={"week_end": "t"})
    cs.assert_window(u["t"], win.spec["universe_start"], win.spec["effective_end"], "signal dates")
    return u.reset_index(drop=True)


def schedule(ts, sessions: pd.DatetimeIndex, perf_start: pd.Timestamp, end: pd.Timestamp) -> dict:
    """execution session -> signal date. Execution = first session after t; the last signal before the period
    executes at the first performance session. Signals executing after ``end`` are dropped."""
    out = {}
    for t in sorted(pd.to_datetime(pd.Series(list(ts)).unique())):
        later = sessions[sessions > t]
        if not len(later):
            continue
        e = max(later[0], perf_start)
        if e > end:
            continue
        out[e] = t              # a later signal mapping to the same session (only at the start) wins
    cs.assert_window(list(out), str(perf_start.date()), str(end.date()), "execution sessions")
    return out


def ranked_picks(rows: pd.DataFrame, signal: str) -> dict:
    """t -> list of (security_id, dv50_rank) sorted by the signal descending (ties by security_id)."""
    g = rows.dropna(subset=[signal]).sort_values(["t", signal, "security_id"], ascending=[True, False, True])
    return {t: list(zip(x["security_id"], x["dv50_rank"])) for t, x in g.groupby("t")}


# ======================================================================== engine

def simulate(win: lv.WinData, sched: dict, picks: dict, account: float = ACCOUNT, spread_mult: float = 1.0,
             n_hold: int = N_HOLD, keep_rank: int = KEEP_RANK) -> dict:
    perf_start = pd.Timestamp(win.guard["perf_start_session"])
    end = pd.Timestamp(win.spec["effective_end"])
    perf = win.sessions[(win.sessions >= perf_start) & (win.sessions <= end)]
    cs.assert_window(perf, str(perf_start.date()), str(end.date()), "simulation sessions")
    cols = list(win.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = win.perf_idx.reindex(perf).to_numpy()
    P = win.close.reindex(perf).to_numpy()
    lr = win.last_row.reindex(cols)
    cash = account
    pos: dict = {}            # sid -> [units, entry_i, entry_idx]
    nav, expo, cost = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded, trades, n_buy, n_sell = 0.0, [], 0, 0
    rank_now: dict = {}
    for i, d in enumerate(perf):
        day_cost = 0.0
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            u, ei, e0 = pos.pop(sid)
            cash += u * I[i, col[sid]]
            trades.append({"sid": sid, "entry": str(perf[ei].date()), "exit": str(d.date()), "reason": "delisted",
                           "ret": I[i, col[sid]] / e0 - 1})
        if d in sched:
            t = sched[d]
            ranked = [(s, r) for s, r in picks.get(t, []) if s in col]
            rank_now = dict(ranked)
            keep = {s for s, _ in ranked[:keep_rank]}
            for sid in sorted(pos):
                if sid in keep:
                    continue
                u, ei, e0 = pos.pop(sid)
                c = col[sid]
                val = u * I[i, c]
                k = rev.order_cost(val / P[i, c], P[i, c], sell=True,
                                   hs=rev.half_spread(rank_now.get(sid, np.nan), P[i, c], spread_mult))["total"]
                cash += val - k
                day_cost += k
                traded += val
                n_sell += 1
                trades.append({"sid": sid, "entry": str(perf[ei].date()), "exit": str(d.date()),
                               "reason": "out_of_top20", "ret": I[i, c] / e0 - 1})
            total = cash + sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
            size = total / n_hold
            for sid, rank in ranked:
                if len(pos) >= n_hold:
                    break
                c = col[sid]
                if sid in pos or not np.isfinite(I[i, c]) or not np.isfinite(P[i, c]) or P[i, c] <= 0:
                    continue
                if pd.notna(lr[sid]) and lr[sid] < d:
                    continue
                amt = min(size, cash)
                if amt < 0.2 * size or amt < 100:
                    break
                k = rev.order_cost(amt / P[i, c], P[i, c], sell=False, hs=rev.half_spread(rank, P[i, c], spread_mult))["total"]
                pos[sid] = [(amt - k) / I[i, c], i, I[i, c]]
                cash -= amt
                day_cost += k
                traded += amt
                n_buy += 1
        sv = sum(u * I[i, col[s]] for s, (u, _, _) in pos.items())
        nav[i] = cash + sv
        expo[i] = sv / nav[i] if nav[i] > 0 else 0.0
        cost[i] = day_cost
    held = [{"sid": s, "entry": str(perf[ei].date()), "exit": None, "reason": "held_at_end",
             "ret": I[-1, col[s]] / e0 - 1} for s, (u, ei, e0) in pos.items()]
    return {"dates": perf, "nav": pd.Series(nav, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(cost, index=perf), "traded": traded, "n_buy": n_buy, "n_sell": n_sell,
            "trades": pd.DataFrame(trades + held)}


# ======================================================================== benchmarks, metrics, criteria

def benchmarks(win: lv.WinData, dates: pd.DatetimeIndex, account: float = ACCOUNT):
    ps, end = win.spec["price_start"], win.spec["effective_end"]
    lvl, px = ind.oneq_on_sessions(win.sessions, ps, end)
    cs.assert_window(lvl.dropna().index, ps, end, "ONEQ")
    q = lvl.reindex(dates)
    kc = cs.order_cost(account, float(px.reindex(dates).iloc[0]), False, ind.HALF_SPREAD["COMP"])
    oneq = (account - kc) * q / q.iloc[0]
    qqq = cs.qqq_benchmark(win.qqq_perf_idx, win.qqq_close, dates, account)
    return oneq, qqq


def passes(m: dict) -> bool:
    return bool(m["cagr"] > m["bench_cagr"] and m["t_excess_monthly"] >= 2.0)


def clean(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if k.startswith("_"):
            continue
        out[k] = float(v) if isinstance(v, (np.floating, np.integer)) else v
    return out


# ======================================================================== register / run

def prereg_block(text: str | None = None) -> str:
    text = LEDGER.read_text() if text is None else text
    a, b = text.index("<!-- PREREG-BEGIN -->"), text.index("<!-- PREREG-END -->")
    return text[a:b + len("<!-- PREREG-END -->")]


def prereg_hash(text: str | None = None) -> str:
    return hashlib.sha256(prereg_block(text).encode()).hexdigest()


def register():
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    if FROZEN.is_file():
        raise SystemExit(f"{FROZEN} exists; registration happens once")
    FROZEN.write_text(json.dumps({"ledger": str(LEDGER.relative_to(ROOT)), "prereg_sha256": prereg_hash(),
                                  "registered_at": pd.Timestamp.now("UTC").isoformat(),
                                  "candidates": CANDIDATES, "periods": PERIODS, "n_hold": N_HOLD,
                                  "keep_rank": KEEP_RANK}, indent=2))
    print("registered", FROZEN)


def run_period(name: str, states_all: pd.DataFrame, terminal: float = rev.TERMINAL_D5) -> dict:
    win = lv.load_window(f"osap_{name}", terminal)
    end = win.spec["effective_end"]
    rows = monthly_universe(win)
    rows = asof_states(rows, states_all, end)
    perf_start = pd.Timestamp(win.guard["perf_start_session"])
    sched = schedule(rows["t"], win.sessions, perf_start, pd.Timestamp(end))
    out = {"guard": win.guard, "rows": rows, "sched": sched, "win": win}
    return out


def evaluate(win, sched, rows, signal: str, spread_mult: float = 1.0) -> tuple[dict, dict, pd.Series, pd.Series]:
    res = simulate(win, sched, ranked_picks(rows, signal), spread_mult=spread_mult)
    oneq, qqq = benchmarks(win, res["dates"])
    m = ind.stock_metrics(res["nav"], oneq, qqq, ACCOUNT, res["cost"], res["traded"], res["exposure"])
    m.update({"n_buy": res["n_buy"], "n_sell": res["n_sell"]})
    return m, res, oneq, qqq


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
    uni = pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str, usecols=["cik"])
    states = build_states(pd.to_numeric(uni["cik"], errors="coerce").dropna().astype(int).unique())
    summary = {"prereg_sha256": frozen["prereg_sha256"], "periods": {}, "candidates": {}}
    for pname in PERIODS:
        base = run_period(pname, states)
        stress = run_period(pname, states, rev.TERMINAL_D5_STRESS)
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
            m, res, oneq, qqq = evaluate(win, sched, rows, sig)
            m2, *_ = evaluate(win, sched, rows, sig, spread_mult=2.0)
            ms, *_ = evaluate(stress["win"], stress["sched"], stress["rows"], sig)
            r = clean(m)
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
        uni = pd.read_csv(cs.INPUTS / "weekly_universe_top300.csv.gz", dtype=str, usecols=["cik"])
        st = build_states(pd.to_numeric(uni["cik"], errors="coerce").dropna().astype(int).unique())
        print(len(st), st["cik"].nunique())
    if a.run:
        s = run()
        print(json.dumps({c: v["verdict"] for c, v in s["candidates"].items()}))


if __name__ == "__main__":
    sys.exit(main())
