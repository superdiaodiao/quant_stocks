"""Real single-stock 2x daily-reset ETFs (Yahoo v8 daily charts) and their closing quoted spreads (Alpaca SIP quotes),
used to check the synthetic 2x leg of the dip_2x study (docs/research_ledger_dip_2x.md, section 0.2).

The funds, their underlyings, issuers, launch dates and expense ratios are declared in ``ETFS`` (expense ratios as
listed by stockanalysis.com on 2026-10-20). Requests go through ``quant.data.sources.http.cached_get`` (body cached
before it is parsed, every request logged to the caller's ``Ledger``); the cache folder and the ledger are the
caller's (the study's ``research_cache/dip_2x``). Keys are read from ``.env.alpaca`` inside Python, never printed.
Vendor price levels stay in the cache; callers only write returns, spreads and statistics.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.ohlc import OHLCSeries, build_real_ohlc
from quant.data.sources import alpaca, yahoo
from quant.data.sources.http import Ledger, SlidingWindowLimiter, cached_get

# ticker -> (underlying, issuer, first trading day, first day at 2x, expense ratio per year). The six Direxion funds of
# 2022-2023 launched as 1.5x funds and moved to 2x "on or around April 2, 2024" (Direxion press release of 2024-02-02);
# NVDL was 1.5x before 2024-01-22 (graniteshares.com/nvdl); METU launched at 2x.
ETFS = {
    "NVDL": ("NVDA", "GraniteShares", "2022-12-13", "2024-01-22", 0.0105),
    "NVDU": ("NVDA", "Direxion", "2023-09-13", "2024-04-02", 0.0092),
    "TSLL": ("TSLA", "Direxion", "2022-08-09", "2024-04-02", 0.0083),
    "AAPU": ("AAPL", "Direxion", "2022-08-09", "2024-04-02", 0.0096),
    "MSFU": ("MSFT", "Direxion", "2022-09-07", "2024-04-02", 0.0098),
    "AMZU": ("AMZN", "Direxion", "2022-09-07", "2024-04-02", 0.0099),
    "GGLL": ("GOOGL", "Direxion", "2022-09-07", "2024-04-02", 0.0096),
    "METU": ("META", "Direxion", "2024-06-05", "2024-06-05", 0.0102),
}
ER_SOURCE = "stockanalysis.com/etf/<ticker>/ (expense ratio and inception date), read 2026-10-20"
YAHOO_START = "2022-01-01"
YAHOO_PER_SECOND = 0.5           # one request every 2 s (as studies/selective_t)
ALPACA_PER_MINUTE = 150
QUOTE_WINDOW = ("15:55:00", "15:55:10")   # ET, a 10-second sample of quotes shortly before the close
QUOTE_LIMIT = 1000                          # quotes per request (the first 1,000 of the window)


def chart_path(raw_dir: Path, sym: str) -> Path:
    return Path(raw_dir) / "yahoo" / f"{sym}.json.gz"


def fetch_charts(symbols, raw_dir: Path, ledger: Ledger, period2: str, log=print) -> dict:
    """One cached Yahoo v8 daily chart per symbol for [YAHOO_START, period2)."""
    limiter = SlidingWindowLimiter({2: 1})
    out = {}
    for sym in symbols:
        p = chart_path(raw_dir, sym)
        cached = p.exists()
        body = cached_get(yahoo.chart_url(sym, YAHOO_START, period2), p, source="yahoo_chart", ledger=ledger,
                          headers=yahoo.HEADERS, limiter=limiter, symbol=sym)
        j = json.loads(body)["chart"]["result"][0]
        if (j.get("meta") or {}).get("dataGranularity") != "1d":
            raise ValueError(f"{sym}: not a daily chart")
        out[sym] = "cached" if cached else "fetched"
        log(f"yahoo {sym}: {out[sym]}")
    return out


def load_chart(path: Path, sym: str, end: str) -> OHLCSeries:
    """Real-price OHLC + adjclose total return of one cached chart (gzip JSON), truncated at ``end``."""
    payload = json.loads(gzip.decompress(Path(path).read_bytes()))
    df, splits = yahoo.parse_ohlc_payload(payload, end)
    assert_dev_dates(df["date"], end)
    bad = df[["open", "high", "low"]].isna().any(axis=1)
    for k in ("open", "high", "low"):
        df.loc[bad, k] = df.loc[bad, "close"]
    return build_real_ohlc(sym, df, splits)


# ======================================================================== Alpaca quotes near the close

def quotes_url(symbol: str, start: str, end: str) -> str:
    q = {"start": start, "end": end, "feed": "sip", "limit": str(QUOTE_LIMIT)}
    return f"{alpaca.BASE}/v2/stocks/{symbol}/quotes?{urlencode(q)}"


def quote_spreads(body: bytes) -> np.ndarray:
    """Relative quoted spreads (ask - bid) / mid of the quotes in one /v2/stocks/{symbol}/quotes body with a
    positive bid and ask >= bid."""
    rows = json.loads(body).get("quotes") or []
    bp = np.array([float(r.get("bp") or 0) for r in rows])
    ap = np.array([float(r.get("ap") or 0) for r in rows])
    ok = (bp > 0) & (ap >= bp)
    mid = (ap[ok] + bp[ok]) / 2
    return (ap[ok] - bp[ok]) / mid


def utc_window(day: str, a: str, b: str) -> tuple[str, str]:
    """RFC-3339 UTC bounds of the New York times ``a`` .. ``b`` on ``day``."""
    s = pd.Timestamp(f"{day} {a}", tz="America/New_York").tz_convert("UTC")
    e = pd.Timestamp(f"{day} {b}", tz="America/New_York").tz_convert("UTC")
    return s.strftime("%Y-%m-%dT%H:%M:%SZ"), e.strftime("%Y-%m-%dT%H:%M:%SZ")


def closing_spreads(symbols, days, raw_dir: Path, ledger: Ledger, env_path: Path, log=print) -> pd.DataFrame:
    """symbol, date, quotes, median / mean relative quoted spread in ``QUOTE_WINDOW`` of each day (cached)."""
    headers = alpaca.env_headers(env_path)
    limiter = SlidingWindowLimiter({60: ALPACA_PER_MINUTE})
    rows = []
    for sym in symbols:
        for d in days:
            start, end = utc_window(d, *QUOTE_WINDOW)
            p = Path(raw_dir) / "alpaca_quotes" / sym / f"{d}.json.gz"
            try:
                body = cached_get(quotes_url(sym, start, end), p, source="alpaca_quotes", ledger=ledger,
                                  headers=headers, limiter=limiter, symbol=sym)
            except FileNotFoundError:
                continue
            sp = quote_spreads(body)
            rows.append({"symbol": sym, "date": d, "quotes": int(len(sp)),
                         "median_spread_bps": float(np.median(sp) * 1e4) if len(sp) else np.nan,
                         "mean_spread_bps": float(np.mean(sp) * 1e4) if len(sp) else np.nan})
        log(f"alpaca quotes {sym}: {sum(1 for r in rows if r['symbol'] == sym)} days")
    return pd.DataFrame(rows)


# ======================================================================== Alpaca daily bars (second vendor)

def daily_bars_url(symbol: str, start: str, end: str) -> str:
    q = {"symbols": symbol, "timeframe": "1Day", "start": start, "end": end, "adjustment": "all", "feed": "sip",
         "limit": "10000"}
    return f"{alpaca.BASE}/v2/stocks/bars?{urlencode(q)}"


def alpaca_daily(symbols, start: str, end: str, raw_dir: Path, ledger: Ledger, env_path: Path) -> dict:
    """symbol -> date-indexed close adjusted for splits and dividends (Alpaca ``adjustment=all``), one cached page
    per symbol for [start, end] (fewer than 10,000 sessions)."""
    headers = alpaca.env_headers(env_path)
    limiter = SlidingWindowLimiter({60: ALPACA_PER_MINUTE})
    out = {}
    for sym in symbols:
        p = Path(raw_dir) / "alpaca_daily" / f"{sym}_{start}_{end}.json.gz"
        body = cached_get(daily_bars_url(sym, f"{start}T00:00:00Z", f"{end}T23:59:59Z"), p, source="alpaca_daily",
                          ledger=ledger, headers=headers, limiter=limiter, symbol=sym)
        f = alpaca.bars_frame(body, sym)
        assert_dev_dates(f["date"], end)
        out[sym] = pd.Series(f["close"].to_numpy(float), index=pd.DatetimeIndex(f["date"]))
    return out
