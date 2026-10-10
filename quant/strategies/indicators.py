"""Classic indicator rules (MA, MACD, RSI, KDJ, Bollinger, Donchian, Keltner, VIX combos, the owner's rules) on
the Nasdaq indices (timing engine) and on the stock panel (K-name engine).

Extracted unchanged from scripts/research_indicators.py: the rule table ``RULES`` / ``RULE_BY_CODE`` with the
signal functions ``r_*``, ``state_machine``, the index data ``load_index_data`` and engine ``index_sim`` (the QQQ
sleeve engine of quant.backtest.exposure with this study's half-spreads), the stock engine ``stock_sim`` /
``StockRunner`` and the metrics / criteria. Used by studies/indicators.py, grid_check, walk_forward and stops.
"""
from __future__ import annotations

import json
from itertools import product
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from quant.backtest import exposure, stop_rules
from quant.backtest.costs import CASH_ETF_FEE, rank_half_spread
from quant.data import guards
from quant.data.benchmarks import oneq_on_sessions  # noqa: F401  (ind.oneq_on_sessions)
from quant.data.index_history import QQQ_RAW
from quant.data.rates import fill_rf, load_dtb3, load_kf_rf
from quant.data.sources.yahoo import parse_chart
from quant.evaluation.metrics import TRADING_DAYS, cagr_of, exposure_metrics, max_drawdown, max_drawdown_of_returns
from quant.paths import CACHE_ROOT
from quant.signals.indicators import atr, bollinger, donchian, ema, kdj, macd, rsi, sma, true_range  # noqa: F401
from quant.strategies import canslim as cs
from quant.strategies import livermore as lv

RAW = CACHE_ROOT / "indicators/raw"
BENCH_DIR = CACHE_ROOT / "benchmarks"
INDEX_DEV_START, INDEX_DEV_END = "1999-03-10", "2014-12-31"
INDEX_TEST_ENTRY, INDEX_TEST_FIRST, INDEX_TEST_END = "2014-12-31", "2015-01-02", "2026-09-30"
ONEQ_FIRST_RETURN = "2003-10-02"
HALF_SPREAD = {"QQQ": 1e-4, "COMP": 2e-4}
K_STOCKS = 5
TP_O1B = 0.15
FutureDataError = guards.FutureDataError


def _bc(vix: pd.Series, like):
    """VIX aligned to ``like`` (Series or DataFrame) by date."""
    v = vix.reindex(like.index).ffill()
    if isinstance(like, pd.DataFrame):
        return pd.DataFrame(np.repeat(v.to_numpy()[:, None], like.shape[1], axis=1), index=like.index,
                            columns=like.columns)
    return v


def _owner_parts(b: dict, bb=(50, 3.0)) -> dict:
    h, l, c = b["high"], b["low"], b["close"]
    ma, up, lo = bollinger(c, *bb)
    k, d, j = kdj(h, l, c)
    out = {"boll_buy": (c > lo) & (c < ma), "boll_sell": (c > ma) & (c < up),
           "kdj_buy": (k > d) & (j < 30), "kdj_sell": (k < d) & (j > 70)}
    if b.get("has_hl", True):
        out["2b_buy"] = (l < l.shift(1)) & (c > l.shift(1))
        out["2b_sell"] = (h > h.shift(1)) & (c < h.shift(1))
    else:
        out["2b_buy"] = out["2b_sell"] = c != c          # not portable on close-only data: never true
    return out


def r_ma(b, s=5, l_=20):
    a, z = sma(b["close"], s), sma(b["close"], l_)
    return a > z, a < z


def r_fixed_ma(b, s=5, l_=20):
    c = b["close"]
    a, z = sma(c, s), sma(c, l_)
    buy = (a > z) & (a.shift(1) <= z.shift(1)) & (a.diff() > 0) & (z.diff() > 0) & (c > a) & (c > z)
    sell = (a < z) & (a.shift(1) >= z.shift(1)) & (a.diff() < 0) & (z.diff() < 0) & (c < a) & (c < z)
    return buy, sell


def r_dow2b(b):
    p = _owner_parts(b)
    return p["2b_buy"], p["2b_sell"]


def r_kdj(b):
    p = _owner_parts(b)
    return p["kdj_buy"], p["kdj_sell"]


def r_boll_orig(b):
    p = _owner_parts(b)
    return p["boll_buy"], p["boll_sell"]


