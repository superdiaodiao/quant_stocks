"""Data version 2 of the reversal 2012-2026 data: the new fill sources (owner decision 2026-10-05, plan section 0).

Data only: no signal, strategy return or cross-stock return aggregate is computed. Version 1 (``INPUTS`` and the v1
cache) is only read. Every raw body and every parsed series stays local, under
``research_cache/reversal_2012_2026_v2_fill/`` (``FILL``); it has its own request ledger (``raw_index.csv.gz``,
``quota_ledger.csv``), so the v1 ledgers are never appended to.

Sources (docs/data_sources_survey.md section 5):
- archive.org Wayback captures of Yahoo's old ``table.csv`` files (hosts ichart.finance.yahoo.com,
  real-chart.finance.yahoo.com, ichart.yahoo.com) and of its history pages (``finance.yahoo.com/q/hp?s=T``,
  2004-2015, about 66 sessions a capture; ``finance.yahoo.com/quote/T/history``, 2016 on, about a year).
  The table.csv and q/hp captures are found through the CDX index of the whole URL prefix (a few dozen index
  pages instead of one query per ticker); quote/T/history captures through one CDX query per ticker.
- companiesmarketcap.com daily market cap (``cmc``), as in the probe: a delisted name takes only a
  ``T.defunct.<year>`` entry within a year of the delisting, or a guessed slug whose title carries ``(T)``.
- QuantQuote's free S&P 500 daily pack (archive.org copy, 1998-01..2013-08-09), read from the probe's zip.

Which securities (``targets``): every v1 security with a missing listed name-week of positive expected top-250 weight
or of unknown size (``CACHE/universe/weekly_listed.csv.gz``), the 50 ``awaiting_d5`` terminal rows (with the OTC
tickers T+Q and T+F after the delisting), the securities with unresolved two-source days in the v1 panel, and
named fixes (CALD's last sessions). Need dates: the missing weeks, each from 75 calendar days before (the dv50
warm-up) to 28 days after (a 4-week hold); the ticker is the one in use that week (``weekly_listed.ticker``).
Order: by the security's summed expected top-250 weight, largest first (D5 and fix names first of all).

Capture choice per (security, ticker, need span): greedy interval cover. A table.csv capture covers its URL's
a..f range (daily files only; ``g=v`` dividend files are kept as events); a q/hp capture covers the 95 calendar
days up to its timestamp (the page's latest 66 sessions); a quote/history capture the 365 days up to it.

Rate: archive.org at most 15 requests a minute (it refuses connections at about 30), ``WORKERS`` requests in flight
(archive.org answers a page in seconds to a minute); companiesmarketcap one every 3 s. A run is resumable: every
body is cached before it is parsed (also reused from the probe's and the mega-cap OOS2 caches), a 404 is cached
as a marker, ``FILL/STOP`` stops the loop cleanly, and ``FILL/progress.json`` says where it is.

Steps::

    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py targets    # the target list (offline)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py index      # bulk CDX pages (~55 requests)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py fetch      # the long run (resumable)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py parse      # bodies -> series (offline)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py cmc        # companiesmarketcap (gap names)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py quantquote # the QuantQuote pack (offline)
    PYTHONPATH=. .venv/bin/python scripts/reversal_data_v2_archive.py status     # counts only
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import re
import sys
import threading
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import reversal_data_common as common  # noqa: E402
from scripts import data_source_probe as probe  # noqa: E402  (its parsers; importing it moves the ledger, reset below)

MAIN = common.MAIN_CHECKOUT
V1_CACHE = MAIN / "research_cache" / "reversal_2012_2026"
V1_INPUTS = ROOT / "output" / "research_only" / "reversal_2012_2026" / "inputs"
FILL = MAIN / "research_cache" / "reversal_2012_2026_v2_fill"
RAW = FILL / "raw"
SERIES = FILL / "series"
PROBE_RAW = MAIN / "research_cache" / "data_source_probe" / "raw"
OOS2_RAW = MAIN / "research_cache" / "megacap_oos2" / "raw"


def use_own_ledger() -> None:
    """Point the shared request ledger at this cache (the probe's import points it at the probe's cache; v1's is
    never touched). Called before every request, since another module may move the shared ledger."""
    common.RAW_INDEX = FILL / "raw_index.csv.gz"
    common.QUOTA_LEDGER = FILL / "quota_ledger.csv"


WAYBACK_PER_MINUTE = 15
WAYBACK_LIMITER = common.SlidingWindowLimiter({2: 1, 60: WAYBACK_PER_MINUTE})
CMC_LIMITER = common.SlidingWindowLimiter({3: 1})
WORKERS = 6
CSV_HOSTS = ("ichart.finance.yahoo.com/table.csv", "real-chart.finance.yahoo.com/table.csv", "ichart.yahoo.com/table.csv")
HP_PREFIX = "finance.yahoo.com/q/hp"
HP_DAYS = 95      # a q/hp capture shows the latest ~66 sessions
QH_DAYS = 365     # a quote/T/history capture shows about a year
WARMUP_DAYS = 75  # dv50 warm-up before a missing week
HOLD_DAYS = 28    # a 4-week hold after it
MIN_GAIN_SESSIONS = 5   # a capture is fetched only when it adds this many uncovered need sessions
POST_DELIST_MAX = 5     # a capture with more than this many sessions after the delisting is dropped (survey rule)
D5_OTC_DAYS = 30        # OTC tickers of D5 names: rows up to 30 days after the delisting
FIX_TARGETS = {  # named v2 fixes that need archive rows (docs/audit_data_v1_vs_quantconnect.md 2.6 #5)
    "1035748": ("CALD", "2018-03-01", "2018-04-10", "fix_cald_last_sessions"),
}
# distributed securities of the v2 fixes with no local close and no Yahoo chart left (inputs_v2/v2_fixes.csv):
# their archived history pages around the distribution, under a pseudo id ``child_{ticker}``
CHILD_TARGETS = {"QRTEP": ("2020-09-10", "2020-10-31"), "GLIBP": ("2018-03-08", "2018-04-30"),
                 "ENVXW": ("2025-07-17", "2025-08-29")}


def log(message: str) -> None:
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {message}", flush=True)


# ======================================================================== pure helpers (tested)


def csv_url_facts(original: str) -> dict:
    """Ticker, kind (daily / dividends / other) and a..f span of an old Yahoo table.csv URL."""
    url = original.replace("&amp;", "&")
    q = dict(re.findall(r"[?&;]([a-zA-Z])=([^&]*)", url))
    g = q.get("g", "d").lower()
    kind = {"d": "daily", "v": "dividends"}.get(g, "other")
    start, end = probe.yahoo_csv_span(url)
    return {"ticker": q.get("s", "").upper(), "kind": kind, "start": start, "end": end}


def hp_url_facts(original: str) -> dict:
    """Ticker and whether a q/hp URL is the default (latest) view; custom a..f views give their range, paged
    views (``y`` > 0) are skipped."""
    url = original.replace("&amp;", "&")
    q = dict(re.findall(r"[?&;]([a-zA-Z])=([^&]*)", url))
    tick = re.sub(r"[^A-Z0-9.\-]", "", q.get("s", "").split("+")[0].upper())
    if q.get("y", "0") not in ("", "0"):
        return {"ticker": tick, "usable": False}
    start, end = probe.yahoo_csv_span(url)
    g = q.get("g", "d").lower()
    return {"ticker": tick, "usable": g in ("d", ""), "start": start, "end": end}


def capture_span(kind: str, stamp: str, start: str | None = None, end: str | None = None) -> tuple[pd.Timestamp, pd.Timestamp]:
    """The dates a capture can hold (calendar span)."""
    taken = pd.Timestamp(stamp[:8])
    if kind == "csv":
        hi = min(pd.Timestamp(end), taken) if end else taken
        lo = pd.Timestamp(start) if start else pd.Timestamp("1990-01-01")
        return lo, hi
    days = HP_DAYS if kind == "hp" else QH_DAYS
    hi = min(pd.Timestamp(end), taken) if end else taken
    lo = max(pd.Timestamp(start), hi - pd.Timedelta(days=days)) if start else hi - pd.Timedelta(days=days)
    return lo, hi


def choose_captures(need: pd.DatetimeIndex, captures: list[tuple], min_gain: int = MIN_GAIN_SESSIONS) -> list[tuple]:
    """Greedy cover of the ``need`` sessions: repeatedly the capture (tuples whose items 3 and 4 are its span) that
    adds the most uncovered sessions, while it adds at least ``min_gain``; ties go to the later capture."""
    left = set(pd.DatetimeIndex(need))
    chosen = []
    pool = list(captures)
    while left and pool:
        best, gain = None, 0
        for c in pool:
            lo, hi = c[3], c[4]
            g = sum(1 for d in left if lo <= d <= hi)
            if g > gain or (g == gain and g and best is not None and c[0] > best[0]):
                best, gain = c, g
        if best is None or gain < min_gain:
            break
        chosen.append(best)
        pool.remove(best)
        left = {d for d in left if not (best[3] <= d <= best[4])}
    return chosen


def split_ratio(text: str) -> float | None:
    """New shares per old share from a Yahoo split text ('2: 1 Stock Split', 'SPLIT:2:1', '1:16 Stock Splits',
    'SPLIT:3/2')."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[:/]\s*(\d+(?:\.\d+)?)", str(text))
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return a / b if a > 0 and b > 0 else None


