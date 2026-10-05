"""Systematic Jesse Livermore-style trend strategy: development grid (2017-2022) and frozen one-shot tests.

Rules (each switchable in ``Config``):

- Leading groups: each Friday, eligible names (Nasdaq common stocks, 50-day median dollar volume rank <= 300, raw
  close >= $10, a close on the Friday) are grouped by Fama-French 49 industry. A group's strength is the median
  total return of its members over ``rs_lb`` sessions (groups with fewer than ``min_members`` members are skipped);
  the top ``n_groups`` groups lead.
- Leading stocks: members of a leading group whose relative strength (same return) is in the top ``rs_top`` of the
  whole eligible universe (or, with ``per_group``, the best ``per_group`` names of each leading group).
- Pivotal point (entry signal at a session's close, on split-adjusted closes):
  ``high252`` close above the highest close of the prior 252 sessions (a new 52-week closing high);
  ``baseL_tT`` close above the highest close of the prior L sessions while those L closes stayed inside a range of
  at most T (max / min - 1 <= T), i.e. a breakout from a tight base. Optional volume confirmation: the session's
  volume >= ``vol_mult`` x the average of the prior 50 sessions.
- Pyramiding: the first purchase is ``tranches[0]`` of a full position (full = NAV / K at the first entry); tranche
  j (j >= 1) is added only after the close is at least j x ``add_step`` above the first entry price (never on a
  loss, never averaging down).
- Losses are cut: close at or below (1 - ``stop``) x first entry price (total-return basis).
- Winners run until the trailing exit: ``smaN`` close below its N-session average; ``lowN`` close below the lowest
  close of the prior N sessions; ``pctX`` close X% or more below the highest close since entry.
- Market: ``sma200`` / ``sma50`` QQQ close above its 200 / 50-session average; ``m_exit`` sells everything when the
  market turns off (otherwise it only blocks new entries); ``none`` no filter.
- At most K names. Idle money: ``cash`` (no interest: IBKR pays none on the first $10,000) or ``qqq_on`` (QQQ while
  the market filter is on, cash while it is off).

Timing: every decision uses closes up to session d and is executed at the close of session d+1 (market-on-close
orders a person can place by hand). Costs: IBKR Pro Tiered for the actual order size from
``scripts/research_reversal_dev.py`` (commission, pass-through, SEC/FINRA on sells, half-spread by dv rank).

HARD DATE GUARD: ``load_window(name)`` truncates every price / return / universe frame to the window's
[price_start, perf_end] and asserts it. The development grid only ever loads ``dev`` (2017-01-01 .. 2022-12-31;
prices from 2015-10 are used for signals only). The one-shot windows can only be run with ``--oneshot`` and the frozen
rule file written before the run; each runs once (it refuses when its output already exists).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_canslim_dev as cs  # noqa: E402  (date guard helpers, QQQ benchmark, order cost)
from scripts import research_reversal_dev as rev  # noqa: E402  (cost model, index, deflated Sharpe)
from scripts import stop_rules  # noqa: E402  (optional stop-loss overrides; research_ledger_stops.md)

NORM = NormalDist()
INPUTS = cs.INPUTS
CACHE = cs.CACHE
OUT = ROOT / "output/research_only/livermore"
LEDGER = ROOT / "docs/research_ledger_livermore.md"
FROZEN = OUT / "frozen_rule.json"
DateGuardError = cs.DateGuardError

WINDOWS = {
    "dev": {"perf_start": "2017-01-01", "perf_end": "2022-12-31", "price_start": "2015-10-01",
            "universe_start": "2016-01-01", "judged_from": "2017-01-01"},
    "test1": {"perf_start": "2023-01-01", "perf_end": "2026-09-30", "price_start": "2021-10-01",
              "universe_start": "2022-01-01", "judged_from": "2023-01-01"},
    "test2": {"perf_start": "2012-01-01", "perf_end": "2016-12-31", "price_start": "2011-06-01",
              "universe_start": "2012-01-01", "judged_from": "2014-01-01"},   # 2012-2013: >2% slots missing
}


# ======================================================================== the single loader

@dataclass
class WinData:
    name: str
    spec: dict
    sessions: pd.DatetimeIndex
    universe: pd.DataFrame
    sig_idx: pd.DataFrame
    perf_idx: pd.DataFrame
    close: pd.DataFrame
    close_adj: pd.DataFrame
    vol_adj: pd.DataFrame
    last_row: pd.Series
    qqq_close: pd.Series
    qqq_perf_idx: pd.Series
    terminal_events: pd.DataFrame
    guard: dict


def load_window(name: str, terminal_awaiting: float = rev.TERMINAL_D5) -> WinData:
    """Load one window. Every frame is cut to [price_start, end] and asserted; ``end`` is the window end or the last
    session with stock prices, whichever is earlier."""
    spec = dict(WINDOWS[name])
    ps, end = spec["price_start"], spec["perf_end"]
    guard = {"window": name, **spec, "frames": {}}

    def note(label, frame, column, start, stop):
        out = cs.truncate(frame, column, start, stop)
        guard["frames"][label] = {"rows_kept": int(len(out)),
                                  "rows_dropped_after_end": int((frame[column].astype(str) > stop).sum()),
                                  "rows_dropped_before_start": int((frame[column].astype(str) < start).sum()),
                                  "min_date": str(out[column].min()), "max_date": str(out[column].max())}
        return out

    panel = pd.read_csv(CACHE / "prices/daily_panel.csv.gz", dtype={"security_id": str, "date": str},
                        usecols=["security_id", "date", "close_raw", "volume_raw", "split_factor", "tr"])
    panel = note("daily_panel", panel, "date", ps, end)
    end = min(end, str(panel["date"].max()))           # test1: stock prices stop at 2026-08-31
    spec["effective_end"] = end
    guard["effective_end"] = end
    uni = pd.read_csv(INPUTS / "weekly_universe_top300.csv.gz", dtype=str)
    uni = note("weekly_universe_top300", uni, "week_end", spec["universe_start"], end)
    uni["dv50_rank"] = pd.to_numeric(uni["dv50_rank"], errors="coerce")
    uni["week_end"] = pd.to_datetime(uni["week_end"])
    qqq = pd.read_csv(CACHE / "factors/qqq_joined.csv", dtype={"date": str})
    qqq = note("qqq_joined", qqq, "date", ps, end).sort_values("date")
    term = pd.read_csv(INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    term = term[term["last_price_date"].notna()]
    term = note("terminal_returns (by last_price_date)", term, "last_price_date", ps, end)

    ids = set(uni["security_id"])
    successor = {r.security_id: r.continued_as for r in term.itertuples() if isinstance(r.continued_as, str)}
    ids |= {successor[s] for s in list(ids) if s in successor}
    panel = panel[panel["security_id"].isin(ids)].copy()
    sessions = pd.DatetimeIndex(pd.to_datetime(qqq["date"]))
    qqq_close = pd.Series(qqq["close"].to_numpy(float), index=sessions)
    qqq_tr = pd.Series(((qqq["close"] + qqq["dividend"].fillna(0)) / qqq["close"].shift(1) - 1).to_numpy(),
                       index=sessions)
    panel["date"] = pd.to_datetime(panel["date"])
    piv = {c: panel.pivot(index="date", columns="security_id", values=c).reindex(sessions)
           for c in ("tr", "close_raw", "volume_raw", "split_factor")}
    tr, close, vol, split = piv["tr"], piv["close_raw"], piv["volume_raw"], piv["split_factor"].fillna(1.0)
    has_row = close.notna()
    last_row = has_row[::-1].idxmax()
    last_row[~has_row.any()] = pd.NaT
    for pred, succ in successor.items():
        if pred not in tr.columns or succ not in tr.columns or pd.isna(last_row.get(pred)):
            continue
        after = tr.index > last_row[pred]
        for fr in (tr, close, vol, split):
            fr.loc[after, pred] = fr.loc[after, succ]
        last_row[pred] = last_row[succ]

    events = []
    last_session = sessions[-1]
    term_by_id = {r.security_id: r for r in term.itertuples()}
    for sid in tr.columns:
        lr = last_row[sid]
        if pd.isna(lr) or lr >= last_session:
            continue
        nxt = sessions[sessions > lr][0]
        r = term_by_id.get(sid)
        status = r.status if r is not None else "no_terminal_record"
        value = float(r.terminal_return) if status == "computed" else (terminal_awaiting if status == "awaiting_d5"
                                                                        else 0.0)
        tr.loc[nxt, sid] = value
        events.append({"security_id": sid, "last_row": lr.date().isoformat(), "booked_on": nxt.date().isoformat(),
                       "status": status, "terminal_return": value})
    events = pd.DataFrame(events)

    cum_split = split.cumprod()
    close_adj = close * cum_split
    vol_adj = vol / cum_split
    sig_idx = rev.make_index(tr, close)
    perf_start = sessions[sessions >= pd.Timestamp(spec["perf_start"])][0]
    tr_perf = tr.copy()
    tr_perf.loc[tr_perf.index <= perf_start] = np.nan
    started = close.notna().mul(np.asarray(close.index >= perf_start), axis=0).cumsum() > 0
    perf_idx = (1 + tr_perf.fillna(0.0)).cumprod().where(started)
    qtr = qqq_tr.copy()
    qtr[qtr.index <= perf_start] = np.nan
    qqq_perf_idx = (1 + qtr.fillna(0.0)).cumprod()
    cs.assert_window(tr_perf.dropna(how="all").index, perf_start.strftime("%Y-%m-%d"), end, "performance returns")
    for label, fr in (("tr", tr), ("close", close), ("volume", vol), ("qqq", qqq_close)):
        cs.assert_window(fr.index, ps, end, label)
    guard["perf_start_session"] = str(perf_start.date())
    guard["max_session_loaded"] = str(sessions.max().date())
    guard["assertion"] = (f"PASS [{name}]: every price/return frame lies in [{ps}, {end}]; performance returns are "
                          f"masked up to {perf_start.date()} (first return used: the session after it)")
    return WinData(name=name, spec=spec, sessions=sessions, universe=uni, sig_idx=sig_idx, perf_idx=perf_idx,
                   close=close.ffill(), close_adj=close_adj, vol_adj=vol_adj, last_row=last_row, qqq_close=qqq_close,
                   qqq_perf_idx=qqq_perf_idx, terminal_events=events, guard=guard)


# ======================================================================== signals

def parse_pivot(pivot: str) -> tuple[int, float | None]:
    """'high252' -> (252, None); 'base50_t20' -> (50, 0.20)."""
    if pivot.startswith("high"):
        return int(pivot[4:]), None
    a, b = pivot[4:].split("_t")
    return int(a), int(b) / 100


def breakout_frame(close_adj: pd.DataFrame, vol_adj: pd.DataFrame, pivot: str, vol_mult: float | None) -> pd.DataFrame:
    """True at session d when d's close breaks above the pivotal point built from closes strictly before d."""
    n, tight = parse_pivot(pivot)
    prior = close_adj.shift(1)
    hi = prior.rolling(n, min_periods=n if tight is not None else max(n - 12, 1)).max()
    sig = close_adj > hi
    if tight is not None:
        lo = prior.rolling(n, min_periods=n).min()
        sig &= (hi / lo - 1) <= tight
    if vol_mult is not None:
        avg = vol_adj.shift(1).rolling(50, min_periods=40).mean()
        sig &= vol_adj >= vol_mult * avg
    return sig.fillna(False)


