"""Sector ETF momentum rotation and moderate QQQ leverage (pre-registered in docs/research_ledger_sector_lev.md, section 0).

Part A, sector rotation (decided at each month end, traded at the next session's close, equal weight):
- A1  top 3 by 12-1 month momentum (M_1 / M_12 - 1) among XLK XLF XLE XLV XLI XLY XLP XLU XLB (+ SMH, SOXX once listed)
- A2  top 1 by the same momentum
- A3  top 3 by 6-month momentum (M_0 / M_6 - 1)
- A4  A1 while SPY's month-end close > its 10-month SMA, else IEF (T-bill ETF before IEF lists on 2002-07-30)

Part B, moderate leverage (QQQ + a 2x leg: synthetic before 2006-06-22, real QLD after; cash = T-bill ETF):
- B1  constant 1.25x (75% QQQ + 25% 2x leg), rebalanced at each month end
- B2  constant 1.5x
- B3  QQQ SMA200 with a 2% band on -> 1.5x, off -> 1.0x (trade on state change and at each month end)
- B4  on -> 1.25x, off -> 0.5x (50% QQQ + 50% T-bill ETF)

Execution: the target decided at the close of t (data up to t) is traded at the close of t+1. Benchmark: ONEQ total
return, spliced with the Nasdaq Composite price index before ONEQ lists (2003-10-01); QQQ is reported.
Costs: IBKR Pro Tiered, $10,000 start (reusing scripts/research_qqq_timing.py and scripts/research_regime.py).

Every vendor frame is truncated at ``END`` immediately after parsing and asserted. Raw data local only:
research_cache/sector_lev/raw/. Outputs: output/research_only/sector_lev/ (returns and metrics only).
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.research_qqq_timing import (
    CASH_ETF_FEE, LEV_ER, NOMINAL_PRICE, START_EQUITY, TRADING_DAYS, assert_dev_dates, bonferroni_t, cagr_of,
    fill_rf, load_dtb3, load_kf_rf, max_drawdown, monthly, parse_chart, synthetic_2x, trend_state, yearly,
)
from scripts.research_regime import month_end_mask, order_cost

ROOT = Path(__file__).resolve().parents[1]
END = "2026-09-30"
CAL_START = "1998-01-02"          # SPY sessions from here define the calendar (NDX warm-up for the SMA200)
RAW = Path("/Users/bytedance/code/quant_stocks/research_cache/sector_lev/raw")
PRIOR_QQQ = Path("/Users/bytedance/code/quant_stocks/research_cache/regime/raw/chart_QQQ.json")
OUT = ROOT / "output/research_only/sector_lev"

SECTORS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "SMH", "SOXX")
ETFS = SECTORS + ("SPY", "QQQ", "ONEQ", "IEF", "QLD")
INDICES = {"IXIC": "%5EIXIC", "NDX": "%5ENDX"}
SPREAD_2X = 0.0070                # frozen: calibrated on real QLD 2006-06-22 .. 2014-12-31 (QQQ timing ledger)
HALF_SPREAD = {**{t: 1e-4 for t in SECTORS[:9]}, "SMH": 2e-4, "SOXX": 2e-4, "SPY": 1e-4, "QQQ": 1e-4,
               "BENCH": 2e-4, "QLDX": 2e-4, "CASH": 1e-4, "IEFX": 1e-4}
HALF_SPLIT = "2015-01-01"
ONEQ_START = "2003-10-01"
CRASH_WINDOWS = {"1999_2002": ("1999-01-01", "2002-12-31"), "2007_2009": ("2007-01-01", "2009-12-31")}
RISK_WINDOWS = {"2000_2002": ("2000-01-01", "2002-12-31"), "2008": ("2008-01-01", "2008-12-31")}
LOCAL_PEAK_FROM = {"2008": "2007-01-01"}      # report-only: the 2008 crash measured from its 2007 high
SMA_LEN, BAND = 200, 0.02


# ======================================================================== data

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


def load_data(end: str = END, raw_dir: Path = RAW) -> Data:
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


def data_checks(d: Data) -> dict:
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
        p = parse_chart(PRIOR_QQQ, END)
        pr = pd.Series(p["adjclose"].values, index=pd.DatetimeIndex(p["date"])).pct_change()
        j = pd.concat([pr, d.rets["QQQ"]], axis=1, join="inner").dropna()
        out["QQQ_vs_regime_cache"] = {"sessions": int(len(j)), "max_abs_daily_diff": float((j.iloc[:, 0] - j.iloc[:, 1]).abs().max())}
    return out


# ======================================================================== Part A signals (month-end data up to t)

def month_end_prices(adj: pd.DataFrame) -> pd.DataFrame:
    return adj[month_end_mask(adj.index)]


def momentum(me: pd.DataFrame, kind: str) -> pd.DataFrame:
    """kind '12-1': M_1 / M_12 - 1; kind '6': M_0 / M_6 - 1 (month-end rows; NaN when the window start is missing)."""
    if kind == "12-1":
        return me.shift(1) / me.shift(12) - 1
    if kind == "6":
        return me / me.shift(6) - 1
    raise KeyError(kind)


def top_n_weights(mom: pd.DataFrame, n: int, need: int = 9) -> pd.DataFrame:
    """Equal weight on the n highest scores (ties by ticker); NaN rows until ``need`` ETFs can be ranked."""
    w = pd.DataFrame(np.nan, index=mom.index, columns=mom.columns)
    for dt, row in mom.iterrows():
        row = row.dropna()
        if len(row) < need:
            continue
        order = sorted(row.index, key=lambda t: (-row[t], t))[:n]
        w.loc[dt] = 0.0
        w.loc[dt, order] = 1.0 / n
    return w


def faber_state(me_spy: pd.Series, months: int = 10) -> pd.Series:
    sma = me_spy.rolling(months, min_periods=months).mean()
    return (me_spy > sma).astype(float).where(sma.notna())


def to_daily(w_me: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.Series]:
    """Month-end targets carried forward daily, plus the rebalance flag (True on month ends with a target)."""
    w = w_me.reindex(sessions).ffill()
    flag = pd.Series(False, index=sessions)
    flag.loc[w_me.dropna(how="all").index] = True
    return w, flag


LEGS_A = list(SECTORS) + ["IEFX"]


def part_a_targets(name: str, adj: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    me = month_end_prices(adj[list(SECTORS)])
    kind, n = {"A1": ("12-1", 3), "A2": ("12-1", 1), "A3": ("6", 3), "A4": ("12-1", 3)}[name]
    w = top_n_weights(momentum(me, kind), n)
    w["IEFX"] = 0.0
    w.loc[w[list(SECTORS)].isna().all(axis=1), "IEFX"] = np.nan
    if name == "A4":
        st = faber_state(month_end_prices(adj[["SPY"]])["SPY"]).reindex(w.index)
        off = st == 0.0
        w.loc[off & w["IEFX"].notna(), :] = 0.0
        w.loc[off & w["IEFX"].notna(), "IEFX"] = 1.0
        w.loc[st.isna(), :] = np.nan
    return to_daily(w[LEGS_A], adj.index)


# ======================================================================== Part B signals

LEGS_B = ["QQQ", "QLDX", "CASH"]


def exposure_weights(e: float) -> dict:
    if e >= 1.0:
        return {"QQQ": 2.0 - e, "QLDX": e - 1.0, "CASH": 0.0}
    return {"QQQ": e, "QLDX": 0.0, "CASH": 1.0 - e}


B_EXPOSURE = {"B1": (1.25, 1.25), "B2": (1.5, 1.5), "B3": (1.5, 1.0), "B4": (1.25, 0.5)}


def part_b_targets(name: str, p_qqq: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    on, off = B_EXPOSURE[name]
    idx = p_qqq.index
    me = pd.Series(month_end_mask(idx), index=idx)
    if on == off:
        st = pd.Series(1.0, index=idx)
    else:
        st = trend_state(p_qqq, SMA_LEN, BAND)
    w = pd.DataFrame(np.nan, index=idx, columns=LEGS_B)
    for k in LEGS_B:
        w.loc[st == 1.0, k] = exposure_weights(on)[k]
        w.loc[st == 0.0, k] = exposure_weights(off)[k]
    change = st.ne(st.shift(1)) & st.shift(1).notna() & st.notna()
    return w, (me | change) & st.notna()


@dataclass(frozen=True)
class Spec:
    name: str
    part: str
    description: str


SPECS = (
    Spec("A1", "A", "top 3 of 9 SPDRs + SMH/SOXX by 12-1 month momentum, equal weight, monthly"),
    Spec("A2", "A", "top 1 by 12-1 month momentum, monthly"),
    Spec("A3", "A", "top 3 by 6-month momentum, equal weight, monthly"),
    Spec("A4", "A", "A1 while SPY > 10-month SMA, else IEF (T-bill ETF before 2002-07-30)"),
    Spec("B1", "B", "constant 1.25x QQQ (75% QQQ + 25% 2x leg), monthly rebalance"),
    Spec("B2", "B", "constant 1.5x QQQ (50% QQQ + 50% 2x leg), monthly rebalance"),
    Spec("B3", "B", "QQQ SMA200 band 2%: on 1.5x, off 1.0x"),
    Spec("B4", "B", "QQQ SMA200 band 2%: on 1.25x, off 0.5x (half T-bill ETF)"),
)
SPEC = {s.name: s for s in SPECS}


def targets(name: str, d: Data) -> tuple[pd.DataFrame, pd.Series]:
    return part_a_targets(name, d.adj) if SPEC[name].part == "A" else part_b_targets(name, d.p_qqq)


def entry_index(w: pd.DataFrame, flag: pd.Series, d: Data, monthly_rule: bool, not_before=None) -> int:
    """First session S (>= not_before) with a QQQ close and a benchmark return, a target decided at S-1 and,
    for monthly rules, S-1 a decision day."""
    have = (d.close["QQQ"].notna() & d.rets["BENCH"].notna()).values
    defined = w.notna().all(axis=1).values
    fl = flag.values
    lo = 1 if not_before is None else max(1, int(w.index.searchsorted(pd.Timestamp(not_before))))
    for i in range(lo, len(w)):
        if have[i] and defined[i - 1] and (fl[i - 1] or not monthly_rule):
            return i
    raise ValueError("no entry possible")


# ======================================================================== simulation

def simulate(w: pd.DataFrame, flag: pd.Series, rets: pd.DataFrame, close: pd.DataFrame, start_i: int,
             end_i: int | None = None, lag: int = 1, costs: bool = True, extra_drag: dict | None = None) -> dict:
    """$10,000 cash at the close of ``start_i`` traded to the target decided ``lag`` sessions earlier; afterwards
    trades at the close of i when the decision at i-lag is a rebalance day or its target differs from the last
    executed one. Holdings drift between trades. ``extra_drag``: {leg: annual drag} for sensitivities."""
    cols = list(w.columns)
    W, F = w.values, flag.reindex(w.index).fillna(False).values
    R = rets[cols].values.copy()
    for k, a in (extra_drag or {}).items():
        R[:, cols.index(k)] -= a / TRADING_DAYS
    P = close[cols].values
    end_i = len(w) - 1 if end_i is None else end_i
    h = np.zeros(len(cols))
    cash = START_EQUITY
    last = None
    values, held, trades, orders, cost_frac = [], [], 0, 0, 0.0
    for i in range(start_i, end_i + 1):
        if i > start_i:
            r = R[i]
            if np.any(np.isnan(r[h > 0])):
                raise ValueError(f"missing return for a held asset on {w.index[i].date()}")
            h = h * (1 + np.nan_to_num(r))
        total = h.sum() + cash
        tgt = W[i - lag]
        if np.any(np.isnan(tgt)):
            raise ValueError(f"no target on {w.index[i - lag].date()}")
        if last is None or F[i - lag] or not np.allclose(tgt, last, atol=1e-12, rtol=0):
            new = tgt * total
            delta = new - h
            c = 0.0
            traded = False
            for k in range(len(cols)):
                if abs(delta[k]) > 1.0:
                    if costs:
                        c += order_cost(abs(delta[k]), P[i, k], delta[k] < 0, HALF_SPREAD[cols[k]])
                    orders += 1
                    traded = True
            if traded or last is None:
                if last is not None:
                    trades += 1
                h = new * (1 - c / total)
                cash = 0.0
                cost_frac += c / total
            last = tgt.copy()
        values.append(h.sum() + cash)
        held.append(h / h.sum() if h.sum() > 0 else h)
    idx = w.index[start_i:end_i + 1]
    v = pd.Series(values, index=idx)
    hw = pd.DataFrame(held, index=idx, columns=cols).shift(1).iloc[1:]
    return {"value": v, "ret": v.pct_change().iloc[1:], "held": hw, "trades": trades, "orders": orders,
            "cost_frac": cost_frac}


def buy_hold(leg: str, d: Data, start_i: int) -> dict:
    w = pd.DataFrame({leg: 1.0}, index=d.sessions)
    return simulate(w, pd.Series(False, index=d.sessions), d.rets, d.close, start_i)


# ======================================================================== metrics

def equity0(r: pd.Series) -> pd.Series:
    """Equity curve starting at 1.0 on the entry close (index: entry date + return dates)."""
    v = (1 + r).cumprod()
    start = r.index[0] - pd.Timedelta(days=1)
    return pd.concat([pd.Series([1.0], index=[start]), v])


def longest_drawdown(v: pd.Series) -> int:
    """Longest stretch (sessions) from a peak until the curve regains it (or the end of the data)."""
    peak = v.cummax()
    under = (v < peak).values
    best = cur = 0
    for u in under:
        cur = cur + 1 if u else 0
        best = max(best, cur)
    return int(best)


def ulcer_index(v: pd.Series) -> float:
    dd = (v / v.cummax() - 1) * 100
    return float(np.sqrt((dd ** 2).mean()))


def full_years(r: pd.Series, min_sessions: int = 240) -> list:
    n = r.groupby(r.index.year).size()
    return [int(y) for y, k in n.items() if k >= min_sessions]


def period_metrics(r: pd.Series, b: pd.Series, q: pd.Series, rf: pd.Series) -> dict:
    v, vb, vq = equity0(r), equity0(b), equity0(q)
    rfr = rf.reindex(r.index)
    ex = r - rfr
    down = np.sqrt((np.minimum(ex, 0) ** 2).mean())
    cagr, bc, qc = cagr_of(r), cagr_of(b), cagr_of(q)
    dd, bdd, qdd = max_drawdown(v), max_drawdown(vb), max_drawdown(vq)
    mr, mb, mq, mrf = monthly(r), monthly(b), monthly(q), monthly(rfr)
    mx, mxq = mr - mb, mr - mq
    y, xb = (mr - mrf).values, (mb - mrf).values
    beta = float(np.cov(y, xb, ddof=1)[0, 1] / np.var(xb, ddof=1)) if len(y) > 2 else float("nan")
    alpha_m = float(y.mean() - beta * xb.mean())
    yr, yb = yearly(r), yearly(b)
    fy = full_years(r)
    yrs = yr.loc[fy] if fy else yr
    return {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
            "cagr": cagr, "oneq_cagr": bc, "qqq_cagr": qc, "cagr_minus_oneq": cagr - bc, "cagr_minus_qqq": cagr - qc,
            "vol": float(r.std() * math.sqrt(TRADING_DAYS)),
            "max_dd": dd, "oneq_max_dd": bdd, "qqq_max_dd": qdd,
            "dd_shallower_than_oneq_pp": (abs(bdd) - abs(dd)) * 100, "dd_shallower_than_qqq_pp": (abs(qdd) - abs(dd)) * 100,
            "longest_dd_sessions": longest_drawdown(v), "oneq_longest_dd_sessions": longest_drawdown(vb),
            "ulcer": ulcer_index(v), "oneq_ulcer": ulcer_index(vb),
            "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "oneq_sharpe": float((b - rfr).mean() / (b - rfr).std() * math.sqrt(TRADING_DAYS)),
            "sortino": float(ex.mean() / down * math.sqrt(TRADING_DAYS)) if down > 0 else float("nan"),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "oneq_calmar": bc / abs(bdd) if bdd < 0 else float("nan"),
            "months": int(len(mx)),
            "ir_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(12)),
            "t_monthly_excess_vs_oneq": float(mx.mean() / mx.std() * math.sqrt(len(mx))),
            "t_monthly_excess_vs_qqq": float(mxq.mean() / mxq.std() * math.sqrt(len(mxq))),
            "beta_vs_oneq": beta, "alpha_ann_vs_oneq": alpha_m * 12,
            "full_years": len(fy), "share_years_beating_oneq": float((yr.loc[fy] > yb.loc[fy]).mean()) if fy else float("nan"),
            "worst_year": int(yrs.idxmin()), "worst_year_ret": float(yrs.min()),
            "worst_month": str(mr.idxmin()), "worst_month_ret": float(mr.min())}


def evaluate(full: dict, h1: dict, h2: dict) -> dict:
    """Pre-registered criteria (section 0.6), applied to one benchmark definition."""
    parts = (full, h1, h2)
    a = all(p["cagr"] > p["oneq_cagr"] for p in parts) and full["t_monthly_excess_vs_oneq"] >= 2.0
    b = all(p["dd_shallower_than_oneq_pp"] >= 10.0 and p["cagr"] >= p["oneq_cagr"] - 0.03 for p in parts)
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


def split(r: pd.Series) -> tuple[pd.DatetimeIndex, pd.DatetimeIndex]:
    return r.index[r.index < HALF_SPLIT], r.index[r.index >= HALF_SPLIT]


def window_stats(r: pd.Series, start: str, end: str) -> dict | None:
    x = r.loc[start:end]
    if len(x) < 20:
        return None
    return {"start": str(x.index[0].date()), "end": str(x.index[-1].date()), "total": float(np.prod(1 + x) - 1),
            "max_dd": max_drawdown(equity0(x))}


def drawdown_episode(v: pd.Series, start: str, end: str, peak_from: str | None = None) -> dict | None:
    """Deepest drawdown of the full curve whose trough falls in [start, end]: peak, trough, recovery.
    ``peak_from``: only peaks on/after this date count (a crash measured from its own local high)."""
    if peak_from is not None:
        v = v.loc[peak_from:]
    dd = v / v.cummax() - 1
    win = dd.loc[start:end]
    if len(win) < 20:
        return None
    trough = win.idxmin()
    peak_val = v.loc[:trough].max()
    peak = v.loc[:trough].idxmax()
    after = v.loc[trough:]
    rec = after[after >= peak_val]
    rec_dt = rec.index[0] if len(rec) else None
    yrs = lambda a, b: (b - a).days / 365.25
    return {"peak": str(peak.date()), "trough": str(trough.date()), "depth": float(win.min()),
            "recovered": str(rec_dt.date()) if rec_dt is not None else "not recovered",
            "years_underwater_peak_to_recovery": yrs(peak, rec_dt) if rec_dt is not None else
            f">{yrs(peak, v.index[-1]):.1f}",
            "years_trough_to_recovery": yrs(trough, rec_dt) if rec_dt is not None else f">{yrs(trough, v.index[-1]):.1f}"}


def risk_report(r: pd.Series) -> dict:
    v = equity0(r)
    out = {}
    for tag, (s, e) in RISK_WINDOWS.items():
        w = window_stats(r, s, e)
        out[tag] = {"window_total": w["total"] if w else None, "episode": drawdown_episode(v, s, e)}
        if tag in LOCAL_PEAK_FROM:
            out[f"{tag}_from_local_peak"] = {"window_total": w["total"] if w else None,
                                             "episode": drawdown_episode(v, s, e, LOCAL_PEAK_FROM[tag])}
    return out


# ======================================================================== main

def run_spec(spec: Spec, d: Data, not_before=None) -> dict:
    w, flag = targets(spec.name, d)
    s_i = entry_index(w, flag, d, spec.part == "A", not_before)
    sim = simulate(w, flag, d.rets, d.close, s_i)
    b, q = buy_hold("BENCH", d, s_i)["ret"], buy_hold("QQQ", d, s_i)["ret"]
    r = sim["ret"]
    assert r.index.equals(b.index) and r.index.equals(q.index)
    assert_dev_dates(r.index, END)
    i1, i2 = split(r)
    full = period_metrics(r, b, q, d.rf)
    h1 = period_metrics(r.loc[i1], b.loc[i1], q.loc[i1], d.rf)
    h2 = period_metrics(r.loc[i2], b.loc[i2], q.loc[i2], d.rf)
    return {"w": w, "flag": flag, "s_i": s_i, "sim": sim, "r": r, "b": b, "q": q, "full": full, "half1": h1,
            "half2": h2, "criteria": evaluate(full, h1, h2)}


def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;",
          {k: v["last"] for k, v in d.guard.items() if isinstance(v, dict)})
    checks = data_checks(d)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(SPECS),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(SPECS)), "configs": {}}
    rows, by_year, mon, crash_rows, risk_rows = [], {}, {}, [], []
    refs_done, starts = set(), []
    for spec in SPECS:
        m = run_spec(spec, d)
        m2 = run_spec(spec, d, not_before=ONEQ_START)
        starts.append(m["s_i"])
        crit = {"spliced": m["criteria"], "oneq_only": m2["criteria"],
                "pass": bool(m["criteria"]["pass"] and m2["criteria"]["pass"])}
        sim, r = m["sim"], m["r"]
        years = len(r) / TRADING_DAYS
        info = {"description": spec.description, "entry_close": str(d.sessions[m["s_i"]].date()),
                "full": m["full"], "half1": m["half1"], "half2": m["half2"],
                "oneq_only": {"entry_close": str(d.sessions[m2["s_i"]].date()), "full": m2["full"],
                              "half1": m2["half1"], "half2": m2["half2"]},
                "criteria": crit, "rebalances": sim["trades"], "rebalances_per_year": sim["trades"] / years,
                "orders": sim["orders"], "cost_drag_per_year": sim["cost_frac"] / years,
                "avg_weight": {k: float(v) for k, v in sim["held"].mean().items() if v > 0.005},
                "crash_windows": {}, "risk": risk_report(r)}
        for tag, (s, e) in CRASH_WINDOWS.items():
            info["crash_windows"][tag] = {"strategy": window_stats(r, s, e), "oneq": window_stats(m["b"], s, e),
                                          "qqq": window_stats(m["q"], s, e)}
            st = info["crash_windows"][tag]
            if st["strategy"]:
                crash_rows.append({"config": spec.name, "window": tag, "start": st["strategy"]["start"],
                                   "end": st["strategy"]["end"], "total": st["strategy"]["total"],
                                   "max_dd": st["strategy"]["max_dd"], "oneq_total": st["oneq"]["total"],
                                   "oneq_max_dd": st["oneq"]["max_dd"], "qqq_total": st["qqq"]["total"],
                                   "qqq_max_dd": st["qqq"]["max_dd"]})
        for who, series in (("strategy", r), ("ONEQ_spliced", m["b"]), ("QQQ", m["q"])):
            if who != "strategy" and (who, m["s_i"]) in refs_done:
                continue
            refs_done.add((who, m["s_i"]))
            rep = info["risk"] if who == "strategy" else risk_report(series)
            for tag, x in rep.items():
                ep = x["episode"] or {}
                risk_rows.append({"config": spec.name if who == "strategy" else f"{who}_from_{d.sessions[m['s_i']].date()}",
                                  "window": tag, "window_total": x["window_total"], **ep})
        sens = {}
        w, flag, s_i = m["w"], m["flag"], m["s_i"]
        variants = [("same_close", dict(lag=0)), ("no_costs", dict(costs=False))]
        if spec.part == "B":
            variants.append(("spread_plus_1pct", dict(extra_drag={"QLDX": 0.01})))
        for tag, kw in variants:
            s2 = simulate(w, flag, d.rets, d.close, s_i, **kw)
            sens[tag] = {"cagr": cagr_of(s2["ret"]), "max_dd": max_drawdown(s2["value"]),
                         "cagr_minus_oneq": cagr_of(s2["ret"]) - m["full"]["oneq_cagr"]}
        info["sensitivities"] = sens
        res["configs"][spec.name] = info
        for tag, p in [("full", m["full"]), ("half1", m["half1"]), ("half2", m["half2"]),
                       ("oneq_full", m2["full"]), ("oneq_half1", m2["half1"]), ("oneq_half2", m2["half2"])]:
            rows.append({"config": spec.name, "period": tag, **p, "pass": crit["pass"] if tag == "full" else None})
        by_year[spec.name] = yearly(r)
        mon[spec.name] = monthly(r)
        f = m["full"]
        print(f"{spec.name} {f['start']}..{f['end']} CAGR {f['cagr']:+.2%} (ONEQ {f['oneq_cagr']:+.2%}, QQQ "
              f"{f['qqq_cagr']:+.2%}) MDD {f['max_dd']:.1%} (ONEQ {f['oneq_max_dd']:.1%}) t {f['t_monthly_excess_vs_oneq']:+.2f}"
              f" | H1 {m['half1']['cagr_minus_oneq']:+.2%} H2 {m['half2']['cagr_minus_oneq']:+.2%} pass={crit['pass']}")
    s0 = min(starts)
    for leg, tag in (("BENCH", "ONEQ_spliced_buyhold"), ("QQQ", "QQQ_buyhold")):
        rr = buy_hold(leg, d, s0)["ret"]
        by_year[tag] = yearly(rr)
        mon[tag] = monthly(rr)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    pd.DataFrame(crash_rows).to_csv(OUT / "crash_windows.csv", index=False, float_format="%.6f")
    pd.DataFrame(risk_rows).to_csv(OUT / "leverage_risk.csv", index=False, float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