def dividend_amount(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)", str(text).replace(",", ""))
    return float(m.group(1)) if m and re.search(r"(?i)div", str(text)) else None


def raw_from_capture(rows: pd.DataFrame, events: list[tuple]) -> pd.DataFrame:
    """The canonical record of one capture: raw close, raw volume, S and D per row.

    A capture's Close is either as traded (the old q/hp pages and table.csv files) or split-adjusted as of the
    capture (quote/T/history). Which one is decided at each in-window split by the capture's own Adj Close: the
    reading whose return on the ex-date is nearer the Adj Close return wins (``mode`` raw / adjusted / none when
    the window holds no split). Raw = Close x the product of the split ratios after the row (adjusted mode),
    volume / that product, dividends x that product."""
    df = rows.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    splits = {}
    divs = {}
    for day, text in events:
        day = pd.Timestamp(day).normalize()
        r = split_ratio(text) if re.search(r"(?i)split", str(text)) else None
        if r and abs(r - 1) > 1e-9:
            splits[day] = splits.get(day, 1.0) * r
        a = dividend_amount(text)
        if a is not None and not re.search(r"(?i)split", str(text)):
            divs[day] = divs.get(day, 0.0) + a
    lo, hi = (df["date"].min(), df["date"].max()) if len(df) else (None, None)
    in_window = {d: r for d, r in splits.items() if lo is not None and lo < d <= hi}
    votes = []
    pos = {d: i for i, d in enumerate(df["date"])}
    for d, r in in_window.items():
        i = pos.get(d)
        if i is None or i == 0:
            continue
        c0, c1 = df.loc[i - 1, "close"], df.loc[i, "close"]
        a0, a1 = df.loc[i - 1, "adj"], df.loc[i, "adj"]
        if not (c0 > 0 and c1 > 0 and a0 > 0 and a1 > 0):
            continue
        truth = a1 / a0 - 1
        votes.append(abs(c1 * r / c0 - 1 - truth) < abs(c1 / c0 - 1 - truth))
    mode = "none" if not votes else ("raw" if sum(votes) * 2 >= len(votes) else "adjusted")
    factor = np.ones(len(df))
    if mode == "adjusted":
        for d, r in in_window.items():
            factor[df["date"].values < np.datetime64(d)] *= r
    out = df.copy()
    out["close_raw"] = df["close"].values * factor
    vol = df["volume"] if "volume" in df else pd.Series(np.nan, index=df.index)
    out["volume_raw"] = vol.astype(float).values / factor
    out["split"] = [splits.get(d, 1.0) for d in df["date"]]
    out["div"] = [divs.get(d, 0.0) * (factor[i] if mode == "adjusted" else 1.0) for i, d in enumerate(df["date"])]
    out["mode"] = mode
    return out


def post_delist_count(dates: pd.Series, delist: pd.Timestamp | None, sessions: pd.DatetimeIndex) -> int:
    """Sessions after the delisting date that a capture shows."""
    if delist is None or pd.isna(delist):
        return 0
    d = pd.DatetimeIndex(pd.to_datetime(dates)).normalize()
    return int(((d > delist) & d.isin(sessions)).sum())


# ======================================================================== parsing with volume