def trail_frame(close_adj: pd.DataFrame, trail: str) -> pd.DataFrame | None:
    """Exit flag at session d for the moving-average / N-day-low trails (pct trails are handled in the engine)."""
    if trail.startswith("sma"):
        n = int(trail[3:])
        return (close_adj < close_adj.rolling(n, min_periods=n).mean()).fillna(False)
    if trail.startswith("low"):
        n = int(trail[3:])
        return (close_adj < close_adj.shift(1).rolling(n, min_periods=n).min()).fillna(False)
    return None


def market_state(qqq_close: pd.Series, rule: str) -> pd.Series:
    if rule == "none":
        return pd.Series(True, index=qqq_close.index)
    n = int(rule[3:])
    return qqq_close > qqq_close.rolling(n, min_periods=n).mean()


def eligible_universe(data: WinData) -> pd.DataFrame:
    """Weekly eligible rows. Weeks after the last universe file week (test1: after 2026-07-17) reuse that week's
    membership (flagged); relative strength is always measured at the actual Friday."""
    uni = data.universe
    ok = (uni["price_ge_10"] == "Y") & (uni["close_on_week_end"] == "Y") & uni["dv50_rank"].notna() & \
        (uni["dv50_rank"] <= 300) & uni["ff49"].notna() & uni["security_id"].isin(data.close_adj.columns)
    u = uni.loc[ok, ["week_end", "security_id", "ticker", "ff49", "ff49_name", "dv50_rank"]].copy()
    last_week = u["week_end"].max()
    fridays = pd.date_range(last_week + pd.Timedelta(days=7), pd.Timestamp(data.spec["effective_end"]), freq="W-FRI")
    fridays = [f for f in fridays if (data.sessions <= f).any() and data.sessions[data.sessions <= f][-1] > last_week]
    extra = []
    base = u[u["week_end"] == last_week]
    for f in fridays:
        extra.append(base.assign(week_end=f, carried=True))
    u["carried"] = False
    if extra:
        u = pd.concat([u] + extra, ignore_index=True)
    cs.assert_window(u["week_end"], data.spec["universe_start"], data.spec["effective_end"], "universe weeks")
    return u