def r_vix_combo(b):
    """analyze.py (2727a062d): VIX >= 25 -> boll | 2B | PE<=100; 15..25 -> boll | KDJ; < 15 -> KDJ | 2B.
    Sell: boll_sell | kdj_sell. PE has no point-in-time data here and counts as false."""
    p = _owner_parts(b)
    v = _bc(b["vix"], b["close"])
    hi, mid, lo = v >= 25, (v >= 15) & (v < 25), v < 15
    buy = (hi & (p["boll_buy"] | p["2b_buy"])) | (mid & (p["boll_buy"] | p["kdj_buy"])) | (lo & (p["kdj_buy"] | p["2b_buy"]))
    return buy, p["boll_sell"] | p["kdj_sell"]


def r_bt_score(b):
    """06b779304 bt_test_utils.BacktraderStrategy: weighted buy score >= 2.25, sell score >= 1.8."""
    p = _owner_parts(b)
    v = _bc(b["vix"], b["close"])
    w = {"bollinger": 1.0, "kdj": 1.2, "signal": 1.5, "pe": 0.8}

    tab = {"bollinger": (1.2, 1.1, 1.0, 0.9), "kdj": (1.0, 1.1, 1.2, 1.3), "signal": (1.3, 1.2, 1.1, 1.0)}

    def vw(name):          # VIX >= 30 / >= 25 / >= 15 / below
        a = np.select([v.to_numpy() >= 30, v.to_numpy() >= 25, v.to_numpy() >= 15], list(tab[name][:3]), tab[name][3])
        return v * 0 + a
    thr = 2 * sum(w.values()) / 4
    f = lambda x: x.astype(float)  # noqa: E731
    buy_score = f(p["boll_buy"]) * vw("bollinger") * w["bollinger"] + f(p["kdj_buy"]) * vw("kdj") * w["kdj"] + \
        f(p["2b_buy"]) * vw("signal") * w["signal"]          # + PE term: no data, false
    sell_score = f(p["boll_sell"]) * vw("bollinger") * w["bollinger"] + f(p["kdj_sell"]) * vw("kdj") * w["kdj"] + \
        f(p["2b_sell"]) * vw("signal") * w["signal"]
    return buy_score >= thr - 1e-12, sell_score >= 0.8 * thr - 1e-12


def r_owner_macd(b):
    dif, dea, hist = macd(b["close"])
    h = b["high"]
    return (dif > dea) & (hist.shift(1) < 0), (dif < dea) & (h > h.shift(2))


def r_donchian(b, n_in=20, n_out=None):
    up, _ = donchian(b["high"], b["low"], n_in)
    _, lo = donchian(b["high"], b["low"], n_out or n_in)
    c = b["close"]
    return c > up, c < lo


def r_owner_keltner(b):
    c = b["close"]
    mid = sma(c, 20).shift(1)
    a = atr(b["high"], b["low"], c, 14).shift(1)
    return c > mid + 1.5 * a, c < mid - 1.5 * a


def r_macd(b):
    dif, dea, _ = macd(b["close"])
    return dif > dea, dif < dea


def r_rsi_mr(b):
    x = rsi(b["close"], 14)
    return x < 30, x > 70


def r_rsi_trend(b):
    x = rsi(b["close"], 14)
    return x > 50, x < 50


def r_boll_mr(b):
    c = b["close"]
    ma, _, lo = bollinger(c, 20, 2.0)
    return c < lo, c > ma


def r_boll_break(b):
    c = b["close"]
    ma, up, _ = bollinger(c, 20, 2.0)
    return c > up, c < ma


def r_keltner(b):
    c = b["close"]
    mid = ema(c, 20)
    a = atr(b["high"], b["low"], c, 10)
    return c > mid + 2 * a, c < mid


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    fn: Callable
    owner: bool
    holding: bool = False        # "hold while condition" rules: stock entries only on the day it turns true
    buy_wins: bool = False
    take_profit: float | None = None
    stock_ok: bool = True


