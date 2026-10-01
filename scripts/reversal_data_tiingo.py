"""Plan step 8 (docs/reversal_2012_2026_data_plan.md, sections 2 and 7): Tiingo daily prices, fetcher only.

Data only. Nothing here computes signals, strategy or portfolio returns, return
rankings or spreads. The checks below are single-stock data flags: date ranges,
session gaps, a raw price jump across a gap, raw dollar volume and raw close
against independent references (the entity check, plan 4.4 R9), and Tiingo's own
adjClose identity (plan 4.2 row check).

What it does:
- reads ``INPUTS/candidate_fetch_list.csv`` (step 6) and keeps the rows planned for
  Tiingo this month (``planned_source == tiingo``, status ``pending``, or a row this
  fetcher deferred earlier);
- orders tickers by tier: B-A, then A (A1, A2), then B-B, then C, then V and the
  tier-C sample; inside a tier the most liquid name (lowest ``best_rank``) first;
- asks ``api.tiingo.com/tiingo/daily/{ticker}/prices?startDate=2011-06-01&endDate=2026-08-31``
  once per ticker (no meta call). The key is read from ``.env.tiingo`` inside Python and
  goes only in the ``Authorization: Token`` header, never in a URL or a log line;
- every request goes through ``cached_get`` (cache first, then the raw index and the
  quota ledger) and a ``SlidingWindowLimiter`` of 45 an hour and 900 a day, seeded from
  the quota ledger so a restart does not reset the windows;
- before a ticker that is new this month it checks
  ``quota_used("tiingo", unique_symbols=True)`` (plus ``--already-used``, symbols the
  account page shows that the ledger does not) and stops at 480; it also stops at
  ``--max-mb`` of Tiingo bytes this month, on HTTP 429, on an error body that mentions a
  limit, and on 401/403;
- does not ask for a ticker when every row that needs it is hidden by a later row in Tiingo's
  ticker list: the API answers a ticker with the row that ends latest (trial 2026-10-01: CA came
  back as a 2023 ETF, CZR as the former Eldorado, GPOR as the 2021 Gulfport), so such a row is
  ``wrong_entity`` without spending a symbol (``--fetch-shadowed`` asks anyway);
- writes status to its own file, ``CACHE/tiingo/fetch_status.csv`` (the candidate list is
  never rewritten), the parsed series to ``CACHE/tiingo/prices/{TICKER}.csv.gz`` and a
  run summary to ``CACHE/tiingo/fetch_summary.json``.

Statuses (one row per candidate row, keyed by security, ticker and needed window):
``done`` (entity check ok), ``done_review`` (data kept, a flag to review), ``partial``
(the series does not cover the needed window), ``wrong_entity`` (the series is another
company, e.g. a reused ticker), ``no_data`` (404 or an empty answer), ``deferred_quota``
(stopped by a budget or limit; the next run or month picks it up), ``error``.

Usage::

    PYTHONPATH=. python scripts/reversal_data_tiingo.py --limit 5          # trial: the top 5 tickers
    PYTHONPATH=. python scripts/reversal_data_tiingo.py                    # the whole month-1 list
    PYTHONPATH=. python scripts/reversal_data_tiingo.py --dry-run          # the order, no request
    PYTHONPATH=. python scripts/reversal_data_tiingo.py --offline --recheck  # re-check cached answers only
    PYTHONPATH=. python scripts/reversal_data_tiingo.py --tickers FRG --fetch-shadowed   # one ticker on purpose
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys
import time

import numpy as np
import pandas as pd

from scripts import reversal_data_common as common

ENV_FILE = common.MAIN_CHECKOUT / ".env.tiingo"
KEY_NAME = "TIINGO_API_KEY"
SOURCE = "tiingo"
API = "https://api.tiingo.com/tiingo/daily/{ticker}/prices"
START, END = "2011-06-01", "2026-08-31"

HOURLY, DAILY = 45, 900  # the free tier allows 50 an hour and 1,000 a day; stay under both
SPACING = 60.0  # seconds between requests: the hour's 45 are spread out, not sent in a burst
MONTH_STOP = 480  # unique symbols this month (the free tier allows 500)
MAX_MB = 900.0  # Tiingo bytes this month (the free tier allows 1 GB)
MAX_CONSECUTIVE_ERRORS = 3

RAW_DIR = common.RAW / "tiingo"
OUT_DIR = common.CACHE / "tiingo"
PRICES_DIR = OUT_DIR / "prices"
STATUS = OUT_DIR / "fetch_status.csv"
SUMMARY = OUT_DIR / "fetch_summary.json"
LOCK = OUT_DIR / "fetch.lock"
CANDIDATES = common.INPUTS / "candidate_fetch_list.csv"
PREFILTER = common.CACHE / "prefilter"

# Fetch order (task and plan 7 fallback 2): B-A, then A, then B-B, then C, then V and the tier-C sample.
TIER_ORDER = ["B_A_float_ge_1B", "A1_wiki_dv_rank300", "A2_mcap_rank400", "A3_float_ge_1B_2012_2018",
              "B_B_float_500M_1B", "C_late_start", "V_verify_sample", "B_C_sample_300M_500M",
              "S_stored_only_delisted_rank300", "B_C_rest_300M_500M"]
MONTH_2_REASONS = {"S_stored_only_delisted_rank300", "B_C_rest_300M_500M"}
RUN_STATUSES = {"pending"}  # candidate-list statuses fetched by default
FINAL = {"done", "done_review", "partial", "wrong_entity", "no_data"}
RETRY = {"deferred_quota", "error"}
QUOTA_WORDS = re.compile(r"limit|allocation|exceed|run over|upgrade|too many|quota", re.IGNORECASE)

PRICE_FIELDS = ["date", "open", "high", "low", "close", "volume", "adjOpen", "adjHigh", "adjLow", "adjClose",
                "adjVolume", "divCash", "splitFactor"]
STATUS_COLUMNS = [
    "security_id", "ticker_for_source", "reason", "needed_start", "needed_end", "order", "status",
    "entity_check", "entity_notes", "http_status", "rows_total", "first_date", "last_date", "rows_in_need",
    "need_sessions", "need_coverage", "covers_start", "covers_end", "max_gap_sessions", "gap_jumps",
    "dv_overlap_days", "dv_ratio_median", "lastsale_checks", "lastsale_agree_share", "dv_overlap_days_all",
    "dv_ratio_median_all", "lastsale_checks_all", "lastsale_agree_share_all", "fields_missing",
    "split_events", "div_events", "adj_identity_max_err", "adj_identity_bad_rows", "zero_volume_rows",
    "raw_path", "raw_sha256", "bytes", "fetched_utc", "updated_utc"]

# Entity check thresholds (plan 4.4 R9). Stored files run ~12% low on dollar volume in 2016.
START_SLACK_DAYS, END_SLACK_DAYS = 7, 21  # as the prefilter's range match
GAP_SESSIONS = 10
GAP_JUMP = 0.5
DV_MIN_DAYS = 20
DV_OK = (0.8, 1.25)
DV_FAIL = (0.5, 2.0)
LASTSALE_TOLERANCE, LASTSALE_LOOSE = 0.02, 0.05  # confirmed / unverified as-of session
LASTSALE_MIN = 2
LASTSALE_OK, LASTSALE_FAIL = 0.9, 0.5
COVERAGE_OK = 0.95
ADJ_TOLERANCE = 1e-6


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(ticker: str) -> str:
    """A file-name form of the ticker."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", ticker.upper())


