"""Spin-off rules SP1-SP4 (docs/research_ledger_spinoffs.md section 0.4): event plans, the daily slot engine at the
close (monthly 25% band, idle money in ONEQ, terminal values on delisting), the registered terminal rule, market cap
at the spin, event-time buy-and-hold returns, and the rule metrics.

Moved unchanged from studies/spinoffs.py in the slimming pass (``Config``, ``Asset`` / ``make_asset``, ``simulate``,
``regular_way_index``, ``build_plan``, ``v2_terminals`` / ``terminal_for`` / ``segment_assets``, ``attach_mcap``,
``bhar_table``, ``metrics``). ``slot_book_metrics`` is the part of the metrics shared with
quant.strategies.ndx_recon. Folders and the judged half ends are arguments declared by the study.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_total as order_cost, volume_tier_half_spread as half_spread
from quant.data.sources.daily_series import load_series
from quant.strategies import indicators as ind

ACCOUNT = 10_000.0
SLOTS = 10
BAND = 0.25                 # monthly rebalance band (fraction of target)
SIZE_ETF = ((2e9, "IWM"), (1e10, "IJH"), (float("inf"), "SPY"))
ONEQ_HALF_SPREAD = 2e-4


@dataclass(frozen=True)
class Config:
    code: str
    who: str = "spinco"       # spinco | parent
    entry_day: int = 5        # n-th regular-way session (1 = first)
    months: int = 12
    min_mcap: float = 0.0
    judged: bool = True


# ======================================================================== panel on sessions

@dataclass
class Asset:
    """One security on the segment's sessions: raw close (NaN where no row), daily total return (0 where no row),
    the last row's session index, the terminal return booked on the session after it (None: not delisted in data),
    and the median raw dollar volume of the regular-way rows so far (max 20)."""
    key: str
    px: np.ndarray
    ret: np.ndarray
    last_i: int
    terminal: float | None
    dv: np.ndarray


def make_asset(key: str, df: pd.DataFrame, sessions: pd.DatetimeIndex, terminal: float | None,
               data_end: pd.Timestamp, delist_date: pd.Timestamp | None = None) -> Asset:
    """``terminal`` applies only when the series ends before ``data_end`` and, where the Form 25 date is known,
    within 15 days of it (a real delisting). A series that stops earlier is a data gap: the position leaves at
    the last close (0%), counted as ``data_end``."""
    s = df.set_index("date")
    s = s[~s.index.duplicated(keep="last")]
    px = s["close_raw"].reindex(sessions).to_numpy(float)
    tr = s["tr"].reindex(sessions).fillna(0.0).to_numpy(float)
    dvol = (s["close_raw"] * s["volume_raw"]).rolling(20, min_periods=1).median().reindex(sessions).ffill()
    rows = np.where(~np.isnan(px))[0]
    last_i = int(rows[-1]) if len(rows) else -1
    ended = len(rows) and s.index[-1] < data_end - pd.Timedelta(days=7)
    if ended and delist_date is not None and s.index[-1] < delist_date - pd.Timedelta(days=15):
        terminal = None   # the data stop before the delisting: a gap, not the delisting
    return Asset(key, px, tr, last_i, terminal if ended else None, dvol.to_numpy(float))


# ======================================================================== engine

def add_months(d: pd.Timestamp, m: int) -> pd.Timestamp:
    return d + pd.DateOffset(months=m)


def simulate(plan: list[dict], assets: dict[str, Asset], sessions: pd.DatetimeIndex, oneq_ret: np.ndarray,
             oneq_px: np.ndarray, months: int, spread_mult: float = 1.0, slots: int = SLOTS) -> dict:
    """Daily engine at the close. ``plan``: candidates with ``key``, ``entry_i`` (session index), ``order``
    (FIFO tie-break), ``event_id``. Registered rules (ledger 0.4): up to ``slots`` positions at NAV/slots, first
    come first served, monthly rebalance with a 25% band, exits after ``months`` calendar months, idle money in
    ONEQ, cash above 2% of NAV swept into ONEQ every close."""
    n = len(sessions)
    by_day: dict[int, list[dict]] = {}
    for c in plan:
        by_day.setdefault(c["entry_i"], []).append(c)
    month_end = np.r_[sessions[1:].month != sessions[:-1].month, True]
    pos: dict[str, dict] = {}
    cash, oneq_sh, oneq_val = ACCOUNT, 0.0, 0.0
    navs, costs, expo, npos = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, int)
    trades, skipped_full, skipped_price, traded = [], 0, 0, 0.0

    def oneq_trade(dollars: float, i: int) -> float:
        """Buy (dollars > 0) or sell ONEQ whole shares; returns the cost."""
        nonlocal cash, oneq_sh, oneq_val
        p = oneq_px[i]
        sh = math.floor(dollars / p) if dollars > 0 else -math.ceil(-dollars / p)
        if dollars < 0:
            sh = max(sh, -math.floor(oneq_sh + 1e-9))
        if sh == 0:
            return 0.0
        c = order_cost(abs(sh), p, sh < 0, max(ONEQ_HALF_SPREAD * spread_mult, 0.005 / p * spread_mult))
        cash -= sh * p + c
        oneq_sh += sh
        oneq_val = oneq_sh * p
        return c

    def raise_cash(need: float, i: int) -> float:
        if cash >= need:
            return 0.0
        return oneq_trade(-(need - cash) * 1.01, i)

    for i in range(n):
        day_cost = 0.0
        # 1. returns of the day
        if i > 0 and oneq_sh:   # ONEQ marked at its close; its distributions (total return - price return) to cash
            gross = oneq_sh * oneq_px[i - 1] * (1.0 + oneq_ret[i])
            oneq_val = oneq_sh * oneq_px[i]
            cash += gross - oneq_val
        for k, p in list(pos.items()):
            a = assets[k]
            if i > p["entry_i"]:
                p["val"] *= 1.0 + a.ret[i]
            # 2. delisted in the data: terminal value on the session after the last row
            if i > a.last_i >= p["entry_i"]:
                term = a.terminal if a.terminal is not None else 0.0
                cash += p["val"] * (1.0 + term)
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(), "exit_kind":
                               "delisted" if a.terminal is not None else "data_end",
                               "ret": (p["wd"] + p["val"] * (1 + term)) / p["inv"] - 1, "days": i - p["entry_i"]})
                del pos[k]
        # 3. planned exits
        for k, p in list(pos.items()):
            if i >= p["exit_i"]:
                a = assets[k]
                price = a.px[i] if a.px[i] == a.px[i] else a.px[a.last_i]
                sh = p["val"] / price
                c = order_cost(sh, price, True, half_spread(a.dv[i], price, spread_mult))
                cash += p["val"] - c
                day_cost += c
                traded += p["val"]
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(), "exit_kind": "planned",
                               "ret": (p["wd"] + p["val"] - c) / p["inv"] - 1, "days": i - p["entry_i"]})
                del pos[k]
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        # 4. entries
        for c_ in sorted(by_day.get(i, []), key=lambda c: c["order"]):
            k = c_["key"]
            a = assets.get(k)
            if k in pos:
                continue
            if len(pos) >= slots:
                skipped_full += 1
                continue
            if a is None or not a.px[i] == a.px[i]:
                skipped_price += 1
                continue
            price = a.px[i]
            sh = math.floor(nav / slots / price)
            if sh < 1:
                skipped_price += 1
                continue
            hs = half_spread(a.dv[i], price, spread_mult)
            c = order_cost(sh, price, False, hs)
            day_cost += raise_cash(sh * price + c, i)
            cash -= sh * price + c
            day_cost += c
            traded += sh * price
            exit_d = add_months(sessions[i], months)
            exit_i = int(sessions.searchsorted(exit_d, side="left"))
            pos[k] = {"val": sh * price, "entry_i": i, "exit_i": exit_i, "inv": sh * price + c, "wd": 0.0,
                      "info": {"event_id": c_["event_id"], "key": k, "entry": sessions[i].date().isoformat()}}
        # 5. monthly rebalance (sells first)
        if month_end[i] and pos:
            nav = cash + oneq_val + sum(p["val"] for p in pos.values())
            tgt = nav / slots
            orders = []
            for k, p in pos.items():
                a = assets[k]
                if not a.px[i] == a.px[i] or abs(p["val"] - tgt) <= BAND * tgt:
                    continue
                sh = math.floor(abs(tgt - p["val"]) / a.px[i]) * (1 if tgt > p["val"] else -1)
                if sh:
                    orders.append((sh, k))
            for sh, k in sorted(orders):
                a, p = assets[k], pos[k]
                price = a.px[i]
                c = order_cost(abs(sh), price, sh < 0, half_spread(a.dv[i], price, spread_mult))
                if sh > 0:
                    day_cost += raise_cash(sh * price + c, i)
                cash -= sh * price + c
                p["val"] += sh * price
                if sh > 0:
                    p["inv"] += sh * price + c
                else:
                    p["wd"] += -sh * price - c
                day_cost += c
                traded += abs(sh) * price
        # 6. sweep cash into ONEQ
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        if cash > 0.02 * nav:
            day_cost += oneq_trade(cash - 0.005 * nav, i)
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        navs[i], costs[i] = nav, day_cost
        expo[i] = sum(p["val"] for p in pos.values()) / nav if nav > 0 else 0.0
        npos[i] = len(pos)
    for k, p in pos.items():
        trades.append({**p["info"], "exit": "", "exit_kind": "open", "ret": (p["wd"] + p["val"]) / p["inv"] - 1,
                       "days": n - 1 - p["entry_i"]})
    return {"nav": pd.Series(navs, index=sessions), "cost": pd.Series(costs, index=sessions),
            "exposure": pd.Series(expo, index=sessions), "npos": pd.Series(npos, index=sessions),
            "trades": pd.DataFrame(trades), "traded": traded, "skipped_full": skipped_full,
            "skipped_price": skipped_price}


# ======================================================================== plans, assets

def attach_mcap(ev: pd.DataFrame, series_dir: Path) -> pd.DataFrame:
    """Market cap at the spin (ledger 0.3): SEC shares outstanding x the raw close of the first regular-way
    session in the price series (the first row on or after it). Local only: it rests on a vendor price."""
    ev = ev.copy()
    caps = []
    for r in ev.itertuples():
        cap = ""
        if r.classification == "spin_off" and r.shares_outstanding and r.spinco_cik == r.spinco_cik:
            s = load_series(series_dir, f"spinco_{int(r.spinco_cik)}")
            if s is not None and r.first_regular_way_date:
                s = s[s.date >= pd.Timestamp(r.first_regular_way_date)]
                if len(s) and (s.date.iloc[0] - pd.Timestamp(r.first_regular_way_date)).days <= 10:
                    cap = float(s.close_raw.iloc[0]) * float(r.shares_outstanding)
        caps.append(cap)
    ev["mcap_usd"] = caps
    return ev


def v2_terminals(v2_inputs: Path) -> dict[int, float]:
    """CIK -> terminal return from data version 2 (Nasdaq names), where it has one."""
    t = pd.read_csv(v2_inputs / "terminal_returns_2012_2026.csv", dtype=str)
    t = t[pd.to_numeric(t["terminal_return"], errors="coerce").notna()]
    out = {}
    for r in t.itertuples():
        try:
            out[int(str(r.security_id).split(".")[0])] = float(r.terminal_return)
        except ValueError:
            pass
    return out


def terminal_for(e, who: str, v2t: dict, d5: float) -> float | None:
    """Registered terminal rule (ledger 0.3) for the security's data end; None = no delisting on file."""
    if who == "parent":
        reason, cik = e.parent_delist_reason, e.parent_cik
    else:
        reason, cik = e.delist_reason, e.spinco_cik
    if cik == cik and int(cik) in v2t:
        return v2t[int(cik)]
    if reason == "merger":
        return 0.0
    if reason in ("bankruptcy", "other"):
        return d5
    return None


