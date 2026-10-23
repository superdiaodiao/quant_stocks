"""Daily total-return series for the buffett study (docs/research_ledger_buffett.md section 0.2): Yahoo v8 charts,
archive.org copies of Yahoo's old table.csv for delisted tickers, Alpaca SIP daily bars after 2016, the 13F price
check and the Yahoo / Alpaca cross-check.

Every request goes through ``quant.data.sources.http.cached_get`` (body cached before parsing, logged to the caller's
``Ledger``: ``research_cache/buffett/{raw_index.csv.gz,quota_ledger.csv}``). Keys (Alpaca) are read inside Python and
never printed. Vendor price levels stay in the cache: the study only writes returns, ratios and statistics.

A series (``Series_``) has ``tr`` (total-return level: Yahoo adjclose, the archived CSV's Adj Close, Alpaca
``adjustment=all``) and ``raw`` (unadjusted close: Yahoo's split-adjusted close times every later split ratio, the
archived CSV's Close, Alpaca ``adjustment=raw``, WIKI's close) on the source's own dates, truncated at ``end``.
Nasdaq Data Link's WIKI PRICES (2011-06 .. 2018-03, adj_close / close) is read from the frozen v1 cache's local
extract (read only, no request).

Fixed 2026-10-23 after the registered run (docs/research_ledger_buffett.md, run log): Alpaca bars with zero volume
(placeholder bars after a delisting, kept under the old ticker) are dropped; a non-Yahoo segment is cut before its first
run of ``STALE_RUN`` sessions with an unchanged total-return level (``cut_stale``), so ``MIN_ROWS`` counts real trades
and a series ends at its last real trade; distributions a source misses are added back (``apply_distributions``,
table ``berkshire_cusips.DISTRIBUTIONS``). ``offline=True`` rebuilds from the raw cache only (no request: a missing
cache file counts as no data, as a failed request did when the cache was filled).
"""
from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import numpy as np
import pandas as pd

from quant.data.guards import assert_dev_dates, truncate_dev
from quant.data.sources import alpaca, yahoo
from quant.data.sources.archive_org import capture_url, yahoo_csv_span
from quant.data.sources.http import Ledger, SlidingWindowLimiter, cached_get
from quant.paths import CACHE_ROOT

YAHOO_FIRST = "1990-01-01"
YAHOO_LIMITER = SlidingWindowLimiter({2: 1})
WAYBACK_HOSTS = ("ichart.finance.yahoo.com/table.csv", "real-chart.finance.yahoo.com/table.csv",
                 "ichart.yahoo.com/table.csv", "table.finance.yahoo.com/table.csv")
WAYBACK_LIMITER = SlidingWindowLimiter({1: 1, 60: 30})
ALPACA_LIMITER = SlidingWindowLimiter({60: 150})
ALPACA_FIRST = "2016-01-01"
MIN_ROWS = 250          # a source segment shorter than a year of sessions (e.g. one archived page) is not used
STALE_RUN = 20          # a non-Yahoo segment ends before its first run of this many unchanged total-return levels


class OfflineMiss(RuntimeError):
    """A request that ``offline=True`` would have had to send (the body is not in the cache)."""


def _get(url: str, cache_path: Path, offline: bool, **kw) -> bytes:
    """``cached_get``, or with ``offline`` the cached body only (a cached 404 still raises FileNotFoundError)."""
    cache_path = Path(cache_path)
    if offline and not cache_path.exists() and not cache_path.with_name(cache_path.name + ".404").exists():
        raise OfflineMiss(str(cache_path))
    return cached_get(url, cache_path, **kw)


def ledger(cache: Path) -> Ledger:
    return Ledger(Path(cache) / "raw_index.csv.gz", Path(cache) / "quota_ledger.csv")


@dataclass
class Series_:
    tr: pd.Series        # total-return level
    raw: pd.Series       # unadjusted close
    source: str
    symbol: str


# ======================================================================== Yahoo

def yahoo_path(raw: Path, symbol: str, first: str = YAHOO_FIRST) -> Path:
    tag = "" if first == YAHOO_FIRST else f"__{first[:4]}"
    return Path(raw) / "yahoo" / f"chart_{symbol}{tag}.json"