def weekly_leaders(u: pd.DataFrame, data: WinData, rs_lb: int, n_groups: int | None, rs_top: float | None,
                   per_group: int | None, min_members: int = 3, n_universe: int = 300) -> pd.DataFrame:
    """Rows (week_end, security_id, rs, rs_pct, group_rank) of the leading stocks of the leading groups."""
    g = u[u["dv50_rank"] <= n_universe].copy()
    sess = data.sessions
    pos = sess.searchsorted(g["week_end"], side="right") - 1
    g = g[pos >= 0].copy()
    pos = pos[pos >= 0]
    g["session"] = sess[pos]
    cols = {s: i for i, s in enumerate(data.sig_idx.columns)}
    ci = g["security_id"].map(cols).to_numpy()
    rs_fr = (data.sig_idx / data.sig_idx.shift(rs_lb) - 1).to_numpy()
    g["rs"] = rs_fr[pos, ci]
    g = g[np.isfinite(g["rs"])].copy()
    g["rs_pct"] = g.groupby("week_end")["rs"].rank(pct=True)
    grp = g.groupby(["week_end", "ff49"]).agg(score=("rs", "median"), n=("rs", "size")).reset_index()
    grp = grp[grp["n"] >= min_members].copy()
    grp["group_rank"] = grp.groupby("week_end")["score"].rank(ascending=False, method="first")
    g = g.merge(grp[["week_end", "ff49", "group_rank"]], on=["week_end", "ff49"], how="left")
    keep = pd.Series(True, index=g.index)
    if n_groups is not None:
        keep &= g["group_rank"] <= n_groups
    if per_group is not None:
        r = g[keep].groupby(["week_end", "ff49"])["rs"].rank(ascending=False, method="first")
        keep &= g.index.isin(r[r <= per_group].index)
    if rs_top is not None:
        keep &= g["rs_pct"] > 1 - rs_top + 1e-9
    out = g[keep].sort_values(["week_end", "rs"], ascending=[True, False])
    return out[["week_end", "session", "security_id", "ticker", "ff49_name", "rs", "rs_pct", "group_rank", "carried"]]


# ======================================================================== config

@dataclass(frozen=True)
class Config:
    n_universe: int = 300
    rs_lb: int = 126
    n_groups: int | None = 8
    rs_top: float | None = 0.20
    per_group: int | None = None
    pivot: str = "high252"            # high252 | baseL_tT
    vol_mult: float | None = None
    k: int = 5
    tranches: tuple = (0.5, 0.5)
    add_step: float = 0.05
    stop: float | None = 0.08
    trail: str = "sma50"              # smaN | lowN | pctX
    market: str = "sma200"            # sma200 | sma50 | none
    m_exit: bool = True
    idle: str = "cash"                # cash | qqq_on
    spread_mult: float = 1.0
    account: float = 10_000.0

    @property
    def name(self) -> str:
        f = lambda x: "off" if x is None else f"{int(round(x * 100))}"  # noqa: E731
        tr = "full" if len(self.tranches) == 1 else f"{len(self.tranches)}x{f(self.add_step)}"
        return "_".join([f"U{self.n_universe}", f"RS{self.rs_lb}", f"G{self.n_groups or 'all'}",
                         f"top{f(self.rs_top)}" + (f"pg{self.per_group}" if self.per_group else ""),
                         self.pivot, f"V{self.vol_mult or 'off'}", f"K{self.k}", f"pyr{tr}", f"stop{f(self.stop)}",
                         self.trail, f"M{self.market}{'x' if self.m_exit else 'e'}", f"idle{self.idle}"])


RULE_FIELDS = [f for f in Config.__dataclass_fields__ if f not in ("spread_mult", "account")]


# ======================================================================== engine

