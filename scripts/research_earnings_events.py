"""Large-cap earnings-event strategies on the frozen data version 1 (pre-registered in
docs/research_ledger_earnings_events.md, section 0, before any run).

Rules (top 250 by dv50 rank, at most 10 names, idle money in ONEQ, $10,000 IBKR Pro Tiered, whole shares, MOC fills):
- E1 announcement premium (Frazzini-Lamont 2007; Barber et al. 2013). The expected date is last year's actual
  announcement (8-K Item 2.02 D0) + 364 days. Buy at the close of expected day -5 (variant: -2), sell at the close of
  expected day +1, whatever the actual date turns out to be.
- E2 earnings-announcement-return drift (Brandt et al. 2008). EAR = stock total return D0-2 close .. D0+1 close minus
  ONEQ. Signal known at S = max(D0+1, the session of the 8-K acceptance); buy at S+1 if EAR >= the 90th percentile of
  the past 365 days of universe EARs; hold 60 sessions; 10 fixed slots (variant: monthly overlapping cohorts of 10).
- E3 = E2 slots, only names above their 200-day SMA at S.

Two halves: H1 2012-01-01..2018-12-31 (judged 2014-2017; 2012, 2013 and 2018 break the 2% rule), H2 2019-01-01..
2026-08-31. Fold 1: develop on H1 (pick primary vs variant by net IR), test on H2; fold 2 the other way round.
Pass (per fold, on the test half): A = CAGR > ONEQ and monthly excess t >= 2; B = max drawdown >= 10 pp shallower and
CAGR >= ONEQ - 3 pp. A rule passes only if both folds pass.

HARD DATE GUARD: each half is read through ``research_livermore.load_window`` (every price / return / universe /
terminal frame truncated to [warm-up start, half end] and asserted); earnings events are kept only when their 8-K
acceptance session is <= the half end; every signal uses events whose acceptance session is <= the signal session.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_canslim_dev as cs  # noqa: E402  (order cost, QQQ benchmark, date guard)
from scripts import research_livermore as lv  # noqa: E402  (window loader, universe, metrics, trade stats)
from scripts import research_indicators as ind  # noqa: E402  (ONEQ on sessions, metrics vs ONEQ, criteria)
from scripts import research_reversal_dev as rev  # noqa: E402  (half spread, D5 terminal values)

from scripts import study_data_version as dv  # noqa: E402  (REVERSAL_DATA_VERSION; docs/robustness_data_v2.md)
OUT = dv.versioned(ROOT / "output/research_only/earnings_events")
INPUTS = cs.INPUTS
ACCOUNT = 10_000.0
N_UNIVERSE = 250
BONFERRONI_T = 2.39   # two-sided 5% Bonferroni for 3 rules, reported only

HALVES = {
    "H1": {"perf_start": "2012-01-01", "perf_end": "2018-12-31", "price_start": "2011-06-01",
           "universe_start": "2012-01-01", "judged_from": "2014-01-01", "judged_to": "2017-12-31"},
    "H2": {"perf_start": "2019-01-01", "perf_end": "2026-08-31", "price_start": "2017-10-01",
           "universe_start": "2017-10-01", "judged_from": "2019-01-01", "judged_to": "2026-08-31"},
}
FOLDS = {"fold1": {"dev": "H1", "test": "H2"}, "fold2": {"dev": "H2", "test": "H1"}}
for _k, _v in HALVES.items():
    lv.WINDOWS[f"ee_{_k}"] = {k: v for k, v in _v.items() if k != "judged_to"}

RULES = {"E1": ("E1_m5p1", "E1_m2p1"), "E2": ("E2_slot10", "E2_cohort"), "E3": ("E3_slot10_sma200",)}


@dataclass(frozen=True)
class Config:
    name: str
    kind: str                 # 'premium' | 'drift_slot' | 'drift_cohort'
    lead: int = 5             # E1: buy at expected day -lead
    lag: int = 1              # E1: sell at expected day +lag
    hold: int = 60            # E2/E3: sessions held
    k: int = 10               # maximum positions
    weight: float = 0.10      # target value per name as a share of total assets
    sma200: bool = False
    spread_mult: float = 1.0


CONFIGS = {
    "E1_m5p1": Config("E1_m5p1", "premium", lead=5, lag=1),
    "E1_m2p1": Config("E1_m2p1", "premium", lead=2, lag=1),
    "E2_slot10": Config("E2_slot10", "drift_slot"),
    "E2_cohort": Config("E2_cohort", "drift_cohort", k=30, weight=1 / 30),
    "E3_slot10_sma200": Config("E3_slot10_sma200", "drift_slot", sma200=True),
}


# ======================================================================== earnings events (point in time)

def load_events(end: str, path: Path = INPUTS / "earnings_events.csv") -> pd.DataFrame:
    """First results release of each fiscal quarter (8-K Item 2.02), acceptance session <= ``end``."""
    e = pd.read_csv(path, dtype=str, usecols=["cik", "security_id", "event_kind", "first_in_fiscal_quarter",
                                              "d0_session", "d0_session_acceptance", "late_furnished"])
    e = e[(e["event_kind"] == "results_release") & (e["first_in_fiscal_quarter"] == "Y")].copy()
    n0 = len(e)
    e = e[e["d0_session_acceptance"] <= end]
    cs.assert_window(e["d0_session_acceptance"], None, end, "earnings acceptance sessions")
    e["d0"] = pd.to_datetime(e["d0_session"])
    e["acc"] = pd.to_datetime(e["d0_session_acceptance"])
    e.attrs["dropped_after_end"] = int(n0 - len(e))
    return e[["cik", "security_id", "d0", "acc", "late_furnished"]].reset_index(drop=True)


def attach_securities(events: pd.DataFrame, universe: pd.DataFrame, successor: dict) -> pd.DataFrame:
    """Events are filed by issuer (CIK); attach them to every security of that issuer in the universe (share classes).
    Events of a 1:1 predecessor (e.g. Google -> Alphabet) also count for the successor's issuer."""
    u = universe[["security_id", "cik"]].dropna().drop_duplicates()
    cik_of = dict(zip(u["security_id"], u["cik"]))
    cik_map = {}
    for pred, succ in successor.items():
        a, b = cik_of.get(pred), cik_of.get(succ)
        if a and b and a != b:
            cik_map[a] = b
    ev = events.copy()
    extra = ev[ev["cik"].isin(cik_map)].assign(cik=lambda x: x["cik"].map(cik_map))
    ev = pd.concat([ev, extra], ignore_index=True).drop_duplicates(["cik", "d0"])
    out = ev.drop(columns=["security_id"]).merge(u, on="cik", how="inner")
    return out.sort_values(["cik", "d0", "security_id"]).reset_index(drop=True)


