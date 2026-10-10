"""Calendar and intraday anomalies on QQQ (pre-registered in docs/research_ledger_calendar.md, section 0).

Eight textbook rules, no parameters:

- C1 turn of the month: hold QQQ on the last session of each month and the first three of the next (days -1..+3).
- C2 "sell in May" (Halloween): hold QQQ November .. April, T-bills May .. October.
- C3 pre-holiday: hold QQQ only on the session right before a scheduled NYSE holiday.
- C4 overnight only: buy at the close, sell at the next open (earns close(t-1) -> open(t), dividends included).
- C5 intraday only: buy at the open, sell at the close (earns open(t) -> close(t)).
- C6 weekend / Monday effect: skip the first session of every calendar week (sell at the week's last close,
  buy back at the next week's first close); hold QQQ otherwise.
- C7 FOMC: hold QQQ on scheduled FOMC announcement days and the session before (days -1, 0).
- C8 C1 or C2 (union).

The idle state is a T-bill ETF (RF - 0.10%/yr, two orders per switch) for C1, C2, C3, C7, C8 and uninvested cash at
0% (one order per switch) for C4, C5, C6. Trades happen in the closing auction (and the opening auction for C4/C5)
at that day's close / open. Costs: IBKR Pro Tiered per order (scripts/research_qqq_timing.py constants), half-spread
1 bp for QQQ and the T-bill ETF, 2 bp for ONEQ. Every period starts fresh from $10,000.

Daily QQQ return split: total r = adjclose_t / adjclose_{t-1} - 1; intraday i = close_t / open_t - 1;
overnight o = (1 + r) / (1 + i) - 1 (so the ex-dividend amount sits in the overnight leg).

Every vendor frame is truncated at ``END`` immediately after parsing and asserted. Raw data local only:
research_cache/calendar/raw/ (Yahoo chart JSON for QQQ / ONEQ, Federal Reserve FOMC calendar pages).
Outputs: output/research_only/calendar/ (returns and metrics only, no vendor price levels).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.backtest.costs import CASH_ETF_FEE, NOMINAL_PRICE, START_EQUITY, etf_order_cost as order_cost
from quant.data.guards import assert_dev_dates
from quant.data.rates import daily_rf
from quant.data.sources import yahoo
from quant.evaluation.criteria import bonferroni_t
from quant.evaluation.metrics import (  # noqa: F401  (cal.* names read by tests)
    TRADING_DAYS, cagr_of, core_metrics, longest_drawdown, max_drawdown, monthly, relative_metrics, t_and_ir, yearly,
)
from quant.paths import CACHE_ROOT, output_dir

END = "2026-09-30"
RAW = CACHE_ROOT / "calendar/raw"
OUT = output_dir("calendar")

HALF_SPREAD_QQQ = 1e-4
HALF_SPREAD_BILL = 1e-4
HALF_SPREAD_ONEQ = 2e-4

PERIODS = {
    "dev": ("1999-03-11", "2014-12-31"),
    "test": ("2015-01-02", "2026-09-30"),
    "full": ("1999-03-11", "2026-09-30"),
    # "half1" / "half2" are the full period split by session count (filled in at run time)
}
JUDGED = "test"

# Weekday NYSE closures that were not scheduled holidays (excluded from C3's holiday list).
UNSCHEDULED_CLOSURES = (
    "2001-09-11", "2001-09-12", "2001-09-13", "2001-09-14",   # September 11 attacks
    "2004-06-11",                                             # President Reagan national day of mourning
    "2007-01-02",                                             # President Ford national day of mourning
    "2012-10-29", "2012-10-30",                               # Hurricane Sandy
    "2018-12-05",                                             # President G.H.W. Bush national day of mourning
    "2025-01-09",                                             # President Carter national day of mourning
)

# Scheduled FOMC meeting announcement days (last day of the meeting). Source: Federal Reserve,
# https://www.federalreserve.gov/monetarypolicy/fomchistorical{YEAR}.htm (1999-2020) and
# https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm (2021-2026), downloaded 2026-10-05.
# Unscheduled meetings, conference calls, notation votes and the cancelled 2020-03-17/18 meeting are excluded.
FOMC_DATES = (
    "1999-02-03", "1999-03-30", "1999-05-18", "1999-06-30", "1999-08-24", "1999-10-05", "1999-11-16", "1999-12-21",
    "2000-02-02", "2000-03-21", "2000-05-16", "2000-06-28", "2000-08-22", "2000-10-03", "2000-11-15", "2000-12-19",
    "2001-01-31", "2001-03-20", "2001-05-15", "2001-06-27", "2001-08-21", "2001-10-02", "2001-11-06", "2001-12-11",
    "2002-01-30", "2002-03-19", "2002-05-07", "2002-06-26", "2002-08-13", "2002-09-24", "2002-11-06", "2002-12-10",
    "2003-01-29", "2003-03-18", "2003-05-06", "2003-06-25", "2003-08-12", "2003-09-16", "2003-10-28", "2003-12-09",
    "2004-01-28", "2004-03-16", "2004-05-04", "2004-06-30", "2004-08-10", "2004-09-21", "2004-11-10", "2004-12-14",
    "2005-02-02", "2005-03-22", "2005-05-03", "2005-06-30", "2005-08-09", "2005-09-20", "2005-11-01", "2005-12-13",
    "2006-01-31", "2006-03-28", "2006-05-10", "2006-06-29", "2006-08-08", "2006-09-20", "2006-10-25", "2006-12-12",
    "2007-01-31", "2007-03-21", "2007-05-09", "2007-06-28", "2007-08-07", "2007-09-18", "2007-10-31", "2007-12-11",
    "2008-01-30", "2008-03-18", "2008-04-30", "2008-06-25", "2008-08-05", "2008-09-16", "2008-10-29", "2008-12-16",
    "2009-01-28", "2009-03-18", "2009-04-29", "2009-06-24", "2009-08-12", "2009-09-23", "2009-11-04", "2009-12-16",
    "2010-01-27", "2010-03-16", "2010-04-28", "2010-06-23", "2010-08-10", "2010-09-21", "2010-11-03", "2010-12-14",
    "2011-01-26", "2011-03-15", "2011-04-27", "2011-06-22", "2011-08-09", "2011-09-21", "2011-11-02", "2011-12-13",
    "2012-01-25", "2012-03-13", "2012-04-25", "2012-06-20", "2012-08-01", "2012-09-13", "2012-10-24", "2012-12-12",
    "2013-01-30", "2013-03-20", "2013-05-01", "2013-06-19", "2013-07-31", "2013-09-18", "2013-10-30", "2013-12-18",
    "2014-01-29", "2014-03-19", "2014-04-30", "2014-06-18", "2014-07-30", "2014-09-17", "2014-10-29", "2014-12-17",
    "2015-01-28", "2015-03-18", "2015-04-29", "2015-06-17", "2015-07-29", "2015-09-17", "2015-10-28", "2015-12-16",
    "2016-01-27", "2016-03-16", "2016-04-27", "2016-06-15", "2016-07-27", "2016-09-21", "2016-11-02", "2016-12-14",
    "2017-02-01", "2017-03-15", "2017-05-03", "2017-06-14", "2017-07-26", "2017-09-20", "2017-11-01", "2017-12-13",
    "2018-01-31", "2018-03-21", "2018-05-02", "2018-06-13", "2018-08-01", "2018-09-26", "2018-11-08", "2018-12-19",
    "2019-01-30", "2019-03-20", "2019-05-01", "2019-06-19", "2019-07-31", "2019-09-18", "2019-10-30", "2019-12-11",
    "2020-01-29", "2020-04-29", "2020-06-10", "2020-07-29", "2020-09-16", "2020-11-05", "2020-12-16",
    "2021-01-27", "2021-03-17", "2021-04-28", "2021-06-16", "2021-07-28", "2021-09-22", "2021-11-03", "2021-12-15",
    "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
    "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
    "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
    "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10",
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
)

MONTHS = {m: i for i, m in enumerate(("January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"), 1)}
MON3 = {k[:3]: v for k, v in MONTHS.items()}


# ======================================================================== FOMC source parsing (data check / test)

def _meeting_last_day(year: int, label: str) -> dt.date:
    """'February 1-2' / 'March 30' / 'Apr/May 30-1' / 'July 31-August 1' / 'March 19-20*' -> last meeting day."""
    label = label.strip().rstrip("*").strip()
    m = re.match(r"([A-Za-z]+) (\d+)-([A-Za-z]+) (\d+)$", label)
    if m:
        return dt.date(year, MONTHS[m.group(3)], int(m.group(4)))
    mon, days = label.split(" ", 1)
    month = MON3[mon.split("/")[1][:3]] if "/" in mon else MONTHS[mon]
    return dt.date(year, month, int(days.strip().rstrip("*").split("-")[-1]))


def parse_fed_pages(raw_dir: Path = RAW, first: int = 1999, last: int = 2026) -> list[str]:
    """Re-derive the scheduled announcement days from the downloaded Federal Reserve pages."""
    out = []
    for y in range(first, min(last, 2020) + 1):
        t = (raw_dir / f"fomchistorical{y}.htm").read_text()
        heads = [re.sub(r"\s+", " ", html.unescape(re.sub("<[^>]+>", "", m.group(1)))).strip()
                 for m in re.finditer(r"<h5[^>]*>(.*?)</h5>", t, re.S)]
        days = []
        for h in heads:
            if not re.search(r"\bMeeting - ", h) or re.search(r"unscheduled|cancelled|notation", h):
                continue
            d = _meeting_last_day(y, h.split(" Meeting")[0])
            if days and (d - days[-1]).days == 1:      # 2003-09-15 / 09-16 listed separately: one meeting
                days[-1] = d
            else:
                days.append(d)
        out += days
    if last >= 2021:
        t = (raw_dir / "fomccalendars.htm").read_text()
        for m in re.finditer(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>', t):
            y = int(m.group(1))
            if not (max(first, 2021) <= y <= last):
                continue
            seg = t[m.end(): m.end() + 60000]
            nxt = seg.find("FOMC Meetings</a></h4>")
            seg = seg[: nxt if nxt > 0 else None]
            months = [x.strip() for x in re.findall(r"fomc-meeting__month[^>]*>\s*(?:<strong>)?([^<]+)", seg)]
            dates = [x.strip() for x in re.findall(r"fomc-meeting__date[^>]*>\s*([^<]+)", seg)]
            for mo, da in zip(months, dates):
                if re.search("unscheduled|notation|cancel", da):
                    continue
                out.append(_meeting_last_day(y, f"{mo} {da}"))
    return sorted(d.isoformat() for d in out)


# ======================================================================== data

def parse_ohlc(path: Path, end: str = END) -> pd.DataFrame:
    return yahoo.parse_ohlc(path, end)


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


def load_data(end: str = END, raw_dir: Path = RAW) -> Data:
    qqq = parse_ohlc(raw_dir / "chart_QQQ.json", end)
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

def scheduled_holidays(sessions: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Weekdays between the first and last session with no trading, minus unscheduled closures."""
    wd = pd.bdate_range(sessions[0], sessions[-1])
    missing = wd.difference(sessions)
    return missing.difference(pd.DatetimeIndex(UNSCHEDULED_CLOSURES))


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