def simulate(cfg: Config, data: WinData, leaders: pd.DataFrame, brk: pd.DataFrame, trail_fr: pd.DataFrame | None,
             m_on: pd.Series, rank_of: dict, account: float | None = None, stops=None,
             sigma: pd.DataFrame | None = None, start: str | None = None) -> dict:
    """Daily simulation. Decisions at the close of session d, orders filled at the close of session d+1.

    Optional (defaults keep the original behaviour): ``stops`` replaces ``cfg.stop`` with a stop_rules.StopSpec (or one
    per simulated session); ``sigma`` is the 20-day return stdev frame for vol stops (computed from ``data.sig_idx``
    when needed); ``start`` starts the simulation (from cash) at a later session than ``perf_start``."""
    account = cfg.account if account is None else account
    sessions = data.sessions
    eff_end = pd.Timestamp(data.spec["effective_end"])
    first = pd.Timestamp(start or data.spec["perf_start"])
    assert first >= pd.Timestamp(data.spec["perf_start"])
    perf = sessions[(sessions >= first) & (sessions <= eff_end)]
    cs.assert_window(perf, data.spec["perf_start"], data.spec["effective_end"], "simulation sessions")
    cols = list(data.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    B = brk.reindex(index=perf, columns=cols).to_numpy()
    T = trail_fr.reindex(index=perf, columns=cols).to_numpy() if trail_fr is not None else None
    Q = data.qqq_perf_idx.reindex(perf).to_numpy()
    QP = data.qqq_close.reindex(perf).to_numpy()
    M = m_on.reindex(perf).to_numpy().astype(bool)
    prev_session = sessions[sessions < perf[0]][-1]
    m_before = bool(m_on.loc[prev_session])
    lr = data.last_row.reindex(cols)
    lr_arr = lr.to_numpy()
    pct_trail = float(cfg.trail[3:]) / 100 if cfg.trail.startswith("pct") else None
    stop_sched = stop_rules.schedule(stops, len(perf)) if stops is not None else None
    SIG = None
    if stop_sched is not None and any(s.kind == "vol" for s in stop_sched):
        sg = stop_rules.sigma20(data.sig_idx) if sigma is None else sigma
        SIG = sg.reindex(index=perf, columns=cols).to_numpy()

    # leaders known at the close of each session: those of the latest Friday on or before it
    lead_by_week = {t: list(zip(g["security_id"], g["rs"])) for t, g in leaders.groupby("week_end")}
    weeks = sorted(lead_by_week)
    wk_pos = np.searchsorted(np.array(weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1

    cash, qunits = account, 0.0
    pos: dict = {}     # sid -> dict(units, entry_i, entry_idx, peak_idx, full, filled, cost_basis)
    pend_sell: dict = {}
    pend_add: set = set()
    pend_buy: list = []
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    counts = {"buy": 0, "add": 0, "sell": 0, "stop": 0, "trail": 0, "market": 0, "delisted": 0, "qqq_orders": 0}
    trades, orders = [], []
    week_for_spread = None

    def hs(sid, price):
        r = rank_of.get(week_for_spread, {}).get(sid, np.nan) if week_for_spread is not None else np.nan
        return rev.half_spread(r, price, cfg.spread_mult)

    for i, d in enumerate(perf):
        day_cost = 0.0
        week_for_spread = weeks[wk_pos[i]] if wk_pos[i] >= 0 else None
        # ended series: the booked terminal value goes to cash, no order
        for sid in [s for s in pos if pd.notna(lr[s]) and d > lr[s]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, col[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": "delisted",
                           "ret": val / p["cost_basis"] - 1, "tranches": p["filled"], "days": i - p["entry_i"]})
            pend_sell.pop(sid, None)
            pend_add.discard(sid)

        def spend_capacity():
            return cash + (qunits * Q[i] if cfg.idle == "qqq_on" else 0.0)

        # 1. sells decided yesterday
        for sid, reason in sorted(pend_sell.items()):
            if sid not in pos:
                continue
            p = pos.pop(sid)
            c = col[sid]
            val = p["units"] * I[i, c]
            k = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c]))
            cash += val - k
            day_cost += k
            traded += val
            counts["sell"] += 1
            trades.append({"sid": sid, "entry": perf[p["entry_i"]], "exit": d, "reason": reason,
                           "ret": (val - k) / p["cost_basis"] - 1, "tranches": p["filled"], "days": i - p["entry_i"]})
            orders.append({"date": d, "sid": sid, "side": "sell", "value": val, "reason": reason})
        pend_sell = {}
        # 2. pyramid adds decided yesterday (only ever after a profit)
        for sid in sorted(pend_add):
            if sid not in pos:
                continue
            p = pos[sid]
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])):
                continue
            amt = min(p["full"] * cfg.tranches[p["filled"]], spend_capacity())
            if amt < 100:
                continue
            k = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
            if cash < amt:      # fund from the QQQ parking position at the same close
                need = amt - cash
                qunits -= need / Q[i]
                cash += need
                kq = cs.order_cost(need, QP[i], True, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                cash -= kq
                day_cost += kq
                traded += need
            p["units"] += (amt - k) / I[i, c]
            p["filled"] += 1
            p["cost_basis"] += amt
            cash -= amt
            day_cost += k
            traded += amt
            counts["add"] += 1
            orders.append({"date": d, "sid": sid, "side": "add", "value": amt, "reason": f"tranche{p['filled']}"})
        pend_add = set()
        # 3. new entries decided yesterday (first tranche)
        nav_now = cash + qunits * Q[i] + sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        for sid in pend_buy:
            if len(pos) >= cfg.k or sid in pos:
                continue
            c = col[sid]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[sid]) and lr[sid] < d):
                continue
            full = nav_now / cfg.k
            amt = min(full * cfg.tranches[0], spend_capacity())
            if amt < 100:
                break
            k = cs.order_cost(amt, P[i, c], False, hs(sid, P[i, c]))
            if cash < amt:
                need = amt - cash
                qunits -= need / Q[i]
                cash += need
                kq = cs.order_cost(need, QP[i], True, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                cash -= kq
                day_cost += kq
                traded += need
            pos[sid] = {"units": (amt - k) / I[i, c], "entry_i": i, "entry_idx": I[i, c], "peak_idx": I[i, c],
                        "full": full, "filled": 1, "cost_basis": amt,
                        "sigma": SIG[i, c] if SIG is not None else np.nan}
            cash -= amt
            day_cost += k
            traded += amt
            counts["buy"] += 1
            orders.append({"date": d, "sid": sid, "side": "buy", "value": amt, "reason": "breakout"})
        pend_buy = []
        # 4. idle money: QQQ while yesterday's market read was on, cash otherwise
        m_yday = M[i - 1] if i > 0 else m_before
        if cfg.idle == "qqq_on":
            stock_val = sum(p["units"] * I[i, col[s]] for s, p in pos.items())
            qv = qunits * Q[i]
            nav = cash + qv + stock_val
            target = max(cash + qv, 0.0) if m_yday else 0.0
            delta = target - qv
            if abs(delta) > 0.02 * nav or (target == 0 and qv > 0):
                k = cs.order_cost(abs(delta), QP[i], delta < 0, max(cs.QQQ_HALF_SPREAD, 0.005 / QP[i]))
                qunits += delta / Q[i]
                cash -= delta + k
                day_cost += k
                traded += abs(delta)
                counts["qqq_orders"] += 1
        # 5. decisions at today's close (filled tomorrow)
        if i < len(perf) - 1:
            for sid, p in pos.items():
                c = col[sid]
                p["peak_idx"] = max(p["peak_idx"], I[i, c]) if np.isfinite(I[i, c]) else p["peak_idx"]
                if p["entry_i"] == i:
                    continue
                r = I[i, c] / p["entry_idx"] - 1
                if stop_sched is not None:
                    hit = stop_rules.triggered(stop_sched[i], I[i, c], p["entry_idx"], p["peak_idx"], p["sigma"])
                else:
                    hit = cfg.stop is not None and r <= -cfg.stop
                if hit:
                    pend_sell[sid] = "stop"
                    counts["stop"] += 1
                elif (T is not None and T[i, c]) or (pct_trail is not None and I[i, c] / p["peak_idx"] - 1 <= -pct_trail):
                    pend_sell[sid] = "trail"
                    counts["trail"] += 1
                elif cfg.market != "none" and cfg.m_exit and not M[i]:
                    pend_sell[sid] = "market_off"
                    counts["market"] += 1
                elif p["filled"] < len(cfg.tranches) and r >= cfg.add_step * p["filled"]:
                    pend_add.add(sid)
            if M[i] and wk_pos[i] >= 0:
                slots = cfg.k - (len(pos) - len(pend_sell))
                if slots > 0:
                    for sid, _rs in lead_by_week[weeks[wk_pos[i]]]:
                        c = col.get(sid)
                        if c is None or sid in pos or not B[i, c]:
                            continue
                        if pd.notna(lr_arr[c]) and lr_arr[c] <= d:      # no data after today: cannot be bought
                            continue
                        pend_buy.append(sid)
                        if len(pend_buy) >= slots:
                            break
        stock_val = sum(p["units"] * I[i, col[s]] for s, p in pos.items())
        navs[i] = cash + qunits * Q[i] + stock_val
        expo[i] = stock_val / navs[i] if navs[i] > 0 else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "cost": pd.Series(costs, index=perf), "traded": traded, "counts": counts,
            "trades": pd.DataFrame(trades), "orders": pd.DataFrame(orders), "open": sorted(pos)}