def url_ticker(ticker: str) -> str:
    """Tiingo's URL form: lower case, share-class dots as dashes (BRK.B -> brk-b)."""
    return ticker.strip().lower().replace(".", "-").replace("/", "-")


def price_url(ticker: str, start: str = START, end: str = END) -> str:
    """The request URL. It never carries the key (that goes in the Authorization header)."""
    return API.format(ticker=url_ticker(ticker)) + f"?startDate={start}&endDate={end}"


def raw_path(ticker: str, start: str = START, end: str = END) -> Path:
    return RAW_DIR / f"{safe_name(ticker)}__{start}_{end}.json.gz"


def auth_headers() -> dict:
    key = common.read_env_key(ENV_FILE, KEY_NAME)
    return {"Authorization": f"Token {key}", "Content-Type": "application/json",
            "User-Agent": "quant_stocks research"}


# ------------------------------------------------------------------ candidates, order and status

def row_key(row) -> tuple[str, str, str, str]:
    return (str(row["security_id"]), str(row["ticker_for_source"]), str(row["needed_start"]),
            str(row["needed_end"]))


def tier_rank(reason: str) -> int:
    return TIER_ORDER.index(reason) if reason in TIER_ORDER else len(TIER_ORDER)


def load_candidates(path: Path = CANDIDATES) -> pd.DataFrame:
    """Every candidate row planned for Tiingo (any status), with its tier rank."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame = frame[frame["planned_source"] == "tiingo"].copy()
    frame["tier"] = frame["reason"].map(tier_rank)
    frame["rank_sort"] = pd.to_numeric(frame["best_rank"], errors="coerce").fillna(1e9)
    return frame.sort_values(["tier", "rank_sort", "security_id", "needed_start"]).reset_index(drop=True)


def select_rows(candidates: pd.DataFrame, status: pd.DataFrame, statuses: set[str] = RUN_STATUSES,
                reasons: set[str] | None = None, include_final: bool = False) -> pd.DataFrame:
    """Rows this run should fetch: candidate status in ``statuses`` (or a row this fetcher deferred
    or failed earlier), not already final in the status file, and (if given) one of ``reasons``."""
    known = {row_key(r): r["status"] for _, r in status.iterrows()} if len(status) else {}
    keep = []
    for _, row in candidates.iterrows():
        mine = known.get(row_key(row), "")
        if mine in FINAL and not include_final:
            continue
        if reasons is not None and row["reason"] not in reasons:
            continue
        if row["status"] in statuses or mine in RETRY or (include_final and mine in FINAL):
            keep.append(row)
    return pd.DataFrame(keep, columns=candidates.columns)


def ticker_order(rows: pd.DataFrame) -> list[str]:
    """Unique tickers in fetch order: a ticker takes the place of its highest-priority row."""
    seen: list[str] = []
    for ticker in rows.sort_values(["tier", "rank_sort", "security_id", "needed_start"])["ticker_for_source"]:
        if ticker not in seen:
            seen.append(ticker)
    return seen


def load_status(path: Path | None = None) -> pd.DataFrame:
    path = path or STATUS
    if not path.exists():
        return pd.DataFrame(columns=STATUS_COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False).reindex(columns=STATUS_COLUMNS, fill_value="")


def merge_status(status: pd.DataFrame, updates: list[dict]) -> pd.DataFrame:
    """``status`` with ``updates`` replacing the rows of the same key (or appended)."""
    if not updates:
        return status
    new = pd.DataFrame(updates).reindex(columns=STATUS_COLUMNS, fill_value="")
    new = new.astype(str).replace({"nan": "", "None": ""})
    keys = set(map(row_key, new.to_dict("records")))
    old = status[[row_key(r) not in keys for r in status.to_dict("records")]] if len(status) else status
    merged = pd.concat([old, new], ignore_index=True) if len(old) else new
    return merged.sort_values(["order", "security_id"], key=lambda s: pd.to_numeric(s, errors="coerce")
                              if s.name == "order" else s).reset_index(drop=True)


def write_status(status: pd.DataFrame, path: Path | None = None) -> None:
    common.atomic_write(path or STATUS, status.reindex(columns=STATUS_COLUMNS).to_csv(index=False).encode("utf-8"))


# ------------------------------------------------------------------ quota ledger and limiter

def ledger_rows(source: str = SOURCE, path: Path | None = None) -> pd.DataFrame:
    """The quota-ledger rows of ``source`` (fetched_utc, month, symbol, status)."""
    path = path or common.QUOTA_LEDGER
    if not path.exists():
        return pd.DataFrame(columns=["fetched_utc", "source", "month", "symbol", "status"])
    rows = [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()[1:]]
    rows = [r[:5] for r in rows if len(r) >= 5 and r[1] == source]
    return pd.DataFrame(rows, columns=["fetched_utc", "source", "month", "symbol", "status"])


def symbols_this_month(month: str, source: str = SOURCE, path: Path | None = None) -> set[str]:
    rows = ledger_rows(source, path)
    return set(rows.loc[rows["month"] == month, "symbol"])


def rolling_symbols(days: int = 30, source: str = SOURCE, path: Path | None = None,
                    now: datetime | None = None) -> int:
    """Unique symbols in the last ``days`` days (in case Tiingo's month is rolling)."""
    rows = ledger_rows(source, path)
    if not len(rows):
        return 0
    stamps = pd.to_datetime(rows["fetched_utc"], utc=True, errors="coerce")
    since = pd.Timestamp(now or utc_now()) - pd.Timedelta(days=days)
    return int(rows.loc[stamps >= since, "symbol"].nunique())


def read_gzip_members(path: Path) -> tuple[str, int]:
    """The text of a multi-member gzip file (one member per appended line), skipping members that do
    not decompress (the shared raw index has some, from concurrent appends), and how many were skipped."""
    import zlib

    data, pos, parts, bad = path.read_bytes(), 0, [], 0
    while pos < len(data):
        decoder = zlib.decompressobj(31)
        try:
            text = decoder.decompress(data[pos:])
            if not decoder.eof:
                raise zlib.error("truncated member")
            parts.append(text.decode("utf-8", "replace"))
            pos = len(data) - len(decoder.unused_data)
        except zlib.error:
            bad += 1
            following = data.find(b"\x1f\x8b\x08", pos + 1)
            if following < 0:
                break
            pos = following
    return "".join(parts), bad


def index_bytes(month: str, source: str = SOURCE, path: Path | None = None) -> int:
    """Response bytes ``source`` logged in ``month`` in the raw index (bad members skipped)."""
    path = path or common.RAW_INDEX
    if not path.exists():
        return 0
    text, _ = read_gzip_members(path)
    total = 0
    for line in text.splitlines():
        parts = line.split(",")
        if len(parts) >= 5 and parts[1] == source and parts[0].startswith(month):
            total += int(parts[4]) if parts[4].isdigit() else 0
    return total


def own_bytes(month: str, directory: Path | None = None) -> int:
    """Uncompressed bytes of this fetcher's raw answers written in ``month`` (gzip ISIZE trailer)."""
    directory = directory or RAW_DIR
    total = 0
    for path in directory.glob("*__*.json.gz*") if directory.exists() else []:
        if path.name.endswith(".404") or path.name.endswith(".tmp"):
            continue
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m")
        if stamp == month and path.stat().st_size >= 4:
            with path.open("rb") as handle:
                handle.seek(-4, os.SEEK_END)
                total += int.from_bytes(handle.read(4), "little")
    return total


def bytes_this_month(month: str, source: str = SOURCE) -> int:
    """Tiingo bytes this month: the larger of the raw index's count and this fetcher's own files."""
    return max(index_bytes(month, source), own_bytes(month))


def make_limiter(hourly: int = HOURLY, daily: int = DAILY, seed: pd.DataFrame | None = None,
                 now: datetime | None = None, spacing: float = 0) -> common.SlidingWindowLimiter:
    """The 45/hour, 900/day limiter (plus at most one request per ``spacing`` seconds, so the hour's
    45 are spread out rather than sent in a burst), seeded with the ledger's requests of the last day
    so that a restart keeps counting them (stamps are converted to this process's monotonic clock)."""
    windows = {3600: hourly, 86400: daily}
    if spacing > 0:
        windows[float(spacing)] = 1
    limiter = common.SlidingWindowLimiter(windows)
    if seed is not None and len(seed):
        now = now or utc_now()
        mono = time.monotonic()
        stamps = pd.to_datetime(seed["fetched_utc"], utc=True, errors="coerce").dropna()
        ages = sorted((pd.Timestamp(now) - stamps).dt.total_seconds().tolist(), reverse=True)
        for age in ages:
            if 0 <= age <= 86400:
                limiter.stamps.append(mono - age)
    return limiter


def limiter_delay(limiter: common.SlidingWindowLimiter) -> float:
    """Seconds the next ``wait()`` would block (read only; for the progress log)."""
    now = time.monotonic()
    delays = []
    for seconds, limit in limiter.windows.items():
        recent = [s for s in limiter.stamps if now - s <= seconds]
        if len(recent) >= limit:
            delays.append(seconds - (now - recent[0]) + 0.01)
    return max(delays) if delays else 0.0


# ------------------------------------------------------------------ the response

def parse_body(data: bytes) -> tuple[list | None, str]:
    """(price rows, "") for a list answer; (None, error text) for an error object or bad JSON."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "not JSON: " + common.redact(data[:200].decode("utf-8", "replace"))
    if isinstance(payload, list):
        return payload, ""
    if isinstance(payload, dict):
        text = str(payload.get("detail") or payload.get("message") or payload.get("error") or payload)
        return None, common.redact(text[:300])
    return None, f"unexpected JSON type {type(payload).__name__}"


def is_quota_text(text: str) -> bool:
    return bool(QUOTA_WORDS.search(text or ""))


def to_frame(prices: list) -> pd.DataFrame:
    """Tiingo rows as a frame: ``close``/``volume`` as traded, ``adj*`` adjusted, ``divCash`` as paid,
    ``splitFactor`` new shares per old share on the ex-date."""
    frame = pd.DataFrame(prices)
    if not len(frame):
        return pd.DataFrame(columns=PRICE_FIELDS)
    frame = frame.reindex(columns=PRICE_FIELDS)
    frame["date"] = pd.to_datetime(frame["date"].astype(str).str[:10])
    for column in PRICE_FIELDS[1:]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)


def field_summary(prices: list, frame: pd.DataFrame) -> dict:
    """What came back: missing fields, split and dividend events, and Tiingo's adjClose identity
    adjClose_t / adjClose_{t-1} = (close_t * splitFactor_t + divCash_t) / close_{t-1} (plan 4.2)."""
    present = set(prices[0]) if prices else set()
    missing = [f for f in ("close", "adjClose", "splitFactor", "divCash", "volume") if f not in present]
    out = {"rows_total": len(frame), "fields_missing": " ".join(missing),
           "first_date": frame["date"].min().strftime("%Y-%m-%d") if len(frame) else "",
           "last_date": frame["date"].max().strftime("%Y-%m-%d") if len(frame) else "",
           "split_events": "", "div_events": 0, "adj_identity_max_err": "", "adj_identity_bad_rows": "",
           "zero_volume_rows": 0}
    if not len(frame):
        return out
    splits = frame[frame["splitFactor"].fillna(1.0).sub(1.0).abs() > 1e-12]
    out["split_events"] = " ".join(f"{d:%Y-%m-%d}:{s:g}" for d, s in zip(splits["date"], splits["splitFactor"]))
    out["div_events"] = int((frame["divCash"].fillna(0) > 0).sum())
    out["zero_volume_rows"] = int((frame["volume"].fillna(0) <= 0).sum())
    if len(frame) > 1 and not missing:
        close, adj = frame["close"].values, frame["adjClose"].values
        split, div = frame["splitFactor"].fillna(1.0).values, frame["divCash"].fillna(0.0).values
        with np.errstate(divide="ignore", invalid="ignore"):
            implied = (close[1:] * split[1:] + div[1:]) / close[:-1]
            ratio = adj[1:] / adj[:-1]
            err = np.abs(ratio / implied - 1.0)
        err = err[np.isfinite(err)]
        if len(err):
            out["adj_identity_max_err"] = f"{err.max():.3g}"
            out["adj_identity_bad_rows"] = int((err > ADJ_TOLERANCE).sum())
    return out


# ------------------------------------------------------------------ entity check (plan 4.4 R9)

def max_gap(dates: pd.Series, sessions: pd.DatetimeIndex) -> tuple[int, list[int]]:
    """The largest run of XNAS sessions missing between consecutive rows, and the row positions
    (into ``dates``) that follow a gap longer than ``GAP_SESSIONS``."""
    if len(dates) < 2:
        return 0, []
    position = sessions.searchsorted(pd.DatetimeIndex(dates))
    missing = np.diff(position) - 1
    after = [i + 1 for i, m in enumerate(missing) if m > GAP_SESSIONS]
    return int(max(missing.max(), 0)), after


def dv_ratio(rows: pd.DataFrame, reference: pd.DataFrame | None) -> tuple[int, float | str]:
    """Overlap days and the median of Tiingo raw close x raw volume over the reference dollar volume."""
    if reference is None or not len(reference) or not len(rows):
        return 0, ""
    joined = rows.merge(reference[["date", "dv"]], on="date", how="inner")
    joined = joined[(joined["dv"] > 0) & (joined["close"] * joined["volume"] > 0)]
    if len(joined) < DV_MIN_DAYS:
        return len(joined), ""
    return len(joined), round(float(np.median(joined["close"] * joined["volume"] / joined["dv"])), 4)


def lastsale_share(rows: pd.DataFrame, lists: pd.DataFrame | None) -> tuple[int, float | str]:
    """Company-list dates and the share where Tiingo's raw close is within 2% of LastSale (5% where
    the as-of session is unverified), as the prefilter's WIKI check."""
    if lists is None or not len(lists) or not len(rows):
        return 0, ""
    quotes = lists.rename(columns={"as_of_session": "date"}).reindex(columns=["date", "last_sale", "as_of_check"])
    joined = rows.merge(quotes, on="date", how="inner")
    joined = joined[joined["last_sale"] > 0]
    if len(joined) < LASTSALE_MIN:
        return len(joined), ""
    band = np.where(joined["as_of_check"] == "confirmed", LASTSALE_TOLERANCE, LASTSALE_LOOSE)
    return len(joined), round(float((np.abs(joined["close"] / joined["last_sale"] - 1.0) <= band).mean()), 4)


