"""Plan step 6 (docs/reversal_2012_2026_data_plan.md, section 3.2): liquidity pre-filter and fetch list.

Data only. Nothing here computes returns, signals, return rankings or spreads.
The only ranks are dollar-volume ranks (median raw close x raw volume) and
market-cap / public-float ranks, used to decide which price histories to fetch.

Inputs (all local, read only):
- ``security_master.csv`` and ``ticker_intervals.csv`` (steps 3-4): which
  security held which Nasdaq ticker when, delist dates, the foreign-filer flag;
- ``form25_nasdaq_2012_2026.csv`` (step 3): public float before each delisting;
- ``listing_snapshots_index.csv`` and ``CACHE/listings/companylist`` (step 2):
  Wayback company lists with LastSale and MarketCap, 2011-2019;
- price series, every one we already hold:
  * WIKI raw (``CACHE/wiki/by_ticker``, 2011-06-01..2018-03-27): vendor raw;
  * Tiingo JSON caches (``research_cache/sue_lt_2020_2026/raw``,
    ``research_cache/tiingo_delisted``, ``research_cache/tiingo_overlap``): vendor raw
    (``close``/``volume`` as traded);
  * Yahoo charts (``research_cache/holdout_2011_2019/yahoo_nominal``): vendor raw
    after the documented split restore (close x later split ratios, volume / the
    same; holdout data report section 5.1);
  * the stored repo files (``cleaned_stocks_data/price``): NOT vendor raw. They are
    split- and (to about 2023) dividend-adjusted, with volume scaled the opposite
    way, so close x volume is the raw dollar volume (checked against WIKI:
    median ratio 0.999-1.000 in 2011-2015, see ``stored_dv_check``). Their close
    is restored to raw only by the split events we know (Yahoo, Tiingo,
    WIKI, ``confirmed_price_adjustments.csv``); a stored close without such a
    restore is an estimate (``price_flag`` E), never a vendor level.

Rules (plan section 3.2; foreign filers are excluded as the owner decided, MIXED
filers only while their latest periodic report is a 20-F/40-F/6-K):
- A1: 2012-01..2018-03, dv rank <= 300 in any week, from WIKI raw, and the listed
  interval is not covered by a vendor raw series;
- A2: 2011-11..2019-06, Wayback company-list market cap rank <= 400 on any capture;
- A3 (proxy fallback, reported separately): a Form 25 delisting 2012-01..2018-03 with
  float >= $1B, not covered and not caught by A1/A2 (mostly the 2012-2014-03
  delistings that WIKI lacks);
- B-A / B-B / B-C: Form 25 delistings 2018-04..2024-06, maximum checked public float
  in the 3 years before (>= $1B / $500M-$1B / $300M-$500M, tier C a seeded sample
  of 20, the rest conditional on the sample);
- C: a series that starts more than 60 days after the first snapshot appearance,
  where A1/A2/B-A hold for the missing window;
- V: a stratified 50-name verification sample of active names (Tiingo next to Yahoo);
- Y: active names ranked <= 300 in any week whose listed interval no vendor raw series
  covers (plan step 7, Yahoo);
- S: names delisted after 2024-06 whose only series is a stored file and which rank
  <= 300 (no plan tier covers them; month 2, reported separately).

Routing (plan section 3.2): 2011-06..2018-03-27 -> WIKI when it has the security
and the entity check passes; delisted names after that -> Tiingo only when one
``supported_tickers`` row covers the needed interval (tickers the security ever used
are tried, newest first); active names -> Yahoo; nothing -> ``unfillable.csv``.

Data hygiene applied on the way (each counted in prefilter_summary.json):
- WIKI rows are cut to the security's listing spans and a WIKI file whose raw close
  disagrees with the company lists' LastSale is dropped (plan 4.4 R9: LSI, TIVO, ...);
- a stored file's rows belong to the file's company (step 4's price_file_cik_map): rows the
  ticker map gave to an earlier holder of the ticker move to the owner when it was listed
  then (ContextLogic's LOGC over LogicBio), and stay with the predecessor otherwise;
- weeks after the last trade (before the Form 25 takes effect) are not "uncovered";
- XBRL floats with x1000 unit errors are dropped ($3,000 a share, 50x the CIK's other facts);
- a security whose successor (step-4 link) holds its history in the successor's stored file
  goes to Yahoo under the successor's ticker; Tiingo is asked only for tickers the security
  still held during the need, and a short partial row starting inside the need counts as a
  newer company (wrong_entity).

Outputs:
  INPUTS/candidate_fetch_list.csv, INPUTS/unfillable.csv   (ids, dates, ranks, SEC floats; no price levels)
  CACHE/prefilter/daily_series.pkl      per security-day close/volume/dv/source (local only)
  CACHE/prefilter/weekly_metrics.pkl    per listed security-week: dv20, dv50, ranks, flags, proxies
  CACHE/prefilter/weekly_coverage.csv   per week: how many of ranks 1-300 have vendor raw
  CACHE/prefilter/security_facts.csv.gz, event_flags.csv, spans/lists/files/entity.csv.gz
  CACHE/prefilter/supported_tickers_{date}.zip   Tiingo's public ticker list (no key)
  CACHE/prefilter/prefilter_summary.json counts, checks, budget

Usage::

    PYTHONPATH=. python scripts/reversal_data_prefilter.py            # build (downloads the Tiingo list once)
    PYTHONPATH=. python scripts/reversal_data_prefilter.py --offline  # fail instead of downloading
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import gzip
import io
import json
from pathlib import Path
import re
import sys
import time
import zipfile

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

MAIN = common.MAIN_CHECKOUT
OUT = common.CACHE / "prefilter"
INPUTS = common.INPUTS
MASTER = INPUTS / "security_master.csv"
INTERVALS = INPUTS / "ticker_intervals.csv"
FORM25 = INPUTS / "form25_nasdaq_2012_2026.csv"
SNAPSHOT_INDEX = INPUTS / "listing_snapshots_index.csv"
CANDIDATES = INPUTS / "candidate_fetch_list.csv"
UNFILLABLE = INPUTS / "unfillable.csv"

WIKI_DIR = common.CACHE / "wiki" / "by_ticker"
STORED_DIR = MAIN / "cleaned_stocks_data" / "price"
YAHOO_DIRS = [MAIN / "research_cache" / "holdout_2011_2019" / "yahoo_nominal"]
TIINGO_DIRS = [MAIN / "research_cache" / "sue_lt_2020_2026" / "raw",
               MAIN / "research_cache" / "tiingo_delisted",
               MAIN / "research_cache" / "tiingo_overlap"]
CONFIRMED_ADJUSTMENTS = MAIN / "stocks_list_dir" / "nasdaq" / "confirmed_price_adjustments.csv"

WINDOW_START, WINDOW_END = "2011-06-01", "2026-08-31"
FIRST_WEEK, LAST_WEEK = "2012-01-06", "2026-07-17"
WIKI_END = "2018-03-27"
FETCH_RANK = 300
MCAP_RANK = 400
MIN_PRICE = 10.0
DV_WINDOWS = {20: 10, 50: 25}  # sessions -> minimum observations for a median
CLOSE_STALE_SESSIONS = 5  # a week-end close may be this many sessions old
LATE_START_DAYS = 60
A1_WINDOW = ("2012-01-01", WIKI_END)
A2_WINDOW = ("2011-11-01", "2019-06-30")
B_WINDOW = ("2018-04-01", "2024-06-30")
S_AFTER = "2024-06-30"
FLOAT_TIERS = (("B_A", 1e9, float("inf")), ("B_B", 5e8, 1e9), ("B_C", 3e8, 5e8))
A3_FLOAT = 1e9
GOOD_FLOAT_FLAGS = {"ok", "review", "no_shares"}
TIER_C_SAMPLE, V_SAMPLE = 20, 50
SEED = 20261001
MONTH_1, MONTH_2 = "2026-10", "2026-11"
TIINGO_MONTHLY_SYMBOLS = 500
TIINGO_RANGE_SLACK_DAYS = 7
TIINGO_SUPPORTED_URL = "https://apimedia.tiingo.com/docs/tiingo/daily/supported_tickers.zip"
# Plan section 3.2: known names no free source has.
KNOWN_UNFILLABLE = {"BMC", "LNCR", "MOLX", "ONXX", "PMTC", "CNSIV", "FMCN", "MRKT"}
# Plan section 3.2 V: the 2025-06-24 break names it lists, and the splits it names after 2023.
V_NAMED = ["KLAC", "NFLX", "BKNG", "HON", "CRWD", "AZN", "LCID", "TLRY"]
V_KNOWN_EVENTS = {  # ticker -> (date, kind) from plan section 4.3
    "PRPL": ("2026-07-20", "split_after_2023"), "SIRI": ("2024-09-10", "split_after_2023"),
    "NFLX": ("2025-11-17", "split_after_2023"), "BKNG": ("2026-04-06", "split_after_2023"),
    "KLAC": ("2026-06-12", "split_after_2023"), "MNST": ("2026-08-11", "split_after_2023"),
    "HON": ("2025-10-30", "odd_split"),
}
BREAK_DATE = "2025-06-24"
SOURCE_RANK = {"wiki": 0, "tiingo": 1, "yahoo": 2, "stored": 3}
VENDOR = {"wiki", "tiingo", "yahoo"}


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# ------------------------------------------------------------------ calendar

def xnas_sessions(start: str = WINDOW_START, end: str = WINDOW_END) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
    return pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)


def week_ends(sessions: pd.DatetimeIndex, first: str = FIRST_WEEK, last: str = LAST_WEEK) -> pd.DatetimeIndex:
    """The last XNAS session of each calendar week (Monday-Sunday) from ``first`` to ``last``."""
    frame = pd.Series(sessions, index=sessions)
    ends = frame.groupby(sessions.to_period("W-SUN")).max()
    ends = pd.DatetimeIndex(ends.values)
    return ends[(ends >= pd.Timestamp(first)) & (ends <= pd.Timestamp(last))]


# ------------------------------------------------------------------ identity and listing

def read_csv_text(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def load_master(path: Path = MASTER) -> pd.DataFrame:
    return read_csv_text(path)


def load_intervals(path: Path = INTERVALS) -> pd.DataFrame:
    """Snapshot-evidence Nasdaq intervals only (SEC current-ticker rows carry no listing dates)."""
    intervals = read_csv_text(path)
    return intervals[(intervals["start"] != "") & (intervals["exchange"] == "NASDAQ")].reset_index(drop=True)


def _day_before(day: str) -> str:
    return (pd.Timestamp(day) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def listing_spans(intervals: pd.DataFrame, master: pd.DataFrame, window_end: str = WINDOW_END) -> pd.DataFrame:
    """One row per interval with the dates it counts as listed (plan section 3.1, simplified).

    ``list_start`` is the first snapshot that shows it; ``list_end`` reaches to the day before
    the next full-list snapshot without it (``end_next_absent``) or, when no later snapshot
    misses it, to ``window_end``; a Form 25 delist date earlier than that cuts it. The IPO
    rule (prices between two snapshots) is applied later, in ``extend_starts``.
    """
    delist = dict(zip(master["security_id"], master["delist_date"]))
    rows = []
    for row in intervals.itertuples(index=False):
        end = _day_before(row.end_next_absent) if row.end_next_absent else window_end
        end = max(end, row.end)
        cut = delist.get(row.security_id, "")
        if cut and cut < end:
            end = max(cut, row.start)
        rows.append({"security_id": row.security_id, "ticker": row.ticker, "list_start": row.start,
                     "list_end": end, "start_prev_absent": row.start_prev_absent, "obs_end": row.end,
                     "name": row.name_in_source, "share_class": row.share_class})
    return pd.DataFrame(rows)


def non_common_interval(name: str, share_class: str) -> bool:
    """The investable filter on the listed name (SPACs, LPs, funds...); ADRs are left to the foreign flag."""
    from src.io.security_universe import NON_COMMON_SECURITY_PATTERN

    if share_class == "ADS":
        return False
    return bool(re.search(NON_COMMON_SECURITY_PATTERN, str(name or ""), re.IGNORECASE))


# ------------------------------------------------------------------ foreign filers

# The switch between foreign and domestic shows in the annual and quarterly reports; a 6-K
# beside 10-Qs is not a switch (it only marks a CIK foreign when it files no report at all).
FOREIGN_BASE = {"20-F", "40-F", "20FR12B", "40FR12B"}
DOMESTIC_BASE = {"10-K", "10-K405", "10-KSB", "10-KT", "10-Q", "10-QSB", "10-QT"}


def periodic_timeline(payload: dict, pages: list[dict] = ()) -> list[tuple[str, str]]:
    """(filing date, 'F' or 'D') for every annual or quarterly report in a submissions JSON, oldest first."""
    from scripts.reversal_data_security_master import _filing_columns

    filings = _filing_columns((payload.get("filings") or {}).get("recent") or {})
    for page in pages:
        filings.extend(_filing_columns(page))
    out = []
    for form, day in filings:
        base = form[:-2] if form.endswith("/A") else form
        if base in FOREIGN_BASE:
            out.append((day, "F"))
        elif base in DOMESTIC_BASE:
            out.append((day, "D"))
    return sorted(set(out))


def foreign_spans_from_timeline(timeline: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Date spans [start, end] in which the latest periodic report is foreign (before the first
    report, the first report's kind applies)."""
    if not timeline:
        return []
    spans, current, since = [], timeline[0][1], "1900-01-01"
    for day, kind in timeline[1:]:
        if kind != current:
            if current == "F":
                spans.append((since, _day_before(day)))
            current, since = kind, day
    if current == "F":
        spans.append((since, "2100-01-01"))
    return spans


