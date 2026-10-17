"""Alpaca market data (SIP daily bars and corporate actions): the API address and the body parsers (moved unchanged
from scripts/reversal_data_v2_1_alpaca.py, phase 3); SIP minute bars, their cached month-by-month fetch and the
regular-hours filter (intraday_minute study, at the end).

The key pair is read from ``.env.alpaca`` inside Python and sent in headers, never printed. Paging, the request
budget (``PER_MINUTE``), the cache layout and the entity rules A1-A7 stay with the caller
(``pipelines/reversal_data/v2_1_alpaca.py``).
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd

BASE = "https://data.alpaca.markets"


ADJUSTMENTS = ("raw", "split", "all")


CA_TYPES = "forward_split,reverse_split,unit_split,stock_dividend,cash_dividend,spin_off,name_change"


def bars_frame(body: bytes | None, symbol: str) -> pd.DataFrame:
    """date, close, volume from one /v2/stocks/bars body."""
    if not body:
        return pd.DataFrame(columns=["date", "close", "volume"])
    data = json.loads(body)
    rows = (data.get("bars") or {}).get(symbol) or []
    if not rows:
        return pd.DataFrame(columns=["date", "close", "volume"])
    f = pd.DataFrame({"date": pd.to_datetime([r["t"][:10] for r in rows]),
                      "close": [float(r["c"]) for r in rows], "volume": [float(r.get("v") or 0) for r in rows]})
    return f.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def corporate_actions(body: bytes | None) -> dict[str, list[dict]]:
    if not body:
        return {}
    return (json.loads(body).get("corporate_actions") or {})


# ======================================================================== minute bars (intraday_minute study)
# GET /v2/stocks/bars?symbols=..&timeframe=1Min&feed=sip&adjustment=all&start&end&limit=10000&page_token. One cached
# body per (symbol, calendar month, page); a stopped fetch resumes from the cache. The free plan allows about 200
# requests a minute; the caller's limiter keeps under it. Timestamps are bar starts in UTC; a bar stamped 09:30 ET
# covers [09:30, 09:31). Regular trading hours (RTH) = bars stamped 09:30 .. 15:59 ET (12:59 on early-close days).

MINUTE_PARAMS = {"timeframe": "1Min", "feed": "sip", "adjustment": "all", "limit": "10000"}
MINUTE_PER_MINUTE = 150
NY = "America/New_York"


def env_headers(env_path) -> dict:
    """Alpaca auth headers from a KEY=VALUE file (values never printed)."""
    from quant.data.sources.http import read_env_key
    return {"APCA-API-KEY-ID": read_env_key(env_path, "ALPACA_API_KEY_ID"),
            "APCA-API-SECRET-KEY": read_env_key(env_path, "ALPACA_API_SECRET_KEY"), "Accept": "application/json"}


def month_windows(first_month: str, last_month: str) -> list[tuple[str, str, str]]:
    """(label YYYY-MM, start, end) RFC-3339 UTC windows for every calendar month in [first_month, last_month]."""
    months = pd.period_range(first_month, last_month, freq="M")
    return [(str(m), f"{m.start_time:%Y-%m-%d}T00:00:00Z", f"{(m + 1).start_time:%Y-%m-%d}T00:00:00Z") for m in months]


def minute_url(symbol: str, start: str, end: str, page_token: str | None = None) -> str:
    from urllib.parse import urlencode
    q = {"symbols": symbol, **MINUTE_PARAMS, "start": start, "end": end}
    if page_token:
        q["page_token"] = page_token
    return f"{BASE}/v2/stocks/bars?{urlencode(q)}"


def minute_page_path(cache_dir, symbol: str, month: str, page: int):
    from pathlib import Path
    return Path(cache_dir) / "raw" / symbol / f"{month}__p{page}.json.gz"


def fetch_minute_month(cache_dir, symbol: str, month: str, start: str, end: str, *, headers: dict, ledger,
                       limiter) -> int:
    """Fetch (or read from cache) every page of one symbol-month; returns the number of pages."""
    from quant.data.sources.http import cached_get
    page, token = 0, None
    while True:
        body = cached_get(minute_url(symbol, start, end, token), minute_page_path(cache_dir, symbol, month, page),
                          source="alpaca_minute", ledger=ledger, headers=headers, limiter=limiter, symbol=symbol)
        token = json.loads(body).get("next_page_token")
        page += 1
        if not token:
            return page


def minute_frame(bodies, symbol: str) -> pd.DataFrame:
    """ts (UTC), open, high, low, close, volume, vwap, trades from a list of /v2/stocks/bars bodies."""
    rows = []
    for body in bodies:
        rows += (json.loads(body).get("bars") or {}).get(symbol) or []
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "vwap", "trades"])
    f = pd.DataFrame({"ts": pd.to_datetime([r["t"] for r in rows], utc=True),
                      "open": [float(r["o"]) for r in rows], "high": [float(r["h"]) for r in rows],
                      "low": [float(r["l"]) for r in rows], "close": [float(r["c"]) for r in rows],
                      "volume": [float(r.get("v") or 0) for r in rows],
                      "vwap": [float(r.get("vw") or r["c"]) for r in rows],
                      "trades": [int(r.get("n") or 0) for r in rows]})
    return f.drop_duplicates("ts", keep="last").sort_values("ts").reset_index(drop=True)


def read_minute_month(cache_dir, symbol: str, month: str) -> pd.DataFrame:
    """Every cached page of one symbol-month, parsed (empty if not fetched)."""
    import gzip
    bodies, page = [], 0
    while True:
        p = minute_page_path(cache_dir, symbol, month, page)
        if not p.exists():
            break
        bodies.append(gzip.decompress(p.read_bytes()))
        if not json.loads(bodies[-1]).get("next_page_token"):
            break
        page += 1
    return minute_frame(bodies, symbol)


def rth_frame(f: pd.DataFrame, early_close: set) -> pd.DataFrame:
    """Regular-hours bars with ET ``date`` and ``minute`` (minutes since 09:30): 0..389, or 0..209 on the dates in
    ``early_close`` (ISO strings, 13:00 ET close)."""
    et = f["ts"].dt.tz_convert(NY)
    minute = (et.dt.hour * 60 + et.dt.minute) - (9 * 60 + 30)
    date = et.dt.strftime("%Y-%m-%d")
    last = np.where(date.isin(early_close), 210, 390)
    keep = (minute >= 0) & (minute < last)
    out = f.loc[keep].copy()
    out.insert(0, "minute", minute[keep].astype(int).values)
    out.insert(0, "date", date[keep].values)
    return out.reset_index(drop=True)
