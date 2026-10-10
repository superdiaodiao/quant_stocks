"""Yahoo v8 chart JSON parsers. Every frame is truncated at ``end`` as soon as it is parsed.

Extracted unchanged from scripts/research_qqq_timing.py (``parse_chart``), scripts/research_calendar.py
(``parse_ohlc``), scripts/research_intraday_t.py (``split_events``) and scripts/research_selective_t.py
(``parse_ohlc_payload``, there ``ohlc_frame``). Dates are New York calendar dates.

The request side (phase 3, moved unchanged from scripts/reversal_data_yahoo.py): ``chart_url`` builds the v8 chart
request (no key involved) and ``HEADERS`` are the request headers Yahoo accepts (it answers 429 to a full browser
User-Agent). Fetching, rate limits and caches stay with the callers.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd

from quant.data.guards import assert_dev_dates, truncate_dev


def _ny_date(seconds) -> str:
    return pd.to_datetime(seconds, unit="s", utc=True).tz_convert("America/New_York").strftime("%Y-%m-%d")


def parse_chart(path: Path, end: str) -> pd.DataFrame:
    """Chart JSON file -> date, close (split-adjusted), adjclose, dividend; truncated at ``end`` at once."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "close": q["close"], "adjclose": adj})
    divs = j.get("events", {}).get("dividends", {})
    dmap = {}
    for v in divs.values():
        d = _ny_date(v["date"])
        dmap[d] = dmap.get(d, 0.0) + float(v["amount"])
    df["dividend"] = df["date"].map(dmap).fillna(0.0)
    df = truncate_dev(df.dropna(subset=["close"]), "date", end)
    if df["date"].duplicated().any():
        raise ValueError(f"duplicate dates in {path}")
    return df.reset_index(drop=True)


def parse_ohlc(path: Path, end: str) -> pd.DataFrame:
    """``parse_chart`` (date, close, adjclose, dividend; truncated at ``end``) plus open / high / low."""
    base = parse_chart(path, end)
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    extra = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "open": q["open"], "high": q["high"], "low": q["low"]})
    extra = truncate_dev(extra, "date", end).drop_duplicates("date")
    out = base.merge(extra, on="date", how="left")
    assert_dev_dates(out["date"], end)
    return out


def split_events(path: Path, end: str) -> dict:
    """{ex-date: new shares per old share} for splits on or before ``end``."""
    j = json.loads(Path(path).read_text())["chart"]["result"][0]
    out = {}
    for v in j.get("events", {}).get("splits", {}).values():
        d = _ny_date(v["date"])
        if d <= end:
            out[d] = float(v["numerator"]) / float(v["denominator"])
    return out


def parse_ohlc_payload(payload: dict, end: str) -> tuple[pd.DataFrame, dict]:
    """Parsed chart JSON -> (date, open, high, low, close (split-adjusted), adjclose, dividend (split-adjusted));
    splits {date: ratio}. Rows without a close are dropped, a repeated date keeps its last row."""
    j = payload["chart"]["result"][0]
    ts = pd.to_datetime(j["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = j["indicators"]["quote"][0]
    adj = j["indicators"].get("adjclose", [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": ts.strftime("%Y-%m-%d"), "open": q["open"], "high": q["high"], "low": q["low"],
                       "close": q["close"], "adjclose": adj})
    dmap = {}
    for v in (j.get("events", {}).get("dividends") or {}).values():
        dmap[_ny_date(v["date"])] = dmap.get(_ny_date(v["date"]), 0.0) + float(v["amount"])
    splits = {}
    for v in (j.get("events", {}).get("splits") or {}).values():
        if _ny_date(v["date"]) <= end:
            splits[_ny_date(v["date"])] = float(v["numerator"]) / float(v["denominator"])
    df["dividend"] = df["date"].map(dmap).fillna(0.0)
    df = df.dropna(subset=["close"]).drop_duplicates("date", keep="last")
    df = truncate_dev(df, "date", end).reset_index(drop=True)
    return df, splits


# ======================================================================== requests

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def _epoch(day: str) -> int:
    return int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())


def chart_url(symbol: str, period1: str, period2: str) -> str:
    """The v8 chart URL for [period1, period2) (dates, UTC midnight); no key involved."""
    params = urlencode({"period1": _epoch(period1), "period2": _epoch(period2), "interval": "1d",
                        "events": "div,splits", "includeAdjustedClose": "true"}, safe=",")
    return CHART.format(symbol=symbol) + "?" + params
