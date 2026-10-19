"""Daily self-checks of the forward observation (docs/forward_observation_checklist.md section B0, "Daily health").

The monthly log alone would show a broken pipeline, a stale vendor or a missed signal only weeks later. These checks
run every US trading day after the close (``quant.observation.daily``, workflow forward_observation_daily.yml) and
each returns ok / warn / fail with a one-line message:

- ``data_freshness``: the latest bar of every series equals the last completed NYSE session (exchange_calendars
  XNYS plus ``EXTRA_CLOSURES`` and closures no vendor has a bar for; a session counts as complete from 18:00 New York
  time, the runner's own convention), and the runner's data end is that session. Only a bar missing for that session
  alone may be late: a warning on the evening's early runs (and before 20:00 New York), a failure on its last run.
- ``data_integrity``: no missing or extra sessions (the forward window and, by sessions only, each file's whole
  history), no non-positive prices, no day move beyond a frozen bound once splits and dividends are taken out, close +
  dividend and adjusted-close returns of one file within 1%, and the history state overlap of the fetch is clean.
- ``vendor_agreement``: the stored series vs a second vendor (Alpaca, Yahoo or Tiingo, whichever is not the source)
  over the whole forward window: real closes within 0.2% (B3 shorts 0.5%), day total returns (dividends, adjusted
  closes) within 1% (warning from 0.5%), and every large day move of the last 10 sessions confirmed by a second
  vendor. No second vendor at all is a failure on the evening's last run.
- ``signal_consistency``: B1 S3-Yb and B2 SEL-A / SEL-P re-derived by a small separate implementation (SMA20 trigger
  and limit exits; Wilder RSI2 with its expanding 2nd percentile / median) from the stored charts, compared trade by
  trade (entry, exit, reason, open positions, repeated trades, and each trade's net return against the rule's fill
  prices) with what the frozen runner produced for the same as-of date; 18 B1 accounts; SEL H_match = w x QQQ total
  return.
- ``log_status``: no month row is ``final`` before the data hold a session of a later month (B3: of its own line).
- ``b3_signal``: from the session after each month-end signal session the frozen S-MISP signal file exists, holds a
  U300 of at most 300 names with valid scores and a worst-2N list of exactly 2N = 20 names (re-sorted here), never
  changes once recorded, and the runner's shorts come from it.
- ``b3_borrow_snapshots``: report only (never more than ok with a note): IBKR borrow-fee snapshots once B3 trades.
- ``replay_drift``: the runner's daily numbers and closed trades for days already recorded do not move between runs
  (1e-9; series built on adjusted closes 2e-6, B3 NAV 1e-4) and no final log row changes; a move is a warning when
  the vendor revised one of that number's own inputs (by more than the vendors' fetch-to-fetch noise) on or before
  that day (named), a failure otherwise.
- ``pipeline_liveness``: the previous completed daily run covered the previous session.

"Independent" means the trigger and exit conditions are recomputed here from the raw vendor files with code that
shares nothing with the frozen simulators (quant.strategies.selective_t / t_grid) or their loaders; the registered
values (checklist B1 / B2 / B3) are transcribed below on purpose instead of imported, so a change to a frozen module
shows up as a mismatch. Nothing here changes a rule, a number of the log, or a frozen file.

health.json holds statuses, dates, symbols, counts and percentages only, never vendor price levels.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

NY = ZoneInfo("America/New_York")
OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}
SCHEMA = 2                                 # 2: replay inputs stored as ratio values (1: 10-digit hashes)

# ======================================================================== frozen values (fixed 2026-10-19)
# Transcribed from docs/forward_observation_checklist.md B0 / B1 / B2 / B3, not imported from quant.strategies.

COMPLETE_HOUR_NY = 18                      # a session counts as complete from 18:00 New York time (runner, B0.2)
START_CLOSE = "2026-10-12"                 # B1 / B2 accounts open at this close (B0.6)
B3_FIRST_SIGNAL = "2026-10-30"             # first S-MISP month-end signal (B3.2)
U18 = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "COST", "NFLX", "CSCO", "INTC", "ADBE",
       "QCOM", "PEP", "AMD", "CMCSA", "AMGN")
B1_RULE = {"drop": 0.10, "sma": 20, "target": 0.06, "max_hold": 20}            # S3-Yb, stock parameters
B2_RULES = {"SEL-A": {"max_hold": 10}, "SEL-P": {"max_hold": 20}}               # RSI2 p2 B OPP ... res25 ALL
B2_RSI_N, B2_P_ENTRY, B2_P_EXIT, B2_MIN_OBS = 2, 0.02, 0.50, 200
B3_N, B3_BUFFER, B3_U300 = 10, 2, 300
B3_BORROW_TIERS = (0.005, 0.02, 0.10)
TICK, THROUGH_FRAC = 0.01, 0.0005          # a resting sell limit fills when the high trades through it

# check thresholds (fixed 2026-10-19; calibrated on the real files 2024-2026, see the checklist B0.8)
DAY_MOVE_BOUND = {"etf": 0.20, "stock": 0.50, "b3": 1.10}   # max |ln(day move)|, split and dividend adjusted
CONFIRM_BOUND = {"etf": 0.07, "stock": 0.15, "b3": 0.40}   # |ln(day move)| above this needs a second vendor
VENDOR_TOL = 0.002                         # B1 / B2 series: real closes of two vendors within 0.2% (seen: 0.041%)
VENDOR_TOL_B3 = 0.005                      # B3 shorts (small caps): within 0.5% (seen: 0.107%)
VENDOR_TR_WARN, VENDOR_TR_FAIL = 0.005, 0.01   # day total returns of two vendors (seen: 4.1 bp Yahoo / Alpaca,
#                                                10.7 bp on B3 names, 88 bp Tiingo / Yahoo on a spin-off day)
SELF_TR_TOL = 0.01                         # one file: close + dividend return vs adjusted-close return (seen: 5.6 bp)
VENDOR_DAYS = 10                           # large day moves to confirm: the last 10 sessions
PRICE_BAND = 0.005                         # rule fill-price return minus runner net: 0..50 bp of costs (real: 3-13 bp)
REPLAY_TOL = 1e-9                          # recorded daily returns, closed trades
REPLAY_TOL_OF = {"hmatch": 2e-6, "oneq": 2e-6, "qqq": 2e-6, "nav": 1e-4}   # series on adjusted closes (seen: 5e-7)
#                                            (B3 NAV: Alpaca rounds adjusted closes to the cent)
FP_FIELDS = ("c", "o", "h", "l", "a", "d", "s")   # input ratios: close / previous, open / high / low over the close,
#                                                   adjusted close / previous, dividend / close, split
FP_TOL = {"c": 1e-8, "o": 1e-8, "h": 1e-8, "l": 1e-8, "a": 2e-6, "d": 1e-8, "s": 1e-9}   # a revision is larger
#                                                   (Yahoo's adjusted closes move by 4-7e-7 from fetch to fetch)
FP_TOL_B3 = dict(FP_TOL, a=3e-3)           # Alpaca-stored B3 charts: adjusted closes rounded to the cent
EFFR_TOL = 1e-9
BORDERLINE = 1e-8                          # relative margin below which an independent decision counts as a tie
INTEGRITY_FROM_DAYS = 45                   # integrity window starts 45 calendar days before START_CLOSE
FINGERPRINT_FROM_DAYS = 7                  # input fingerprints from 7 days before START_CLOSE (the history anchor)
B3_STALE_FAIL_SESSIONS = 2                 # a held short whose bars stop 1-2 sessions ago: fail (vendor delay?)
B3_MIN_COVERAGE, B3_MIN_U300 = 0.85, 200   # warn below (in sample: coverage 90-99%, U300 262-280)
EFFR_WARN_SESSIONS, EFFR_FAIL_SESSIONS = 3, 6   # the EFFR file this many sessions behind (published next morning)
FRESH_GRACE_HOUR_NY = 20                   # a bar missing before 20:00 New York on its own day is a warning
EXTRA_CLOSURES: tuple = ()                 # unscheduled NYSE closures exchange_calendars does not know yet
#                                            (e.g. a national day of mourning): add "YYYY-MM-DD" here
MAX_ITEMS = 25


@dataclass
class Check:
    name: str
    status: str
    message: str
    items: list = field(default_factory=list)
    data: dict = field(default_factory=dict, repr=False)   # for the daily run, not written to health.json

    def as_dict(self) -> dict:
        out = {"name": self.name, "status": self.status, "message": self.message}
        if self.items:
            out["items"] = self.items[:MAX_ITEMS] + ([f"... {len(self.items) - MAX_ITEMS} more"]
                                                    if len(self.items) > MAX_ITEMS else [])
        return out


def worst(statuses) -> str:
    return max(statuses, key=_RANK.__getitem__, default=OK)


def _d(x) -> str:
    return str(pd.Timestamp(x).date())


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


# ======================================================================== NYSE calendar


@lru_cache(maxsize=16)
def _xnys(start: str, end: str) -> pd.DatetimeIndex:
    import exchange_calendars as xcals
    cal = xcals.get_calendar("XNYS", start=start, end=end)
    return pd.DatetimeIndex(cal.sessions).tz_localize(None)


def nyse_sessions(start, end) -> pd.DatetimeIndex:
    """XNYS sessions in [start, end] (holidays, unscheduled closures and half days as exchange_calendars has them),
    without ``EXTRA_CLOSURES`` (closures announced after the pinned library version)."""
    a, b = pd.Timestamp(start), pd.Timestamp(end)
    ses = _xnys(str((a - pd.Timedelta(days=7)).date()), str((b + pd.Timedelta(days=7)).date()))
    ses = ses.difference(pd.DatetimeIndex(pd.to_datetime(list(EXTRA_CLOSURES))))
    return ses[(ses >= a) & (ses <= b)]


def unscheduled_closures(frames: dict, others: dict | None, calendar: pd.DatetimeIndex, start, target) -> list:
    """Calendar sessions in [start, target) that no stored B1 / B2 series and no second vendor has a bar for, while
    the stored series do hold a later bar: an exchange closure the calendar library does not know yet (e.g. a
    national day of mourning). One vendor having the bar and another lacking it is a data fault, not a closure."""
    have = [df.index for df in frames.values() if df is not None and len(df)]
    if not have:
        return []
    last = max(ix.max() for ix in have)
    ses = calendar[(calendar >= pd.Timestamp(start)) & (calendar < min(pd.Timestamp(target), last))]
    seen = have[0]
    for ix in have[1:] + [df.index for f in (others or {}).values() for df in f.values() if df is not None]:
        seen = seen.union(ix)
    return [d for d in ses if d not in seen]


def default_calendar(now: datetime | None = None) -> pd.DatetimeIndex:
    now = now or datetime.now(timezone.utc)
    start = pd.Timestamp(START_CLOSE) - pd.Timedelta(days=INTEGRITY_FROM_DAYS + 30)
    return nyse_sessions(start, pd.Timestamp(now.date()) + pd.Timedelta(days=40))   # (a month's last session known)


def last_completed_session(now: datetime | None = None, calendar: pd.DatetimeIndex | None = None) -> pd.Timestamp:
    """The latest NYSE session whose 18:00 New York time has passed (half days too: their bars are final by then)."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    today = pd.Timestamp(now.date())
    cal = calendar if calendar is not None else nyse_sessions(today - pd.Timedelta(days=30), today)
    ses = cal[cal <= today]
    if len(ses) and ses[-1] == today and now.hour < COMPLETE_HOUR_NY:
        ses = ses[:-1]
    if not len(ses):
        raise ValueError(f"no NYSE session on or before {today.date()} in the calendar")
    return ses[-1]