RULES = [
    Rule("O1", "owner_ma5_20", r_ma, True, holding=True),
    Rule("O1b", "owner_ma5_20_tp15", r_ma, True, holding=True, take_profit=TP_O1B),
    Rule("O2", "owner_fixed_ma5_20", r_fixed_ma, True),
    Rule("O3", "owner_dow_2b", r_dow2b, True, stock_ok=False),
    Rule("O4", "owner_kdj14", r_kdj, True),
    Rule("O5", "owner_boll50_3_orig", r_boll_orig, True),
    Rule("O6", "owner_vix_combo", r_vix_combo, True, buy_wins=True),
    Rule("O7", "owner_bt_score", r_bt_score, True),
    Rule("O9", "owner_macd", r_owner_macd, True),
    Rule("O10", "owner_donchian20", r_donchian, True),
    Rule("O11", "owner_keltner20_14_1p5", r_owner_keltner, True),
    Rule("T1", "macd_12_26_9", r_macd, False, holding=True),
    Rule("T2", "rsi14_mr_30_70", r_rsi_mr, False),
    Rule("T3", "rsi14_trend_50", r_rsi_trend, False, holding=True),
    Rule("T4", "donchian_20_10", lambda b: r_donchian(b, 20, 10), False),
    Rule("T5", "donchian_55_20", lambda b: r_donchian(b, 55, 20), False),
    Rule("T6", "boll20_2_mr", r_boll_mr, False),
    Rule("T7", "boll20_2_breakout", r_boll_break, False),
    Rule("T8", "keltner_ema20_2atr10", r_keltner, False),
]
RULE_BY_CODE = {r.code: r for r in RULES}


def state_machine(buy, sell, close=None, take_profit: float | None = None, buy_wins: bool = False) -> pd.Series:
    """Decision state at each close t (1 = want to hold). Flat -> buy on a buy signal; long -> sell on a sell signal
    (unless ``buy_wins`` and buy is also true) or, with ``take_profit``, when close_t >= entry * (1 + tp), where the
    entry is the close of the session after the buy decision (the fill)."""
    b = np.asarray(pd.Series(buy).fillna(False), bool)
    s = np.asarray(pd.Series(sell).fillna(False), bool)
    c = np.asarray(close, float) if close is not None else None
    out = np.zeros(len(b))
    st, entry, filled_at = 0, np.nan, -1
    for t in range(len(b)):
        if st == 1 and filled_at == t:
            entry = c[t] if c is not None else np.nan
        if st == 0:
            if b[t]:
                st, filled_at, entry = 1, t + 1, np.nan
        else:
            hit_tp = take_profit is not None and np.isfinite(entry) and c[t] >= entry * (1 + take_profit)
            if hit_tp or (s[t] and not (buy_wins and b[t])):
                st = 0
        out[t] = st
    return pd.Series(out, index=pd.Series(buy).index)


