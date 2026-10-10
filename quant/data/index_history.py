"""QQQ's daily 1x return history spliced onto the Nasdaq-100 price index before QQQ lists, with QLD and the
risk-free rate, as the QQQ timing studies use it.

Extracted unchanged from scripts/research_qqq_timing.py (``DevData``, ``load_dev_data``); the raw folder is an
argument (default research_cache/qqq_timing/raw). Every vendor frame is truncated at ``end`` right after parsing
and asserted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.rates import fill_rf, load_dtb3, load_kf_rf
from quant.data.sources.yahoo import parse_chart
from quant.paths import CACHE_ROOT

QQQ_RAW = CACHE_ROOT / "qqq_timing/raw"
QQQ_FIRST_CLOSE = "1999-03-10"          # QQQ's first close; strategies and the benchmark enter at this close
NDX_LAST_BEFORE_QQQ = "1999-03-09"


@dataclass
class DevData:
    sessions: pd.DatetimeIndex          # NDX sessions before 1999-03-10, QQQ sessions from then on
    r1: pd.Series                       # 1x daily return (NDX price before 1999-03-11, QQQ total return after)
    price: pd.Series                    # QQQ split-adjusted close for share counts (NaN before 1999-03-10)
    rf: pd.Series                       # Ken French daily RF on the sessions
    qld: pd.Series                      # real QLD daily total return (2006-06-22 ..)
    qqq_raw: pd.DataFrame               # QQQ close / adjclose / dividend rows (for the data checks)
    dtb3: pd.Series
    guard: dict = field(default_factory=dict)


def load_dev_data(end: str, raw: Path = QQQ_RAW) -> DevData:
    qqq = parse_chart(raw / "chart_QQQ.json", end)
    qld = parse_chart(raw / "chart_QLD.json", end)
    ndx = parse_chart(raw / "chart_%5ENDX.json", end)
    rf = load_kf_rf(end)
    dtb3 = load_dtb3(end, raw / "DTB3.csv")

    ndx = ndx[ndx["date"] < qqq["date"].iloc[0]]
    idx_ndx = pd.DatetimeIndex(ndx["date"])
    idx_qqq = pd.DatetimeIndex(qqq["date"])
    sessions = idx_ndx.append(idx_qqq)
    level_ndx = pd.Series(ndx["close"].values, index=idx_ndx)
    level_qqq = pd.Series(qqq["adjclose"].values, index=idx_qqq)
    r_ndx = level_ndx.pct_change()
    r_qqq = level_qqq.pct_change()
    # 1999-03-10 (QQQ's first day): NDX return from 1999-03-09 to 1999-03-10 is the best available
    first = idx_qqq[0]
    ndx_all = parse_chart(raw / "chart_%5ENDX.json", end).set_index("date")["close"]
    r_qqq.iloc[0] = ndx_all.loc[first.strftime("%Y-%m-%d")] / ndx_all.loc[idx_ndx[-1].strftime("%Y-%m-%d")] - 1
    r1 = pd.concat([r_ndx, r_qqq]).fillna(0.0)
    price = pd.Series(np.nan, index=sessions)
    price.loc[idx_qqq] = qqq["close"].values
    rf_s = fill_rf(rf, dtb3, sessions)
    qld_level = pd.Series(qld["adjclose"].values, index=pd.DatetimeIndex(qld["date"]))
    qld_r = qld_level.pct_change().dropna()

    guard = {"dev_end": end}
    for name, ix in [("sessions", sessions), ("qqq", idx_qqq), ("qld", qld_r.index), ("ndx", idx_ndx),
                     ("kf_rf", rf.index), ("dtb3", dtb3.index)]:
        assert_dev_dates(ix, end)
        guard[name] = {"first": str(ix.min().date()), "last": str(ix.max().date()), "rows": int(len(ix))}
    return DevData(sessions=sessions, r1=r1, price=price, rf=rf_s, qld=qld_r, qqq_raw=qqq, dtb3=dtb3, guard=guard)