def regular_way_index(sessions: pd.DatetimeIndex, first_rw: str, n: int) -> int | None:
    """Index of the n-th regular-way session; None when the first regular-way day is outside ``sessions``
    (an event before a segment must not be moved to the segment's first days)."""
    d = pd.Timestamp(first_rw)
    if d < sessions[0] or d > sessions[-1]:
        return None
    i = int(sessions.searchsorted(d, side="left"))
    j = i + n - 1
    return j if j < len(sessions) else None


def build_plan(ev: pd.DataFrame, cfg: Config, sessions: pd.DatetimeIndex, start: str, end: str,
               nasdaq_only: bool = False) -> list[dict]:
    plan, parents_seen = [], set()
    e = ev[(ev.classification == "spin_off") & (ev.first_regular_way_date != "")]
    if nasdaq_only:
        e = e[e.nasdaq_listed == "Y"]
    for r in e.sort_values(["first_regular_way_date", "event_id"]).itertuples():
        i = regular_way_index(sessions, r.first_regular_way_date, cfg.entry_day)
        if i is None or not (pd.Timestamp(start) <= sessions[i] <= pd.Timestamp(end)):
            continue
        if cfg.min_mcap and not (r.mcap_usd and float(r.mcap_usd) >= cfg.min_mcap):
            continue
        if cfg.who == "parent":
            if r.parent_cik != r.parent_cik:
                continue
            key = f"parent_{int(r.parent_cik)}"
            if (key, r.distribution_date) in parents_seen:
                continue
            parents_seen.add((key, r.distribution_date))
        else:
            key = f"spinco_{int(r.spinco_cik)}"
        mcap = float(r.mcap_usd) if r.mcap_usd else 0.0
        plan.append({"key": key, "entry_i": i, "event_id": r.event_id,
                     "order": (r.distribution_date, -mcap, r.event_id)})
    return plan