def foreign_spans(master: pd.DataFrame, offline: bool = True) -> dict[str, list[tuple[str, str]]]:
    """security_id -> spans when it is a foreign filer. Y: always; MIXED: from its submissions
    timeline (a 6-K beside 10-Qs counts as foreign only when it is the latest periodic form);
    N and UNKNOWN: never (UNKNOWN filed no periodic report since 2011-06)."""
    from scripts.reversal_data_security_master import load_submissions

    out = {}
    timelines = {}
    for row in master.itertuples(index=False):
        if row.foreign_filer == "Y":
            out[row.security_id] = [("1900-01-01", "2100-01-01")]
        elif row.foreign_filer == "MIXED":
            cik = int(row.cik)
            if cik not in timelines:
                payload = load_submissions(cik, offline=offline)
                timelines[cik] = periodic_timeline(payload) if payload else []
            spans = foreign_spans_from_timeline(timelines[cik])
            out[row.security_id] = spans if timelines[cik] else [("1900-01-01", "2100-01-01")]
    return out


def is_foreign_on(spans: list[tuple[str, str]] | None, day: str) -> bool:
    return bool(spans) and any(a <= day <= b for a, b in spans)


# ------------------------------------------------------------------ ticker -> security by date

class TickerMap:
    """Which security a ticker-keyed price row belongs to.

    A row dated inside a listing span of the ticker belongs to that span's security
    (``direct``). A row outside every span of the ticker goes to the security of the
    ticker's nearest span when that security was listed then under another ticker (a vendor
    keeps a renamed company's history under its newest ticker: FB, GOOGL); otherwise it is
    dropped (another company, another exchange, or before the listing). Direct rows win over
    such continuity rows when both give the same security-day.
    """

    def __init__(self, spans: pd.DataFrame):
        self.by_ticker: dict[str, list[tuple[np.datetime64, np.datetime64, str]]] = defaultdict(list)
        for row in spans.itertuples(index=False):
            self.by_ticker[row.ticker].append(
                (np.datetime64(row.list_start), np.datetime64(row.list_end), row.security_id))
        for ticker in self.by_ticker:
            self.by_ticker[ticker].sort()
        whole = spans.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
        self.security_span = {s: (np.datetime64(a), np.datetime64(b)) for s, a, b in
                              zip(whole.index, whole["first"], whole["last"])}

    def tickers(self) -> set[str]:
        return set(self.by_ticker)

    def assign(self, ticker: str, dates) -> tuple[np.ndarray, np.ndarray]:
        """(security ids, '' where none; direct flags) for ``dates`` of ``ticker``."""
        dates = np.asarray(dates, dtype="datetime64[D]")
        owner = np.full(len(dates), "", dtype=object)
        direct = np.zeros(len(dates), dtype=bool)
        spans = self.by_ticker.get(ticker, [])
        if not spans or not len(dates):
            return owner, direct
        for start, end, security in spans:
            inside = (dates >= start) & (dates <= end) & (owner == "")
            owner[inside] = security
            direct[inside] = True
        rest = np.flatnonzero(owner == "")
        if len(rest):
            starts = np.array([s for s, _, _ in spans], dtype="datetime64[D]")
            ends = np.array([e for _, e, _ in spans], dtype="datetime64[D]")
            days = dates[rest][:, None]
            distance = np.where(days < starts[None, :], starts[None, :] - days, days - ends[None, :]).astype("int64")
            nearest = distance.argmin(axis=1)
            securities = np.array([s for _, _, s in spans], dtype=object)[nearest]
            first = np.array([self.security_span[s][0] for s in securities], dtype="datetime64[D]")
            last = np.array([self.security_span[s][1] for s in securities], dtype="datetime64[D]")
            ok = (dates[rest] >= first) & (dates[rest] <= last)
            owner[rest[ok]] = securities[ok]
        return owner, direct


# ------------------------------------------------------------------ price series

SERIES_COLUMNS = ["ticker", "date", "close", "volume", "src", "file"]


def _frame(ticker: str, dates, close, volume, src: str, file: str) -> pd.DataFrame:
    frame = pd.DataFrame({"date": pd.to_datetime(pd.Series(dates)).dt.normalize().values,
                          "close": pd.to_numeric(pd.Series(close), errors="coerce").values,
                          "volume": pd.to_numeric(pd.Series(volume), errors="coerce").values})
    frame["ticker"], frame["src"], frame["file"] = ticker, src, file
    frame = frame[(frame["date"] >= WINDOW_START) & (frame["date"] <= WINDOW_END)]
    frame = frame[(frame["close"] > 0) & frame["volume"].notna()]
    return frame.drop_duplicates("date", keep="last")[SERIES_COLUMNS]


def read_wiki(tickers: set[str], directory: Path = WIKI_DIR) -> list[pd.DataFrame]:
    """WIKI raw close and volume (as traded) for every WIKI file whose ticker is a Nasdaq ticker."""
    from scripts.reversal_data_wiki import READ_KW, safe_ticker

    frames = []
    for ticker in sorted(tickers):
        path = directory / f"{safe_ticker(ticker)}.csv.gz"
        if path.exists():
            data = pd.read_csv(path, usecols=["ticker", "date", "close", "volume"], **READ_KW)
            frames.append(_frame(ticker, data["date"], data["close"], data["volume"], "wiki", path.name))
    return frames


def read_tiingo(tickers: set[str], directories: list[Path] = TIINGO_DIRS) -> list[pd.DataFrame]:
    """Tiingo daily JSON caches ({meta, prices}): ``close`` and ``volume`` are as traded.

    A post-delisting OTC ticker (SIVBQ, BBBYQ, SPWRQ) carries the Nasdaq history of the
    ticker without its Q, which is the ticker the listing intervals know.
    """
    frames = []
    for directory in directories:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ticker = str((payload.get("meta") or {}).get("ticker") or path.stem).upper()
            if ticker not in tickers and ticker.endswith("Q") and ticker[:-1] in tickers:
                ticker = ticker[:-1]
            prices = payload.get("prices") or []
            if not prices:
                continue
            data = pd.DataFrame(prices)
            frames.append(_frame(ticker, data["date"].str[:10], data["close"], data["volume"], "tiingo",
                                 f"{directory.name}/{path.name}"))
    return frames


def yahoo_split_factor(stamps: pd.DatetimeIndex, splits: dict) -> np.ndarray:
    """Product of the split ratios (numerator/denominator) dated after each stamp."""
    factor = np.ones(len(stamps))
    for event in (splits or {}).values():
        day = pd.to_datetime(event["date"], unit="s").normalize()
        ratio = float(event["numerator"]) / float(event["denominator"])
        factor[stamps < day] *= ratio
    return factor


def read_yahoo(directories: list[Path] = YAHOO_DIRS) -> list[pd.DataFrame]:
    """Yahoo v8 charts: close and volume are split-adjusted, so raw close = close x later split
    ratios and raw volume = volume / the same (the documented split restore, holdout data
    report 5.1); dollar volume is unchanged by it."""
    frames = []
    for directory in directories:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
            stamps = pd.to_datetime(result.get("timestamp") or [], unit="s").normalize()
            if not len(stamps):
                continue
            quote = result["indicators"]["quote"][0]
            factor = yahoo_split_factor(stamps, (result.get("events") or {}).get("splits"))
            close = pd.to_numeric(pd.Series(quote["close"]), errors="coerce").values * factor
            volume = pd.to_numeric(pd.Series(quote["volume"]), errors="coerce").values / factor
            ticker = str(result.get("meta", {}).get("symbol") or path.stem).upper()
            frames.append(_frame(ticker, stamps, close, volume, "yahoo", f"{directory.name}/{path.name}"))
    return frames


def read_stored(directory: Path = STORED_DIR) -> list[pd.DataFrame]:
    """The stored repo files: close x volume is the raw dollar volume; the close itself stays an
    estimate (split- and partly dividend-adjusted), see the module notes."""
    frames = []
    for path in sorted(directory.glob("*.csv")):
        ticker = path.stem.upper().split("_")[0]
        data = pd.read_csv(path, usecols=lambda c: c in ("date", "close", "volume"))
        if not {"date", "close", "volume"} <= set(data.columns):
            continue
        frames.append(_frame(ticker, data["date"], data["close"], data["volume"], "stored", path.name))
    return frames


def assign_securities(frames: list[pd.DataFrame], ticker_map: TickerMap) -> pd.DataFrame:
    """Every source row with its security (rows no security held are dropped)."""
    out = []
    for frame in frames:
        if frame.empty:
            continue
        owner, direct = ticker_map.assign(frame["ticker"].iloc[0], frame["date"].values)
        keep = owner != ""
        if keep.any():
            part = frame[keep].copy()
            part["security_id"], part["direct"] = owner[keep], direct[keep]
            out.append(part)
    if not out:
        return pd.DataFrame(columns=SERIES_COLUMNS + ["security_id", "direct"])
    return pd.concat(out, ignore_index=True)


PRICE_FILE_OWNERS = common.RAW / "sec" / "derived" / "price_file_cik_map.csv"


def predecessors(master: pd.DataFrame) -> dict[str, set[str]]:
    """security -> every security linked to it through step 4's successor links (any number of hops)."""
    parent = defaultdict(set)
    for sid, successor in zip(master["security_id"], master["successor_security_id"]):
        if successor:
            parent[successor].add(sid)
    out = {}
    for sid in list(parent):
        seen, todo = set(), [sid]
        while todo:
            for prior in parent.get(todo.pop(), ()):
                if prior not in seen:
                    seen.add(prior)
                    todo.append(prior)
        out[sid] = seen
    return out


