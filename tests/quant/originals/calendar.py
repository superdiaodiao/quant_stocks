"""Frozen reference copy from scripts/research_calendar.py at master d91c71a (before phase 2): the original
functions tests/quant/test_quant_core.py compares the quant core with. Do not edit."""
from __future__ import annotations
import json
import math
from pathlib import Path
import numpy as np
import pandas as pd
from tests.quant.originals.qqq_timing import (
    COMMISSION_MAX_FRAC, COMMISSION_MIN, COMMISSION_PER_SHARE, FEES_PER_SHARE, SELL_REG_FRAC, TRADING_DAYS,
    assert_dev_dates, cagr_of, max_drawdown, monthly, parse_chart, truncate_dev, yearly,
)


END = "2026-09-30"


def parse_ohlc(path: Path, end: str = END) -> pd.DataFrame:
    """parse_chart (date, close, adjclose, dividend; truncated at ``end``) plus open / high / low."""
    base = parse_chart(path, end)
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    extra = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "open": q["open"], "high": q["high"], "low": q["low"]})
    extra = truncate_dev(extra, "date", end).drop_duplicates("date")
    out = base.merge(extra, on="date", how="left")
    assert_dev_dates(out["date"], end)
    return out


def order_cost(value: float, price: float, sell: bool, half_spread: float) -> float:
    if value <= 0:
        return 0.0
    shares = value / price
    comm = min(max(COMMISSION_MIN, COMMISSION_PER_SHARE * shares), COMMISSION_MAX_FRAC * value)
    return comm + FEES_PER_SHARE * shares + half_spread * value + (SELL_REG_FRAC * value if sell else 0.0)


def longest_drawdown(value: pd.Series) -> int:
    """Longest stretch (sessions) from a peak until the value is back at that peak (or the end)."""
    peak = value.cummax().values
    under = value.values < peak - 1e-12
    best = run = 0
    for u in under:
        run = run + 1 if u else 0
        best = max(best, run)
    return int(best)


def t_and_ir(x: pd.Series) -> tuple[float, float]:
    sd = x.std()
    if not sd > 0:
        return 0.0, 0.0
    return float(x.mean() / sd * math.sqrt(len(x))), float(x.mean() / sd * math.sqrt(12))


def core_metrics(r: pd.Series, rf: pd.Series) -> dict:
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    ex = r - rf.reindex(r.index)
    down = np.sqrt(np.mean(np.minimum(ex.values, 0.0) ** 2))
    cagr, dd = cagr_of(r), max_drawdown(v0)
    y, m = yearly(r), monthly(r)
    return {"cagr": cagr, "vol": float(r.std() * math.sqrt(TRADING_DAYS)), "max_dd": dd,
            "longest_dd_sessions": longest_drawdown(v0), "sharpe": float(ex.mean() / ex.std() * math.sqrt(TRADING_DAYS)),
            "sortino": float(ex.mean() / down * math.sqrt(TRADING_DAYS)) if down > 0 else float("nan"),
            "calmar": cagr / abs(dd) if dd < 0 else float("nan"),
            "worst_year": float(y.min()), "worst_year_which": int(y.idxmin()),
            "worst_month": float(m.min()), "worst_month_which": str(m.idxmin())}


def relative_metrics(r: pd.Series, b: pd.Series, rf: pd.Series, tag: str) -> dict:
    """Strategy vs a buy-and-hold benchmark on the benchmark's index."""
    r = r.reindex(b.index)
    mr, mb = monthly(r), monthly(b)
    mx = mr - mb
    t, ir = t_and_ir(mx)
    rfm = monthly(rf.reindex(b.index))
    y_, x_ = (mr - rfm).values, (mb - rfm).values
    beta = float(np.cov(y_, x_, ddof=1)[0, 1] / np.var(x_, ddof=1))
    alpha = float((y_.mean() - beta * x_.mean()) * 12)
    v0 = pd.concat([pd.Series([1.0]), (1 + r).cumprod().reset_index(drop=True)])
    vb = pd.concat([pd.Series([1.0]), (1 + b).cumprod().reset_index(drop=True)])
    dd, bdd = max_drawdown(v0), max_drawdown(vb)
    yr, yb = yearly(r), yearly(b)
    return {f"{tag}_window_start": str(b.index[0].date()), f"cagr_in_{tag}_window": cagr_of(r),
            f"{tag}_cagr": cagr_of(b), f"excess_vs_{tag}": cagr_of(r) - cagr_of(b),
            f"max_dd_in_{tag}_window": dd, f"{tag}_max_dd": bdd, f"dd_shallower_than_{tag}_pp": (abs(bdd) - abs(dd)) * 100,
            f"t_monthly_excess_vs_{tag}": t, f"ir_vs_{tag}": ir, f"beta_vs_{tag}": beta, f"alpha_vs_{tag}": alpha,
            f"share_years_beating_{tag}": float((yr > yb).mean()), f"years_{tag}": int(len(yr)), f"months_{tag}": int(len(mx))}
