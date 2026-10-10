"""Selective / swing "T" trades (有条件、可跨天的做T) around a held base, pre-registered in
docs/research_ledger_selective_t.md (section 0). Not QuantConnect.

Base: 75% of a $10,000 cash account in integer shares, 25% settled cash as the reserve (~1/3 of the base).
A T cycle sells (S1, S2) or adds (S3) q = floor(B / 3) shares and is closed by a limit order or at the max hold:

- S1 (overextended, sell high / buy back lower): close_t >= (1 + X) * SMA20_t, or RSI14_t > 75 -> sell q at the
  open of t+1; buy back with a limit at the SMA20 up to the previous close; else at the close of holding day 20.
- S2 (gap up): open_t >= (1 + G) * close_{t-1} -> sell q just after the open at open * (1 - slip); buy back with a
  limit at close_{t-1} (gap fill) on the gap day and the 5 sessions after; else at the close of the 5th.
- S3 (big dip, add / sell on recovery): close_t / close_{t-1} - 1 <= -D, or close_t <= (1 - Y) * SMA20_t -> buy q
  at the open of t+1 from settled cash; sell with a limit at min(SMA20, entry * (1 + Z)) (entry * (1 + Z) when the
  SMA20 is not above the entry); else at the close of holding day 20.
- S4 = an S1 cycle and an S3 cycle running independently.

Cash account: every buy needs settled cash; sale proceeds settle the next session (T+1). Margin variant (report
only): 100% base, no reserve, negative cash charged T-bill + 1.5%, day-trade (PDT) counts. Limits need a trade
through of max($0.01, 0.05%). Costs: quant.backtest.costs.ibkr_order_cost (IBKR Tiered) plus a half-spread
(QQQ 1 bp, stocks / ONEQ 2 bp, at least half a cent) on every order.

Data: QQQ / ONEQ Yahoo charts of the calendar study; 18 large caps fetched here from Yahoo v8 (daily OHLC, splits,
dividends; one request per 2 s; raw bodies local only under research_cache/selective_t/raw). Every vendor frame is
truncated at ``END`` and asserted. Outputs (returns, counts, statistics; no price levels):
output/research_only/selective_t/.

Usage:  PYTHONPATH=. .venv/bin/python -m quant study selective_t [--fetch]   (or scripts/research_selective_t.py)
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

from quant.backtest import execution
from quant.backtest.costs import ibkr_order_cost, tick_half_spread
from quant.data.guards import assert_dev_dates
from quant.data.ohlc import OHLCSeries, build_real_ohlc
from quant.data.rates import daily_rf
from quant.data.sources import yahoo
from quant.evaluation.criteria import NORM, ab_criteria, bh_reject, bonferroni_t  # noqa: F401  (st.bh_reject)
from quant.evaluation.metrics import (
    TRADING_DAYS, cagr_of, core_metrics, max_drawdown, monthly, relative_metrics, t_and_ir, yearly,
)
from quant.paths import MAIN_CHECKOUT as MAIN, ROOT  # noqa: F401  (st.ROOT / st.MAIN: forward_prices)
from quant.signals import technical

END = "2026-09-30"
CACHE = MAIN / "research_cache/selective_t"
RAW = CACHE / "raw"
FETCH_LOG = CACHE / "fetch_log.csv"
PANEL = MAIN / "research_cache/reversal_2012_2026_v2/prices"
OUT = ROOT / "output/research_only/selective_t"

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
FETCH_START = "2011-01-01"
SECONDS_PER_REQUEST = 2.0
STOP_CODES = (401, 403, 429)

STOCKS = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX", "CSCO", "INTC", "ADBE",
          "QCOM", "PEP", "AMD", "CMCSA", "AMGN")
# frozen security master ids (v2 panel) per ticker, in time order
PANEL_IDS = {"AAPL": ["320193"], "MSFT": ["789019"], "NVDA": ["1045810"], "AMZN": ["1018724"],
             "GOOGL": ["1288776.A", "1652044.A"], "META": ["1326801"], "TSLA": ["1318605"],
             "AVGO": ["1441634", "1649338", "1730168"], "COST": ["909832"], "NFLX": ["1065280"],
             "CSCO": ["858877"], "INTC": ["50863"], "ADBE": ["796343"], "QCOM": ["804328"], "PEP": ["77476"],
             "AMD": ["2488"], "CMCSA": ["1166691.T-CMCSA"], "AMGN": ["318154"]}

START_EQUITY = 10_000.0
BASE_FRAC = 0.75
BAND = (0.70, 0.80)
BUFFER = 5.0
CAP_MULT = 1.10
SMA_N = 20
RSI_N = 14
MARGIN_SPREAD = 0.015
HALF_SPREAD_BPS = {"QQQ": 1.0, "ONEQ": 2.0}
STOCK_HALF_SPREAD_BPS = 2.0
S2_SLIP = {"QQQ": 0.0005, "stock": 0.0010}
STOCK_PERIODS = {"half1": ("2012-01-03", "2018-12-31"), "half2": ("2019-01-02", END), "full": ("2012-01-03", END)}
ONEQ_FIRST_RETURN = "2003-10-02"

# parameters per asset class (section 0.4)
PARAMS = {
    "QQQ": {"Xa": 0.03, "Xb": 0.06, "RSI": 75.0, "G": 0.02, "D": 0.02, "Ya": 0.03, "Yb": 0.06, "Z": 0.03},
    "stock": {"Xa": 0.05, "Xb": 0.10, "RSI": 75.0, "G": 0.04, "D": 0.04, "Ya": 0.05, "Yb": 0.10, "Z": 0.06},
}
MAX_HOLD = {"S1": 20, "S2": 6, "S3": 20}     # holding days, the entry day is day 1 (S2: gap day + 5 sessions)
CONFIGS = {   # name -> [(family, trigger, parameter key)]
    "S1-Xa": [("S1", "X", "Xa")], "S1-Xb": [("S1", "X", "Xb")], "S1-RSI": [("S1", "RSI", "RSI")],
    "S2": [("S2", "G", "G")],
    "S3-D": [("S3", "D", "D")], "S3-Ya": [("S3", "Y", "Ya")], "S3-Yb": [("S3", "Y", "Yb")],
    "S4a": [("S1", "X", "Xa"), ("S3", "Y", "Ya")], "S4b": [("S1", "RSI", "RSI"), ("S3", "D", "D")],
}
ASSETS = ("QQQ", "U18")
SOURCES = {"QQQ": MAIN / "research_cache/calendar/raw/chart_QQQ.json",       # the calendar study's QQQ / ONEQ
           "ONEQ": MAIN / "research_cache/calendar/raw/chart_ONEQ.json"}


ceil_cent = execution.ceil_cent
floor_cent = execution.floor_cent


def hs_bps(sym: str) -> float:
    return HALF_SPREAD_BPS.get(sym, STOCK_HALF_SPREAD_BPS)


def hs_of(sym: str, price: float) -> float:
    return tick_half_spread(hs_bps(sym), price)


def cost(shares: float, price: float, sell: bool, sym: str, on: bool = True) -> float:
    if not on or shares <= 0:
        return 0.0
    return ibkr_order_cost(shares, price, sell, hs_of(sym, price))["total"]


# ======================================================================== fetch (Yahoo v8, polite)

def chart_url(symbol: str, period1: str, period2: str) -> str:
    ep = lambda d: int(datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())  # noqa: E731
    params = urlencode({"period1": ep(period1), "period2": ep(period2), "interval": "1d",
                        "events": "div,splits", "includeAdjustedClose": "true"}, safe=",")
    return CHART.format(symbol=symbol) + "?" + params


def raw_path(sym: str) -> Path:
    return RAW / f"{sym}.json.gz"


def fetch(symbols=STOCKS, getter=None, sleep=time.sleep) -> dict:
    """One request per symbol without a cached body, one per 2 s; stops at the first 401/403/429."""
    RAW.mkdir(parents=True, exist_ok=True)
    period2 = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    out = {}
    if not FETCH_LOG.exists():
        FETCH_LOG.write_text("fetched_utc,symbol,http_status,bytes\n")
    for sym in symbols:
        if raw_path(sym).exists():
            out[sym] = "cached"
            continue
        url = chart_url(sym, FETCH_START, period2)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            if getter is not None:
                data, status = getter(url), 200
            else:
                with urlopen(Request(url, headers=HEADERS), timeout=30) as resp:
                    data, status = resp.read(), resp.status
        except HTTPError as exc:
            with FETCH_LOG.open("a") as fh:
                fh.write(f"{now},{sym},{exc.code},0\n")
            out[sym] = f"HTTP {exc.code}"
            if exc.code in STOP_CODES:
                print(f"fetch: HTTP {exc.code} on {sym}; stopping (re-run resumes)")
                break
            sleep(SECONDS_PER_REQUEST)
            continue
        payload = json.loads(data)
        res = (payload.get("chart") or {}).get("result") or [None]
        if not res[0] or (res[0].get("meta") or {}).get("dataGranularity") != "1d":
            out[sym] = "not daily / empty"
        else:
            tmp = raw_path(sym).with_suffix(".tmp")
            tmp.write_bytes(gzip.compress(data, mtime=0))
            tmp.rename(raw_path(sym))
            out[sym] = "ok"
        with FETCH_LOG.open("a") as fh:
            fh.write(f"{now},{sym},{status},{len(data)}\n")
        print(f"fetch: {sym} {out[sym]} ({len(data)} bytes)")
        sleep(SECONDS_PER_REQUEST)
    return out


# ======================================================================== data

@dataclass
class Market:
    sym: str
    s: OHLCSeries            # real OHLC, prev close (today's units), split, dividend, total return
    factor: np.ndarray       # real = split-adjusted * factor
    sma_now: np.ndarray      # SMA20 through t, today's units (signals at the close of t)
    sma_prev: np.ndarray     # SMA20 through t-1, today's units (limits resting during t)
    rsi: np.ndarray          # RSI14 (Wilder) through t


def ohlc_frame(payload: dict, end: str = END) -> tuple[pd.DataFrame, dict]:
    """date, open, high, low, close (split-adjusted), adjclose, dividend (split-adjusted); splits {date: ratio}."""
    return yahoo.parse_ohlc_payload(payload, end)


def rsi_wilder(c: np.ndarray, n: int = RSI_N) -> np.ndarray:
    return technical.rsi_wilder(c, n)


def market_from_frame(sym: str, df: pd.DataFrame, splits: dict) -> Market:
    bad = df[["open", "high", "low"]].isna().any(axis=1)
    if bad.any():   # a day without O/H/L: use the close for all three (counted in the data checks)
        for k in ("open", "high", "low"):
            df.loc[bad, k] = df.loc[bad, "close"]
    s = build_real_ohlc(sym, df, splits)
    s.checks["ohlc_filled_from_close"] = int(bad.sum())
    adj = df["close"].to_numpy(float)
    factor = s.close / adj
    sma = pd.Series(adj).rolling(SMA_N, min_periods=SMA_N).mean().to_numpy()
    return Market(sym, s, factor, sma * factor, np.r_[np.nan, sma[:-1]] * factor, rsi_wilder(adj))


def load_qqq_like(sym: str) -> Market:
    df = yahoo.parse_ohlc(SOURCES[sym], END)
    assert_dev_dates(df["date"], END)
    return market_from_frame(sym, df, yahoo.split_events(SOURCES[sym], END))


def load_stock(sym: str) -> Market:
    payload = json.loads(gzip.decompress(raw_path(sym).read_bytes()))
    df, splits = ohlc_frame(payload, END)
    assert_dev_dates(df["date"], END)
    return market_from_frame(sym, df, splits)


def panel_check(m: Market) -> dict:
    """Yahoo-derived real closes vs the frozen v2 panel close_raw on common dates; split dates vs split_factor."""
    frames = []
    for sid in PANEL_IDS.get(m.sym, []):
        p = PANEL / f"{sid}.csv"
        if p.exists():
            frames.append(pd.read_csv(p, usecols=["date", "close_raw", "split_factor"]))
    if not frames:
        return {"panel_days": 0}
    pan = pd.concat(frames).drop_duplicates("date", keep="last")
    pan = pan[pan["close_raw"] > 0]
    y = pd.DataFrame({"date": m.s.sessions.strftime("%Y-%m-%d"), "yahoo": m.s.close})
    j = y.merge(pan, on="date")
    rel = (j["yahoo"] / j["close_raw"] - 1).abs()
    ys = {str(d.date()) for d, r in zip(m.s.sessions, m.s.split) if r != 1.0}
    ps = set(pan.loc[pan["split_factor"] != 1, "date"])
    lo = j["date"].min() if len(j) else ""
    return {"panel_days": int(len(j)), "first_common": lo, "last_common": j["date"].max() if len(j) else "",
            "median_abs_rel_diff": float(rel.median()), "share_within_0p5pct": float((rel <= 0.005).mean()),
            "share_within_2pct": float((rel <= 0.02).mean()), "max_abs_rel_diff": float(rel.max()),
            "max_diff_date": str(j.loc[rel.idxmax(), "date"]) if len(j) else "",
            "yahoo_splits_in_panel_window": sorted(d for d in ys if lo <= d <= "2026-08-31"),
            "panel_splits": sorted(ps), "split_dates_agree": sorted(d for d in ys if lo <= d <= "2026-08-31") == sorted(
                d for d in ps if d >= lo)}


# ======================================================================== simulation

@dataclass(frozen=True)
class Fam:
    family: str        # S1 / S2 / S3
    trigger: str       # X / RSI / G / D / Y
    value: float
    z: float = 0.0     # S3 target gain
    max_hold: int = 20


@dataclass(frozen=True)
class Spec:
    fams: tuple = ()
    mode: str = "cash"           # cash / margin
    base_frac: float = BASE_FRAC
    rebalance: bool = True
    costs: bool = True
    through: bool = True
    slip: float = 0.0            # S2 sale slippage


@dataclass
class Cycle:
    fam: Fam
    q: int
    entry_px: float
    entry_i: int
    ref: float = 0.0             # S2: the gap-fill limit reference (previous close, today's units)
    days: int = 0
    c_entry: float = 0.0


def fams_of(config: str, asset_class: str) -> tuple:
    p = PARAMS[asset_class]
    return tuple(Fam(f, trig, p[key], p["Z"] if f == "S3" else 0.0, MAX_HOLD[f]) for f, trig, key in CONFIGS[config])


def signal(m: Market, t: int, fam: Fam) -> bool:
    """A close-of-t signal (S1 / S3)."""
    s = m.s
    if fam.trigger == "X":
        return bool(np.isfinite(m.sma_now[t]) and s.close[t] >= (1 + fam.value) * m.sma_now[t])
    if fam.trigger == "RSI":
        return bool(np.isfinite(m.rsi[t]) and m.rsi[t] > fam.value)
    if fam.trigger == "D":
        return bool(np.isfinite(s.prev_close[t]) and s.close[t] / s.prev_close[t] - 1 <= -fam.value)
    if fam.trigger == "Y":
        return bool(np.isfinite(m.sma_now[t]) and s.close[t] <= (1 - fam.value) * m.sma_now[t])
    raise ValueError(fam.trigger)


def exit_limit(m: Market, t: int, cy: Cycle) -> float:
    f = cy.fam.family
    if f == "S1":
        return floor_cent(m.sma_prev[t]) if np.isfinite(m.sma_prev[t]) else float("nan")
    if f == "S2":
        return floor_cent(cy.ref)
    tgt = cy.entry_px * (1 + cy.fam.z)
    sma = m.sma_prev[t]
    return ceil_cent(min(sma, tgt) if np.isfinite(sma) and sma > cy.entry_px else tgt)


def simulate(m: Market, s: int, e: int, spec: Spec, rf: pd.Series | None = None) -> dict:
    """$10,000 at the close of s-1; sessions s..e. Daily values, trip records, counters."""
    d, sym, on, cash_mode = m.s, m.sym, spec.costs, spec.mode == "cash"
    p0 = d.close[s - 1]
    b = int(math.floor(spec.base_frac * (START_EQUITY - BUFFER) / (p0 * (1 + hs_of(sym, p0)))))
    c0 = cost(b, p0, False, sym, on)
    settled, pending = START_EQUITY - b * p0 - c0, 0.0
    cost_frac = c0 / START_EQUITY
    cycles: dict[str, Cycle | None] = {f.family: None for f in spec.fams}
    queued: dict[str, tuple[Fam, int] | None] = {f.family: None for f in spec.fams}
    trips, values, expo, shortfall, interest = [], [START_EQUITY], [], 0, 0.0
    month = d.sessions.to_period("M")
    rfv = rf.reindex(d.sessions).fillna(0.0).to_numpy() if rf is not None else np.zeros(len(d.sessions))

    def held() -> int:
        n = b
        for cy in cycles.values():
            if cy is not None:
                n += cy.q if cy.fam.family == "S3" else -cy.q
        return n

    def pay(amount: float) -> None:   # a buy: settled cash first
        nonlocal settled
        settled -= amount

    def receive(amount: float) -> None:  # a sale: settles next session in the cash account
        nonlocal settled, pending
        if cash_mode:
            pending += amount
        else:
            settled += amount

    def close_cycle(fam_name: str, t: int, px: float, reason: str) -> None:
        nonlocal shortfall, cost_frac
        cy = cycles[fam_name]
        sell_first = cy.fam.family in ("S1", "S2")
        c_exit = cost(cy.q, px, not sell_first, sym, on)
        if sell_first:
            if cash_mode and settled < cy.q * px + c_exit:
                shortfall += 1
            pay(cy.q * px + c_exit)
            gross = cy.q * (cy.entry_px - px)
        else:
            receive(cy.q * px - c_exit)
            gross = cy.q * (px - cy.entry_px)
        cy_cost = cy.c_entry + c_exit
        cost_frac += c_exit / max(values[-1], 1.0)
        trips.append({"family": cy.fam.family, "entry": d.sessions[cy.entry_i], "exit": d.sessions[t], "q": cy.q,
                      "gross": gross, "cost": cy_cost, "notional": cy.q * cy.entry_px, "reason": reason,
                      "days": cy.days, "same_day": t == cy.entry_i})
        cycles[fam_name] = None

    def open_cycle(fam: Fam, t: int, q: int, px: float, ref: float = 0.0) -> None:
        nonlocal shortfall, cost_frac
        sell_first = fam.family in ("S1", "S2")
        c_in = cost(q, px, sell_first, sym, on)
        if sell_first:
            receive(q * px - c_in)
        else:
            if cash_mode and settled < q * px + c_in:
                shortfall += 1
            pay(q * px + c_in)
        cost_frac += c_in / max(values[-1], 1.0)
        cycles[fam.family] = Cycle(fam, q, px, t, ref, c_entry=c_in)

    for t in range(s, e + 1):
        r = d.split[t]
        if r != 1.0:
            b = int(round(b * r))
            for cy in cycles.values():
                if cy is not None:
                    cy.q, cy.entry_px, cy.ref = int(round(cy.q * r)), cy.entry_px / r, cy.ref / r
            for k, v in queued.items():
                if v is not None:
                    queued[k] = (v[0], int(round(v[1] * r)))
        settled += pending                       # yesterday's sales settle (T+1)
        pending = 0.0
        settled += held() * d.div[t]              # shares held at the previous close
        if not cash_mode and settled < 0:
            i_ = -settled * (rfv[t] + MARGIN_SPREAD / TRADING_DAYS)
            settled -= i_
            interest += i_
        o, h, lo, c = d.open[t], d.high[t], d.low[t], d.close[t]
        # 1. entries at the open: queued close signals (S1 sell / S3 buy), then the S2 gap check
        for name, qd in list(queued.items()):
            if qd is not None:
                fam, q = qd
                queued[name] = None
                if q > 0:
                    open_cycle(fam, t, q, o)
        for fam in spec.fams:
            if fam.family == "S2" and cycles["S2"] is None and t < e and np.isfinite(d.prev_close[t]) \
                    and o >= (1 + fam.value) * d.prev_close[t]:
                q = b // 3
                if q > 0:
                    open_cycle(fam, t, q, o * (1 - spec.slip), ref=d.prev_close[t])
        # 2. exits: resting limits during the day, then the max-hold close
        for name, cy in list(cycles.items()):
            if cy is None:
                continue
            cy.days += 1
            lim = exit_limit(m, t, cy)
            px = None
            if np.isfinite(lim):
                if cy.fam.family in ("S1", "S2"):
                    ok = (not cash_mode) or settled >= cy.q * lim + cost(cy.q, lim, False, sym, on) + BUFFER
                    if ok:
                        px = execution.fill_buy_limit(o, lo, lim, spec.through)
                else:
                    px = execution.fill_sell_limit(o, h, lim, spec.through)
            if px is not None:
                close_cycle(name, t, px, "target")
            elif cy.days >= cy.fam.max_hold:
                close_cycle(name, t, c, "timeout")
            elif t == e:
                close_cycle(name, t, c, "end")
        # 3. close signals for the next open
        if t < e:
            for fam in spec.fams:
                if fam.family in ("S1", "S3") and cycles[fam.family] is None and queued[fam.family] is None \
                        and signal(m, t, fam):
                    q = b // 3
                    if fam.family == "S3" and cash_mode:
                        q = min(q, int(max(0.0, math.floor((settled + pending - BUFFER) / (CAP_MULT * c)))))
                    queued[fam.family] = (fam, q)
        v = held() * c + settled + pending
        expo.append(held() * c / v)
        # 4. month-end base rebalance (cash account, nothing open or queued)
        if spec.rebalance and t < e and month[t + 1] != month[t] and all(x is None for x in cycles.values()) \
                and all(x is None for x in queued.values()):
            w = b * c / v
            if not (BAND[0] <= w <= BAND[1]):
                nb = int(math.floor(spec.base_frac * (v - BUFFER) / (c * (1 + hs_of(sym, c)))))
                dq = nb - b
                if dq > 0 and cash_mode:
                    dq = min(dq, int(max(0.0, math.floor((settled - BUFFER) / (c * (1 + hs_of(sym, c)) + 0.01)))))
                if dq != 0:
                    cc = cost(abs(dq), c, dq < 0, sym, on)
                    if dq > 0:
                        pay(dq * c + cc)
                    else:
                        receive(-dq * c - cc)
                    cost_frac += cc / v
                    b += dq
                    v = held() * c + settled + pending
        values.append(v)
    val = pd.Series(values, index=d.sessions[s - 1: e + 1])
    tdf = pd.DataFrame(trips, columns=["family", "entry", "exit", "q", "gross", "cost", "notional", "reason", "days",
                                       "same_day"])
    sd = pd.Series(0, index=d.sessions[s: e + 1])
    if not tdf.empty:
        cnt = tdf.loc[tdf["same_day"].astype(bool), "exit"].value_counts()
        sd = sd.add(cnt.reindex(sd.index).fillna(0).astype(int), fill_value=0)
    pdt = sd.rolling(5, min_periods=1).sum() > 3
    return {"value": val, "ret": val.pct_change().iloc[1:], "trips": tdf, "exposure": float(np.mean(expo)),
            "cost_frac": cost_frac, "shortfall_days": shortfall, "day_trades": int(sd.sum()),
            "pdt_breach_share": float(pdt.mean()), "interest": interest}


# ======================================================================== periods, metrics

def first_valid(m: Market) -> int:
    return int(np.argmax(np.isfinite(m.sma_prev) & np.isfinite(m.rsi)))


def qqq_bounds(m: Market) -> dict:
    s = max(first_valid(m), 1)
    e = int(m.s.sessions.searchsorted(pd.Timestamp(END), side="right")) - 1
    mid = s + (e - s + 1) // 2
    return {"full": (s, e), "half1": (s, mid - 1), "half2": (mid, e)}


def stock_bounds(m: Market) -> dict:
    out = {}
    fv = max(first_valid(m), 1)
    for k, (a, b_) in STOCK_PERIODS.items():
        s = max(int(m.s.sessions.searchsorted(pd.Timestamp(a))), fv)
        e = int(m.s.sessions.searchsorted(pd.Timestamp(b_), side="right")) - 1
        if e - s > 60:
            out[k] = (s, e)
    return out


def match_returns(m: Market, s: int, e: int, w: float) -> pd.Series:
    return w * m.s.tr.iloc[s: e + 1]


def trip_stats(tr: pd.DataFrame, stock_years: float) -> dict:
    if tr.empty:
        return {"trips": 0, "trips_per_year": 0.0, "win_rate_net": float("nan"), "gross_per_trip_bp": float("nan"),
                "net_per_trip_bp": float("nan"), "gross_per_trip_usd": float("nan"), "net_per_trip_usd": float("nan"),
                "cost_per_trip_bp": float("nan"), "share_target": float("nan"), "share_timeout": float("nan"),
                "share_end": float("nan"), "mean_days": float("nan"), "same_day_share": float("nan")}
    net = tr["gross"] - tr["cost"]
    rn = tr["reason"].value_counts(normalize=True)
    return {"trips": int(len(tr)), "trips_per_year": len(tr) / stock_years,
            "win_rate_net": float((net > 0).mean()),
            "gross_per_trip_bp": float((tr["gross"] / tr["notional"]).mean() * 1e4),
            "net_per_trip_bp": float((net / tr["notional"]).mean() * 1e4),
            "gross_per_trip_usd": float(tr["gross"].mean()), "net_per_trip_usd": float(net.mean()),
            "cost_per_trip_bp": float((tr["cost"] / tr["notional"]).mean() * 1e4),
            "share_target": float(rn.get("target", 0.0)), "share_timeout": float(rn.get("timeout", 0.0)),
            "share_end": float(rn.get("end", 0.0)), "mean_days": float(tr["days"].mean()),
            "same_day_share": float(tr["same_day"].astype(bool).mean())}


def evaluate(r: pd.Series, refs: dict, oneq: pd.Series, rf: pd.Series) -> dict:
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
         **core_metrics(r, rf)}
    for tag, ref in refs.items():
        ref = ref.reindex(r.index)
        m[f"{tag}_cagr"] = cagr_of(ref)
        m[f"excess_vs_{tag}"] = m["cagr"] - cagr_of(ref)
        m[f"{tag}_max_dd"] = max_drawdown(pd.concat([pd.Series([1.0]), (1 + ref).cumprod()]))
        t, ir = t_and_ir(monthly(r) - monthly(ref))
        m[f"t_monthly_excess_vs_{tag}"], m[f"ir_vs_{tag}"] = t, ir
    m.update(relative_metrics(r, oneq, rf, "oneq"))
    return m


def judge(per: dict) -> dict:
    h1, h2, f = per["half1"], per["half2"], per["full"]
    adds = (h1["excess_vs_hbase"] > 0 and h2["excess_vs_hbase"] > 0 and f["t_monthly_excess_vs_hbase"] >= 2.0
            and h1["excess_vs_hmatch"] > 0 and h2["excess_vs_hmatch"] > 0)
    a, b = ab_criteria((h1, h2), f, cagr="excess_vs_oneq", bench=None)
    return {"adds_value_vs_hbase": bool(adds), "A": bool(a), "B": bool(b), "pass_vs_oneq": bool(a or b),
            "worth_forward_watch": bool(adds and (a or b))}


# ======================================================================== runs per asset

VARIANTS = ("net", "gross", "touch_fill", "margin")


def variant_spec(fams: tuple, vn: str, slip: float) -> Spec:
    base = Spec(fams, slip=slip)
    if vn == "gross":
        return replace(base, costs=False)
    if vn == "touch_fill":
        return replace(base, through=False)
    if vn == "margin":
        return replace(base, mode="margin", base_frac=1.0, rebalance=False)
    return base


def ref_specs(vn: str) -> dict:
    costs = vn != "gross"
    return {"hbase": Spec((), costs=costs), "h100": Spec((), mode="margin", base_frac=1.0, rebalance=False,
                                                        costs=costs)}


def run_one(m: Market, s: int, e: int, config: str, asset_class: str, vn: str, rf: pd.Series, ref_cache: dict) -> dict:
    """One market, one period, one variant: the rule's daily returns, its references and trip records."""
    key = (m.sym, s, e, vn != "gross")
    if key not in ref_cache:
        ref_cache[key] = {k: simulate(m, s, e, sp, rf)["ret"] for k, sp in ref_specs(vn).items()}
    slip = S2_SLIP["QQQ" if asset_class == "QQQ" else "stock"]
    sim = simulate(m, s, e, variant_spec(fams_of(config, asset_class), vn, slip), rf)
    return {"ret": sim["ret"], "hbase": ref_cache[key]["hbase"], "h100": ref_cache[key]["h100"],
            "hmatch": match_returns(m, s, e, sim["exposure"]), "sim": sim}


