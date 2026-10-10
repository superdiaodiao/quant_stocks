"""Nasdaq-100 reconstitution rules R1 / R2 / R4 (docs/research_ledger_ndx_recon.md section 0.4): event plans, the
daily slot engine at the close (idle money in ONEQ), event-time abnormal returns, and the rule metrics.

Moved unchanged from studies/ndx_recon.py in the slimming pass (``Asset``, ``simulate``, ``sidx``,
``r1_eligible`` / ``r2_eligible``, ``build_plan``, ``car_table``, ``metrics``). The R2 holding period and the CAR
post window are arguments; the event table is quant.data.ndx_events.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from quant.backtest.costs import ibkr_order_total as order_cost, volume_tier_half_spread as half_spread
from quant.evaluation.stats import mean_t_n_se
from quant.strategies.spinoffs import slot_book_metrics

ACCOUNT = 10_000.0
SLOTS = 10
ONEQ_HALF_SPREAD = 2e-4


# ============================================================================== engine

class Asset:
    def __init__(self, df: pd.DataFrame, sessions: pd.DatetimeIndex):
        s = df.set_index("date")
        s = s[~s.index.duplicated(keep="last")]
        self.px = s["close_raw"].reindex(sessions).to_numpy(float)
        self.ret = s["tr"].reindex(sessions).fillna(0.0).to_numpy(float)
        dv = (s["close_raw"] * s["volume_raw"]).rolling(20, min_periods=1).median()
        self.dv = dv.reindex(sessions).ffill().to_numpy(float)
        rows = np.where(~np.isnan(self.px))[0]
        self.last_i = int(rows[-1]) if len(rows) else -1


def simulate(plan: list[dict], assets: dict, sessions: pd.DatetimeIndex, oneq_ret: np.ndarray,
             oneq_px: np.ndarray, spread_mult: float = 1.0, slots: int = SLOTS) -> dict:
    """Daily engine at the close (ledger 0.4). ``plan``: dicts with ``key`` (event id), ``entry_i``, ``exit_i``,
    ``order``. Each entry buys NAV/slots of the name in whole shares, funded by selling ONEQ; exits at the
    planned close (sells before buys on the same day); up to ``slots`` names; idle money in ONEQ."""
    n = len(sessions)
    by_day: dict[int, list[dict]] = {}
    for c in plan:
        by_day.setdefault(c["entry_i"], []).append(c)
    pos: dict[str, dict] = {}
    cash, oneq_sh, oneq_val = ACCOUNT, 0.0, 0.0
    navs, costs, expo, npos = np.zeros(n), np.zeros(n), np.zeros(n), np.zeros(n, int)
    trades, skipped_full, skipped_price, traded = [], 0, 0, 0.0

    def oneq_trade(dollars: float, i: int) -> float:
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

    for i in range(n):
        day_cost = 0.0
        if i > 0 and oneq_sh:
            gross = oneq_sh * oneq_px[i - 1] * (1.0 + oneq_ret[i])
            oneq_val = oneq_sh * oneq_px[i]
            cash += gross - oneq_val
        for k, p in list(pos.items()):
            a = assets[k]
            if i > p["entry_i"]:
                p["val"] *= 1.0 + a.ret[i]
                p["oneq"] *= 1.0 + oneq_ret[i]
            if i >= p["exit_i"] or i > a.last_i:
                last = min(i, a.last_i)
                price = a.px[last]
                sh = p["sh"]
                c = order_cost(sh, price, True, half_spread(a.dv[last], price, spread_mult))
                cash += p["val"] - c
                day_cost += c
                traded += p["val"]
                ret = (p["val"] - c) / p["inv"] - 1
                trades.append({**p["info"], "exit": sessions[i].date().isoformat(),
                               "exit_kind": "planned" if i >= p["exit_i"] and i <= a.last_i else "data_end",
                               "ret": ret, "oneq_ret": p["oneq"] - 1, "excess": ret - (p["oneq"] - 1),
                               "pnl": p["val"] - c - p["inv"], "days": i - p["entry_i"]})
                del pos[k]
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        for c_ in sorted(by_day.get(i, []), key=lambda c: c["order"]):
            k = c_["key"]
            a = assets.get(k)
            if k in pos:
                continue
            if len(pos) >= slots:
                skipped_full += 1
                continue
            if a is None or not a.px[i] == a.px[i] or c_["exit_i"] <= i:
                skipped_price += 1
                continue
            price = a.px[i]
            sh = math.floor(nav / slots / price)
            if sh < 1:
                skipped_price += 1
                continue
            c = order_cost(sh, price, False, half_spread(a.dv[i], price, spread_mult))
            if cash < sh * price + c:
                day_cost += oneq_trade(-(sh * price + c - cash) * 1.01, i)
            cash -= sh * price + c
            day_cost += c
            traded += sh * price
            pos[k] = {"val": sh * price, "sh": sh, "entry_i": i, "exit_i": c_["exit_i"], "inv": sh * price + c,
                      "oneq": 1.0, "info": {"event_id": k, "rule_leg": c_.get("leg", ""),
                                            "entry": sessions[i].date().isoformat()}}
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        if cash > 0.02 * nav:
            day_cost += oneq_trade(cash - 0.005 * nav, i)
        nav = cash + oneq_val + sum(p["val"] for p in pos.values())
        navs[i], costs[i] = nav, day_cost
        expo[i] = sum(p["val"] for p in pos.values()) / nav if nav > 0 else 0.0
        npos[i] = len(pos)
    for k, p in pos.items():
        trades.append({**p["info"], "exit": "", "exit_kind": "open", "ret": p["val"] / p["inv"] - 1,
                       "oneq_ret": p["oneq"] - 1, "excess": p["val"] / p["inv"] - p["oneq"],
                       "pnl": p["val"] - p["inv"], "days": n - 1 - p["entry_i"]})
    return {"nav": pd.Series(navs, index=sessions), "cost": pd.Series(costs, index=sessions),
            "exposure": pd.Series(expo, index=sessions), "npos": pd.Series(npos, index=sessions),
            "trades": pd.DataFrame(trades), "traded": traded, "skipped_full": skipped_full,
            "skipped_price": skipped_price}


# ============================================================================== plans and diagnostics

def sidx(sessions: pd.DatetimeIndex, d: str) -> int | None:
    if not d:
        return None
    i = int(sessions.searchsorted(pd.Timestamp(d), side="left"))
    return i if i < len(sessions) and sessions[i] == pd.Timestamp(d) else None


def r1_eligible(e) -> bool:
    return e.side == "add" and e.type == "announced" and bool(e.buy_session) and bool(e.effective_date)


def r2_eligible(e) -> bool:
    return e.side == "delete" and e.type == "index" and bool(e.effective_date)


def build_plan(ev: pd.DataFrame, sessions: pd.DatetimeIndex, start: str, end: str, legs: tuple,
               hold_r2: int) -> list[dict]:
    """R1: buy at the close of the buy session, sell at the effective date; R2: buy an index deletion at the
    effective date, hold ``hold_r2`` sessions; entries inside [start, end] only."""
    plan = []
    for e in ev.itertuples():
        if "R1" in legs and r1_eligible(e):
            i, j = sidx(sessions, e.buy_session), sidx(sessions, e.effective_date)
            if i is None or j is None or j <= i:
                continue
            leg = "R1"
        elif "R2" in legs and r2_eligible(e):
            i = sidx(sessions, e.effective_date)
            if i is None:
                continue
            j = i + hold_r2
            leg = "R2"
        else:
            continue
        if not (pd.Timestamp(start) <= sessions[i] <= pd.Timestamp(end)):
            continue
        plan.append({"key": e.event_id, "entry_i": i, "exit_i": min(j, len(sessions) - 1), "leg": leg,
                     "order": (sessions[i], leg, e.ticker)})
    return plan


def car_table(ev: pd.DataFrame, sessions: pd.DatetimeIndex, bench: dict, assets: dict,
              post_days: int) -> pd.DataFrame:
    """Event-time cumulative abnormal returns (sum of daily return minus benchmark return) per event:
    ann  close of the announcement session -> close of the effective date (announcement reaction + run-up)
    r1   close of the buy session (first session after the announcement) -> close of the effective date
    post close of the effective date -> close of +5 / +``post_days`` sessions."""
    rows = []
    for e in ev.itertuples():
        a = assets.get(e.event_id)
        if a is None or not e.effective_date:
            continue
        ie = sidx(sessions, e.effective_date)
        ia = sidx(sessions, e.ann_session) if e.ann_session else None
        ib = sidx(sessions, e.buy_session) if e.buy_session else None
        if ie is None:
            continue
        row = {"event_id": e.event_id, "group": e.group, "kind": e.kind, "side": e.side, "type": e.type,
               "ticker": e.ticker, "inclusion_date": e.inclusion_date, "half": "H1" if e.inclusion_date <= "2018-12-31" else "H2"}
        for b in ("QQQ", "ONEQ"):
            br = bench[b]["ret"].to_numpy(float)
            def car(i0, i1):     # returns of sessions i0+1 .. i1
                if i0 is None or i1 is None or i1 <= i0 or i1 > a.last_i:
                    return np.nan
                if np.isnan(a.px[i0]) or np.isnan(a.px[i1]):
                    return np.nan
                return float(np.sum(a.ret[i0 + 1:i1 + 1] - br[i0 + 1:i1 + 1]))
            row[f"ann_{b}"] = car(ia, ie)
            row[f"r1_{b}"] = car(ib, ie)
            row[f"post5_{b}"] = car(ie, ie + 5) if ie + 5 < len(sessions) else np.nan
            row[f"post20_{b}"] = car(ie, ie + post_days) if ie + post_days < len(sessions) else np.nan
        row["days_ann_to_eff"] = (ie - ia) if ia is not None else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def metrics(res: dict, bench: dict, bonferroni_t: float) -> dict:
    """``slot_book_metrics`` (vs ONEQ, criteria A / B) plus the CAGR excess, ``t_excess_monthly >= bonferroni_t``,
    entries / skips, average exposure and the per-trade excess vs ONEQ (``mean_t_n_se``)."""
    m = slot_book_metrics(res, bench, ACCOUNT)
    m["excess_cagr"] = m["cagr"] - m["bench_cagr"]
    m["t_ge_bonferroni"] = bool(m["t_excess_monthly"] >= bonferroni_t)
    tr = res["trades"]
    m["entries"] = int(len(tr))
    m["skipped_full"], m["skipped_no_price"] = res["skipped_full"], res["skipped_price"]
    m["avg_exposure"] = float(res["exposure"].mean())
    if len(tr):
        mm, tt, nn = mean_t_n_se(tr["excess"])
        m["trade_mean_ret"] = float(tr["ret"].mean())
        m["trade_mean_excess_vs_oneq"] = mm
        m["trade_t_excess"] = tt
        m["trade_median_excess"] = float(tr["excess"].median())
        m["trade_win_vs_oneq"] = float((tr["excess"] > 0).mean())
        m["trade_mean_pnl_usd"] = float(tr["pnl"].mean())
        m["trade_mean_days"] = float(tr["days"].mean())
    return m


def segment_assets(assets: dict, sessions: pd.DatetimeIndex, seg: pd.DatetimeIndex) -> dict:
    """The assets cut to the sub-calendar ``seg`` of ``sessions`` (last row index clipped to the segment)."""
    seg_assets = {}
    for k, a in assets.items():
        b = Asset.__new__(Asset)
        lo = sessions.get_loc(seg[0])
        hi = sessions.get_loc(seg[-1]) + 1
        b.px, b.ret, b.dv = a.px[lo:hi], a.ret[lo:hi], a.dv[lo:hi]
        b.last_i = min(a.last_i - lo, hi - lo - 1) if a.last_i >= lo else -1
        seg_assets[k] = b
    return seg_assets
