"""Connors-style short-term mean reversion (RSI(2) pullback in an uptrend): the QQQ state machine and ETF engine
(rules R1 / R2) and the stock engine (R3 / R4) of studies/mean_reversion.py.

Extracted unchanged from studies/mean_reversion.py in the slimming pass (``connors_state``, ``load_etf``,
``etf_weights``, ``etf_sim``, ``episodes``, ``etf_trade_stats``, ``StockInputs``, ``stock_sim``, ``benchmarks``,
``stock_result``). The frozen parameters (RSI / SMA lengths, entry thresholds, slots, time stop, account,
half-spreads) are ``Params``, given by the study; ``rsi2`` became ``indicators.rsi(p, cfg.rsi_n)``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import etf_order_cost, rank_half_spread
from quant.backtest.target_weights import two_state
from quant.data import etf_panel
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart
from quant.signals.indicators import sma
from quant.strategies import canslim as cs
from quant.strategies import indicators as ind
from quant.strategies import livermore as lv


@dataclass(frozen=True)
class Params:
    rsi_n: int                    # RSI(n)
    sma_long: int                 # trend filter: close > SMA(long)
    sma_exit: int                 # exit: close > SMA(exit)
    qqq_entry_rsi: float          # R1 / R2 entry: RSI < this
    stock_entry_rsi: float        # R3 / R4 entry: RSI < this
    max_hold: int                 # sessions after the entry fill
    k: int                        # stock slots
    slot: float                   # share of equity per slot
    n_universe: int               # weekly dv50 top-N universe
    account: float
    etf_half_spread: dict         # per ETF leg (CASH free)
    oneq_half_spread: float


# ======================================================================== signals (causal)

def connors_state(p: pd.Series, cfg: Params) -> pd.Series:
    """Decision state at each close: 1 in a trade, 0 flat, NaN before the indicators exist (entry: P > SMA(long)
    and RSI(n) < ``cfg.qqq_entry_rsi``; exit: P > SMA(exit))."""
    entry_rsi = cfg.qqq_entry_rsi
    long_ma, exit_ma, r = sma(p, cfg.sma_long), sma(p, cfg.sma_exit), ind.rsi(p, cfg.rsi_n)
    defined = (long_ma.notna() & exit_ma.notna() & r.notna()).to_numpy()
    entry = ((p > long_ma) & (r < entry_rsi)).to_numpy()
    exit_ = (p > exit_ma).to_numpy()
    out = np.full(len(p), np.nan)
    s = None
    for i in range(len(p)):
        if not defined[i]:
            if s is not None:
                out[i] = s
            continue
        if s is None:
            s = 0.0
        if s == 0.0 and entry[i]:
            s = 1.0
        elif s == 1.0 and exit_[i]:
            s = 0.0
        out[i] = s
    return pd.Series(out, index=p.index)


# ======================================================================== QQQ rules

def load_etf(end: str, qld_path: Path):
    """The ETF panel (quant.data.etf_panel) plus QLD from ``qld_path``, truncated at ``end`` and asserted."""
    d = etf_panel.load_data(end)
    q = parse_chart(qld_path, end)
    assert_dev_dates(q["date"], end)
    ix = pd.DatetimeIndex(q["date"])
    extra = ix.difference(d.sessions)
    if len(extra):
        raise ValueError(f"QLD has sessions not in the QQQ calendar: {list(extra[:5])}")
    d.adj["QLD"] = pd.Series(q["adjclose"].values, index=ix).reindex(d.sessions)
    d.close["QLD"] = pd.Series(q["close"].values, index=ix).reindex(d.sessions)
    listed = d.sessions[d.sessions >= ix[0]]
    missing = int(d.adj["QLD"].reindex(listed).isna().sum())
    if missing:
        raise ValueError(f"QLD misses {missing} QQQ sessions after listing")
    d.rets["QLD"] = d.adj["QLD"].pct_change(fill_method=None)
    d.guard["QLD"] = {"first": q["date"].iloc[0], "last": q["date"].iloc[-1], "rows": int(len(q)),
                      "missing_sessions_after_listing": missing}
    return d


def etf_weights(rule: str, state: pd.Series) -> pd.DataFrame:
    if rule == "R1":
        on, off, cols = {"QQQ": 1.0}, {"CASH": 1.0}, ("QQQ", "CASH")
    elif rule == "R2":
        on, off, cols = {"QQQ": 0.5, "QLD": 0.5}, {"QQQ": 1.0}, ("QQQ", "QLD")
    else:
        raise KeyError(rule)
    return two_state(state, on, off, cols)


def etf_sim(w: pd.DataFrame, rets: pd.DataFrame, close: pd.DataFrame, start_i: int, cfg: Params, lag: int = 1,
            costs: bool = True) -> dict:
    """Same mechanics as quant.backtest.target_weights.simulate_target_weights, plus a cost-free CASH leg and the
    pre-trade value per session; ``cfg.account`` start, ``cfg.etf_half_spread`` per leg."""
    cols = list(w.columns)
    W, R, P = w.values, rets[cols].values, close[cols].values
    end_i = len(w) - 1
    h = np.zeros(len(cols))
    cash = cfg.account
    last = None
    values, pre, execd = [], [], []
    trades = orders = 0
    cost_frac = 0.0
    for i in range(start_i, end_i + 1):
        if i > start_i:
            r = R[i]
            if np.any(np.isnan(r[h > 0])):
                raise ValueError(f"missing return for a held asset on {w.index[i].date()}")
            h = h * (1 + np.nan_to_num(r))
        total = h.sum() + cash
        pre.append(total)
        tgt = W[i - lag]
        if np.any(np.isnan(tgt)):
            raise ValueError(f"no target on {w.index[i - lag].date()}")
        if last is None or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            for k in range(len(cols)):
                if cols[k] == "CASH" or abs(delta[k]) <= 1.0:
                    continue
                if costs:
                    c += etf_order_cost(abs(delta[k]), P[i, k], delta[k] < 0, cfg.etf_half_spread[cols[k]])
                orders += 1
            if last is not None:
                trades += 1
            h = new * (1 - c / total)
            cash = 0.0
            cost_frac += c / total
            last = tgt.copy()
        values.append(h.sum() + cash)
        execd.append(last.copy())
    idx = w.index[start_i:end_i + 1]
    v = pd.Series(values, index=idx)
    return {"value": v, "pre": pd.Series(pre, index=idx), "ret": v.pct_change().iloc[1:],
            "executed": pd.DataFrame(execd, index=idx, columns=cols), "switches": trades, "orders": orders,
            "cost_frac": cost_frac}


def episodes(in_state: pd.Series) -> list:
    """(entry fill i, exit fill i) positions of completed trades; ``in_state`` = executed state after each close."""
    s = in_state.to_numpy().astype(int)
    out, e = [], (0 if s[0] == 1 else None)
    for i in range(1, len(s)):
        if s[i] == 1 and s[i - 1] == 0:
            e = i
        elif s[i] == 0 and s[i - 1] == 1 and e is not None:
            out.append((e, i))
            e = None
    return out


def etf_trade_stats(sim: dict, in_state: pd.Series, default_ret: pd.Series) -> dict:
    eps = episodes(in_state)
    rows = []
    for e, x in eps:
        tr = sim["value"].iloc[x] / sim["pre"].iloc[e] - 1
        span = default_ret.iloc[e + 1:x + 1]
        base = float((1 + span.fillna(0)).prod() - 1)
        rows.append({"entry": str(in_state.index[e].date()), "exit": str(in_state.index[x].date()),
                     "sessions": x - e, "ret": tr, "default_ret": base, "win": bool(tr > base)})
    t = pd.DataFrame(rows)
    held_days = in_state.iloc[:-1]
    return {"trades": int(len(t)), "win_rate": float(t["win"].mean()) if len(t) else float("nan"),
            "avg_trade_ret": float(t["ret"].mean()) if len(t) else float("nan"),
            "avg_trade_minus_default": float((t["ret"] - t["default_ret"]).mean()) if len(t) else float("nan"),
            "median_sessions": float(t["sessions"].median()) if len(t) else float("nan"),
            "open_at_end": bool(in_state.iloc[-1] == 1), "time_in_market": float(held_days.mean()),
            "_trades": t}


# ======================================================================== stock rules

class StockInputs:
    """Signals, eligibility and benchmarks for one window (every frame from ``quant.data.panel.load_window``)."""

    def __init__(self, data, cfg: Params):
        self.data = data
        u = lv.eligible_universe(data)
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in u.groupby("week_end")}
        top = u[u["dv50_rank"] <= cfg.n_universe]
        self.elig = {t: list(g["security_id"]) for t, g in top.groupby("week_end")}
        for t in self.rank_of:
            self.elig.setdefault(t, [])
        self.carried_weeks = int(u.loc[u["carried"], "week_end"].nunique())
        p = data.sig_idx
        self.rsi = ind.rsi(p, cfg.rsi_n)
        self.entry = (p > sma(p, cfg.sma_long)) & (self.rsi < cfg.stock_entry_rsi)
        self.exit = p > sma(p, cfg.sma_exit)
        lvl, px = ind.oneq_on_sessions(data.sessions, data.spec["price_start"], data.spec["effective_end"])
        self.oneq_lvl, self.oneq_px = lvl, px


def stock_sim(inp: StockInputs, idle: str, cfg: Params, lag: int = 1, spread_mult: float = 1.0) -> dict:
    """R3 / R4 engine (``idle`` "cash" or "oneq"): orders at the close of t+1 (``lag`` 1) or t (0), up to ``cfg.k``
    names at ``cfg.slot`` of equity, whole shares, time stop ``cfg.max_hold`` sessions after the entry fill."""
    account, k_max, slot, max_hold, oneq_hs = cfg.account, cfg.k, cfg.slot, cfg.max_hold, cfg.oneq_half_spread
    data = inp.data
    sessions = data.sessions
    perf = sessions[(sessions >= pd.Timestamp(data.spec["perf_start"])) &
                    (sessions <= pd.Timestamp(data.spec["effective_end"]))]
    cs.assert_window(perf, data.spec["perf_start"], data.spec["effective_end"], "simulation sessions")
    cols = list(data.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    E = inp.entry.reindex(index=perf, columns=cols).fillna(False).to_numpy()
    X = inp.exit.reindex(index=perf, columns=cols).fillna(False).to_numpy()
    RS = inp.rsi.reindex(index=perf, columns=cols).to_numpy()
    OL = inp.oneq_lvl.reindex(perf).to_numpy()
    OP = inp.oneq_px.reindex(perf).to_numpy()
    lr = data.last_row.reindex(cols).to_numpy()
    weeks = sorted(inp.elig)
    wk_pos = np.searchsorted(np.array(weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1

    cash, o_units = account, 0.0
    pos: dict = {}
    pend_sell: dict = {}
    pend_buy: list = []
    navs, expo, costs, inmkt = (np.zeros(len(perf)) for _ in range(4))
    traded = 0.0
    counts = {"buy": 0, "sell_sma5": 0, "sell_time": 0, "delisted": 0, "oneq_orders": 0, "skipped_no_cash": 0}
    trades = []

    def hs(sid, price, i):
        w = weeks[wk_pos[i]] if wk_pos[i] >= 0 else None
        return rank_half_spread(inp.rank_of.get(w, {}).get(sid, np.nan), price, spread_mult)

    def stock_value(i):
        return sum(p["units"] * I[i, col[s]] for s, p in pos.items())

    def decide(i, n_after_sells):
        """Exit and entry decisions at the close of perf[i]."""
        d = perf[i]
        sells = {}
        for sid, p in pos.items():
            c = col[sid]
            held = i - p["entry_i"]          # 0 on the fill day (lag 1): the exit rule is checked there too
            if X[i, c]:
                sells[sid] = "sma5"
            elif held >= max_hold - lag:     # lag 1: decided on session 9, filled on session 10 after entry
                sells[sid] = "time"
        slots = k_max - (n_after_sells - len(sells))
        buys = []
        if slots > 0 and wk_pos[i] >= 0:
            cand = []
            for sid in inp.elig[weeks[wk_pos[i]]]:
                c = col.get(sid)
                if c is None or sid in pos or not E[i, c]:
                    continue
                if pd.notna(lr[c]) and (lr[c] <= d if lag == 1 else lr[c] < d):
                    continue
                cand.append((RS[i, c], sid))
            buys = [s for _, s in sorted(cand)[:slots]]
        return sells, buys

    def execute(i, sells, buys):
        nonlocal cash, o_units, traded
        day_cost, acted = 0.0, False
        for sid, reason in sorted(sells.items()):
            if sid not in pos:
                continue
            p = pos.pop(sid)
            c = col[sid]
            val = p["units"] * I[i, c]
            k = cs.order_cost(val, P[i, c], True, hs(sid, P[i, c], i))
            cash += val - k
            day_cost += k
            traded += val
            counts[f"sell_{reason}"] += 1
            trades.append({"sid": sid, "entry": str(perf[p["entry_i"]].date()), "exit": str(perf[i].date()),
                           "reason": reason, "ret": (val - k) / p["paid"] - 1, "sessions": i - p["entry_i"],
                           "shares": p["shares"]})
            acted = True
        nav_now = cash + o_units * OL[i] + stock_value(i)
        for sid in buys:
            if len(pos) >= k_max or sid in pos:
                continue
            c = col[sid]
            d = perf[i]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[c]) and lr[c] < d):
                continue
            px = P[i, c]
            sh = math.floor(slot * nav_now / px)
            avail = cash + (o_units * OL[i] * (1 - 2 * oneq_hs) - 1.0 if idle == "oneq" else 0.0)
            while sh >= 1 and sh * px + cs.order_cost(sh * px, px, False, hs(sid, px, i)) > avail:
                sh = math.floor(min(sh - 1, (avail - 1.0) / px))
            if sh < 1:
                counts["skipped_no_cash"] += 1
                continue
            amt = sh * px
            k = cs.order_cost(amt, px, False, hs(sid, px, i))
            cash -= amt + k
            day_cost += k
            traded += amt
            pos[sid] = {"units": amt / I[i, c], "entry_i": i, "paid": amt + k, "shares": sh}
            counts["buy"] += 1
            acted = True
        return day_cost, acted

    def park(i, force):
        """R4: move idle money into ONEQ (whole shares) after the day's stock orders."""
        nonlocal cash, o_units, traded
        if idle != "oneq" or not (force or cash < 0 or cash > OP[i]):
            return 0.0
        cur_val = o_units * OL[i]
        total = cash + cur_val
        tgt_sh = math.floor(total / OP[i])
        cur_sh = cur_val / OP[i]
        while True:
            delta_val = tgt_sh * OP[i] - cur_val
            k = cs.order_cost(abs(delta_val), OP[i], delta_val < 0, oneq_hs) if abs(delta_val) > 0.01 else 0.0
            if total - tgt_sh * OP[i] - k >= 0 or tgt_sh <= 0:
                break
            tgt_sh -= 1
        if abs(tgt_sh - cur_sh) < 0.5 and cash >= 0:
            return 0.0
        delta_val = tgt_sh * OP[i] - cur_val
        k = cs.order_cost(abs(delta_val), OP[i], delta_val < 0, oneq_hs)
        o_units += delta_val / OL[i]
        cash -= delta_val + k
        traded += abs(delta_val)
        counts["oneq_orders"] += 1
        return k

    for i, d in enumerate(perf):
        day_cost, acted = 0.0, False
        for sid in [s for s in pos if pd.notna(lr[col[s]]) and d > lr[col[s]]]:
            p = pos.pop(sid)
            val = p["units"] * I[i, col[sid]]
            cash += val
            counts["delisted"] += 1
            trades.append({"sid": sid, "entry": str(perf[p["entry_i"]].date()), "exit": str(d.date()),
                           "reason": "delisted", "ret": val / p["paid"] - 1, "sessions": i - p["entry_i"],
                           "shares": p["shares"]})
            pend_sell.pop(sid, None)
            acted = True
        if lag == 1:
            c1, a1 = execute(i, pend_sell, pend_buy)
            day_cost += c1
            acted |= a1
            pend_sell, pend_buy = {}, []
            if i < len(perf) - 1:
                pend_sell, pend_buy = decide(i, len(pos))
        else:
            if i < len(perf) - 1:
                s, b = decide(i, len(pos))
                c1, a1 = execute(i, s, b)
                day_cost += c1
                acted |= a1
        day_cost += park(i, force=(i == 0) or acted)
        sv = stock_value(i)
        navs[i] = cash + o_units * OL[i] + sv
        expo[i] = sv / navs[i] if navs[i] > 0 else 0.0
        inmkt[i] = 1.0 if pos else 0.0
        costs[i] = day_cost
    return {"dates": perf, "nav": pd.Series(navs, index=perf), "exposure": pd.Series(expo, index=perf),
            "in_market": pd.Series(inmkt, index=perf), "cost": pd.Series(costs, index=perf), "traded": traded,
            "counts": counts, "trades": pd.DataFrame(trades), "open": sorted(pos)}


