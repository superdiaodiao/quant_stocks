"""The long-only ETF panel of the regime-switching study (QQQ calendar from 1999-03-10), also read by
research_mean_reversion.

Extracted unchanged from studies/regime.py (``Data``, ``load_data``, the tickers, the ETF half-spreads and the
benchmark name). Every vendor frame is truncated at ``end`` right after parsing and asserted.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.rates import daily_rf
from quant.data.sources.yahoo import parse_chart
from quant.paths import CACHE_ROOT

RAW = CACHE_ROOT / "regime/raw"
TICKERS = ("QQQ", "ONEQ", "SPY", "IEF", "TLT", "SHY", "BIL", "GLD", "MTUM", "USMV", "QUAL", "VLUE")
HALF_SPREAD = {"QQQ": 1e-4, "SPY": 1e-4, "IEF": 1e-4, "GLD": 1e-4, "ONEQ": 2e-4, "MTUM": 2e-4, "USMV": 2e-4}
BENCH = "ONEQ"


@dataclass
class Data:
    sessions: pd.DatetimeIndex
    adj: pd.DataFrame        # total-return (adjusted) closes, NaN before listing
    close: pd.DataFrame      # split-adjusted closes, for share counts in the cost model
    rets: pd.DataFrame       # daily total returns, NaN on/before the first close
    rf: pd.Series            # daily T-bill return on the sessions
    raw: dict
    guard: dict


def load_data(end: str, raw_dir: Path = RAW) -> Data:
    raw = {t: parse_chart(raw_dir / f"chart_{t}.json", end) for t in TICKERS}
    sessions = pd.DatetimeIndex(raw["QQQ"]["date"])       # QQQ (1999-03-10 ..) defines the trading calendar
    adj, close = {}, {}
    for t, df in raw.items():
        df = df[pd.to_datetime(df["date"]) >= sessions[0]]      # SPY's 1993-1999 rows are not needed
        ix = pd.DatetimeIndex(df["date"])
        extra = ix.difference(sessions)
        if len(extra):
            raise ValueError(f"{t} has sessions not in the QQQ calendar: {list(extra[:5])}")
        adj[t] = pd.Series(df["adjclose"].values, index=ix).reindex(sessions)
        close[t] = pd.Series(df["close"].values, index=ix).reindex(sessions)
    adj, close = pd.DataFrame(adj), pd.DataFrame(close)
    rets = adj.pct_change(fill_method=None)
    rf = daily_rf(sessions, end)
    guard = {"end": end}
    for t, df in raw.items():
        assert_dev_dates(df["date"], end)
        guard[t] = {"first": df["date"].iloc[0], "last": df["date"].iloc[-1], "rows": int(len(df))}
    assert_dev_dates(sessions, end)
    return Data(sessions=sessions, adj=adj, close=close, rets=rets, rf=rf, raw=raw, guard=guard)