RULES = (
    RuleSpec("C1", "B", "turn of the month: hold days -1..+3, T-bills otherwise"),
    RuleSpec("C2", "B", "sell in May: hold Nov-Apr, T-bills May-Oct"),
    RuleSpec("C3", "B", "pre-holiday: hold the session before scheduled NYSE holidays"),
    RuleSpec("C4", "C", "overnight only: buy at the close, sell at the next open"),
    RuleSpec("C5", "C", "intraday only: buy at the open, sell at the close"),
    RuleSpec("C6", "C", "weekend effect: skip the first session of each week"),
    RuleSpec("C7", "B", "FOMC: hold the announcement day and the session before"),
    RuleSpec("C8", "B", "C1 or C2"),
)
SPEC = {r.name: r for r in RULES}


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


def switch_cost(v: float, frm: str, to: str, q_price: float) -> tuple[float, int]:
    """Cost and order count of moving the whole account from state ``frm`` to ``to`` (Q / B / C)."""
    cost, orders = 0.0, 0
    for state, sell in ((frm, True), (to, False)):
        if state == "Q":
            cost += order_cost(v, q_price, sell, HALF_SPREAD_QQQ)
            orders += 1
        elif state == "B":
            cost += order_cost(v, NOMINAL_PRICE, sell, HALF_SPREAD_BILL)
            orders += 1
    return cost, orders


