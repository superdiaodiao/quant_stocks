"""Frozen reference copy from scripts/research_indicators.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from originals import qqq_timing as qt  # noqa: E402  (Yahoo parser + date guard, RF, index simulator, metrics)
from originals import livermore as lv  # noqa: E402  (stock windows loader, universe, metrics)
from originals import canslim_dev as cs  # noqa: E402  (order cost, QQQ benchmark)
from originals import reversal_dev as rev  # noqa: E402  (half spread, deflated Sharpe)


BENCH_DIR = Path("/Users/bytedance/code/quant_stocks/research_cache/benchmarks")


def oneq_on_sessions(sessions: pd.DatetimeIndex, start: str, end: str) -> tuple[pd.Series, pd.Series]:
    oneq = qt.parse_chart(BENCH_DIR / "chart_ONEQ.json", end)
    oneq = oneq[oneq["date"] >= start]
    lv.cs.assert_window(oneq["date"], start, end, "ONEQ")
    lvl = pd.Series(oneq["adjclose"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    px = pd.Series(oneq["close"].to_numpy(float), index=pd.DatetimeIndex(oneq["date"])).reindex(sessions).ffill()
    return lvl, px
