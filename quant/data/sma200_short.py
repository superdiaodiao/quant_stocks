"""Inputs of the 200-day-SMA short study (docs/research_ledger_sma200_short.md, section 0.2). Reads only; every frame
is truncated at ``end`` right after parsing and asserted.

- QQQ family: QQQ total return spliced on the Nasdaq-100 price index before 1999-03-10, QQQ's split-adjusted close
  and dividends, QLD, Ken French RF + DTB3 (``quant.data.index_history``, research_cache/qqq_timing/raw); the NY Fed
  EFFR (research_cache/leverage_methods/raw, ``quant.data.effr``); ONEQ (research_cache/benchmarks) with the Nasdaq
  Composite price index (research_cache/international/raw) before ONEQ lists; PSQ and SQQQ
  (research_cache/sma200_short/raw, ``quant.data.inverse_etfs``).
- Index level: ^NDX, ^IXIC, ^GSPC price indices (research_cache/international/raw, exchange dates) and FRED DTB3
  accrued per calendar day between sessions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data import index_history, inverse_etfs
from quant.data.benchmarks import ONEQ_CHART
from quant.data.effr import load_effr
from quant.data.guards import assert_dev_dates
from quant.data.sources.yahoo import parse_chart, parse_chart_exchange
from quant.paths import CACHE_ROOT

CACHE = CACHE_ROOT / "sma200_short"
RAW = CACHE / "raw"
INTL_RAW = CACHE_ROOT / "international/raw"
EFFR_DIR = CACHE_ROOT / "leverage_methods/raw"
IXIC_CHART = INTL_RAW / "chart_%5EIXIC.json"
# index -> first date used (rows before are dropped; ^GSPC from 1950 as the study's out-of-family check)
INDEX_FIRST = {"^NDX": "1985-10-01", "^IXIC": "1971-02-05", "^GSPC": "1950-01-03"}


@dataclass
class QQQData:
    sessions: pd.DatetimeIndex     # NDX sessions before 1999-03-10, QQQ sessions from then on
    r1: pd.Series                  # 1x daily total return (NDX price before 1999-03-11)
    level: pd.Series               # (1 + r1).cumprod(): the signal series P of the frozen T01 rule
    r_price: pd.Series             # QQQ daily price return (split-adjusted close; 0 up to 1999-03-10)
    div: pd.Series                 # QQQ dividend per share on the ex-date (split-adjusted)
    price: pd.Series               # QQQ split-adjusted close (NaN before 1999-03-10)
    rf: pd.Series                  # daily T-bill (Ken French RF, DTB3 / 252 after the last French row)
    effr: pd.Series                # EFFR (annual decimal), forward-filled on the sessions
    oneq_star: pd.Series           # ONEQ total return from 2003-10-02, Nasdaq Composite price return before
    oneq_price: pd.Series          # ONEQ close (NaN before it lists)
    qld: pd.Series
    funds: dict                    # "PSQ" / "SQQQ" -> DataFrame close / adjclose / dividend (own dates)
    guard: dict = field(default_factory=dict)


def load_qqq(end: str, raw_qqq: Path = index_history.QQQ_RAW, raw_funds: Path = RAW,
             effr_dir: Path = EFFR_DIR, oneq_chart: Path = ONEQ_CHART, ixic_chart: Path = IXIC_CHART) -> QQQData:
    d = index_history.load_dev_data(end, raw_qqq)
    s = d.sessions
    q = d.qqq_raw.set_index(pd.DatetimeIndex(d.qqq_raw["date"]))
    close = q["close"].astype(float)
    r_price = close.pct_change().reindex(s).fillna(0.0)
    div = q["dividend"].astype(float).reindex(s).fillna(0.0)
    effr = load_effr(end, effr_dir)
    effr_s = effr.reindex(effr.index.union(s)).ffill().reindex(s)
    oneq = parse_chart(oneq_chart, end)
    oneq_lvl = pd.Series(oneq["adjclose"].astype(float).values, index=pd.DatetimeIndex(oneq["date"]))
    oneq_px = pd.Series(oneq["close"].astype(float).values, index=pd.DatetimeIndex(oneq["date"]))
    ixic = parse_chart(ixic_chart, end)
    ixic_lvl = pd.Series(ixic["close"].astype(float).values, index=pd.DatetimeIndex(ixic["date"]))
    first_oneq = oneq_lvl.index[0]
    r_oneq = oneq_lvl.pct_change()
    r_ixic = ixic_lvl.pct_change()
    star = pd.Series(np.nan, index=s)
    pre = s[s <= first_oneq]
    post = s[s > first_oneq]
    star.loc[pre] = r_ixic.reindex(pre).values
    star.loc[post] = r_oneq.reindex(post).values
    funds = {k: inverse_etfs.load(raw_funds, k, end) for k in inverse_etfs.FUNDS}
    guard = dict(d.guard)
    for name, ix in [("oneq", oneq_lvl.index), ("ixic", ixic_lvl.index), ("effr", effr.index),
                     *[(k, v.index) for k, v in funds.items()]]:
        assert_dev_dates(ix, end)
        guard[name] = {"first": str(ix.min().date()), "last": str(ix.max().date()), "rows": int(len(ix))}
    return QQQData(sessions=s, r1=d.r1, level=(1 + d.r1).cumprod(), r_price=r_price, div=div,
                   price=d.price, rf=d.rf, effr=effr_s, oneq_star=star, oneq_price=oneq_px.reindex(s), qld=d.qld,
                   funds=funds, guard=guard)


def fund_returns(df: pd.DataFrame, sessions: pd.DatetimeIndex) -> pd.Series:
    """Daily total return of a fund (adjclose) on ``sessions`` (NaN before its first return); its dates must be
    sessions."""
    extra = df.index.difference(sessions)
    if len(extra):
        raise ValueError(f"fund dates outside the sessions: {list(extra[:5])}")
    return df["adjclose"].pct_change().reindex(sessions)


def load_indices(end: str, raw: Path = INTL_RAW, first: dict = INDEX_FIRST) -> dict:
    """{symbol: price-index closes from ``first[symbol]``} on the exchange's own dates."""
    out = {}
    for sym, f in first.items():
        df = parse_chart_exchange(raw / f"chart_{sym.replace('^', '%5E')}.json", end)
        assert_dev_dates(df["date"], end)
        p = pd.Series(df["close"].astype(float).values, index=pd.DatetimeIndex(df["date"]))
        out[sym] = p[p.index >= pd.Timestamp(f)]
    return out