# ======================================================================== metrics

def perf_metrics(nav: pd.Series, bench: pd.Series, account: float, cost: pd.Series, traded: float,
                 exposure: pd.Series) -> dict:
    # the starting value sits 7 days before the first close so that the first week and month keep their return
    anchor = nav.index[0] - pd.Timedelta(days=7)
    nav0 = pd.concat([pd.Series([account], index=[anchor]), nav])
    bn0 = pd.concat([pd.Series([account], index=[anchor]), bench])
    r, b = nav0.pct_change().dropna(), bn0.pct_change().dropna()
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    cagr = (nav.iloc[-1] / account) ** (1 / years) - 1
    bcagr = (bench.iloc[-1] / account) ** (1 / years) - 1
    a = r - b
    wa = (cs.weekly(nav0).pct_change() - cs.weekly(bn0).pct_change()).dropna()
    mn, mb = nav0.resample("ME").last().dropna(), bn0.resample("ME").last().dropna()
    ma = (mn.pct_change() - mb.pct_change()).dropna()
    sd = a.std(ddof=1)
    out = {
        "start": str(nav.index[0].date()), "end": str(nav.index[-1].date()),
        "cagr": cagr, "qqq_cagr": bcagr, "excess_cagr": cagr - bcagr,
        "vol": r.std(ddof=1) * math.sqrt(252), "max_dd": rev.max_drawdown(r), "qqq_max_dd": rev.max_drawdown(b),
        "sharpe": r.mean() / r.std(ddof=1) * math.sqrt(252) if r.std() > 0 else np.nan,
        "qqq_sharpe": b.mean() / b.std(ddof=1) * math.sqrt(252),
        "ir": a.mean() / sd * math.sqrt(252) if sd > 0 else np.nan,
        "t_excess_weekly": wa.mean() / wa.std(ddof=1) * math.sqrt(len(wa)) if wa.std() > 0 else np.nan,
        "t_excess_monthly": ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)) if ma.std() > 0 else np.nan,
        "ir_weekly_per_period": wa.mean() / wa.std(ddof=1) if wa.std() > 0 else np.nan,
        "weekly_active_skew": float(wa.skew()), "weekly_active_kurt": float(wa.kurt() + 3), "weeks": len(wa),
        "months": len(ma), "tracking_error": sd * math.sqrt(252),
        "turnover_one_way_per_year": traded / 2 / nav.mean() / years,
        "cost_drag_per_year": float((cost / nav.shift(1).fillna(account)).sum() / years),
        "cost_usd_total": float(cost.sum()),
        "time_in_stocks": float((exposure > 0.05).mean()), "avg_exposure": float(exposure.mean()),
        "active_weekly_mean_ann": wa.mean() * 52,
    }
    for n in (5, 10):
        out[f"active_ann_drop_best{n}w"] = wa.drop(wa.nlargest(n).index).mean() * 52
    by = {}
    for y in sorted(set(nav.index.year)):
        m = nav.index.year == y
        prev_n = nav[nav.index.year < y].iloc[-1] if (nav.index.year < y).any() else account
        prev_b = bench[bench.index.year < y].iloc[-1] if (bench.index.year < y).any() else account
        sy, by_ = nav[m].iloc[-1] / prev_n - 1, bench[m].iloc[-1] / prev_b - 1
        by[int(y)] = {"strategy": round(float(sy), 4), "qqq": round(float(by_), 4), "excess": round(float(sy - by_), 4)}
    out["by_year"] = by
    out["years_beating_qqq"] = sum(v["excess"] > 0 for v in by.values())
    out["_monthly_active"] = ma
    return out


def trade_stats(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"closed_trades": 0}
    w, l_ = trades[trades["ret"] > 0], trades[trades["ret"] <= 0]
    return {"closed_trades": int(len(trades)), "win_rate": float((trades["ret"] > 0).mean()),
            "avg_trade_ret": float(trades["ret"].mean()), "avg_win": float(w["ret"].mean()) if len(w) else np.nan,
            "avg_loss": float(l_["ret"].mean()) if len(l_) else np.nan,
            "median_days": float(trades["days"].median()) if "days" in trades else np.nan,
            "share_pyramided": float((trades["tranches"] > 1).mean()), "best_trade": float(trades["ret"].max()),
            "worst_trade": float(trades["ret"].min())}


# ======================================================================== runner

class Runner:
    """Caches the frames that several configs share; runs one config on one window."""

    def __init__(self, data: WinData, exclude: frozenset = frozenset()):
        self.data = data
        self.exclude = exclude
        self.u = eligible_universe(data)
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in self.u.groupby("week_end")}
        self._lead, self._brk, self._trail, self._m = {}, {}, {}, {}

    def leaders(self, cfg):
        key = (cfg.n_universe, cfg.rs_lb, cfg.n_groups, cfg.rs_top, cfg.per_group)
        if key not in self._lead:
            lead = weekly_leaders(self.u, self.data, cfg.rs_lb, cfg.n_groups, cfg.rs_top, cfg.per_group,
                                  n_universe=cfg.n_universe)
            self._lead[key] = lead[~lead["security_id"].isin(self.exclude)]
        return self._lead[key]

    def brk(self, cfg):
        key = (cfg.pivot, cfg.vol_mult)
        if key not in self._brk:
            self._brk[key] = breakout_frame(self.data.close_adj, self.data.vol_adj, cfg.pivot, cfg.vol_mult)
        return self._brk[key]

    def trail(self, cfg):
        if cfg.trail not in self._trail:
            self._trail[cfg.trail] = trail_frame(self.data.close_adj, cfg.trail)
        return self._trail[cfg.trail]

    def m(self, cfg):
        if cfg.market not in self._m:
            self._m[cfg.market] = market_state(self.data.qqq_close, cfg.market)
        return self._m[cfg.market]

    def run(self, cfg: Config):
        d = self.data
        res = simulate(cfg, d, self.leaders(cfg), self.brk(cfg), self.trail(cfg), self.m(cfg), self.rank_of)
        bench = cs.qqq_benchmark(d.qqq_perf_idx, d.qqq_close, res["dates"], cfg.account)
        m = perf_metrics(res["nav"], bench, cfg.account, res["cost"], res["traded"], res["exposure"])
        m.update({f"n_{k}": v for k, v in res["counts"].items()})
        m.update(trade_stats(res["trades"]))
        return m, res, bench