def keep_stored_owner(rows: pd.DataFrame, master: pd.DataFrame, ticker_map: TickerMap,
                      owners_path: Path = PRICE_FILE_OWNERS) -> tuple[pd.DataFrame, dict]:
    """A stored file is a vendor's history of its current company under the current ticker (the
    file's CIK from step 4). A row the ticker map gave to another company is moved to the owner's
    security when the owner was listed that day under another ticker (ContextLogic's LOGC file
    over LogicBio's LOGC years; II-VI's history in COHR over old Coherent). When the owner was not
    listed yet, the row is its predecessor's history (a reorganisation: old Marvell, old Sinclair)
    and stays where it is."""
    if not owners_path.exists():
        return rows, {"stored_owner_rule": "price_file_cik_map.csv missing; not applied"}
    owners = read_csv_text(owners_path)
    owner_cik = {f: c for f, c in zip(owners["file"], owners["cik"]) if c}
    cik_of = dict(zip(master["security_id"], master["cik"]))
    securities_of = defaultdict(list)
    for sid, cik in cik_of.items():
        securities_of[cik].append(sid)
    links = predecessors(master)
    rows = rows.reset_index(drop=True)
    files, owner_rows = rows["file"].values, rows["security_id"].values
    stored = np.flatnonzero(rows["src"].eq("stored").values)
    file_cik = pd.Series(files[stored]).map(owner_cik).fillna("").values
    row_cik = pd.Series(owner_rows[stored]).map(cik_of).fillna("").values
    mismatch = stored[(file_cik != "") & (file_cik != row_cik)]
    moved = dropped = 0
    keep = np.ones(len(rows), dtype=bool)
    new_owner = owner_rows.copy()
    dates = rows["date"].values.astype("datetime64[D]")
    for k in mismatch:
        cik, sid = owner_cik[files[k]], owner_rows[k]
        mine = securities_of.get(cik, [])
        if any(sid in links.get(own, ()) for own in mine):
            continue
        day = dates[k]
        target = [own for own in mine if own in ticker_map.security_span
                  and ticker_map.security_span[own][0] <= day <= ticker_map.security_span[own][1]]
        if len(target) == 1:
            new_owner[k] = target[0]
            moved += 1
        elif len(target) > 1:
            keep[k] = False  # a multi-class owner: no way to tell which class the file is
            dropped += 1
    changed = new_owner != owner_rows
    rows = rows.assign(security_id=new_owner, direct=rows["direct"].values & ~changed)[keep].copy()
    return rows.drop_duplicates(["security_id", "date", "src", "file"]), {
        "stored_rows_moved_to_file_owner": int(moved), "stored_rows_dropped_not_owner": int(dropped)}


def best_rows(rows: pd.DataFrame) -> pd.DataFrame:
    """One row per security-day: vendor before stored (WIKI, Tiingo, Yahoo, stored), direct
    before continuity rows. ``n_src`` counts the sources that had the day."""
    rows = rows.assign(rank=rows["src"].map(SOURCE_RANK), indirect=~rows["direct"].astype(bool))
    rows = rows.sort_values(["security_id", "date", "rank", "indirect"], kind="stable")
    counts = rows.groupby(["security_id", "date"])["src"].nunique().rename("n_src")
    best = rows.drop_duplicates(["security_id", "date"], keep="first").set_index(["security_id", "date"])
    best = best.join(counts).reset_index()
    best["dv"] = best["close"] * best["volume"]
    best["vendor"] = best["src"].isin(VENDOR)
    return best.drop(columns=["rank", "indirect"])


# ------------------------------------------------------------------ Wayback company lists

def load_company_lists(ticker_map: TickerMap, index_path: Path = SNAPSHOT_INDEX) -> pd.DataFrame:
    """Company-list rows mapped to securities: snapshot_date, as_of_session, security_id, ticker,
    last_sale, market_cap, as_of_check. Only rows inside a Nasdaq listing span of the ticker
    count (the lists also carry NYSE and OTC names)."""
    index = read_csv_text(index_path)
    index = index[index["source"] == "wayback_companylist"]
    out = []
    for row in index.itertuples(index=False):
        path = MAIN / row.snapshot_file
        if not path.exists():
            continue
        data = pd.read_csv(path, dtype=str, keep_default_na=False)
        symbols = data["Symbol"].str.strip().str.upper()
        day = np.datetime64(row.snapshot_date)
        for symbol, last_sale, cap in zip(symbols, data["LastSale"], data["MarketCap Raw"]):
            owner, direct = ticker_map.assign(symbol, [day])
            if owner[0] and direct[0]:
                out.append((row.snapshot_date, row.as_of_session or row.snapshot_date, owner[0], symbol,
                            pd.to_numeric(str(last_sale).lstrip("$"), errors="coerce"),
                            pd.to_numeric(cap, errors="coerce"), row.as_of_check))
    frame = pd.DataFrame(out, columns=["snapshot_date", "as_of_session", "security_id", "ticker", "last_sale",
                                       "market_cap", "as_of_check"])
    return frame.drop_duplicates(["snapshot_date", "security_id"], keep="first")


def wiki_entity_check(rows: pd.DataFrame, lists: pd.DataFrame) -> pd.DataFrame:
    """Plan 4.4 R9, the level part: per (security, WIKI file), WIKI raw close on a company list's
    as-of session against its LastSale. Within 2% (5% where the as-of session is unverified)
    counts as agreeing; a file with 2+ comparisons of which fewer than half agree fails."""
    wiki = rows[rows["src"] == "wiki"][["security_id", "file", "date", "close"]]
    quotes = lists[lists["last_sale"] > 0].assign(date=lambda f: pd.to_datetime(f["as_of_session"]))
    joined = wiki.merge(quotes[["security_id", "date", "last_sale", "as_of_check"]], on=["security_id", "date"])
    if joined.empty:
        return pd.DataFrame(columns=["security_id", "file", "n_compared", "n_agree", "fails"])
    band = np.where(joined["as_of_check"] == "confirmed", 0.02, 0.05)
    joined["agree"] = (joined["close"] / joined["last_sale"] - 1.0).abs() <= band
    result = joined.groupby(["security_id", "file"]).agg(n_compared=("agree", "size"), n_agree=("agree", "sum"))
    result = result.reset_index()
    result["fails"] = (result["n_compared"] >= 2) & (result["n_agree"] < 0.5 * result["n_compared"])
    return result


def stored_dv_check(rows: pd.DataFrame) -> dict:
    """Stored close x volume against WIKI raw close x volume on the same security-days, by year
    (median and 5th/95th percentiles of the ratio): the basis for using stored dollar volume."""
    wiki = rows[(rows["src"] == "wiki") & rows["direct"]][["security_id", "date", "close", "volume"]]
    stored = rows[(rows["src"] == "stored") & rows["direct"]][["security_id", "date", "close", "volume"]]
    joined = wiki.merge(stored, on=["security_id", "date"], suffixes=("_w", "_s"))
    joined = joined[(joined["volume_w"] > 0) & (joined["volume_s"] > 0)]
    ratio = (joined["close_s"] * joined["volume_s"]) / (joined["close_w"] * joined["volume_w"])
    by_year = ratio.groupby(joined["date"].dt.year)
    return {"security_days": int(len(joined)), "securities": int(joined["security_id"].nunique()),
            "by_year": {int(y): {"median": round(float(r.median()), 4), "p05": round(float(r.quantile(0.05)), 4),
                                 "p95": round(float(r.quantile(0.95)), 4)} for y, r in by_year}}


# ------------------------------------------------------------------ stage 1: daily series