def basket_mean(series: list) -> pd.Series:
    return pd.concat(series, axis=1).mean(axis=1, skipna=True).dropna()


def run(args=None) -> dict:
    if args is not None and getattr(args, "fetch", False):
        print("FETCH:", fetch())
    qqq, oneq = load_qqq_like("QQQ"), load_qqq_like("ONEQ")
    stocks = {}
    for sym in STOCKS:
        if raw_path(sym).exists():
            stocks[sym] = load_stock(sym)
    missing = [x for x in STOCKS if x not in stocks]
    if missing:
        raise SystemExit(f"missing raw data for {missing}; run with --fetch")
    rf = daily_rf(qqq.s.sessions, END)
    checks = {"QQQ": qqq.s.checks, "ONEQ": oneq.s.checks}
    pc_rows = []
    for sym, m in stocks.items():
        pc = panel_check(m)
        checks[sym] = {**m.s.checks, **pc}
        pc_rows.append({"symbol": sym, **{k: v for k, v in m.s.checks.items() if k != "splits"},
                        "splits": json.dumps(m.s.checks["splits"]), **{k: (json.dumps(v) if isinstance(v, list) else v)
                                                                        for k, v in pc.items()}})
        extra = pd.DatetimeIndex(m.s.sessions).difference(qqq.s.sessions)
        if len(extra):
            raise ValueError(f"{sym} sessions not in the QQQ calendar: {list(extra[:5])}")
    OUT.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(pc_rows).to_csv(OUT / "data_checks.csv", index=False, float_format="%.6g")
    print("DATA CHECKS:", json.dumps({k: {kk: vv for kk, vv in v.items() if kk in (
        "first", "last", "ohlc_missing_or_nonpositive", "open_or_close_outside_high_low", "ohlc_filled_from_close",
        "median_abs_rel_diff", "share_within_0p5pct", "max_abs_rel_diff", "split_dates_agree")} for k, v in checks.items()},
        default=str))

    n_trials = len(CONFIGS) * len(ASSETS)
    res = {"end": END, "n_trials": n_trials, "bonferroni_t_one_sided_5pct": bonferroni_t(n_trials),
           "data_checks": checks, "assets": {}}
    rows, by_year, trips_year, per_stock, mon = [], {}, [], [], {}
    ref_cache: dict = {}

    def oneq_for(index: pd.DatetimeIndex) -> pd.Series:
        so = max(index[0], pd.Timestamp(ONEQ_FIRST_RETURN))
        return execution.buy_hold_returns(oneq.s, qqq.s.sessions, so, index[-1], hs_bps("ONEQ"), True)

    # ---------------- QQQ
    qb = qqq_bounds(qqq)
    res["assets"]["QQQ"] = {"periods": {k: [str(qqq.s.sessions[s].date()), str(qqq.s.sessions[e].date())]
                                        for k, (s, e) in qb.items()}, "configs": {}, "references": {}}
    for pk, (s, e) in qb.items():
        hb = simulate(qqq, s, e, ref_specs("net")["hbase"], rf)["ret"]
        h1 = simulate(qqq, s, e, ref_specs("net")["h100"], rf)["ret"]
        oq = oneq_for(hb.index)
        for nm, rr in (("H_base", hb), ("H100", h1)):
            mm = evaluate(rr, {"hbase": hb, "h100": h1}, oq, rf)
            res["assets"]["QQQ"]["references"].setdefault(pk, {})[nm] = mm
            rows.append({"asset": "QQQ", "config": nm, "variant": "net", "period": pk, **mm})
        if pk == "full":
            by_year["QQQ_H_base"], by_year["QQQ_H100"], by_year["QQQ_ONEQ"] = yearly(hb), yearly(h1), yearly(oq)
    for cfg in CONFIGS:
        info = {"periods": {}}
        for pk, (s, e) in qb.items():
            years = (e - s + 1) / TRADING_DAYS
            pr = {}
            for vn in VARIANTS:
                o = run_one(qqq, s, e, cfg, "QQQ", vn, rf, ref_cache)
                refs = {"hbase": o["hbase"], "hmatch": o["hmatch"], "h100": o["h100"]}
                mm = evaluate(o["ret"], refs, oneq_for(o["ret"].index), rf)
                mm.update(trip_stats(o["sim"]["trips"], years))
                mm.update({"exposure": o["sim"]["exposure"], "cost_drag_per_year": o["sim"]["cost_frac"] / years,
                           "shortfall_days": o["sim"]["shortfall_days"],
                           "day_trades_per_year": o["sim"]["day_trades"] / years,
                           "pdt_breach_share": o["sim"]["pdt_breach_share"]})
                pr[vn] = mm
                rows.append({"asset": "QQQ", "config": cfg, "variant": vn, "period": pk, **mm})
                if pk == "full" and vn == "net":
                    by_year[f"QQQ_{cfg}"] = yearly(o["ret"])
                    mon[f"QQQ_{cfg}_minus_hbase"] = monthly(o["ret"]) - monthly(o["hbase"])
                    trips_year.append(trips_by_year(o["sim"]["trips"], "QQQ", cfg))
            info["periods"][pk] = pr
        info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in qb})
        res["assets"]["QQQ"]["configs"][cfg] = info
        report_line("QQQ", cfg, info)

    # ---------------- U18 basket (each stock its own $10k account; basket = equal-weight mean of daily returns)
    sb = {sym: stock_bounds(m) for sym, m in stocks.items()}
    res["assets"]["U18"] = {"periods": {k: list(v) for k, v in STOCK_PERIODS.items()}, "configs": {}, "references": {}}
    for pk in STOCK_PERIODS:
        hb = basket_mean([simulate(m, *sb[x][pk], ref_specs("net")["hbase"], rf)["ret"] for x, m in stocks.items()
                          if pk in sb[x]])
        h1 = basket_mean([simulate(m, *sb[x][pk], ref_specs("net")["h100"], rf)["ret"] for x, m in stocks.items()
                          if pk in sb[x]])
        oq = oneq_for(hb.index)
        for nm, rr in (("H_base", hb), ("H100", h1)):
            mm = evaluate(rr, {"hbase": hb, "h100": h1}, oq, rf)
            res["assets"]["U18"]["references"].setdefault(pk, {})[nm] = mm
            rows.append({"asset": "U18", "config": nm, "variant": "net", "period": pk, **mm})
        if pk == "full":
            by_year["U18_H_base"], by_year["U18_H100"], by_year["U18_ONEQ"] = yearly(hb), yearly(h1), yearly(oq)
    for cfg in CONFIGS:
        info = {"periods": {}}
        for pk in STOCK_PERIODS:
            pr = {}
            for vn in VARIANTS:
                outs = {x: run_one(m, *sb[x][pk], cfg, "stock", vn, rf, ref_cache) for x, m in stocks.items()
                        if pk in sb[x]}
                r = basket_mean([o["ret"] for o in outs.values()])
                refs = {k: basket_mean([o[k] for o in outs.values()]) for k in ("hbase", "hmatch", "h100")}
                mm = evaluate(r, refs, oneq_for(r.index), rf)
                stock_years = sum(len(o["ret"]) for o in outs.values()) / TRADING_DAYS
                trips = pd.concat([o["sim"]["trips"].assign(symbol=x) for x, o in outs.items()], ignore_index=True)
                mm.update(trip_stats(trips, stock_years))
                mm.update({"trips_per_year_basket_total": len(trips) / (len(r) / TRADING_DAYS),
                           "exposure": float(np.mean([o["sim"]["exposure"] for o in outs.values()])),
                           "cost_drag_per_year": float(np.mean([o["sim"]["cost_frac"] / (len(o["ret"]) / TRADING_DAYS)
                                                                for o in outs.values()])),
                           "shortfall_days": int(sum(o["sim"]["shortfall_days"] for o in outs.values())),
                           "day_trades_per_year": float(np.mean([o["sim"]["day_trades"] / (len(o["ret"]) / TRADING_DAYS)
                                                                 for o in outs.values()])),
                           "pdt_breach_share": float(np.mean([o["sim"]["pdt_breach_share"] for o in outs.values()])),
                           "n_stocks": len(outs)})
                pr[vn] = mm
                rows.append({"asset": "U18", "config": cfg, "variant": vn, "period": pk, **mm})
                if vn == "net":
                    for x, o in outs.items():
                        yrs = len(o["ret"]) / TRADING_DAYS
                        ts_ = trip_stats(o["sim"]["trips"], yrs)
                        per_stock.append({"config": cfg, "period": pk, "symbol": x,
                                          "cagr": cagr_of(o["ret"]), "hbase_cagr": cagr_of(o["hbase"]),
                                          "excess_vs_hbase": cagr_of(o["ret"]) - cagr_of(o["hbase"]),
                                          "excess_vs_hmatch": cagr_of(o["ret"]) - cagr_of(o["hmatch"]),
                                          "t_monthly_excess_vs_hbase": t_and_ir(monthly(o["ret"]) - monthly(o["hbase"]))[0],
                                          "trips_per_year": ts_["trips_per_year"], "win_rate_net": ts_["win_rate_net"],
                                          "net_per_trip_bp": ts_["net_per_trip_bp"], "share_target": ts_["share_target"]})
                    if pk == "full":
                        by_year[f"U18_{cfg}"] = yearly(r)
                        mon[f"U18_{cfg}_minus_hbase"] = monthly(r) - monthly(refs["hbase"])
                        trips_year.append(trips_by_year(trips, "U18", cfg))
            info["periods"][pk] = pr
        info["judgement"] = judge({pk: info["periods"][pk]["net"] for pk in STOCK_PERIODS})
        res["assets"]["U18"]["configs"][cfg] = info
        report_line("U18", cfg, info)

    # ---------------- multiple testing over the 18 trials
    names, tvals = [], []
    for a in ASSETS:
        for cfg in CONFIGS:
            names.append(f"{a} {cfg}")
            tvals.append(res["assets"][a]["configs"][cfg]["periods"]["full"]["net"]["t_monthly_excess_vs_hbase"])
    p = np.array([1 - NORM.cdf(t) for t in tvals])
    rej = bh_reject(p)
    res["multiple_testing"] = {n: {"t_vs_hbase": t, "p_one_sided": float(pp), "bh_reject_q05": bool(rj),
                                   "above_bonferroni": bool(t >= res["bonferroni_t_one_sided_5pct"])}
                               for n, t, pp, rj in zip(names, tvals, p, rej)}
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=str))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    pd.concat([x for x in trips_year if x is not None]).to_csv(OUT / "trips_by_year.csv", index=False,
                                                                float_format="%.4f")
    pd.DataFrame(per_stock).to_csv(OUT / "per_stock.csv", index=False, float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_excess_vs_hbase.csv", float_format="%.6f")
    return res


def trips_by_year(tr: pd.DataFrame, asset: str, cfg: str) -> pd.DataFrame | None:
    if tr.empty:
        return None
    tr = tr.copy()
    tr["net"] = tr["gross"] - tr["cost"]
    tr["net_bp"] = tr["net"] / tr["notional"] * 1e4
    tr["gross_bp"] = tr["gross"] / tr["notional"] * 1e4
    g = tr.groupby([tr["exit"].dt.year, "family"])
    out = pd.DataFrame({"trips": g.size(), "win_rate_net": g["net"].apply(lambda x: (x > 0).mean()),
                        "gross_bp_mean": g["gross_bp"].mean(), "net_bp_mean": g["net_bp"].mean(),
                        "share_target": g["reason"].apply(lambda x: (x == "target").mean())}).reset_index()
    out = out.rename(columns={"exit": "year"})
    out.insert(0, "config", cfg)
    out.insert(0, "asset", asset)
    return out


def report_line(asset: str, cfg: str, info: dict) -> None:
    f, h1, h2 = (info["periods"][k]["net"] for k in ("full", "half1", "half2"))
    print(f"{asset} {cfg}: trips/yr {f['trips_per_year']:.1f}, win {f['win_rate_net']:.0%}, gross/net per trip "
          f"{f['gross_per_trip_bp']:+.0f}/{f['net_per_trip_bp']:+.0f} bp, target {f['share_target']:.0%}; vs H_base "
          f"h1 {h1['excess_vs_hbase']:+.2%} h2 {h2['excess_vs_hbase']:+.2%} t {f['t_monthly_excess_vs_hbase']:+.2f}; "
          f"vs H_match h1 {h1['excess_vs_hmatch']:+.2%} h2 {h2['excess_vs_hmatch']:+.2%}; {info['judgement']}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--fetch", action="store_true", help="fetch missing Yahoo charts first (one per 2 s)")
    ap.add_argument("--fetch-only", action="store_true")
    a = ap.parse_args(argv)
    if a.fetch_only:
        print(fetch())
        return
    run(a)


if __name__ == "__main__":
    main()