def simulate(seg: np.ndarray, d: Data, s: int, e: int, idle: str, costs: bool = True) -> dict:
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
    c, n_ord = switch_cost(v, pos, target, cl[s - 1])
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
            c, n_ord = switch_cost(v, pos, tgt, op[t])
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
                c, n_ord = switch_cost(v, pos, tgt, cl[t])
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
    a = m["excess_vs_oneq"] > 0 and m["t_monthly_excess_vs_oneq"] >= 2.0
    b = m["dd_shallower_than_oneq_pp"] >= 10.0 and m["excess_vs_oneq"] >= -0.03
    return {"A": bool(a), "B": bool(b), "pass": bool(a or b)}


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


def period_bounds(d: Data) -> dict:
    out = {}
    for k, (a, b) in PERIODS.items():
        s = int(d.sessions.searchsorted(pd.Timestamp(a)))
        e = int(d.sessions.searchsorted(pd.Timestamp(b), side="right")) - 1
        out[k] = (s, e)
    s, e = out["full"]
    mid = s + (e - s + 1) // 2
    out["half1"] = (s, mid - 1)
    out["half2"] = (mid, e)
    return out


def evaluate_period(name: str, seg: np.ndarray, d: Data, s: int, e: int, idle: str, costs: bool) -> dict:
    sim = simulate(seg, d, s, e, idle, costs)
    r = sim["ret"]
    q = buy_hold(d.r, d.close, HALF_SPREAD_QQQ, s, e) if costs else d.r.iloc[s: e + 1]
    first_oneq = int(np.argmax(d.oneq_r.notna().values))
    so = max(s, first_oneq)
    b = buy_hold(d.oneq_r, d.oneq_close, HALF_SPREAD_ONEQ, so, e) if costs else d.oneq_r.iloc[so: e + 1]
    assert_dev_dates(r.index, END)
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

