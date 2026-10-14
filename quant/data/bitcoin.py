"""Daily BTC-USD from the free public sources, cross-checked, on a 7-day UTC calendar and on US sessions.

Conventions (pre-registered in docs/research_ledger_bitcoin.md 0.2):

- **Daily close** of UTC day D = the close of the candle that starts at 00:00 UTC on D, i.e. the price at 00:00 UTC
  of D+1 (20:00 New York in summer time, 19:00 in winter). Every source is labelled this way
  (``quant.data.sources.crypto``).
- **Primary series**: Bitstamp (one venue, continuous from 2011-08); a Bitstamp candle with zero volume (the venue
  was halted, e.g. 2015-01-06..08 after its hack) takes the close of the first source in ``FILL_ORDER`` that has
  that day. Coinbase, Yahoo (a CoinMarketCap-style aggregate) and Kraken (last 720 days only) are the checks.
- **US sessions**: the BTC close attached to US session S is the UTC-day-S close (about 3-4 hours after the 16:00
  New York close of S); a BTC return over session S compounds every UTC day after the previous session up to S,
  so weekends and holidays fall into the next session's return.

Pure data layer: no strategy decisions. Raw files are read from ``research_cache/bitcoin/raw`` (local only).
"""
from __future__ import annotations

import glob
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates
from quant.data.sources import crypto
from quant.data.sources.yahoo import parse_chart
from quant.paths import CACHE_ROOT

CACHE = CACHE_ROOT / "bitcoin"
PRIMARY = "bitstamp"
FILL_ORDER = ("coinbase", "yahoo", "kraken")
AGREE_TOL = 0.005                 # daily returns "agree" when within 0.5 percentage points
# Block-reward halvings (block heights 210,000 / 420,000 / 630,000 / 840,000), UTC dates.
HALVINGS = {"2012-11-28": 210_000, "2016-07-09": 420_000, "2020-05-11": 630_000, "2024-04-20": 840_000}
ETFS = ("IBIT", "FBTC")


@dataclass
class Sources:
    frames: dict            # source -> frame (date, open, high, low, close, volume), truncated at end
    close: pd.DataFrame     # UTC calendar x source closes
    volume: pd.DataFrame


def _read(paths) -> list[bytes]:
    return [Path(p).read_bytes() for p in sorted(paths)]


def load_sources(end: str, cache: Path = CACHE) -> Sources:
    raw = cache / "raw"
    kr = sorted(glob.glob(str(raw / "kraken/ohlc_1440_fetched_*.json")))
    frames = {
        "bitstamp": crypto.parse_bitstamp(_read(glob.glob(str(raw / "bitstamp/*.json"))), end),
        "coinbase": crypto.parse_coinbase(_read(glob.glob(str(raw / "coinbase/*.json"))), end),
        "yahoo": crypto.parse_yahoo_crypto(_read(glob.glob(str(raw / "yahoo/chart_BTC-USD_*.json")))[-1], end),
    }
    if kr:
        day = Path(kr[-1]).stem.rsplit("_", 1)[-1]
        frames["kraken"] = crypto.parse_kraken(Path(kr[-1]).read_bytes(), end, today_utc=day)
    close = pd.DataFrame({k: v.set_index("date")["close"] for k, v in frames.items()})
    volume = pd.DataFrame({k: v.set_index("date")["volume"] for k, v in frames.items()})
    close.index = volume.index = pd.DatetimeIndex(close.index)
    close, volume = close.sort_index(), volume.sort_index()
    assert_dev_dates(close.index, end)
    return Sources(frames=frames, close=close, volume=volume)


def coverage(s: Sources) -> dict:
    """First / last day, rows, missing calendar days and zero-volume days of every source."""
    out = {}
    for k in s.close.columns:
        c = s.close[k].dropna()
        full = pd.date_range(c.index[0], c.index[-1], freq="D")
        out[k] = {"first": str(c.index[0].date()), "last": str(c.index[-1].date()), "rows": int(len(c)),
                  "missing_calendar_days": int(len(full.difference(c.index))),
                  "zero_volume_days": int((s.volume[k] == 0).sum()),
                  "zero_volume_days_list_since_2014": [str(d.date()) for d in
                                                       s.volume.index[(s.volume[k] == 0).values]
                                                       if d >= pd.Timestamp("2014-01-01")]}
    return out