def segment_assets(ev: pd.DataFrame, sessions: pd.DatetimeIndex, d5: float, data_end: pd.Timestamp, series_dir: Path,
                   v2_inputs: Path) -> dict:
    v2t = v2_terminals(v2_inputs)
    assets = {}
    for r in ev[ev.classification == "spin_off"].itertuples():
        for who, cik in (("spinco", r.spinco_cik), ("parent", r.parent_cik)):
            if cik != cik:
                continue
            key = f"{who}_{int(cik)}"
            if key in assets:
                continue
            df = load_series(series_dir, key)
            if df is None:
                continue
            dd = r.delist_form25_date if who == "spinco" else r.parent_delist_form25_date
            assets[key] = make_asset(key, df, sessions, terminal_for(r, who, v2t, d5), data_end,
                                     pd.Timestamp(dd) if dd else None)
    return assets


# ======================================================================== metrics, diagnostics

def slot_book_metrics(res: dict, bench: dict, account: float) -> dict:
    """``indicators.stock_metrics`` of an engine's NAV vs ONEQ (and QQQ) buy and hold from ``account``, with the
    criteria A / B / pass as ``crit_A`` / ``crit_B`` / ``crit_pass`` (the shared head of the spin-off and Nasdaq-100
    rule metrics)."""
    nav = res["nav"]
    oneq = (1 + bench["ONEQ"]["ret"]).cumprod()
    oneq = oneq / oneq.iloc[0] * account
    qqq = (1 + bench["QQQ"]["ret"]).cumprod()
    qqq = qqq / qqq.iloc[0] * account
    m = ind.stock_metrics(nav, oneq, qqq, account, res["cost"], res["traded"], res["exposure"])
    crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
    m["crit_A"], m["crit_B"], m["crit_pass"] = crit["A"], crit["B"], crit["pass"]
    return m