def data_checks(d: Data) -> dict:
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
        parsed = parse_fed_pages(RAW)
        out["fomc_matches_fed_pages"] = parsed == list(FOMC_DATES)
        out["fomc_fed_pages_count"] = len(parsed)
    except FileNotFoundError as exc:
        out["fomc_matches_fed_pages"] = f"pages missing: {exc}"
    return out


# ======================================================================== main

def run(args=None) -> dict:
    d = load_data(END)
    print("DATE GUARD: every frame truncated at", END, "and asserted;", d.guard)
    checks = data_checks(d)
    print("DATA CHECKS:", {k: v for k, v in checks.items() if k != "holidays_per_year"})
    bounds = period_bounds(d)
    hol = scheduled_holidays(d.sessions)
    res = {"guard": d.guard, "data_checks": checks, "n_trials": len(RULES),
           "bonferroni_t_one_sided_5pct": bonferroni_t(len(RULES)), "judged_period": JUDGED,
           "periods": {k: [str(d.sessions[s].date()), str(d.sessions[e].date())] for k, (s, e) in bounds.items()},
           "rules": {}, "benchmarks": {}}
    rows, by_year, mon = [], {}, {}
    for spec in RULES:
        seg = segments(spec.name, d.sessions, hol)
        info = {"description": spec.description, "idle": spec.idle, "periods": {}}
        for pk, (s, e) in bounds.items():
            net = evaluate_period(spec.name, seg, d, s, e, spec.idle, True)
            gross = evaluate_period(spec.name, seg, d, s, e, spec.idle, False)
            cash0 = evaluate_period(spec.name, seg, d, s, e, "C", True)["metrics"]
            pr = {"net": net["metrics"], "gross": gross["metrics"], "criteria": criteria(net["metrics"]),
                  "gross_criteria": criteria(gross["metrics"]),
                  "sens_cash0": {k: cash0[k] for k in ("cagr", "max_dd", "excess_vs_oneq", "cost_drag_per_year")},
                  "effect": effect_size(spec.name, seg, d, s, e)}
            info["periods"][pk] = pr
            for tag in ("net", "gross"):
                rows.append({"rule": spec.name, "period": pk, "basis": tag, **pr[tag],
                             **{f"crit_{k}": v for k, v in pr["criteria" if tag == "net" else "gross_criteria"].items()}})
            if pk == "full":
                by_year[spec.name] = yearly(net["ret"])
                by_year[f"{spec.name}_gross"] = yearly(gross["ret"])
                mon[spec.name] = monthly(net["ret"])
        res["rules"][spec.name] = info
        t = info["periods"][JUDGED]
        print(f"{spec.name} test: CAGR {t['net']['cagr']:+.2%} (gross {t['gross']['cagr']:+.2%}; ONEQ "
              f"{t['net']['oneq_cagr']:+.2%}, QQQ {t['net']['qqq_cagr']:+.2%}) MDD {t['net']['max_dd']:.1%} "
              f"(ONEQ {t['net']['oneq_max_dd']:.1%}) t {t['net']['t_monthly_excess_vs_oneq']:+.2f} "
              f"cost {t['net']['cost_drag_per_year']:.2%}/yr pass={t['criteria']['pass']}")
    # benchmark rows
    for pk, (s, e) in bounds.items():
        q = buy_hold(d.r, d.close, HALF_SPREAD_QQQ, s, e)
        first_oneq = int(np.argmax(d.oneq_r.notna().values))
        so = max(s, first_oneq)
        b = buy_hold(d.oneq_r, d.oneq_close, HALF_SPREAD_ONEQ, so, e)
        res["benchmarks"][pk] = {"QQQ": core_metrics(q, d.rf), "ONEQ": {"start": str(b.index[0].date()),
                                                                      **core_metrics(b, d.rf)}}
        if pk == "full":
            by_year["QQQ_buyhold"] = yearly(q)
            by_year["ONEQ_buyhold"] = yearly(b)
            mon["QQQ_buyhold"] = monthly(q)
            mon["ONEQ_buyhold"] = monthly(b)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "results.json").write_text(json.dumps(res, indent=2, default=float))
    pd.DataFrame(rows).to_csv(OUT / "summary.csv", index=False, float_format="%.6f")
    pd.DataFrame(by_year).to_csv(OUT / "by_year.csv", float_format="%.6f")
    mk = pd.DataFrame(mon)
    mk.index = mk.index.astype(str)
    mk.to_csv(OUT / "monthly_returns.csv", float_format="%.6f")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    run(ap.parse_args(argv))


if __name__ == "__main__":
    main()