def agreement(close: pd.DataFrame, tol: float = AGREE_TOL) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(pair table, by-year table): share of common days whose close-to-close returns differ by <= ``tol``,
    the largest difference and its day, the median relative level gap, and the same share for monthly returns
    (month-end closes, tolerance 1 point)."""
    r = close.pct_change(fill_method=None)
    me = close.groupby(close.index.to_period("M")).last()
    rm = me.pct_change(fill_method=None)
    rows, years = [], {}
    cols = list(close.columns)
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            d = (r[a] - r[b]).dropna()
            if not len(d):
                continue
            dm = (rm[a] - rm[b]).dropna()
            lv = (close[a] / close[b] - 1).dropna()
            rows.append({"pair": f"{a}-{b}", "first": str(d.index[0].date()), "days": int(len(d)),
                         "share_daily_within_0p5pct": float((d.abs() <= tol).mean()),
                         "max_abs_daily_diff": float(d.abs().max()), "max_day": str(d.abs().idxmax().date()),
                         "median_abs_level_gap": float(lv.abs().median()),
                         "months": int(len(dm)), "share_monthly_within_1pct": float((dm.abs() <= 0.01).mean()),
                         "daily_return_corr": float(r[a].corr(r[b]))})
            years[f"{a}-{b}"] = (d.abs() <= tol).groupby(d.index.year).mean()
    return pd.DataFrame(rows), pd.DataFrame(years)


def primary_close(s: Sources, primary: str = PRIMARY, fill: tuple = FILL_ORDER) -> tuple[pd.Series, dict]:
    """The primary close on every UTC calendar day of its history, stale (zero-volume) days filled from ``fill``.
    Raises when a day cannot be filled."""
    c = s.close[primary].copy()
    stale = (s.volume[primary] == 0) | c.isna()
    filled = {}
    for d in c.index[stale.reindex(c.index).fillna(True).values]:
        for k in fill:
            if k in s.close and pd.notna(s.close.at[d, k]):
                c.at[d] = s.close.at[d, k]
                filled[str(d.date())] = k
                break
    first = c.first_valid_index()
    c = c.loc[first:]
    full = pd.date_range(first, c.index[-1], freq="D")
    if len(full.difference(c.index)) or c.isna().any():
        raise ValueError(f"{primary}: calendar gaps remain")
    return c, {"primary": primary, "filled_days": filled, "first": str(first.date()), "last": str(c.index[-1].date())}


def source_close(s: Sources, name: str, before: pd.Series) -> pd.Series:
    """Series ``name`` from its first day, ``before`` (the primary) for the days before it: the data
    sensitivities (e.g. Coinbase from 2015-07-20, Bitstamp before)."""
    c = s.close[name].dropna()
    head = before.loc[:c.index[0] - pd.Timedelta(days=1)]
    return pd.concat([head, c])


def on_sessions(btc: pd.Series, sessions: pd.DatetimeIndex) -> pd.Series:
    """The UTC-day-S BTC close on each US session S (every session must be a calendar day in ``btc``)."""
    inside = sessions[(sessions >= btc.index[0]) & (sessions <= btc.index[-1])]
    missing = inside.difference(btc.index)
    if len(missing):
        raise ValueError(f"sessions without a BTC close: {list(missing[:5])}")
    return btc.reindex(sessions)


def calendar_rf(rf_sessions: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """Daily T-bill return on the 7-day calendar: the session rate on session days, 0 on the other days (the Ken
    French daily RF accrues per trading day, so the compounded total is unchanged)."""
    return rf_sessions.reindex(days).fillna(0.0)


def load_etf(symbol: str, end: str, cache: Path = CACHE) -> pd.DataFrame:
    """A US-listed spot bitcoin ETF (IBIT, FBTC) from its Yahoo chart file (New York session dates)."""
    path = sorted(glob.glob(str(cache / f"raw/yahoo/chart_{symbol}_*.json")))[-1]
    return parse_chart(Path(path), end)


def drawdown_episodes(p: pd.Series, depth: float = -0.5) -> list[dict]:
    """Peak-to-trough falls deeper than ``depth`` of a price series, one per peak (with recovery date)."""
    out, peak_v, peak_d, trough_v, trough_d, in_dd = [], p.iloc[0], p.index[0], np.inf, None, False
    for d, v in p.items():
        if v >= peak_v:
            if in_dd and trough_v / peak_v - 1 <= depth:
                out.append({"peak": str(peak_d.date()), "trough": str(trough_d.date()),
                            "depth": float(trough_v / peak_v - 1), "recovered": str(d.date()),
                            "days_peak_to_recovery": int((d - peak_d).days)})
            peak_v, peak_d, trough_v, trough_d, in_dd = v, d, np.inf, None, False
        else:
            in_dd = True
            if v < trough_v:
                trough_v, trough_d = v, d
    if in_dd and trough_v / peak_v - 1 <= depth:
        out.append({"peak": str(peak_d.date()), "trough": str(trough_d.date()), "depth": float(trough_v / peak_v - 1),
                    "recovered": "not recovered", "days_peak_to_recovery": None})
    return out
