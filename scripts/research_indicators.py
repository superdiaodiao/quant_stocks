"""Owner's own technical-indicator strategies and textbook MACD / RSI / channel rules: development grids and one-shot tests.

Two applications (pre-registered in docs/research_ledger_indicators.md, section 0, before the first run):

(a) Index timing: in = hold the index ETF, out = a T-bill ETF.  Two instruments:
    ``COMP``: signals on ^IXIC OHLC, P&L on the Nasdaq Composite total return (ONEQ adjusted close from 2003-10-02;
    ^IXIC price return before ONEQ existed);  ``QQQ``: signals on ^NDX OHLC, P&L on QQQ total return.
    Development 1999-03-10 .. 2014-12-31 (every frame truncated at 2014-12-31 and asserted); one-shot 2015-01-02 ..
    2026-09-30 for the single frozen rule.
(b) Individual stocks: Nasdaq common stocks in the weekly dollar-volume top 300 with close >= $10 (frozen data version
    1 panel via ``scripts/research_livermore.load_window``), at most 5 names, NAV/5 per entry, idle cash at 0%.
    Development 2017-2022; one-shots 2023-01..2026-08-31 and 2012-2016 (2012-2013 reported, not judged).

Benchmark (decided before any result): Nasdaq Composite total return = ONEQ adjusted close; QQQ reported alongside.
Timing: signals from closes up to t, executed at the close of t+1.  Costs: IBKR Pro Tiered (see the ledger).
The stock panel has closes only, so stock indicators use closes for highs / lows (documented in the ledger).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import NormalDist
from typing import Callable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import research_qqq_timing as qt  # noqa: E402  (Yahoo parser + date guard, RF, index simulator, metrics)
from scripts import research_livermore as lv  # noqa: E402  (stock windows loader, universe, metrics)
from scripts import research_canslim_dev as cs  # noqa: E402  (order cost, QQQ benchmark)
from scripts import research_reversal_dev as rev  # noqa: E402  (half spread, deflated Sharpe)

NORM = NormalDist()
OUT = ROOT / "output/research_only/indicators"
LEDGER = ROOT / "docs/research_ledger_indicators.md"
FROZEN = OUT / "frozen_rules.json"
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/indicators/raw")
BENCH_DIR = Path("/Users/bytedance/code/quant_stocks/research_cache/benchmarks")
QQQ_RAW = qt.CACHE

INDEX_DEV_START, INDEX_DEV_END = "1999-03-10", "2014-12-31"
INDEX_TEST_ENTRY, INDEX_TEST_FIRST, INDEX_TEST_END = "2014-12-31", "2015-01-02", "2026-09-30"
ONEQ_FIRST_RETURN = "2003-10-02"
HALF_SPREAD = {"QQQ": 1e-4, "COMP": 2e-4}
K_STOCKS = 5
TP_O1B = 0.15
FutureDataError = qt.FutureDataError


# ======================================================================== indicators (pure, causal)

def sma(x, n: int):
    return x.rolling(n, min_periods=n).mean()


def ema(x, n: int):
    """Exponential average with alpha = 2 / (n + 1), started on the first value (pandas adjust=False)."""
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(x, n: int):
    """Wilder smoothing (alpha = 1 / n), started on the first value."""
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close, n: int = 14):
    d = close.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    au, ad = wilder(up.iloc[1:], n), wilder(dn.iloc[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100 - 100 / (1 + au / ad)
    out = out.where(ad > 0, 100.0).where(au.notna())
    return out.reindex(close.index)


def macd(close, fast: int = 12, slow: int = 26, signal: int = 9):
    dif = ema(close, fast) - ema(close, slow)
    dea = dif.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return dif, dea, dif - dea


def kdj(high, low, close, n: int = 14, alpha: float = 1 / 3):
    """The owner's KDJ (src/strategy/common.calculate_kdj): RSV over n sessions, K = EMA(RSV), D = EMA(K), J = 3K - 2D."""
    hh, ll = high.rolling(n).max(), low.rolling(n).min()
    rsv = (close - ll) / (hh - ll) * 100
    k = rsv.ewm(alpha=alpha, adjust=False).mean()
    d = k.ewm(alpha=alpha, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def bollinger(close, n: int, k: float):
    ma = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return ma, ma + k * sd, ma - k * sd


def true_range(high, low, close):
    pc = close.shift(1)
    a, b, c = (high - low), (high - pc).abs(), (low - pc).abs()
    tr = np.maximum(np.maximum(a, b), c)
    return tr.where(pc.notna())


def atr(high, low, close, n: int):
    return wilder(true_range(high, low, close).iloc[1:], n).reindex(close.index)


def donchian(high, low, n: int):
    """Channel from the n sessions BEFORE t (today's own bar is not in it)."""
    return high.shift(1).rolling(n, min_periods=n).max(), low.shift(1).rolling(n, min_periods=n).min()


# ======================================================================== rules -> (buy, sell) booleans at close t

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


# ======================================================================== index data

def parse_ohlc(path: Path, end: str) -> pd.DataFrame:
    """Yahoo chart JSON -> OHLC by date (price index, no adjustment), truncated at ``end`` and asserted."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"]}).dropna(subset=["close"])
    df = qt.truncate_dev(df, "date", end).drop_duplicates("date", keep="last")
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
    oneq = qt.parse_chart(BENCH_DIR / "chart_ONEQ.json", end)
    qqq = qt.parse_chart(QQQ_RAW / "chart_QQQ.json", end)
    rf_kf, dtb3 = qt.load_kf_rf(end), qt.load_dtb3(end)
    guard = {"end": end}
    for name, ix in [("ixic", ixic.index), ("ndx", ndx.index), ("vix", vixf.index), ("oneq", oneq["date"]),
                     ("qqq", qqq["date"]), ("kf_rf", rf_kf.index), ("dtb3", dtb3.index)]:
        qt.assert_dev_dates(ix, end)
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
    rf = qt.fill_rf(rf_kf, dtb3, all_s)
    vix = vixf["close"]
    bars = {"COMP": {**{k: ixic[k] for k in ("open", "high", "low", "close")}, "vix": vix, "has_hl": True},
            "QQQ": {**{k: ndx[k] for k in ("open", "high", "low", "close")}, "vix": vix, "has_hl": True}}
    return IndexData(bars=bars, r1={"COMP": r_comp.fillna(0.0), "QQQ": r_qqq_inst.fillna(0.0)},
                     price={"COMP": price_comp, "QQQ": price_qqq}, vix=vix, rf=rf,
                     qqq_r=qqq_lvl.pct_change().reindex(comp_s), guard=guard)


@contextmanager
def _half_spread(hs: float):
    old = qt.HALF_SPREAD
    qt.HALF_SPREAD = hs
    try:
        yield
    finally:
        qt.HALF_SPREAD = old


def index_sim(target: pd.Series, data: IndexData, inst: str, start: str, end: str, lag: int = 1,
              cash_is_etf: bool = True) -> dict:
    r1 = data.r1[inst]
    sess = r1.index
    rf = data.rf.reindex(sess)
    rc = rf - qt.CASH_ETF_FEE / qt.TRADING_DAYS if cash_is_etf else pd.Series(0.0, index=sess)
    zero = pd.Series(0.0, index=sess)
    with _half_spread(HALF_SPREAD[inst]):
        sim = qt.simulate(target.reindex(sess), r1, zero, rc, data.price[inst], start, end, lag=lag,
                          cash_is_etf=cash_is_etf)
    qt.assert_dev_dates(sim["ret"].index, end)
    return sim


def index_metrics(sim: dict, bench: dict, rf: pd.Series) -> dict:
    m = qt.metrics(sim, bench, rf)
    m["excess_cagr"] = m.pop("excess_cagr_vs_qqq")
    m["bench_cagr"] = qt.cagr_of(bench["ret"])
    m["bench_max_dd"] = qt.max_drawdown(bench["value"])
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


# ======================================================================== stock data and engine

def oneq_on_sessions(sessions: pd.DatetimeIndex, start: str, end: str) -> tuple[pd.Series, pd.Series]:
    oneq = qt.parse_chart(BENCH_DIR / "chart_ONEQ.json", end)
    oneq = oneq[oneq["date"] >= start]
    lv.cs.assert_window(oneq["date"], start, end, "ONEQ")
    lvl = pd.Series(oneq["adjclose"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    px = pd.Series(oneq["close"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    return lvl, px


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
              account: float = 10_000.0, k: int = K_STOCKS) -> dict:
    """Daily engine. Decisions at the close of d (sells for held names, buys for eligible names), fills at d+1."""
    sessions = data.sessions
    eff_end = pd.Timestamp(data.spec["effective_end"])
    perf = sessions[(sessions >= pd.Timestamp(data.spec["perf_start"])) & (sessions <= eff_end)]
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
        pend_buy = []
        if i < len(perf) - 1:
            for sid, p in pos.items():
                if p["entry_i"] == i:
                    continue
                c = col[sid]
                if rule.take_profit is not None and I[i, c] / p["entry_idx"] - 1 >= rule.take_profit:
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
    m["qqq_max_dd"] = rev.max_drawdown(r)
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


# ======================================================================== development runs

def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def run_index_dev() -> pd.DataFrame:
    data = load_index_data(INDEX_DEV_END)
    print("DATE GUARD (index dev): all frames truncated at", INDEX_DEV_END, {k: v["last"] for k, v in data.guard.items()
                                                                             if isinstance(v, dict)})
    rows, monthly_keep = [], {}
    for inst in ("COMP", "QQQ"):
        sess = data.r1[inst].index
        const = pd.Series(1.0, index=sess)
        bench_own = index_sim(const, data, inst, INDEX_DEV_START, INDEX_DEV_END)
        bench_comp = index_sim(pd.Series(1.0, index=data.r1["COMP"].index), data, "COMP", INDEX_DEV_START, INDEX_DEV_END)
        targets = index_targets(data, inst)
        monthly_keep[f"{inst}_buyhold"] = qt.monthly(bench_own["ret"])
        for r in RULES:
            tgt = targets[r.code]
            sim = index_sim(tgt, data, inst, INDEX_DEV_START, INDEX_DEV_END)
            if inst == "COMP":
                m = index_metrics(sim, bench_own, data.rf)
            else:
                # judged against the Composite (ONEQ) on common dates; own-instrument buy-and-hold for robustness
                m = index_metrics(sim, _align(bench_comp, sim), data.rf)
            mo = index_metrics(sim, bench_own, data.rf)
            row = {"instrument": inst, "code": r.code, "rule": r.name, "owner": r.owner, "counted": True,
                   **{k: m[k] for k in ("cagr", "bench_cagr", "excess_cagr", "max_dd", "bench_max_dd", "sharpe",
                                        "calmar", "t_monthly_excess", "ir_monthly", "skew_mx", "kurt_mx", "months",
                                        "time_1x", "trades_per_year", "cost_drag_per_year", "worst_year",
                                        "worst_year_which")},
                   "own_bh_cagr": mo["bench_cagr"], "own_bh_max_dd": mo["bench_max_dd"],
                   "excess_vs_own_bh": mo["excess_cagr"], "t_vs_own_bh": mo["t_monthly_excess"]}
            c = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_monthly_excess"])
            co = criteria(mo["cagr"], mo["max_dd"], mo["bench_cagr"], mo["bench_max_dd"], mo["t_monthly_excess"])
            row.update({"crit_A": c["A"], "crit_B": c["B"], "own_crit_A": co["A"], "own_crit_B": co["B"]})
            for tag, kw in (("sameday", dict(lag=0)), ("cash0", dict(cash_is_etf=False))):
                s2 = index_sim(tgt, data, inst, INDEX_DEV_START, INDEX_DEV_END, **kw)
                row[f"{tag}_cagr"] = qt.cagr_of(s2["ret"])
            # halves of the development period
            for h, (a, b) in (("h1", (INDEX_DEV_START, "2006-12-29")), ("h2", ("2006-12-29", INDEX_DEV_END))):
                sh = index_sim(tgt, data, inst, a, b)
                bh = index_sim(const, data, inst, a, b)
                row[f"{h}_excess_vs_own_bh"] = qt.cagr_of(sh["ret"]) - qt.cagr_of(bh["ret"])
            rows.append(row)
            monthly_keep[f"{inst}_{r.code}"] = qt.monthly(sim["ret"])
    grid = pd.DataFrame(rows)
    n = len(grid)
    grid["dsr"] = [rev.deflated_sharpe(x.ir_monthly, int(x.months), x.skew_mx, x.kurt_mx,
                                       grid["ir_monthly"].to_numpy())["dsr"] for x in grid.itertuples()]
    out = OUT / "index_dev_1999_2014"
    out.mkdir(parents=True, exist_ok=True)
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    mk = pd.DataFrame(monthly_keep)
    mk.index = mk.index.astype(str)
    mk.to_csv(out / "monthly_returns.csv", float_format="%.6f")
    (out / "guard.json").write_text(json.dumps({"guard": data.guard, "n_variants": n}, indent=2))
    return grid


def _align(bench: dict, sim: dict) -> dict:
    """Composite benchmark restricted to the dates of ``sim`` (both start at the same entry close)."""
    v = bench["value"].reindex(sim["value"].index).ffill()
    return {"value": v, "ret": v.pct_change().iloc[1:].fillna(0.0)}


def run_stock_dev() -> pd.DataFrame:
    data = lv.load_window("dev")
    print(data.guard["assertion"])
    runner = StockRunner(data)
    rows = []
    out = OUT / "stock_dev_2017_2022"
    out.mkdir(parents=True, exist_ok=True)
    mon = {}
    for r in [x for x in RULES if x.stock_ok]:
        m, res, oneq, qqq = runner.run(r)
        h1 = stock_sub_metrics(res["nav"].loc[:"2019-12-31"], oneq.loc[:"2019-12-31"], qqq.loc[:"2019-12-31"],
                               "2017-01-01", res["cost"].loc[:"2019-12-31"], res["exposure"].loc[:"2019-12-31"])
        h2 = stock_sub_metrics(res["nav"], oneq, qqq, "2020-01-01", res["cost"], res["exposure"])
        c = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_excess_monthly"])
        row = {"code": r.code, "rule": r.name, "owner": r.owner, "counted": True,
               **{k: m[k] for k in ("cagr", "bench_cagr", "excess_cagr", "qqq_cagr", "max_dd", "bench_max_dd",
                                    "qqq_max_dd", "sharpe", "t_excess_monthly", "t_excess_weekly",
                                    "ir_weekly_per_period", "weekly_active_skew", "weekly_active_kurt", "weeks",
                                    "turnover_one_way_per_year", "cost_drag_per_year", "time_in_stocks",
                                    "avg_exposure", "years_beating_bench", "n_buy", "n_sell", "n_take_profit",
                                    "n_delisted", "closed_trades", "win_rate", "avg_trade_ret", "median_days")
                  if k in m},
               "h1_2017_2019_excess": h1["excess_cagr"], "h2_2020_2022_excess": h2["excess_cagr"],
               "crit_A": c["A"], "crit_B": c["B"]}
        rows.append(row)
        mon[r.code] = m["_monthly_active"]
        print(f"{r.code:4s} {r.name:24s} CAGR {m['cagr']:+.1%} ONEQ {m['bench_cagr']:+.1%} DD {m['max_dd']:.0%} "
              f"t {m['t_excess_monthly']:+.2f} buys {m['n_buy']}")
    grid = pd.DataFrame(rows)
    grid["dsr_weekly"] = [rev.deflated_sharpe(x.ir_weekly_per_period, int(x.weeks), x.weekly_active_skew,
                                              x.weekly_active_kurt, grid["ir_weekly_per_period"].to_numpy())["dsr"]
                          for x in grid.itertuples()]
    grid.to_csv(out / "grid.csv", index=False, float_format="%.6f")
    pd.DataFrame(mon).to_csv(out / "monthly_active_vs_oneq.csv", float_format="%.6f")
    (out / "guard.json").write_text(json.dumps(data.guard, indent=2, default=str))
    return grid


def pick(grid: pd.DataFrame, t_col: str, robust_col: str | None = None) -> tuple[pd.Series, str]:
    """Pre-registered freeze rule (ledger 0.5)."""
    def choose(g):
        a = g[g["crit_A"]]
        if len(a):
            return a.sort_values(t_col, ascending=False).iloc[0], "A"
        b = g[g["crit_B"]]
        if len(b):
            return b.sort_values("excess_cagr", ascending=False).iloc[0], "B"
        return g.sort_values(t_col, ascending=False).iloc[0], "none_highest_t"
    if robust_col is not None:
        g = grid[(grid["crit_A"] & grid[f"{robust_col}_A"]) | (~grid["crit_A"] & grid["crit_B"] & grid[f"{robust_col}_B"])]
        if len(g):
            return choose(g)
    return choose(grid)


# ======================================================================== freeze and one-shots

def _freeze_section() -> str:
    text = LEDGER.read_text()
    a, b = text.index("<!-- FREEZE-BEGIN -->"), text.index("<!-- FREEZE-END -->")
    return text[a:b]


def write_frozen(index_code: str, stock_code: str) -> None:
    FROZEN.write_text(json.dumps({"index_rule": index_code, "index_instrument": "COMP", "stock_rule": stock_code,
                                  "frozen_at": pd.Timestamp.now("UTC").isoformat(),
                                  "ledger_freeze_sha256": hashlib.sha256(_freeze_section().encode()).hexdigest()},
                                 indent=2))


def frozen() -> dict:
    if not FROZEN.exists():
        raise SystemExit("no frozen_rules.json: freeze the rules (ledger section 4) before any one-shot test")
    fr = json.loads(FROZEN.read_text())
    if hashlib.sha256(_freeze_section().encode()).hexdigest() != fr["ledger_freeze_sha256"]:
        raise SystemExit("the ledger freeze section changed after freezing; refusing to run")
    return fr


def run_oneshot_index() -> dict:
    fr = frozen()
    out = OUT / "oneshot_index_2015_2026"
    if (out / "result.json").exists():
        raise SystemExit(f"already run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = load_index_data(INDEX_TEST_END)
    print("ONE-SHOT index: frames truncated at", INDEX_TEST_END, {k: v["last"] for k, v in data.guard.items()
                                                                   if isinstance(v, dict)})
    rule = RULE_BY_CODE[fr["index_rule"]]
    res = {"rule": rule.code, "name": rule.name, "guard": data.guard}
    bench = index_sim(pd.Series(1.0, index=data.r1["COMP"].index), data, "COMP", INDEX_TEST_ENTRY, INDEX_TEST_END)
    assert str(bench["ret"].index[0].date()) == INDEX_TEST_FIRST
    qqq_bh = index_sim(pd.Series(1.0, index=data.r1["QQQ"].index), data, "QQQ", INDEX_TEST_ENTRY, INDEX_TEST_END)
    res["ONEQ_buyhold"] = {"cagr": qt.cagr_of(bench["ret"]), "max_dd": qt.max_drawdown(bench["value"])}
    res["QQQ_buyhold"] = {"cagr": qt.cagr_of(qqq_bh["ret"]), "max_dd": qt.max_drawdown(qqq_bh["value"])}
    by_year = {"ONEQ_buyhold": qt.yearly(bench["ret"]), "QQQ_buyhold": qt.yearly(qqq_bh["ret"])}
    for inst in ("COMP", "QQQ"):
        bars = data.bars[inst]
        buy, sell = rule.fn(bars)
        tgt = state_machine(buy, sell, bars["close"], rule.take_profit, rule.buy_wins)
        sim = index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END)
        m = index_metrics(sim, bench if inst == "COMP" else _align(bench, sim), data.rf)
        m["state_at_entry_close"] = float(tgt.loc[INDEX_TEST_ENTRY])
        m["sameday_cagr"] = qt.cagr_of(index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END, lag=0)["ret"])
        m["cash0_cagr"] = qt.cagr_of(index_sim(tgt, data, inst, INDEX_TEST_ENTRY, INDEX_TEST_END,
                                               cash_is_etf=False)["ret"])
        m["criteria"] = criteria(m["cagr"], m["max_dd"], m["bench_cagr"], m["bench_max_dd"], m["t_monthly_excess"])
        res[inst] = m
        by_year[f"{rule.code}_{inst}"] = qt.yearly(sim["ret"])
    res["verdict_judged_on"] = "COMP (ONEQ instrument vs ONEQ buy-and-hold)"
    res["pass"] = res["COMP"]["criteria"]["pass"]
    (out / "result.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(by_year).to_csv(out / "by_year.csv", float_format="%.6f")
    print(json.dumps({k: res[k] for k in ("ONEQ_buyhold", "QQQ_buyhold")}, indent=1, default=float))
    for inst in ("COMP", "QQQ"):
        m = res[inst]
        print(f"{rule.code} on {inst}: CAGR {m['cagr']:+.2%} vs ONEQ {m['bench_cagr']:+.2%}, DD {m['max_dd']:.1%} "
              f"(ONEQ {m['bench_max_dd']:.1%}), t {m['t_monthly_excess']:.2f}, criteria {m['criteria']}")
    return res


def run_oneshot_stock(name: str) -> dict:
    assert name in ("test1", "test2")
    fr = frozen()
    out = OUT / f"oneshot_stock_{name}"
    if (out / "result.json").exists():
        raise SystemExit(f"{name} already run once ({out / 'result.json'}); a one-shot test is not re-run")
    out.mkdir(parents=True, exist_ok=True)
    data = lv.load_window(name)
    print(data.guard["assertion"])
    rule = RULE_BY_CODE[fr["stock_rule"]]
    runner = StockRunner(data)
    m, res, oneq, qqq = runner.run(rule)
    judged = stock_sub_metrics(res["nav"], oneq, qqq, data.spec["judged_from"], res["cost"], res["exposure"])
    res["nav"].to_frame("nav").assign(oneq=oneq, qqq=qqq, exposure=res["exposure"]).to_csv(out / "daily_nav.csv")
    res["trades"].to_csv(out / "trades.csv", index=False)
    judged["_monthly_active"].to_csv(out / "judged_monthly_active.csv", header=["active"])
    result = {"window": name, "rule": rule.code, "name": rule.name, "date_guard": data.guard,
              "full_window": _clean(m), "judged_from": data.spec["judged_from"], "judged": _clean(judged),
              "carried_universe_weeks": runner.carried_weeks, "open_positions_at_end": res["open"]}
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str))
    j = judged
    print(f"{name} judged {j['start']}..{j['end']}: CAGR {j['cagr']:+.1%} ONEQ {j['bench_cagr']:+.1%} QQQ "
          f"{j['qqq_cagr']:+.1%} DD {j['max_dd']:.0%} (ONEQ {j['bench_max_dd']:.0%}) t_m {j['t_excess_monthly']:.2f}")
    return result


def evaluate_stock() -> dict:
    r = {n: json.loads((OUT / f"oneshot_stock_{n}/result.json").read_text()) for n in ("test1", "test2")}
    ma = pd.concat([pd.read_csv(OUT / f"oneshot_stock_{n}/judged_monthly_active.csv", index_col=0)["active"]
                    for n in ("test1", "test2")])
    t = float(ma.mean() / ma.std(ddof=1) * math.sqrt(len(ma)))
    j = {n: r[n]["judged"] for n in r}
    a = {"cagr_above_oneq_each": {n: j[n]["cagr"] > j[n]["bench_cagr"] for n in j}, "combined_months": len(ma),
         "combined_t_monthly": t}
    a["pass"] = all(a["cagr_above_oneq_each"].values()) and t >= 2
    b = {n: {"dd_shallower_pts": abs(j[n]["bench_max_dd"]) - abs(j[n]["max_dd"]),
             "cagr_gap_pts": j[n]["cagr"] - j[n]["bench_cagr"]} for n in j}
    for n in j:
        b[n]["pass"] = b[n]["dd_shallower_pts"] >= 0.10 and b[n]["cagr_gap_pts"] >= -0.03
    b["pass"] = all(v["pass"] for v in b.values() if isinstance(v, dict))
    res = {"A": a, "B": b, "pass": a["pass"] or b["pass"]}
    (OUT / "oneshot_stock_evaluation.json").write_text(json.dumps(res, indent=2, default=str))
    print(json.dumps(res, indent=2, default=str))
    return res


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dev", choices=["index", "stock", "both"], help="development grids (behind the date guards)")
    p.add_argument("--freeze", nargs=2, metavar=("INDEX_CODE", "STOCK_CODE"), help="write frozen_rules.json")
    p.add_argument("--oneshot", choices=["index", "stock_test1", "stock_test2"], help="run one frozen one-shot (once)")
    p.add_argument("--evaluate-stock", action="store_true", help="apply the stock pass criteria to both one-shots")
    a = p.parse_args(argv)
    if a.dev in ("index", "both"):
        g = run_index_dev()
        print(g[["instrument", "code", "rule", "cagr", "bench_cagr", "max_dd", "bench_max_dd", "t_monthly_excess",
                 "crit_A", "crit_B", "own_crit_A", "own_crit_B", "trades_per_year"]].to_string())
        sel, why = pick(g[g["instrument"] == "COMP"].merge(
            g[g["instrument"] == "QQQ"][["code", "own_crit_A", "own_crit_B"]].rename(
                columns={"own_crit_A": "qqq_A", "own_crit_B": "qqq_B"}), on="code"), "t_monthly_excess", "qqq")
        print("index freeze candidate by the pre-registered rule:", sel["code"], sel["rule"], why)
    if a.dev in ("stock", "both"):
        g = run_stock_dev()
        sel, why = pick(g[g["n_buy"] >= 30], "t_excess_monthly")
        print("stock freeze candidate by the pre-registered rule:", sel["code"], sel["rule"], why)
    if a.freeze:
        write_frozen(*a.freeze)
        print("frozen:", FROZEN.read_text())
    if a.oneshot == "index":
        run_oneshot_index()
    elif a.oneshot:
        run_oneshot_stock(a.oneshot.split("_")[1])
    if a.evaluate_stock:
        evaluate_stock()


if __name__ == "__main__":
    main()
