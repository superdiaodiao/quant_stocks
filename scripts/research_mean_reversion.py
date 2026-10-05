"""Connors-style short-term daily mean reversion (RSI(2) pullback in an uptrend).

Pre-registered in docs/research_ledger_mean_reversion.md, section 0 (written before any result). Four rules, run once:

- R1 (QQQ):  flat -> in when QQQ > SMA200 and RSI(2) < 10; in -> flat when QQQ > SMA5. In = 100% QQQ, flat = cash
             earning the T-bill rate (no cost to switch into cash).
- R2 (QQQ, leveraged): same state machine; in = 50% QQQ + 50% QLD (1.5x), flat = 100% QQQ.
- R3 (stocks): weekly dv50 top-100 Nasdaq common stocks (frozen data version 1). Entry close > SMA200 and RSI(2) < 5;
             exit close > SMA5 or at the latest at the close of the 10th session after the entry fill. Up to 5
             names at 20% of equity, whole shares, lowest RSI(2) first. Idle money = cash at 0%.
- R4 (stocks): R3 with idle money parked in ONEQ (whole shares, adjusted only on days with stock fills).

Timing: every signal uses closes up to t; orders fill at the close of t+1 (MOC). Same-close fills are a report-only
sensitivity. Costs: IBKR Pro Tiered (ETF model of scripts/research_regime.py; stock model of
scripts/research_reversal_dev.py). Benchmark: ONEQ total return. Outputs carry returns and metrics only.
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
from scripts import research_regime as rr  # noqa: E402  (ETF loader, metrics, A/B criteria)
from scripts import research_qqq_timing as qt  # noqa: E402  (Yahoo parser, date guard, cost constants)
from scripts import research_livermore as lv  # noqa: E402  (stock window loader, eligible universe)
from scripts import research_canslim_dev as cs  # noqa: E402  (order cost, QQQ benchmark, window guard)
from scripts import research_reversal_dev as rev  # noqa: E402  (half spread, terminal D5 values)
from scripts import research_indicators as ind  # noqa: E402  (rsi, ONEQ on sessions, stock metrics, criteria)

END = "2026-09-30"
OUT = ROOT / "output/research_only/mean_reversion"
QLD_PATH = Path("/Users/bytedance/code/quant_stocks/research_cache/qqq_timing/raw/chart_QLD.json")
ETF_HALF_SPREAD = {"QQQ": 1e-4, "QLD": 2e-4, "ONEQ": 2e-4, "CASH": 0.0}
ONEQ_HALF_SPREAD = 2e-4

RSI_N = 2
SMA_LONG = 200
SMA_EXIT = 5
QQQ_ENTRY_RSI = 10.0
STOCK_ENTRY_RSI = 5.0
MAX_HOLD = 10            # sessions after the entry fill
K = 5
SLOT = 0.20
N_UNIVERSE = 100
ACCOUNT = 10_000.0
N_TRIALS = 4

STOCK_WINDOWS = {
    "F1": {"perf_start": "2012-04-02", "perf_end": "2018-12-31", "price_start": "2011-06-01",
           "universe_start": "2012-01-01", "judged_from": "2012-04-02"},
    "F2": {"perf_start": "2019-01-02", "perf_end": "2026-08-31", "price_start": "2017-06-01",
           "universe_start": "2018-12-01", "judged_from": "2019-01-02"},
}
for _k, _v in STOCK_WINDOWS.items():
    lv.WINDOWS[f"mr_{_k}"] = _v


# ======================================================================== signals (causal)

def sma(x, n: int):
    return x.rolling(n, min_periods=n).mean()


def rsi2(p):
    return ind.rsi(p, RSI_N)


def connors_state(p: pd.Series, entry_rsi: float = QQQ_ENTRY_RSI) -> pd.Series:
    """Decision state at each close: 1 in a trade, 0 flat, NaN before the indicators exist."""
    long_ma, exit_ma, r = sma(p, SMA_LONG), sma(p, SMA_EXIT), rsi2(p)
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

def load_etf(end: str = END):
    d = rr.load_data(end)
    q = qt.parse_chart(QLD_PATH, end)
    qt.assert_dev_dates(q["date"], end)
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
    return rr.two_state(state, on, off, cols)


def etf_sim(w: pd.DataFrame, rets: pd.DataFrame, close: pd.DataFrame, start_i: int, lag: int = 1,
            costs: bool = True) -> dict:
    """Same mechanics as research_regime.simulate, plus a cost-free CASH leg and the pre-trade value per session."""
    cols = list(w.columns)
    W, R, P = w.values, rets[cols].values, close[cols].values
    end_i = len(w) - 1
    h = np.zeros(len(cols))
    cash = ACCOUNT
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
                    c += rr.order_cost(abs(delta[k]), P[i, k], delta[k] < 0, ETF_HALF_SPREAD[cols[k]])
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


def run_etf(lines: list) -> dict:
    d = load_etf(END)
    print("DATE GUARD: every ETF frame truncated at", END, "and asserted;",
          {k: v["last"] for k, v in d.guard.items() if isinstance(v, dict)})
    p = d.adj["QQQ"]
    state = connors_state(p)
    rets = d.rets.copy()
    rets["CASH"] = d.rf
    close = d.close.copy()
    close["CASH"] = 1.0
    res, rows, by_year, mon = {}, [], {}, {}
    for rule, assets, default in (("R1", ("QQQ",), "CASH"), ("R2", ("QQQ", "QLD"), "QQQ")):
        w = etf_weights(rule, state)
        s_i = rr.entry_index(w, d, assets, False)
        sim = etf_sim(w, rets, close, s_i)
        bq, qq = rr.buy_hold("ONEQ", d, s_i), rr.buy_hold("QQQ", d, s_i)
        r, b, q = sim["ret"], bq["ret"], qq["ret"]
        assert r.index.equals(b.index) and r.index.equals(q.index)
        qt.assert_dev_dates(r.index, END)
        i1, i2 = rr.halves(r.index)
        full = rr.period_metrics(r, b, q, d.rf)
        h1 = rr.period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
        h2 = rr.period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
        crit = rr.evaluate(full, h1, h2)
        in_state = (sim["executed"]["QLD" if rule == "R2" else "QQQ"] > 0).astype(int)
        ts = etf_trade_stats(sim, in_state, rets[default].loc[in_state.index])
        years = len(r) / qt.TRADING_DAYS
        info = {"entry_close": str(d.sessions[s_i].date()), "full": full, "half1": h1, "half2": h2, "criteria": crit,
                "switches_per_year": sim["switches"] / years, "cost_drag_per_year": sim["cost_frac"] / years,
                **{k: v for k, v in ts.items() if not k.startswith("_")}}
        for tag, kw in (("same_close", dict(lag=0)), ("no_costs", dict(costs=False))):
            s2 = etf_sim(w, rets, close, s_i, **kw)
            info[f"sens_{tag}"] = {"cagr": qt.cagr_of(s2["ret"]), "max_dd": qt.max_drawdown(s2["value"]),
                                   "cagr_minus_oneq": qt.cagr_of(s2["ret"]) - full["oneq_cagr"]}
        if rule == "R1":
            r0 = rets.copy()
            r0["CASH"] = 0.0
            s3 = etf_sim(w, r0, close, s_i)
            info["sens_cash_0pct"] = {"cagr": qt.cagr_of(s3["ret"]), "max_dd": qt.max_drawdown(s3["value"]),
                                      "cagr_minus_oneq": qt.cagr_of(s3["ret"]) - full["oneq_cagr"]}
        res[rule] = info
        ts["_trades"].to_csv(OUT / f"trades_{rule}.csv", index=False, float_format="%.6f")
        for tag, pm in (("full", full), ("half1", h1), ("half2", h2)):
            rows.append({"rule": rule, "period": tag, **pm, "pass": crit["pass"] if tag == "full" else None})
        by_year[rule] = qt.yearly(r)
        by_year[f"ONEQ_from_{rule}_entry"] = qt.yearly(b)
        by_year[f"QQQ_from_{rule}_entry"] = qt.yearly(q)
        by_year[f"{rule}_time_in_market"] = in_state.iloc[:-1].groupby(in_state.index[:-1].year).mean()
        mon[rule] = qt.monthly(r)
        mon[f"ONEQ_from_{rule}_entry"] = qt.monthly(b)
        line = (f"{rule} {full['start']}..{full['end']} CAGR {full['cagr']:+.2%} (ONEQ {full['oneq_cagr']:+.2%}, "
                f"QQQ {full['qqq_cagr']:+.2%}) MDD {full['max_dd']:.1%} (ONEQ {full['oneq_max_dd']:.1%}) "
                f"t {full['t_monthly_excess_vs_oneq']:+.2f} trades {ts['trades']} win {ts['win_rate']:.0%} "
                f"in-market {ts['time_in_market']:.0%} pass={crit['pass']}")
        print(line)
        lines.append(line)
    pd.DataFrame(rows).to_csv(OUT / "etf_summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "etf_by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "etf_monthly_returns.csv", float_format="%.6f")
    return {"guard": d.guard, "rules": res}


# ======================================================================== stock rules

class StockInputs:
    """Signals, eligibility and benchmarks for one window (every frame from ``lv.load_window``)."""

    def __init__(self, data):
        self.data = data
        u = lv.eligible_universe(data)
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in u.groupby("week_end")}
        top = u[u["dv50_rank"] <= N_UNIVERSE]
        self.elig = {t: list(g["security_id"]) for t, g in top.groupby("week_end")}
        for t in self.rank_of:
            self.elig.setdefault(t, [])
        self.carried_weeks = int(u.loc[u["carried"], "week_end"].nunique())
        p = data.sig_idx
        self.rsi = rsi2(p)
        self.entry = (p > sma(p, SMA_LONG)) & (self.rsi < STOCK_ENTRY_RSI)
        self.exit = p > sma(p, SMA_EXIT)
        lvl, px = ind.oneq_on_sessions(data.sessions, data.spec["price_start"], data.spec["effective_end"])
        self.oneq_lvl, self.oneq_px = lvl, px


def stock_sim(inp: StockInputs, idle: str = "cash", lag: int = 1, spread_mult: float = 1.0,
              account: float = ACCOUNT) -> dict:
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
        return rev.half_spread(inp.rank_of.get(w, {}).get(sid, np.nan), price, spread_mult)

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
            elif held >= MAX_HOLD - lag:     # lag 1: decided on session 9, filled on session 10 after entry
                sells[sid] = "time"
        slots = K - (n_after_sells - len(sells))
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
            if len(pos) >= K or sid in pos:
                continue
            c = col[sid]
            d = perf[i]
            if not (np.isfinite(I[i, c]) and np.isfinite(P[i, c])) or (pd.notna(lr[c]) and lr[c] < d):
                continue
            px = P[i, c]
            sh = math.floor(SLOT * nav_now / px)
            avail = cash + (o_units * OL[i] * (1 - 2 * ONEQ_HALF_SPREAD) - 1.0 if idle == "oneq" else 0.0)
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
            k = cs.order_cost(abs(delta_val), OP[i], delta_val < 0, ONEQ_HALF_SPREAD) if abs(delta_val) > 0.01 else 0.0
            if total - tgt_sh * OP[i] - k >= 0 or tgt_sh <= 0:
                break
            tgt_sh -= 1
        if abs(tgt_sh - cur_sh) < 0.5 and cash >= 0:
            return 0.0
        delta_val = tgt_sh * OP[i] - cur_val
        k = cs.order_cost(abs(delta_val), OP[i], delta_val < 0, ONEQ_HALF_SPREAD)
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


def benchmarks(inp: StockInputs, dates, account: float = ACCOUNT):
    q = inp.oneq_lvl.reindex(dates)
    kc = cs.order_cost(account, float(inp.oneq_px.reindex(dates).iloc[0]), False, ONEQ_HALF_SPREAD)
    oneq = (account - kc) * q / q.iloc[0]
    qqq = cs.qqq_benchmark(inp.data.qqq_perf_idx, inp.data.qqq_close, dates, account)
    return oneq, qqq


def stock_result(inp: StockInputs, idle: str, **kw) -> tuple[dict, dict]:
    res = stock_sim(inp, idle=idle, **kw)
    oneq, qqq = benchmarks(inp, res["dates"])
    m = ind.stock_metrics(res["nav"], oneq, qqq, ACCOUNT, res["cost"], res["traded"], res["exposure"])
    t = res["trades"]
    m["closed_trades"] = int(len(t))
    m["win_rate"] = float((t["ret"] > 0).mean()) if len(t) else float("nan")
    m["avg_trade_ret"] = float(t["ret"].mean()) if len(t) else float("nan")
    m["median_sessions"] = float(t["sessions"].median()) if len(t) else float("nan")
    m["time_in_market"] = float(res["in_market"].mean())
    m.update({f"n_{k}": v for k, v in res["counts"].items()})
    res["oneq"], res["qqq"] = oneq, qqq
    return m, res


def run_stocks(lines: list) -> dict:
    out = {}
    year_rows, mon = [], {}
    for fold in STOCK_WINDOWS:
        data = lv.load_window(f"mr_{fold}")
        print(data.guard["assertion"])
        inp = StockInputs(data)
        stress = StockInputs(lv.load_window(f"mr_{fold}", terminal_awaiting=rev.TERMINAL_D5_STRESS))
        for rule, idle in (("R3", "cash"), ("R4", "oneq")):
            m, res = stock_result(inp, idle)
            crit = ind.criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
            sens = {}
            for tag, src, kw in (("spread2x", inp, dict(spread_mult=2.0)), ("same_close", inp, dict(lag=0)),
                                 ("terminal_minus100", stress, {})):
                ms, _ = stock_result(src, idle, **kw)
                sens[tag] = {"cagr": ms["cagr"], "excess_cagr": ms["excess_cagr"], "max_dd": ms["max_dd"],
                             "t_excess_monthly": ms["t_excess_monthly"]}
            if fold == "F1":
                sub = ind.stock_sub_metrics(res["nav"], res["oneq"], res["qqq"], "2014-01-01", res["cost"],
                                            res["exposure"])
                sens["judged_2014_2018_report_only"] = {k: sub[k] for k in ("cagr", "bench_cagr", "excess_cagr",
                                                                            "max_dd", "bench_max_dd",
                                                                            "t_excess_monthly")}
            key = f"{rule}_{fold}"
            out[key] = {"metrics": {k: v for k, v in m.items() if not k.startswith("_")}, "criteria": crit,
                        "sensitivities": sens, "carried_universe_weeks": inp.carried_weeks,
                        "open_positions_at_end": res["open"], "guard": data.guard}
            res["trades"].to_csv(OUT / f"trades_{key}.csv", index=False, float_format="%.6f")
            nav0 = pd.concat([pd.Series([ACCOUNT], index=[res["nav"].index[0] - pd.Timedelta(days=1)]), res["nav"]])
            bn0 = pd.concat([pd.Series([ACCOUNT], index=[res["oneq"].index[0] - pd.Timedelta(days=1)]), res["oneq"]])
            mon[key] = qt.monthly(nav0.pct_change().dropna())
            mon[f"ONEQ_{fold}"] = qt.monthly(bn0.pct_change().dropna())
            tim = res["in_market"].groupby(res["in_market"].index.year).mean()
            nyr = res["trades"].assign(y=pd.to_datetime(res["trades"]["exit"]).dt.year).groupby("y") \
                if len(res["trades"]) else None
            for y, v in m["by_year"].items():
                g = nyr.get_group(y) if nyr is not None and y in nyr.groups else pd.DataFrame(columns=["ret"])
                year_rows.append({"rule": rule, "fold": fold, "year": y, "strategy": v["strategy"], "oneq": v["oneq"],
                                  "excess": v["excess"], "time_in_market": float(tim.get(y, np.nan)),
                                  "trades_closed": int(len(g)),
                                  "win_rate": float((g["ret"] > 0).mean()) if len(g) else float("nan")})
            line = (f"{key} {m['start']}..{m['end']} CAGR {m['cagr']:+.2%} (ONEQ {m['bench_cagr']:+.2%}, QQQ "
                    f"{m['qqq_cagr']:+.2%}) MDD {m['max_dd']:.1%} (ONEQ {m['bench_max_dd']:.1%}) t "
                    f"{m['t_excess_monthly']:+.2f} trades {m['closed_trades']} win {m['win_rate']:.0%} in-market "
                    f"{m['time_in_market']:.0%} cost {m['cost_drag_per_year']:.2%}/yr pass={crit['pass']}")
            print(line, flush=True)
            lines.append(line)
    for rule in ("R3", "R4"):
        f1, f2 = out[f"{rule}_F1"]["criteria"]["pass"], out[f"{rule}_F2"]["criteria"]["pass"]
        out[f"{rule}_verdict"] = "pass" if (f1 and f2) else ("unstable (one fold only)" if (f1 or f2) else "fail")
    pd.DataFrame(year_rows).to_csv(OUT / "stock_by_year.csv", index=False, float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "stock_monthly_returns.csv", float_format="%.6f")
    return out


# ======================================================================== main

def run(args) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    lines: list = []
    res = {"n_trials": N_TRIALS, "bonferroni_t_one_sided_5pct": qt.bonferroni_t(N_TRIALS)}
    if not args.stocks_only:
        res["etf"] = run_etf(lines)
    if not args.etf_only:
        res["stocks"] = run_stocks(lines)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=str))
    (OUT / "run_log.txt").write_text("\n".join(lines) + "\n")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--etf-only", action="store_true")
    ap.add_argument("--stocks-only", action="store_true")
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