def tbill_per_session(index: pd.DatetimeIndex, dtb3: pd.Series) -> pd.Series:
    """T-bill (DTB3, the previous session's yield) accrued per calendar day between consecutive sessions:
    (1 + y)^(days / 365) - 1; NaN where no yield exists yet (before 1954-01-04)."""
    y = dtb3.reindex(dtb3.index.union(index)).ffill().reindex(index)
    gap = pd.Series(index, index=index).diff().dt.days.fillna(1).values
    return pd.Series((1 + y.shift(1).values) ** (gap / 365.0) - 1, index=index)


def calendar_days(index: pd.DatetimeIndex) -> pd.Series:
    return pd.Series(pd.Series(index, index=index).diff().dt.days.fillna(1).values, index=index)


def daily_tbill_unquantised(sessions: pd.DatetimeIndex, dtb3: pd.Series) -> pd.Series:
    """DTB3 / 252 on ``sessions`` with the previous session's yield (0 before the first yield). A post-hoc check only:
    the study's registered T-bill is the Ken French daily RF, which the file gives in whole basis points per day
    (steps of about 2.5% a year)."""
    y = dtb3.reindex(dtb3.index.union(sessions)).ffill().reindex(sessions)
    return (y.shift(1) / 252.0).fillna(0.0)


def etf_dividend_yield(chart: Path, index: pd.Series, fee: float, end: str) -> tuple[float, str]:
    """An index's dividend yield per year measured on the dates it shares with an ETF that tracks it: the ETF's
    total-return CAGR (adjclose) minus the index's price CAGR, plus the ETF's expense ratio. Returns (yield, first
    common date). A post-hoc check only (the registered index level is on the price basis)."""
    e = parse_chart(chart, end)
    tr = pd.Series(e["adjclose"].astype(float).values, index=pd.DatetimeIndex(e["date"]))
    j = tr.index.intersection(index.index)
    yrs = (j[-1] - j[0]).days / 365.25
    g = lambda x: (x.loc[j[-1]] / x.loc[j[0]]) ** (1 / yrs) - 1
    return float(g(tr) - g(index) + fee), str(j[0].date())


def dividend_per_session(index: pd.DatetimeIndex, since: str, measured: float, before: float) -> pd.Series:
    """Dividend accrual per session of a constant yield (calendar days / 365): ``measured`` from ``since`` on,
    ``before`` earlier."""
    y = np.where(index >= pd.Timestamp(since), measured, before)
    return pd.Series(y * calendar_days(index).values / 365.0, index=index)


def data_checks(d: QQQData, idx: dict, start: str, end: str) -> dict:
    """Coverage only (no strategy results): the splice points, missing values on the judged window, fund coverage
    and the largest moves of each fund, the index series' sessions per year and gaps."""
    w = d.sessions[(d.sessions > pd.Timestamp(start)) & (d.sessions <= pd.Timestamp(end))]
    out = {"window_sessions": int(len(w)), "first_return": str(w[0].date()), "last_return": str(w[-1].date()),
           "oneq_star_missing_in_window": int(d.oneq_star.reindex(w).isna().sum()),
           "ixic_part_last_date": str(d.sessions[d.sessions <= d.oneq_price.first_valid_index()][-1].date()),
           "effr_missing_in_window": int(d.effr.reindex(w).isna().sum()),
           "qqq_dividends_in_window": int((d.div.reindex(w) > 0).sum()),
           "qqq_close_above_16_67_every_session": bool((d.price.reindex(w) > 16.67).all())}
    for k, df in d.funds.items():
        c = inverse_etfs.coverage(df)
        c["dates_outside_qqq_sessions"] = int(len(df.index.difference(d.sessions)))
        out[k] = c
    for sym, p in idx.items():
        per_year = p.groupby(p.index.year).size()
        full = per_year.iloc[1:-1]
        gaps = pd.Series(p.index[1:] - p.index[:-1]).dt.days
        out[sym] = {"first": str(p.index[0].date()), "last": str(p.index[-1].date()), "rows": int(len(p)),
                    "sessions_per_full_year_min": int(full.min()), "sessions_per_full_year_max": int(full.max()),
                    "longest_gap_calendar_days": int(gaps.max()), "duplicates": int(p.index.duplicated().sum()),
                    "non_positive": int((p <= 0).sum())}
    return out