def sub_metrics(nav: pd.Series, bench: pd.Series, start: str, cost: pd.Series, exposure: pd.Series,
                account: float = 10_000.0) -> dict:
    """Metrics on a sub-window, both series rebased to ``account`` at the last close before ``start``."""
    before = nav.index[nav.index < pd.Timestamp(start)]
    if not len(before):
        return perf_metrics(nav, bench, account, cost, 0.0, exposure)
    t0 = before[-1]
    keep = nav.index > t0
    n = nav[keep] / nav.loc[t0] * account
    b = bench[keep] / bench.loc[t0] * account
    return perf_metrics(n, b, account, cost[keep], 0.0, exposure[keep])


# ======================================================================== development grid

def build_grid() -> list[tuple[str, Config]]:
    base = Config()
    grid, seen = [], set()

    def add(tag, cfg):
        if cfg.name not in seen:
            seen.add(cfg.name)
            grid.append((tag, cfg))

    add("baseline", base)
    # A: portfolio / market / exit factorial on the baseline entry rule
    for k in (3, 5):
        for idle in ("cash", "qqq_on"):
            for market, m_exit in (("sma200", True), ("sma200", False), ("sma50", True), ("none", True)):
                for trail in ("sma50", "low20", "pct20"):
                    add("A_portfolio_market_exit", replace(base, k=k, idle=idle, market=market, m_exit=m_exit,
                                                           trail=trail))
    # B: entry rule (groups, leaders, pivot, volume), one change at a time, idle cash and idle QQQ-when-on
    for idle in ("cash", "qqq_on"):
        b = replace(base, idle=idle)
        for ch in (dict(pivot="base25_t15"), dict(pivot="base50_t20"), dict(pivot="base35_t12"),
                   dict(vol_mult=1.5), dict(rs_lb=252), dict(n_groups=4), dict(n_groups=12), dict(n_groups=None),
                   dict(rs_top=0.10), dict(rs_top=0.30), dict(rs_top=None, per_group=2), dict(n_universe=150)):
            add("B_entry_one_at_a_time", replace(b, **ch))
    # C: pyramiding, stops, trails, number of names, one change at a time
    for idle in ("cash", "qqq_on"):
        b = replace(base, idle=idle)
        for ch in (dict(tranches=(1.0,)), dict(tranches=(1 / 3, 1 / 3, 1 / 3)), dict(add_step=0.10),
                   dict(tranches=(1 / 3, 1 / 3, 1 / 3), add_step=0.10), dict(stop=0.05), dict(stop=0.10),
                   dict(trail="sma20"), dict(trail="low50"), dict(trail="pct15"), dict(trail="pct25"),
                   dict(k=4), dict(k=6)):
            add("C_pyramid_stop_one_at_a_time", replace(b, **ch))
    # D: small factorial of the core Livermore choices
    for pivot in ("high252", "base50_t20"):
        for vm in (None, 1.5):
            for tranches in ((1.0,), (0.5, 0.5)):
                for trail in ("sma50", "pct20"):
                    for idle in ("cash", "qqq_on"):
                        add("D_core_factorial", replace(base, pivot=pivot, vol_mult=vm, tranches=tranches,
                                                        trail=trail, idle=idle))
    return grid


def neighbours(cfg: Config, rows: list) -> list:
    out = []
    for r in rows:
        diff = [f for f in RULE_FIELDS if str(r["cfg"].__getattribute__(f)) != str(getattr(cfg, f))]
        if len(diff) == 1 or (len(diff) == 2 and set(diff) == {"tranches", "add_step"}):
            out.append(r)
    return out


def plateau_pick(rows: list, min_buys: int = 30) -> tuple[dict, pd.DataFrame]:
    """Pre-registered freeze rule: highest median net IR over (variant + its one-change neighbours), among variants
    with >= ``min_buys`` first entries and >= 3 neighbours."""
    scored = []
    for r in rows:
        nb = neighbours(r["cfg"], rows)
        if r["m"]["n_buy"] < min_buys or len(nb) < 3:
            continue
        irs = [r["m"]["ir"]] + [x["m"]["ir"] for x in nb]
        scored.append({"variant": r["variant"], "config": r["cfg"].name, "plateau_ir": float(np.median(irs)),
                       "own_ir": r["m"]["ir"], "n_neighbours": len(nb),
                       "neighbours_beating_qqq": sum(x["m"]["excess_cagr"] > 0 for x in nb)})
    tab = pd.DataFrame(scored).sort_values(["plateau_ir", "own_ir"], ascending=False)
    best = next(r for r in rows if r["variant"] == int(tab.iloc[0]["variant"]))
    return best, tab


def fmt(v):
    if isinstance(v, (np.floating, np.integer)) and not isinstance(v, bool):
        return float(v)
    return v