def mapping_spans(spans: pd.DataFrame) -> pd.DataFrame:
    """Spans widened back to the day after ``start_prev_absent`` so rows of an IPO (or a rename)
    between two snapshots are assigned; ``extend_starts`` then keeps only what prices show."""
    widened = spans.copy()
    has_prev = widened["start_prev_absent"] != ""
    widened.loc[has_prev, "list_start"] = [
        (pd.Timestamp(d) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") for d in widened.loc[has_prev, "start_prev_absent"]]
    widened["list_start"] = np.minimum(widened["list_start"], spans["list_start"])
    return widened


def extend_starts(spans: pd.DataFrame, best: pd.DataFrame) -> pd.DataFrame:
    """The IPO rule of plan 3.1: an interval starts at its first price row after the snapshot
    that did not show it, when that row comes before the first snapshot that does."""
    first_rows = best.groupby("security_id")["date"].apply(lambda d: np.sort(d.values))
    starts = []
    for row in spans.itertuples(index=False):
        start = row.list_start
        if row.start_prev_absent and row.security_id in first_rows.index:
            dates = first_rows[row.security_id]
            low = np.datetime64(row.start_prev_absent) + np.timedelta64(1, "D")
            inside = dates[(dates >= low) & (dates < np.datetime64(row.list_start))]
            if len(inside):
                start = str(inside[0])[:10]
        starts.append(start)
    return spans.assign(list_start=starts)


def build_daily(master: pd.DataFrame, intervals: pd.DataFrame) -> dict:
    spans = listing_spans(intervals, master)
    ticker_map = TickerMap(mapping_spans(spans))
    tickers = ticker_map.tickers()
    log(f"spans: {len(spans)} intervals, {spans['security_id'].nunique()} securities, {len(tickers)} tickers")
    frames = read_wiki(tickers)
    log(f"wiki files: {len(frames)}")
    frames += read_tiingo(tickers)
    frames += read_yahoo()
    log(f"vendor files (wiki+tiingo+yahoo): {len(frames)}")
    stored = read_stored()
    log(f"stored files: {len(stored)}")
    rows = assign_securities(frames + stored, ticker_map)
    log(f"assigned rows: {len(rows)} ({rows['src'].value_counts().to_dict()})")
    rows, owner_facts = keep_stored_owner(rows, master, ticker_map)
    log(f"stored owner rule: {owner_facts}")
    lists = load_company_lists(ticker_map)
    log(f"company-list rows mapped: {len(lists)}")
    entity = wiki_entity_check(rows, lists)
    failing = set(zip(entity.loc[entity["fails"], "security_id"], entity.loc[entity["fails"], "file"]))
    dropped = rows["src"].eq("wiki") & pd.Series(list(zip(rows["security_id"], rows["file"]))).isin(failing).values
    rows_kept = rows[~dropped]
    dv_check = stored_dv_check(rows_kept)
    best = best_rows(rows_kept)
    spans = extend_starts(spans, best)
    files = (rows.groupby(["security_id", "src", "file"])
             .agg(first=("date", "min"), last=("date", "max"), rows=("date", "size"), direct=("direct", "mean"))
             .reset_index())
    files["first"], files["last"] = files["first"].dt.strftime("%Y-%m-%d"), files["last"].dt.strftime("%Y-%m-%d")
    files["entity_check_failed"] = [(s, f) in failing for s, f in zip(files["security_id"], files["file"])]
    facts = {"intervals": int(len(spans)), "securities_with_rows": int(best["security_id"].nunique()),
             "rows_by_source": {k: int(v) for k, v in rows["src"].value_counts().items()},
             "best_rows_by_source": {k: int(v) for k, v in best["src"].value_counts().items()},
             "wiki_entity_check": {"files_compared": int(len(entity)), "files_failed": int(entity["fails"].sum()),
                                   "rows_dropped": int(dropped.sum()),
                                   "failed": [f"{s}:{f}" for s, f in sorted(failing)]},
             "stored_dv_check": dv_check, **owner_facts}
    return {"spans": spans, "best": best, "lists": lists, "files": files, "entity": entity, "facts": facts}


def save_daily(stage: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stage["best"].to_pickle(OUT / "daily_series.pkl")
    for name in ("spans", "lists", "files", "entity"):
        common.atomic_write(OUT / f"{name}.csv.gz", gzip.compress(stage[name].to_csv(index=False).encode(), mtime=0))
    common.atomic_write(OUT / "daily_facts.json", (json.dumps(stage["facts"], indent=1, default=str) + "\n").encode())


def load_daily() -> dict:
    read = lambda name: pd.read_csv(OUT / f"{name}.csv.gz", dtype=str, keep_default_na=False)
    stage = {name: read(name) for name in ("spans", "lists", "files", "entity")}
    stage["lists"]["last_sale"] = pd.to_numeric(stage["lists"]["last_sale"], errors="coerce")
    stage["lists"]["market_cap"] = pd.to_numeric(stage["lists"]["market_cap"], errors="coerce")
    stage["best"] = pd.read_pickle(OUT / "daily_series.pkl")
    stage["facts"] = json.loads((OUT / "daily_facts.json").read_text())
    return stage


# ------------------------------------------------------------------ stage 2: weekly metrics

SRC_CODES = {"wiki": 0, "tiingo": 1, "yahoo": 2, "stored": 3}
SRC_NAMES = {v: k for k, v in SRC_CODES.items()}


def listed_weeks(spans: pd.DataFrame, master: pd.DataFrame, foreign: dict, weeks: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per (security, week end) it is listed on: the ticker of the covering interval,
    whether that interval's name is non-common, and whether the security is a foreign filer then."""
    values = weeks.values.astype("datetime64[D]")
    rows = []
    for row in spans.sort_values("list_start").itertuples(index=False):
        low = np.searchsorted(values, np.datetime64(row.list_start), "left")
        high = np.searchsorted(values, np.datetime64(row.list_end), "right")
        if high > low:
            bad = non_common_interval(row.name, row.share_class)
            for k in range(low, high):
                rows.append((row.security_id, k, row.ticker, bad))
    frame = pd.DataFrame(rows, columns=["security_id", "week_index", "ticker", "non_common"])
    frame = frame.drop_duplicates(["security_id", "week_index"], keep="last")
    frame["week_end"] = weeks[frame["week_index"].values]
    days = frame["week_end"].dt.strftime("%Y-%m-%d")
    frame["foreign"] = [is_foreign_on(foreign.get(s), d) for s, d in zip(frame["security_id"], days)]
    return frame.reset_index(drop=True)


def wide_panels(best: pd.DataFrame, sessions: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Session x security panels: dv, close, source code, vendor flag (NaN where no row)."""
    best = best[best["date"].isin(sessions)]
    pivot = lambda column: best.pivot(index="date", columns="security_id", values=column).reindex(sessions)
    panels = {"dv": pivot("dv"), "close": pivot("close")}
    panels["src"] = best.assign(code=best["src"].map(SRC_CODES).astype(float)).pivot(
        index="date", columns="security_id", values="code").reindex(sessions)[panels["dv"].columns]
    panels["close"] = panels["close"][panels["dv"].columns]
    return panels


def series_metrics(panels: dict[str, pd.DataFrame], weeks: pd.DatetimeIndex) -> dict[str, pd.DataFrame]:
    """Week-end x security panels: dv20/dv50 medians (minimum observations in DV_WINDOWS), the last
    close within CLOSE_STALE_SESSIONS and its source, the vendor-only close, and vendor rows in 50."""
    out = {}
    for window, minimum in DV_WINDOWS.items():
        out[f"dv{window}"] = panels["dv"].rolling(window, min_periods=minimum).median().reindex(weeks)
        log(f"dv{window} medians done")
    out["close"] = panels["close"].ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    out["src"] = panels["src"].ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    vendor = panels["src"].lt(SRC_CODES["stored"])
    out["vendor_close"] = panels["close"].where(vendor).ffill(limit=CLOSE_STALE_SESSIONS).reindex(weeks)
    out["vendor_n50"] = vendor.astype(float).rolling(50, min_periods=1).sum().reindex(weeks)
    out["n50"] = panels["dv"].notna().astype(float).rolling(50, min_periods=1).sum().reindex(weeks)
    return out


def take(panel: pd.DataFrame, weeks_idx: np.ndarray, securities: pd.Series) -> np.ndarray:
    """panel values at (week position, security) pairs; NaN where the security has no column."""
    columns = {s: k for k, s in enumerate(panel.columns)}
    positions = securities.map(columns)
    values = np.full(len(securities), np.nan)
    ok = positions.notna().values
    values[ok] = panel.values[weeks_idx[ok], positions[ok].astype(int).values]
    return values


def price_flag(close: np.ndarray, src: np.ndarray) -> np.ndarray:
    """Y/N from a vendor raw close; from a stored close (an estimate that later forward splits
    and dividends only push down) Y at $10 or more, else U (unknown); '' with no close."""
    flag = np.full(len(close), "", dtype=object)
    has = ~np.isnan(close)
    vendor = has & (src < SRC_CODES["stored"])
    stored = has & (src == SRC_CODES["stored"])
    flag[vendor] = np.where(close[vendor] >= MIN_PRICE, "Y", "N")
    flag[stored] = np.where(close[stored] >= MIN_PRICE, "Y", "U")
    return flag


def rank_within_weeks(frame: pd.DataFrame, value: str, eligible: pd.Series) -> pd.Series:
    """Descending rank of ``value`` within each week among ``eligible`` rows (NaN elsewhere)."""
    ranks = frame.loc[eligible].groupby("week_end")[value].rank(ascending=False, method="first")
    return ranks.reindex(frame.index)


def weekly_table(stage: dict, master: pd.DataFrame, foreign: dict, sessions: pd.DatetimeIndex,
                 weeks: pd.DatetimeIndex) -> pd.DataFrame:
    weekly = listed_weeks(stage["spans"], master, foreign, weeks)
    log(f"listed security-weeks: {len(weekly)}")
    panels = wide_panels(stage["best"], sessions)
    metrics = series_metrics(panels, weeks)
    idx, securities = weekly["week_index"].values, weekly["security_id"]
    for name, panel in metrics.items():
        weekly[name] = take(panel, idx, securities)
    weekly["src"] = [SRC_NAMES.get(int(c), "") if not np.isnan(c) else "" for c in weekly["src"].values]
    codes = weekly["src"].map(SRC_CODES).fillna(-1).values
    weekly["price_ge_10"] = price_flag(weekly["close"].values, codes)
    # Covered by a vendor raw series: a vendor close this week, and vendor rows make up at least
    # 80% of the rows in the 50-session window (so a new listing's first weeks count too).
    weekly["vendor_ok"] = weekly["vendor_close"].notna() & (weekly["vendor_n50"] >= 0.8 * weekly["n50"])
    weekly["universe"] = ~weekly["foreign"] & ~weekly["non_common"]
    priced = weekly["universe"] & weekly["price_ge_10"].isin(["Y", "U"])
    weekly["dv50_rank"] = rank_within_weeks(weekly, "dv50", priced & weekly["dv50"].notna())
    weekly["dv20_rank"] = rank_within_weeks(weekly, "dv20", priced & weekly["dv20"].notna())
    weekly["dv50_rank_vendor_only"] = rank_within_weeks(weekly, "dv50", priced & weekly["dv50"].notna()
                                                        & weekly["vendor_ok"])
    return weekly.drop(columns=["week_index"])


TRADING_BOUND_DAYS = 30


def mark_trading_bounds(weekly: pd.DataFrame, spans: pd.DataFrame, best: pd.DataFrame) -> pd.DataFrame:
    """``outside_trading``: a listed week after the last row of every source when the listing ends
    within 30 days of that row (between the last trade and the Form 25 taking effect), or before
    the first row when trading starts within 30 days of the first listing. Such weeks have no
    prices to fetch, so they never count as uncovered."""
    first_row = best.groupby("security_id")["date"].min()
    last_row = best.groupby("security_id")["date"].max()
    listed = spans.groupby("security_id").agg(first=("list_start", "min"), last=("list_end", "max"))
    first_listed = pd.to_datetime(weekly["security_id"].map(listed["first"]))
    last_listed = pd.to_datetime(weekly["security_id"].map(listed["last"]))
    first, last = weekly["security_id"].map(first_row), weekly["security_id"].map(last_row)
    ended = (weekly["week_end"] > last) & ((last_listed - last).dt.days <= TRADING_BOUND_DAYS)
    unstarted = (weekly["week_end"] < first) & ((first - first_listed).dt.days <= TRADING_BOUND_DAYS)
    return weekly.assign(outside_trading=(ended | unstarted).fillna(False).astype(bool))


# ------------------------------------------------------------------ Tiingo supported tickers

TIINGO_START_SLACK_DAYS = 7
# A delisted name stops trading up to about ten sessions before its Form 25 takes effect.
TIINGO_END_SLACK_DAYS = 21


def supported_tickers_path(day: str | None = None) -> Path:
    existing = sorted(OUT.glob("supported_tickers_*.zip"))
    if existing and day is None:
        return existing[-1]
    return OUT / f"supported_tickers_{day or datetime.now(timezone.utc).strftime('%Y-%m-%d')}.zip"


def load_supported_tickers(offline: bool = False) -> pd.DataFrame:
    """Tiingo's public ticker list (no key; fetched once through ``cached_get``, then from cache)."""
    path = supported_tickers_path()
    if not path.exists():
        if offline:
            raise FileNotFoundError(f"{path} is not cached and --offline was given")
        common.cached_get(TIINGO_SUPPORTED_URL, path, source="tiingo_supported_tickers",
                          headers={"User-Agent": "quant_stocks research"}, symbol="supported_tickers")
    with zipfile.ZipFile(path) as archive:
        name = [n for n in archive.namelist() if n.endswith(".csv")][0]
        frame = pd.read_csv(archive.open(name), dtype=str, keep_default_na=False)
    frame["ticker"] = frame["ticker"].str.upper().str.strip()
    return frame


def tiingo_index(supported: pd.DataFrame) -> dict[str, list[dict]]:
    """ticker -> rows (exchange, start, end) of US-dollar stocks."""
    stocks = supported[(supported["assetType"].str.lower() == "stock") & (supported["priceCurrency"].isin(["USD", ""]))]
    index = defaultdict(list)
    for row in stocks.itertuples(index=False):
        index[row.ticker].append({"exchange": row.exchange, "start": row.startDate[:10], "end": row.endDate[:10]})
    return index


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def tiingo_range_match(tickers: list[str], need_start: str, need_end: str, index: dict[str, list[dict]]) -> dict:
    """Plan 3.2: a single supported_tickers row whose start and end cover the needed interval.

    Tickers are tried in the order given (the security's own tickers, newest first). Result
    ``match``: Y (a row covers it), partial (the best row covers part of it), wrong_entity
    (the ticker has rows, none overlapping the need: a reused ticker held by another company),
    no_row. ``reused`` is set when the ticker also has a row starting after the need ends
    (the request may return the newer company; check the entity after the fetch).
    """
    best = {"match": "no_row", "ticker": tickers[0] if tickers else "", "row_start": "", "row_end": "",
            "coverage": 0.0, "reused": False, "exchange": ""}
    need_days = max(_days(need_start, need_end), 1)
    for ticker in tickers:
        rows = [r for r in index.get(ticker, []) if r["start"] and r["end"]]
        if not rows:
            continue
        reused = any(r["start"] > need_end for r in rows)
        for r in rows:
            covers = (_days(r["start"], need_start) >= -TIINGO_START_SLACK_DAYS
                      and _days(need_end, r["end"]) >= -TIINGO_END_SLACK_DAYS)
            overlap = max(0, _days(max(r["start"], need_start), min(r["end"], need_end)))
            coverage = 1.0 if covers else min(1.0, overlap / need_days)
            candidate = {"match": "Y" if covers else ("partial" if overlap > 0 else "wrong_entity"),
                         "ticker": ticker, "row_start": r["start"], "row_end": r["end"],
                         "coverage": round(coverage, 4), "reused": reused, "exchange": r["exchange"]}
            order = {"Y": 3, "partial": 2, "wrong_entity": 1, "no_row": 0}
            if (order[candidate["match"]], candidate["coverage"]) > (order[best["match"]], best["coverage"]):
                best = candidate
        if best["match"] == "Y":
            break
    return best


# ------------------------------------------------------------------ stage 3: proxies

MAX_PROXY_FLOAT = 3e12  # larger XBRL floats are unit errors (SeqLL $4.8T)
PROXY_CARRY_DAYS = 365  # plan 3.3: proxies are carried forward up to 12 months


def interval_flags(spans: pd.DataFrame) -> dict[tuple[str, str], bool]:
    """(security, ticker) -> whether that listing's name is non-common."""
    return {(s, t): non_common_interval(n, c) for s, t, n, c in
            zip(spans["security_id"], spans["ticker"], spans["name"], spans["share_class"])}


def mcap_ranks(lists: pd.DataFrame, spans: pd.DataFrame, foreign: dict) -> pd.DataFrame:
    """Company-list market-cap rank per capture date among universe names (non-foreign then, common)."""
    flags = interval_flags(spans)
    frame = lists[lists["market_cap"] > 0].copy()
    frame["universe"] = [not flags.get((s, t), False) and not is_foreign_on(foreign.get(s), d)
                         for s, t, d in zip(frame["security_id"], frame["ticker"], frame["snapshot_date"])]
    frame = frame[frame["universe"]]
    frame["mcap_rank"] = frame.groupby("snapshot_date")["market_cap"].rank(ascending=False, method="first")
    return frame


# A float of $20B or more is kept only when its shares count gives at most $3,000 a share
# (x1000 unit errors: Codiak $100.8B and $412.7B, Sientra $108.8B-$442.7B).
BIG_FLOAT, MAX_FLOAT_PER_SHARE = 2e10, 3000.0
FLOAT_JUMP_RATIO = 50
SHARES_MATCH_DAYS = 180


def float_facts(offline: bool = True) -> pd.DataFrame:
    """XBRL dei:EntityPublicFloat from the cached frames (step 3), plausible values only: positive,
    at most $3T, and a float of $20B or more only with a shares count (nearest
    EntityCommonStockSharesOutstanding within 180 days) giving <= $3,000 a share."""
    from scripts.reversal_data_form25 import fetch_float_frames, frame_periods

    floats = fetch_float_frames("EntityPublicFloat", "USD", frame_periods(), offline=offline)
    floats = floats[(floats["val"] > 0) & (floats["val"] <= MAX_PROXY_FLOAT) & floats["end"].notna()]
    floats = floats.sort_values(["cik", "end"]).drop_duplicates(["cik", "end"], keep="last")[["cik", "end", "val"]]
    shares = fetch_float_frames("EntityCommonStockSharesOutstanding", "shares", frame_periods(), offline=offline)
    shares = shares[(shares["val"] > 0) & shares["end"].notna()].rename(columns={"val": "shares"})
    floats = floats.astype({"end": "datetime64[ns]"}).sort_values("end")
    shares = shares[["cik", "end", "shares"]].astype({"end": "datetime64[ns]"}).sort_values("end")
    joined = pd.merge_asof(floats, shares, on="end", by="cik", direction="nearest",
                           tolerance=pd.Timedelta(days=SHARES_MATCH_DAYS))
    big = joined["val"] >= BIG_FLOAT
    bad = big & (joined["shares"].isna() | (joined["val"] / joined["shares"] > MAX_FLOAT_PER_SHARE))
    # A fact of $1B or more that is 50 times the lower median of the CIK's other facts is a unit
    # error too (Astrotech $55B among $10M-$50M floats, Vintage Wine Estates $35B).
    def others_lower_median(values: pd.Series) -> pd.Series:
        array = values.to_numpy()
        out = np.full(len(array), np.nan)
        for k in range(len(array)):
            rest = np.sort(np.delete(array, k))
            if len(rest):
                out[k] = rest[(len(rest) - 1) // 2]
        return pd.Series(out, index=values.index)

    reference = joined.groupby("cik", group_keys=False)["val"].apply(others_lower_median)
    bad |= (joined["val"] >= 1e9) & (joined["val"] > FLOAT_JUMP_RATIO * reference.reindex(joined.index))
    return joined[~bad].sort_values(["cik", "end"])[["cik", "end", "val"]].reset_index(drop=True)


def attach_proxies(weekly: pd.DataFrame, master: pd.DataFrame, ranks: pd.DataFrame, floats: pd.DataFrame) -> pd.DataFrame:
    """Add ``mcap`` (latest company-list market cap of the security within 12 months before) and
    ``float_usd`` (the CIK's XBRL public float measured within 12 months either side, nearest)."""
    cik = dict(zip(master["security_id"], master["cik"].astype(int)))
    weekly = weekly.assign(cik=weekly["security_id"].map(cik).astype("Int64"), _order=np.arange(len(weekly)),
                           week_end=weekly["week_end"].astype("datetime64[ns]"))
    caps = ranks.assign(week_end=pd.to_datetime(ranks["snapshot_date"]).astype("datetime64[ns]"))[
        ["security_id", "week_end", "market_cap"]]
    left = weekly[["security_id", "week_end", "_order"]].sort_values("week_end")
    merged = pd.merge_asof(left, caps.sort_values("week_end"), on="week_end", by="security_id", direction="backward",
                           tolerance=pd.Timedelta(days=PROXY_CARRY_DAYS))
    weekly["mcap"] = merged.set_index("_order")["market_cap"].reindex(weekly["_order"]).values
    left = weekly[["cik", "week_end", "_order"]].dropna(subset=["cik"]).astype({"cik": int}).sort_values("week_end")
    facts = floats.rename(columns={"end": "week_end"}).astype({"cik": int, "week_end": "datetime64[ns]"})
    facts = facts.sort_values("week_end")
    merged = pd.merge_asof(left, facts, on="week_end", by="cik", direction="nearest",
                           tolerance=pd.Timedelta(days=PROXY_CARRY_DAYS))
    weekly["float_usd"] = merged.set_index("_order")["val"].reindex(weekly["_order"]).values
    return weekly.drop(columns=["_order"])


def proxy_cutoffs(weekly: pd.DataFrame, low: int = 200, high: int = 250) -> pd.DataFrame:
    """Per week, the median market cap and the median float of the names ranked ``low``-``high``
    by dv50 (plan 3.3 check 1)."""
    band = weekly[(weekly["dv50_rank"] >= low) & (weekly["dv50_rank"] <= high)]
    return band.groupby("week_end").agg(cut_mcap=("mcap", "median"), cut_float=("float_usd", "median"))


def proxy_above(weekly: pd.DataFrame, cutoffs: pd.DataFrame) -> pd.Series:
    """True where a row's market cap (or, without one, its float) reaches that week's cutoff."""
    cut = cutoffs.reindex(weekly["week_end"]).reset_index(drop=True)
    mcap, flt = weekly["mcap"].reset_index(drop=True), weekly["float_usd"].reset_index(drop=True)
    above = (mcap >= cut["cut_mcap"]) | (mcap.isna() & (flt >= cut["cut_float"]))
    return pd.Series(above.fillna(False).values, index=weekly.index)


# ------------------------------------------------------------------ stage 4: per-security facts

TRADING_EXCHANGES = {"Nasdaq", "NYSE", "CBOE"}
# Successor routing needs this many predecessor sessions in the successor's stored file, dated
# this many days before the successor's own first listing (not a few overlap days at a handover).
SUCCESSOR_MIN_ROWS, SUCCESSOR_LEAD_DAYS = 120, 90
WARMUP_DAYS = 75  # calendar days before the first uncovered week (50 sessions for the median)
HOLD_DAYS = 28  # an exchange move keeps a vendor series up to 4 weeks after it (plan 4.5)


def sec_current_tickers(row) -> list[str]:
    """The CIK's current SEC tickers on Nasdaq, NYSE or CBOE (warrants and units dropped)."""
    tickers, exchanges = row.tickers_sec_current.split(), row.exchanges_sec_current.split()
    if len(exchanges) != len(tickers):
        exchanges = [""] * len(tickers)
    return [t for t, x in zip(tickers, exchanges) if x in TRADING_EXCHANGES and not WARRANT_LIKE.search(t)]


def security_facts(weekly: pd.DataFrame, spans: pd.DataFrame, master: pd.DataFrame, best: pd.DataFrame,
                   form25: pd.DataFrame) -> pd.DataFrame:
    """One row per security: listing dates, activity, coverage and rank facts used by the rules."""
    universe = weekly[weekly["universe"]]
    missing = ~universe["vendor_ok"] & ~universe["outside_trading"]
    uncovered = universe[missing]
    rank = universe[["dv50_rank", "dv20_rank"]].min(axis=1)
    facts = pd.DataFrame({
        "universe_weeks": universe.groupby("security_id").size(),
        "first_universe_week": universe.groupby("security_id")["week_end"].min(),
        "last_universe_week": universe.groupby("security_id")["week_end"].max(),
        "uncovered_weeks": uncovered.groupby("security_id").size(),
        "first_uncovered": uncovered.groupby("security_id")["week_end"].min(),
        "last_uncovered": uncovered.groupby("security_id")["week_end"].max(),
        "best_rank": rank.groupby(universe["security_id"]).min(),
        "best_rank_uncovered": rank[missing].groupby(uncovered["security_id"]).min(),
        "uncovered_unpriced_weeks": uncovered["dv50"].isna().groupby(uncovered["security_id"]).sum(),
    })
    a1 = universe["week_end"].between(*A1_WINDOW)
    facts["best_rank_a1"] = rank[a1].groupby(universe.loc[a1, "security_id"]).min()
    facts["uncovered_weeks"] = facts["uncovered_weeks"].fillna(0).astype(int)
    by_security = spans.sort_values("list_start").groupby("security_id")
    facts = facts.join(pd.DataFrame({
        "first_listed": by_security["list_start"].min(), "last_listed": by_security["list_end"].max(),
        "last_ticker": by_security["ticker"].last(),
        "tickers": by_security["ticker"].apply(lambda t: " ".join(dict.fromkeys(reversed(list(t))))),
    }), how="outer")
    last_held = spans.groupby(["security_id", "ticker"])["list_end"].max().reset_index()
    facts["ticker_last_held"] = last_held.groupby("security_id").apply(
        lambda g: " ".join(f"{t}:{e}" for t, e in zip(g["ticker"], g["list_end"])), include_groups=False)
    for column in ("uncovered_weeks", "universe_weeks", "uncovered_unpriced_weeks"):
        facts[column] = facts[column].fillna(0).astype(int)
    first_price = best.groupby("security_id")["date"].min()
    facts["first_price"] = first_price.reindex(facts.index)
    info = master.set_index("security_id")
    facts = facts.join(info[["cik", "name", "share_class", "delist_date", "delist_form25_accession", "foreign_filer",
                             "transfer_date", "tickers_sec_current", "exchanges_sec_current"]], how="left")
    floats = form25.set_index("accession")[["public_float_usd", "float_check_flag", "implied_float_per_share",
                                             "classification"]]
    facts = facts.join(floats, on="delist_form25_accession")
    facts["public_float_usd"] = pd.to_numeric(facts["public_float_usd"], errors="coerce")
    facts["active_nasdaq"] = facts["last_listed"].eq(WINDOW_END)
    current = {r.security_id: sec_current_tickers(r) for r in master.itertuples(index=False)}
    facts["yahoo_tickers"] = [" ".join(current.get(s, [])) for s in facts.index]
    facts["active"] = facts["active_nasdaq"] | ((facts["delist_date"].fillna("") == "") & (facts["yahoo_tickers"] != ""))
    # A security whose step-4 successor chain reaches an active security, when the vendor keeps
    # its history under the successor's ticker: some of its rows come from a stored file the
    # successor's CIK owns (old Quidel in QuidelOrtho's QDEL, old DraftKings in DKNG). A link
    # that only hands a ticker to another company (21st Century Fox -> Fox Corp) has no such rows.
    successor = dict(zip(master["security_id"], master["successor_security_id"]))
    cik_of = dict(zip(master["security_id"], master["cik"]))
    owners = (dict(zip(read_csv_text(PRICE_FILE_OWNERS)["file"], read_csv_text(PRICE_FILE_OWNERS)["cik"]))
              if PRICE_FILE_OWNERS.exists() else {})
    stored_rows = best[best["src"] == "stored"][["security_id", "file", "date"]]
    stored_rows = stored_rows.assign(owner=stored_rows["file"].map(owners))
    via = {}
    for sid in facts.index[~facts["active"]]:
        nxt, hops = successor.get(sid, ""), 0
        while nxt and hops < 5 and nxt in facts.index and not facts.at[nxt, "active"]:
            nxt, hops = successor.get(nxt, ""), hops + 1
        if not (nxt and nxt in facts.index and facts.at[nxt, "active"]):
            continue
        heir_listed = pd.Timestamp(facts.at[nxt, "first_listed"]) - pd.Timedelta(days=SUCCESSOR_LEAD_DAYS)
        mine = stored_rows[(stored_rows["security_id"] == sid) & (stored_rows["owner"] == cik_of.get(nxt))
                           & (stored_rows["date"] < heir_listed)]
        if len(mine) >= SUCCESSOR_MIN_ROWS:
            via[sid] = nxt
    facts["via_successor"] = pd.Series(via).reindex(facts.index).fillna("")
    return facts


def needed_window(row) -> tuple[str, str]:
    """[start, end] of the dates a fetch must cover: from 75 days before the first uncovered week
    (not before the listing or 2011-06-01) to the last uncovered week, or to the end of the
    listing when the gap runs to it (plus 4 weeks after an exchange move, to 2026-08-31 at most)."""
    start = max(WINDOW_START, str(row.first_listed)[:10],
                (row.first_uncovered - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d"))
    if (row.last_universe_week - row.last_uncovered).days <= 7:
        end = str(row.last_listed)[:10]
        if isinstance(row.transfer_date, str) and row.transfer_date:
            end = (pd.Timestamp(end) + pd.Timedelta(days=HOLD_DAYS)).strftime("%Y-%m-%d")
    else:
        end = row.last_uncovered.strftime("%Y-%m-%d")
    return start, min(end, WINDOW_END)


# ------------------------------------------------------------------ split / dividend flags (V sample)

def _ordinary_ratio(value: float, tolerance: float = 0.001) -> bool:
    for denominator in range(1, 11):
        numerator = round(value * denominator)
        if 1 <= numerator <= 10 and abs(numerator / denominator / value - 1) <= tolerance:
            return True
    return False


def event_flags() -> pd.DataFrame:
    """Single-stock data flags for the V sample: ticker, date, kind (odd_split, split_after_2023,
    special_dividend), value, source. From the cached Yahoo and Tiingo files and WIKI's tables."""
    rows = []
    for directory in YAHOO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            result = json.loads(path.read_text(encoding="utf-8"))["chart"]["result"][0]
            ticker = str(result.get("meta", {}).get("symbol") or path.stem).upper()
            events = result.get("events") or {}
            stamps = pd.to_datetime(result.get("timestamp") or [], unit="s").normalize()
            close = pd.Series(result["indicators"]["quote"][0]["close"], index=stamps, dtype=float)
            close = close * yahoo_split_factor(stamps, events.get("splits"))
            for event in (events.get("splits") or {}).values():
                day = pd.to_datetime(event["date"], unit="s").normalize()
                ratio = float(event["numerator"]) / float(event["denominator"])
                if not _ordinary_ratio(ratio):
                    rows.append((ticker, day, "odd_split", ratio, "yahoo"))
                if day >= pd.Timestamp("2024-01-01"):
                    rows.append((ticker, day, "split_after_2023", ratio, "yahoo"))
            for event in (events.get("dividends") or {}).values():
                day = pd.to_datetime(event["date"], unit="s").normalize()
                prior = close[close.index < day].dropna()
                factor = yahoo_split_factor(pd.DatetimeIndex([day]), events.get("splits"))[0]
                if len(prior) and float(event["amount"]) * factor > 0.10 * prior.iloc[-1]:
                    rows.append((ticker, day, "special_dividend", float(event["amount"]) * factor / prior.iloc[-1], "yahoo"))
    for directory in TIINGO_DIRS:
        for path in sorted(directory.glob("*.json")) if directory.exists() else []:
            payload = json.loads(path.read_text(encoding="utf-8"))
            ticker = str((payload.get("meta") or {}).get("ticker") or path.stem).upper()
            prices = pd.DataFrame(payload.get("prices") or [])
            if prices.empty:
                continue
            prices["date"] = pd.to_datetime(prices["date"].str[:10])
            prior = prices["close"].shift(1)
            for day, split, cash, before in zip(prices["date"], prices["splitFactor"], prices["divCash"], prior):
                if split != 1.0 and not _ordinary_ratio(float(split)):
                    rows.append((ticker, day, "odd_split", float(split), "tiingo"))
                if split != 1.0 and day >= pd.Timestamp("2024-01-01"):
                    rows.append((ticker, day, "split_after_2023", float(split), "tiingo"))
                if cash and before and cash > 0.10 * before:
                    rows.append((ticker, day, "special_dividend", cash / before, "tiingo"))
    wiki = common.CACHE / "wiki"
    if (wiki / "wiki_split_events.csv").exists():
        splits = pd.read_csv(wiki / "wiki_split_events.csv", dtype={"ticker": str}, keep_default_na=False)
        for ticker, day, ratio in zip(splits["ticker"], splits["date"], splits["split_ratio"]):
            if not _ordinary_ratio(float(ratio)):
                rows.append((ticker, pd.Timestamp(day), "odd_split", float(ratio), "wiki"))
    if (wiki / "wiki_dividends.csv").exists():
        dividends = pd.read_csv(wiki / "wiki_dividends.csv", dtype={"ticker": str}, keep_default_na=False)
        big = dividends[pd.to_numeric(dividends["pct_of_prior_close"], errors="coerce") > 0.10]
        for ticker, day, pct in zip(big["ticker"], big["date"], big["pct_of_prior_close"]):
            rows.append((ticker, pd.Timestamp(day), "special_dividend", float(pct), "wiki"))
    for ticker, (day, kind) in V_KNOWN_EVENTS.items():
        rows.append((ticker, pd.Timestamp(day), kind, np.nan, "plan_4.3"))
    return pd.DataFrame(rows, columns=["ticker", "date", "kind", "value", "source"])


def stored_break_names(day: str = BREAK_DATE, directory: Path = STORED_DIR) -> list[str]:
    """Stored files whose close jumps by a split-sized ratio on ``day`` (the 2025-06-24 unit break:
    rows from it on are in post-split units, earlier rows were not rescaled)."""
    from src.io.nasdaq_update import _split_ratio

    names = []
    for path in sorted(directory.glob("*.csv")):
        data = pd.read_csv(path, usecols=lambda c: c in ("date", "close"))
        data = data[(data["date"] >= "2025-06-01") & (data["date"] <= day)]
        if len(data) >= 2 and data["date"].iloc[-1] == day and data["close"].iloc[-2] > 0:
            ratio = data["close"].iloc[-1] / data["close"].iloc[-2]
            if (ratio >= 1.4 or ratio <= 1 / 1.4) and _split_ratio(1 / ratio) is not None:
                names.append(path.stem.upper())
    return names


# ------------------------------------------------------------------ stage 5: rules

REASON_PRIORITY = ["B_A_float_ge_1B", "A1_wiki_dv_rank300", "A2_mcap_rank400", "A3_float_ge_1B_2012_2018",
                   "B_B_float_500M_1B", "C_late_start", "B_C_sample_300M_500M", "V_verify_sample",
                   "Y_active_rank300", "S_stored_only_delisted_rank300", "B_C_rest_300M_500M"]
MONTH_2_REASONS = {"S_stored_only_delisted_rank300", "B_C_rest_300M_500M"}


def seeded_sample(ids: list[str], n: int, seed: int = SEED) -> list[str]:
    ids = sorted(ids)
    if len(ids) <= n:
        return ids
    rng = np.random.default_rng(seed)
    return sorted(rng.choice(ids, size=n, replace=False).tolist())


def float_usable(value: float, flag: str, per_share=None) -> bool:
    """A Form 25 float that passed step 3's check, and (at $20B or more) $3,000 a share or less."""
    if pd.isna(value) or flag not in GOOD_FLOAT_FLAGS:
        return False
    per_share = pd.to_numeric(per_share, errors="coerce")
    return not (value >= BIG_FLOAT and (pd.isna(per_share) or per_share > MAX_FLOAT_PER_SHARE))


def float_tier(value: float, flag: str, per_share=None) -> str:
    if not float_usable(value, flag, per_share):
        return ""
    for name, low, high in FLOAT_TIERS:
        if low <= value < high:
            return name
    return ""


def late_start(row) -> tuple[str, str] | None:
    """The missing window of a series that starts more than LATE_START_DAYS after the listing."""
    if pd.isna(row.first_price):
        return None
    listed = max(pd.Timestamp(WINDOW_START), pd.Timestamp(row.first_listed))
    if (row.first_price - listed).days <= LATE_START_DAYS:
        return None
    return listed.strftime("%Y-%m-%d"), (row.first_price - pd.Timedelta(days=1)).strftime("%Y-%m-%d")


def rule_hits(facts: pd.DataFrame, ranks: pd.DataFrame, floats: pd.DataFrame) -> dict[str, dict[str, tuple]]:
    """security -> {reason: (metric, value)} for every rule that holds (V is added later)."""
    hits: dict[str, dict[str, tuple]] = defaultdict(dict)
    a2 = ranks[ranks["snapshot_date"].between(*A2_WINDOW)]
    best_mcap = a2.groupby("security_id")["mcap_rank"].min()
    float_by_cik = {c: g for c, g in floats.groupby("cik")}
    caps_by_security = {s: g for s, g in ranks.groupby("security_id")}
    for sid, row in facts.iterrows():
        if row.uncovered_weeks <= 0:
            continue
        delist = row.delist_date or ""
        if pd.notna(row.best_rank_a1) and row.best_rank_a1 <= FETCH_RANK:
            hits[sid]["A1_wiki_dv_rank300"] = ("dv_rank_min_2012_2018", int(row.best_rank_a1))
        if sid in best_mcap.index and best_mcap[sid] <= MCAP_RANK:
            hits[sid]["A2_mcap_rank400"] = ("mcap_rank_min_2011_11_2019_06", int(best_mcap[sid]))
        tier = float_tier(row.public_float_usd, row.float_check_flag, row.implied_float_per_share)
        if delist and B_WINDOW[0] <= delist <= B_WINDOW[1] and tier:
            reason = {"B_A": "B_A_float_ge_1B", "B_B": "B_B_float_500M_1B", "B_C": "B_C_tier"}[tier]
            hits[sid][reason] = ("form25_max_float_3y_usd", float(row.public_float_usd))
        if (delist and "2012-01-01" <= delist <= WIKI_END
                and float_usable(row.public_float_usd, row.float_check_flag, row.implied_float_per_share)
                and row.public_float_usd >= A3_FLOAT and not hits[sid]):
            hits[sid]["A3_float_ge_1B_2012_2018"] = ("form25_max_float_3y_usd", float(row.public_float_usd))
        window = late_start(row)
        if window:
            caps = caps_by_security.get(sid)
            in_caps = caps[caps["snapshot_date"].between(*window)] if caps is not None else None
            facts_cik = float_by_cik.get(int(row.cik)) if pd.notna(row.cik) else None
            in_float = (facts_cik[(facts_cik["end"] >= window[0]) & (facts_cik["end"] <= window[1])]
                        if facts_cik is not None else None)
            reasons = []
            if in_caps is not None and len(in_caps) and in_caps["mcap_rank"].min() <= MCAP_RANK:
                reasons.append(("mcap_rank_min_missing_window", int(in_caps["mcap_rank"].min())))
            if in_float is not None and len(in_float) and in_float["val"].max() >= A3_FLOAT:
                reasons.append(("xbrl_float_max_missing_window_usd", float(in_float["val"].max())))
            if reasons:
                hits[sid]["C_late_start"] = reasons[0] + (window,)
        if ((row.active or row.via_successor) and pd.notna(row.best_rank_uncovered)
                and row.best_rank_uncovered <= FETCH_RANK):
            hits[sid]["Y_active_rank300"] = ("dv_rank_min_uncovered_weeks", int(row.best_rank_uncovered))
        if (not row.active and not row.via_successor and str(row.last_listed)[:10] > S_AFTER
                and pd.notna(row.best_rank_uncovered)
                and row.best_rank_uncovered <= FETCH_RANK):
            hits[sid]["S_stored_only_delisted_rank300"] = ("dv_rank_min_uncovered_weeks", int(row.best_rank_uncovered))
    tier_c = sorted(s for s, h in hits.items() if "B_C_tier" in h)
    # The sample tests tier C on its own, so it is drawn from tier-C names that no higher rule
    # (A, B-A, B-B, C) already fetches; the others are fetched for that rule anyway.
    higher = set(REASON_PRIORITY[:REASON_PRIORITY.index("B_C_sample_300M_500M")])
    alone = [s for s in tier_c if not (set(hits[s]) & higher)]
    sample = set(seeded_sample(alone, TIER_C_SAMPLE))
    for sid in tier_c:
        value = hits[sid].pop("B_C_tier")
        hits[sid]["B_C_sample_300M_500M" if sid in sample else "B_C_rest_300M_500M"] = value
    return {s: h for s, h in hits.items() if h}


def verification_sample(facts: pd.DataFrame, weekly: pd.DataFrame, events: pd.DataFrame,
                        breaks: list[str]) -> dict[str, str]:
    """security -> V category for 50 active names: the plan's named break names, the stored files'
    2025-06-24 break names, splits after 2023, odd split factors, special dividends above 10%
    (each by best dv rank), then a seeded draw from active names ranked <= 300 in the last year,
    in three rank buckets."""
    # Active names that are in the universe (non-foreign common stock) in the last year.
    recent_start = pd.Timestamp(LAST_WEEK) - pd.DateOffset(years=1)
    active = facts[facts["active"] & (facts["last_universe_week"] >= recent_start)]
    by_ticker = {}
    for sid, row in active.iterrows():
        # Its own Nasdaq tickers and the one Yahoo would be asked for (not every SEC ticker of the
        # CIK: those include notes and preferreds, e.g. OXLCN of Oxford Lane).
        for ticker in str(row.tickers).split() + [yahoo_ticker(row)]:
            by_ticker.setdefault(ticker, sid)
    order = active["best_rank"].fillna(1e9)
    chosen: dict[str, str] = {}

    def add(sids, category):
        for sid in sorted(set(sids), key=lambda s: order.get(s, 1e9)):
            if len(chosen) >= V_SAMPLE:
                return
            chosen.setdefault(sid, category)

    add([by_ticker[t] for t in V_NAMED if t in by_ticker], "named_break_2025_06_24")
    add([by_ticker[t] for t in breaks if t in by_ticker], "stored_break_2025_06_24")
    for kind in ("split_after_2023", "odd_split", "special_dividend"):
        add([by_ticker[t] for t in events.loc[events["kind"] == kind, "ticker"] if t in by_ticker], kind)
    if len(chosen) < V_SAMPLE:
        recent = weekly[(weekly["week_end"] >= recent_start) & weekly["universe"]
                        & weekly["security_id"].isin(active.index) & ~weekly["security_id"].isin(list(chosen))]
        best = recent.groupby("security_id")["dv50_rank"].min().dropna()
        buckets = [best[(best > lo) & (best <= hi)].index.tolist() for lo, hi in ((0, 100), (100, 200), (200, 300))]
        need = V_SAMPLE - len(chosen)
        for k, ids in enumerate(buckets):
            share = need // 3 + (1 if k < need % 3 else 0)
            for sid in seeded_sample(ids, share, SEED + k):
                chosen.setdefault(sid, f"random_rank_{k * 100 + 1}_{k * 100 + 100}")
    return chosen


# ------------------------------------------------------------------ stage 6: routing

CANDIDATE_COLUMNS = ["security_id", "ticker_for_source", "needed_start", "needed_end", "reason", "prefilter_metric",
                     "prefilter_value", "planned_source", "tiingo_range_match", "status", "fetch_month",
                     # extras
                     "reasons_all", "priority", "cik", "name", "active", "delist_date", "uncovered_weeks",
                     "best_rank", "tiingo_row_start", "tiingo_row_end", "tiingo_coverage", "tiingo_reused_ticker",
                     "v_category", "note"]
UNFILLABLE_COLUMNS = ["security_id", "ticker", "needed_start", "needed_end", "sources_tried", "est_weeks_in_top250",
                      "proxy", "proxy_value", "reason", "cik", "name", "delist_date", "status"]


def yahoo_ticker(row) -> str:
    current = str(row.yahoo_tickers).split()
    if row.active_nasdaq or not current:
        return row.last_ticker
    return row.last_ticker if row.last_ticker in current else current[0]


WARRANT_LIKE = re.compile(r"^[A-Z]{4}(W|WS|U|R)$|[-.](W|WS|U|R|WT)$")


def tiingo_tickers(row, need_start: str = "") -> list[str]:
    """The security's own Nasdaq tickers (newest first) that it still held on or after
    ``need_start`` (a vendor keeps a history under the later ticker, never under one the company
    gave up before: 21st Century Fox's old NWSA is News Corp's from 2013), then the CIK's
    current SEC tickers (warrants, units and rights dropped)."""
    held = dict(pair.rsplit(":", 1) for pair in str(row.ticker_last_held).split() if ":" in pair)
    own = [t for t in str(row.tickers).split() if not need_start or held.get(t, "9999") >= need_start]
    extra = [t for t in str(row.tickers_sec_current).split() if t not in own and not WARRANT_LIKE.search(t)]
    return own + extra


def wiki_alternative(sid: str, row, start: str, end: str, lists: pd.DataFrame) -> str:
    """A WIKI file under one of the CIK's current SEC tickers that no listing interval of this
    security names, covering at least half of [start, min(end, WIKI_END)] and agreeing with the
    company lists' LastSale: returned as 'TICKER', else ''."""
    from scripts.reversal_data_wiki import READ_KW, safe_ticker

    if start > WIKI_END:
        return ""
    stop = min(end, WIKI_END)
    for ticker in [t for t in str(row.tickers_sec_current).split() if t not in str(row.tickers).split()]:
        path = WIKI_DIR / f"{safe_ticker(ticker)}.csv.gz"
        if not path.exists():
            continue
        data = pd.read_csv(path, usecols=["ticker", "date", "close"], **READ_KW)
        inside = data[(data["date"] >= start) & (data["date"] <= stop)]
        if len(inside) < 0.5 * 252 * max(_days(start, stop), 1) / 365:
            continue
        quotes = lists[(lists["security_id"] == sid) & (lists["last_sale"] > 0)]
        joined = inside.merge(quotes, left_on="date", right_on="as_of_session")
        if len(joined) and ((joined["close"] / joined["last_sale"] - 1).abs() <= 0.02).mean() >= 0.5:
            return ticker
    return ""


MIN_PARTIAL_COVERAGE, LOW_PARTIAL_COVERAGE = 0.5, 0.1


def useful_partial(match: dict, need_start: str) -> bool:
    """Spend a Tiingo symbol on a full match; on a partial one only when it covers at least half
    the need, or at least a tenth of it from its start (the same company until Tiingo stops). A
    short row that starts inside the need is most likely a newer company on a reused ticker."""
    if match["match"] == "Y":
        return True
    if match["match"] != "partial":
        return False
    if match["coverage"] >= MIN_PARTIAL_COVERAGE:
        return True
    starts_in_time = _days(match["row_start"], need_start) >= -TIINGO_START_SLACK_DAYS
    return starts_in_time and match["coverage"] >= LOW_PARTIAL_COVERAGE


def route(facts: pd.DataFrame, hits: dict, vsample: dict, index: dict, lists: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for sid in sorted(set(hits) | set(vsample)):
        row = facts.loc[sid]
        reasons = sorted(hits.get(sid, {}), key=REASON_PRIORITY.index)
        base = {"security_id": sid, "cik": row.cik, "name": row["name"], "active": "Y" if row.active else ("successor" if row.via_successor else "N"),
                "delist_date": row.delist_date or "", "uncovered_weeks": int(row.uncovered_weeks),
                "best_rank": "" if pd.isna(row.best_rank) else int(row.best_rank), "v_category": vsample.get(sid, ""),
                "tiingo_range_match": "", "tiingo_row_start": "", "tiingo_row_end": "", "tiingo_coverage": "",
                "tiingo_reused_ticker": "", "note": ""}
        if reasons:
            primary = reasons[0]
            metric = hits[sid][primary]
            start, end = needed_window(row)
            if primary == "C_late_start" and len(metric) > 2:
                start = min(start, metric[2][0])
            out = {**base, "needed_start": start, "needed_end": end, "reason": primary,
                   "reasons_all": " ".join(reasons), "priority": REASON_PRIORITY.index(primary) + 1,
                   "prefilter_metric": metric[0], "prefilter_value": metric[1]}
            month = MONTH_2 if primary in MONTH_2_REASONS else MONTH_1
            alternative = "" if row.active else wiki_alternative(sid, row, start, end, lists)
            if row.active:
                out.update(planned_source="yahoo", ticker_for_source=yahoo_ticker(row), status="pending",
                           fetch_month=MONTH_1)
            elif row.via_successor:
                heir = facts.loc[row.via_successor]
                out.update(planned_source="yahoo", ticker_for_source=yahoo_ticker(heir), status="pending",
                           fetch_month=MONTH_1, note=f"history under successor {row.via_successor} (step-4 link)")
            elif alternative and end <= WIKI_END:
                out.update(planned_source="wiki", ticker_for_source=alternative, status="cached", fetch_month="",
                           note="WIKI file under a current SEC ticker of the CIK; LastSale level check passed")
            else:
                match = tiingo_range_match(tiingo_tickers(row, start), start, end, index)
                out.update(tiingo_range_match=match["match"], tiingo_row_start=match["row_start"],
                           tiingo_row_end=match["row_end"], tiingo_coverage=match["coverage"],
                           tiingo_reused_ticker="Y" if match["reused"] else "", ticker_for_source=match["ticker"])
                if useful_partial(match, start):
                    status = ("conditional_tier_c" if primary == "B_C_rest_300M_500M"
                              else "pending_month2" if month == MONTH_2 else "pending")
                    out.update(planned_source="tiingo", status=status, fetch_month=month)
                    if match["reused"]:
                        out["note"] = "ticker also has a newer Tiingo row: check the entity after the fetch"
                else:
                    newer = match["match"] == "wrong_entity" or (
                        match["match"] == "partial" and _days(match["row_start"], start) < -TIINGO_START_SLACK_DAYS)
                    out.update(planned_source="unfillable", fetch_month="", status="wrong_entity" if newer else "no_data")
                    if match["match"] == "partial":
                        out["note"] = (f"Tiingo row {match['row_start']}..{match['row_end']} covers only "
                                       f"{match['coverage']:.0%} of the need")
            rows.append(out)
        if sid in vsample:
            start = max(WINDOW_START, str(row.first_listed)[:10])
            ticker = yahoo_ticker(row)
            match = tiingo_range_match([ticker] + [t for t in tiingo_tickers(row, start) if t != ticker], start,
                                       WINDOW_END, index)
            rows.append({**base, "needed_start": start, "needed_end": WINDOW_END, "reason": "V_verify_sample",
                         "reasons_all": " ".join(["V_verify_sample"] + reasons), "priority": REASON_PRIORITY.index("V_verify_sample") + 1,
                         "prefilter_metric": "v_category", "prefilter_value": vsample[sid], "planned_source": "tiingo",
                         "ticker_for_source": match["ticker"] or ticker, "status": "pending", "fetch_month": MONTH_1,
                         "tiingo_range_match": match["match"], "tiingo_row_start": match["row_start"],
                         "tiingo_row_end": match["row_end"], "tiingo_coverage": match["coverage"],
                         "tiingo_reused_ticker": "Y" if match["reused"] else "",
                         "note": "verification: Tiingo next to the Yahoo series"})
    frame = pd.DataFrame(rows)
    return frame.reindex(columns=CANDIDATE_COLUMNS)


def apply_budget(candidates: pd.DataFrame, used: int, reserve: int = 50,
                 monthly: int = TIINGO_MONTHLY_SYMBOLS) -> tuple[pd.DataFrame, dict]:
    """Month-1 Tiingo symbols in priority order (plan 7 fallback 2: B-A, A, B-B, C, then the samples)
    up to the monthly cap less what the ledger shows used and a retry reserve; the rest is
    ``deferred_quota`` to month 2."""
    frame = candidates.copy()
    month1 = frame[(frame["planned_source"] == "tiingo") & (frame["fetch_month"] == MONTH_1)]
    order = month1.sort_values(["priority", "security_id"])
    room = monthly - used - reserve
    seen, deferred = [], []
    for k, row in order.iterrows():
        if row.ticker_for_source in seen:
            continue
        if len(seen) < room:
            seen.append(row.ticker_for_source)
        else:
            deferred.append(k)
    keep = set(seen)
    for k, row in order.iterrows():
        if row.ticker_for_source not in keep:
            frame.loc[k, ["status", "fetch_month"]] = ["deferred_quota", MONTH_2]
    month2 = frame[(frame["planned_source"] == "tiingo") & (frame["fetch_month"] == MONTH_2)]
    return frame, {"month1_symbols": len(keep), "month1_room": room, "ledger_used_this_month": used,
                   "reserve": reserve, "deferred_quota_symbols": int(frame.loc[frame["status"] == "deferred_quota",
                                                                                "ticker_for_source"].nunique()),
                   "month2_symbols": int(month2["ticker_for_source"].nunique()),
                   "month2_symbols_unconditional": int(month2.loc[month2["status"] != "conditional_tier_c",
                                                                  "ticker_for_source"].nunique())}


# ------------------------------------------------------------------ stage 7: unfillable and coverage

def unfillable_rows(candidates: pd.DataFrame, facts: pd.DataFrame, weekly: pd.DataFrame, above: pd.Series,
                    ranks: pd.DataFrame) -> pd.DataFrame:
    """Names (or parts of a needed window) no free source has: routed unfillable, or the part of
    the window a partial Tiingo row leaves out (30 days or more)."""
    gaps = []
    for row in candidates[candidates["reason"] != "V_verify_sample"].itertuples(index=False):
        wiki = "wiki(ends 2018-03-27)" if row.needed_start >= "2018-01-01" else "wiki(no usable file)"
        if row.planned_source == "unfillable":
            tried = f"{wiki}; tiingo_supported({row.tiingo_range_match or 'no_row'}); yahoo(delisted)"
            gaps.append((row, row.needed_start, row.needed_end, tried))
        elif row.planned_source == "tiingo" and row.tiingo_range_match == "partial":
            tried = f"{wiki}; tiingo_supported(partial {row.tiingo_row_start}..{row.tiingo_row_end})"
            if _days(row.needed_start, row.tiingo_row_start) >= 30:
                gaps.append((row, row.needed_start, _shift(row.tiingo_row_start, -1), tried))
            if _days(row.tiingo_row_end, row.needed_end) >= 30:
                gaps.append((row, _shift(row.tiingo_row_end, 1), row.needed_end, tried))
    universe = weekly[weekly["universe"]]
    out = []
    for row, start, end, tried in gaps:
        mine = universe[(universe["security_id"] == row.security_id)
                        & universe["week_end"].between(pd.Timestamp(start), pd.Timestamp(end))]
        # The same proxies the weekly estimate used: market caps carried up to 12 months into the window.
        carried_from = _shift(start, -PROXY_CARRY_DAYS)
        caps = ranks[(ranks["security_id"] == row.security_id) & ranks["snapshot_date"].between(carried_from, end)]
        if len(caps) and mine["mcap"].notna().any():
            proxy, value = "mcap", f"best_rank_{int(caps['mcap_rank'].min())}"
        elif mine["float_usd"].notna().any():
            proxy, value = "float", float(mine["float_usd"].max())
        else:
            proxy, value = "none", ""
        out.append({"security_id": row.security_id, "ticker": row.ticker_for_source, "needed_start": start,
                    "needed_end": end, "sources_tried": tried, "est_weeks_in_top250": int(above.loc[mine.index].sum()),
                    "proxy": proxy, "proxy_value": value, "reason": row.reason, "cik": row.cik, "name": row.name,
                    "delist_date": row.delist_date, "status": row.status})
    return pd.DataFrame(out, columns=UNFILLABLE_COLUMNS)


def _shift(day: str, days: int) -> str:
    return (pd.Timestamp(day) + pd.Timedelta(days=days)).strftime("%Y-%m-%d")


def planned_cover(candidates: pd.DataFrame) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]]:
    """security -> windows a planned (or cached) Yahoo, Tiingo or WIKI request should fill."""
    spans = defaultdict(list)
    usable = candidates[candidates["planned_source"].isin(["yahoo", "tiingo", "wiki"])
                        & (candidates["status"] != "conditional_tier_c")]
    for row in usable.itertuples(index=False):
        start, end = pd.Timestamp(row.needed_start), pd.Timestamp(row.needed_end)
        if row.planned_source == "tiingo" and row.tiingo_row_start:
            start, end = max(start, pd.Timestamp(row.tiingo_row_start)), min(end, pd.Timestamp(row.tiingo_row_end))
        spans[row.security_id].append((start, end))
    return spans


def weekly_coverage(weekly: pd.DataFrame, above: pd.Series, candidates: pd.DataFrame) -> pd.DataFrame:
    """Per week, before any fetch: of the names ranked 1-300 by dv50 (raw close >= $10, U counted in),
    how many have a usable vendor raw series, how many only a stored file, how many only an
    estimated (U) price; how many listed universe names have no price at all, and how many of
    those have a proxy at or above the median of ranks 200-250. The *_after_plan columns count
    what the planned requests would add (window overlap only, not a promise of data)."""
    universe = weekly[weekly["universe"]].copy()
    cover = planned_cover(candidates)
    planned = np.zeros(len(universe), dtype=bool)
    positions = {s: np.flatnonzero(universe["security_id"].values == s) for s in cover}
    days = universe["week_end"].values
    for sid, windows in cover.items():
        rows = positions[sid]
        for start, end in windows:
            planned[rows[(days[rows] >= np.datetime64(start)) & (days[rows] <= np.datetime64(end))]] = True
    universe["planned"] = planned
    universe["above"] = above.loc[universe.index].values
    top = universe[universe["dv50_rank"] <= FETCH_RANK]
    unpriced = universe[(universe["dv50"].isna() | universe["close"].isna()) & ~universe["outside_trading"]]
    g, t, u = universe.groupby("week_end"), top.groupby("week_end"), unpriced.groupby("week_end")
    table = pd.DataFrame({
        "n_listed_universe": g.size(),
        "n_ranked": g["dv50_rank"].count(),
        "top300_vendor_raw": t["vendor_ok"].sum(),
        "top300_stored_only": (~top["vendor_ok"] & (top["src"] == "stored")).groupby(top["week_end"]).sum(),
        "top300_price_unknown": (top["price_ge_10"] == "U").groupby(top["week_end"]).sum(),
        "top300_vendor_after_plan": (top["vendor_ok"] | top["planned"]).groupby(top["week_end"]).sum(),
        "n_unpriced": u.size(),
        "n_unpriced_proxy_above_rank200_250": u["above"].sum(),
        "n_unpriced_proxy_above_unplanned": (unpriced["above"] & ~unpriced["planned"]).groupby(unpriced["week_end"]).sum(),
    }).fillna(0).astype(int)
    table.index.name = "week_end"
    return table.reset_index()


# ------------------------------------------------------------------ main

def coverage_by_year(table: pd.DataFrame) -> dict:
    years = table.assign(year=pd.to_datetime(table["week_end"]).dt.year).groupby("year")
    columns = [c for c in table.columns if c != "week_end"]
    return {int(y): {c: {"median": float(g[c].median()), "min": int(g[c].min()), "max": int(g[c].max())}
                     for c in columns} for y, g in years}


def summarise(candidates: pd.DataFrame, unfillable: pd.DataFrame, budget: dict, coverage: pd.DataFrame,
              facts: pd.DataFrame, hits: dict, stage_facts: dict, extra: dict) -> dict:
    fetch = candidates[candidates["reason"] != "V_verify_sample"]
    any_reason = Counter(r for h in hits.values() for r in h)
    known = {}
    for ticker in sorted(KNOWN_UNFILLABLE):
        mine = facts[facts["tickers"].fillna("").str.split().apply(lambda ts: ticker in ts)]
        rows = candidates[candidates["security_id"].isin(mine.index)]
        known[ticker] = (" ".join(f"{r.planned_source}/{r.status}/{r.tiingo_range_match or '-'}" for r in rows.itertuples())
                         or ("not a candidate" if len(mine) else "no Nasdaq interval"))
    return {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "rows": int(len(candidates)), "securities": int(candidates["security_id"].nunique()),
        "rows_by_reason": dict(Counter(candidates["reason"])),
        "securities_by_any_reason": dict(any_reason),
        "rows_by_planned_source": dict(Counter(candidates["planned_source"])),
        "rows_by_source_and_reason": {f"{s}|{r}": int(n) for (s, r), n in
                                      candidates.groupby(["planned_source", "reason"]).size().items()},
        "rows_by_status": dict(Counter(candidates["status"])),
        "tiingo_range_match": dict(Counter(candidates.loc[candidates["tiingo_range_match"] != "", "tiingo_range_match"])),
        "tiingo_budget": budget,
        "yahoo_symbols": int(candidates.loc[candidates["planned_source"] == "yahoo", "ticker_for_source"].nunique()),
        "unfillable_rows": int(len(unfillable)),
        "unfillable_est_weeks_in_top250": int(unfillable["est_weeks_in_top250"].sum()) if len(unfillable) else 0,
        "known_unfillable_from_plan": known,
        "fetch_rows_active": int((fetch["active"] == "Y").sum()),
        "coverage_by_year": coverage_by_year(coverage),
        "daily_stage": stage_facts,
        **extra,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="never download (the Tiingo list must be cached)")
    parser.add_argument("--rebuild-daily", action="store_true", help="re-read every price file")
    args = parser.parse_args(argv)
    started = time.time()
    master, intervals = load_master(), load_intervals()
    form25 = read_csv_text(FORM25)
    if args.rebuild_daily or not (OUT / "daily_series.pkl").exists():
        log("stage 1: daily series from every price source")
        save_daily(build_daily(master, intervals))
    stage = load_daily()
    log(f"stage 1 loaded: {len(stage['best'])} security-days")
    foreign = foreign_spans(master, offline=True)
    sessions = xnas_sessions()
    weeks = week_ends(sessions)
    log(f"stage 2: weekly metrics, {len(weeks)} weeks {weeks[0].date()}..{weeks[-1].date()}")
    weekly = weekly_table(stage, master, foreign, sessions, weeks)
    weekly = mark_trading_bounds(weekly, stage["spans"], stage["best"])
    log("stage 3: proxies")
    ranks = mcap_ranks(stage["lists"], stage["spans"], foreign)
    floats = float_facts(offline=True)
    weekly = attach_proxies(weekly, master, ranks, floats)
    cutoffs = proxy_cutoffs(weekly)
    above = proxy_above(weekly, cutoffs)
    weekly.to_pickle(OUT / "weekly_metrics.pkl")
    log("stage 4: security facts and rules")
    facts = security_facts(weekly, stage["spans"], master, stage["best"], form25)
    hits = rule_hits(facts, ranks, floats)
    log(f"rule hits: {len(hits)} securities")
    events = event_flags()
    breaks = stored_break_names()
    log(f"V inputs: {len(events)} event flags, {len(breaks)} stored 2025-06-24 break files")
    vsample = verification_sample(facts, weekly, events, breaks)
    log("stage 5: routing")
    supported = load_supported_tickers(offline=args.offline)
    candidates = route(facts, hits, vsample, tiingo_index(supported), stage["lists"])
    used = common.quota_used("tiingo", MONTH_1, unique_symbols=True)
    candidates, budget = apply_budget(candidates, used)
    candidates = candidates.sort_values(["priority", "security_id", "planned_source"]).reset_index(drop=True)
    unfillable = unfillable_rows(candidates, facts, weekly, above, ranks)
    coverage = weekly_coverage(weekly, above, candidates)
    common.atomic_write(CANDIDATES, candidates.to_csv(index=False).encode())
    common.atomic_write(UNFILLABLE, unfillable.to_csv(index=False).encode())
    common.atomic_write(OUT / "weekly_coverage.csv", coverage.to_csv(index=False).encode())
    common.atomic_write(OUT / "security_facts.csv.gz",
                        gzip.compress(facts.reset_index().rename(columns={"index": "security_id"}).to_csv(index=False).encode(), mtime=0))
    events.to_csv(OUT / "event_flags.csv", index=False)
    extra = {"stored_break_files_2025_06_24": breaks, "v_sample_categories": dict(Counter(vsample.values())),
             "event_flags": dict(Counter(events["kind"])),
             "supported_tickers": {"file": supported_tickers_path().name, "sha256": common.sha256_file(supported_tickers_path()),
                                   "rows": int(len(supported))},
             "uncovered_universe_securities": int((facts["uncovered_weeks"] > 0).sum()),
             "uncovered_not_candidates": int(((facts["uncovered_weeks"] > 0) & ~facts.index.isin(list(hits))).sum()),
             "seed": SEED, "runtime_s": round(time.time() - started, 1)}
    summary = summarise(candidates, unfillable, budget, coverage, facts, hits, stage["facts"], extra)
    common.atomic_write(OUT / "prefilter_summary.json", (json.dumps(summary, indent=1, default=str) + "\n").encode())
    log(f"candidates: {len(candidates)} rows; by reason {summary['rows_by_reason']}")
    log(f"by planned source {summary['rows_by_planned_source']}; status {summary['rows_by_status']}")
    log(f"tiingo budget {budget}; yahoo symbols {summary['yahoo_symbols']}; unfillable {len(unfillable)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