def parse_hp(text: str) -> tuple[pd.DataFrame, list]:
    rows = re.findall(r'<td class="yfnc_tabledata1" nowrap align="right">([A-Z][a-z]{2} \d{1,2}, \d{4})</td>'
                      r'((?:<td class="yfnc_tabledata1" align="right">[^<]*</td>){6})', text)
    out = []
    for d, cells in rows:
        v = re.findall(r">([^<]*)</td>", cells)
        try:
            out.append({"date": pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y")),
                        "close": float(v[3].replace(",", "")), "volume": float(v[4].replace(",", "") or "nan"),
                        "adj": float(v[5].replace(",", ""))})
        except ValueError:
            continue
    _, events = probe.parse_yahoo_hp(text)
    return pd.DataFrame(out, columns=["date", "close", "volume", "adj"]), events


def parse_qh_store(text: str) -> tuple[pd.DataFrame, list]:
    key = '"HistoricalPriceStore":'
    i = text.find(key)
    if i < 0:
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[i + len(key):])
    except ValueError:
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    rows, events = [], []
    for x in obj.get("prices", []):
        day = pd.Timestamp(dt.datetime.fromtimestamp(int(x["date"]), dt.timezone.utc).date())
        if "type" in x:
            t = str(x["type"]).upper()
            if t == "SPLIT":
                events.append((day, f"SPLIT:{x.get('numerator', '')}:{x.get('denominator', '')}"
                               if x.get("numerator") else f"SPLIT:{x.get('splitRatio', '')}"))
            elif t == "DIVIDEND":
                events.append((day, f"DIVIDEND:{x.get('amount', x.get('data', ''))}"))
        elif x.get("close") is not None and x.get("adjclose") is not None:
            rows.append({"date": day, "close": float(x["close"]), "volume": float(x.get("volume") or np.nan),
                         "adj": float(x["adjclose"])})
    return pd.DataFrame(rows, columns=["date", "close", "volume", "adj"]), events


def parse_qh_table(text: str) -> tuple[pd.DataFrame, list]:
    cell = r"<td[^>]*>\s*(?:<span[^>]*>)?\s*([^<]*?)\s*(?:</span>)?\s*</td>\s*"
    rows = re.findall(r"<tr[^>]*>\s*" + cell.replace("([^<]*?)", r"([A-Z][a-z]{2} \d{1,2}, \d{4})") + "(" + cell * 6 + ")",
                      text)
    out = []
    for d, cells, *_ in rows:
        v = [x for x in re.findall(r">\s*([^<>]*?)\s*<", cells) if x.strip()]
        try:
            day = pd.Timestamp(dt.datetime.strptime(d, "%b %d, %Y"))
            vol = v[5].replace(",", "") if len(v) > 5 else ""
            out.append({"date": day, "close": float(v[3].replace(",", "")), "adj": float(v[4].replace(",", "")),
                        "volume": float(vol) if re.fullmatch(r"[\d.]+", vol) else np.nan})
        except (ValueError, IndexError):
            continue
    _, events = probe.parse_yahoo_table(text)
    return pd.DataFrame(out, columns=["date", "close", "volume", "adj"]), events


def parse_csv(text: str) -> pd.DataFrame:
    from io import StringIO
    df = pd.read_csv(StringIO(text))
    return pd.DataFrame({"date": pd.to_datetime(df["Date"]), "close": df["Close"].astype(float),
                         "volume": df["Volume"].astype(float) if "Volume" in df else np.nan,
                         "adj": df["Adj Close"].astype(float)})


def parse_body(kind: str, text: str) -> tuple[pd.DataFrame, list]:
    if kind == "csv":
        if not text.startswith("Date,"):
            return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
        try:
            return parse_csv(text), []
        except Exception:  # noqa: BLE001
            return pd.DataFrame(columns=["date", "close", "volume", "adj"]), []
    if kind == "div":
        events = []
        for line in text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 2 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[0].strip()):
                try:
                    events.append((pd.Timestamp(parts[0].strip()), f"DIVIDEND:{float(parts[1])}"))
                except ValueError:
                    pass
        return pd.DataFrame(columns=["date", "close", "volume", "adj"]), events
    if kind == "hp":
        return parse_hp(text)
    df, ev = parse_qh_store(text)
    if df.empty:
        df, ev = parse_qh_table(text)
    return df, ev


# ======================================================================== targets


def v1_sessions() -> pd.DatetimeIndex:
    return probe.v1_sessions()