def benchmarks(inp: StockInputs, dates, account: float, oneq_hs: float):
    """ONEQ (one buy order) and QQQ buy-and-hold NAVs of ``account`` dollars on ``dates``."""
    q = inp.oneq_lvl.reindex(dates)
    kc = cs.order_cost(account, float(inp.oneq_px.reindex(dates).iloc[0]), False, oneq_hs)
    oneq = (account - kc) * q / q.iloc[0]
    qqq = cs.qqq_benchmark(inp.data.qqq_perf_idx, inp.data.qqq_close, dates, account)
    return oneq, qqq


def stock_result(inp: StockInputs, idle: str, cfg: Params, **kw) -> tuple[dict, dict]:
    res = stock_sim(inp, idle, cfg, **kw)
    oneq, qqq = benchmarks(inp, res["dates"], cfg.account, cfg.oneq_half_spread)
    m = ind.stock_metrics(res["nav"], oneq, qqq, cfg.account, res["cost"], res["traded"], res["exposure"])
    t = res["trades"]
    m["closed_trades"] = int(len(t))
    m["win_rate"] = float((t["ret"] > 0).mean()) if len(t) else float("nan")
    m["avg_trade_ret"] = float(t["ret"].mean()) if len(t) else float("nan")
    m["median_sessions"] = float(t["sessions"].median()) if len(t) else float("nan")
    m["time_in_market"] = float(res["in_market"].mean())
    m.update({f"n_{k}": v for k, v in res["counts"].items()})
    res["oneq"], res["qqq"] = oneq, qqq
    return m, res
