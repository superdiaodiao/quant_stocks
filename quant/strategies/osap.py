"""Long-only profitability predictors picked after the OSAP screen (docs/research_ledger_osap.md 2.1): point-in-time
SEC fundamentals for GP (gross profit / assets) and OperProfRD ((operating income + D&A + R&D) / assets), the monthly
top-300 universe, the execution schedule and the buffered top-N daily engine with IBKR Pro Tiered costs.

Moved unchanged from studies/osap_backtest.py (``extract_annual_facts``, ``fy_values``, ``fundamental_states``,
``build_states``, ``asof_states``, ``monthly_universe``, ``schedule``, ``ranked_picks``, ``simulate``,
``benchmarks``, ``passes`` and the period loading / evaluation of ``run``). The study's frozen numbers (account,
names held, keep rank, rank cut, fact age) and the states cache file are parameters.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_cost, rank_half_spread
from quant.data import eps as pit_eps
from quant.data import panel
from quant.data.panel import TERMINAL_D5
from quant.strategies import canslim as cs  # (companyfacts paths, date guard, QQQ benchmark)
from quant.strategies import indicators as ind
from quant.strategies import livermore as lv

DateGuardError = cs.DateGuardError

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
    p = pit_eps._cf_path(cik)
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


def build_states(ciks, cache: Path | None) -> pd.DataFrame:
    """``fundamental_states`` of every CIK, read from / written to ``cache`` (None: no cache)."""
    if cache is not None and cache.is_file():
        return pd.read_csv(cache, dtype={"filed": str, "gp_fy_end": str, "opr_fy_end": str})
    parts = [fundamental_states(extract_annual_facts(int(c))) for c in sorted(set(int(x) for x in ciks))]
    out = pd.concat([p for p in parts if len(p)], ignore_index=True)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        out.to_csv(cache, index=False)
    return out


def asof_states(rows: pd.DataFrame, states: pd.DataFrame, end: str, max_age_days: int) -> pd.DataFrame:
    """For each (signal date t, cik) row, the latest state filed strictly before t; facts filed after ``end`` are
    dropped first (date guard) and the result is asserted point in time. A signal whose fiscal year ended more than
    ``max_age_days`` before t is NaN."""
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
        m.loc[~(age <= max_age_days), c] = np.nan
    return m


# ======================================================================== universe and schedule

def monthly_universe(win: lv.WinData, dv_rank_max: int) -> pd.DataFrame:
    """Rows (t, security_id, ticker, cik, dv50_rank) for each month's last universe Friday; weeks after the last
    universe file week reuse that week's membership (flagged ``carried``)."""
    uni = win.universe
    sic = pd.to_numeric(uni["sic"], errors="coerce")
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & uni["dv50_rank"].notna() & \
        (uni["dv50_rank"] <= dv_rank_max) & ~sic.between(6000, 6999) & uni["security_id"].isin(win.close.columns)
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

def simulate(win: lv.WinData, sched: dict, picks: dict, account: float, spread_mult: float = 1.0, *,
             n_hold: int, keep_rank: int) -> dict:
    """Hold the top ``n_hold`` (equal weight at entry), keep a holding while it stays in the top ``keep_rank``, fill
    empty slots best-first at each scheduled session; delisted names leave at their terminal value."""
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
                k = ibkr_order_cost(val / P[i, c], P[i, c], sell=True,
                                   hs=rank_half_spread(rank_now.get(sid, np.nan), P[i, c], spread_mult))["total"]
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
                k = ibkr_order_cost(amt / P[i, c], P[i, c], sell=False, hs=rank_half_spread(rank, P[i, c], spread_mult))["total"]
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


# ======================================================================== benchmarks, criteria, a period's run

def benchmarks(win: lv.WinData, dates: pd.DatetimeIndex, account: float):
    """ONEQ and QQQ buy-and-hold values of ``account`` on ``dates`` (IBKR entry cost, total return)."""
    ps, end = win.spec["price_start"], win.spec["effective_end"]
    lvl, px = ind.oneq_on_sessions(win.sessions, ps, end)
    cs.assert_window(lvl.dropna().index, ps, end, "ONEQ")
    q = lvl.reindex(dates)
    kc = cs.order_cost(account, float(px.reindex(dates).iloc[0]), False, ind.HALF_SPREAD["COMP"])
    oneq = (account - kc) * q / q.iloc[0]
    qqq = cs.qqq_benchmark(win.qqq_perf_idx, win.qqq_close, dates, account)
    return oneq, qqq


def passes(m: dict) -> bool:
    """Pass in one period: net CAGR above ONEQ's and monthly-excess t >= 2."""
    return bool(m["cagr"] > m["bench_cagr"] and m["t_excess_monthly"] >= 2.0)


def run_period(name: str, spec: dict, states_all: pd.DataFrame, dv_rank_max: int, max_age_days: int,
               terminal: float = TERMINAL_D5) -> dict:
    """Load the panel window ``osap_<name>`` (``spec``), the monthly universe with its point-in-time signals and the
    execution schedule."""
    win = panel.load_window(f"osap_{name}", dict(spec), terminal)
    end = win.spec["effective_end"]
    rows = monthly_universe(win, dv_rank_max)
    rows = asof_states(rows, states_all, end, max_age_days)
    perf_start = pd.Timestamp(win.guard["perf_start_session"])
    sched = schedule(rows["t"], win.sessions, perf_start, pd.Timestamp(end))
    out = {"guard": win.guard, "rows": rows, "sched": sched, "win": win}
    return out


def evaluate(win, sched, rows, signal: str, account: float, n_hold: int, keep_rank: int,
             spread_mult: float = 1.0) -> tuple[dict, dict, pd.Series, pd.Series]:
    """Simulate ``signal`` and compute its metrics against ONEQ / QQQ (strategies.indicators.stock_metrics)."""
    res = simulate(win, sched, ranked_picks(rows, signal), account, spread_mult=spread_mult, n_hold=n_hold,
                   keep_rank=keep_rank)
    oneq, qqq = benchmarks(win, res["dates"], account)
    m = ind.stock_metrics(res["nav"], oneq, qqq, account, res["cost"], res["traded"], res["exposure"])
    m.update({"n_buy": res["n_buy"], "n_sell": res["n_sell"]})
    return m, res, oneq, qqq
