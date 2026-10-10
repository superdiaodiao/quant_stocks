"""Sector / index ETF panel with the tradable legs of the leverage studies (benchmark, 2x leg, cash, IEF).

Extracted unchanged from scripts/research_sector_lev.py (``Data``, ``build_legs``, ``chain_qqq``, ``load_data``,
``data_checks``); also read by research_voltarget and research_leverage_methods. Calendar: SPY sessions from
``CAL_START``. Legs: BENCH = ONEQ total return, Nasdaq Composite price before ONEQ's first return; QLDX = synthetic 2x
before QLD's first return, real QLD after; CASH = T-bill ETF; IEFX = IEF, T-bill ETF before IEF lists.
Every vendor frame is truncated at ``end`` right after parsing and asserted.

``data_checks`` uses two return metrics (``cagr_of``, ``monthly``) from quant.evaluation.metrics: pure functions,
the one place where the data layer reads the evaluation layer (docs/architecture.md section 3).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.rates import fill_rf, load_dtb3, load_kf_rf
from quant.data.sources.yahoo import parse_chart
from quant.data.synthetic import CASH_ETF_FEE, NOMINAL_PRICE, synthetic_2x
from quant.evaluation.metrics import cagr_of, monthly
from quant.paths import CACHE_ROOT

TRADING_DAYS = 252
CAL_START = "1998-01-02"          # SPY sessions from here define the calendar (NDX warm-up for the SMA200)
RAW = CACHE_ROOT / "sector_lev/raw"
PRIOR_QQQ = CACHE_ROOT / "regime/raw/chart_QQQ.json"
SECTORS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH", "SOXX")
ETFS = SECTORS + ("SPY", "QQQ", "ONEQ", "IEF", "QLD")
INDICES = {"IXIC": "%5EIXIC", "NDX": "%5ENDX"}
SPREAD_2X = 0.0070                # frozen: calibrated on real QLD 2006-06-22 .. 2014-12-31 (QQQ timing ledger)
ONEQ_START = "2003-10-01"
LEVERAGE_LEGS = ["QQQ", "QLDX", "CASH"]       # part B / voltarget legs: QQQ, the 2x leg, the T-bill ETF
LEG_HALF_SPREAD = {**{t: 1e-4 for t in SECTORS[:9]}, "SMH": 2e-4, "SOXX": 2e-4, "SPY": 1e-4, "QQQ": 1e-4,
                   "BENCH": 2e-4, "QLDX": 2e-4, "CASH": 1e-4, "IEFX": 1e-4}


@dataclass
class Data:
    sessions: pd.DatetimeIndex
    adj: pd.DataFrame        # adjusted closes of the ETFs (NaN before listing)
    rets: pd.DataFrame       # daily returns of every tradable leg (incl. BENCH, QLDX, CASH, IEFX)
    close: pd.DataFrame      # split-adjusted closes for share counts (nominal $50 for synthetic legs)
    p_qqq: pd.Series         # QQQ total-return level, NDX price chained before 1999-03-10 (signal only)
    rf: pd.Series
    raw: dict
    guard: dict


def build_legs(adj: pd.DataFrame, close: pd.DataFrame, ixic: pd.Series, rf: pd.Series,
               spread: float = SPREAD_2X) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Returns / closes of every leg; spliced legs switch to the real ETF from its first daily return."""
    rets = adj.pct_change(fill_method=None)
    px = close.copy()
    info = {}
    # benchmark: ONEQ, Nasdaq Composite price before ONEQ's first return
    b = rets["ONEQ"].copy()
    first = b.first_valid_index()
    pre = ixic.pct_change(fill_method=None)
    b.loc[:first] = pre.loc[:first]
    b.loc[first] = rets.loc[first, "ONEQ"]
    rets["BENCH"] = b
    px["BENCH"] = close["ONEQ"].fillna(NOMINAL_PRICE)
    # 2x leg: synthetic before QLD's first daily return
    syn = synthetic_2x(rets["QQQ"], rf, spread)
    q = rets["QLD"]
    fq = q.first_valid_index()
    rets["QLDX"] = syn.where(rets.index < fq, q)
    px["QLDX"] = close["QLD"].fillna(NOMINAL_PRICE)
    info["qld_first_return"] = str(fq.date())
    # cash: T-bill ETF
    rets["CASH"] = rf - CASH_ETF_FEE / TRADING_DAYS
    px["CASH"] = NOMINAL_PRICE
    # IEF, T-bill ETF before IEF's first daily return
    fi = rets["IEF"].first_valid_index()
    rets["IEFX"] = rets["CASH"].where(rets.index < fi, rets["IEF"])
    px["IEFX"] = close["IEF"].fillna(NOMINAL_PRICE)
    info["ief_first_return"] = str(fi.date())
    return rets, px, info


