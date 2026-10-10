"""Calendar and intraday anomalies on QQQ: the QQQ open / close data with the overnight / intraday split, the
holding calendars C1-C8, the two-auction simulator and the per-period evaluation of studies/calendar.py.

Extracted unchanged from studies/calendar.py in the slimming pass (``Data``, ``split_legs``, ``load_data``, the
``hold_c*`` calendars, ``segments``, ``switch_cost``, ``simulate``, ``buy_hold``, ``effect_size``,
``period_bounds``, ``evaluate_period``, ``data_checks``). The study's frozen values are arguments: the half-spreads
(``Spreads``), the periods, the date-guard end and the raw folder. ``criteria`` is the A / B test of
``quant.evaluation.criteria.ab_criteria`` on one period with the excess CAGR as the return key.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import CASH_ETF_FEE, NOMINAL_PRICE, START_EQUITY, etf_order_cost as order_cost
from quant.data.event_calendar import FOMC_DATES, UNSCHEDULED_CLOSURES, parse_fed_pages, scheduled_holidays
from quant.data.guards import assert_dev_dates
from quant.data.rates import daily_rf
from quant.data.sources import yahoo
from quant.evaluation.criteria import ab_criteria
from quant.evaluation.metrics import (
    TRADING_DAYS, cagr_of, core_metrics, max_drawdown, monthly, relative_metrics, t_and_ir,
)


@dataclass(frozen=True)
class Spreads:
    """Half-spreads of the ETF order-cost model: QQQ, the T-bill ETF and ONEQ."""
    qqq: float
    bill: float
    oneq: float


# ======================================================================== data

@dataclass
class Data:
    sessions: pd.DatetimeIndex
    r: pd.Series            # QQQ daily total return (close to close, dividends included)
    o: pd.Series            # overnight leg: close(t-1) -> open(t), dividend included
    i: pd.Series            # intraday leg: open(t) -> close(t)
    open: pd.Series         # split-adjusted open (share counts at the opening auction)
    close: pd.Series        # split-adjusted close (share counts at the closing auction)
    oneq_r: pd.Series       # ONEQ daily total return (NaN before its first return)
    oneq_close: pd.Series
    rf: pd.Series           # daily T-bill return
    raw: dict
    guard: dict


def split_legs(adj: pd.Series, open_: pd.Series, close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    r = adj.pct_change()
    i = close / open_ - 1
    o = (1 + r) / (1 + i) - 1
    return r, o, i


def load_data(end: str, raw_dir: Path) -> Data:
    """QQQ (open / close / adjusted close) and ONEQ from the Yahoo charts in ``raw_dir`` and the daily T-bill rate,
    every frame truncated at ``end`` and asserted."""
    qqq = yahoo.parse_ohlc(raw_dir / "chart_QQQ.json", end)
    oneq = yahoo.parse_chart(raw_dir / "chart_ONEQ.json", end)
    sessions = pd.DatetimeIndex(qqq["date"])
    extra = pd.DatetimeIndex(oneq["date"]).difference(sessions)
    if len(extra):
        raise ValueError(f"ONEQ has sessions not in the QQQ calendar: {list(extra[:5])}")
    adj = pd.Series(qqq["adjclose"].values, index=sessions)
    op = pd.Series(qqq["open"].values, index=sessions)
    cl = pd.Series(qqq["close"].values, index=sessions)
    bad = op.isna() | (op <= 0)
    if bad.any():
        raise ValueError(f"QQQ open missing / non-positive on {list(sessions[bad][:5].date)}")
    r, o, i = split_legs(adj, op, cl)
    oadj = pd.Series(oneq["adjclose"].values, index=pd.DatetimeIndex(oneq["date"])).reindex(sessions)
    ocl = pd.Series(oneq["close"].values, index=pd.DatetimeIndex(oneq["date"])).reindex(sessions)
    rf = daily_rf(sessions, end)
    for df in (qqq, oneq):
        assert_dev_dates(df["date"], end)
    assert_dev_dates(sessions, end)
    guard = {"end": end, "QQQ": {"first": qqq["date"].iloc[0], "last": qqq["date"].iloc[-1], "rows": int(len(qqq))},
             "ONEQ": {"first": oneq["date"].iloc[0], "last": oneq["date"].iloc[-1], "rows": int(len(oneq))},
             "rf_last": str(rf.index[-1].date())}
    return Data(sessions=sessions, r=r, o=o, i=i, open=op, close=cl, oneq_r=oadj.pct_change(fill_method=None),
                oneq_close=ocl, rf=rf, raw={"QQQ": qqq, "ONEQ": oneq}, guard=guard)


# ======================================================================== calendars (known in advance)

def month_position(sessions: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray]:
    """(k from the start: 1, 2, ...; k from the end: -1 = last session, -2, ...) within each calendar month."""
    per = sessions.to_period("M")
    s = pd.Series(np.arange(len(sessions)), index=sessions)
    g = s.groupby(per)
    first = g.cumcount().values + 1
    last = -(g.cumcount(ascending=False).values + 1)
    return first, last


def hold_c1(sessions: pd.DatetimeIndex) -> np.ndarray:
    first, last = month_position(sessions)
    return (last == -1) | (first <= 3)


def hold_c2(sessions: pd.DatetimeIndex) -> np.ndarray:
    return np.isin(sessions.month, (11, 12, 1, 2, 3, 4))


def hold_c3(sessions: pd.DatetimeIndex, holidays: pd.DatetimeIndex) -> np.ndarray:
    """Session t is pre-holiday when a scheduled holiday lies strictly between t and the next session."""
    h = np.zeros(len(sessions), dtype=bool)
    hol = holidays.values
    nxt = np.append(sessions.values[1:], np.datetime64("NaT", "ns"))
    for k in range(len(sessions) - 1):
        lo, hi = sessions.values[k], nxt[k]
        j = np.searchsorted(hol, lo, side="right")
        h[k] = j < len(hol) and hol[j] < hi
    # last session: holiday on the next weekday(s) (none in our data window; kept for completeness)
    last = sessions[-1]
    nx = last + pd.offsets.BDay(1)
    h[-1] = nx in holidays
    return h


def hold_c6(sessions: pd.DatetimeIndex) -> np.ndarray:
    """False on the first session of each calendar (ISO) week, True otherwise."""
    iso = sessions.isocalendar()
    key = (iso["year"].values.astype(int) * 100 + iso["week"].values.astype(int))
    first = np.ones(len(sessions), dtype=bool)
    first[1:] = key[1:] != key[:-1]
    first[0] = False          # previous session unknown; never used (simulations start later)
    return ~first


def hold_c7(sessions: pd.DatetimeIndex, fomc=FOMC_DATES) -> np.ndarray:
    days = pd.DatetimeIndex(fomc)
    h = np.zeros(len(sessions), dtype=bool)
    pos = sessions.get_indexer(days[(days >= sessions[0]) & (days <= sessions[-1])])
    if (pos < 0).any():
        raise ValueError("an FOMC announcement day is not a trading session")
    h[pos] = True
    h[np.clip(pos - 1, 0, None)] = True
    return h


@dataclass(frozen=True)
class RuleSpec:
    name: str
    idle: str              # "B" = T-bill ETF, "C" = cash at 0%
    description: str


def segments(name: str, sessions: pd.DatetimeIndex, holidays: pd.DatetimeIndex | None = None) -> np.ndarray:
    """Bool array (n, 2): QQQ held during [overnight leg, intraday leg] of each session."""
    n = len(sessions)
    if name == "C4":
        return np.column_stack([np.ones(n, bool), np.zeros(n, bool)])
    if name == "C5":
        return np.column_stack([np.zeros(n, bool), np.ones(n, bool)])
    if name == "C1":
        h = hold_c1(sessions)
    elif name == "C2":
        h = hold_c2(sessions)
    elif name == "C3":
        h = hold_c3(sessions, scheduled_holidays(sessions) if holidays is None else holidays)
    elif name == "C6":
        h = hold_c6(sessions)
    elif name == "C7":
        h = hold_c7(sessions)
    elif name == "C8":
        h = hold_c1(sessions) | hold_c2(sessions)
    elif name == "QQQ":
        h = np.ones(n, bool)
    else:
        raise KeyError(name)
    return np.column_stack([h, h])


# ======================================================================== simulation

def switch_cost(v: float, frm: str, to: str, q_price: float, spreads: Spreads) -> tuple[float, int]:
    """Cost and order count of moving the whole account from state ``frm`` to ``to`` (Q / B / C)."""
    cost, orders = 0.0, 0
    for state, sell in ((frm, True), (to, False)):
        if state == "Q":
            cost += order_cost(v, q_price, sell, spreads.qqq)
            orders += 1
        elif state == "B":
            cost += order_cost(v, NOMINAL_PRICE, sell, spreads.bill)
            orders += 1
    return cost, orders


def simulate(seg: np.ndarray, d: Data, s: int, e: int, idle: str, costs: bool = True, *, spreads: Spreads) -> dict:
    """$10,000 cash at the close of session s-1, traded there into the state for session s's overnight leg.

    Each session t = s..e: overnight leg (QQQ earns o_t, the T-bill ETF earns the whole day's bill return), open
    auction (switch if the intraday state differs), intraday leg (QQQ earns i_t), close auction (switch into the
    state for t+1's overnight leg; no trade at the close of e). Values are recorded at each close."""
    o, i, op, cl = d.o.values, d.i.values, d.open.values, d.close.values
    bill = d.rf.values - CASH_ETF_FEE / TRADING_DAYS
    st = lambda held: "Q" if held else idle            # noqa: E731
    v = START_EQUITY
    pos = "C"
    target = st(seg[s, 0])
    c, n_ord = switch_cost(v, pos, target, cl[s - 1], spreads)
    v -= c if costs else 0.0
    cost_frac, orders, switches = (c / START_EQUITY if costs else 0.0), n_ord, 0
    pos = target
    values = [v]
    for t in range(s, e + 1):
        if pos == "Q":
            v *= 1 + o[t]
        elif pos == "B":
            v *= 1 + bill[t]
        tgt = st(seg[t, 1])
        if tgt != pos:
            if "B" in (pos, tgt):
                raise ValueError("T-bill ETF switches happen at closes only")
            c, n_ord = switch_cost(v, pos, tgt, op[t], spreads)
            if costs:
                cost_frac += c / v
                v -= c
            orders += n_ord
            switches += 1
            pos = tgt
        if pos == "Q":
            v *= 1 + i[t]
        if t < e:
            tgt = st(seg[t + 1, 0])
            if tgt != pos:
                c, n_ord = switch_cost(v, pos, tgt, cl[t], spreads)
                if costs:
                    cost_frac += c / v
                    v -= c
                orders += n_ord
                switches += 1
                pos = tgt
        values.append(v)
    idx = d.sessions[s - 1: e + 1]
    val = pd.Series(values, index=idx)
    return {"value": val, "ret": val.pct_change().iloc[1:], "switches": switches, "orders": orders,
            "cost_frac": cost_frac, "time_in_qqq": float(seg[s: e + 1].mean())}


def buy_hold(r: pd.Series, price: pd.Series, half_spread: float, s: int, e: int) -> pd.Series:
    """Daily returns of buy-and-hold bought at the close of s-1 (one buy order), s..e."""
    c = order_cost(START_EQUITY, float(price.iloc[s - 1]), False, half_spread)
    rr = r.iloc[s: e + 1].copy()
    rr.iloc[0] = (1 + rr.iloc[0]) * (START_EQUITY - c) / START_EQUITY - 1
    return rr


# ======================================================================== metrics

def criteria(m: dict) -> dict:
    """A (excess CAGR vs ONEQ > 0 and monthly-excess t >= 2) or B (drawdown >= 10 points shallower and excess CAGR
    >= -3 points) on one period: ``ab_criteria([m], m, cagr="excess_vs_oneq", bench=None)``."""
    a, b = ab_criteria([m], m, cagr="excess_vs_oneq", bench=None)
    return {"A": a, "B": b, "pass": bool(a or b)}


def effect_size(name: str, seg: np.ndarray, d: Data, s: int, e: int) -> dict:
    """Gross QQQ mean return per held leg vs the rest (annualised x252), Welch t."""
    if name in ("C4", "C5"):
        a, b = d.o.iloc[s: e + 1], d.i.iloc[s: e + 1]
        diff = a - b
        return {"overnight_mean_ann": float(a.mean() * TRADING_DAYS), "intraday_mean_ann": float(b.mean() * TRADING_DAYS),
                "overnight_cum_ann": cagr_of(a), "intraday_cum_ann": cagr_of(b),
                "paired_t_overnight_minus_intraday": float(diff.mean() / diff.std() * math.sqrt(len(diff)))}
    r = d.r.iloc[s: e + 1]
    h = seg[s: e + 1, 0]
    x, y = r[h], r[~h]
    se = math.sqrt(x.var() / len(x) + y.var() / len(y))
    return {"held_days": int(len(x)), "other_days": int(len(y)), "held_mean_ann": float(x.mean() * TRADING_DAYS),
            "other_mean_ann": float(y.mean() * TRADING_DAYS), "welch_t": float((x.mean() - y.mean()) / se)}


def period_bounds(d: Data, periods: dict) -> dict:
    """Session positions (s, e) of each named period, plus "half1" / "half2" = "full" split by session count."""
    out = {}
    for k, (a, b) in periods.items():
        s = int(d.sessions.searchsorted(pd.Timestamp(a)))
        e = int(d.sessions.searchsorted(pd.Timestamp(b), side="right")) - 1
        out[k] = (s, e)
    s, e = out["full"]
    mid = s + (e - s + 1) // 2
    out["half1"] = (s, mid - 1)
    out["half2"] = (mid, e)
    return out


def evaluate_period(name: str, seg: np.ndarray, d: Data, s: int, e: int, idle: str, costs: bool, *,
                    spreads: Spreads, end: str) -> dict:
    sim = simulate(seg, d, s, e, idle, costs, spreads=spreads)
    r = sim["ret"]
    q = buy_hold(d.r, d.close, spreads.qqq, s, e) if costs else d.r.iloc[s: e + 1]
    first_oneq = int(np.argmax(d.oneq_r.notna().values))
    so = max(s, first_oneq)
    b = buy_hold(d.oneq_r, d.oneq_close, spreads.oneq, so, e) if costs else d.oneq_r.iloc[so: e + 1]
    assert_dev_dates(r.index, end)
    years = len(r) / TRADING_DAYS
    m = {"start": str(r.index[0].date()), "end": str(r.index[-1].date()), "sessions": int(len(r)),
         **core_metrics(r, d.rf), "qqq_cagr": cagr_of(q), "excess_vs_qqq": cagr_of(r) - cagr_of(q),
         "qqq_max_dd": max_drawdown(pd.concat([pd.Series([1.0]), (1 + q).cumprod()])),
         **relative_metrics(r, b, d.rf, "oneq")}
    tq, irq = t_and_ir(monthly(r) - monthly(q))
    m.update({"t_monthly_excess_vs_qqq": tq, "ir_vs_qqq": irq, "switches_per_year": sim["switches"] / years,
              "orders_per_year": sim["orders"] / years, "time_in_qqq": sim["time_in_qqq"],
              "cost_drag_per_year": sim["cost_frac"] / years})
    return {"metrics": m, "ret": r, "q": q, "b": b}


# ======================================================================== data checks

def data_checks(d: Data, raw_dir: Path) -> dict:
    q = d.raw["QQQ"]
    op, hi, lo = q["open"], q["high"], q["low"]
    prev = q["close"].shift(1)
    a = q["adjclose"].pct_change()
    c = (q["close"] + q["dividend"]) / prev - 1
    hol = scheduled_holidays(d.sessions)
    per_year = pd.Series(1, index=hol).groupby(hol.year).sum()
    out = {
        "qqq_open_missing_or_nonpositive": int((op.isna() | (op <= 0)).sum()),
        "qqq_open_outside_high_low": int(((op > hi * (1 + 1e-6)) | (op < lo * (1 - 1e-6))).sum()),
        "qqq_open_equals_prev_close_share": float((op.iloc[1:] == prev.iloc[1:]).mean()),
        "qqq_open_equals_close_share": float((op == q["close"]).mean()),
        "qqq_max_abs_overnight": float(d.o.abs().max()), "qqq_max_abs_overnight_date": str(d.o.abs().idxmax().date()),
        "qqq_overnight_abs_gt_10pct_days": [str(x.date()) for x in d.o.index[d.o.abs() > 0.10]],
        "qqq_max_abs_intraday": float(d.i.abs().max()), "qqq_max_abs_intraday_date": str(d.i.abs().idxmax().date()),
        "qqq_legs_product_max_err": float(((1 + d.o) * (1 + d.i) - 1 - d.r).abs().max()),
        "qqq_max_abs_adj_vs_close_div": float((a - c).abs().max()), "qqq_dividends": int((q["dividend"] > 0).sum()),
        "oneq_first": d.guard["ONEQ"]["first"],
        "holidays_total": int(len(hol)), "holidays_per_year_min_max": [int(per_year.min()), int(per_year.max())],
        "holidays_per_year": {int(k): int(v) for k, v in per_year.items()},
        "unscheduled_closures_found": [x for x in UNSCHEDULED_CLOSURES if pd.Timestamp(x) not in d.sessions],
        "fomc_dates_in_window": int(((pd.DatetimeIndex(FOMC_DATES) >= d.sessions[0])
                                     & (pd.DatetimeIndex(FOMC_DATES) <= d.sessions[-1])).sum()),
    }
    try:
        parsed = parse_fed_pages(raw_dir)
        out["fomc_matches_fed_pages"] = parsed == list(FOMC_DATES)
        out["fomc_fed_pages_count"] = len(parsed)
    except FileNotFoundError as exc:
        out["fomc_matches_fed_pages"] = f"pages missing: {exc}"
    return out