def entity_check(frame: pd.DataFrame, need_start: str, need_end: str, sessions: pd.DatetimeIndex,
                 reference: pd.DataFrame | None = None, lists: pd.DataFrame | None = None) -> dict:
    """Whether the Tiingo series is this security over its needed window.

    ``reference``: the security's own daily raw dollar volume from other sources (date, dv; WIKI,
    the stored files after the prefilter's owner rule, Yahoo). ``lists``: Wayback company-list
    LastSale (as_of_session, last_sale). Returns the status fields and a verdict ok/review/fail.
    """
    start, end = pd.Timestamp(need_start), pd.Timestamp(need_end)
    need = sessions[(sessions >= start) & (sessions <= end)]
    inside = frame[(frame["date"] >= start) & (frame["date"] <= end)]
    out = {"rows_in_need": len(inside), "need_sessions": len(need), "need_coverage": "",
           "covers_start": "", "covers_end": "", "max_gap_sessions": "", "gap_jumps": 0,
           "dv_overlap_days": 0, "dv_ratio_median": "", "lastsale_checks": 0, "lastsale_agree_share": ""}
    fails, reviews = [], []
    if not len(inside):
        out["entity_check"], out["entity_notes"] = "fail", "no rows in the needed window"
        return out
    first, last = frame["date"].min(), frame["date"].max()
    out["covers_start"] = "Y" if first <= start + pd.Timedelta(days=START_SLACK_DAYS) else "N"
    out["covers_end"] = "Y" if last >= end - pd.Timedelta(days=END_SLACK_DAYS) else "N"
    # Sessions inside the allowed slack (a delisted name stops trading before its Form 25 takes
    # effect) are not counted as missing; holes inside the series are.
    lo = max(start, first) if out["covers_start"] == "Y" else start
    hi = min(end, last) if out["covers_end"] == "Y" else end
    counted = need[(need >= lo) & (need <= hi)]
    out["need_coverage"] = round(float(np.isin(counted, inside["date"].values).mean()) if len(counted) else 1.0, 4)
    gap, after = max_gap(inside["date"], sessions)
    out["max_gap_sessions"] = gap
    close = inside["close"].values
    split = inside["splitFactor"].fillna(1.0).values
    jumps = [i for i in after if split[i] == 1.0 and close[i - 1] > 0
             and abs(close[i] / close[i - 1] - 1.0) > GAP_JUMP]
    out["gap_jumps"] = len(jumps)
    if gap > GAP_SESSIONS:
        reviews.append(f"gap of {gap} sessions")
    if jumps:
        reviews.append(f"{len(jumps)} raw jump(s) over 50% across a gap")
    # Judge inside the needed window; when the window has no reference, judge on the whole overlap
    # with the security's own history (e.g. WIKI 2011-2018 for a name delisted in 2018-2020).
    for scope, rows in (("window", inside), ("all", frame)):
        n, ratio = dv_ratio(rows, reference)
        m, share = lastsale_share(rows, lists)
        suffix = "" if scope == "window" else "_all"
        out[f"dv_overlap_days{suffix}"], out[f"dv_ratio_median{suffix}"] = n, ratio
        out[f"lastsale_checks{suffix}"], out[f"lastsale_agree_share{suffix}"] = m, share
    scope = "window"
    if out["dv_ratio_median"] == "" and out["lastsale_agree_share"] == "":
        scope = "all"
        reviews.append("no independent reference in the window" + (
            "" if out["dv_ratio_median_all"] != "" or out["lastsale_agree_share_all"] != ""
            else " or elsewhere"))
    suffix = "" if scope == "window" else "_all"
    where = "" if scope == "window" else " (outside the window)"
    ratio, share = out[f"dv_ratio_median{suffix}"], out[f"lastsale_agree_share{suffix}"]
    if ratio != "":
        if not DV_FAIL[0] <= ratio <= DV_FAIL[1]:
            fails.append(f"dollar volume x{ratio:.3g} of the reference{where}")
        elif not DV_OK[0] <= ratio <= DV_OK[1]:
            reviews.append(f"dollar volume x{ratio:.3g} of the reference{where}")
    if share != "":
        text = f"raw close agrees with LastSale on {share:.0%} of {out[f'lastsale_checks{suffix}']} dates{where}"
        if share < LASTSALE_FAIL:
            fails.append(text)
        elif share < LASTSALE_OK:
            reviews.append(text)
    if out["need_coverage"] < COVERAGE_OK or out["covers_start"] == "N" or out["covers_end"] == "N":
        reviews.append(f"covers {out['need_coverage']:.0%} of the needed sessions")
    out["entity_check"] = "fail" if fails else ("review" if reviews else "ok")
    out["entity_notes"] = "; ".join(fails + reviews)
    return out