def fetch_yahoo(raw: Path, symbol: str, led: Ledger, period2: str, first: str = YAHOO_FIRST,
                offline: bool = False) -> bool:
    """Cache one Yahoo v8 daily chart [first, period2); False when Yahoo has no such symbol (404). ``offline``: a chart
    that is not cached raises ``OfflineMiss``."""
    try:
        _get(yahoo.chart_url(symbol, first, period2), yahoo_path(raw, symbol, first), offline, source="yahoo",
             ledger=led, headers=yahoo.HEADERS, limiter=YAHOO_LIMITER, symbol=symbol)
        return True
    except FileNotFoundError:
        return False


def yahoo_series(raw: Path, symbol: str, end: str, first: str = YAHOO_FIRST) -> Series_ | None:
    p = yahoo_path(raw, symbol, first)
    if not p.exists():
        return None
    try:
        df = yahoo.parse_chart(p, end)
    except (KeyError, IndexError, TypeError):
        return None
    if df.empty:
        return None
    assert_dev_dates(df["date"], end)
    idx = pd.DatetimeIndex(df["date"])
    close = pd.Series(df["close"].astype(float).values, index=idx)
    splits = yahoo.split_events(p, end)
    factor = pd.Series(1.0, index=idx)
    for d, ratio in splits.items():
        factor[idx < pd.Timestamp(d)] *= ratio
    tr = pd.Series(df["adjclose"].astype(float).values, index=idx)
    ok = (tr > 0) & (close > 0)
    return Series_(tr[ok], (close * factor)[ok], "yahoo", symbol)


# ======================================================================== archive.org copies of Yahoo's table.csv

def cdx_url(host: str, symbol: str) -> str:
    """CDX query for captures of ``host?s=<symbol>...`` (a URL prefix: also matches longer tickers, which
    ``wayback_captures`` drops)."""
    return ("https://web.archive.org/cdx/search/cdx?url=" + host + "%3Fs%3D" + symbol
            + "&matchType=prefix&filter=statuscode:200&fl=timestamp,original,length&limit=5000")


