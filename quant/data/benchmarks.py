"""Benchmarks on a study's trading calendar.

ONEQ (Fidelity Nasdaq Composite ETF, total return) is the benchmark every pre-registered criterion is judged
against; QQQ is reported. ``oneq_on_sessions`` was extracted unchanged from scripts/research_indicators.py.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from quant.data.guards import assert_window
from quant.data.sources.yahoo import parse_chart
from quant.paths import CACHE_ROOT

BENCH_DIR = CACHE_ROOT / "benchmarks"
ONEQ_CHART = BENCH_DIR / "chart_ONEQ.json"
ONEQ_FIRST_RETURN = "2003-10-02"     # ONEQ's first daily total return


def oneq_on_sessions(sessions: pd.DatetimeIndex, start: str, end: str,
                     path: Path = ONEQ_CHART) -> tuple[pd.Series, pd.Series]:
    """(total-return level, split-adjusted close) of ONEQ on ``sessions``, forward-filled; rows in [start, end]."""
    oneq = parse_chart(path, end)
    oneq = oneq[oneq["date"] >= start]
    assert_window(oneq["date"], start, end, "ONEQ")
    lvl = pd.Series(oneq["adjclose"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    px = pd.Series(oneq["close"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    return lvl, px