def row_status(check: dict) -> str:
    """The candidate row's status from its entity check."""
    if check["entity_check"] == "fail":
        return "wrong_entity"
    if check["need_coverage"] != "" and (check["need_coverage"] < COVERAGE_OK or check["covers_start"] == "N"
                                         or check["covers_end"] == "N"):
        return "partial"
    return "done_review" if check["entity_check"] == "review" else "done"


# ------------------------------------------------------------------ references and calendar

def xnas_sessions(start: str = START, end: str = END) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNAS", start="2010-01-04", end="2026-12-31")
    return pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)


def load_references(security_ids: set[str], directory: Path | None = None) -> tuple[dict, dict]:
    """Per security: its daily raw dollar volume from non-Tiingo sources (the prefilter's best
    rows: WIKI, the stored files after the owner rule, Yahoo) and its company-list LastSale."""
    dv, quotes = {}, {}
    directory = directory or PREFILTER
    best_path, lists_path = directory / "daily_series.pkl", directory / "lists.csv.gz"
    if best_path.exists():
        best = pd.read_pickle(best_path)
        best = best[best["security_id"].isin(security_ids) & (best["src"] != "tiingo")]
        dv = {sid: group[["date", "dv", "src"]].reset_index(drop=True) for sid, group in best.groupby("security_id")}
    if lists_path.exists():
        lists = pd.read_csv(lists_path, dtype=str, keep_default_na=False)
        lists = lists[lists["security_id"].isin(security_ids)].copy()
        lists["as_of_session"] = pd.to_datetime(lists["as_of_session"])
        lists["last_sale"] = pd.to_numeric(lists["last_sale"], errors="coerce")
        quotes = {sid: group.reset_index(drop=True) for sid, group in lists.groupby("security_id")}
    return dv, quotes