def build_targets() -> pd.DataFrame:
    """One row per (security, ticker, need span); the per-security priority is the summed expected top-250 weight
    of its missing weeks (unknown-size weeks count 1)."""
    w = pd.read_csv(V1_CACHE / "universe/weekly_listed.csv.gz",
                    usecols=["week_end", "security_id", "ticker", "missing", "evidence", "p_top250"],
                    dtype={"security_id": str, "ticker": str})
    m = w[w["missing"] == True].copy()  # noqa: E712
    m["weight"] = np.where(m["evidence"].eq("unknown"), 1.0, m["p_top250"].astype(float).fillna(0.0))
    m = m[(m["weight"] >= 0.001)]
    m["week_end"] = pd.to_datetime(m["week_end"])
    rows = []
    prio = m.groupby("security_id")["weight"].sum()
    for (sid, tk), g in m.groupby(["security_id", "ticker"]):
        days = sorted(g["week_end"])
        # merge each week's [week_end - 75d, week_end + 28d] into spans
        spans = []
        for d in days:
            a, b = d - pd.Timedelta(days=WARMUP_DAYS), d + pd.Timedelta(days=HOLD_DAYS)
            if spans and a <= spans[-1][1] + pd.Timedelta(days=7):
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        for a, b in spans:
            rows.append({"security_id": sid, "ticker": tk, "category": "missing_weeks", "need_start": a.date(),
                         "need_end": b.date(), "priority": float(prio[sid]), "n_weeks": len(g)})
    # D5 names: the last Nasdaq weeks and the OTC tickers after the delisting
    t = pd.read_csv(V1_INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    d5 = t[t["status"] == "awaiting_d5"]
    top = max(prio.max() if len(prio) else 1.0, 1.0) + 1000
    for r in d5.itertuples():
        dd = pd.Timestamp(r.delist_date)
        last = pd.Timestamp(r.last_price_date) if isinstance(r.last_price_date, str) and r.last_price_date else \
            dd - pd.Timedelta(days=90)
        for tk, a, b, cat in ((r.ticker, last - pd.Timedelta(days=WARMUP_DAYS), dd + pd.Timedelta(days=D5_OTC_DAYS), "d5_nasdaq"),
                              (r.ticker + "Q", dd - pd.Timedelta(days=10), dd + pd.Timedelta(days=D5_OTC_DAYS), "d5_otc"),
                              (r.ticker + "F", dd - pd.Timedelta(days=10), dd + pd.Timedelta(days=D5_OTC_DAYS), "d5_otc")):
            rows.append({"security_id": r.security_id, "ticker": tk, "category": cat, "need_start": a.date(),
                         "need_end": b.date(), "priority": top, "n_weeks": 0})
    for sid, (tk, a, b, cat) in FIX_TARGETS.items():
        rows.append({"security_id": sid, "ticker": tk, "category": cat, "need_start": a, "need_end": b,
                     "priority": top, "n_weeks": 0})
    for tk, (a, b) in CHILD_TARGETS.items():
        rows.append({"security_id": f"child_{tk}", "ticker": tk, "category": "fix_child", "need_start": a,
                     "need_end": b, "priority": top, "n_weeks": 0})
    # unresolved two-source days of the v1 panel: a third vote
    p = pd.read_csv(V1_CACHE / "prices/daily_panel.csv.gz", usecols=["security_id", "date", "flags"], dtype=str)
    u = p[p["flags"].fillna("").str.contains("disagree_unresolved")].copy()
    u["date"] = pd.to_datetime(u["date"])
    tick = w[["week_end", "security_id", "ticker"]].copy()
    tick["week_end"] = pd.to_datetime(tick["week_end"])
    tick = tick.sort_values("week_end")
    u = pd.merge_asof(u.sort_values("date"), tick.rename(columns={"week_end": "date"}), on="date", by="security_id",
                      direction="forward")
    u = u.dropna(subset=["ticker"])
    cnt = u.groupby("security_id").size()
    for (sid, tk), g in u.groupby(["security_id", "ticker"]):
        days = sorted(g["date"])
        spans = []
        for d in days:
            a, b = d - pd.Timedelta(days=3), d + pd.Timedelta(days=1)
            if spans and a <= spans[-1][1] + pd.Timedelta(days=30):
                spans[-1][1] = max(spans[-1][1], b)
            else:
                spans.append([a, b])
        for a, b in spans:
            rows.append({"security_id": sid, "ticker": tk, "category": "unresolved_days", "need_start": a.date(),
                         "need_end": b.date(), "priority": float(cnt[sid]) / 50.0, "n_weeks": 0})
    out = pd.DataFrame(rows)
    out["ticker"] = out["ticker"].astype(str).str.upper()
    out = out.sort_values(["priority", "security_id", "need_start"], ascending=[False, True, True])
    FILL.mkdir(parents=True, exist_ok=True)
    out.to_csv(FILL / "targets.csv", index=False)
    log(f"targets: {len(out)} spans, {out['security_id'].nunique()} securities, {out['ticker'].nunique()} tickers; "
        f"by category {out.groupby('category').size().to_dict()}")
    return out


def targets() -> pd.DataFrame:
    return pd.read_csv(FILL / "targets.csv", dtype=str)


# ======================================================================== CDX index


def _cdx_get(url: str, cache: Path, symbol: str) -> str | None:
    use_own_ledger()
    try:
        return common.cached_get(url, cache, source="wayback_cdx", limiter=WAYBACK_LIMITER, timeout=600,
                                 symbol=symbol).decode("utf-8", "replace")
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001
        log(f"  cdx {symbol}: {type(exc).__name__}: {exc}")
        return None


def bulk_index() -> None:
    """Every capture under the table.csv hosts and q/hp, from the CDX index pages (cached)."""
    for prefix in CSV_HOSTS + (HP_PREFIX,):
        name = prefix.replace("/", "_")
        base = f"https://web.archive.org/cdx/search/cdx?url={prefix}&matchType=prefix&filter=statuscode:200"
        n_text = _cdx_get(base + "&showNumPages=true", RAW / "cdx_bulk" / f"{name}__pages.txt", name)
        base += "&fl=timestamp,original,length"
        if not n_text or not n_text.strip().isdigit():
            log(f"{prefix}: page count unknown ({n_text!r})")
            continue
        n = int(n_text.strip())
        for page in range(n):
            if (FILL / "STOP").exists():
                return
            body = _cdx_get(base + f"&page={page}", RAW / "cdx_bulk" / f"{name}__p{page:04d}.txt", name)
            log(f"{prefix} page {page + 1}/{n}: {'ok' if body is not None else 'failed'}")


def bulk_captures() -> pd.DataFrame:
    """The bulk index as a table: kind (csv / div / hp), ticker, stamp, original, span."""
    path = FILL / "bulk_captures.csv.gz"
    files = sorted((RAW / "cdx_bulk").glob("*__p*.txt"))
    sig = str(len(files)) + ":" + str(sum(f.stat().st_size for f in files))
    if path.exists() and (FILL / "bulk_captures.sig").exists() and (FILL / "bulk_captures.sig").read_text() == sig:
        return pd.read_csv(path, dtype=str)
    rows = []
    for f in files:
        is_hp = f.name.startswith(HP_PREFIX.replace("/", "_"))
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split(" ")
            if len(parts) != 3:
                continue
            ts, orig, ln = parts
            if is_hp:
                facts = hp_url_facts(orig)
                if not facts.get("usable") or not facts["ticker"]:
                    continue
                if not ln.isdigit() or int(ln) < 5000:
                    continue
                rows.append(("hp", facts["ticker"], ts, orig, facts.get("start") or "", facts.get("end") or ""))
            else:
                facts = csv_url_facts(orig)
                if not facts["ticker"] or facts["kind"] == "other":
                    continue
                if facts["kind"] == "daily" and (not ln.isdigit() or int(ln) < 1500):
                    continue
                rows.append(("csv" if facts["kind"] == "daily" else "div", facts["ticker"], ts, orig,
                             facts.get("start") or "", facts.get("end") or ""))
    out = pd.DataFrame(rows, columns=["kind", "ticker", "stamp", "original", "start", "end"]).drop_duplicates(
        ["kind", "ticker", "stamp"])
    out.to_csv(path, index=False)
    (FILL / "bulk_captures.sig").write_text(sig)
    return out


def qh_captures(ticker: str) -> list[tuple[str, str]] | None:
    url = (f"https://web.archive.org/cdx/search/cdx?url=finance.yahoo.com/quote/{ticker}/history&matchType=prefix"
           "&filter=statuscode:200&fl=timestamp,original,length&limit=5000")
    probe_copy = PROBE_RAW / "cdx" / f"qh_{ticker}.txt"  # the survey pilot asked the same query (limit 5000)
    if probe_copy.exists() and not (RAW / "cdx_qh" / f"{ticker}.txt").exists():
        body = probe_copy.read_text(encoding="utf-8", errors="replace")
    else:
        body = _cdx_get(url, RAW / "cdx_qh" / f"{ticker}.txt", ticker)
    if body is None:
        return None
    out = []
    for line in body.splitlines():
        parts = line.split(" ")
        if len(parts) == 3 and parts[2].isdigit() and int(parts[2]) > 5000:
            if re.search(rf"/quote/{re.escape(ticker)}/history", parts[1], re.I):
                out.append((parts[0], parts[1]))
    return out


# ======================================================================== bodies


def body_path(kind: str, ticker: str, stamp: str) -> Path:
    return RAW / f"wayback_{kind}" / f"{ticker}__{stamp}.gz"


def cached_body(kind: str, ticker: str, stamp: str) -> bytes | None:
    """A body already on disk: this cache, the probe's, or the mega-cap OOS2 cache (csv only)."""
    candidates = [body_path(kind, ticker, stamp), PROBE_RAW / f"wayback_{kind}" / f"{ticker}__{stamp}.gz"]
    if kind == "csv":
        candidates.append(OOS2_RAW / "wayback_yahoo" / f"{ticker}__{stamp}.csv.gz")
    for p in candidates:
        if p.exists():
            data = p.read_bytes()
            try:
                data = gzip.decompress(data)
            except OSError:
                pass
            if data[:2] == b"\x1f\x8b":
                data = gzip.decompress(data)
            return data
        if p.with_name(p.name + ".404").exists():
            return b""
    return None


def fetch_body(kind: str, ticker: str, stamp: str, original: str) -> bytes | None:
    have = cached_body(kind, ticker, stamp)
    if have is not None:
        return have
    url = f"https://web.archive.org/web/{stamp}id_/{original.replace('&amp;', '&')}"
    use_own_ledger()
    try:
        data = common.cached_get(url, body_path(kind, ticker, stamp), source="wayback", limiter=WAYBACK_LIMITER,
                                 timeout=300, symbol=ticker, retries=2)
    except FileNotFoundError:
        return b""
    except Exception as exc:  # noqa: BLE001
        log(f"  page {kind} {ticker} {stamp}: {type(exc).__name__}")
        return None
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


# ======================================================================== the long fetch


def plan_security(t: pd.DataFrame, bulk_by_ticker: dict, sessions: pd.DatetimeIndex, failures: list | None = None) -> list[tuple]:
    """The captures to fetch for one security's target spans: (stamp, original, kind, lo, hi, ticker). A failed
    per-ticker CDX query is appended to ``failures`` (the security is then asked again on the next run)."""
    jobs = []
    for r in t.itertuples():
        a, b = pd.Timestamp(r.need_start), pd.Timestamp(r.need_end)
        need = sessions[(sessions >= a) & (sessions <= b)]
        if not len(need):
            continue
        tk = r.ticker
        caps = []
        for row in bulk_by_ticker.get(tk, []):
            kind, stamp, orig, start, end = row
            if kind == "div":
                continue
            lo, hi = capture_span(kind, stamp, start or None, end or None)
            if hi < a or lo > b + pd.Timedelta(days=(HP_DAYS if kind == "hp" else 0)):
                continue
            caps.append((stamp, orig, kind, lo, hi, tk))
        if pd.Timestamp("2015-06-01") <= b + pd.Timedelta(days=QH_DAYS):  # quote/T/history exists from about 2016
            qh = qh_captures(tk)
            if qh is None and failures is not None:
                failures.append(tk)
            for stamp, orig in qh or []:
                lo, hi = capture_span("qh", stamp)
                if hi >= a and lo <= b:
                    caps.append((stamp, orig, "qh", lo, hi, tk))
        # table.csv first (raw closes, long spans), then pages
        csvs = [c for c in caps if c[2] == "csv"]
        pages = [c for c in caps if c[2] != "csv"]
        chosen = choose_captures(need, csvs)
        covered = set()
        for c in chosen:
            covered |= {d for d in need if c[3] <= d <= c[4]}
        rest = pd.DatetimeIndex(sorted(set(need) - covered))
        chosen += choose_captures(rest, pages)
        # dividend files for the chosen csv spans
        for row in bulk_by_ticker.get(tk, []):
            kind, stamp, orig, start, end = row
            if kind == "div" and any(c[2] == "csv" for c in chosen):
                lo, hi = capture_span("csv", stamp, start or None, end or None)
                if hi >= a and lo <= b:
                    chosen.append((stamp, orig, "div", lo, hi, tk))
                    break
        jobs += chosen
    seen, out = set(), []
    for j in jobs:
        key = (j[2], j[5], j[0])
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


def run_fetch(limit_securities: int = 0, batch: int = 8) -> None:
    """The long run: securities in priority order, ``batch`` at a time, their captures fetched by ``WORKERS`` threads
    under the shared 15-a-minute limiter. A security is marked done (``fetch_done.csv``) when none of its captures
    failed; a failed one is asked again on the next run. ``FILL/STOP`` stops between batches."""
    use_own_ledger()
    t = targets()
    bulk = bulk_captures()
    bulk_by_ticker: dict[str, list] = {}
    for r in bulk.itertuples(index=False):
        bulk_by_ticker.setdefault(r.ticker, []).append((r.kind, r.stamp, r.original,
                                                        r.start if isinstance(r.start, str) else "",
                                                        r.end if isinstance(r.end, str) else ""))
    sessions = v1_sessions()
    order = list(dict.fromkeys(t["security_id"]))
    if limit_securities:
        order = order[:limit_securities]
    done_path = FILL / "fetch_done.csv"
    done = set(pd.read_csv(done_path, dtype=str)["security_id"]) if done_path.exists() else set()
    todo = [sid for sid in order if sid not in done]
    lock = threading.Lock()
    stats = {"securities_done": len(done & set(order)), "securities_total": len(order), "pages_ok": 0,
             "pages_failed": 0, "pages_cached_or_404": 0,
             "started_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    from concurrent.futures import ThreadPoolExecutor
    for b0 in range(0, len(todo), batch):
        if (FILL / "STOP").exists():
            log("STOP file found: stopping")
            break
        group = todo[b0:b0 + batch]
        cdx_failed: dict[str, list] = {sid: [] for sid in group}

        def plan(sid):
            try:
                return plan_security(t[t["security_id"] == sid], bulk_by_ticker, sessions, cdx_failed[sid])
            except Exception as exc:  # noqa: BLE001
                log(f"  plan {sid}: {type(exc).__name__}: {exc}")
                cdx_failed[sid].append("plan_error")
                return []

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:  # the per-ticker CDX queries, in parallel
            planned = list(pool.map(plan, group))
        jobs = dict(zip(group, planned))
        flat = [(sid, j) for sid, js in jobs.items() for j in js]
        failed: dict[str, int] = {sid: len(cdx_failed[sid]) for sid in group}

        def one(item):
            sid, (stamp, orig, kind, lo, hi, tk) = item
            cached = cached_body(kind, tk, stamp) is not None
            data = fetch_body(kind, tk, stamp, orig)
            with lock:
                if data is None:
                    stats["pages_failed"] += 1
                    failed[sid] += 1
                elif cached:
                    stats["pages_cached_or_404"] += 1
                else:
                    stats["pages_ok"] += 1

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(one, flat))
        plan_rows = pd.DataFrame([{"security_id": sid, "ticker": j[5], "kind": j[2], "stamp": j[0], "original": j[1],
                                   "lo": j[3].date(), "hi": j[4].date()} for sid, j in flat])
        if len(plan_rows):
            plan_rows.to_csv(FILL / "fetch_plan.csv", mode="a", header=not (FILL / "fetch_plan.csv").exists(),
                             index=False)
        finished = [sid for sid in group if failed[sid] == 0]
        if finished:
            pd.DataFrame([{"security_id": sid, "jobs": len(jobs[sid]),
                           "finished_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
                          for sid in finished]).to_csv(done_path, mode="a", header=not done_path.exists(), index=False)
            stats["securities_done"] += len(finished)
        stats.update({"last_batch": group, "position": min(b0 + batch, len(todo)), "todo_at_start": len(todo),
                      "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")})
        common.atomic_write(FILL / "progress.json", (json.dumps(stats, indent=1) + "\n").encode())
        log(f"batch {b0 // batch + 1}: {len(flat)} captures for {len(group)} securities, {sum(failed.values())} failed "
            f"(done {stats['securities_done']}/{len(order)}; pages ok {stats['pages_ok']}, cached {stats['pages_cached_or_404']}, "
            f"failed {stats['pages_failed']})")
        if sum(failed.values()) > 5:  # archive.org is refusing: pause before the next batch
            time.sleep(300)


# ======================================================================== parse -> series


def parse_all() -> dict:
    """Every fetched capture of every target into ``SERIES/archive/{security_id}.csv.gz``: one row per (capture,
    date) with the capture's raw record; identity rules applied (rows only inside the ticker's need spans are kept
    per span below; a capture showing more than 5 sessions after the delisting is dropped for the original ticker)."""
    t = targets()
    sessions = v1_sessions()
    master = pd.read_csv(V1_INPUTS / "security_master.csv", dtype=str).set_index("security_id")
    plan = pd.read_csv(FILL / "fetch_plan.csv", dtype=str) if (FILL / "fetch_plan.csv").exists() else \
        pd.DataFrame(columns=["security_id", "ticker", "kind", "stamp", "original"])
    plan = plan.drop_duplicates(["security_id", "ticker", "kind", "stamp"])
    intervals = pd.read_csv(V1_INPUTS / "ticker_intervals.csv", dtype=str)
    (SERIES / "archive").mkdir(parents=True, exist_ok=True)
    caps_out = []
    counts = {"securities": 0, "captures": 0, "captures_dropped_post_delisting": 0, "rows": 0}
    for sid, g in plan.groupby("security_id"):
        delist = master.loc[sid, "delist_date"] if sid in master.index else None
        delist = pd.Timestamp(delist) if isinstance(delist, str) and delist else None
        own = intervals[intervals["security_id"] == sid]
        tt = t[t["security_id"] == sid]
        frames = []
        events_div: dict[str, list] = {}
        for r in g[g["kind"] == "div"].itertuples():
            data = cached_body("div", r.ticker, r.stamp)
            if data:
                _, ev = parse_body("div", data.decode("utf-8", "replace"))
                events_div.setdefault(r.ticker, []).extend(ev)
        for r in g[g["kind"] != "div"].itertuples():
            data = cached_body(r.kind, r.ticker, r.stamp)
            if not data:
                continue
            df, ev = parse_body(r.kind, data.decode("utf-8", "replace"))
            if df.empty:
                caps_out.append({"security_id": sid, "ticker": r.ticker, "kind": r.kind, "stamp": r.stamp, "rows": 0,
                                 "status": "no_rows"})
                continue
            if r.kind == "csv":
                ev = ev + events_div.get(r.ticker, [])
            rec = raw_from_capture(df, ev)
            spans = tt[tt["ticker"] == r.ticker]
            cat = set(spans["category"])
            otc = cat <= {"d5_otc"}
            child = cat <= {"fix_child"}
            n_post = post_delist_count(rec["date"], delist, sessions)
            status = "ok"
            if not otc and not child and n_post > POST_DELIST_MAX:
                status = "dropped_post_delisting"
                counts["captures_dropped_post_delisting"] += 1
            # the ticker in use: rows inside this security's ticker intervals for that ticker (OTC tickers: inside the
            # D5 span only); kept rows also inside the target spans widened by the capture itself (the dv warm-up)
            keep = np.zeros(len(rec), dtype=bool)
            if otc or child:
                for s in spans.itertuples():
                    keep |= (rec["date"] >= pd.Timestamp(s.need_start)).values & (rec["date"] <= pd.Timestamp(s.need_end)).values
            else:
                iv = own[own["ticker"].str.upper() == r.ticker]
                for s in iv.itertuples():
                    lo = pd.Timestamp(s.start) - pd.Timedelta(days=WARMUP_DAYS)
                    hi = pd.Timestamp(s.end_next_absent if isinstance(s.end_next_absent, str) and s.end_next_absent
                                      else s.end) + pd.Timedelta(days=5)
                    keep |= (rec["date"] >= lo).values & (rec["date"] <= hi).values
                if delist is not None:
                    keep &= (rec["date"] <= delist).values
                if not len(iv):
                    keep[:] = False
                    status = "ticker_not_in_intervals"
            rec = rec[keep & rec["date"].isin(sessions).values]
            caps_out.append({"security_id": sid, "ticker": r.ticker, "kind": r.kind, "stamp": r.stamp,
                             "rows": int(len(rec)), "post_delisting_sessions": n_post, "mode": rec["mode"].iloc[0]
                             if len(rec) else "", "status": status if len(rec) or status != "ok" else "outside_spans"})
            if status != "ok" or rec.empty:
                continue
            rec = rec.assign(ticker=r.ticker, kind=r.kind, capture=r.stamp, otc=otc)
            frames.append(rec[["date", "ticker", "kind", "capture", "otc", "close", "adj", "volume", "close_raw",
                               "volume_raw", "split", "div", "mode"]])
            counts["captures"] += 1
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["date", "ticker", "kind", "capture", "otc", "close", "adj", "volume", "close_raw", "volume_raw",
                     "split", "div", "mode"])
        common.atomic_write(SERIES / "archive" / f"{sid}.csv.gz",
                            gzip.compress(out.to_csv(index=False).encode(), mtime=0))
        counts["securities"] += 1
        counts["rows"] += int(len(out))
    pd.DataFrame(caps_out).to_csv(FILL / "captures.csv", index=False)
    common.atomic_write(FILL / "parse_summary.json", (json.dumps(counts, indent=1) + "\n").encode())
    log(f"parsed: {counts}")
    return counts