def run_dev(args) -> dict:
    out = OUT / "dev_2017_2022"
    out.mkdir(parents=True, exist_ok=True)
    data = load_window("dev")
    assert data.spec["effective_end"] <= "2022-12-31" and data.guard["max_session_loaded"] <= "2022-12-31"
    print(data.guard["assertion"])
    for name, g in data.guard["frames"].items():
        print(f"  guard {name}: kept {g['rows_kept']} rows [{g['min_date']} .. {g['max_date']}], dropped "
              f"{g['rows_dropped_after_end']} after the end and {g['rows_dropped_before_start']} before the start")
    runner = Runner(data)
    stress = Runner(load_window("dev", terminal_awaiting=rev.TERMINAL_D5_STRESS))
    grid = build_grid()
    rows = []
    for i, (tag, cfg) in enumerate(grid, 1):
        m, res, bench = runner.run(cfg)
        m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
        rows.append({"variant": i, "tag": tag, "cfg": cfg, "m": m, "excess_spread2x": m2["excess_cagr"],
                     "ir_spread2x": m2["ir"]})
        print(f"[{i}/{len(grid)}] {cfg.name}: CAGR {m['cagr']:+.1%} ex {m['excess_cagr']:+.1%} IR {m['ir']:.2f} "
              f"t {m['t_excess_monthly']:.2f} DD {m['max_dd']:.0%} buys {m['n_buy']} adds {m['n_add']} "
              f"cost {m['cost_drag_per_year']:.2%}", flush=True)
    flat = []
    for r in rows:
        m = {k: fmt(v) for k, v in r["m"].items() if k not in ("by_year", "_monthly_active")}
        d = asdict(r["cfg"])
        d["tranches"] = "/".join(f"{x:.3g}" for x in r["cfg"].tranches)
        flat.append({"variant": r["variant"], "tag": r["tag"], "config": r["cfg"].name, **d, **m,
                     "excess_cagr_spread2x": r["excess_spread2x"], "ir_spread2x": r["ir_spread2x"],
                     "by_year_excess": json.dumps({y: v["excess"] for y, v in r["m"]["by_year"].items()})})
    df = pd.DataFrame(flat)
    df.to_csv(out / "grid.csv", index=False)
    n = len(df)
    trials = df["ir_weekly_per_period"].to_numpy()

    def robust(r, label):
        cfg, m = r["cfg"], r["m"]
        mres = runner.run(cfg)
        _, res, bench = mres
        ms, *_ = stress.run(cfg)
        nb = neighbours(cfg, rows)
        dsr = rev.deflated_sharpe(float(m["ir_weekly_per_period"]), int(m["weeks"]), float(m["weekly_active_skew"]),
                                  float(m["weekly_active_kurt"]), trials)
        res["nav"].to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(out / f"{label}_daily_nav.csv")
        res["trades"].to_csv(out / f"{label}_trades.csv", index=False)
        res["orders"].to_csv(out / f"{label}_orders.csv", index=False)
        nbt = pd.DataFrame([{"variant": x["variant"], "config": x["cfg"].name,
                             "changed": [f for f in RULE_FIELDS if str(getattr(x["cfg"], f)) != str(getattr(cfg, f))],
                             "excess_cagr": x["m"]["excess_cagr"], "ir": x["m"]["ir"], "max_dd": x["m"]["max_dd"],
                             "cagr": x["m"]["cagr"]} for x in nb])
        nbt.to_csv(out / f"{label}_neighbours.csv", index=False)
        return {"variant": r["variant"], "config": cfg.name, "cfg": asdict(cfg),
                "metrics": {k: fmt(v) for k, v in m.items() if k != "_monthly_active"},
                "excess_spread2x": r["excess_spread2x"], "terminal_minus100_excess_cagr": ms["excess_cagr"],
                "neighbours": nbt.to_dict("records"),
                "neighbours_beating_qqq": int((nbt["excess_cagr"] > 0).sum()) if len(nbt) else 0,
                "deflated_sharpe_active_weekly": dsr}

    by_ir = max(rows, key=lambda r: r["m"]["ir"])
    pick, plateau = plateau_pick(rows)
    plateau.to_csv(out / "plateau_scores.csv", index=False)
    base_row = rows[0]
    summary = {
        "date_guard": data.guard, "variants_tried": n, "variants_by_tag": df["tag"].value_counts().to_dict(),
        "variants_beating_qqq": int((df["excess_cagr"] > 0).sum()),
        "variants_beating_qqq_spread2x": int((df["excess_cagr_spread2x"] > 0).sum()),
        "variants_dd_10pts_shallower_and_cagr_within_3": int(((df["max_dd"] - df["qqq_max_dd"] >= 0.10) &
                                                              (df["excess_cagr"] >= -0.03)).sum()),
        "median_excess_cagr": float(df["excess_cagr"].median()),
        "bonferroni_t_one_sided_5pct": float(NORM.inv_cdf(1 - 0.05 / n)),
        "baseline": robust(base_row, "baseline"),
        "top_by_ir": robust(by_ir, "top_by_ir"),
        "plateau_pick": robust(pick, "plateau_pick"),
        "qqq": {"cagr": float(base_row["m"]["qqq_cagr"]), "max_dd": float(base_row["m"]["qqq_max_dd"]),
                "sharpe": float(base_row["m"]["qqq_sharpe"])},
        "carried_universe_weeks": int(runner.u.loc[runner.u["carried"], "week_end"].nunique()),
        "terminal_events": data.terminal_events["status"].value_counts().to_dict() if len(data.terminal_events) else {},
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print("variants:", n, "beating QQQ:", summary["variants_beating_qqq"], "Bonferroni t:",
          round(summary["bonferroni_t_one_sided_5pct"], 2))
    return summary


def run_dev_drop_best_name() -> dict:
    """Post-hoc robustness (development period only): rerun every variant without the single name that made the
    largest dollar profit for the plateau pick, and the three reported variants without their own best name."""
    out = OUT / "dev_2017_2022"
    data = load_window("dev")
    assert data.guard["max_session_loaded"] <= "2022-12-31"
    full = Runner(data)
    grid = build_grid()
    summ = json.loads((out / "summary.json").read_text())

    def best_name(cfg):
        _, res, _ = full.run(cfg)
        o = res["orders"]
        t = res["trades"]
        # dollar P&L per name = sells + terminal values - buys - adds (open positions valued at the end)
        pnl = {}
        for r in o.itertuples():
            pnl[r.sid] = pnl.get(r.sid, 0.0) + (r.value if r.side == "sell" else -r.value)
        return max(pnl, key=pnl.get), pnl

    reported = {}
    for key in ("baseline", "top_by_ir", "plateau_pick"):
        cfg = grid[summ[key]["variant"] - 1][1]
        sid, pnl = best_name(cfg)
        m, *_ = Runner(data, frozenset([sid])).run(cfg)
        reported[key] = {"variant": summ[key]["variant"], "dropped": sid, "dropped_pnl_usd": pnl[sid],
                         "excess_cagr": m["excess_cagr"], "ir": m["ir"], "cagr": m["cagr"], "max_dd": m["max_dd"],
                         "t_excess_monthly": m["t_excess_monthly"]}
        print(key, reported[key], flush=True)
    drop = reported["plateau_pick"]["dropped"]
    ex = Runner(data, frozenset([drop]))
    rows = []
    for i, (tag, cfg) in enumerate(grid, 1):
        m, *_ = ex.run(cfg)
        rows.append({"variant": i, "config": cfg.name, "excess_cagr_ex": m["excess_cagr"], "ir_ex": m["ir"],
                     "max_dd_ex": m["max_dd"], "t_monthly_ex": m["t_excess_monthly"]})
    df = pd.DataFrame(rows)
    df.to_csv(out / f"grid_without_{drop}.csv", index=False)
    res = {"dropped_for_grid": drop, "variants_beating_qqq_without_it": int((df["excess_cagr_ex"] > 0).sum()),
           "median_excess_without_it": float(df["excess_cagr_ex"].median()), "reported": reported}
    (out / "drop_best_name.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    return res


# ======================================================================== one-shot tests

def frozen_config() -> tuple[Config, dict]:
    if not FROZEN.is_file():
        raise SystemExit(f"no frozen rule at {FROZEN}: freeze the rule in the ledger first")
    fr = json.loads(FROZEN.read_text())
    c = dict(fr["config"])
    c["tranches"] = tuple(c["tranches"])
    cfg = Config(**c)
    if cfg.name != fr["config_name"]:
        raise SystemExit("frozen rule file is inconsistent")
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    h = hashlib.sha256(text[a:b].encode()).hexdigest()
    if h != fr["ledger_freeze_sha256"]:
        raise SystemExit("the ledger freeze block changed after the rule was frozen")
    return cfg, fr


def write_frozen(cfg: Config, criterion: str) -> None:
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    d = asdict(cfg)
    d["tranches"] = list(cfg.tranches)
    FROZEN.write_text(json.dumps({"config": d, "config_name": cfg.name, "criterion_applied": criterion,
                                  "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                                  "ledger_freeze_sha256": hashlib.sha256(text[a:b].encode()).hexdigest()}, indent=2))


def run_oneshot(name: str) -> dict:
    assert name in ("test1", "test2")
    cfg, fr = frozen_config()
    out = OUT / f"oneshot_{name}"
    if (out / "result.json").exists():
        raise SystemExit(f"{name} has already been run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = load_window(name)
    print(data.guard["assertion"])
    runner = Runner(data)
    m, res, bench = runner.run(cfg)
    m2, *_ = runner.run(replace(cfg, spread_mult=2.0))
    ms, *_ = Runner(load_window(name, terminal_awaiting=rev.TERMINAL_D5_STRESS)).run(cfg)
    judged = sub_metrics(res["nav"], bench, data.spec["judged_from"], res["cost"], res["exposure"])
    res["nav"].to_frame("nav").assign(qqq=bench, exposure=res["exposure"]).to_csv(out / "daily_nav.csv")
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["orders"].to_csv(out / "orders.csv", index=False)
    judged["_monthly_active"].to_csv(out / "judged_monthly_active.csv", header=["active"])
    result = {"window": name, "config": cfg.name, "frozen_at": fr["frozen_at"], "date_guard": data.guard,
              "full_window": {k: fmt(v) for k, v in m.items() if k != "_monthly_active"},
              "judged_window_from": data.spec["judged_from"],
              "judged": {k: fmt(v) for k, v in judged.items() if k != "_monthly_active"},
              "excess_spread2x": m2["excess_cagr"], "terminal_minus100_excess_cagr": ms["excess_cagr"],
              "carried_universe_weeks": int(runner.u.loc[runner.u["carried"], "week_end"].nunique()),
              "terminal_events": data.terminal_events["status"].value_counts().to_dict()
              if len(data.terminal_events) else {}, "open_positions_at_end": res["open"]}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    j = judged
    print(f"{name} judged {j['start']}..{j['end']}: CAGR {j['cagr']:+.1%} QQQ {j['qqq_cagr']:+.1%} ex "
          f"{j['excess_cagr']:+.1%} DD {j['max_dd']:.0%} (QQQ {j['qqq_max_dd']:.0%}) t_m {j['t_excess_monthly']:.2f}")
    return result


def recompute_oneshot_metrics(name: str) -> dict:
    """Metrics recomputed from a one-shot's saved daily NAV (no strategy re-run), with the first-month fix."""
    out = OUT / f"oneshot_{name}"
    d = pd.read_csv(out / "daily_nav.csv", index_col=0, parse_dates=True)
    cost = pd.Series(0.0, index=d.index)
    spec = WINDOWS[name]
    full = perf_metrics(d["nav"], d["qqq"], 10_000.0, cost, 0.0, d["exposure"])
    judged = sub_metrics(d["nav"], d["qqq"], spec["judged_from"], cost, d["exposure"])
    judged["_monthly_active"].to_csv(out / "judged_monthly_active_recomputed.csv", header=["active"])
    res = {"note": "recomputed from daily_nav.csv; cost fields are zero here, see result.json for costs",
           "full_window": {k: fmt(v) for k, v in full.items() if k != "_monthly_active"},
           "judged": {k: fmt(v) for k, v in judged.items() if k != "_monthly_active"}}
    (out / "metrics_recomputed.json").write_text(json.dumps(res, indent=2, default=str))
    return res


def evaluate_tests() -> dict:
    """Both pre-registered criteria on the two judged test windows (written after both one-shots ran)."""
    r = {n: recompute_oneshot_metrics(n) for n in ("test1", "test2")}
    ma = pd.concat([pd.read_csv(OUT / f"oneshot_{n}/judged_monthly_active_recomputed.csv", index_col=0)["active"]
                    for n in ("test1", "test2")])
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    j = {n: r[n]["judged"] for n in r}
    crit_a = {"excess_positive_each": {n: j[n]["excess_cagr"] > 0 for n in j}, "combined_months": len(ma),
              "combined_t_monthly": t}
    crit_a["pass"] = all(crit_a["excess_positive_each"].values()) and t >= 2
    crit_b = {n: {"dd_shallower_pts": j[n]["max_dd"] - j[n]["qqq_max_dd"],
                  "cagr_gap_pts": j[n]["excess_cagr"],
                  "pass": (j[n]["max_dd"] - j[n]["qqq_max_dd"] >= 0.10) and (j[n]["excess_cagr"] >= -0.03)} for n in j}
    crit_b["pass"] = all(v["pass"] for v in crit_b.values() if isinstance(v, dict))
    fr = json.loads(FROZEN.read_text())
    out = {"criterion_applied": fr["criterion_applied"], "A_excess_and_t": crit_a, "B_drawdown": crit_b}
    out["verdict_on_applied"] = crit_a["pass"] if fr["criterion_applied"] == "A" else crit_b["pass"]
    (OUT / "oneshot_evaluation.json").write_text(json.dumps(out, indent=2, default=str))
    print(json.dumps(out, indent=2, default=str))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--oneshot", choices=["test1", "test2"], help="run one frozen one-shot test (once only)")
    p.add_argument("--dev-drop-best-name", action="store_true", help="post-hoc dev robustness: drop the best name")
    p.add_argument("--evaluate", action="store_true", help="apply the pre-registered criteria to both one-shots")
    p.add_argument("--freeze-variant", type=int, help="write the frozen-rule file for this dev grid variant")
    p.add_argument("--criterion", choices=["A", "B"], help="pass criterion that applies (with --freeze-variant)")
    args = p.parse_args(argv)
    if args.freeze_variant:
        if args.criterion is None:
            raise SystemExit("--criterion is required with --freeze-variant")
        cfg = build_grid()[args.freeze_variant - 1][1]
        write_frozen(cfg, args.criterion)
        print("frozen:", cfg.name)
    elif args.dev_drop_best_name:
        run_dev_drop_best_name()
    elif args.oneshot:
        run_oneshot(args.oneshot)
    elif args.evaluate:
        evaluate_tests()
    else:
        run_dev(args)


if __name__ == "__main__":
    main()