def supported_latest(directory: Path | None = None) -> dict[str, dict]:
    """ticker -> the row of Tiingo's ticker list (the prefilter's cached ``supported_tickers`` zip, any
    asset type) that ends latest. The API answers a ticker with that row's company: in the trial
    (2026-10-01) CA came back as a 2023 ETF, CZR as the former Eldorado, GPOR as the 2021 Gulfport."""
    import zipfile

    directory = directory or PREFILTER
    paths = sorted(directory.glob("supported_tickers_*.zip"))
    if not paths:
        return {}
    with zipfile.ZipFile(paths[-1]) as archive:
        name = [n for n in archive.namelist() if n.endswith(".csv")][0]
        frame = pd.read_csv(archive.open(name), dtype=str, keep_default_na=False)
    frame["ticker"] = frame["ticker"].str.upper().str.strip()
    latest = frame.sort_values(["ticker", "endDate", "startDate"]).drop_duplicates("ticker", keep="last")
    return {r.ticker: {"start": r.startDate[:10], "end": r.endDate[:10], "asset_type": r.assetType,
                       "exchange": r.exchange} for r in latest.itertuples(index=False)}


def shadowed_by(row, latest: dict[str, dict]) -> dict | None:
    """The later Tiingo row that hides the row the prefilter matched for ``row`` (None if none)."""
    later = latest.get(str(row["ticker_for_source"]).upper())
    matched_end = str(row.get("tiingo_row_end", "") or "")[:10]
    if later and matched_end and later["end"] > matched_end:
        return later
    return None