def parse_ohlc(path: Path, end: str) -> pd.DataFrame:
    """Yahoo chart JSON -> OHLC by date (price index, no adjustment), truncated at ``end`` and asserted."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"]}).dropna(subset=["close"])
    df = guards.truncate_dev(df, "date", end).drop_duplicates("date", keep="last")
    df.index = pd.DatetimeIndex(df.pop("date"))
    for k in ("open", "high", "low"):
        df[k] = df[k].fillna(df["close"])
    return df


@dataclass
class IndexData:
    bars: dict          # instrument -> OHLC frame of the signal index (on the instrument's sessions)
    r1: dict            # instrument -> daily total return of the held index
    price: dict         # instrument -> ETF close for share counts (NaN where no ETF)
    vix: pd.Series
    rf: pd.Series
    qqq_r: pd.Series    # QQQ total return on COMP sessions (report only)
    guard: dict


def load_index_data(end: str = INDEX_DEV_END) -> IndexData:
    ixic = parse_ohlc(RAW / "chart_%5EIXIC_full.json", end)
    ndx = parse_ohlc(QQQ_RAW / "chart_%5ENDX.json", end)
    vixf = parse_ohlc(RAW / "chart_%5EVIX.json", end)
    oneq = parse_chart(BENCH_DIR / "chart_ONEQ.json", end)
    qqq = parse_chart(QQQ_RAW / "chart_QQQ.json", end)
    rf_kf, dtb3 = load_kf_rf(end), load_dtb3(end)
    guard = {"end": end}
    for name, ix in [("ixic", ixic.index), ("ndx", ndx.index), ("vix", vixf.index), ("oneq", oneq["date"]),
                     ("qqq", qqq["date"]), ("kf_rf", rf_kf.index), ("dtb3", dtb3.index)]:
        guards.assert_dev_dates(ix, end)
        ix = pd.DatetimeIndex(ix)
        guard[name] = {"first": str(ix.min().date()), "last": str(ix.max().date()), "rows": int(len(ix))}

    comp_s = ixic.index
    r_ixic = ixic["close"].pct_change()
    oneq_lvl = pd.Series(oneq["adjclose"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"]))
    r_oneq = oneq_lvl.pct_change().reindex(comp_s)
    r_comp = r_ixic.copy()
    use = comp_s >= pd.Timestamp(ONEQ_FIRST_RETURN)
    r_comp[use] = r_oneq[use]
    if r_comp[use].isna().any():
        raise ValueError("ONEQ is missing on Composite sessions")
    price_comp = pd.Series(oneq["close"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(comp_s)

    qqq_lvl = pd.Series(qqq["adjclose"].to_numpy(float), index=pd.DatetimeIndex(qqq["date"]))
    ndx_s = ndx.index
    r_ndx = ndx["close"].pct_change()
    r_q = qqq_lvl.pct_change().reindex(ndx_s)
    r_qqq_inst = r_ndx.copy()
    useq = ndx_s > qqq_lvl.index[0]
    r_qqq_inst[useq] = r_q[useq]
    price_qqq = pd.Series(qqq["close"].to_numpy(float), index=pd.DatetimeIndex(qqq["date"])).reindex(ndx_s)

    all_s = comp_s.union(ndx_s)
    rf = fill_rf(rf_kf, dtb3, all_s)
    vix = vixf["close"]
    bars = {"COMP": {**{k: ixic[k] for k in ("open", "high", "low", "close")}, "vix": vix, "has_hl": True},
            "QQQ": {**{k: ndx[k] for k in ("open", "high", "low", "close")}, "vix": vix, "has_hl": True}}
    return IndexData(bars=bars, r1={"COMP": r_comp.fillna(0.0), "QQQ": r_qqq_inst.fillna(0.0)},
                     price={"COMP": price_comp, "QQQ": price_qqq}, vix=vix, rf=rf,
                     qqq_r=qqq_lvl.pct_change().reindex(comp_s), guard=guard)


def index_sim(target: pd.Series, data: IndexData, inst: str, start: str, end: str, lag: int = 1,
              cash_is_etf: bool = True) -> dict:
    r1 = data.r1[inst]
    sess = r1.index
    rf = data.rf.reindex(sess)
    rc = rf - CASH_ETF_FEE / TRADING_DAYS if cash_is_etf else pd.Series(0.0, index=sess)
    zero = pd.Series(0.0, index=sess)
    sim = exposure.simulate(target.reindex(sess), r1, zero, rc, data.price[inst], start, end, lag=lag,
                            cash_is_etf=cash_is_etf, half_spread=HALF_SPREAD[inst])
    guards.assert_dev_dates(sim["ret"].index, end)
    return sim


def index_metrics(sim: dict, bench: dict, rf: pd.Series) -> dict:
    m = exposure_metrics(sim, bench, rf)
    m["excess_cagr"] = m.pop("excess_cagr_vs_qqq")
    m["bench_cagr"] = cagr_of(bench["ret"])
    m["bench_max_dd"] = max_drawdown(bench["value"])
    m["trades_per_year"] = m["switches_per_year"]
    return m


def criteria(cagr: float, mdd: float, b_cagr: float, b_mdd: float, t: float) -> dict:
    a = bool(cagr > b_cagr and t >= 2)
    b = bool(abs(mdd) <= abs(b_mdd) - 0.10 and cagr >= b_cagr - 0.03)
    return {"A": a, "B": b, "pass": a or b}


def index_targets(data: IndexData, inst: str) -> dict:
    out = {}
    for r in RULES:
        bars = data.bars[inst]
        buy, sell = r.fn(bars)
        out[r.code] = state_machine(buy, sell, bars["close"], r.take_profit, r.buy_wins)
    return out


def stock_bars(data) -> dict:
    c = data.close_adj
    return {"open": c, "high": c, "low": c, "close": c, "vix": None, "has_hl": False}


def stock_signals(rule: Rule, bars: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    buy, sell = rule.fn(bars)
    buy, sell = buy.fillna(False).astype(bool), sell.fillna(False).astype(bool)
    if rule.holding:
        buy = buy & ~buy.shift(1, fill_value=False)
    return buy, sell


def stock_sim(rule: Rule, data, buy: pd.DataFrame, sell: pd.DataFrame, elig: dict, rank_of: dict,
              account: float = 10_000.0, k: int = K_STOCKS, stops=None, sigma: pd.DataFrame | None = None,
              start: str | None = None) -> dict:
    """Daily engine. Decisions at the close of d (sells for held names, buys for eligible names), fills at d+1.

    Optional (defaults keep the original behaviour): ``stops`` adds a stop_rules.StopSpec (or one per simulated
    session) checked before the rule's own exit; ``sigma`` is the 20-day return stdev frame for vol stops (from
    ``data.sig_idx`` when needed); ``start`` starts the simulation (from cash) after ``perf_start``."""
    sessions = data.sessions
    eff_end = pd.Timestamp(data.spec["effective_end"])
    first = pd.Timestamp(start or data.spec["perf_start"])
    assert first >= pd.Timestamp(data.spec["perf_start"])
    perf = sessions[(sessions >= first) & (sessions <= eff_end)]
    lv.cs.assert_window(perf, data.spec["perf_start"], data.spec["effective_end"], "simulation sessions")
    cols = list(data.perf_idx.columns)
    col = {s: i for i, s in enumerate(cols)}
    I = data.perf_idx.reindex(perf).to_numpy()
    P = data.close.reindex(perf).to_numpy()
    B = buy.reindex(index=perf, columns=cols).fillna(False).to_numpy()
    S = sell.reindex(index=perf, columns=cols).fillna(False).to_numpy()
    lr = data.last_row.reindex(cols)
    lr_arr = lr.to_numpy()
    weeks = sorted(elig)
    wk_pos = np.searchsorted(np.array(weeks, dtype="datetime64[ns]"), perf.values, side="right") - 1
    cash = account
    pos: dict = {}
    pend_sell, pend_buy = {}, []
    navs, expo, costs = np.zeros(len(perf)), np.zeros(len(perf)), np.zeros(len(perf))
    traded = 0.0
    counts = {"buy": 0, "sell": 0, "take_profit": 0, "delisted": 0}
    trades = []
    stop_sched = stop_rules.schedule(stops, len(perf)) if stops is not None else None
    SIG = None
    if stop_sched is not None:
        counts["stop"] = 0
        if any(s_.kind == "vol" for s_ in stop_sched):
            sg = stop_rules.sigma20(data.sig_idx) if sigma is None else sigma
            SIG = sg.reindex(index=perf, columns=cols).to_numpy()

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
            pos[sid] = {"units": (amt - kc) / I[i, c], "entry_i": i, "entry_idx": I[i, c], "cost_basis": amt,
                        "peak_idx": I[i, c], "sigma": SIG[i, c] if SIG is not None else np.nan}
            cash -= amt
            day_cost += kc
            traded += amt
            counts["buy"] += 1
        pend_buy = []
        if i < len(perf) - 1:
            for sid, p in pos.items():
                c = col[sid]
                if np.isfinite(I[i, c]):
                    p["peak_idx"] = max(p["peak_idx"], I[i, c])
                if p["entry_i"] == i:
                    continue
                if stop_sched is not None and stop_rules.triggered(stop_sched[i], I[i, c], p["entry_idx"],
                                                                   p["peak_idx"], p["sigma"]):
                    pend_sell[sid] = "stop"
                    counts["stop"] += 1
                elif rule.take_profit is not None and I[i, c] / p["entry_idx"] - 1 >= rule.take_profit:
                    pend_sell[sid] = "take_profit"
                    counts["take_profit"] += 1
                elif S[i, c]:
                    pend_sell[sid] = "signal"
            slots = k - (len(pos) - len(pend_sell))
            if slots > 0 and wk_pos[i] >= 0:
                for sid in elig[weeks[wk_pos[i]]]:
                    c = col.get(sid)
                    if c is None or sid in pos or not B[i, c]:
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
            "cost": pd.Series(costs, index=perf), "traded": traded, "counts": counts,
            "trades": pd.DataFrame(trades), "open": sorted(pos)}


class StockRunner:
    def __init__(self, data):
        self.data = data
        u = lv.eligible_universe(data)
        self.elig = {t: list(g.sort_values("dv50_rank")["security_id"]) for t, g in u.groupby("week_end")}
        self.rank_of = {t: dict(zip(g["security_id"], g["dv50_rank"])) for t, g in u.groupby("week_end")}
        self.carried_weeks = int(u.loc[u["carried"], "week_end"].nunique())
        self.bars = stock_bars(data)
        self.bars["vix"] = parse_ohlc(RAW / "chart_%5EVIX.json", data.spec["effective_end"])["close"]
        lvl, px = oneq_on_sessions(data.sessions, data.spec["price_start"], data.spec["effective_end"])
        self.oneq_lvl, self.oneq_px = lvl, px

    def benchmarks(self, dates, account):
        q = self.oneq_lvl.reindex(dates)
        kc = cs.order_cost(account, float(self.oneq_px.reindex(dates).iloc[0]), False, HALF_SPREAD["COMP"])
        oneq = (account - kc) * q / q.iloc[0]
        qqq = cs.qqq_benchmark(self.data.qqq_perf_idx, self.data.qqq_close, dates, account)
        return oneq, qqq

    def run(self, rule: Rule, account: float = 10_000.0):
        buy, sell = stock_signals(rule, self.bars)
        res = stock_sim(rule, self.data, buy, sell, self.elig, self.rank_of, account)
        oneq, qqq = self.benchmarks(res["dates"], account)
        m = stock_metrics(res["nav"], oneq, qqq, account, res["cost"], res["traded"], res["exposure"])
        m.update({f"n_{x}": v for x, v in res["counts"].items()})
        m.update(lv.trade_stats(res["trades"].assign(tranches=1)) if len(res["trades"]) else {"closed_trades": 0})
        return m, res, oneq, qqq


def stock_metrics(nav, oneq, qqq, account, cost, traded, exposure) -> dict:
    m = lv.perf_metrics(nav, oneq, account, cost, traded, exposure)
    m["bench_cagr"], m["bench_max_dd"] = m.pop("qqq_cagr"), m.pop("qqq_max_dd")
    m["bench_sharpe"] = m.pop("qqq_sharpe")
    years = (nav.index[-1] - nav.index[0]).days / 365.25
    m["qqq_cagr"] = (qqq.iloc[-1] / account) ** (1 / years) - 1
    r = pd.concat([pd.Series([account], index=[qqq.index[0] - pd.Timedelta(days=7)]), qqq]).pct_change().dropna()
    m["qqq_max_dd"] = max_drawdown_of_returns(r)
    for y, v in m["by_year"].items():
        v["oneq"] = v.pop("qqq")
    m["years_beating_bench"] = m.pop("years_beating_qqq")
    return m


def stock_sub_metrics(nav, oneq, qqq, start, cost, exposure, account=10_000.0) -> dict:
    before = nav.index[nav.index < pd.Timestamp(start)]
    if not len(before):
        return stock_metrics(nav, oneq, qqq, account, cost, 0.0, exposure)
    t0 = before[-1]
    keep = nav.index > t0
    f = lambda s: s[keep] / s.loc[t0] * account  # noqa: E731
    return stock_metrics(f(nav), f(oneq), f(qqq), account, cost[keep], 0.0, exposure[keep])


# ---------------------------------------------------------------- post-hoc parameter grids (from scripts/research_grid_check.py)
MA_SHORT, MA_LONG = range(3, 11), range(20, 61)
DC_ENTRY, DC_EXIT = range(10, 61), range(5, 31, 5)
OWNER_REF = {"ma": (5, 20), "donchian": (20, 20)}


def ma_grid() -> list[tuple[int, int]]:
    return [(s, l_) for s, l_ in product(MA_SHORT, MA_LONG) if s < l_]


def donchian_grid() -> list[tuple[int, int]]:
    return list(product(DC_ENTRY, DC_EXIT))


GRIDS = {"ma": ma_grid, "donchian": donchian_grid}
PARAM_NAMES = {"ma": ("short", "long"), "donchian": ("entry", "exit")}


def make_rule(kind: str, a: int, b: int) -> Rule:
    """The MA(a, b) crossover or Donchian(a, b) channel rule of the post-hoc parameter grids (research_grid_check,
    research_walk_forward)."""
    if kind == "ma":
        return Rule(f"MA{a}_{b}", f"ma_{a}_{b}", lambda x, a=a, b=b: r_ma(x, a, b), True, holding=True)
    return Rule(f"DC{a}_{b}", f"donchian_{a}_{b}", lambda x, a=a, b=b: r_donchian(x, a, b), True)