def metrics(res: dict, bench: dict, bonferroni_t: float) -> dict:
    """``slot_book_metrics`` plus ``t_excess_monthly >= bonferroni_t``, entries / skips, average positions and the
    per-trade return statistics and exit kinds."""
    m = slot_book_metrics(res, bench, ACCOUNT)
    m["t_ge_bonferroni"] = bool(m["t_excess_monthly"] >= bonferroni_t)
    tr = res["trades"]
    m["entries"] = int(len(tr))
    m["skipped_full"], m["skipped_no_price"] = res["skipped_full"], res["skipped_price"]
    m["avg_positions"] = float(res["npos"].mean())
    if len(tr):
        m["trade_mean_ret"] = float(tr["ret"].mean())
        m["trade_median_ret"] = float(tr["ret"].median())
        m["trade_win_rate"] = float((tr["ret"] > 0).mean())
        m["exits_delisted"] = int((tr["exit_kind"] == "delisted").sum())
        m["exits_data_end"] = int((tr["exit_kind"] == "data_end").sum())
    return m


def bhar_table(ev: pd.DataFrame, sessions: pd.DatetimeIndex, bench: dict, assets: dict, h1_end: str) -> pd.DataFrame:
    """Event-time buy-and-hold returns (ledger 0.7): stock vs ONEQ and vs the size ETF, 6/12/24 months,
    from the close of regular-way day 5 (and day 1). After a delisting the money sits in the benchmark. Events
    entered on or before ``h1_end`` are half H1."""
    rows = []
    lv = {k: (1 + v["ret"]).cumprod().to_numpy() for k, v in bench.items()}
    for r in ev[(ev.classification == "spin_off") & (ev.first_regular_way_date != "")].itertuples():
        mcap = float(r.mcap_usd) if r.mcap_usd else float("nan")
        size_etf = "IWM" if mcap != mcap else next(e for cap, e in SIZE_ETF if mcap < cap)
        for who in ("spinco", "parent"):
            cik = r.spinco_cik if who == "spinco" else r.parent_cik
            if cik != cik or f"{who}_{int(cik)}" not in assets:
                continue
            a = assets[f"{who}_{int(cik)}"]
            for day in (1, 5):
                i0 = regular_way_index(sessions, r.first_regular_way_date, day)
                if i0 is None or not a.px[i0] == a.px[i0]:
                    continue
                for h in (6, 12, 24):
                    i1 = int(sessions.searchsorted(add_months(sessions[i0], h), side="left"))
                    if i1 >= len(sessions):
                        continue
                    seg = a.ret[i0 + 1: i1 + 1].copy()
                    out = {}
                    for b in ("ONEQ", size_etf):
                        bl = lv[b]
                        if a.last_i < i1:   # delisted (or data ends) inside the window
                            j = max(a.last_i, i0)
                            term = a.terminal if a.terminal is not None else 0.0
                            stock = np.prod(1 + a.ret[i0 + 1: j + 1]) * (1 + term) * bl[i1] / bl[j] - 1
                        else:
                            stock = float(np.prod(1 + seg)) - 1
                        out[b] = (stock, bl[i1] / bl[i0] - 1)
                    rows.append({"event_id": r.event_id, "who": who, "entry_day": day, "months": h,
                                 "half": "H1" if sessions[i0] <= pd.Timestamp(h1_end) else "H2",
                                 "nasdaq": r.nasdaq_listed, "size_etf": size_etf,
                                 "ret": out["ONEQ"][0], "oneq": out["ONEQ"][1], "size_bench": out[size_etf][1],
                                 "ret_sizepath": out[size_etf][0], "delisted_in_window": bool(a.last_i < i1)})
    t = pd.DataFrame(rows)
    if len(t):
        t["bhar_oneq"] = t["ret"] - t["oneq"]
        t["bhar_size"] = t["ret_sizepath"] - t["size_bench"]
    return t
