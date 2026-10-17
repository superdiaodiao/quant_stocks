"""International study data: long daily price-index histories, the investable USD ETFs and the FX rates that tie
the two together (docs/research_ledger_international.md, section 0.2).

All from Yahoo's v8 chart API (the owner accepted Yahoo's terms), no key involved. Raw bodies are cached under
``research_cache/international/raw`` before parsing and every request is logged in the study's own ledger
(``research_cache/international/{raw_index.csv.gz,quota_ledger.csv}``); nothing here is ever committed and no
frozen cache is touched. Requests are spaced 2 seconds apart.

- Indices are **price** indices (no dividends) in **local currency**; their dates are the exchange's own calendar
  dates (``parse_chart_exchange``).
- ETFs are US-listed, in USD; ``adjclose`` is the total-return (dividend-adjusted) close; New York dates
  (``parse_chart``).
- FX: Yahoo ``XXX=X`` daily rates, London dates; only used for the report-only currency decomposition.

Every frame is truncated at ``end`` right after parsing (the parsers) and asserted again here.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.sources.http import Ledger, SlidingWindowLimiter, cached_get
from quant.data.sources.yahoo import HEADERS, chart_url, parse_chart, parse_chart_exchange
from quant.paths import CACHE_ROOT

CACHE = CACHE_ROOT / "international"
RAW = CACHE / "raw"
FIRST = "1900-01-01"

# index symbol -> (market label, currency, FX symbol, FX quoted as local per USD?)
INDICES = {
    "^N225": ("Japan", "JPY", "JPY=X", True),
    "^GDAXI": ("Germany", "EUR", "EURUSD=X", False),
    "^FTSE": ("UK", "GBP", "GBPUSD=X", False),
    "^STOXX50E": ("Eurozone", "EUR", "EURUSD=X", False),
    "^HSI": ("Hong Kong", "HKD", "HKD=X", True),
    "^GSPTSE": ("Canada", "CAD", "CAD=X", True),
    "^AXJO": ("Australia", "AUD", "AUDUSD=X", False),
    "^KS11": ("Korea", "KRW", "KRW=X", True),
    "^GSPC": ("US S&P 500", "USD", None, False),
    "^IXIC": ("US Nasdaq Composite", "USD", None, False),
    "^NDX": ("US Nasdaq-100", "USD", None, False),
}
US_INDICES = ("^GSPC", "^IXIC", "^NDX")
# the country ETF that tracks each index (report-only validation of the index data and the currency effect)
INDEX_ETF = {"^N225": "EWJ", "^GDAXI": "EWG", "^FTSE": "EWU", "^STOXX50E": "FEZ", "^HSI": "EWH", "^GSPTSE": "EWC",
             "^AXJO": "EWA", "^KS11": "EWY", "^GSPC": "SPY", "^IXIC": "ONEQ", "^NDX": "QQQ"}
ETFS = ("EWJ", "EWG", "EWU", "FEZ", "EWH", "EWC", "EWA", "EWY", "EFA", "EEM", "VEU", "QQQ", "SPY", "ONEQ")
FX = tuple(sorted({v[2] for v in INDICES.values() if v[2]}))


def raw_path(symbol: str, raw: Path = RAW) -> Path:
    return raw / f"chart_{quote(symbol, safe='')}.json"


def ledger(cache: Path = CACHE) -> Ledger:
    return Ledger(cache / "raw_index.csv.gz", cache / "quota_ledger.csv")


def fetch(period2: str, raw: Path = RAW, symbols=None) -> list[str]:
    """Download every missing chart (whole history up to ``period2``, exclusive), 1 request per 2 seconds."""
    lim = SlidingWindowLimiter({2: 1})
    led = ledger(raw.parent)
    done = []
    for s in symbols or (tuple(INDICES) + ETFS + FX):
        cached_get(chart_url(s, FIRST, period2), raw_path(s, raw), source="yahoo", ledger=led, headers=HEADERS,
                   limiter=lim, symbol=s)
        done.append(s)
    return done


@dataclass
class Data:
    index: dict          # symbol -> pd.Series of local price-index closes on the exchange's own dates
    etf_adj: dict        # symbol -> total-return closes (USD, New York dates)
    etf_close: dict      # symbol -> split-adjusted closes (for share counts)
    fx: dict             # symbol -> Yahoo FX close (London dates)
    guard: dict


def _series(df: pd.DataFrame, col: str) -> pd.Series:
    return pd.Series(df[col].astype(float).values, index=pd.DatetimeIndex(df["date"]))


def load(end: str, raw: Path = RAW) -> Data:
    index, adj, close, fx, guard = {}, {}, {}, {}, {"end": end}
    for s in INDICES:
        df = parse_chart_exchange(raw_path(s, raw), end)
        assert_dev_dates(df["date"], end)
        index[s] = _series(df, "close")
    for s in ETFS:
        df = parse_chart(raw_path(s, raw), end)
        assert_dev_dates(df["date"], end)
        adj[s], close[s] = _series(df, "adjclose"), _series(df, "close")
    for s in FX:
        p = raw_path(s, raw)
        if p.exists():
            df = parse_chart_exchange(p, end)
            assert_dev_dates(df["date"], end)
            fx[s] = _series(df, "close")
    for k, v in [*index.items(), *adj.items(), *fx.items()]:
        guard[k] = {"first": str(v.index[0].date()), "last": str(v.index[-1].date()), "rows": int(len(v))}
    return Data(index=index, etf_adj=adj, etf_close=close, fx=fx, guard=guard)


# ======================================================================== validation (no rule results)

def series_checks(p: pd.Series, big: float = 0.15, stale_run: int = 5) -> dict:
    """Coverage and sanity of one daily close series: duplicate / non-positive values, sessions per full year,
    longest calendar gap, flat stretches (``stale_run`` or more identical closes in a row), the share of zero
    returns, days beyond +-``big`` and the five largest absolute daily moves."""
    r = p.pct_change().dropna()
    per_year = p.groupby(p.index.year).size()
    full = per_year.iloc[1:-1] if len(per_year) > 2 else per_year
    gaps = pd.Series(p.index[1:] - p.index[:-1], index=p.index[1:]).dt.days
    same = (p.diff() == 0).astype(int).values
    run = best = 0
    runs = 0
    for x in same:
        run = run + 1 if x else 0
        if run == stale_run - 1:
            runs += 1
        best = max(best, run)
    top = r.abs().sort_values(ascending=False).head(5)
    return {"first": str(p.index[0].date()), "last": str(p.index[-1].date()), "rows": int(len(p)),
            "duplicates": int(p.index.duplicated().sum()), "non_positive": int((p <= 0).sum()),
            "sessions_per_full_year_min": int(full.min()), "sessions_per_full_year_max": int(full.max()),
            "year_min_sessions": int(full.idxmin()),
            "longest_gap_calendar_days": int(gaps.max()), "longest_gap_ends": str(gaps.idxmax().date()),
            "flat_runs_ge_%d" % stale_run: int(runs), "longest_flat_run": int(best + 1 if best else 0),
            "share_zero_returns": float((r == 0).mean()), "days_abs_move_gt_%dpct" % round(big * 100): int((r.abs() > big).sum()),
            "largest_moves": [[str(d.date()), float(r.loc[d])] for d in top.index]}


def zero_return_share_by_decade(p: pd.Series) -> dict:
    r = p.pct_change().dropna()
    return {f"{d}s": float((x == 0).mean()) for d, x in r.groupby((r.index.year // 10) * 10)}


def month_end(p: pd.Series) -> pd.Series:
    return p.groupby(p.index.to_period("M")).last()


def usd_per_local(fx: pd.Series, inverse: bool) -> pd.Series:
    return 1.0 / fx if inverse else fx


def etf_vs_index(index: pd.Series, etf_adj: pd.Series, fx: pd.Series | None, inverse: bool) -> dict:
    """Monthly comparison (month-end closes, so Asian vs New York timing washes out) of the ETF's USD total
    return with the index's local price return and with the index's USD price return (index x FX). The gap
    ETF - index(USD) is roughly the dividend yield minus the fund's costs and tracking error."""
    mi, me = month_end(index), month_end(etf_adj)
    out = {}
    both = pd.concat([mi.pct_change(), me.pct_change()], axis=1, keys=["idx", "etf"]).dropna()
    if fx is not None:
        mf = month_end(usd_per_local(fx, inverse))
        both = pd.concat([both, mf.pct_change().rename("fx")], axis=1).dropna()
        both["idx_usd"] = (1 + both["idx"]) * (1 + both["fx"]) - 1
    else:
        both["fx"] = 0.0
        both["idx_usd"] = both["idx"]
    n = len(both)
    if n < 24:
        return {"months": n}
    ann = lambda x: float((1 + x).prod() ** (12 / len(x)) - 1)
    out.update({"from": str(both.index[0]), "to": str(both.index[-1]), "months": n,
                "corr_etf_vs_index_local": float(both["etf"].corr(both["idx"])),
                "corr_etf_vs_index_usd": float(both["etf"].corr(both["idx_usd"])),
                "cagr_index_local_price": ann(both["idx"]), "cagr_fx_usd_per_local": ann(both["fx"]),
                "cagr_index_usd_price": ann(both["idx_usd"]), "cagr_etf_usd_total": ann(both["etf"]),
                "gap_etf_minus_index_usd": ann(both["etf"]) - ann(both["idx_usd"]),
                "tracking_error_ann": float((both["etf"] - both["idx_usd"]).std() * np.sqrt(12)),
                "fx_share_of_usd_variance": float(both["fx"].var() / both["idx_usd"].var())})
    return out