def session_index(sessions: pd.DatetimeIndex, dates, side: str = "left") -> np.ndarray:
    """Index of the first session on or after each date (len(sessions) when there is none)."""
    return np.searchsorted(sessions.values, pd.to_datetime(pd.Series(dates)).values, side=side)


# ======================================================================== membership

class Membership:
    """Top-250 membership: a session uses the latest universe week on or before it."""

    def __init__(self, u: pd.DataFrame, sessions: pd.DatetimeIndex, n: int = N_UNIVERSE):
        u = u[u["dv50_rank"] <= n]
        self.weeks = np.array(sorted(u["week_end"].unique()), dtype="datetime64[ns]")
        self.rank = {pd.Timestamp(t): dict(zip(g["security_id"], g["dv50_rank"])) for t, g in u.groupby("week_end")}
        self.cik = dict(zip(u["security_id"], u["cik"]))
        self.ticker = u.drop_duplicates("security_id", keep="last").set_index("security_id")["ticker"].to_dict()
        self.sessions = sessions
        self._wk = np.searchsorted(self.weeks, sessions.values, side="right") - 1

    def at(self, i: int) -> dict:
        w = self._wk[i] if 0 <= i < len(self._wk) else -1
        return self.rank.get(pd.Timestamp(self.weeks[w]), {}) if w >= 0 else {}


# ======================================================================== E1 candidates

def expected_dates(sessions: pd.DatetimeIndex, d0: pd.Series, days: int = 364) -> np.ndarray:
    """Session index of the expected announcement: last year's D0 + 364 days, rolled to the next session."""
    return session_index(sessions, pd.to_datetime(d0) + pd.Timedelta(days=days))