def session_on_or_before(day, calendar: pd.DatetimeIndex) -> pd.Timestamp:
    ses = calendar[calendar <= pd.Timestamp(day)]
    if not len(ses):
        raise ValueError(f"no session on or before {_d(day)}")
    return ses[-1]


def next_session(day, calendar: pd.DatetimeIndex) -> pd.Timestamp | None:
    later = calendar[calendar > pd.Timestamp(day)]
    return later[0] if len(later) else None


def sessions_between(a, b, calendar: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """Sessions strictly after a and strictly before b."""
    return calendar[(calendar > pd.Timestamp(a)) & (calendar < pd.Timestamp(b))]


def month_end_sessions(calendar: pd.DatetimeIndex, first, last) -> list:
    """The last session of each calendar month, from ``first`` through ``last`` (both dates)."""
    ses = calendar[(calendar >= pd.Timestamp(first))]
    out = pd.Series(ses, index=ses).groupby(ses.to_period("M")).max()
    return [d for d in out if d <= pd.Timestamp(last)]


# ======================================================================== charts (own reader of the Yahoo-format files)


def read_payload(source) -> dict:
    if isinstance(source, dict):
        return source
    p = Path(source)
    data = p.read_bytes()
    return json.loads(gzip.decompress(data) if p.suffix == ".gz" else data)


def chart_frame(source, end=None) -> pd.DataFrame | None:
    """A Yahoo v8 chart body (dict or file) -> frame by New York date: open / high / low / close as stored
    (split-adjusted), adjclose, div (split-adjusted cash amount), split (ratio on its date) and factor (real price =
    stored price x factor, the product of the later splits up to ``end``). Rows without a close are dropped, a
    repeated date keeps its last row. None when unreadable or empty."""
    try:
        j = read_payload(source)["chart"]["result"][0]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None
    ts = j.get("timestamp") or []
    if not ts:
        return None
    dates = pd.to_datetime(ts, unit="s", utc=True).tz_convert(NY).tz_localize(None).normalize()
    q = (j.get("indicators") or {}).get("quote", [{}])[0]
    n = len(dates)

    def col(v):
        return pd.to_numeric(pd.Series(v if v is not None else [None] * n), errors="coerce").to_numpy(float)
    adj = ((j["indicators"].get("adjclose") or [{}])[0].get("adjclose")) or q.get("close")
    df = pd.DataFrame({k: col(q.get(k)) for k in ("open", "high", "low", "close")}, index=dates)
    df["adjclose"] = col(adj)
    df = df[np.isfinite(df["close"].to_numpy())]
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if end is not None:
        df = df[df.index <= pd.Timestamp(end)]
    if df.empty:
        return None
    ev = j.get("events") or {}

    def day(t):
        return pd.Timestamp(pd.to_datetime(int(t), unit="s", utc=True).tz_convert(NY).date())
    div = pd.Series(0.0, index=df.index)
    for v in (ev.get("dividends") or {}).values():
        d = day(v["date"])
        if d in div.index:
            div[d] += float(v["amount"])
    split = pd.Series(1.0, index=df.index)
    for v in (ev.get("splits") or {}).values():
        d = day(v["date"])
        if d in split.index:
            split[d] = float(v["numerator"]) / float(v["denominator"])
    df["div"] = div
    df["split"] = split
    later = split[::-1].cumprod()[::-1].shift(-1).fillna(1.0)      # product of the splits after each date
    df["factor"] = later.to_numpy(float)
    return df


def real_close(df: pd.DataFrame) -> pd.Series:
    return df["close"] * df["factor"]


def _ceil_cent(x: float) -> float:
    return math.ceil(round(x / TICK, 6)) * TICK


# ======================================================================== 1. data freshness


def in_grace(now: datetime, target) -> bool:
    """Before FRESH_GRACE_HOUR_NY on the session day itself a vendor may not have published the bar yet."""
    ny = now.astimezone(NY)
    return pd.Timestamp(ny.date()) == pd.Timestamp(target) and ny.hour < FRESH_GRACE_HOUR_NY


def check_freshness(frames: dict, target, calendar: pd.DatetimeIndex, data_end=None, held: dict | None = None,
                    effr_last=None, grace: bool = False, held_other: dict | None = None) -> Check:
    """``frames`` {symbol: frame or None} (B1 / B2 series); ``held`` {ticker: frame or None} (B3 shorts held now);
    ``held_other`` {ticker: (last bar of the second vendor or None, True when the second vendor has no data for it)}.
    With ``grace`` (an early run of the evening, or a run before 20:00 New York) a bar missing for the session just
    closed is a warning, but only when it is the only bar missing (the series ends at the previous session): a series
    further behind, and every stale series on the evening's last run, fails. A held short whose bars stop is a
    failure when the second vendor has later bars (the stored vendor is behind), a warning when both vendors stop at
    the same day or the second vendor has no data (delisted or halted: the frozen rule covers it at its last value);
    without a second vendor it fails when 1-2 sessions behind (vendor delay?) and warns when further behind."""
    target = pd.Timestamp(target)
    before = calendar[calendar < target]
    prev = before[-1] if len(before) else None
    late = " (not published yet? the evening's last run fails if still missing)"
    items, state = [], {"fail": False, "warn": False}

    def behind_of(last) -> int:
        return len(calendar[(calendar > pd.Timestamp(last)) & (calendar <= target)])

    def stale(what: str, last) -> None:
        if grace and last is not None and prev is not None and pd.Timestamp(last) == prev:
            items.append(f"{what}: last bar {_d(last)}, expected {_d(target)}{late}")
            state["warn"] = True
            return
        n = behind_of(last) if last is not None else None
        items.append(f"{what}: last bar {_d(last) if last is not None else 'none'}, expected {_d(target)}"
                     + (f" ({n} sessions behind)" if n and n > 1 else ""))
        state["fail"] = True

    for sym, df in frames.items():
        if df is None:
            items.append(f"{sym}: no readable data file")
            state["fail"] = True
            continue
        last = df.index[df.index <= target].max()
        if pd.isna(last) or last != target:
            stale(sym, None if pd.isna(last) else last)
    lasts = {df.index[df.index <= target].max() for df in frames.values() if df is not None and len(df)}
    if not grace and len(frames) > 1 and all(df is not None for df in frames.values()) and len(lasts) == 1 \
            and next(iter(lasts)) < target:
        items.append(f"every series stops at {_d(next(iter(lasts)))}: if the exchange was closed on {_d(target)} "
                     "(an unscheduled closure), add that date to health.EXTRA_CLOSURES")
    if data_end is not None and pd.Timestamp(data_end) != target:
        stale("runner data end", pd.Timestamp(data_end))
    for tick, df in (held or {}).items():
        last = None if df is None else df.index[df.index <= target].max()
        if last is None or pd.isna(last):
            items.append(f"B3 short {tick}: no readable chart")
            state["fail"] = True
            continue
        if last == target:
            continue
        other, gone = (held_other or {}).get(tick, (None, False))
        n = behind_of(last)
        if other is not None and pd.Timestamp(other) > last:
            stale(f"B3 short {tick} (the second vendor has bars through {_d(other)})", last)
        elif other is not None or gone:
            items.append(f"B3 short {tick}: last bar {_d(last)}, {n} session(s) behind, and the second vendor "
                         + ("has no data for it" if gone and other is None else f"also stops at {_d(other)}")
                         + ": delisted or halted? (the frozen rule covers it at its last value)")
            state["warn"] = True
        elif n <= B3_STALE_FAIL_SESSIONS:
            stale(f"B3 short {tick} (no second vendor to tell a vendor delay from a delisting)", last)
        else:
            items.append(f"B3 short {tick}: last bar {_d(last)}, {n} sessions behind (the frozen rule covers it at "
                         "its last value)")
            state["warn"] = True
    if effr_last is not None:
        n = behind_of(effr_last)
        if n > EFFR_WARN_SESSIONS:
            items.append(f"EFFR file ends {_d(effr_last)}, {n} sessions behind")
            state["fail" if n > EFFR_FAIL_SESSIONS else "warn"] = True
    n = len(frames) + len(held or {})
    status = FAIL if state["fail"] else WARN if state["warn"] else OK
    msg = (f"all {n} series end at the last completed session {_d(target)}" if status == OK
           else f"{len(items)} problem(s) against the last completed session {_d(target)}")
    return Check("data_freshness", status, msg, items)


# ======================================================================== 2. data integrity


def _bound_of(sym: str) -> float:
    return DAY_MOVE_BOUND["etf" if sym in ("QQQ", "ONEQ") else "stock"]


def integrity_items(sym: str, df: pd.DataFrame, start, target, calendar: pd.DatetimeIndex, bound: float,
                    gaps_fail: bool = True, self_tr: bool = False) -> tuple[list, list]:
    """(failures, warnings) of one series on [start, target]. ``self_tr``: the close + dividend return and the
    adjusted-close return of the same file must agree within SELF_TR_TOL (Yahoo / Tiingo files; not Alpaca charts,
    which carry no dividend events)."""
    fails, warns = [], []
    start, target = pd.Timestamp(start), pd.Timestamp(target)
    w = df[(df.index >= start) & (df.index <= target)]
    if w.empty:
        return [f"{sym}: no rows in {_d(start)}..{_d(target)}"], []
    exp = calendar[(calendar >= max(start, w.index[0])) & (calendar <= w.index[-1])]   # a stale end: freshness
    missing = exp.difference(w.index)
    extra = w.index.difference(calendar)
    if len(missing):
        (fails if gaps_fail else warns).append(
            f"{sym}: {len(missing)} missing session(s): {', '.join(_d(d) for d in missing[:5])}")
    if len(extra):
        fails.append(f"{sym}: {len(extra)} bar(s) on non-session dates: {', '.join(_d(d) for d in extra[:5])}")
    px = w[["open", "high", "low", "close", "adjclose"]]
    nonpos = (px <= 0).any(axis=1)
    if nonpos.any():
        fails.append(f"{sym}: non-positive price on {', '.join(_d(d) for d in w.index[nonpos][:5])}")
    nan = ~np.isfinite(px.to_numpy()).all(axis=1)
    if nan.any():
        warns.append(f"{sym}: open / high / low / adjclose missing on {', '.join(_d(d) for d in w.index[nan][:5])} "
                     "(the runner fills them from the close)")
    with np.errstate(invalid="ignore"):
        o, h, lo, c = (w[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        bad = (o > h * (1 + 1e-6)) | (o < lo * (1 - 1e-6)) | (c > h * (1 + 1e-6)) | (c < lo * (1 - 1e-6))
    if bad.any():
        warns.append(f"{sym}: open or close outside high-low on {', '.join(_d(d) for d in w.index[bad][:5])}")
    # day moves: split-adjusted close with the dividend added back, and the adjusted close (total return)
    full = df[df.index <= target]
    # (a raw move that a split or a dividend explains is within the bound here, since both are taken out)
    with np.errstate(invalid="ignore", divide="ignore"):
        move = np.log((full["close"] + full["div"]) / full["close"].shift(1))
        tr = np.log(full["adjclose"] / full["adjclose"].shift(1))
    for d in full.index[full.index >= start]:
        m, t = move.get(d, np.nan), tr.get(d, np.nan)
        if not (np.isfinite(m) and np.isfinite(t)):
            continue
        if abs(m) > bound or abs(t) > bound:
            what = f"{sym} {_d(d)}: day move {_pct(math.exp(m) - 1)} (adjusted close {_pct(math.exp(t) - 1)})"
            ev = []
            if full.at[d, "split"] != 1.0:
                ev.append(f"split {full.at[d, 'split']:g}")
            if full.at[d, "div"] > 0:
                ev.append("dividend")
            fails.append(what + (f" beyond the frozen bound even with the {' and '.join(ev)} taken out" if ev
                                 else " beyond the frozen bound; no split or dividend explains it"))
    if self_tr:
        with np.errstate(invalid="ignore", divide="ignore"):
            gap = (np.exp(move - tr) - 1).abs()
        gap = gap[(gap.index >= start) & np.isfinite(gap)]
        for d, x in gap[gap > SELF_TR_TOL].items():
            fails.append(f"{sym} {_d(d)}: close + dividend return and adjusted-close return differ by "
                         f"{x * 100:.2f} points (a dividend, split or adjusted close is wrong)")
    return fails, warns


def large_moves(frames: dict, target, calendar: pd.DatetimeIndex, held: dict | None = None,
                days: int = VENDOR_DAYS) -> dict:
    """{symbol: [dates]} of the day moves above CONFIRM_BOUND (split and dividend adjusted) on the last ``days``
    sessions: within the frozen bound, but they must be confirmed by a second vendor (check_vendors)."""
    recent = calendar[calendar <= pd.Timestamp(target)][-days:]
    out = {}
    for sym, df, kind in [(s, f, "etf" if s in ("QQQ", "ONEQ") else "stock") for s, f in frames.items()] + \
            [(s, f, "b3") for s, f in (held or {}).items()]:
        if df is None:
            continue
        full = df[df.index <= pd.Timestamp(target)]
        with np.errstate(invalid="ignore", divide="ignore"):
            m = np.log((full["close"] + full["div"]) / full["close"].shift(1)).reindex(recent)
        big = [d for d, x in m.items() if np.isfinite(x) and abs(x) > CONFIRM_BOUND[kind]]
        if big:
            out[sym] = big
    return out


def check_integrity(frames: dict, target, calendar: pd.DatetimeIndex, start=None, held: dict | None = None,
                    held_start=None, history: bool = False, overlap: dict | None = None,
                    closures=(), accepted=()) -> Check:
    """``history``: also the sessions of each file's history before the window (real NYSE calendar only);
    ``overlap`` {symbol: the fetch's history check}: the fetched tail must agree with the committed history state;
    ``closures``: sessions dropped from ``calendar`` because no vendor has a bar for them (reported as warnings);
    ``accepted`` {"SYMBOL YYYY-MM-DD"}: day moves and total-return gaps the owner reviewed (--accept-data; e.g. a
    corporate action), reported but not failed. ``data["keys"]``: the keys of the failing day items."""
    start = pd.Timestamp(start) if start is not None else (pd.Timestamp(START_CLOSE)
                                                           - pd.Timedelta(days=INTEGRITY_FROM_DAYS))
    fails, warns = [], []
    warns += [f"{_d(d)}: no stored series and no second vendor has a bar for this NYSE session: an unscheduled "
              "closure? (left out of the calendar; add it to health.EXTRA_CLOSURES)" for d in closures]
    for sym, chk in sorted((overlap or {}).items()):
        if chk and chk.get("overlap_ok") is False:
            fails.append(f"{sym}: the fetched tail disagrees with the committed history state on overlapping days "
                         f"(max close-ratio difference {chk.get('overlap_max_abs_close_ratio_diff')})")
    for sym, df in frames.items():
        if df is None:
            fails.append(f"{sym}: no readable data file")
            continue
        f, w = integrity_items(sym, df, start, target, calendar, _bound_of(sym), self_tr=True)
        fails += f
        warns += w
        if history and df.index[0] < start:              # the history before the window: sessions only
            full = nyse_sessions(df.index[0], start - pd.Timedelta(days=1))
            h = df.index[df.index < start]
            miss, extra = full.difference(h), h.difference(full)
            if len(miss) or len(extra):
                fails.append(f"{sym}: history before {_d(start)}: {len(miss)} missing session(s) "
                             f"{', '.join(_d(d) for d in miss[:3])}, {len(extra)} non-session bar(s) "
                             f"{', '.join(_d(d) for d in extra[:3])}")
    for tick, df in (held or {}).items():
        if df is None:
            continue                                      # reported by data_freshness
        f, w = integrity_items(f"B3 short {tick}", df, held_start or start, target, calendar, DAY_MOVE_BOUND["b3"],
                               gaps_fail=False)           # small caps can be halted: a gap is a warning
        fails += f
        warns += w
    keys, kept, acc = [], [], []
    for x in fails:
        m = re.match(r"(.+?) (\d{4}-\d{2}-\d{2}): (day move|close \+ dividend)", x)
        if m and f"{m.group(1)} {m.group(2)}" in set(accepted or ()):
            acc.append(f"{x} (accepted earlier)")
            continue
        if m:
            keys.append(f"{m.group(1)} {m.group(2)}")
        kept.append(x)
    fails = kept
    status = FAIL if fails else WARN if warns else OK
    n = len(frames) + len(held or {})
    msg = (f"{n} series clean from {_d(start)} to {_d(target)} (sessions, positive prices, day moves within the "
           "frozen bounds)" if status == OK else f"{len(fails)} failure(s), {len(warns)} warning(s)")
    return Check("data_integrity", status, msg, fails + warns + acc, {"keys": keys})


# ======================================================================== 3. vendor agreement


def check_vendors(primary: dict, others: dict, target, calendar: pd.DatetimeIndex, sources: dict | None = None,
                  notes=(), days: int = VENDOR_DAYS, extra_days: dict | None = None, confirm: dict | None = None,
                  tols: dict | None = None, strict: bool = True, accepted=(), must=()) -> Check:
    """``primary`` {symbol: frame of the stored file}; ``others`` {vendor: {symbol: frame}} fetched by the run;
    ``sources`` {symbol: vendor of the stored file} (that vendor is not compared with itself). On every common session
    among the last ``days`` and the ``extra_days`` of each symbol (the whole forward window), the real closes (stored
    price x later splits) must agree within ``tols`` [symbol] (default VENDOR_TOL) and the day total returns (stored
    adjusted close and stored close + dividend vs the second vendor's adjusted close; an Alpaca-stored chart has no
    dividend events, so only its adjusted close) within VENDOR_TR_FAIL (warning above VENDOR_TR_WARN). ``confirm``
    {symbol: [dates]} (``large_moves``): each such move needs a second vendor that agrees on that day. ``accepted``
    {"SYMBOL YYYY-MM-DD"}: disagreements the owner reviewed (--accept-data), reported but not failed. ``must``: the
    symbols that need a second vendor; with none at all for any of them the check fails when ``strict`` (the
    evening's last run), else warns. Not ``strict`` (an early run), a disagreement on the session just closed and a
    large move the second vendor has no bar for yet are warnings: the vendors may still publish or correct that bar
    until 20:00 New York. ``data["keys"]``: the failing "SYMBOL YYYY-MM-DD" keys."""
    target = pd.Timestamp(target)
    ses = calendar[calendar <= target]
    recent = ses[-days:]
    fails, warns, acc, keys = [], [], [], []
    compared, done, vendors_used = 0, set(), set()
    confirmed, seen = set(), set()
    accepted = set(accepted or ())
    settle = " (the session just closed: a vendor may still correct it; the evening's last run fails if it stays)"

    def bad(sym, d, text, out=fails):
        key = f"{sym} {_d(d)}"
        if out is fails and not strict and pd.Timestamp(d) == target:
            out, text = warns, text + settle
        if out is fails and key in accepted:
            acc.append(f"{text} (accepted earlier)")
            return
        out.append(text)
        if out is fails:
            keys.append(key)
    for vendor, frames in others.items():
        for sym, sdf in frames.items():
            pdf = primary.get(sym)
            if pdf is None or sdf is None or (sources or {}).get(sym) == vendor:
                continue
            want = recent.union(pd.DatetimeIndex((extra_days or {}).get(sym, [])))
            sdf = sdf[sdf.index <= target]
            a, b = real_close(pdf).reindex(ses), real_close(sdf).reindex(ses)
            pa, sa = pdf["adjclose"].reindex(ses), sdf["adjclose"].reindex(ses)
            pc = pdf["close"].reindex(ses)
            ta, tb = pa / pa.shift(1), sa / sa.shift(1)
            td = (pc + pdf["div"].reindex(ses).fillna(0.0)) / pc.shift(1)
            j = pd.concat([a, b, ta, td, tb], axis=1, keys=["p", "s", "tp", "td", "ts"]).reindex(want)
            j = j[j["p"].notna() & j["s"].notna()]
            if j.empty:
                continue
            done.add(sym)
            vendors_used.add(vendor)
            compared += len(j)
            tol = (tols or {}).get(sym, VENDOR_TOL)
            rel = (j["p"] / j["s"] - 1).abs()
            gaps = [(j["tp"] / j["ts"] - 1).abs()]
            if (sources or {}).get(sym) != "alpaca":
                gaps.append((j["td"] / j["ts"] - 1).abs())
            trd = pd.concat(gaps, axis=1).max(axis=1, skipna=False)
            for d in j.index:
                seen.add((sym, d))
                if rel[d] > tol:
                    bad(sym, d, f"{sym} {_d(d)}: stored close differs from {vendor} by {rel[d] * 100:.2f}% "
                                f"(tolerance {tol:.1%})")
                elif np.isfinite(trd[d]) and trd[d] > VENDOR_TR_FAIL:
                    bad(sym, d, f"{sym} {_d(d)}: stored day total return differs from {vendor} by "
                                f"{trd[d] * 100:.2f} points (a dividend, split or adjusted close is wrong)")
                elif np.isfinite(trd[d]):
                    confirmed.add((sym, d))
                    if trd[d] > VENDOR_TR_WARN:
                        bad(sym, d, f"{sym} {_d(d)}: stored day total return differs from {vendor} by "
                                    f"{trd[d] * 100:.2f} points (a corporate action treated differently?)", warns)
    for sym, ds in sorted((confirm or {}).items()):
        for d in ds:
            if (sym, pd.Timestamp(d)) in confirmed:
                continue
            text = f"{sym} {_d(d)}: day move beyond the confirmation bound and no second vendor confirms it"
            if not strict and (sym, pd.Timestamp(d)) not in seen:
                bad(sym, d, text + " yet (the evening's last run fails if still unconfirmed)", warns)
            else:
                bad(sym, d, text)
    missing = [m for m in must if m not in done]
    none_at_all = bool(must) and len(missing) == len(must)
    items = fails + warns + acc + ([f"no second vendor for {', '.join(missing)}"] if missing else []) \
        + [str(n) for n in notes]
    data = {"keys": keys}
    if fails or (none_at_all and strict):
        msg = (f"{len(fails)} close(s), day return(s) or large move(s) not confirmed by a second vendor"
               if fails else "no second vendor available to compare with (the evening's last run)")
        return Check("vendor_agreement", FAIL, msg, items, data)
    if none_at_all or not done:
        return Check("vendor_agreement", WARN, "no second vendor available to compare with", items, data)
    head = (f"{len(done)} series vs {', '.join(sorted(vendors_used))}: {compared} days agree (closes within "
            f"{VENDOR_TOL:.1%}, B3 shorts {VENDOR_TOL_B3:.1%}; day total returns within {VENDOR_TR_FAIL:.0%})"
            + (f"; {sum(len(v) for v in (confirm or {}).values())} large move(s) confirmed" if confirm else ""))
    if warns or missing:
        return Check("vendor_agreement", WARN, head + f"; {len(warns) + bool(missing)} warning(s)", items, data)
    return Check("vendor_agreement", OK, head, items, data)


# ======================================================================== 4. signal consistency (B1 / B2)


def _window(df: pd.DataFrame, end, start_close=START_CLOSE) -> tuple[int, int] | None:
    """(first forward session, last session <= end) as positions in ``df``; None before the first forward session."""
    ses = df.index
    i0 = ses.searchsorted(pd.Timestamp(start_close))
    if i0 >= len(ses) or ses[i0] != pd.Timestamp(start_close):
        return None
    e = int(ses.searchsorted(pd.Timestamp(end), side="right")) - 1
    return (i0 + 1, e) if e >= i0 + 1 else None


def b1_replay(df: pd.DataFrame, end, rule: dict = B1_RULE, start_close=START_CLOSE) -> dict:
    """S3-Yb on one account, from the rule text: trigger at the close of t when close <= (1 - drop) x SMA20 (split
    adjusted closes, day t included), buy at the next open (one trade at a time); sell limit each day = min(SMA20
    through the previous close, entry x (1 + target)), or entry x (1 + target) when that SMA is not above the entry,
    rounded up to the cent; filled at the open when the open is at or above it, else when the high trades through it
    by max(1 cent, 0.05%); forced sale at the close of holding day ``max_hold`` (entry day = day 1). At the last
    session an open trade is reported as open ('end')."""
    out = {"trades": [], "next": [], "ties": [], "started": False}
    win = _window(df, end, start_close)
    if win is None:
        return out
    s, e = win
    out["started"] = True
    c = df["close"].to_numpy(float)
    sma = pd.Series(c).rolling(rule["sma"], min_periods=rule["sma"]).mean().to_numpy()
    o, h, f = df["open"].to_numpy(float), df["high"].to_numpy(float), df["factor"].to_numpy(float)
    o = np.where(np.isfinite(o), o, c)
    h = np.where(np.isfinite(h), h, c)
    ses = df.index
    lim_k = 1 - rule["drop"]

    def trig(i):
        if not np.isfinite(sma[i]):
            return False
        margin = (lim_k * sma[i] - c[i]) / c[i]
        if abs(margin) < BORDERLINE:
            out["ties"].append(f"trigger at {_d(ses[i])}")
        return margin >= 0
    cur, queued = None, False
    for i in range(s, e + 1):
        if queued:
            cur = {"entry": i, "entry_adj": o[i], "days": 0}
            queued = False
        if cur is not None:
            cur["days"] += 1
            ent = cur["entry_adj"] * f[i]                  # entry price in today's units
            tgt = ent * (1 + rule["target"])
            sp = sma[i - 1] * f[i] if i >= 1 else np.nan
            x = min(sp, tgt) if (np.isfinite(sp) and sp > ent) else tgt
            if np.isfinite(sp) and abs(sp - ent) / ent < BORDERLINE:
                out["ties"].append(f"limit choice {_d(ses[i])}")
            lim = _ceil_cent(x)
            need = lim + max(TICK, THROUGH_FRAC * lim)
            oi, hi = o[i] * f[i], h[i] * f[i]
            for v, ref in ((oi, lim), (hi, need)):
                if abs(v - ref) / ref < BORDERLINE:
                    out["ties"].append(f"fill at {_d(ses[i])}")
            reason, px = None, None
            if oi >= lim or hi >= need - 1e-9:
                reason, px = "target", (oi if oi >= lim else lim)
            elif cur["days"] >= rule["max_hold"]:
                reason, px = "timeout", c[i] * f[i]
            elif i == e:
                reason, px = "end", c[i] * f[i]
            if reason:
                out["trades"].append({"entry": ses[cur["entry"]], "exit": ses[i], "reason": reason,
                                      "ret": px / ent - 1})
                if reason == "end":
                    out["next"].append(f"open since {ses[cur['entry']]:%m-%d} (holding day {cur['days']} of "
                                       f"{rule['max_hold']}; sell limit resting)")
                    cur = None
                    continue
                cur = None
        if i < e:
            if cur is None and trig(i):
                queued = True
        elif cur is None and trig(i):
            out["next"].append(f"close <= {lim_k:g} x SMA{rule['sma']} at the {ses[i]:%m-%d} close: buy at the "
                               "next open")
    return out


def rsi_wilder(c: np.ndarray, n: int) -> np.ndarray:
    """Wilder RSI(n): seed = mean of the first n changes, then (prev x (n - 1) + x) / n."""
    out = np.full(len(c), np.nan)
    if len(c) <= n:
        return out
    d = np.diff(c)
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = up[:n].mean(), dn[:n].mean()
    for t in range(n, len(c)):
        if t > n:
            au = (au * (n - 1) + up[t - 1]) / n
            ad = (ad * (n - 1) + dn[t - 1]) / n
        out[t] = 100.0 if ad == 0 else 100.0 - 100.0 / (1.0 + au / ad)
    return out


def b2_levels(df: pd.DataFrame, end, start_close=START_CLOSE) -> dict | None:
    """RSI2 of the split-adjusted QQQ closes and, for each forward session, its expanding 2nd percentile and median
    over its own history through the previous session (at least 200 values; linear interpolation)."""
    win = _window(df, end, start_close)
    if win is None:
        return None
    s, e = win
    r = rsi_wilder(df["close"].to_numpy(float), B2_RSI_N)
    lo, med = {}, {}
    for i in range(s, e + 1):
        hist = r[:i]
        hist = hist[np.isfinite(hist)]
        if len(hist) < B2_MIN_OBS:
            lo[i] = med[i] = np.nan
            continue
        lo[i], med[i] = np.quantile(hist, [B2_P_ENTRY, B2_P_EXIT])
    return {"s": s, "e": e, "rsi": r, "lo": lo, "med": med, "sessions": df.index,
            "open": np.where(np.isfinite(df["open"].to_numpy(float)), df["open"].to_numpy(float),
                             df["close"].to_numpy(float)), "close": df["close"].to_numpy(float)}


def b2_replay(lv: dict, max_hold: int) -> dict:
    """RSI2-dip (B), OPP exit: entry at the next open after a close with RSI2 <= its p2 level (no trade open or
    queued); exit at the next open after a close with RSI2 >= its median (from the entry day on); otherwise a forced
    sale at the close of holding day ``max_hold``. At the last session an open trade is reported as open ('end')."""
    out = {"trades": [], "next": [], "ties": []}
    s, e, r, ses = lv["s"], lv["e"], lv["rsi"], lv["sessions"]

    def cmp(i, level, name):
        if not (np.isfinite(r[i]) and np.isfinite(level)):
            return None
        if abs(r[i] - level) < BORDERLINE * max(1.0, abs(level)):
            out["ties"].append(f"{name} at {_d(ses[i])}")
        return r[i] - level
    cur, qx, qe = None, False, False
    for i in range(s, e + 1):
        o_, c_ = lv["open"], lv["close"]
        if cur is not None and qx:
            out["trades"].append({"entry": ses[cur["entry"]], "exit": ses[i], "reason": "target",
                                  "ret": o_[i] / o_[cur["entry"]] - 1})
            cur, qx = None, False
        if qe:
            cur, qe = {"entry": i, "days": 0}, False
        if cur is not None:
            cur["days"] += 1
            if cur["days"] >= max_hold:
                out["trades"].append({"entry": ses[cur["entry"]], "exit": ses[i], "reason": "timeout",
                                      "ret": c_[i] / o_[cur["entry"]] - 1})
                cur = None
        x_lo, x_med = cmp(i, lv["lo"][i], "entry level"), cmp(i, lv["med"][i], "exit level")
        if i == e:
            if cur is not None:
                out["trades"].append({"entry": ses[cur["entry"]], "exit": ses[i], "reason": "end",
                                      "ret": c_[i] / o_[cur["entry"]] - 1})
                tail = ("RSI2 at or above its median: sell at the next open" if x_med is not None and x_med >= 0 else
                        "time stop at the next close" if cur["days"] + 1 >= max_hold else "holding")
                out["next"].append(f"open since {ses[cur['entry']]:%m-%d} (holding day {cur['days']} of "
                                   f"{max_hold}); {tail}")
            elif x_lo is not None and x_lo <= 0:
                out["next"].append(f"RSI2 at or below its expanding 2nd percentile at the {ses[i]:%m-%d} close: "
                                   "buy at the next open")
            continue
        if cur is not None and x_med is not None and x_med >= 0:
            qx = True
        if x_lo is not None and x_lo <= 0 and cur is None and not qe:
            qe = True
    return out


def _key(t: dict) -> tuple:
    """(entry, exit, reason) of a trade; a trade still open at the as-of session has reason 'end'."""
    return (_d(t["entry"]), _d(t["exit"]), "end" if t.get("open") or t["reason"] == "end" else t["reason"])


def _compare(name: str, mine: list, theirs: list, ties: list) -> tuple[list, list]:
    """(failures, warnings): trades only one side has. With a numerical tie in the independent replay (a value
    within 1e-8 of its threshold) a difference is a warning, not a failure."""
    a, b = Counter(_key(t) for t in mine), Counter(_key(t) for t in theirs)
    only_a, only_b = sorted((a - b).elements()), sorted((b - a).elements())
    diffs = [f"{name}: independent rule has {k[0]}→{k[1]} {k[2]}, runner does not" for k in only_a]
    diffs += [f"{name}: runner has {k[0]}→{k[1]} {k[2]}"
              + (" twice or more" if k in a else ", independent rule does not") for k in only_b]
    own = {_key(t): t for t in mine}
    for t in theirs:
        m = own.get(_key(t))
        if m is None or "ret" not in m or t.get("net_pct") is None:
            continue
        gap = m["ret"] - float(t["net_pct"]) / 100
        if not (-1e-9 <= gap <= PRICE_BAND):
            k = _key(t)
            diffs.append(f"{name}: {k[0]}→{k[1]} {k[2]}: runner net {float(t['net_pct']):+.2f}% vs "
                         f"{m['ret'] * 100:+.2f}% from the rule's fill prices (costs must be 0..{PRICE_BAND:.1%})")
            only_a.append(k)
    if ties and diffs:
        first = min(only_a + only_b, key=lambda k: k[0])
        lo = pd.Timestamp(first[0]) - pd.Timedelta(days=5)
        days = [pd.Timestamp(re.search(r"\d{4}-\d{2}-\d{2}", x).group()) for x in ties]
        if any(lo <= d <= pd.Timestamp(first[1]) for d in days):    # a tie can explain the first divergence
            return [], [d + f" (numerical tie: {', '.join(ties[:3])})" for d in diffs]
    return diffs, []


def hmatch_items(name: str, line: dict, df: pd.DataFrame, end) -> list:
    """H_match (the main comparison) of a SEL line recomputed from its definition: each day of month m is w_m (the
    month's mean exposure the runner reports) x the instrument's day total return (own reader: adjusted close ratio;
    no financing, w <= 1). Catches a wrong instrument, month or w in the frozen H_match chain."""
    hm = line.get("hmatch")
    if hm is None or not len(hm):
        return []
    a = df["adjclose"][df.index <= pd.Timestamp(end)]
    tr = a / a.shift(1) - 1
    out = []
    for d, x in hm.dropna().items():
        w = (line.get("w") or {}).get(str(pd.Timestamp(d).to_period("M")))
        want = np.nan if w is None or d not in tr.index else w * tr[d]
        if not (np.isfinite(want) and abs(x - want) <= REPLAY_TOL):
            out.append(f"{name}: H_match {_d(d)} is {x * 100:+.4f}%, w x QQQ total return gives "
                       + (f"{want * 100:+.4f}%" if np.isfinite(want) else "nothing (no w or no bar)"))
    return out[:5] + ([f"{name}: ... {len(out) - 5} more H_match day(s)"] if len(out) > 5 else [])


def check_signals(lines: dict, frames: dict, end, stocks=None, universe_check: bool = True) -> tuple[Check, list]:
    """Compare the runner's B1 / SEL-A / SEL-P trades (``lines`` = runner ``compute(...)['lines']``) with the
    independent replay on ``frames`` (own reader, truncated at the as-of date). Returns (check, next-session
    notes)."""
    fails, warns, nxt, counts = [], [], [], {}
    stocks = tuple(stocks if stocks is not None else U18)
    if universe_check and stocks != U18:
        fails.append(f"B1 universe {list(stocks)} differs from the registered U18")
    # B1
    b1 = lines.get("B1", {})
    mine_all = []
    for sym in stocks:
        df = frames.get(sym)
        if df is None:
            fails.append(f"B1 {sym}: no data for the independent check")
            continue
        rp = b1_replay(df, end)
        for t in rp["trades"]:
            t["symbol"] = sym
        mine_all += rp["trades"]
        nxt += [f"B1 {sym}: {x}" for x in rp["next"]]
        theirs = [t for t in (b1.get("trades") or []) if t["symbol"] == sym]
        f, w = _compare(f"B1 {sym}", rp["trades"], theirs, rp["ties"])
        fails += f
        warns += w
        if rp["started"] != bool(b1.get("started")) and rp["started"]:
            fails.append(f"B1 {sym}: the forward window has started but the runner's B1 line has not")
    counts["B1"] = len(mine_all)
    n_started = sum(1 for sym in stocks if frames.get(sym) is not None and _window(frames[sym], end) is not None)
    if b1.get("started") and b1.get("n_accounts") is not None and int(b1["n_accounts"]) != n_started:
        fails.append(f"B1: the runner's basket has {b1['n_accounts']} accounts, the independent check {n_started}")
    # B2
    qqq = frames.get("QQQ")
    lv = b2_levels(qqq, end) if qqq is not None else None
    for name, rule in B2_RULES.items():
        line = lines.get(name, {})
        if lv is None:
            if line.get("started"):
                fails.append(f"{name}: runner started but the independent check has no forward window")
            counts[name] = 0
            continue
        if not line.get("started"):
            fails.append(f"{name}: the forward window has started but the runner's line has not")
            continue
        rp = b2_replay(lv, rule["max_hold"])
        theirs = [{**t, "symbol": ""} for t in line.get("trades") or []]
        f, w = _compare(name, rp["trades"], theirs, rp["ties"])
        fails += f
        warns += w
        nxt += [f"{name}: {x}" for x in rp["next"]]
        counts[name] = len(rp["trades"])
        fails += hmatch_items(name, line, qqq, end)
    if lv is not None and np.isfinite(lv["lo"].get(lv["e"], np.nan)):
        i = lv["e"]
        nxt.append(f"B2 RSI2 at the {lv['sessions'][i]:%m-%d} close: {lv['rsi'][i]:.1f} (entry level "
                   f"{lv['lo'][i]:.1f}, exit level {lv['med'][i]:.1f})")
    status = FAIL if fails else WARN if warns else OK
    started = any(lines.get(k, {}).get("started") for k in ("B1", *B2_RULES))
    if status == OK:
        msg = ("before the forward window: nothing to compare" if not started and not any(counts.values()) else
               "runner trades match the independent rules: "
               + ", ".join(f"{k} {v} trade(s)" for k, v in counts.items()))
    else:
        msg = f"{len(fails)} mismatch(es)" + (f", {len(warns)} at a numerical tie" if warns else "")
    return Check("signal_consistency", status, msg, fails + warns), nxt


# ======================================================================== 5. log status (no final row too early)

_TABLE = re.compile(r"<!-- table:(?P<key>[\w-]+):start -->\n(?P<body>.*?)<!-- table:(?P=key):end -->", re.S)


def check_log_status(text: str, data_end, line_ends: dict | None = None) -> Check:
    """Every month row before the month of the data end is final, the row of that month is provisional.
    ``line_ends`` {table key: last session of that line}: a line that stops earlier (B3 while its next signal file
    is not computed yet, which ``b3_signal`` reports) is judged against its own last session."""
    if not text:
        return Check("log_status", WARN, "no log text to check")
    last = pd.Timestamp(data_end).to_period("M")
    m = re.search(r"data through: (\d{4}-\d{2}-\d{2})", text)
    items = []
    if not m or pd.Timestamp(m.group(1)) != pd.Timestamp(data_end):
        items.append(f"status line 'data through' is {m.group(1) if m else 'missing'}, data end {_d(data_end)}")
    n = 0
    for t in _TABLE.finditer(text):
        for ln in t.group("body").splitlines():
            cells = [c.strip() for c in ln.strip().strip("|").split("|")]
            if not (ln.startswith("|") and re.fullmatch(r"\d{4}-\d{2}", cells[0] or "")):
                continue
            n += 1
            mon, status = pd.Period(cells[0], "M"), cells[-1]
            own = (line_ends or {}).get(t.group("key"))
            lim = pd.Timestamp(own).to_period("M") if own is not None else last
            want = "final" if mon < lim else "provisional"
            if mon > lim or status != want:
                items.append(f"{t.group('key')} {cells[0]}: status '{status}', expected '{want}' with data through "
                             f"{_d(own if own is not None else data_end)}")
    if items:
        return Check("log_status", FAIL, f"{len(items)} row(s) with a wrong status", items)
    return Check("log_status", OK, f"{n} month row(s); only {last} is provisional" if n else "no month rows yet")


# ======================================================================== 6. B3 S-MISP signal files


def b3_due_signals(calendar: pd.DatetimeIndex, target) -> list:
    """Month-end signal sessions whose file is due by ``target``: the signal is computed by the run of the next
    session (when the data first show that it was the month's last session), before its execution at that close."""
    return [s for s in month_end_sessions(calendar, B3_FIRST_SIGNAL, target) if s < pd.Timestamp(target)]


def worst_2n(sc: pd.DataFrame, col: str = "S-MISP") -> list:
    v = sc[np.isfinite(pd.to_numeric(sc[col], errors="coerce"))].copy()
    v[col] = v[col].astype(float)
    return list(v.sort_values([col, "security_id"])["security_id"].head(B3_BUFFER * B3_N))


def validate_signal(path: Path, s) -> tuple[list, list, pd.DataFrame | None]:
    """(failures, warnings, frame) of one frozen signal file and its _meta.json."""
    fails, warns = [], []
    name = path.name
    try:
        sc = pd.read_csv(path, dtype={"security_id": str, "ticker": str, "yahoo": str})
    except (OSError, ValueError) as exc:
        return [f"{name}: unreadable ({type(exc).__name__})"], [], None
    need = {"s", "security_id", "ticker", "S-MISP", "borrow"}
    if not need <= set(sc.columns):
        return [f"{name}: missing columns {sorted(need - set(sc.columns))}"], [], None
    if (pd.to_datetime(sc["s"]) != pd.Timestamp(s)).any():
        fails.append(f"{name}: rows with another signal date")
    if sc["security_id"].isna().any() or sc["security_id"].duplicated().any():
        fails.append(f"{name}: missing or repeated security ids")
    score = pd.to_numeric(sc["S-MISP"], errors="coerce")
    scored = int(score.notna().sum())
    if len(sc) > B3_U300:
        fails.append(f"{name}: {len(sc)} names, more than the U{B3_U300}")
    if ((score < 0) | (score > 1)).any():
        fails.append(f"{name}: scores outside 0..1")
    tiers = pd.to_numeric(sc["borrow"], errors="coerce")
    if not tiers.apply(lambda x: any(abs(x - t) < 1e-12 for t in B3_BORROW_TIERS)).all():
        fails.append(f"{name}: borrow rates outside the registered tiers {B3_BORROW_TIERS}")
    if scored < B3_N:
        fails.append(f"{name}: {scored} scored names, fewer than N = {B3_N} (no trade)")
    elif scored < B3_BUFFER * B3_N:
        warns.append(f"{name}: {scored} scored names, fewer than 2N = {B3_BUFFER * B3_N}")
    if len(sc) < B3_MIN_U300 or (len(sc) and scored / len(sc) < B3_MIN_COVERAGE):
        warns.append(f"{name}: U300 {len(sc)} names, {scored} scored (in sample 262-280, 90-99%)")
    meta_p = path.with_name(path.stem + "_meta.json")
    if not meta_p.exists():
        fails.append(f"{meta_p.name}: missing")
    else:
        meta = json.loads(meta_p.read_text())
        mine = list(sc.set_index("security_id").loc[worst_2n(sc), "ticker"])
        want = min(B3_BUFFER * B3_N, scored)
        if len(meta.get("worst_2n") or []) != want or list(meta.get("worst_2n") or []) != mine:
            fails.append(f"{meta_p.name}: worst-2N list ({len(meta.get('worst_2n') or [])} names) is not the "
                         f"{want} lowest scores of the file")
        if int(meta.get("u300", -1)) != len(sc) or int(meta.get("scored", -1)) != scored:
            fails.append(f"{meta_p.name}: counts (u300 {meta.get('u300')}, scored {meta.get('scored')}) differ from "
                         f"the file ({len(sc)}, {scored})")
    return fails, warns, sc


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_b3(state_dir: Path, target, calendar: pd.DatetimeIndex, line: dict | None,
             known_hashes: dict | None = None, grace: bool = False, data_end=None) -> tuple[Check, dict, dict]:
    """Returns (check, {file name: sha256} of the valid signal files, {security id: yahoo symbol} of the names held
    after the latest rebalance). With ``grace`` a signal due at this session's run that is still missing is a warning
    (it needs this session's bar; the evening's run after 20:00 New York fails)."""
    target = pd.Timestamp(target)
    due = b3_due_signals(calendar, target)
    hashes, held_map, fails, warns = {}, {}, [], []
    if not due:
        nxt = month_end_sessions(calendar, B3_FIRST_SIGNAL, pd.Timestamp(B3_FIRST_SIGNAL) + pd.Timedelta(days=40))
        when = ("is today's close: computed by the next session's run" if nxt and nxt[0] == target
                else f"is the {_d(nxt[0]) if nxt else B3_FIRST_SIGNAL} close")
        return Check("b3_signal", OK, f"no S-MISP signal due yet (the first signal {when})"), hashes, held_map
    signals = {}
    for s in due:
        p = Path(state_dir) / f"s_{_d(s)}.csv"
        exe = next_session(s, calendar)
        if not p.exists():
            msg = (f"signal {_d(s)} missing: due at the {_d(exe)} run (executed at that close); the line stops at "
                   f"{_d(s)}")
            if grace and exe == target:
                warns.append(msg + " (needs this session's bar; a run after 20:00 New York fails if still missing)")
            else:
                fails.append(msg)
            continue
        f, w, sc = validate_signal(p, s)
        fails += f
        warns += w
        h = file_sha256(p)
        old = (known_hashes or {}).get(p.name)
        if old and old != h:
            fails.append(f"{p.name}: changed after it was first recorded (frozen signal files are never recomputed)")
        if not f:
            hashes[p.name] = h
            signals[s] = sc
    executable = [s for s in signals if next_session(s, calendar) is not None and next_session(s, calendar) <= target]
    if executable:
        if not line or not line.get("started"):
            fails.append(f"signal(s) {', '.join(_d(s) for s in executable)} executable but the runner's B3 line did "
                         f"not start ({(line or {}).get('reason', 'no line')})")
        else:
            end = pd.Timestamp(data_end) if data_end is not None else target
            if pd.Timestamp(line["last_session"]) != end and not line.get("pending"):
                fails.append(f"B3 line ends {_d(line['last_session'])}, the data end at {_d(end)}")
            mk = line["mk"]
            for t, s, order, info in line["sched"]:
                exe = next_session(s, calendar)
                if exe is None or mk.sessions[t] != exe:
                    fails.append(f"signal {_d(s)} executed at {_d(mk.sessions[t])}, the next session is "
                                 f"{_d(exe) if exe is not None else 'unknown'}")
            held = sorted(line["res"]["held"], key=lambda x: x[0])
            if held:
                t_last, names = held[-1]
                sig = [s for t, s, *_ in line["sched"] if t <= t_last]
                s_last = max(sig) if sig else None
                sc = signals.get(pd.Timestamp(s_last)) if s_last is not None else None
                if sc is not None:
                    allowed = set(worst_2n(sc))
                    out = [n for n in names if n not in allowed]
                    if out:
                        fails.append(f"B3 shorts outside the worst 2N of {_d(s_last)}: {out}")
                    if len(names) > B3_N:
                        fails.append(f"B3 holds {len(names)} shorts, more than N = {B3_N}")
                    elif len(names) < B3_N:
                        warns.append(f"B3 holds {len(names)} shorts after the {_d(mk.sessions[t_last])} rebalance "
                                     f"(N = {B3_N}; fewer only when whole-share rounding exhausts the worst 2N)")
                    ymap = dict(zip(sc["security_id"], sc["yahoo"] if "yahoo" in sc else sc["ticker"]))
                    held_map = {n: ymap.get(n, n) for n in names}
    status = FAIL if fails else WARN if warns else OK
    msg = (f"{len(signals)} signal file(s) valid ({', '.join(_d(s) for s in signals)})"
           + (f"; B3 line through {_d(line['last_session'])}" if line and line.get("started") else "")
           if status == OK else f"{len(fails)} failure(s), {len(warns)} warning(s)")
    return Check("b3_signal", status, msg, fails + warns), hashes, held_map


def check_b3_borrow(borrow_dir: Path, target, line: dict | None) -> Check:
    """Report only, never more than ok with a note (a warning every day would make the status meaningless): IBKR
    borrow-fee snapshots (scripts/record_borrow_fees.py) once B3 holds shorts. They are recorded in the private data
    repo, not on GitHub Actions; the strategy itself uses the registered tiers."""
    if not line or not line.get("started"):
        return Check("b3_borrow_snapshots", OK, "not needed before B3's first trade")
    from quant.data.sources.ibkr_borrow_fees import BorrowSnapshots
    idx = BorrowSnapshots(Path(borrow_dir)).index
    usable = [t for t, _ in idx if t < pd.Timestamp(target) + pd.Timedelta(days=1)]
    if not usable:
        return Check("b3_borrow_snapshots", OK, "report only: no IBKR borrow snapshot here (recorded in the private "
                                                "data repo, not available on Actions); the report-only fee columns "
                                                "show 'no snapshot', the strategy uses the registered tiers")
    age = (pd.Timestamp(target) - usable[-1].normalize()).days
    if age > 4:
        return Check("b3_borrow_snapshots", OK, f"report only: the latest IBKR borrow snapshot is {age} days old "
                                                f"({usable[-1].date()})")
    return Check("b3_borrow_snapshots", OK, f"{len(idx)} IBKR snapshot(s), latest {usable[-1].date()}")


# ======================================================================== 7. replay drift


def _fmt(v: float) -> str:
    return "nan" if not np.isfinite(v) else f"{v:.12g}"


def chart_fingerprints(df: pd.DataFrame, start, end, days=None) -> dict:
    """{date: "c o h l a d s"}: the price ratios of each day as values (no levels; FP_FIELDS): close / previous
    close, open / high / low over the close, adjusted close / previous, dividend / close, split. Compared with a
    tolerance (FP_TOL), so the vendors' fetch-to-fetch noise is not a revision. ``days``: only these dates (B3:
    holding periods)."""
    c, a = df["close"], df["adjclose"]
    rows = pd.DataFrame({"c": c / c.shift(1), "o": df["open"] / c, "h": df["high"] / c, "l": df["low"] / c,
                         "a": a / a.shift(1), "d": df["div"] / c, "s": df["split"]})
    rows = rows[(rows.index >= pd.Timestamp(start)) & (rows.index <= pd.Timestamp(end))]
    if days is not None:
        rows = rows[rows.index.isin(pd.DatetimeIndex(days))]
    return {_d(d): " ".join(_fmt(x) for x in r) for d, r in zip(rows.index, rows.to_numpy(float))}


def effr_fingerprints(path: Path, start, end) -> dict:
    """{date: rate} of the EFFR file's daily rates."""
    if not Path(path).exists():
        return {}
    df = pd.read_csv(path)
    d = pd.to_datetime(df.iloc[:, 0], format="%m/%d/%Y", errors="coerce")
    v = pd.to_numeric(df.iloc[:, 2], errors="coerce")
    s = pd.Series(v.to_numpy(float), index=d).dropna().sort_index()
    s = s[(s.index >= pd.Timestamp(start)) & (s.index <= pd.Timestamp(end))]
    return {_d(k): _fmt(x) for k, x in s.items()}


def _returns(level: pd.Series) -> pd.Series:
    return (level / level.shift(1) - 1).iloc[1:]


def replay_numbers(lines: dict) -> dict:
    """{series: {date: value}} and {trade key: net} of a runner result (daily returns only)."""
    series, trades, held = {}, {}, {}
    for k in ("B1", *B2_RULES):
        v = lines.get(k, {})
        if not v.get("started"):
            continue
        for s in ("ret", "hbase", "hmatch", "oneq"):
            x = v[s].dropna()
            series[f"{k}.{s}"] = {_d(d): float(y) for d, y in x.items()}
        for t in v.get("trades") or []:
            if t.get("open") or t["reason"] == "end":
                continue
            key = "|".join((k, t.get("symbol", ""), _d(t["entry"]), _d(t["exit"]), t["reason"]))
            trades[key] = float(t["net_pct"]) / 100
    v = lines.get("B3", {})
    if v.get("started"):
        for s in ("nav", "qqq", "oneq"):
            series[f"B3.{s}"] = {_d(d): float(y) for d, y in _returns(v[s]).items()}
        mk = v["mk"]
        for t, names in v["res"]["held"]:
            held[_d(mk.sessions[t])] = " ".join(sorted(str(n) for n in names))
    return {"series": series, "trades": trades, "b3_held": held}


def _differs(old, new, tol) -> bool:
    """Two recorded fingerprints differ by more than the tolerance (a dict per FP_FIELDS, or one number)."""
    try:
        a, b = [float(x) for x in str(old).split()], [float(x) for x in str(new).split()]
    except ValueError:
        return old != new                         # (an unreadable value: compared as text)
    if len(a) != len(b):
        return True
    tols = [tol[f] for f in FP_FIELDS] if isinstance(tol, dict) else [tol] * len(a)
    for x, y, t in zip(a, b, tols):
        if math.isfinite(x) != math.isfinite(y) or (math.isfinite(x) and abs(x - y) > t):
            return True
    return False


def input_changes(prev: dict, cur: dict, before: str | None = None) -> list:
    """[(kind, key, date)] of the fingerprints recorded before that differ now beyond the vendors' fetch-to-fetch
    noise (FP_TOL; B3 charts FP_TOL_B3). New dates are not changes, and neither is a fingerprint that is missing now
    (a dropped bar or a stopped line is caught by the other checks; it must not excuse a drift). Dates on or after
    ``before`` (the previous run's data end: its last bar may still be corrected that evening) are left out: no
    number compared here depends on them."""
    out = []
    for kind, tol in (("px", FP_TOL), ("b3", FP_TOL_B3), ("effr", EFFR_TOL)):
        prev_k, cur_k = prev.get(kind) or {}, cur.get(kind) or {}
        if kind == "effr":
            prev_k, cur_k = {"EFFR": prev_k}, {"EFFR": cur_k}
        for key, per in prev_k.items():
            now = cur_k.get(key, {})
            for p, old in per.items():
                if before is not None and p >= before:
                    continue
                new = now.get(p)
                if new is not None and _differs(old, new, tol):
                    out.append((kind, key, p))
    return out


def _needs(what: str, stocks) -> set:
    """The inputs (kind, key; key None = every key of that kind) a recorded number depends on, so that a vendor
    revision explains only the numbers built on that instrument. ``what``: a series name ("B1.ret"), a trade key
    ("B1|AAPL|..."), "B3.held" or "log:<line>"."""
    if what.startswith("log:"):
        line = what[4:]
        return (_needs(f"{line}.ret", stocks) | _needs(f"{line}.oneq", stocks)
                | (_needs("B3.held", stocks) | {("effr", None)} if line == "B3" else set()))
    if "|" in what:
        line, sym = what.split("|")[:2]
        return {("px", sym)} if line == "B1" else {("px", "QQQ")}
    line, kind = what.split(".")
    if kind == "oneq":
        return {("px", "ONEQ")}
    if line == "B1":
        return {("px", s) for s in stocks}
    if line in B2_RULES or kind == "qqq":
        return {("px", "QQQ")}
    return {("b3", None), ("px", "QQQ")} | ({("effr", None)} if kind == "nav" else set())   # B3 nav / held


def _explained(what: str, day: str, changes: list, stocks=()) -> list:
    """The input changes on or before ``day`` that can move the number ``what`` (``_needs``)."""
    need = _needs(what, stocks)
    return [(kind, key, per) for kind, key, per in changes
            if ((kind, key) in need or (kind, None) in need) and per <= day]


_FINAL_ROW = re.compile(r"(B1|SEL-A|SEL-P|B3) (\d{4}-\d{2}): a final row changed")


def check_replay(prev: dict | None, cur_numbers: dict, cur_inputs: dict, data_end, runner_warnings=(),
                 accept: bool = False) -> tuple[Check, dict | None]:
    """Compare this run's numbers with the recorded ones on the days that were already complete at the previous run
    (returns before its last session, H_match before its last month, closed trades), and the log's final rows
    (the runner reports a final row it rewrote). Returns (check, new state or None when the state must not be
    rewritten)."""
    data_end = pd.Timestamp(data_end)
    state = {"schema": SCHEMA, "data_end": _d(data_end), "numbers": copy.deepcopy(cur_numbers), "inputs": cur_inputs}
    notes = [f"runner: {w.splitlines()[0]}" for w in runner_warnings if not _FINAL_ROW.match(w)]
    if not prev or not prev.get("numbers") or prev.get("schema") != SCHEMA:
        n = sum(len(v) for v in cur_numbers["series"].values())
        why = "" if not prev or not prev.get("numbers") else " (the recorded state had an older format)"
        return Check("replay_drift", OK, f"baseline recorded ({n} daily numbers){why}", notes), state
    p_end = pd.Timestamp(prev["data_end"])
    if data_end < p_end:
        return Check("replay_drift", WARN, f"as-of earlier than the recorded state ({_d(p_end)}): not compared, "
                                           "state kept", notes), None
    pn, cn = prev["numbers"], cur_numbers
    stocks = sorted(k for k in {**(prev.get("inputs") or {}).get("px", {}), **(cur_inputs.get("px") or {})}
                    if k not in ("QQQ", "ONEQ"))
    changes = input_changes(prev.get("inputs") or {}, cur_inputs, before=_d(p_end))
    drift_fail, drift_warn = [], []
    keep = {"series": {}, "trades": {}, "b3_held": {}}
    for name, old in pn["series"].items():
        line, kind = name.split(".")
        tol = REPLAY_TOL_OF.get(kind, REPLAY_TOL)
        new = cn["series"].get(name, {})
        for day, x in old.items():
            if kind == "hmatch":
                if pd.Timestamp(day).to_period("M") >= p_end.to_period("M"):
                    continue
            elif pd.Timestamp(day) >= p_end:
                continue
            y = new.get(day)
            if y is not None and abs(y - x) <= tol:
                continue
            what = (f"{name} {day}: {x * 100:+.6f}% -> " + (f"{y * 100:+.6f}%" if y is not None else "missing"))
            # H_match of a month uses that whole month's exposure: a revision up to the month end explains it
            upto = _d(pd.Period(day, "M").end_time) if kind == "hmatch" else day
            if _explained(name, upto, changes, stocks):
                drift_warn.append(what)
            else:
                drift_fail.append(what)
                keep["series"].setdefault(name, {})[day] = x
    for key, x in pn.get("trades", {}).items():
        line, _, _, exit_, _ = key.split("|")
        if pd.Timestamp(exit_) >= p_end:
            continue
        y = cn["trades"].get(key)
        if y is not None and abs(y - x) <= REPLAY_TOL:
            continue
        what = f"trade {key}: " + ("missing now" if y is None else f"net {x * 100:+.4f}% -> {y * 100:+.4f}%")
        if _explained(key, _d(exit_), changes, stocks):
            drift_warn.append(what)
        else:
            drift_fail.append(what)
            keep["trades"][key] = x
    for day, names in pn.get("b3_held", {}).items():
        if pd.Timestamp(day) >= p_end or cn["b3_held"].get(day) == names:
            continue
        what = f"B3 shorts after the {day} rebalance changed"
        if _explained("B3.held", day, changes, stocks):
            drift_warn.append(what)
        else:
            drift_fail.append(what)
            keep["b3_held"][day] = names
    for w in runner_warnings:
        m = _FINAL_ROW.match(w)
        if not m:
            continue
        what = f"log {m.group(1)} {m.group(2)}: a final row changed"
        if _explained(f"log:{m.group(1)}", _d(pd.Period(m.group(2), "M").end_time), changes, stocks):
            drift_warn.append(what)
        else:
            drift_fail.append(what + " with no vendor revision of its inputs behind it (a hand edit or a code change?)")
    if not accept:
        for k in ("series", "trades", "b3_held"):
            for name, v in keep[k].items():
                if k == "series":
                    state["numbers"]["series"].setdefault(name, {}).update(v)
                else:
                    state["numbers"][k][name] = v
    rev = sorted({f"{key} {per}" for _, key, per in changes})
    rev_item = [f"vendor revised: {', '.join(rev[:12])}{' ...' if len(rev) > 12 else ''}"] if rev else []
    if drift_fail:
        msg = (f"{len(drift_fail)} recorded number(s) changed with no vendor revision of their own inputs behind them"
               + (" (accepted: state rewritten)" if accept else " (the recorded values are kept until fixed or "
                  "accepted with --accept-replay)"))
        return Check("replay_drift", WARN if accept else FAIL, msg,
                     drift_fail + drift_warn + rev_item + notes), state
    if drift_warn:
        return Check("replay_drift", WARN, f"{len(drift_warn)} recorded number(s) changed after a vendor revision",
                     rev_item + drift_warn + notes), state
    if rev:
        return Check("replay_drift", WARN, "vendor revised history; no recorded number changed",
                     rev_item + notes), state
    # (no counts or dates in the message: a rerun of the same session must write the same health.json)
    return Check("replay_drift", OK, "recorded daily numbers and closed trades unchanged", notes), state


# ======================================================================== 8. pipeline liveness


def check_liveness(prev_health: dict | None, target, calendar: pd.DatetimeIndex, host: str | None = None) -> Check:
    """The previous completed daily run must have covered the session before ``target`` (or ``target`` itself).
    ``host`` ("actions" / "local"): a record written by a local run does not count for the scheduled job, so the
    first GitHub Actions run after a local one starts the chain instead of failing on the days between."""
    if not prev_health or not prev_health.get("last_run_session"):
        return Check("pipeline_liveness", OK, "first daily run (no earlier health record)")
    if host == "actions" and prev_health.get("host") != "actions":
        return Check("pipeline_liveness", OK, "first daily run on GitHub Actions (the earlier record came from a "
                                              "local run)")
    last = pd.Timestamp(prev_health["last_run_session"])
    missed = sessions_between(last, target, calendar)
    if pd.Timestamp(target) < last:
        return Check("pipeline_liveness", WARN, f"as-of {_d(target)} is before the last completed run "
                                                f"({_d(last)})")
    if len(missed):
        return Check("pipeline_liveness", FAIL, f"no completed daily run for {len(missed)} session(s) since "
                                                f"{_d(last)}", [_d(d) for d in missed])
    return Check("pipeline_liveness", OK, "no session missed since the previous completed run")


# ======================================================================== report


def report(target, checks: list, data_end=None, last_run_session=None, next_notes=(), sources=None,
           host: str | None = None) -> dict:
    status = worst(c.status for c in checks)
    return {"schema": SCHEMA, "session": _d(target), "status": status, "host": host or "local",
            "data_end": _d(data_end) if data_end is not None else None,
            "last_run_session": _d(last_run_session) if last_run_session is not None else None,
            "checks": [c.as_dict() for c in checks], "next_session": list(next_notes),
            "sources": dict(sorted((sources or {}).items()))}


def dumps(obj) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def load_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def summary_lines(rep: dict) -> list:
    out = [f"HEALTH {rep['status'].upper()} for session {rep['session']}"]
    for c in rep["checks"]:
        out.append(f"  [{c['status']:4}] {c['name']}: {c['message']}")
        for it in c.get("items", []):
            out.append(f"         - {it}")
    for n in rep.get("next_session", []):
        out.append(f"  next: {n}")
    return out