# ======================================================================== companiesmarketcap and QuantQuote


def run_cmc(limit: int = 0) -> None:
    """companiesmarketcap for the delisted gap securities (the probe's identity rules), one request every 3 s."""
    t = targets()
    t = t[t["category"].isin(["missing_weeks", "d5_nasdaq", "fix_cald_last_sessions"])]
    master = pd.read_csv(V1_INPUTS / "security_master.csv", dtype=str).set_index("security_id")
    use_own_ledger()
    probe.RAW = FILL / "raw"  # the probe's helpers cache under this module's raw folder (the probe's own files reused)
    order = list(dict.fromkeys(t["security_id"]))
    if limit:
        order = order[:limit]
    (SERIES / "cmc").mkdir(parents=True, exist_ok=True)
    rows = []
    for sid in order:
        if (FILL / "STOP").exists():
            break
        tt = t[t["security_id"] == sid]
        tk = tt["ticker"].iloc[0]
        name = master.loc[sid, "name"] if sid in master.index else ""
        delist = master.loc[sid, "delist_date"] if sid in master.index else ""
        yr = int(str(delist)[:4]) if isinstance(delist, str) and delist[:4].isdigit() else None
        if yr is None and pd.Timestamp(tt["need_end"].max()) < pd.Timestamp("2026-06-01"):
            yr = pd.Timestamp(tt["need_end"].max()).year
        hit = None
        for q in (tk, re.sub(r"(?i)\b(inc|corp|corporation|ltd|holdings?|co|plc|llc|group|the)\b\.?,?", "",
                             str(name)).strip(" ,.")):
            if not q:
                continue
            for base in (PROBE_RAW, FILL / "raw"):
                p = base / "cmc" / f"search__{re.sub(r'[^A-Za-z0-9]+', '_', q)}.json"
                if p.exists():
                    hit = probe.cmc_pick(json.loads(p.read_text() or "[]"), tk, yr)
                    break
            else:
                try:
                    hit = probe.cmc_pick(probe.cmc_search(q), tk, yr)
                except Exception as exc:  # noqa: BLE001
                    log(f"  cmc search {q}: {type(exc).__name__}")
            if hit:
                break
        if not hit:
            hit = _cmc_guess_cached(tk, name, yr)
        if not hit:
            rows.append({"security_id": sid, "ticker": tk, "slug": "", "identifier": ""})
            continue
        slug = hit["url"]
        h = _cmc_page(slug, "marketcap")
        if h is None:
            continue
        m = re.search(r"data = (\[.*?\]);", h, re.S)
        d = json.loads(m.group(1)) if m else []
        s = pd.DataFrame({"date": [pd.Timestamp(x["d"], unit="s").normalize() for x in d],
                          "mcap": [float(x["m"]) for x in d]})
        common.atomic_write(SERIES / "cmc" / f"{sid}.csv.gz", gzip.compress(s.to_csv(index=False).encode(), mtime=0))
        rows.append({"security_id": sid, "ticker": tk, "slug": slug, "identifier": hit["identifier"], "rows": len(s)})
        log(f"cmc {tk}: {hit['identifier']} rows {len(s)}")
    pd.DataFrame(rows).to_csv(SERIES / "cmc" / "_matches.csv", index=False)


