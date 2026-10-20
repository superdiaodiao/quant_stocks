"""Inverse Nasdaq-100 ETFs (ProShares PSQ, -1x daily; SQQQ, -3x daily) from Yahoo's v8 daily chart, and the
synthetic daily-reset model they are checked against (docs/research_ledger_sma200_short.md, section 0).

Requests go through ``quant.data.sources.http.cached_get``: the body is cached before it is parsed and every
request is logged to the caller's ``Ledger`` (the study's ``research_cache/sma200_short``). No key is involved (the
owner accepted Yahoo's terms). Vendor price levels stay in the cache; callers only write returns and statistics.

The synthetic model is the -k analogue of ``quant.data.synthetic.synthetic_2x`` (a fund of leverage L holds its
capital in T-bills and a swap on L x capital, so it earns rf on the capital and pays the financing on the swap):
``r_L = L * r_1x - (L - 1) * rf - (er + spread) / 252``; for L = -1 that is ``-r + 2 rf - (er + spread) / 252``.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.sources import yahoo
from quant.data.sources.http import Ledger, SlidingWindowLimiter, cached_get

TRADING_DAYS = 252
# ticker -> (daily leverage, issuer, inception, expense ratio per year). Expense ratios are the issuer's stated net
# ratios (proshares.com fund pages); they only set the starting point of the calibrated spread.
FUNDS = {
    "PSQ": (-1.0, "ProShares", "2006-06-19", 0.0095),
    "SQQQ": (-3.0, "ProShares", "2010-02-09", 0.0095),
}
FIRST = "2006-01-01"


def chart_path(raw: Path, sym: str) -> Path:
    return Path(raw) / f"chart_{sym}.json"


def ledger(cache: Path) -> Ledger:
    return Ledger(Path(cache) / "raw_index.csv.gz", Path(cache) / "quota_ledger.csv")


def fetch(raw: Path, cache: Path, period2: str, symbols=tuple(FUNDS), log=print) -> dict:
    """One cached Yahoo v8 daily chart per symbol for [FIRST, period2), one request per 2 seconds."""
    lim = SlidingWindowLimiter({2: 1})
    led = ledger(cache)
    out = {}
    for s in symbols:
        p = chart_path(raw, s)
        was = p.exists()
        cached_get(yahoo.chart_url(s, FIRST, period2), p, source="yahoo", ledger=led, headers=yahoo.HEADERS,
                   limiter=lim, symbol=s)
        out[s] = "cached" if was else "fetched"
        log(f"yahoo {s}: {out[s]}")
    return out


def load(raw: Path, sym: str, end: str) -> pd.DataFrame:
    """date-indexed close (split-adjusted), adjclose (total return), dividend; truncated at ``end``."""
    df = yahoo.parse_chart(chart_path(raw, sym), end)
    assert_dev_dates(df["date"], end)
    out = df.set_index(pd.DatetimeIndex(df["date"]))[["close", "adjclose", "dividend"]].astype(float)
    if (out[["close", "adjclose"]] <= 0).any().any():
        raise ValueError(f"{sym}: non-positive price")
    return out


def splits(raw: Path, sym: str, end: str) -> dict:
    return yahoo.split_events(chart_path(raw, sym), end)


def coverage(df: pd.DataFrame) -> dict:
    """First / last date, rows, sessions per full year, longest calendar gap, the largest absolute daily total
    returns (a reverse split mis-adjusted by the vendor would show up here)."""
    r = df["adjclose"].pct_change().dropna()
    per_year = df.groupby(df.index.year).size()
    full = per_year.iloc[1:-1] if len(per_year) > 2 else per_year
    gaps = pd.Series(df.index[1:] - df.index[:-1]).dt.days
    top = r.abs().sort_values(ascending=False).head(5)
    return {"first": str(df.index[0].date()), "last": str(df.index[-1].date()), "rows": int(len(df)),
            "sessions_per_full_year_min": int(full.min()), "sessions_per_full_year_max": int(full.max()),
            "longest_gap_calendar_days": int(gaps.max()), "dividends": int((df["dividend"] > 0).sum()),
            "largest_abs_daily_moves": [[str(d.date()), float(r.loc[d])] for d in top.index]}


def synthetic_inverse(r1: pd.Series, rf: pd.Series, leverage: float, er: float, spread: float = 0.0) -> pd.Series:
    """Daily-reset fund of ``leverage`` (< 0 for an inverse fund) on the 1x daily return ``r1``:
    ``L * r1 - (L - 1) * rf - (er + spread) / 252``. ``rf`` is the daily T-bill return on the same index."""
    L = float(leverage)
    return L * r1 - (L - 1.0) * rf - (er + spread) / TRADING_DAYS


def cagr(r: pd.Series) -> float:
    r = r.dropna()
    return float(np.prod(1 + r.values) ** (TRADING_DAYS / len(r)) - 1)


def calibrate_spread(real: pd.Series, r1: pd.Series, rf: pd.Series, leverage: float, er: float) -> float:
    """Spread (per year, bisection) that makes the synthetic fund's CAGR equal the real fund's over ``real``'s
    dates (``r1`` / ``rf`` on the same dates)."""
    target = cagr(real)
    lo, hi = -0.10, 0.10
    for _ in range(80):
        mid = (lo + hi) / 2
        if cagr(synthetic_inverse(r1, rf, leverage, er, mid)) > target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def tracking(real: pd.Series, syn: pd.Series, rf: pd.Series) -> dict:
    """Synthetic vs real daily returns on the same dates: CAGR gap, daily / monthly tracking error, correlation,
    largest daily gap, gap by calendar year, and the OLS slope of the daily gap on the daily T-bill return (0 if the
    model's interest term is right; the model has 2 rf for a -1x fund)."""
    d = syn - real
    m = lambda x: (1 + x).groupby(x.index.to_period("M")).prod() - 1
    X = np.column_stack([np.ones(len(d)), rf.reindex(d.index).values])
    coef, *_ = np.linalg.lstsq(X, d.values, rcond=None)
    by = {int(y): float(np.prod(1 + syn[syn.index.year == y]) - np.prod(1 + real[real.index.year == y]))
          for y in sorted(set(real.index.year))}
    return {"window": [str(real.index[0].date()), str(real.index[-1].date())], "sessions": int(len(real)),
            "cagr_real": cagr(real), "cagr_synthetic": cagr(syn), "cagr_gap_syn_minus_real": cagr(syn) - cagr(real),
            "tracking_error_daily_ann": float(d.std() * np.sqrt(TRADING_DAYS)),
            "tracking_error_monthly_ann": float((m(syn) - m(real)).std() * np.sqrt(12)),
            "corr": float(np.corrcoef(syn.values, real.values)[0, 1]),
            "max_abs_daily_gap": float(d.abs().max()), "max_abs_daily_gap_date": str(d.abs().idxmax().date()),
            "gap_slope_on_daily_rf": float(coef[1]), "gap_by_year": by}