def precheck_rows(ticker: str, rows: pd.DataFrame, later: dict, order: int) -> list[dict]:
    """``wrong_entity`` without a request: the API would serve the later row's company."""
    note = (f"not asked: Tiingo's list has a later {later['asset_type']} row for {ticker} on {later['exchange']} "
            f"({later['start']}..{later['end']}) and the API serves the row that ends latest")
    now = utc_now().isoformat(timespec="seconds")
    return [{"security_id": r["security_id"], "ticker_for_source": ticker, "reason": r["reason"],
             "needed_start": r["needed_start"], "needed_end": r["needed_end"], "order": order,
             "status": "wrong_entity", "entity_check": "precheck", "entity_notes": note, "updated_utc": now}
            for _, r in rows.iterrows()]


# ------------------------------------------------------------------ one ticker

def fetch_one(ticker: str, headers: dict | None, limiter, offline: bool = False) -> dict:
    """One ticker through ``cached_get``. ``outcome``: ok, no_data, quota, auth, error, not_cached."""
    from urllib.error import HTTPError

    path = raw_path(ticker)
    marker = path.with_name(path.name + ".404")
    out = {"ticker": ticker, "outcome": "", "prices": None, "http_status": "", "message": "",
           "raw_path": "", "raw_sha256": "", "bytes": 0, "fetched_utc": "", "requested": False}
    if offline and not path.exists() and not marker.exists():
        out["outcome"] = "not_cached"
        return out
    out["requested"] = not path.exists() and not marker.exists()
    try:
        data = common.cached_get(price_url(ticker), path, source=SOURCE, headers=headers, limiter=limiter,
                                 symbol=ticker.upper(), timeout=120, retries=3)
    except FileNotFoundError:
        out.update(outcome="no_data", http_status=404, message="404 (ticker not found)")
        return out
    except HTTPError as exc:
        try:
            body = exc.read()[:300].decode("utf-8", "replace")
        except Exception:  # the body may be gone
            body = ""
        text = common.redact(body)
        quota = exc.code == 429 or is_quota_text(text)
        out.update(outcome="quota" if quota else "auth", http_status=exc.code,
                   message=f"HTTP {exc.code}: {text}".strip())
        return out
    except RuntimeError as exc:  # cached_get gave up after its retries; the message is redacted
        out.update(outcome="error", message=str(exc)[:300])
        return out
    prices, error = parse_body(data)
    out.update(http_status=200, raw_path=str(path), bytes=len(data), raw_sha256=common.sha256_bytes(data),
               fetched_utc=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(timespec="seconds"))
    if prices is None:
        # an error object answered with 200 was cached: set it aside so a later run asks again
        aside = path.with_name(path.name + f".error-{utc_now():%Y%m%dT%H%M%S}")
        os.replace(path, aside)
        out.update(outcome="quota" if is_quota_text(error) else "error", message=error, raw_path=str(aside))
        return out
    out.update(outcome="ok" if prices else "no_data", prices=prices,
               message="" if prices else "empty answer (no rows in the window)")
    return out


def write_prices(ticker: str, frame: pd.DataFrame) -> Path:
    path = PRICES_DIR / f"{safe_name(ticker)}.csv.gz"
    text = frame.assign(date=frame["date"].dt.strftime("%Y-%m-%d")).to_csv(index=False)
    common.atomic_write(path, gzip.compress(text.encode("utf-8"), mtime=0))
    return path


def evaluate_rows(ticker: str, result: dict, rows: pd.DataFrame, order: int, sessions: pd.DatetimeIndex,
                  dv: dict, quotes: dict) -> list[dict]:
    """Status rows for every candidate row served by ``ticker``."""
    now = utc_now().isoformat(timespec="seconds")
    base = {"http_status": result["http_status"], "raw_path": result["raw_path"], "raw_sha256": result["raw_sha256"],
            "bytes": result["bytes"], "fetched_utc": result["fetched_utc"], "updated_utc": now, "order": order}
    frame = None
    if result["outcome"] == "ok":
        frame = to_frame(result["prices"])
        base.update(field_summary(result["prices"], frame))
    updates = []
    for _, row in rows.iterrows():
        update = {**base, "security_id": row["security_id"], "ticker_for_source": ticker, "reason": row["reason"],
                  "needed_start": row["needed_start"], "needed_end": row["needed_end"]}
        if frame is not None:
            check = entity_check(frame, row["needed_start"], row["needed_end"], sessions,
                                 dv.get(row["security_id"]), quotes.get(row["security_id"]))
            update.update(check, status=row_status(check))
        else:
            status = {"no_data": "no_data", "quota": "deferred_quota"}.get(result["outcome"], "error")
            update.update(status=status, entity_check="", entity_notes=result["message"])
        updates.append(update)
    return updates


# ------------------------------------------------------------------ the run

def take_lock(path: Path | None = None) -> None:
    """One fetcher at a time (the limiter and the budget are per process)."""
    path = path or LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            pid = int(path.read_text().split()[0])
            os.kill(pid, 0)
            raise SystemExit(f"another fetcher is running (pid {pid}); {path}")
        except (ValueError, IndexError, ProcessLookupError):
            log(f"stale lock {path.name} replaced")
        except PermissionError:
            raise SystemExit(f"another fetcher holds {path}")
    path.write_text(f"{os.getpid()} {utc_now().isoformat(timespec='seconds')}\n")