def _cmc_page(slug: str, page: str) -> str | None:
    for base in (PROBE_RAW, OOS2_RAW):
        p = base / "cmc" / f"{slug}__{'mcap' if page == 'marketcap' else page}.html.gz"
        if p.exists():
            return gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
    try:
        return common.cached_get(f"https://companiesmarketcap.com/{slug}/{page}/",
                                 FILL / "raw" / "cmc" / f"{slug}__{'mcap' if page == 'marketcap' else page}.html.gz",
                                 source="companiesmarketcap", limiter=CMC_LIMITER, headers=probe.UA,
                                 symbol=slug).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return None


def _cmc_guess_cached(ticker: str, name: str, yr: int | None) -> dict | None:
    for slug in probe.cmc_slugs(name)[:3]:
        h = _cmc_page(slug, "marketcap")
        if not h:
            continue
        title = " ".join(re.findall(r"<title>(.*?)</title>", h, re.S))
        if f"({ticker.upper()})" not in title.upper():
            continue
        m = re.search(r"data = (\[.*?\]);", h, re.S)
        d = json.loads(m.group(1)) if m else []
        if not d:
            continue
        last = pd.Timestamp(d[-1]["d"], unit="s")
        if yr and abs(last.year - yr) > 1:
            continue
        return {"url": slug, "identifier": f"guess:{slug}"}
    return None