def chain_qqq(adj_qqq: pd.Series, ndx: pd.Series) -> pd.Series:
    """QQQ total-return level, NDX price returns before QQQ's first close (used only for the SMA200 signal)."""
    fq = adj_qqq.first_valid_index()
    r = adj_qqq.pct_change(fill_method=None)
    rn = ndx.pct_change(fill_method=None)
    r = r.where(r.index > fq, rn)
    r.loc[fq] = ndx.loc[fq] / ndx.loc[:fq].iloc[-2] - 1
    r = r.loc[ndx.first_valid_index():].iloc[1:]
    return (1 + r.fillna(0)).cumprod()


def load_data(end: str, raw_dir: Path = RAW) -> Data:
    raw = {t: parse_chart(raw_dir / f"chart_{t}.json", end) for t in ETFS}
    raw.update({k: parse_chart(raw_dir / f"chart_{v}.json", end) for k, v in INDICES.items()})
    spy = raw["SPY"]
    sessions = pd.DatetimeIndex(spy.loc[pd.to_datetime(spy["date"]) >= CAL_START, "date"])
    adj, close = {}, {}
    for t, df in raw.items():
        df = df[pd.to_datetime(df["date"]) >= sessions[0]]
        ix = pd.DatetimeIndex(df["date"])
        extra = ix.difference(sessions)
        if len(extra):
            raise ValueError(f"{t} has sessions not in the SPY calendar: {list(extra[:5])}")
        adj[t] = pd.Series(df["adjclose"].values, index=ix).reindex(sessions)
        close[t] = pd.Series(df["close"].values, index=ix).reindex(sessions)
    adj, close = pd.DataFrame(adj), pd.DataFrame(close)
    rf = fill_rf(load_kf_rf(end), load_dtb3(end), sessions)
    etf_adj, etf_close = adj[list(ETFS)], close[list(ETFS)]
    rets, px, info = build_legs(etf_adj, etf_close, adj["IXIC"], rf)
    p_qqq = chain_qqq(adj["QQQ"], adj["NDX"]).reindex(sessions)
    guard = {"end": end, **info}
    for t, df in raw.items():
        assert_dev_dates(df["date"], end)
        guard[t] = {"first": df["date"].iloc[0], "last": df["date"].iloc[-1], "rows": int(len(df))}
    assert_dev_dates(sessions, end)
    return Data(sessions=sessions, adj=etf_adj, rets=rets, close=px, p_qqq=p_qqq, rf=rf, raw=raw, guard=guard)


def data_checks(d: Data, end: str) -> dict:
    out = {}
    for t in ETFS:
        df = d.raw[t]
        a = df["adjclose"].pct_change()
        c = (df["close"] + df["dividend"]) / df["close"].shift(1) - 1
        diff = (a - c).dropna()
        listed = d.sessions[d.sessions >= pd.Timestamp(df["date"].iloc[0])]
        out[t] = {"first": df["date"].iloc[0], "missing_sessions_after_listing": int(d.adj[t].reindex(listed).isna().sum()),
                  "max_abs_adj_vs_close_div": float(diff.abs().max()), "max_abs_daily_return": float(a.abs().max())}
    ov = d.rets.loc[pd.Timestamp(ONEQ_START) + pd.Timedelta(days=1):]
    ix = d.raw["IXIC"].set_index(pd.DatetimeIndex(d.raw["IXIC"]["date"]))["adjclose"].reindex(d.sessions).pct_change(fill_method=None)
    ix = ix.reindex(ov.index)
    out["ONEQ_minus_IXIC_price_overlap"] = {"window": [str(ov.index[0].date()), str(ov.index[-1].date())],
                                            "cagr_oneq": cagr_of(ov["ONEQ"]), "cagr_ixic_price": cagr_of(ix),
                                            "gap": cagr_of(ov["ONEQ"]) - cagr_of(ix)}
    real = d.rets["QLD"].dropna()
    syn = synthetic_2x(d.rets["QQQ"], d.rf, SPREAD_2X).reindex(real.index)
    dd = syn - real
    out["synthetic_2x_vs_real_QLD"] = {"window": [str(real.index[0].date()), str(real.index[-1].date())],
                                      "spread": SPREAD_2X, "cagr_real": cagr_of(real), "cagr_syn": cagr_of(syn),
                                      "gap_syn_minus_real": cagr_of(syn) - cagr_of(real),
                                      "te_daily_ann": float(dd.std() * math.sqrt(TRADING_DAYS)),
                                      "te_monthly_ann": float((monthly(syn) - monthly(real)).std() * math.sqrt(12)),
                                      "corr": float(np.corrcoef(syn, real)[0, 1])}
    if PRIOR_QQQ.exists():
        p = parse_chart(PRIOR_QQQ, end)
        pr = pd.Series(p["adjclose"].values, index=pd.DatetimeIndex(p["date"])).pct_change()
        j = pd.concat([pr, d.rets["QQQ"]], axis=1, join="inner").dropna()
        out["QQQ_vs_regime_cache"] = {"sessions": int(len(j)), "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())}
    return out