def release_lock(path: Path | None = None) -> None:
    path = path or LOCK
    try:
        if path.exists() and path.read_text().split()[0] == str(os.getpid()):
            path.unlink()
    except (OSError, IndexError):
        pass


def budget_state(month: str, already_used: int) -> dict:
    used = common.quota_used(SOURCE, month, unique_symbols=True)
    return {"ledger_unique_symbols": used, "already_used_outside_ledger": already_used,
            "counted_symbols": used + already_used, "rolling_30d_ledger_symbols": rolling_symbols(30),
            "requests_this_month": common.quota_used(SOURCE, month),
            "mb_this_month": round(bytes_this_month(month) / 1e6, 1)}


def plan_rows(args) -> tuple[pd.DataFrame, pd.DataFrame, list[str], pd.DataFrame]:
    candidates = load_candidates(Path(args.candidates))
    status = load_status()
    reasons = set(args.reasons.split(",")) if args.reasons else None
    selected = select_rows(candidates, status, set(args.statuses.split(",")), reasons, args.recheck)
    return candidates, status, ticker_order(selected), selected


def run(args) -> int:
    month = utc_now().strftime("%Y-%m")
    candidates, status, order, selected = plan_rows(args)
    position = {ticker: k + 1 for k, ticker in enumerate(order)}
    if args.tickers:
        wanted = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
        missing = [t for t in wanted if t not in position]
        if missing:
            log(f"not in this selection (final already, another status, or not a Tiingo row): {' '.join(missing)}")
        order = [t for t in order if t in set(wanted)]
    todo = order[: args.limit] if args.limit else order
    log(f"candidate rows for Tiingo: {len(candidates)}; selected rows: {len(selected)}; tickers in order: "
        f"{len(order)}; this run: {len(todo)}")
    by_tier = selected.groupby("reason")["ticker_for_source"].nunique().reindex(TIER_ORDER).dropna().astype(int)
    log("tickers by tier: " + ", ".join(f"{k} {v}" for k, v in by_tier.items()))
    if args.dry_run:
        latest = supported_latest()
        hidden = 0
        for ticker in todo:
            mine = selected[selected["ticker_for_source"] == ticker]
            first = mine.iloc[0]
            later = [shadowed_by(r, latest) for _, r in mine.iterrows()]
            skip = bool(later) and all(later) and not args.fetch_shadowed and not raw_path(ticker).exists()
            hidden += skip
            print(f"{position[ticker]:4d} {ticker:8s} {first['reason']:32s} best_rank={first['best_rank']:>6s} "
                  f"need {first['needed_start']}..{first['needed_end']} cached={raw_path(ticker).exists()}"
                  + (f" NOT ASKED (later row {later[0]['start']}..{later[0]['end']} {later[0]['asset_type']})"
                     if skip else ""))
        log(f"dry run: {len(todo)} tickers, {hidden} not asked (hidden by a later row), "
            f"{sum(raw_path(t).exists() for t in todo)} cached, so at most {len(todo) - hidden - sum(raw_path(t).exists() for t in todo)} new symbols")
        return 0
    take_lock()
    try:
        return _run(args, month, candidates, status, todo, position, selected)
    finally:
        release_lock()