QQ_ZIP = PROBE_RAW / "quantquote" / "quantquote_daily_sp500_83986.zip"


def run_quantquote() -> None:
    """The QuantQuote pack (already downloaded by the probe): every target ticker's file, inside its spans."""
    t = targets()
    z = zipfile.ZipFile(QQ_ZIP)
    names = {Path(n).name.lower(): n for n in z.namelist()}
    intervals = pd.read_csv(V1_INPUTS / "ticker_intervals.csv", dtype=str)
    (SERIES / "quantquote").mkdir(parents=True, exist_ok=True)
    n = 0
    for sid, g in t[t["category"] != "d5_otc"].groupby("security_id"):
        frames = []
        for tk in sorted(set(g["ticker"])):
            key = f"table_{tk.lower()}.csv"
            if key not in names:
                continue
            df = pd.read_csv(z.open(names[key]), header=None,
                             names=["date", "time", "open", "high", "low", "close", "volume"])
            df["date"] = pd.to_datetime(df["date"].astype(str), format="%Y%m%d")
            iv = intervals[(intervals["security_id"] == sid) & (intervals["ticker"].str.upper() == tk)]
            keep = np.zeros(len(df), dtype=bool)
            for s in iv.itertuples():
                keep |= (df["date"] >= pd.Timestamp(s.start) - pd.Timedelta(days=WARMUP_DAYS)).values & \
                        (df["date"] <= pd.Timestamp(s.end)).values
            df = df[keep]
            if len(df):
                frames.append(df[["date", "close", "volume"]].assign(ticker=tk))
        if frames:
            out = pd.concat(frames).drop_duplicates("date").sort_values("date")
            out.to_csv(SERIES / "quantquote" / f"{sid}.csv.gz", index=False)
            n += 1
    log(f"quantquote: {n} securities with rows")


