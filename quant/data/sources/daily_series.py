"""Cached daily price series of one symbol for the event studies (spin-offs, Nasdaq-100 reconstitution): the Yahoo
chart (cached by quant.data.sources.sec_cache under research_cache/spinoffs) and the Quandl WIKI file (to 2018-03).

Extracted unchanged from scripts/research_spinoffs.py (``_yahoo_frame``, ``series_yahoo``, ``series_wiki``); each
returns date, close_raw, volume_raw, tr (daily total return) and source, or None.
"""
from __future__ import annotations

import pandas as pd

from quant.data.sources import sec_cache as sec
from quant.paths import CACHE_ROOT

WIKI = CACHE_ROOT / "reversal_2012_2026/wiki/by_ticker"


def _yahoo_frame(res: dict) -> pd.DataFrame:
    ts = pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert("America/New_York")
    q = res["indicators"]["quote"][0]
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
    df = pd.DataFrame({"date": pd.to_datetime(ts.strftime("%Y-%m-%d")), "close": q["close"],
                       "volume": q["volume"], "adj": adj})
    df = df.dropna(subset=["close", "adj"]).drop_duplicates("date", keep="last").sort_values("date")
    splits = (res.get("events") or {}).get("splits") or {}
    df["F"] = 1.0
    for v in splits.values():
        d = pd.Timestamp(pd.to_datetime(v["date"], unit="s", utc=True).tz_convert("America/New_York").date())
        ratio = float(v["numerator"]) / float(v["denominator"])
        if ratio > 0:
            df.loc[df.date < d, "F"] *= ratio
    df["close_raw"] = df["close"] * df["F"]
    df["volume_raw"] = df["volume"].fillna(0) / df["F"]
    df["tr"] = df["adj"].pct_change()
    return df[["date", "close_raw", "volume_raw", "tr"]]


def series_yahoo(symbol: str, offline: bool = False) -> pd.DataFrame | None:
    if not symbol:
        return None
    try:
        res = sec.yahoo_chart(symbol, offline=offline)
    except FileNotFoundError:
        return None
    if res is None or not res.get("timestamp"):
        return None
    if (res.get("meta") or {}).get("dataGranularity", "1d") != "1d":
        return None
    df = _yahoo_frame(res)
    df["source"] = "yahoo:" + symbol
    return df


def series_wiki(symbol: str) -> pd.DataFrame | None:
    p = WIKI / f"{symbol}.csv.gz"
    if not symbol or not p.exists():
        return None
    w = pd.read_csv(p)
    df = pd.DataFrame({"date": pd.to_datetime(w["date"]), "close_raw": w["close"], "volume_raw": w["volume"],
                       "tr": w["adj_close"].pct_change()})
    df["source"] = "wiki:" + symbol
    return df.dropna(subset=["close_raw"]).sort_values("date")
