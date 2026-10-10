"""Walk-forward parameter re-selection on the MA / Donchian grids of quant.strategies.indicators: yearly picks from
past data only, the stand-alone runs of every grid point and the stitched runs (index timing and stock engine).

Moved unchanged from studies/walk_forward.py (pre-registered in docs/research_ledger_indicators.md section 9); the
module-level settings of the study (kinds, look-backs, metrics, account) are arguments.

Picking: every January 1 of year Y, the grid point with the best ``score`` (daily Sharpe over the risk-free rate, or
CAGR) over the trailing L years of returns dated before Y (``trailing_window`` raises on any leak); ties keep the
first grid point. Stitching: the picked points drive one continuous simulation; the decision year of a session is
quant.data.calendar.decision_year (the last close of Y-1 already uses Y's parameters). In the stock engine a change
of parameters realigns the portfolio to what the new parameters would hold at the switch close ("switch" trades).
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.backtest.costs import rank_half_spread
from quant.data import guards, panel
from quant.data.calendar import decision_year
from quant.strategies import canslim as cs
from quant.strategies import indicators as ri
from quant.strategies import livermore as lv
from quant.strategies.indicators import GRIDS, OWNER_REF, make_rule

FutureDataError = ri.FutureDataError


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


def all_picks(rets_by_kind: dict, years, rf, lookbacks, metrics) -> dict:
    """(kind, L, metric) -> {year: pick dict}."""
    out = {}
    for kind, rets in rets_by_kind.items():
        for L in lookbacks:
            for m in metrics:
                out[(kind, L, m)] = {y: pick(rets, y, L, m, rf) for y in years}
    return out


# ======================================================================== index setting

def index_standalone(data: ri.IndexData, start: str, end: str, kinds) -> tuple[dict, dict]:
    """Stand-alone continuous run of every grid point: kind -> {(a, b): daily return}, kind -> {(a, b): target}."""
    bars = data.bars["COMP"]
    rets, tgts = {k: {} for k in kinds}, {k: {} for k in kinds}
    for kind in kinds:
        for a, b in GRIDS[kind]():
            r = make_rule(kind, a, b)
            buy, sell = r.fn(bars)
            tgt = ri.state_machine(buy, sell, bars["close"])
            sim = ri.index_sim(tgt, data, "COMP", start, end)
            guards.assert_dev_dates(sim["ret"].index, end)
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


# ======================================================================== stock setting

def stock_engine(data, sig: dict, sched: np.ndarray, elig: dict, rank_of: dict, start: str | None = None,
                 switch_targets: dict | None = None, record_at: set | None = None,
                 account: float = 10_000.0, k: int = ri.K_STOCKS) -> dict:
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
    cs.assert_window(perf, str(first.date()), data.spec["effective_end"], "simulation sessions")
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
        return rank_half_spread(rank_of.get(w, {}).get(sid, np.nan), price)

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


def stock_standalone(data, runner: ri.StockRunner, years, kinds, account: float = 10_000.0) -> tuple[dict, dict]:
    """kind -> {(a, b): daily NAV return from the window start}, kind -> {(a, b): {switch date: holdings}}."""
    perf = perf_sessions(data)
    cols = list(data.perf_idx.columns)
    rec = set(switch_dates(data, years))
    rets, hold = {k: {} for k in kinds}, {k: {} for k in kinds}
    for kind in kinds:
        for a, b in GRIDS[kind]():
            r = make_rule(kind, a, b)
            sig = {0: signal_arrays(r, runner.bars, perf, cols)}
            res = stock_engine(data, sig, np.zeros(len(perf), int), runner.elig, runner.rank_of, record_at=rec)
            cs.assert_window(res["nav"].index, data.spec["perf_start"], data.spec["effective_end"], "nav")
            nav0 = pd.concat([pd.Series([account], index=[perf[0] - pd.Timedelta(days=1)]), res["nav"]])
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
    sig = {key: signal_arrays(make_rule(kind, *key), runner.bars, perf, cols) for key in keys}
    dy = decision_year(perf)
    sched = np.empty(len(perf), dtype=object)          # filled one by one: the keys are (a, b) tuples
    for i, y in enumerate(dy):
        sched[i] = picks[y]["key"]
    pos_of = {d: i for i, d in enumerate(perf)}
    # realign only when the parameters change (and at the start, from cash); an unchanged pick is not a switch
    targets = {pos_of[d]: hold[picks[y]["key"]][d] for j, (y, d) in enumerate(zip(years, sw))
               if j == 0 or picks[y]["key"] != picks[years[j - 1]]["key"]}
    return stock_engine(data, sig, sched, runner.elig, runner.rank_of, start=start, switch_targets=targets)


# ======================================================================== checks (re-pick from truncated data, engine)

def repick_index_truncated(picks: dict, years, raw, start: str, kinds) -> list:
    """Re-pick every year from index data loaded only up to Dec 31 of Y-1; raises if any pick differs."""
    checked = []
    for y in years:
        cut = f"{y - 1}-12-31"
        d2 = ri.load_index_data(cut, raw)
        r2, _ = index_standalone(d2, start, cut, kinds)
        for key, by in picks.items():
            p2 = pick(r2[key[0]], y, key[1], key[2], d2.rf)
            if p2["key"] != by[y]["key"]:
                raise AssertionError(f"truncated re-pick differs for {key} {y}: {p2['key']} vs {by[y]['key']}")
        checked.append(y)
    print(f"index: truncated re-pick identical for {len(checked)} years x {len(picks)} configurations")
    return checked


def repick_stock_truncated(picks: dict, years, window: dict, raw, kinds, account: float = 10_000.0) -> list:
    """Re-pick each year from a stock window (``window`` with perf_end = Dec 31 of Y-1) loaded only up to that day
    (slow: one full grid per year); raises if any pick differs."""
    ok = []
    for y in years:
        name = f"wf_to_{y - 1}"
        d2 = panel.load_window(name, {**window, "perf_end": f"{y - 1}-12-31"})
        assert d2.spec["effective_end"] <= f"{y - 1}-12-31"
        r2, _ = stock_standalone(d2, ri.StockRunner(d2, raw), [], kinds, account)
        for key, by in picks.items():
            p2 = pick(r2[key[0]], y, key[1], key[2], None)
            if p2["key"] != by[y]["key"]:
                raise AssertionError(f"stock truncated re-pick differs for {key} {y}: {p2['key']} vs {by[y]['key']}")
        ok.append(y)
        print(f"stock truncated re-pick {y}: identical")
    return ok


def check_engine_matches_reference(raw, kinds) -> dict:
    """Stand-alone stock_engine == ri.stock_sim on the development window for the owner's 20/20 and 5/20."""
    data = lv.load_window("dev")
    runner = ri.StockRunner(data, raw)
    perf = perf_sessions(data)
    out = {}
    for kind in kinds:
        rule = make_rule(kind, *OWNER_REF[kind])
        _, ref, _, _ = runner.run(rule)
        sig = {0: signal_arrays(rule, runner.bars, perf, list(data.perf_idx.columns))}
        mine = stock_engine(data, sig, np.zeros(len(perf), int), runner.elig, runner.rank_of)
        diff = float((mine["nav"] - ref["nav"]).abs().max())
        assert diff < 1e-6, diff
        out[kind] = diff
    return out