YAHOO_LIMITER = common.SlidingWindowLimiter({2: 1})


def run_yahoo_otc() -> None:
    """Plan 4.5: an OTC close after the last Nasdaq session replaces D5. Live Yahoo v8 charts of the OTC tickers
    (T+Q after a bankruptcy, T+F for a foreign issuer) of every ``awaiting_d5`` name, delisting - 60 days to + 60 days,
    with version 1's request headers (Yahoo answers 429 to a full browser User-Agent). The bare ticker is not asked:
    after a delisting it may be another company's. Rows go to ``SERIES/yahoo_otc/{security_id}.csv.gz``."""
    use_own_ledger()
    t = pd.read_csv(V1_INPUTS / "terminal_returns_2012_2026.csv", dtype=str)
    d5 = t[t["status"] == "awaiting_d5"]
    (SERIES / "yahoo_otc").mkdir(parents=True, exist_ok=True)
    found = 0
    for r in d5.itertuples():
        dd = pd.Timestamp(r.delist_date)
        a = int((dd - pd.Timedelta(days=60)).timestamp())
        b = int((dd + pd.Timedelta(days=60)).timestamp())
        frames = []
        for tk in (r.ticker + "Q", r.ticker + "F"):
            url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}?period1={a}&period2={b}&interval=1d"
                   "&events=div%2Csplits")
            probe_copy = PROBE_RAW / "yahooq" / f"{tk}_{a}_{b}.json.gz"
            try:
                if probe_copy.exists():
                    body = gzip.decompress(probe_copy.read_bytes())
                else:
                    body = common.cached_get(url, RAW / "yahoo_otc" / f"{tk}_{a}_{b}.json.gz", source="yahoo",
                                             limiter=YAHOO_LIMITER, symbol=tk,
                                             headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            except Exception as exc:  # noqa: BLE001  (404 cached as a marker)
                log(f"  yahoo {tk}: {type(exc).__name__}")
                continue
            res = ((json.loads(body).get("chart") or {}).get("result") or [None])[0]
            ts = (res or {}).get("timestamp") or []
            if not ts:
                continue
            q = res["indicators"]["quote"][0]
            dates = pd.to_datetime(ts, unit="s").tz_localize("UTC").tz_convert("America/New_York").tz_localize(None).normalize()
            frames.append(pd.DataFrame({"date": dates, "close_raw": q["close"], "volume_raw": q["volume"], "ticker": tk,
                                        "exchange": res["meta"].get("exchangeName", "")}).dropna(subset=["close_raw"]))
        if frames:
            out = pd.concat(frames).sort_values("date")
            out.to_csv(SERIES / "yahoo_otc" / f"{r.security_id}.csv.gz", index=False)
            found += 1
            log(f"yahoo otc {r.ticker}: {len(out)} rows ({' '.join(sorted(set(out['ticker'])))})")
    log(f"yahoo otc: {found} of {len(d5)} D5 names have an OTC chart")


def status() -> dict:
    out = {}
    if (FILL / "progress.json").exists():
        out["progress"] = json.loads((FILL / "progress.json").read_text())
    if (FILL / "targets.csv").exists():
        t = targets()
        out["targets"] = {"spans": len(t), "securities": int(t["security_id"].nunique())}
    if common.QUOTA_LEDGER.exists():
        q = pd.read_csv(common.QUOTA_LEDGER, dtype=str)
        out["requests"] = q.groupby(["source", "status"]).size().to_dict()
    print(json.dumps(out, indent=1, default=str))
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["targets", "index", "fetch", "parse", "cmc", "quantquote", "yahoo_otc", "status"])
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)
    FILL.mkdir(parents=True, exist_ok=True)
    use_own_ledger()
    if args.step == "targets":
        build_targets()
    elif args.step == "index":
        bulk_index()
        log(f"bulk captures: {len(bulk_captures())}")
    elif args.step == "fetch":
        run_fetch(args.limit)
    elif args.step == "parse":
        parse_all()
    elif args.step == "cmc":
        run_cmc(args.limit)
    elif args.step == "quantquote":
        run_quantquote()
    elif args.step == "yahoo_otc":
        run_yahoo_otc()
    else:
        status()


if __name__ == "__main__":
    main()
