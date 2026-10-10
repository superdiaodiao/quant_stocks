"""Selective "T" trading (做T) around a held base: the S1 / S2 / S3 signal families, the configurations (S3-Yb is
the forward-observed stock line B1) and the cash / margin account engine, with the Yahoo v8 data of the 18 stocks.

Extracted unchanged from studies/selective_t.py (phase 1 study; the engine moved here in phase 2 so that the
forward observation, quant.observation, and research_t_grid use it without importing a study): ``Market`` /
``market_from_frame`` / ``load_qqq_like`` / ``load_stock`` / ``fetch``, ``Spec`` / ``Fam`` / ``Cycle``, ``simulate``,
``run_one``, ``match_returns``, ``basket_mean`` and the frozen parameters.
"""
from __future__ import annotations

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
from quant.data.sources import yahoo
from quant.evaluation.metrics import TRADING_DAYS
from quant.paths import MAIN_CHECKOUT as MAIN, ROOT  # noqa: F401  (ROOT: forward observation)
from quant.signals import technical
from quant.strategies import registry



END = "2026-09-30"
CACHE = MAIN / "research_cache/selective_t"
RAW = CACHE / "raw"
FETCH_LOG = CACHE / "fetch_log.csv"
PANEL = MAIN / "research_cache/reversal_2012_2026_v2/prices"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
FETCH_START = "2011-01-01"
SECONDS_PER_REQUEST = 2.0
STOP_CODES = (401, 403, 429)
STOCKS = registry.U18            # the 18 large caps (frozen in quant.strategies.registry)
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


def match_returns(m: Market, s: int, e: int, w: float) -> pd.Series:
    return w * m.s.tr.iloc[s: e + 1]


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