def premium_candidates(ev: pd.DataFrame, sessions: pd.DatetimeIndex, memb: Membership, lead: int, lag: int,
                       first_i: int, last_i: int) -> dict:
    """{entry session index: [(security_id, exit index, priority, expected index)]} for E1.

    A base event e (last year) is a candidate at t = expected - lead when, using only events whose acceptance
    session is <= t: the issuer has no event with D0 >= D0(e) + 300 days (this year's report not out yet), it had an
    event within the last 200 days, and the security is in the top 250 at t."""
    acc_i = session_index(sessions, ev["acc"])
    exp_i = expected_dates(sessions, ev["d0"])
    by_cik = {}
    for j, (c, d) in enumerate(zip(ev["cik"], ev["d0"])):
        by_cik.setdefault(c, []).append((d, acc_i[j]))
    out: dict = {}
    seen = set()
    for j, r in enumerate(ev.itertuples(index=False)):
        t = exp_i[j] - lead
        if t < first_i or t > last_i or exp_i[j] >= len(sessions):
            continue
        key = (r.cik, t)
        if key in seen:
            continue
        rank = memb.at(t).get(r.security_id)
        if rank is None:
            continue
        td = sessions[t]
        known = [d for d, a in by_cik[r.cik] if a <= t]
        if any(d >= r.d0 + pd.Timedelta(days=300) for d in known):
            continue
        if not any(d >= td - pd.Timedelta(days=200) for d in known):
            continue
        seen.add(key)
        out.setdefault(t, []).append((r.security_id, exp_i[j] + lag, float(rank), int(exp_i[j])))
    for t in out:
        out[t] = dedupe_by_issuer(sorted(out[t], key=lambda x: (x[2], x[0])), memb)
    return out


def dedupe_by_issuer(cands: list, memb: Membership) -> list:
    """Keep the first (best-priority) share class of each issuer."""
    seen, keep = set(), []
    for c in cands:
        cik = memb.cik.get(c[0], c[0])
        if cik in seen:
            continue
        seen.add(cik)
        keep.append(c)
    return keep


# ======================================================================== E2 / E3 signals