def wayback_captures(raw: Path, symbol: str, led: Ledger, offline: bool = False) -> pd.DataFrame:
    """Archived daily table.csv captures of ``symbol`` on the four old Yahoo hosts: timestamp, original, length,
    start, end (from the URL's a..f parameters; a capture without them covers everything up to its timestamp)."""
    rows = []
    for host in WAYBACK_HOSTS:
        cache = Path(raw) / "wayback_cdx" / f"{symbol}__{host.split('/')[0]}.txt"
        try:
            body = _get(cdx_url(host, symbol), cache, offline, source="wayback_cdx", ledger=led,
                        limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
        except Exception:  # noqa: BLE001  (an unanswered CDX query counts as no captures; it is not cached)
            continue
        for line in body.decode("latin-1").splitlines():
            parts = line.split(" ")
            if len(parts) != 3:
                continue
            url = parts[1].replace("&amp;", "&")
            if not re.search(rf"[?&;]s={re.escape(symbol)}(&|$)", url, re.I):
                continue
            g = re.search(r"[?&;]g=([a-z])", url)
            if g and g.group(1) != "d":
                continue
            st, en = yahoo_csv_span(url)
            stamp = parts[0]
            rows.append({"timestamp": stamp, "original": parts[1], "length": int(parts[2]) if parts[2].isdigit() else 0,
                         "start": st or "1900-01-01",
                         "end": min(en or "2100-01-01", f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}")})
    return pd.DataFrame(rows, columns=["timestamp", "original", "length", "start", "end"])


def wayback_frame(raw: Path, symbol: str, stamp: str, original: str, led: Ledger,
                  offline: bool = False) -> pd.DataFrame | None:
    """One archived table.csv as date, close, adj (oldest first); None when the body is not a Yahoo CSV."""
    cache = Path(raw) / "wayback" / f"{symbol}__{stamp}.csv.gz"
    try:
        body = _get(capture_url(stamp, original), cache, offline, source="wayback", ledger=led,
                    limiter=WAYBACK_LIMITER, timeout=240, symbol=symbol)
    except Exception:  # noqa: BLE001
        return None
    text = body.decode("latin-1")
    if not text.startswith("Date,"):
        return None
    df = pd.read_csv(io.StringIO(text))
    adj = [c for c in df.columns if re.match(r"adj\.?\s*close", str(c), re.I)]     # "Adj Close" / "Adj. Close*"
    if not {"Date", "Close"} <= set(df.columns) or not adj:
        return None
    out = pd.DataFrame({"date": pd.to_datetime(df["Date"], errors="coerce", format="mixed"),
                        "close": pd.to_numeric(df["Close"], errors="coerce"),
                        "adj": pd.to_numeric(df[adj[0]], errors="coerce")}).dropna()
    out = out[(out["close"] > 0) & (out["adj"] > 0)]
    return out.drop_duplicates("date").sort_values("date").reset_index(drop=True)


def best_wayback(raw: Path, symbol: str, need_a: str, need_b: str, led: Ledger, tries: int = 8,
                 offline: bool = False) -> pd.DataFrame | None:
    """The archived CSV that best covers [need_a, need_b]. Candidates: captures whose URL span starts by need_a
    (+10 days) and whose data end no later than need_b + 400 days (a later capture of a reused ticker belongs to
    another company), the ``tries`` with the widest stated coverage; each is parsed and the one whose *actual* rows
    overlap the window the most wins (a capture without span parameters may hold only a page of rows)."""
    caps = wayback_captures(raw, symbol, led, offline)
    if caps.empty:
        return None
    a, b = pd.Timestamp(need_a), pd.Timestamp(need_b)
    caps["s"], caps["e"] = pd.to_datetime(caps["start"], errors="coerce"), pd.to_datetime(caps["end"], errors="coerce")
    caps = caps.dropna(subset=["s", "e"])
    caps = caps[(caps["s"] <= a + pd.Timedelta(days=10)) & (caps["e"] <= b + pd.Timedelta(days=400))
                & (caps["e"] >= a)]
    if caps.empty:
        return None
    caps = caps.assign(cover=(caps["e"].clip(upper=b) - a).dt.days).sort_values(
        ["cover", "length", "timestamp"], ascending=[False, False, False])
    best, best_days = None, -1
    for r in caps.head(tries).itertuples():
        f = wayback_frame(raw, symbol, r.timestamp, r.original, led, offline)
        if f is None or f.empty:
            continue
        lo, hi = max(f["date"].min(), a), min(f["date"].max(), b)
        days = (hi - lo).days
        if days > best_days:
            best, best_days = f, days
    return best


def wayback_series(frame: pd.DataFrame, symbol: str, end: str) -> Series_:
    f = truncate_dev(frame.assign(date=frame["date"].dt.strftime("%Y-%m-%d")), "date", end)
    idx = pd.DatetimeIndex(f["date"])
    return Series_(pd.Series(f["adj"].astype(float).values, index=idx),
                   pd.Series(f["close"].astype(float).values, index=idx), "wayback", symbol)


# ======================================================================== Alpaca

def alpaca_url(symbol: str, start: str, end: str, adjustment: str, page_token: str | None = None) -> str:
    q = {"symbols": symbol, "timeframe": "1Day", "start": f"{start}T00:00:00Z", "end": f"{end}T23:59:59Z",
         "adjustment": adjustment, "feed": "sip", "limit": "10000"}
    if page_token:
        q["page_token"] = page_token
    return f"{alpaca.BASE}/v2/stocks/bars?{urlencode(q)}"


def alpaca_frame(raw: Path, symbol: str, start: str, end: str, adjustment: str, led: Ledger,
                 headers: dict, offline: bool = False) -> pd.DataFrame:
    """date, close of every page for [start, end] (cached page by page). Bars with zero volume are dropped: Alpaca
    keeps placeholder bars (no trade, the last price repeated) under a delisted ticker until it is reused."""
    rows, token, page = [], None, 0
    while True:
        p = Path(raw) / "alpaca" / f"{symbol}_{adjustment}_{start}_{end}_p{page}.json.gz"
        body = _get(alpaca_url(symbol, start, end, adjustment, token), p, offline, source="alpaca_daily", ledger=led,
                    headers=headers, limiter=ALPACA_LIMITER, symbol=symbol)
        j = json.loads(body)
        rows += (j.get("bars") or {}).get(symbol) or []
        token = j.get("next_page_token")
        page += 1
        if not token:
            break
    rows = [r for r in rows if float(r.get("v") or 0) > 0]
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    f = pd.DataFrame({"date": pd.to_datetime([r["t"][:10] for r in rows]), "close": [float(r["c"]) for r in rows]})
    return f.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)


def alpaca_series(raw: Path, symbol: str, end: str, led: Ledger, headers: dict, start: str = ALPACA_FIRST,
                  offline: bool = False) -> Series_ | None:
    adj = alpaca_frame(raw, symbol, start, end, "all", led, headers, offline)
    rw = alpaca_frame(raw, symbol, start, end, "raw", led, headers, offline)
    if adj.empty:
        return None
    assert_dev_dates(adj["date"].dt.strftime("%Y-%m-%d"), end)
    tr = pd.Series(adj["close"].values, index=pd.DatetimeIndex(adj["date"]))
    r = pd.Series(rw["close"].values, index=pd.DatetimeIndex(rw["date"])).reindex(tr.index)
    return Series_(tr, r, "alpaca", symbol)


# ======================================================================== Nasdaq Data Link WIKI (local, read only)

WIKI_DIR = CACHE_ROOT / "reversal_2012_2026/wiki/by_ticker"     # the frozen v1 cache's WIKI extract, 2011-06..2018-03


def wiki_series(symbol: str, end: str, wiki_dir: Path = WIKI_DIR) -> Series_ | None:
    """WIKI PRICES rows of ``symbol`` (2011-06-01 .. 2018-03-27): adj_close (splits + dividends) and close."""
    p = Path(wiki_dir) / f"{symbol}.csv.gz"
    if not p.exists():
        return None
    df = pd.read_csv(p, dtype={"ticker": str, "date": str}, usecols=["date", "close", "adj_close"])
    df = truncate_dev(df.dropna(), "date", end)
    df = df[(df["close"] > 0) & (df["adj_close"] > 0)]
    idx = pd.DatetimeIndex(df["date"])
    return Series_(pd.Series(df["adj_close"].astype(float).values, index=idx),
                   pd.Series(df["close"].astype(float).values, index=idx), "wiki", symbol)


# ======================================================================== combining, checks

def cut_stale(s: Series_, run: int = STALE_RUN) -> Series_:
    """``s`` up to its last real move before the first run of ``run`` sessions whose total-return level does not
    change (a source carrying a stale price forward); unchanged when there is no such run."""
    r = s.tr.pct_change().to_numpy()
    flat = np.r_[False, r[1:] == 0]
    k = 0
    for i, f in enumerate(flat):
        k = k + 1 if f else 0
        if k == run:
            keep = i - run + 1           # rows [0, keep) end with the last printed price before the run
            return Series_(s.tr.iloc[:keep], s.raw.reindex(s.tr.index).iloc[:keep], s.source, s.symbol)
    return s


def apply_distributions(s: Series_, events: list, price_of) -> Series_:
    """Add the value of distributed shares to the ex-date's total return: (close + ratio x price of the distributed
    share) / previous close - 1 on unadjusted closes. ``events``: (ex-date, ratio, price source or None, note) with
    None pricing the distributed share at the parent's own close; ``price_of(source, date)`` -> unadjusted close.
    The level is re-chained from the first row; dates outside the series are skipped."""
    tr = s.tr.copy()
    raw = s.raw.reindex(tr.index)
    ret = tr.pct_change()
    hit = False
    for d, ratio, src, _note in events:
        t = pd.Timestamp(d)
        if t not in tr.index or tr.index.get_loc(t) == 0:
            continue
        p0, p1 = float(raw.iloc[tr.index.get_loc(t) - 1]), float(raw.loc[t])
        q = p1 if src is None else float(price_of(src, t))
        if not (np.isfinite(p0) and np.isfinite(p1) and np.isfinite(q) and p0 > 0):
            raise ValueError(f"{s.symbol} {d}: no unadjusted close for the distribution")
        ret.loc[t] = (p1 + ratio * q) / p0 - 1
        hit = True
    if not hit:
        return s
    return Series_(tr.iloc[0] * (1 + ret.fillna(0.0)).cumprod(), s.raw, s.source, s.symbol)


def splice(parts: list[Series_]) -> Series_:
    """Chain total-return segments (each later segment starts after the previous one's last date, linked at its
    first day by that day's own return from the earlier segment's last level)."""
    tr = parts[0].tr.copy()
    raw = parts[0].raw.copy()
    for p in parts[1:]:
        later = p.tr[p.tr.index > tr.index[-1]]
        if later.empty:
            continue
        ret = p.tr.pct_change().reindex(later.index)
        first = later.index[0]
        prior = p.tr[p.tr.index <= tr.index[-1]]
        ret.iloc[0] = later.iloc[0] / prior.iloc[-1] - 1 if len(prior) else 0.0
        lvl = tr.iloc[-1] * (1 + ret.fillna(0.0)).cumprod()
        tr = pd.concat([tr, lvl])
        raw = pd.concat([raw, p.raw.reindex(later.index)])
        assert first > parts[0].tr.index[-1]
    return Series_(tr, raw, "+".join(x.source for x in parts), "+".join(x.symbol for x in parts))


def implied_price(value: float, shares: float, filing_date: str) -> float:
    """13F value per share in dollars: values are in thousands of dollars in filings before 2023 (the SEC changed
    the unit to dollars for filings from 2023-01-03)."""
    if not shares or shares <= 0:
        return np.nan
    return value * (1000.0 if filing_date < "2023-01-03" else 1.0) / shares


def price_check(checks: pd.DataFrame, series: dict, lo: float = 0.80, hi: float = 1.25) -> pd.DataFrame:
    """Per (cusip, report_date): the 13F-implied price over the source's unadjusted close on the report date (or
    the last close within 5 calendar days before it). ``checks``: cusip, report_date, filing_date, value, shares."""
    rows = []
    for r in checks.itertuples():
        s = series.get(r.cusip)
        imp = implied_price(r.value, r.shares, r.filing_date)
        px = np.nan
        if s is not None:
            d = pd.Timestamp(r.report_date)
            w = s.raw[(s.raw.index <= d) & (s.raw.index >= d - pd.Timedelta(days=5))].dropna()
            px = float(w.iloc[-1]) if len(w) else np.nan
        ratio = imp / px if np.isfinite(px) and px > 0 else np.nan
        rows.append({"cusip": r.cusip, "report_date": r.report_date, "ratio": ratio,
                     "ok": bool(np.isfinite(ratio) and lo <= ratio <= hi), "has_price": bool(np.isfinite(px))})
    return pd.DataFrame(rows)


def crosscheck(a: pd.Series, b: pd.Series, days: pd.DatetimeIndex) -> dict:
    """Yahoo vs Alpaca daily total returns on ``days`` (both must have the day and the day before)."""
    ra, rb = a.pct_change(), b.pct_change()
    j = pd.concat([ra, rb], axis=1, keys=["a", "b"]).reindex(days).dropna()
    if len(j) < 20:
        return {"sessions": int(len(j))}
    ca = float(np.prod(1 + j["a"]) ** (252 / len(j)) - 1)
    cb = float(np.prod(1 + j["b"]) ** (252 / len(j)) - 1)
    return {"sessions": int(len(j)), "cagr_yahoo": ca, "cagr_alpaca": cb, "diff_pp": (ca - cb) * 100,
            "corr": float(j["a"].corr(j["b"])), "max_abs_daily_diff": float((j["a"] - j["b"]).abs().max())}


# ======================================================================== the B1 price universe

def need_windows(checks: pd.DataFrame, end: str, before: int = 10, after: int = 200) -> pd.DataFrame:
    """Per CUSIP the dates its series must cover: from ``before`` days before its first filing to ``after`` days after
    its last (the position is held until the next state), capped at ``end``."""
    w = checks.groupby("cusip").agg(a=("filing_date", "min"), b=("filing_date", "max"))
    w["need_a"] = (pd.to_datetime(w["a"]) - pd.Timedelta(days=before)).dt.strftime("%Y-%m-%d")
    w["need_b"] = (pd.to_datetime(w["b"]) + pd.Timedelta(days=after)).dt.strftime("%Y-%m-%d").clip(upper=end)
    return w[["need_a", "need_b"]]


def build_series(specs: dict, windows: pd.DataFrame, raw: Path, led: Ledger, end: str, period2: str,
                 alpaca_headers, log=print, distributions: dict | None = None,
                 offline: bool = False) -> tuple[dict, pd.DataFrame]:
    """cusip -> Series_ for every CUSIP with a spec (list of (source, symbol, first, last) segments), and a status
    table. ``alpaca_headers``: a zero-argument callable returning the Alpaca auth headers (read only when needed).
    Non-Yahoo segments end before a stale run (``cut_stale``); ``distributions`` (cusip -> events, see
    ``apply_distributions``) are applied to the finished series; ``offline``: no request (see the module note)."""
    out, rows = {}, []
    hdr = None

    def price_of(src, t):
        kind, sym = src
        if kind != "yahoo":
            raise ValueError(kind)
        if not fetch_yahoo(raw, sym, led, period2, offline=offline):
            raise FileNotFoundError(sym)
        y = yahoo_series(raw, sym, end)
        return y.raw.get(pd.Timestamp(t), np.nan)

    for c, spec in specs.items():
        need_a, need_b = windows.at[c, "need_a"], windows.at[c, "need_b"]
        if spec is None:
            rows.append({"cusip": c, "spec": "", "status": "unmapped", "need_a": need_a, "need_b": need_b})
            continue
        parts = []
        for src, sym, f0, f1 in spec:
            a = max(need_a, f0) if f0 else need_a
            b = min(need_b, f1) if f1 else need_b
            if a > b:
                continue
            s = None
            if src == "yahoo":
                if fetch_yahoo(raw, sym, led, period2, offline=offline):
                    s = yahoo_series(raw, sym, end)
            elif src == "wayback":
                fr = best_wayback(raw, sym, a, b, led, offline=offline)
                s = wayback_series(fr, sym, end) if fr is not None else None
            elif src == "alpaca":
                hdr = hdr or ({} if offline else alpaca_headers())
                s = alpaca_series(raw, sym, end, led, hdr, offline=offline)
            elif src == "wiki":
                s = wiki_series(sym, end)
            else:
                raise ValueError(src)
            if s is not None:
                keep = (s.tr.index >= pd.Timestamp(f0 or "1900-01-01")) & (s.tr.index <= pd.Timestamp(f1 or end))
                s = Series_(s.tr[keep], s.raw.reindex(s.tr.index)[keep], s.source, s.symbol)
                if src != "yahoo":
                    s = cut_stale(s)
            if s is not None and len(s.tr) >= MIN_ROWS:
                parts.append(s)
        spec_txt = ";".join(f"{x[0]}:{x[1]}" + (f"[{x[2] or ''}..{x[3] or ''}]" if (x[2] or x[3]) else "")
                            for x in spec)
        if parts:
            s = splice(parts) if len(parts) > 1 else parts[0]
            if distributions and c in distributions:
                s = apply_distributions(s, distributions[c], price_of)
            out[c] = s
            rows.append({"cusip": c, "spec": spec_txt, "status": "series", "source": s.source, "symbol": s.symbol,
                         "first": str(s.tr.index[0].date()), "last": str(s.tr.index[-1].date()),
                         "need_a": need_a, "need_b": need_b})
        else:
            rows.append({"cusip": c, "spec": spec_txt, "status": "no_data", "need_a": need_a, "need_b": need_b})
        log(f"  {c} {rows[-1]['status']} {spec_txt}")
    return out, pd.DataFrame(rows)


def longest_flat_run(level: pd.Series) -> int:
    """The longest run of consecutive sessions whose level does not change (0 for fewer than two rows)."""
    r = level.dropna().pct_change().to_numpy()[1:]
    best = k = 0
    for x in r:
        k = k + 1 if x == 0 else 0
        best = max(best, k)
    return int(best)


def accept(pc: pd.DataFrame, max_fail: float = 0.20) -> pd.DataFrame:
    """Per CUSIP: report dates checked (the source has a close within 5 days), share that failed, accepted (at least
    one check and a failed share <= ``max_fail``), and the median ratio."""
    g = pc[pc["has_price"]].groupby("cusip")
    out = pd.DataFrame({"checked": g.size(), "failed_share": 1 - g["ok"].mean(), "median_ratio": g["ratio"].median()})
    out["accepted"] = (out["checked"] >= 1) & (out["failed_share"] <= max_fail)
    return out


def matrices(series: dict, cusips, sessions: pd.DatetimeIndex) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    """(total-return levels, unadjusted closes, last row date) on ``sessions`` for ``cusips``: NaN before a series'
    first date, carried forward inside it and flat after its last date (the engine turns an ended series into cash
    at that level)."""
    lv, px, last = {}, {}, {}
    for c in cusips:
        s = series[c]
        tr = s.tr[~s.tr.index.duplicated(keep="last")]
        rw = s.raw[~s.raw.index.duplicated(keep="last")].reindex(tr.index)
        rw = rw.fillna(tr)              # a missing unadjusted close falls back to the adjusted level (costs only)
        u = sessions.union(tr.index)
        lv[c] = tr.reindex(u).ffill().reindex(sessions)
        px[c] = rw.reindex(u).ffill().reindex(sessions)
        last[c] = tr.index[-1]
    return pd.DataFrame(lv, index=sessions), pd.DataFrame(px, index=sessions), pd.Series(last)
