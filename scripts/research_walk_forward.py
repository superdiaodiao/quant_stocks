"""Walk-forward parameter re-selection ("adapt to the recent market") on the fine grids of research_grid_check.py.

Pre-registered in docs/research_ledger_indicators.md section 9 BEFORE any walk-forward number was computed.
THIS IS A WEAK TEST: every year used here was seen before (sections 2-3, 5, 7) and the grid itself is post-hoc.

Every January 1 of year Y, pick the grid point with the best Sharpe (secondary: CAGR) over the trailing L years
(L in {1, 3, 5}; primary L = 3, Sharpe) using only returns dated before Y, then trade it during Y. The picked points
are stitched into ONE continuous simulation (positions carry across years; at the switch close the portfolio moves to
what the new parameters would hold; all costs, including switching trades, are charged).

Settings and grids, data, costs and simulators all reuse scripts/research_indicators.py / research_grid_check.py:
  index  ^IXIC OHLC signals, hold ONEQ (^IXIC price return before 2003-10-02), T-bill ETF when out;
         stand-alone runs from 1999-03-10; walk-forward 2002-01 .. 2026-09-30
  stock  weekly dollar-volume top 300, close >= $10, up to 5 names, $10k IBKR Tiered; one continuous window
         (prices from 2011-06, universe and performance from 2012-01, to the last price day 2026-08-31);
         walk-forward 2018-01 .. 2026-08-31
Decision timing: the pick for Y uses data through the last close of Y-1; decisions from that close on use Y's
parameters; every decision fills at the next close (as in section 0.3).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_grid_check as gc  # noqa: E402
from scripts import research_indicators as ri  # noqa: E402

qt, lv, cs, rev = ri.qt, ri.lv, ri.cs, ri.rev
from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT = dv.versioned(ROOT / "output/research_only/walk_forward")
FutureDataError = ri.FutureDataError

LOOKBACKS = (1, 3, 5)
METRICS = ("sharpe", "cagr")
PRIMARY = {"L": 3, "metric": "sharpe"}
KINDS = ("ma", "donchian")

INDEX_DATA_START, INDEX_END = ri.INDEX_DEV_START, ri.INDEX_TEST_END       # 1999-03-10 .. 2026-09-30
INDEX_WF_YEARS = range(2002, 2027)
INDEX_HALVES = (("2002-01-01", "2013-12-31"), ("2014-01-01", "2026-09-30"))
STOCK_WINDOW = {"perf_start": "2012-01-01", "perf_end": "2026-09-30", "price_start": "2011-06-01",
                "universe_start": "2012-01-01", "judged_from": "2018-01-01"}
STOCK_WF_YEARS = range(2018, 2027)
STOCK_HALVES = (("2018-01-01", "2021-12-31"), ("2022-01-01", "2026-08-31"))
ACCOUNT = 10_000.0
BONFERRONI_4 = 2.2414          # one-sided 5% / 4 primary tests (reported only)


# ======================================================================== picking (past data only)

def trailing_window(r: pd.Series, year: int, L: int) -> pd.Series:
    """Daily returns dated in [Jan 1 of year-L, Jan 1 of year). Raises if anything on/after Jan 1 of ``year`` leaks."""
    lo, hi = pd.Timestamp(f"{year - L}-01-01"), pd.Timestamp(f"{year}-01-01")
    w = r[(r.index >= lo) & (r.index < hi)].dropna()
    if len(w) and w.index.max() >= hi:
        raise FutureDataError(f"pick for {year} would use {w.index.max().date()}")
    return w


def score(w: pd.Series, metric: str, rf: pd.Series | None = None) -> float:
    if len(w) < 20:
        return -np.inf
    if metric == "sharpe":
        ex = w - rf.reindex(w.index).fillna(0.0) if rf is not None else w
        sd = ex.std()
        return float(ex.mean() / sd * math.sqrt(252)) if sd > 0 else -np.inf
    if metric == "cagr":
        return float(np.prod(1 + w.values) ** (252 / len(w)) - 1)
    raise ValueError(metric)


def pick(rets: dict, year: int, L: int, metric: str, rf: pd.Series | None = None) -> dict:
    """Best grid point (keys in ``rets`` order; ties -> first) by ``metric`` over the trailing L years before ``year``."""
    best, best_v, first, last = None, -np.inf, None, None
    for key, r in rets.items():
        w = trailing_window(r, year, L)
        v = score(w, metric, rf)
        if best is None or v > best_v:
            best, best_v = key, v
            first, last = (w.index.min(), w.index.max()) if len(w) else (pd.NaT, pd.NaT)
    if last is not None and pd.notna(last):
        assert last < pd.Timestamp(f"{year}-01-01")
    return {"year": year, "L": L, "metric": metric, "key": best, "value": best_v,
            "window_first": str(first.date()) if pd.notna(first) else "", "window_last": str(last.date()) if pd.notna(last) else ""}


def all_picks(rets_by_kind: dict, years, rf=None) -> dict:
    """(kind, L, metric) -> {year: pick dict}."""
    out = {}
    for kind, rets in rets_by_kind.items():
        for L in LOOKBACKS:
            for m in METRICS:
                out[(kind, L, m)] = {y: pick(rets, y, L, m, rf) for y in years}
    return out


def decision_year(sessions: pd.DatetimeIndex) -> np.ndarray:
    """For a decision at close t: the year of the next session (decisions at Y-1's last close already use Y's
    parameters, since the pick uses data through that close and the order fills at the next close)."""
    yrs = sessions.year.to_numpy()
    out = np.empty_like(yrs)
    out[:-1] = yrs[1:]
    out[-1] = yrs[-1]
    return out


# ======================================================================== index setting

def index_standalone(data: ri.IndexData, end: str) -> tuple[dict, dict]:
    """Stand-alone continuous run of every grid point: kind -> {(a, b): daily return}, kind -> {(a, b): target}."""
    bars = data.bars["COMP"]
    rets, tgts = {k: {} for k in KINDS}, {k: {} for k in KINDS}
    for kind in KINDS:
        for a, b in gc.GRIDS[kind]():
            r = gc.make_rule(kind, a, b)
            buy, sell = r.fn(bars)
            tgt = ri.state_machine(buy, sell, bars["close"])
            sim = ri.index_sim(tgt, data, "COMP", INDEX_DATA_START, end)
            qt.assert_dev_dates(sim["ret"].index, end)
            rets[kind][(a, b)] = sim["ret"]
            tgts[kind][(a, b)] = tgt
    return rets, tgts


def stitch_index_target(tgts: dict, picks: dict, sessions: pd.DatetimeIndex) -> tuple[pd.Series, pd.Series]:
    """Stitched decision target (NaN before the first walk-forward year) and the parameter label used per session."""
    dy = decision_year(sessions)
    out = pd.Series(np.nan, index=sessions)
    lab = pd.Series("", index=sessions, dtype=object)
    for y, p in picks.items():
        m = dy == y
        out[m] = tgts[p["key"]].reindex(sessions)[m]
        lab[m] = f"{p['key'][0]}/{p['key'][1]}"
    return out, lab


def ret_metrics(r: pd.Series, b: pd.Series, rf: pd.Series) -> dict:
    """CAGR / max DD / monthly-excess t of a daily return series and its benchmark over the same days (rebased)."""
    r, b = r.dropna(), b.reindex(r.index).fillna(0.0)
    v, bv = (1 + r).cumprod(), (1 + b).cumprod()
    v0, bv0 = pd.concat([pd.Series([1.0]), v]), pd.concat([pd.Series([1.0]), bv])
    mx = qt.monthly(r) - qt.monthly(b)
    ex = r - rf.reindex(r.index).fillna(0.0)
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()),
            "cagr": qt.cagr_of(r), "bench_cagr": qt.cagr_of(b), "excess_cagr": qt.cagr_of(r) - qt.cagr_of(b),
            "max_dd": float((v0 / v0.cummax() - 1).min()), "bench_max_dd": float((bv0 / bv0.cummax() - 1).min()),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(252)) if ex.std() > 0 else float("nan"),
            "t_monthly_excess": float(mx.mean() / mx.std() * math.sqrt(len(mx))) if mx.std() > 0 else 0.0,
            "months": int(len(mx))}


def judge(full: dict, halves: list[dict]) -> dict:
    a = bool(full["cagr"] > full["bench_cagr"] and all(h["cagr"] > h["bench_cagr"] for h in halves)
             and full["t_monthly_excess"] >= 2)
    b = bool(abs(full["max_dd"]) <= abs(full["bench_max_dd"]) - 0.10 and full["cagr"] >= full["bench_cagr"] - 0.03)
    return {"A": a, "B": b, "pass": a or b}


def by_year(r: pd.Series, b: pd.Series) -> dict:
    ys = (1 + r).groupby(r.index.year).prod() - 1
    yb = (1 + b.reindex(r.index).fillna(0.0)).groupby(r.index.year).prod() - 1
    return {int(y): {"strategy": float(ys[y]), "oneq": float(yb[y]), "excess": float(ys[y] - yb[y])} for y in ys.index}


def run_index(verify_truncated: bool = True) -> tuple[list, list, dict]:
    data = ri.load_index_data(INDEX_END)
    for v in data.guard.values():
        if isinstance(v, dict):
            assert pd.Timestamp(v["last"]) <= pd.Timestamp(INDEX_END)
    rets, tgts = index_standalone(data, INDEX_END)
    picks = all_picks(rets, INDEX_WF_YEARS, data.rf)
    checked = []
    if verify_truncated:                       # re-pick every year from data loaded only up to Dec 31 of Y-1
        for y in INDEX_WF_YEARS:
            cut = f"{y - 1}-12-31"
            d2 = ri.load_index_data(cut)
            r2, _ = index_standalone(d2, cut)
            for key, by in picks.items():
                p2 = pick(r2[key[0]], y, key[1], key[2], d2.rf)
                if p2["key"] != by[y]["key"]:
                    raise AssertionError(f"truncated re-pick differs for {key} {y}: {p2['key']} vs {by[y]['key']}")
            checked.append(y)
        print(f"index: truncated re-pick identical for {len(checked)} years x {len(picks)} configurations")

    sess = data.r1["COMP"].index
    start = str(sess[sess < pd.Timestamp(f"{INDEX_WF_YEARS[0]}-01-01")][-1].date())   # last close of 2001 (cash)
    bench = ri.index_sim(pd.Series(1.0, index=sess), data, "COMP", start, INDEX_END)
    qqq_r = data.qqq_r.reindex(bench["ret"].index).fillna(0.0)
    rows, pick_rows, detail = [], [], {}
    for (kind, L, m), by in picks.items():
        tgt, lab = stitch_index_target(tgts[kind], by, sess)
        sim = ri.index_sim(tgt, data, "COMP", start, INDEX_END)
        qt.assert_dev_dates(sim["ret"].index, INDEX_END)
        assert sim["ret"].index[0] >= pd.Timestamp(f"{INDEX_WF_YEARS[0]}-01-01")
        r = sim["ret"]
        full = ret_metrics(r, bench["ret"], data.rf)
        mm = ri.index_metrics(sim, bench, data.rf)
        assert abs(mm["cagr"] - full["cagr"]) < 1e-9
        halves = [ret_metrics(r[(r.index >= a) & (r.index <= b)], bench["ret"], data.rf) for a, b in INDEX_HALVES]
        j = judge(full, halves)
        n_switch = sum(by[y]["key"] != by[y - 1]["key"] for y in list(INDEX_WF_YEARS)[1:])
        fixed = {k: ret_metrics(rr[rr.index > pd.Timestamp(start)], bench["ret"], data.rf)["cagr"]
                 for k, rr in rets[kind].items()}
        rank = int(sum(v > full["cagr"] for v in fixed.values()) + 1)
        owner = gc.OWNER_REF[kind]
        rows.append({"setting": "index", "kind": kind, "L": L, "metric": m,
                     "primary": L == PRIMARY["L"] and m == PRIMARY["metric"],
                     **{k: full[k] for k in ("start", "end", "cagr", "bench_cagr", "excess_cagr", "max_dd",
                                             "bench_max_dd", "sharpe", "t_monthly_excess", "months")},
                     "qqq_cagr": qt.cagr_of(qqq_r),
                     "h1_cagr": halves[0]["cagr"], "h1_bench": halves[0]["bench_cagr"],
                     "h1_t": halves[0]["t_monthly_excess"],
                     "h2_cagr": halves[1]["cagr"], "h2_bench": halves[1]["bench_cagr"],
                     "h2_t": halves[1]["t_monthly_excess"],
                     "switches_param": n_switch, "trades_per_year": mm["trades_per_year"],
                     "cost_drag_per_year": mm["cost_drag_per_year"],
                     "rank_among_fixed_points_cagr": rank, "n_points": len(fixed),
                     "fixed_best_cagr_hindsight": max(fixed.values()),
                     "fixed_median_cagr": float(np.median(list(fixed.values()))),
                     "owner_fixed_cagr": fixed[owner], **j})
        for y, p in by.items():
            pick_rows.append({"setting": "index", "kind": kind, "L": L, "metric": m, "year": y,
                              "params": f"{p['key'][0]}/{p['key'][1]}", "trailing_value": p["value"],
                              "window_first": p["window_first"], "window_last": p["window_last"]})
        detail[f"index_{kind}_L{L}_{m}"] = {"by_year": by_year(r, bench["ret"]),
                                            "halves": halves, "full": full, "judge": j}
    return rows, pick_rows, {"detail": detail, "truncated_recheck_years": checked}


# ======================================================================== stock setting

def stock_engine(data, sig: dict, sched: np.ndarray, elig: dict, rank_of: dict, start: str | None = None,
                 switch_targets: dict | None = None, record_at: set | None = None,
                 account: float = ACCOUNT, k: int = ri.K_STOCKS) -> dict:
    """ri.stock_sim generalised to a parameter schedule. ``sig``: key -> (B, S) boolean arrays on the simulation
    sessions x data.perf_idx columns; ``sched[i]``: the key whose signals drive the decision at close i.
    ``switch_targets``: session index -> set of security ids the new parameters would hold at that close; held names
    outside it are sold ("switch") and its names are bought first. ``record_at``: session dates at which the end-of-
    day holdings (after that day's fills, before decisions) are recorded. With a constant ``sched`` and no switch
    targets this is exactly ri.stock_sim (tested)."""
    sessions = data.sessions
    eff_end = pd.Timestamp(data.spec["effective_end"])
    first = pd.Timestamp(start or data.spec["perf_start"])
    perf = sessions[(sessions >= first) & (sessions <= eff_end)]
    lv.cs.assert_window(perf, str(first.date()), data.spec["effective_end"], "simulation sessions")
    assert len(sched) == len(perf)
    switch_targets = switch_targets or {}
    record_at = record_at or set()
    cols = list(data.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    lr = data.last_row.reindex(cols)
    lr_arr = lr.to_numpy()
    weeks = sorted(elig)
    wk_pos = np.searchsorted(np.array(weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1
    cash = account
    pos: dict = {}
    pend_sell, pend_buy = {}, []
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded, switch_cost = 0.0, 0.0
    counts = {"buy": 0, "sell": 0, "take_profit": 0, "delisted": 0, "switch_sell": 0, "switch_buy": 0}
    trades, holdings = [], {}
    switch_buys = set()

    def hs(sid, price, i):
        w = weeks[wk_pos[i]] if wk_pos[i] >= 0 else None
        return rev.half_spread(rank_of.get(w, {}).get(sid, np.nan), price)

    for i, d in enumerate(perf):
        day_cost = 0.0
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, col[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": "delisted",
                           "ret": val / p["cost_basis"] - 1, "days": i - p["entry_i"]})
            pend_sell.pop(sid, None)
        for sid, reason in sorted(pend_sell.items()):
            if sid not in pos:
                continue
            p = pos.pop(sid)
            c = col[sid]
            val = p["units"] * I[i, c]
            kc = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c], i))
            cash += val - kc
            day_cost += kc
            traded += val
            counts["sell"] += 1
            if reason == "switch":
                counts["switch_sell"] += 1
                switch_cost += kc
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": reason,
                           "ret": (val - kc) / p["cost_basis"] - 1, "days": i - p["entry_i"]})
        pend_sell = {}
        nav_now = cash + sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        for sid in pend_buy:
            if len(pos) >= k or sid in pos:
                continue
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[sid]) and lr[sid] < d):
                continue
            amt = min(nav_now / k, cash)
            if amt < 100:
                break
            kc = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c], i))
            pos[sid] = {"units": (amt - kc) / I[i, c], "entry_i": i, "entry_idx": I[i, c], "cost_basis": amt}
            cash -= amt
            day_cost += kc
            traded += amt
            counts["buy"] += 1
            if sid in switch_buys:
                counts["switch_buy"] += 1
                switch_cost += kc
        pend_buy, switch_buys = [], set()
        if d in record_at:
            holdings[d] = set(pos)
        if i < len(perf) - 1:
            B, S = sig[sched[i]]
            tgt = switch_targets.get(i)
            for sid, p in pos.items():
                c = col[sid]
                if tgt is not None and sid not in tgt:
                    pend_sell[sid] = "switch"          # the new parameters would not hold it
                    continue
                if p["entry_i"] == i:
                    continue
                if S[i, c]:
                    pend_sell[sid] = "signal"
            slots = k - (len(pos) - len(pend_sell))
            if slots > 0 and tgt is not None:
                w = weeks[wk_pos[i]] if wk_pos[i] >= 0 else None
                rk = rank_of.get(w, {})
                for sid in sorted(tgt, key=lambda s: (rk.get(s, np.inf), s)):
                    c = col.get(sid)
                    if c is None or sid in pos or S[i, c] or len(pend_buy) >= slots:
                        continue
                    if pd.notna(lr_arr[c]) and lr_arr[c] <= d:
                        continue
                    pend_buy.append(sid)
                    switch_buys.add(sid)
            if len(pend_buy) < slots and wk_pos[i] >= 0:
                for sid in elig[weeks[wk_pos[i]]]:
                    c = col.get(sid)
                    if c is None or sid in pos or sid in pend_buy or not B[i, c]:
                        continue
                    if pd.notna(lr_arr[c]) and lr_arr[c] <= d:
                        continue
                    pend_buy.append(sid)
                    if len(pend_buy) >= slots:
                        break
        stock_val = sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        navs[i] = cash + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "counts": counts, "switch_cost": switch_cost,
            "trades": pd.DataFrame(trades), "open": sorted(pos), "holdings": holdings}


def signal_arrays(rule: ri.Rule, bars: dict, perf: pd.DatetimeIndex, cols: list) -> tuple[np.ndarray, np.ndarray]:
    assert rule.take_profit is None
    buy, sell = ri.stock_signals(rule, bars)
    return (buy.reindex(index=perf, columns=cols).fillna(False).to_numpy(),
            sell.reindex(index=perf, columns=cols).fillna(False).to_numpy())


def perf_sessions(data, start: str | None = None) -> pd.DatetimeIndex:
    s = data.sessions
    first = pd.Timestamp(start or data.spec["perf_start"])
    return s[(s >= first) & (s <= pd.Timestamp(data.spec["effective_end"]))]


def switch_dates(data, years) -> list:
    """Last session of Y-1 for each walk-forward year Y (the switch decision close)."""
    s = perf_sessions(data)
    return [s[s < pd.Timestamp(f"{y}-01-01")][-1] for y in years]


def stock_standalone(data, runner: ri.StockRunner, years) -> tuple[dict, dict]:
    """kind -> {(a, b): daily NAV return from the 2012 start}, kind -> {(a, b): {switch date: holdings}}."""
    perf = perf_sessions(data)
    cols = list(data.perf_idx.columns)
    rec = set(switch_dates(data, years))
    rets, hold = {k: {} for k in KINDS}, {k: {} for k in KINDS}
    for kind in KINDS:
        for a, b in gc.GRIDS[kind]():
            r = gc.make_rule(kind, a, b)
            sig = {0: signal_arrays(r, runner.bars, perf, cols)}
            res = stock_engine(data, sig, np.zeros(len(perf), int), runner.elig, runner.rank_of, record_at=rec)
            lv.cs.assert_window(res["nav"].index, data.spec["perf_start"], data.spec["effective_end"], "nav")
            nav0 = pd.concat([pd.Series([ACCOUNT], index=[perf[0] - pd.Timedelta(days=1)]), res["nav"]])
            rets[kind][(a, b)] = nav0.pct_change().dropna()
            hold[kind][(a, b)] = res["holdings"]
        print(f"stock stand-alone {kind}: {len(rets[kind])} points")
    return rets, hold


def stitched_stock(data, runner, kind: str, picks: dict, hold: dict) -> dict:
    years = sorted(picks)
    sw = switch_dates(data, years)
    start = str(sw[0].date())
    perf = perf_sessions(data, start)
    cols = list(data.perf_idx.columns)
    keys = sorted({p["key"] for p in picks.values()})
    sig = {key: signal_arrays(gc.make_rule(kind, *key), runner.bars, perf, cols) for key in keys}
    dy = decision_year(perf)
    sched = np.empty(len(perf), dtype=object)          # filled one by one: the keys are (a, b) tuples
    for i, y in enumerate(dy):
        sched[i] = picks[y]["key"]
    pos_of = {d: i for i, d in enumerate(perf)}
    # realign only when the parameters change (and at the start, from cash); an unchanged pick is not a switch
    targets = {pos_of[d]: hold[picks[y]["key"]][d] for j, (y, d) in enumerate(zip(years, sw))
               if j == 0 or picks[y]["key"] != picks[years[j - 1]]["key"]}
    return stock_engine(data, sig, sched, runner.elig, runner.rank_of, start=start, switch_targets=targets)


def stock_metrics_wf(res: dict, runner) -> tuple[dict, list[dict], dict]:
    nav = res["nav"]
    oneq, qqq = runner.benchmarks(res["dates"], ACCOUNT)
    wf_start = f"{STOCK_WF_YEARS[0]}-01-01"
    full = ri.stock_sub_metrics(nav, oneq, qqq, wf_start, res["cost"], res["exposure"])
    halves = []
    for a, b in STOCK_HALVES:
        keep = nav.index <= pd.Timestamp(b)
        halves.append(ri.stock_sub_metrics(nav[keep], oneq[keep], qqq[keep], a, res["cost"][keep],
                                           res["exposure"][keep]))
    return full, halves, oneq


def run_stock(years=STOCK_WF_YEARS) -> tuple[list, list, dict]:
    lv.WINDOWS["wf"] = dict(STOCK_WINDOW)
    data = lv.load_window("wf")
    print(data.guard["assertion"])
    assert data.spec["effective_end"] <= "2026-08-31"
    runner = ri.StockRunner(data)
    rets, hold = stock_standalone(data, runner, years)
    picks = all_picks(rets, years, None)
    rows, pick_rows, detail = [], [], {}
    sw0 = switch_dates(data, years)[0]
    for (kind, L, m), by in picks.items():
        res = stitched_stock(data, runner, kind, by, hold[kind])
        full, halves, oneq = stock_metrics_wf(res, runner)
        fj = {"cagr": full["cagr"], "bench_cagr": full["bench_cagr"], "max_dd": full["max_dd"],
              "bench_max_dd": full["bench_max_dd"], "t_monthly_excess": full["t_excess_monthly"]}
        j = judge(fj, [{"cagr": h["cagr"], "bench_cagr": h["bench_cagr"]} for h in halves])
        n_switch = sum(by[y]["key"] != by[y - 1]["key"] for y in list(years)[1:])
        fixed = {}
        for key, rr in rets[kind].items():
            x = rr[rr.index > sw0]
            fixed[key] = float(np.prod(1 + x.values) ** (365.25 / (x.index[-1] - sw0).days) - 1)
        rank = int(sum(v > full["cagr"] for v in fixed.values()) + 1)
        yrs = (res["nav"].index[-1] - res["nav"].index[0]).days / 365.25
        rows.append({"setting": "stock", "kind": kind, "L": L, "metric": m,
                     "primary": L == PRIMARY["L"] and m == PRIMARY["metric"],
                     "start": full["start"], "end": full["end"], "cagr": full["cagr"],
                     "bench_cagr": full["bench_cagr"], "excess_cagr": full["excess_cagr"], "max_dd": full["max_dd"],
                     "bench_max_dd": full["bench_max_dd"], "sharpe": full["sharpe"],
                     "t_monthly_excess": full["t_excess_monthly"], "months": full["months"],
                     "qqq_cagr": full["qqq_cagr"],
                     "h1_cagr": halves[0]["cagr"], "h1_bench": halves[0]["bench_cagr"],
                     "h1_t": halves[0]["t_excess_monthly"],
                     "h2_cagr": halves[1]["cagr"], "h2_bench": halves[1]["bench_cagr"],
                     "h2_t": halves[1]["t_excess_monthly"],
                     "switches_param": n_switch, "buys": res["counts"]["buy"],
                     "switch_sells": res["counts"]["switch_sell"], "switch_buys": res["counts"]["switch_buy"],
                     "switch_cost_usd": res["switch_cost"], "cost_usd_total": float(res["cost"].sum()),
                     "trades_per_year": (res["counts"]["buy"] + res["counts"]["sell"]) / yrs,
                     "cost_drag_per_year": full["cost_drag_per_year"],
                     "rank_among_fixed_points_cagr": rank, "n_points": len(fixed),
                     "fixed_best_cagr_hindsight": max(fixed.values()),
                     "fixed_median_cagr": float(np.median(list(fixed.values()))),
                     "owner_fixed_cagr": fixed[gc.OWNER_REF[kind]], **j})
        for y, p in by.items():
            pick_rows.append({"setting": "stock", "kind": kind, "L": L, "metric": m, "year": y,
                              "params": f"{p['key'][0]}/{p['key'][1]}", "trailing_value": p["value"],
                              "window_first": p["window_first"], "window_last": p["window_last"]})
        detail[f"stock_{kind}_L{L}_{m}"] = {"by_year": {int(y): {**v} for y, v in full["by_year"].items()},
                                            "judge": j, "counts": res["counts"]}
        print(f"stock {kind} L={L} {m}: CAGR {full['cagr']:+.3f} vs ONEQ {full['bench_cagr']:+.3f}")
    return rows, pick_rows, {"detail": detail, "picks": picks, "guard": data.guard["assertion"]}


def verify_stock_truncated(picks: dict, years=STOCK_WF_YEARS) -> list:
    """Re-pick each year from a window loaded only up to Dec 31 of Y-1 (slow: one full grid per year)."""
    ok = []
    for y in years:
        name = f"wf_to_{y - 1}"
        lv.WINDOWS[name] = {**STOCK_WINDOW, "perf_end": f"{y - 1}-12-31"}
        d2 = lv.load_window(name)
        assert d2.spec["effective_end"] <= f"{y - 1}-12-31"
        r2, _ = stock_standalone(d2, ri.StockRunner(d2), [])
        for key, by in picks.items():
            p2 = pick(r2[key[0]], y, key[1], key[2], None)
            if p2["key"] != by[y]["key"]:
                raise AssertionError(f"stock truncated re-pick differs for {key} {y}: {p2['key']} vs {by[y]['key']}")
        ok.append(y)
        print(f"stock truncated re-pick {y}: identical")
    return ok


def check_engine_matches_reference() -> dict:
    """Stand-alone stock_engine == ri.stock_sim on the development window for the owner's 20/20 and 5/20."""
    data = lv.load_window("dev")
    runner = ri.StockRunner(data)
    perf = perf_sessions(data)
    out = {}
    for kind in KINDS:
        rule = gc.make_rule(kind, *gc.OWNER_REF[kind])
        _, ref, _, _ = runner.run(rule)
        sig = {0: signal_arrays(rule, runner.bars, perf, list(data.perf_idx.columns))}
        mine = stock_engine(data, sig, np.zeros(len(perf), int), runner.elig, runner.rank_of)
        diff = float((mine["nav"] - ref["nav"]).abs().max())
        assert diff < 1e-6, diff
        out[kind] = diff
    return out


# ======================================================================== main

def main(argv=None):
    p = argparse.ArgumentParser(description="walk-forward parameter re-selection (pre-registered, ledger section 9)")
    p.add_argument("--setting", choices=["index", "stock", "both"], default="both")
    p.add_argument("--no-index-verify", action="store_true")
    p.add_argument("--verify-stock-truncated", action="store_true")
    p.add_argument("--verify-stock-only", action="store_true",
                   help="re-pick every stock year from truncated windows and compare with picks_both.csv")
    a = p.parse_args(argv)
    OUT.mkdir(parents=True, exist_ok=True)
    if a.verify_stock_only:
        pk = pd.read_csv(OUT / "picks_both.csv")
        pk = pk[pk["setting"] == "stock"]
        picks = {}
        for (kind, L, m), g in pk.groupby(["kind", "L", "metric"]):
            picks[(kind, int(L), m)] = {int(r.year): {"key": tuple(int(x) for x in r.params.split("/"))}
                                        for r in g.itertuples()}
        ok = verify_stock_truncated(picks)
        (OUT / "stock_truncated_recheck.json").write_text(json.dumps(
            {"years_identical": ok, "configurations": len(picks)}, indent=2))
        return
    rows, prow, meta = [], [], {}
    if a.setting in ("index", "both"):
        r, pr, m = run_index(not a.no_index_verify)
        rows += r
        prow += pr
        meta["index"] = m
    if a.setting in ("stock", "both"):
        meta["engine_check"] = check_engine_matches_reference()
        r, pr, m = run_stock()
        rows += r
        prow += pr
        if a.verify_stock_truncated:
            m["truncated_recheck_years"] = verify_stock_truncated(m["picks"])
        m.pop("picks")
        meta["stock"] = m
    res = pd.DataFrame(rows)
    picks = pd.DataFrame(prow)
    tag = a.setting
    res.to_csv(OUT / f"results_{tag}.csv", index=False, float_format="%.6f")
    picks.to_csv(OUT / f"picks_{tag}.csv", index=False, float_format="%.6f")
    (OUT / f"detail_{tag}.json").write_text(json.dumps(meta, indent=1, default=str))
    (OUT / "README.json").write_text(json.dumps({
        "status": "pre-registered in docs/research_ledger_indicators.md section 9; weak test (years seen before, "
                  "grid post-hoc)",
        "primary": PRIMARY, "lookbacks": LOOKBACKS, "metrics": METRICS,
        "index": {"walk_forward": [f"{INDEX_WF_YEARS[0]}-01", INDEX_END], "halves": INDEX_HALVES},
        "stock": {"window": STOCK_WINDOW, "walk_forward": [f"{STOCK_WF_YEARS[0]}-01", "2026-08-31"],
                  "halves": STOCK_HALVES},
        "bonferroni_t_4_primary_one_sided_5pct": BONFERRONI_4}, indent=2))
    pd.set_option("display.width", 250)
    cols = ["setting", "kind", "L", "metric", "primary", "cagr", "bench_cagr", "qqq_cagr", "max_dd", "bench_max_dd",
            "t_monthly_excess", "h1_cagr", "h1_bench", "h2_cagr", "h2_bench", "switches_param",
            "rank_among_fixed_points_cagr", "A", "B", "pass"]
    print(res[cols].to_string(float_format=lambda v: f"{v:+.3f}"))
    prim = picks.merge(res.loc[res["primary"], ["setting", "kind", "L", "metric"]])
    print(prim.pivot_table(index="year", columns=["setting", "kind"], values="params", aggfunc="first").to_string())


if __name__ == "__main__":
    main()