def ear_table(ev: pd.DataFrame, sessions: pd.DatetimeIndex, sig_idx: pd.DataFrame, oneq: pd.Series,
              memb: Membership, close_adj: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per universe event: EAR (D0-2 close .. D0+1 close, stock minus ONEQ), signal session S and SMA flag."""
    d0_i = session_index(sessions, ev["d0"])
    acc_i = session_index(sessions, ev["acc"])
    exact = d0_i < len(sessions)
    exact[exact] = sessions[d0_i[exact]] == pd.DatetimeIndex(ev["d0"][exact])
    cols = {c: k for k, c in enumerate(sig_idx.columns)}
    S = sig_idx.to_numpy()
    Q = oneq.reindex(sessions).to_numpy()
    sma_ok = None
    if close_adj is not None:
        sma = close_adj.rolling(200, min_periods=200).mean()
        sma_ok = (close_adj > sma).to_numpy()
    rows = []
    for j, r in enumerate(ev.itertuples(index=False)):
        i = d0_i[j]
        if not exact[j] or i < 2 or i + 1 >= len(sessions) or r.security_id not in cols:
            continue
        rank = memb.at(i).get(r.security_id)
        if rank is None:
            continue
        c = cols[r.security_id]
        a, b = S[i - 2, c], S[i + 1, c]
        qa, qb = Q[i - 2], Q[i + 1]
        if not (np.isfinite(a) and np.isfinite(b) and np.isfinite(qa) and np.isfinite(qb)) or a <= 0:
            continue
        s_i = int(max(i + 1, acc_i[j]))
        rows.append({"security_id": r.security_id, "cik": r.cik, "d0_i": int(i), "s_i": s_i,
                     "ear": (b / a - 1) - (qb / qa - 1), "rank": float(rank),
                     "above_sma200": bool(sma_ok[s_i, c]) if sma_ok is not None and s_i < len(sessions) else False,
                     "late_furnished": r.late_furnished == "Y"})
    t = pd.DataFrame(rows)
    if len(t):
        t["s_date"] = sessions[np.minimum(t["s_i"].to_numpy(), len(sessions) - 1)]
        t = t[t["s_i"] < len(sessions)]
        # one row per issuer and event (share classes: keep the best-ranked class)
        t = t.sort_values(["cik", "d0_i", "rank", "security_id"]).drop_duplicates(["cik", "d0_i"])
        t = t.sort_values(["s_i", "ear", "security_id"], ascending=[True, False, True]).reset_index(drop=True)
    return t


def trailing_threshold(t: pd.DataFrame, q: float = 0.90, window_days: int = 365, min_n: int = 100) -> pd.Series:
    """Per event: the q-quantile of the EARs whose signal session is strictly earlier and within ``window_days``."""
    dates = t["s_date"].to_numpy()
    ear = t["ear"].to_numpy()
    out = np.full(len(t), np.nan)
    cache = {}
    for j in range(len(t)):
        d = dates[j]
        if d in cache:
            out[j] = cache[d]
            continue
        hi = np.searchsorted(dates, d, side="left")
        lo = np.searchsorted(dates, d - np.timedelta64(window_days, "D"), side="left")
        v = np.quantile(ear[lo:hi], q) if hi - lo >= min_n else np.nan
        cache[d] = v
        out[j] = v
    return pd.Series(out, index=t.index)


def drift_slot_candidates(t: pd.DataFrame, cfg: Config, memb: Membership, first_i: int, last_i: int) -> dict:
    sel = t[(t["ear"] >= t["thr"]) & t["thr"].notna()]
    if cfg.sma200:
        sel = sel[sel["above_sma200"]]
    out: dict = {}
    for r in sel.itertuples(index=False):
        e = r.s_i + 1
        if e < first_i or e > last_i:
            continue
        out.setdefault(e, []).append((r.security_id, e + cfg.hold, -r.ear, r.d0_i))
    for e in out:
        out[e] = dedupe_by_issuer(sorted(out[e], key=lambda x: (x[2], x[0])), memb)
    return out


def drift_cohort_candidates(t: pd.DataFrame, cfg: Config, sessions: pd.DatetimeIndex, memb: Membership,
                            first_i: int, last_i: int, n: int = 10) -> dict:
    """Last session of each month: the top ``n`` EARs (>= threshold) with S in that month; buy the next session."""
    months = sessions.to_period("M")
    month_end = [i for i in range(len(sessions) - 1) if months[i] != months[i + 1]]
    sel = t[(t["ear"] >= t["thr"]) & t["thr"].notna()].copy()
    sel["month"] = sessions[sel["s_i"].to_numpy()].to_period("M")
    out: dict = {}
    for m in month_end:
        e = m + 1
        if e < first_i or e > last_i:
            continue
        g = sel[(sel["month"] == months[m]) & (sel["s_i"] <= m)].sort_values(["ear", "security_id"],
                                                                              ascending=[False, True])
        c = [(r.security_id, e + cfg.hold, -r.ear, r.d0_i) for r in g.itertuples(index=False)]
        c = dedupe_by_issuer(c, memb)
        if c:
            out[e] = c[: 3 * n]       # spares in case of held names / missing prices; at most n are bought
    return out


# ======================================================================== simulation

def simulate(plan: dict, cfg: Config, sessions: pd.DatetimeIndex, perf: np.ndarray, I: np.ndarray, P: np.ndarray,
             cols: dict, last_row: pd.Series, oneq_lvl: np.ndarray, oneq_px: np.ndarray, memb: Membership,
             per_entry_cap: int | None = None) -> dict:
    """Daily engine. ``plan``: {entry session index: [(sid, exit index, priority, info)]} in priority order.
    ``perf``: the global session indices of the performance window. Whole shares bought at the raw close; positions
    valued with the total-return index. Idle money in ONEQ (whole shares)."""
    cash = ACCOUNT
    q_units = 0.0                       # ONEQ held, in total-return-index units (shares * px / lvl)
    q_shares = 0
    pos: dict = {}
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    trades, orders = [], []
    counts = {"buy": 0, "sell": 0, "delisted": 0, "skip_full": 0, "skip_no_price": 0, "skip_lt_1_share": 0,
              "skip_held": 0, "skip_cash": 0, "oneq_orders": 0}
    lr = {s: last_row.get(s) for s in cols}
    weeks = sessions.to_period("W-FRI")
    hs_q = ind.HALF_SPREAD["COMP"] * cfg.spread_mult

    def hs(sid, price, i):
        return rev.half_spread(memb.at(i).get(sid, np.nan), price, cfg.spread_mult)

    def oneq_sell(n, i):
        nonlocal cash, q_units, q_shares, traded
        n = min(n, q_shares)
        if n <= 0:
            return 0.0
        val = q_units * oneq_lvl[i] * n / q_shares
        k = cs.order_cost(val, oneq_px[i], True, hs_q)
        q_units -= val / oneq_lvl[i]
        q_shares -= n
        cash += val - k
        traded += val
        counts["oneq_orders"] += 1
        return k

    def oneq_buy(i):
        nonlocal cash, q_units, q_shares, traded
        n = int((cash * 0.995) // oneq_px[i])
        if n <= 0:
            return 0.0
        val = n * oneq_px[i]
        k = cs.order_cost(val, oneq_px[i], False, hs_q)
        if val + k > cash:
            n -= 1
            val = n * oneq_px[i]
            k = cs.order_cost(val, oneq_px[i], False, hs_q)
            if n <= 0:
                return 0.0
        cash -= val + k
        q_units += val / oneq_lvl[i]
        q_shares += n
        traded += val
        counts["oneq_orders"] += 1
        return k

    for n_, i in enumerate(perf):
        day_cost = 0.0
        if n_ == 0:
            day_cost += oneq_buy(i)
        # delisted positions: value (terminal return included) goes to cash, no order
        for sid in [x for x in pos if pd.notna(lr[x]) and sessions[i] > lr[x]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, cols[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"security_id": sid, "entry": sessions[p["entry_i"]].date(), "exit": sessions[i].date(),
                           "reason": "delisted", "ret": val / p["cost_basis"] - 1, "days": int(i - p["entry_i"]),
                           "tranches": 1})
        # 1. scheduled sells
        for sid in sorted([x for x, p in pos.items() if p["exit_i"] <= i]):
            p = pos.pop(sid)
            c = cols[sid]
            val = p["units"] * I[i, c]
            k = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c], i))
            cash += val - k
            day_cost += k
            traded += val
            counts["sell"] += 1
            trades.append({"security_id": sid, "entry": sessions[p["entry_i"]].date(), "exit": sessions[i].date(),
                           "reason": "scheduled", "ret": (val - k) / p["cost_basis"] - 1, "days": int(i - p["entry_i"]),
                           "tranches": 1})
            orders.append({"date": sessions[i].date(), "security_id": sid, "side": "sell", "value": round(val, 2)})
        # 2. buys
        stock_val = sum(p["units"] * I[i, cols[x]] for x, p in pos.items())
        nav_now = cash + stock_val + q_units * oneq_lvl[i]
        bought_here = 0
        for sid, exit_i, _prio, info in plan.get(i, []):
            if per_entry_cap is not None and bought_here >= per_entry_cap:
                break
            if len(pos) >= cfg.k:
                counts["skip_full"] += 1
                continue
            if sid in pos:
                counts["skip_held"] += 1
                continue
            c = cols.get(sid)
            if c is None or not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[sid]) and lr[sid] < sessions[i]) \
                    or pd.isna(lr[sid]):
                counts["skip_no_price"] += 1
                continue
            shares = int((nav_now * cfg.weight) // P[i, c])
            if shares < 1:
                counts["skip_lt_1_share"] += 1
                continue
            val = shares * P[i, c]
            k = cs.order_cost(val, P[i, c], False, hs(sid, P[i, c], i))
            if val + k > cash:
                need = val + k - cash
                q_val_share = q_units * oneq_lvl[i] / q_shares if q_shares else np.inf
                n_sell = int(math.ceil(need * 1.01 / q_val_share)) if q_shares else 0
                if n_sell > q_shares or not q_shares:
                    counts["skip_cash"] += 1
                    continue
                day_cost += oneq_sell(n_sell, i)
                if val + k > cash:
                    counts["skip_cash"] += 1
                    continue
            cash -= val + k
            day_cost += k
            traded += val
            pos[sid] = {"units": val / I[i, c], "entry_i": int(i), "exit_i": int(exit_i), "cost_basis": val + k,
                        "info": info}
            counts["buy"] += 1
            bought_here += 1
            orders.append({"date": sessions[i].date(), "security_id": sid, "side": "buy", "value": round(val, 2)})
        # 3. weekly sweep of idle cash into ONEQ
        last_of_week = i + 1 >= len(sessions) or weeks[i] != weeks[i + 1]
        stock_val = sum(p["units"] * I[i, cols[x]] for x, p in pos.items())
        nav_now = cash + stock_val + q_units * oneq_lvl[i]
        if last_of_week and cash > 0.02 * nav_now:
            day_cost += oneq_buy(i)
        navs[n_] = cash + stock_val + q_units * oneq_lvl[i]
        expo[n_] = stock_val / navs[n_] if navs[n_] > 0 else 0.0
        costs[n_] = day_cost
    idx = sessions[perf]
    return {"dates": idx, "nav": pd.Series(navs, index=idx), "exposure": pd.Series(expo, index=idx),
            "cost": pd.Series(costs, index=idx), "traded": traded, "counts": counts,
            "trades": pd.DataFrame(trades), "orders": pd.DataFrame(orders), "open": sorted(pos)}


# ======================================================================== metrics

def window_metrics(res: dict, oneq: pd.Series, qqq: pd.Series, start: str, end: str) -> dict:
    """Metrics on [start, end], every curve rebased to $10,000 at the last close before ``start``."""
    nav = res["nav"]
    before = nav.index[nav.index < pd.Timestamp(start)]
    keep = (nav.index <= pd.Timestamp(end)) & ((nav.index > before[-1]) if len(before) else True)
    if len(before):
        t0 = before[-1]
        f = lambda s: s[keep] / s.loc[t0] * ACCOUNT  # noqa: E731
    else:
        f = lambda s: s[keep]  # noqa: E731
    tr = res["trades"]
    m = ind.stock_metrics(f(nav), f(oneq), f(qqq), ACCOUNT, res["cost"][keep], 0.0, res["exposure"][keep])
    if len(tr):
        ent = pd.to_datetime(tr["entry"])
        sub = tr[(ent >= pd.Timestamp(start)) & (ent <= pd.Timestamp(end))]
    else:
        sub = tr
    m.update(lv.trade_stats(sub) if len(sub) else {"closed_trades": 0})
    o = res["orders"]
    if len(o):
        od = pd.to_datetime(o["date"])
        m["stock_orders"] = int(((od >= pd.Timestamp(start)) & (od <= pd.Timestamp(end))).sum())
    else:
        m["stock_orders"] = 0
    crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
    m["crit_A"], m["crit_B"], m["crit_pass"] = crit["A"], crit["B"], crit["pass"]
    m["t_ge_bonferroni"] = bool(m["t_excess_monthly"] >= BONFERRONI_T)
    return m


def event_diagnostics(plan_all: dict, sessions, I_sig, cols, oneq_lvl, actual: dict | None = None) -> dict:
    """Frictionless event-level averages of (stock - ONEQ) from entry close to exit close, all candidates."""
    rows = []
    for e, cands in plan_all.items():
        for sid, x, _p, info in cands:
            c = cols.get(sid)
            if c is None or x >= len(sessions):
                continue
            a, b = I_sig[e, c], I_sig[x, c]
            if not (np.isfinite(a) and np.isfinite(b)) or a <= 0:
                continue
            r = (b / a - 1) - (oneq_lvl[x] / oneq_lvl[e] - 1)
            row = {"entry": sessions[e], "ret": r}
            if actual is not None:
                row["hit"] = any(e < d <= x for d in actual.get(sid, ()))
            rows.append(row)
    d = pd.DataFrame(rows)
    if d.empty:
        return {"n": 0}
    mon = d.groupby(d["entry"].dt.to_period("M"))["ret"].mean()
    out = {"n": int(len(d)), "mean": float(d["ret"].mean()), "median": float(d["ret"].median()),
           "t_monthly_means": float(mon.mean() / mon.std(ddof=1) * math.sqrt(len(mon))) if len(mon) > 2 else np.nan,
           "months": int(len(mon))}
    if "hit" in d:
        out["hit_rate_actual_in_window"] = float(d["hit"].mean())
    return out


# ======================================================================== runner

class Half:
    def __init__(self, name: str, terminal_awaiting: float = rev.TERMINAL_D5):
        self.name = name
        spec = HALVES[name]
        self.spec = spec
        self.win = lv.load_window(f"ee_{name}", terminal_awaiting)
        w = self.win
        end = w.spec["effective_end"]
        self.end = end
        self.sessions = w.sessions
        self.lvl, self.px = ind.oneq_on_sessions(w.sessions, spec["price_start"], end)
        cs.assert_window(self.lvl.dropna().index, spec["price_start"], end, "ONEQ")
        term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
        succ = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
        u = lv.eligible_universe(w)
        u = u.merge(w.universe[["week_end", "security_id", "cik"]].drop_duplicates(["week_end", "security_id"]),
                    on=["week_end", "security_id"], how="left")
        u["cik"] = u.groupby("security_id")["cik"].transform(lambda s: s.ffill().bfill())
        self.memb = Membership(u, self.sessions)
        ev = load_events(end)
        self.events_dropped_after_end = ev.attrs["dropped_after_end"]
        self.ev = attach_securities(ev, u, succ)
        self.guard = dict(w.guard)
        self.guard["earnings_events"] = {"kept": int(len(self.ev)), "dropped_acceptance_after_end": self.events_dropped_after_end,
                                         "max_acceptance": str(self.ev["acc"].max().date())}
        self.guard["assertion"] += f"; ONEQ in [{spec['price_start']}, {end}]; earnings acceptance <= {end}"
        perf_first = int(np.searchsorted(self.sessions.values, np.datetime64(spec["perf_start"]), side="left"))
        last = int(np.searchsorted(self.sessions.values, np.datetime64(end), side="right") - 1)
        self.perf = np.arange(perf_first, last + 1)
        cs.assert_window(self.sessions[self.perf], spec["perf_start"], end, "simulation sessions")
        self.cols = {c: k for k, c in enumerate(w.perf_idx.columns)}
        self.I = w.perf_idx.to_numpy()
        self.P = w.close.reindex(columns=w.perf_idx.columns).to_numpy()
        self.Isig = w.sig_idx.reindex(columns=w.perf_idx.columns).to_numpy()
        self.L, self.X = self.lvl.to_numpy(), self.px.to_numpy()
        self.ear = ear_table(self.ev, self.sessions, w.sig_idx, self.lvl, self.memb, w.close_adj)
        self.ear["thr"] = trailing_threshold(self.ear)
        cs.assert_window(self.sessions[self.ear["s_i"].to_numpy()], None, end, "EAR signal sessions")
        self.actual = {}
        d0_i = session_index(self.sessions, self.ev["d0"])
        for sid, i in zip(self.ev["security_id"], d0_i):
            self.actual.setdefault(sid, []).append(int(i))
        q = self.lvl.iloc[self.perf]
        kc = cs.order_cost(ACCOUNT, float(self.px.iloc[self.perf[0]]), False, ind.HALF_SPREAD["COMP"])
        self.oneq = (ACCOUNT - kc) * q / q.iloc[0]
        self.qqq = cs.qqq_benchmark(w.qqq_perf_idx, w.qqq_close, self.sessions[self.perf], ACCOUNT)

    def plan(self, cfg: Config) -> dict:
        f, l_ = int(self.perf[0]), int(self.perf[-1])
        if cfg.kind == "premium":
            return premium_candidates(self.ev, self.sessions, self.memb, cfg.lead, cfg.lag, f, l_)
        if cfg.kind == "drift_slot":
            return drift_slot_candidates(self.ear, cfg, self.memb, f, l_)
        return drift_cohort_candidates(self.ear, cfg, self.sessions, self.memb, f, l_)

    def run(self, cfg: Config) -> tuple[dict, dict, dict]:
        plan = self.plan(cfg)
        cap = 10 if cfg.kind == "drift_cohort" else None
        res = simulate(plan, cfg, self.sessions, self.perf, self.I, self.P, self.cols, self.win.last_row,
                       self.L, self.X, self.memb, per_entry_cap=cap)
        full = window_metrics(res, self.oneq, self.qqq, self.spec["perf_start"], self.end)
        judged = window_metrics(res, self.oneq, self.qqq, self.spec["judged_from"], min(self.spec["judged_to"], self.end))
        for m in (full, judged):
            m.update({f"n_{k}": v for k, v in res["counts"].items()})
        return full, judged, {"res": res, "plan": plan}


# ======================================================================== main

def _f(v):
    if isinstance(v, (np.floating, np.integer)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, np.bool_):
        return bool(v)
    return v


def _clean(m: dict) -> dict:
    return {k: _f(v) for k, v in m.items() if not k.startswith("_") and k != "by_year"}


def run() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    for sub in ("daily_nav", "trades"):
        (OUT / sub).mkdir(exist_ok=True)
    results, by_year, judged_all, diags, guards = [], [], {}, {}, {}
    for hname in ("H1", "H2"):
        h = Half(hname)
        hs_ = Half(hname, terminal_awaiting=rev.TERMINAL_D5_STRESS)
        guards[hname] = h.guard
        print(h.guard["assertion"], flush=True)
        print(f"  {hname}: events {len(h.ev)}, EAR rows {len(h.ear)}, with threshold {int(h.ear['thr'].notna().sum())}",
              flush=True)
        for name, cfg in CONFIGS.items():
            full, judged, extra = h.run(cfg)
            f2, j2, _ = h.run(replace(cfg, spread_mult=2.0))
            f3, j3, _ = hs_.run(cfg)
            judged_all[(hname, name)] = judged
            res = extra["res"]
            tick = h.memb.ticker
            tr = res["trades"]
            if len(tr):
                tr.assign(ticker=tr["security_id"].map(tick)).to_csv(OUT / "trades" / f"{hname}_{name}.csv", index=False)
            res["nav"].to_frame("nav").assign(oneq=h.oneq, qqq=h.qqq, exposure=res["exposure"],
                                              cost=res["cost"]).to_csv(OUT / "daily_nav" / f"{hname}_{name}.csv")
            for scope, m, m2, m3 in (("full", full, f2, f3), ("judged", judged, j2, j3)):
                results.append({"half": hname, "config": name, "scope": scope, **asdict(cfg), **_clean(m),
                                "excess_cagr_spread2x": m2["excess_cagr"], "pass_spread2x": m2["crit_pass"],
                                "excess_cagr_terminal_minus100": m3["excess_cagr"],
                                "pass_terminal_minus100": m3["crit_pass"]})
            for y, v in full["by_year"].items():
                ent = pd.to_datetime(tr["entry"]).dt.year if len(tr) else pd.Series(dtype=int)
                by_year.append({"half": hname, "config": name, "year": y, **v,
                                "judged_year": HALVES[hname]["judged_from"][:4] <= str(y) <= HALVES[hname]["judged_to"][:4],
                                "trades_entered": int((ent == y).sum())})
            # frictionless event-level diagnostics
            if cfg.kind == "premium":
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L, h.actual)
            elif cfg.kind == "drift_slot" and not cfg.sma200:
                allp = {}
                for r in h.ear.itertuples(index=False):
                    e = r.s_i + 1
                    if h.perf[0] <= e <= h.perf[-1]:
                        allp.setdefault(e, []).append((r.security_id, e + 60, 0, 0))
                diags[(hname, "E2_all_events")] = event_diagnostics(allp, h.sessions, h.Isig, h.cols, h.L)
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L)
            elif cfg.sma200:
                diags[(hname, name)] = event_diagnostics(extra["plan"], h.sessions, h.Isig, h.cols, h.L)
            print(f"[{hname}] {name}: judged CAGR {judged['cagr']:+.1%} ONEQ {judged['bench_cagr']:+.1%} "
                  f"t_m {judged['t_excess_monthly']:.2f} IR {judged['ir']:.2f} DD {judged['max_dd']:.0%} "
                  f"(ONEQ {judged['bench_max_dd']:.0%}) trades {judged.get('closed_trades', 0)} "
                  f"pass {judged['crit_pass']} | full ex {full['excess_cagr']:+.1%}", flush=True)
        del h, hs_

    # fold selection and verdicts
    folds = {}
    for fold, fd in FOLDS.items():
        picks = {}
        for rule, names in RULES.items():
            if len(names) == 1:
                pick = names[0]
                note = "no variant"
            else:
                irs = {n: judged_all[(fd["dev"], n)]["ir"] for n in names}
                pick = names[1] if irs[names[1]] > irs[names[0]] else names[0]
                note = {n: round(float(v), 3) for n, v in irs.items()}
            tm = judged_all[(fd["test"], pick)]
            picks[rule] = {"picked": pick, "dev_ir": note, "test_half": fd["test"],
                           "test": {k: _f(tm[k]) for k in ("cagr", "bench_cagr", "excess_cagr", "t_excess_monthly", "ir",
                                                           "max_dd", "bench_max_dd", "crit_A", "crit_B", "crit_pass",
                                                           "t_ge_bonferroni", "closed_trades")}}
        folds[fold] = picks
    verdict = {}
    for rule in RULES:
        p1, p2 = folds["fold1"][rule]["test"]["crit_pass"], folds["fold2"][rule]["test"]["crit_pass"]
        verdict[rule] = "pass" if (p1 and p2) else ("unstable (one fold)" if (p1 or p2) else "fail")
    pd.DataFrame(results).to_csv(OUT / "results.csv", index=False)
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", index=False)
    summary = {"date_guard": guards, "configs": {k: asdict(v) for k, v in CONFIGS.items()}, "folds": folds,
               "verdict": verdict,
               "event_diagnostics": {f"{a}:{b}": v for (a, b), v in diags.items()},
               "bonferroni_t_reported_only": BONFERRONI_T}
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps({"folds": folds, "verdict": verdict}, indent=1, default=str))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.parse_args(argv)
    run()


if __name__ == "__main__":
    main()