def _run(args, month, candidates, status, todo, position, selected) -> int:
    started = utc_now().isoformat(timespec="seconds")
    before = budget_state(month, args.already_used)
    log(f"budget before: {before} (stop at {args.month_stop} symbols, {args.max_mb} MB)")
    if not args.already_used:
        log("note: --already-used is 0; symbols used this month outside the ledger (account page) are not counted")
    served = candidates[candidates["ticker_for_source"].isin(todo)]
    dv, quotes = load_references(set(served["security_id"]))
    log(f"references: dollar volume for {len(dv)} securities, LastSale for {len(quotes)}")
    sessions = xnas_sessions()
    headers = None if args.offline else auth_headers()
    day_ago = (utc_now() - timedelta(days=1)).isoformat(timespec="seconds")
    seed = ledger_rows()
    limiter = make_limiter(args.hourly, args.daily, seed[seed["fetched_utc"] >= day_ago], spacing=args.spacing)
    log(f"limiter: {args.hourly}/hour, {args.daily}/day, one per {args.spacing:g} s; seeded with "
        f"{len(limiter.stamps)} ledger requests of the last day")
    month_symbols = symbols_this_month(month)
    counted = before["counted_symbols"]
    # ``blocked``: a budget or rate stop. Uncached tickers after it are deferred; cached ones cost
    # nothing and are still checked. ``stop``: an auth failure or repeated errors end the run.
    stop, blocked, errors, done, skipped, prechecked = "", "", 0, [], [], []
    latest = supported_latest()
    log(f"Tiingo ticker list: {len(latest)} tickers (rows hidden by a later row are not asked"
        f"{'' if not args.fetch_shadowed else '; --fetch-shadowed: asked anyway'})")
    for k, ticker in enumerate(todo, 1):
        path = raw_path(ticker)
        cached = path.exists() or path.with_name(path.name + ".404").exists()
        new_symbol = not cached and ticker.upper() not in month_symbols
        mine = selected[selected["ticker_for_source"] == ticker]
        later = [shadowed_by(r, latest) for _, r in mine.iterrows()]
        if not cached and not args.fetch_shadowed and later and all(later):
            served_rows = candidates[candidates["ticker_for_source"] == ticker]
            served_rows = served_rows[[shadowed_by(r, latest) is not None for _, r in served_rows.iterrows()]]
            updates = precheck_rows(ticker, served_rows, later[0], position[ticker])
            status = merge_status(status, updates)
            write_status(status)
            prechecked.append(ticker)
            log(f"[{k}/{len(todo)}] {ticker} not asked (a later Tiingo row {later[0]['start']}..{later[0]['end']} "
                f"{later[0]['asset_type']} hides it) -> " + "; ".join(f"{u['security_id']}:wrong_entity" for u in updates))
            continue
        if not cached and not args.offline:
            if not blocked and new_symbol and counted >= args.month_stop:
                blocked = f"monthly symbol stop: {counted} counted >= {args.month_stop}"
            if not blocked and bytes_this_month(month) / 1e6 >= args.max_mb:
                blocked = f"monthly bytes stop: {bytes_this_month(month) / 1e6:.0f} MB >= {args.max_mb}"
            if blocked:
                skipped.append(ticker)
                continue
            delay = limiter_delay(limiter)
            if delay > max(90.0, args.spacing + 30):
                log(f"waiting {delay / 60:.1f} min for the rate window before {ticker}")
        result = fetch_one(ticker, headers, limiter, offline=args.offline)
        if result["requested"] and new_symbol:
            month_symbols.add(ticker.upper())
            counted = common.quota_used(SOURCE, month, unique_symbols=True) + args.already_used
        if result["outcome"] == "not_cached":
            log(f"[{k}/{len(todo)}] {ticker}: not cached (offline), skipped")
            continue
        rows = candidates[candidates["ticker_for_source"] == ticker]
        updates = evaluate_rows(ticker, result, rows, position[ticker], sessions, dv, quotes)
        if result["outcome"] == "ok":
            write_prices(ticker, to_frame(result["prices"]))
        if result["outcome"] == "auth":
            stop = f"auth stop on {ticker}: {result['message']}"
            for update in updates:
                update["status"] = "error"
            status = merge_status(status, updates)
            write_status(status)
            break
        if result["outcome"] == "quota":
            blocked = f"quota stop on {ticker}: {result['message']}"
            skipped.append(ticker)
            log(f"[{k}/{len(todo)}] {ticker}: {blocked}")
            continue
        errors = errors + 1 if result["outcome"] == "error" else 0
        status = merge_status(status, updates)
        write_status(status)
        done.append(ticker)
        first = updates[0]
        log(f"[{k}/{len(todo)}] {ticker} {result['outcome']} http={result['http_status']} "
            f"rows={first.get('rows_total', '')} {first.get('first_date', '')}..{first.get('last_date', '')} "
            f"{'cache' if not result['requested'] else 'fetched'} -> "
            + "; ".join(f"{u['security_id']}:{u['status']}" for u in updates)
            + f" | symbols counted {counted}")
        if errors >= MAX_CONSECUTIVE_ERRORS:
            stop = f"{errors} errors in a row (last: {result['message']})"
            break
    if blocked and skipped:
        left = selected[selected["ticker_for_source"].isin(skipped)]
        deferred = [{"security_id": r["security_id"], "ticker_for_source": r["ticker_for_source"],
                     "reason": r["reason"], "needed_start": r["needed_start"], "needed_end": r["needed_end"],
                     "order": position[r["ticker_for_source"]], "status": "deferred_quota",
                     "entity_notes": blocked, "updated_utc": utc_now().isoformat(timespec="seconds")}
                    for _, r in left.iterrows()]
        status = merge_status(status, deferred)
        write_status(status)
        log(f"STOP: {blocked}; deferred_quota: {len(deferred)} rows, {len(skipped)} tickers")
    if stop:
        log(f"STOP: {stop}")
    after = budget_state(month, args.already_used)
    summary = {"started_utc": started, "finished_utc": utc_now().isoformat(timespec="seconds"), "month": month,
               "args": {k: v for k, v in vars(args).items()}, "tickers_processed": done,
               "tickers_deferred": skipped, "tickers_not_asked_hidden_by_later_row": prechecked,
               "stop": stop or blocked,
               "budget_before": before, "budget_after": after,
               "status_counts": status["status"].value_counts().to_dict() if len(status) else {}}
    common.atomic_write(SUMMARY, (json.dumps(summary, indent=1, default=str) + "\n").encode("utf-8"))
    log(f"budget after: {after}")
    log(f"status file: {STATUS} ({len(status)} rows: {summary['status_counts']})")
    return 1 if stop else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--limit", type=int, default=0, help="process only the first N tickers in priority order")
    parser.add_argument("--dry-run", action="store_true", help="print the order; no request")
    parser.add_argument("--offline", action="store_true", help="use cached answers only; no request")
    parser.add_argument("--recheck", action="store_true",
                        help="also re-evaluate rows already final in the status file (from cache)")
    parser.add_argument("--already-used", type=int, default=0,
                        help="symbols used this month that the ledger does not show (Tiingo account page)")
    parser.add_argument("--month-stop", type=int, default=MONTH_STOP)
    parser.add_argument("--max-mb", type=float, default=MAX_MB)
    parser.add_argument("--hourly", type=int, default=HOURLY)
    parser.add_argument("--daily", type=int, default=DAILY)
    parser.add_argument("--spacing", type=float, default=SPACING, help="minimum seconds between requests")
    parser.add_argument("--fetch-shadowed", action="store_true",
                        help="ask Tiingo even when a later row in its ticker list hides the matched row")
    parser.add_argument("--statuses", default=",".join(sorted(RUN_STATUSES)),
                        help="candidate-list statuses to fetch (month 2: pending_month2,conditional_tier_c)")
    parser.add_argument("--reasons", default="", help="comma-separated reasons to restrict to")
    parser.add_argument("--tickers", default="", help="comma-separated tickers to restrict to (kept in priority order)")
    parser.add_argument("--candidates", default=str(CANDIDATES))
    args = parser.parse_args(argv)
    if args.hourly > HOURLY or args.daily > DAILY or args.month_stop > MONTH_STOP:
        parser.error(f"limits above {HOURLY}/hour, {DAILY}/day or {MONTH_STOP} symbols are not allowed")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
