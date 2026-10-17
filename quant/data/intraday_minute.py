"""Minute bars for the intraday_minute study: fetch (Alpaca SIP, cached per symbol-month), regular-hours sessions,
early-close days, and the daily OHLC rebuilt from minutes for the validation against Yahoo daily bars.

Raw bodies live in ``research_cache/intraday_minute/raw/<SYMBOL>/<YYYY-MM>__p<n>.json.gz`` (local only, never
committed); the request ledger is ``research_cache/intraday_minute/{raw_index.csv.gz,quota_ledger.csv}``. A parsed
regular-hours copy is kept as ``research_cache/intraday_minute/rth_<SYMBOL>.pkl`` (rebuilt when missing).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.sources import alpaca
from quant.data.sources.http import Ledger, SlidingWindowLimiter
from quant.paths import CACHE_ROOT, MAIN_CHECKOUT

CACHE = CACHE_ROOT / "intraday_minute"
ENV = MAIN_CHECKOUT / ".env.alpaca"
LEDGER = Ledger(CACHE / "raw_index.csv.gz", CACHE / "quota_ledger.csv")

# NYSE / Nasdaq 13:00 ET early closes, 2016-2026 (exchange holiday calendars; checked against the data by
# ``early_close_check``). 2016-12-23, 2020-07-02, 2021-12-23 and 2022-07-01 were full days.
EARLY_CLOSE = frozenset({
    "2016-11-25",
    "2017-07-03", "2017-11-24",
    "2018-07-03", "2018-11-23", "2018-12-24",
    "2019-07-03", "2019-11-29", "2019-12-24",
    "2020-11-27", "2020-12-24",
    "2021-11-26",
    "2022-11-25",
    "2023-07-03", "2023-11-24",
    "2024-07-03", "2024-11-29", "2024-12-24",
    "2025-07-03", "2025-11-28", "2025-12-24",
    "2026-11-27", "2026-12-24",
})


def fetch(symbols, first_month: str, last_month: str, log=print) -> dict:
    """Fetch every missing symbol-month (cached; resumable). Returns pages per symbol."""
    headers = alpaca.env_headers(ENV)
    limiter = SlidingWindowLimiter({60: alpaca.MINUTE_PER_MINUTE})
    pages = {}
    for sym in symbols:
        n = 0
        for month, start, end in alpaca.month_windows(first_month, last_month):
            n += alpaca.fetch_minute_month(CACHE, sym, month, start, end, headers=headers, ledger=LEDGER,
                                           limiter=limiter)
        pages[sym] = n
        log(f"fetched {sym}: {n} pages")
    return pages


def load_rth(symbol: str, first_month: str, last_month: str, end: str, rebuild: bool = False) -> pd.DataFrame:
    """Regular-hours minute bars (date, minute, ts, open, high, low, close, volume, vwap, trades), cut at ``end``."""
    pkl = CACHE / f"rth_{symbol}_{first_month}_{last_month}.pkl"
    if pkl.exists() and not rebuild:
        f = pd.read_pickle(pkl)
    else:
        parts = [alpaca.read_minute_month(CACHE, symbol, m) for m, _, _ in alpaca.month_windows(first_month, last_month)]
        f = alpaca.rth_frame(pd.concat([p for p in parts if len(p)], ignore_index=True), set(EARLY_CLOSE))
        f.to_pickle(pkl)
    f = f[f["date"] <= end].reset_index(drop=True)
    assert f["date"].max() <= end
    return f


def session_length(date: str) -> int:
    return 210 if date in EARLY_CLOSE else 390


def daily_from_minutes(f: pd.DataFrame) -> pd.DataFrame:
    """Per session: open (first RTH bar open), high, low, close (last RTH bar close), volume, bars present."""
    g = f.groupby("date", sort=True)
    return pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                         "close": g["close"].last(), "volume": g["volume"].sum(), "bars": g.size(),
                         "last_minute": g["minute"].max()})


def extended_daily(symbol: str, first_month: str, last_month: str, end: str) -> pd.DataFrame:
    """Per ET date from all bars (extended hours included): volume 09:30-15:59 and 13:00-15:59, and the open / volume of
    the bar stamped at the session's close (16:00, or 13:00 on early closes), which holds the closing-cross print
    when it is reported at the close."""
    out = []
    for m, _, _ in alpaca.month_windows(first_month, last_month):
        f = alpaca.read_minute_month(CACHE, symbol, m)
        if not len(f):
            continue
        et = f["ts"].dt.tz_convert(alpaca.NY)
        minute = (et.dt.hour * 60 + et.dt.minute - 570).values
        date = et.dt.strftime("%Y-%m-%d").values
        g = pd.DataFrame({"date": date, "minute": minute, "open": f["open"].values, "volume": f["volume"].values})
        g = g[g["date"] <= end]
        rth = g[(g["minute"] >= 0) & (g["minute"] < 390)].groupby("date")["volume"].sum()
        late = g[(g["minute"] >= 210) & (g["minute"] < 390)].groupby("date")["volume"].sum()
        g["close_minute"] = np.where(pd.Series(g["date"]).isin(EARLY_CLOSE).values, 210, 390)
        cb = g[g["minute"] == g["close_minute"]].set_index("date")
        out.append(pd.DataFrame({"rth_volume": rth, "late_volume": late}).join(
            cb[["open", "volume"]].rename(columns={"open": "close_bar_open", "volume": "close_bar_volume"})))
    return pd.concat(out).sort_index()
