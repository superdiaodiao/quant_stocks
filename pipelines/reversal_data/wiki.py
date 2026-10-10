"""Step 5 of docs/reversal_2012_2026_data_plan.md: the free Nasdaq Data Link WIKI/PRICES table.

Data only. This script downloads the WIKI end-of-day table (it stopped on
2018-03-27) for dates on or after 2011-06-01, keeps every ticker (NYSE ones
too; the universe step filters later) and every column, raw and adjusted:
``open, high, low, close, volume, ex-dividend, split_ratio`` as traded and
``adj_open, adj_high, adj_low, adj_close, adj_volume``.

It never computes strategy or portfolio returns, signals or rankings by
return. The checks it writes are per-stock data checks (row counts, date
ranges, calendar gaps, missing fields, OHLC consistency, whether WIKI's own
adjustment factor moves only on its split and dividend days), a coverage
check against the Form 25 delisting list, and a level-basis comparison with
the repo's stored price files, reported as price ratios.

Order of attempts (each request goes through ``cached_get``, so a rerun
resumes from the cache and every request is logged with the key redacted):

1. bulk export with the date filter: ``qopts.export=true&date.gte=2011-06-01``,
   polled until the file is ready, then the zip it links to;
2. if the filtered export never becomes ready, the unfiltered export (the
   whole table, filtered locally);
3. if neither works, cursor paging: ``date.gte=2011-06-01``, 10,000 rows a
   page, following ``meta.next_cursor_id``.

Small extra requests, all cached and rerun from the cache:
  * the previous XNAS session (2011-05-31) for the whole table, so a first row
    dated 2011-06-01 has a prior close (dividend percentage, split table);
  * the one-off key probe (AAPL from 2018-03-20) made before the export, and
    whole-history probes of large names delisted in 2012-2013, which show
    whether WIKI itself lacks them or only the export does.

Reading the outputs: always ``dtype={"ticker": str}, keep_default_na=False,
na_values=[""]`` (``read_ticker_file``). With default pandas options the
ticker TRUE reads as a boolean and a ticker such as NA would read as NaN.

Outputs (local only, under research_cache/reversal_2012_2026):
  raw/wiki/WIKI_PRICES_{fetched_utc}.zip (+ .json sidecar)  or raw/wiki/pages/NNNNN_{cursor}.json.gz
  raw/wiki/export/status_{utc}.json      every export poll response
  raw/wiki/prior/..., raw/wiki/probe/... the small requests above
  wiki/by_ticker/{TICKER}.csv.gz         one file per ticker, rows dated >= 2011-06-01
  wiki/wiki_ticker_summary.csv           per-ticker coverage and data-error counts
  wiki/wiki_flags.csv.gz                 one row per flagged ticker-day
  wiki/wiki_missing_sessions.csv.gz      one row per XNAS session missing inside a ticker's span
  wiki/wiki_split_events.csv, wiki/wiki_dividends.csv
  wiki/wiki_prior_rows.csv               the last row before 2011-06-01 per ticker (prior close only)
  wiki/wiki_date_counts.csv              tickers per date, against the XNAS calendar
  wiki/wiki_form25_coverage.csv          per Form 25 Nasdaq common delisting: does WIKI have it
  wiki/stored_consistency_10.csv (+ _daily.csv.gz)   ratios against cleaned_stocks_data/price
  wiki/wiki_build.json                   counts, survivorship, probes, file sizes and hashes
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
import zipfile

import numpy as np
import pandas as pd

from pipelines.reversal_data.common import (
    CACHE,
    INPUTS,
    MAIN_CHECKOUT,
    RAW,
    SlidingWindowLimiter,
    atomic_write,
    cached_get,
    read_env_key,
    redact,
    sha256_file,
)

SOURCE = "nasdaqdatalink_wiki"
ENV_PATH = MAIN_CHECKOUT / ".env.nasdaqdatalink"
KEY_NAME = "NASDAQ_DATA_LINK_API_KEY"
BASE_URL = "https://data.nasdaq.com/api/v3/datatables/WIKI/PRICES"
START = "2011-06-01"
WIKI_LAST_DATE = "2018-03-27"
RAW_WIKI = RAW / "wiki"
OUT = CACHE / "wiki"
BY_TICKER = OUT / "by_ticker"
STORED_PRICES = MAIN_CHECKOUT / "cleaned_stocks_data" / "price"
FORM25_PATH = INPUTS / "form25_nasdaq_2012_2026.csv"
MASTER_PATH = INPUTS / "security_master.csv"
CHECK_TICKERS = ["AAPL", "MSFT", "INTC", "CSCO", "NFLX", "SBUX", "MNST", "CTSH", "COST", "KLAC"]
COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume", "ex-dividend", "split_ratio",
           "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume"]
NUMERIC = COLUMNS[2:]
READ_KW = {"dtype": {"ticker": str, "date": str}, "keep_default_na": False, "na_values": [""]}
PER_PAGE = 10_000
# Free-key limits are documented as 300 calls / 10 s and 2,000 / 10 min; stay far under them.
LIMITER = SlidingWindowLimiter({10: 10, 600: 300})
# Tolerances for the data checks.
ADJ_FACTOR_TOL = 1e-4      # relative error allowed in WIKI's own adjustment-factor step
CENT_ROUNDING = 0.005      # half a cent: many WIKI adjusted series are rounded to cents
BIG_RATIO_HI, BIG_RATIO_LO = 2.0, 0.5   # raw close ratio with no split that day (rule R1)
GAP_SESSIONS = 10          # rule R9
FLAT_RUN = 3               # rule R4
PRIOR_WINDOW_DAYS = 14     # calendar days before START searched for a prior close
# Survivorship: no WIKI ticker's rows end before this date (found in round 1, checked on every build).
SURVIVOR_CUTOFF = "2014-04-01"
COVER_BEFORE_DAYS, COVER_AFTER_DAYS = 30, 5   # a delisting is covered if WIKI traded the ticker in this window
PERIODS = {"2012-01..2014-03": ("2012-01-01", "2014-03-31"), "2014-04..2018-03": ("2014-04-01", WIKI_LAST_DATE)}
# The key probe made by hand before the export (round 1), now reproduced from its cache.
KEY_PROBE = {"ticker": "AAPL", "date.gte": "2018-03-20"}
KEY_PROBE_FILE = "AAPL_2018-03-20.json"
# Large names that stopped trading between 2011-06 and 2014-03 and have no WIKI file in the export.
DELISTED_PROBES = ["BMC", "ONXX", "MOLX", "CVH", "NYX", "HNZ", "PCS", "VMED", "MHS", "EP", "DELL"]
# Ratio steps in the stored-file comparison: a persistent level change of the stored/WIKI basis.
STEP_WINDOW, STEP_THRESHOLD = 5, 0.002


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_url(params: dict, api_key: str, fmt: str = "json") -> str:
    """The request URL; the key goes last so redact() leaves the rest readable."""
    query = urlencode({**params, "api_key": api_key})
    return f"{BASE_URL}.{fmt}?{query}"


def read_ticker_file(path) -> pd.DataFrame:
    """A WIKI output CSV with tickers kept as text (TRUE stays 'TRUE', NA stays 'NA')."""
    return pd.read_csv(path, **READ_KW)


def misread_by_default(tickers) -> list[str]:
    """Tickers that a default ``pd.read_csv`` of a one-ticker file would not give back as the same text."""
    bad = []
    for ticker in tickers:
        back = pd.read_csv(io.StringIO(f"ticker\n{ticker}\n{ticker}\n"))["ticker"]
        if not all(isinstance(value, str) and value == ticker for value in back):
            bad.append(ticker)
    return bad


# --------------------------------------------------------------------------- parsers

def parse_export_status(payload: bytes) -> dict:
    """``{status, link, snapshot_time, last_refreshed}`` from a qopts.export=true response."""
    body = json.loads(payload)
    if "quandl_error" in body or "error" in body:
        raise RuntimeError(f"export refused: {body.get('quandl_error') or body.get('error')}")
    bulk = body.get("datatable_bulk_download") or {}
    file_info = bulk.get("file") or {}
    return {
        "status": file_info.get("status"),
        "link": file_info.get("link"),
        "snapshot_time": file_info.get("data_snapshot_time"),
        "last_refreshed": (bulk.get("datatable") or {}).get("last_refreshed_time"),
    }


def parse_page(payload: bytes) -> tuple[pd.DataFrame, str | None]:
    """Rows and the next cursor from one paged datatable JSON response."""
    body = json.loads(payload)
    if "quandl_error" in body:
        raise RuntimeError(f"page refused: {body['quandl_error']}")
    table = body["datatable"]
    names = [column["name"] for column in table["columns"]]
    frame = pd.DataFrame(table["data"], columns=names)
    return frame, (body.get("meta") or {}).get("next_cursor_id")


def _coerce(frame: pd.DataFrame) -> pd.DataFrame:
    missing = [c for c in COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"WIKI rows are missing columns {missing}")
    frame = frame[COLUMNS].copy()
    frame["ticker"] = frame["ticker"].astype(str)
    frame["date"] = frame["date"].astype(str).str.slice(0, 10)
    for column in NUMERIC:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame


def has_header_row(head: bytes) -> bool:
    return head.split(b"\n", 1)[0].decode("utf-8", "replace").strip().lower().startswith("ticker,")


def read_export_csv(handle, has_header: bool, start: str = START, chunksize: int = 1_000_000) -> pd.DataFrame:
    """Rows dated >= ``start`` from the CSV inside the export zip (header row optional)."""
    reader = pd.read_csv(handle, header=0 if has_header else None, names=None if has_header else COLUMNS,
                         chunksize=chunksize, low_memory=False, **READ_KW)
    parts = []
    for chunk in reader:
        chunk = _coerce(chunk)
        parts.append(chunk[chunk["date"] >= start])
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(parts, ignore_index=True)


def read_export_zip(path: Path, start: str = START) -> pd.DataFrame:
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"{path.name}: expected one CSV member, found {members}")
        with archive.open(members[0]) as handle:
            header = has_header_row(handle.read(4096))
        with archive.open(members[0]) as handle:
            return read_export_csv(handle, header, start)


def safe_ticker(ticker: str) -> str:
    """A filename for a WIKI ticker (they use letters, digits, '_' and '.')."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", ticker)


def wiki_symbol(ticker: str) -> str:
    """A listing or SEC ticker in WIKI's spelling: class shares use '_' (BRK.B, BRK-B -> BRK_B)."""
    return re.sub(r"[.\-/ ]", "_", ticker.strip().upper())


# --------------------------------------------------------------------------- acquisition

def _api_key() -> str:
    return read_env_key(ENV_PATH, KEY_NAME)


def api_get(params: dict, cache_path: Path, symbol: str = "") -> bytes:
    """A WIKI API response, from the cache when present (then the key is not even read)."""
    cache_path = Path(cache_path)
    if cache_path.exists():
        data = cache_path.read_bytes()
        return gzip.decompress(data) if cache_path.suffix == ".gz" else data
    return cached_get(build_url(params, _api_key()), cache_path, source=SOURCE, limiter=LIMITER, symbol=symbol)


def _cursor_tag(cursor: str | None) -> str:
    return "first" if not cursor else hashlib.sha256(cursor.encode()).hexdigest()[:12]


def fetch_paged(params: dict, cache_dir: Path, max_pages: int = 5000, symbol: str = "",
                restart_on_error: bool = True) -> list[Path]:
    """Every page of a cursor-paged query, cached as NNNNN_{hash of the cursor that asked for it}.json.gz.

    The cursor is part of each page's cache name, so a resumed run follows exactly the chain it
    cached. If a page cannot be fetched with the cursor of the page before it (an expired cursor),
    the cached chain is moved aside (never deleted) and paging starts again from the first page, once.
    Quota and permission refusals (401/403/429) are raised, not retried.
    """
    cache_dir = Path(cache_dir)
    paths, cursor = [], None
    for number in range(max_pages):
        page_params = dict(params)
        if cursor:
            page_params["qopts.cursor_id"] = cursor
        path = cache_dir / f"{number:05d}_{_cursor_tag(cursor)}.json.gz"
        try:
            frame, cursor = parse_page(api_get(page_params, path, symbol))
        except (RuntimeError, FileNotFoundError) as exc:
            if number == 0 or not restart_on_error:
                raise
            stale = cache_dir.with_name(f"{cache_dir.name}_stale_{_utc_stamp()}")
            cache_dir.rename(stale)
            print(f"page {number} failed ({redact(str(exc))[:120]}); chain moved to {stale.name}, restarting",
                  flush=True)
            return fetch_paged(params, cache_dir, max_pages, symbol, restart_on_error=False)
        paths.append(path)
        if number % 25 == 0 and number:
            print(f"page {number}: {len(frame)} rows", flush=True)
        if not cursor:
            return paths
    raise RuntimeError(f"paging did not finish within {max_pages} pages")


def read_pages(paths: list[Path], start: str | None = START) -> pd.DataFrame:
    frames = [parse_page(gzip.decompress(p.read_bytes()))[0] for p in paths]
    frame = _coerce(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame(columns=COLUMNS)
    if start is not None:
        frame = frame[frame["date"] >= start]
    return frame.reset_index(drop=True)


def existing_zip() -> Path | None:
    for path in sorted(RAW_WIKI.glob("WIKI_PRICES_*.zip"), reverse=True):
        if zipfile.is_zipfile(path):
            return path
    return None


def request_export(api_key: str, filtered: bool, max_wait_s: float, poll_s: float = 30.0) -> dict | None:
    """Poll the export endpoint until the file is ready; the final status dict, or None on timeout."""
    params = {"date.gte": START, "qopts.export": "true"} if filtered else {"qopts.export": "true"}
    url = build_url(params, api_key)
    started = time.monotonic()
    tag = "filtered" if filtered else "full"
    while True:
        cache_path = RAW_WIKI / "export" / f"status_{tag}_{_utc_stamp()}.json"
        payload = cached_get(url, cache_path, source=SOURCE, limiter=LIMITER, refetch=True)
        status = parse_export_status(payload)
        print(f"export ({tag}): status={status['status']} snapshot={status['snapshot_time']}", flush=True)
        if status["status"] == "fresh" and status["link"]:
            # the presigned link carries temporary credentials: keep only its path in the cached status
            body = json.loads(payload)
            body["datatable_bulk_download"]["file"]["link"] = redact(status["link"])
            atomic_write(cache_path, json.dumps(body, separators=(",", ":")).encode())
            return {**status, "params": params, "status_file": str(cache_path)}
        if time.monotonic() - started > max_wait_s:
            return None
        time.sleep(poll_s)


def download_export(status: dict) -> Path:
    stamp = _utc_stamp()
    path = RAW_WIKI / f"WIKI_PRICES_{stamp}.zip"
    cached_get(status["link"], path, source=SOURCE, limiter=LIMITER, timeout=600)
    if not zipfile.is_zipfile(path):
        bad = path.with_name(path.name + ".bad")
        path.rename(bad)
        raise RuntimeError(f"export download is not a zip (kept as {bad.name})")
    sidecar = {
        "fetched_utc": stamp,
        "endpoint": f"{BASE_URL}.json",
        "params_no_key": status["params"],
        "export_status": status["status"],
        "data_snapshot_time": status["snapshot_time"],
        "last_refreshed_time": status["last_refreshed"],
        "status_file": status["status_file"],
        "link_redacted": redact(status["link"].split("?", 1)[0]),
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    atomic_write(path.with_suffix(".json"), (json.dumps(sidecar, indent=2) + "\n").encode())
    return path


def fetch_pages(max_pages: int = 5000) -> list[Path]:
    """Cursor paging fallback for the whole table from START."""
    return fetch_paged({"date.gte": START, "qopts.per_page": PER_PAGE}, RAW_WIKI / "pages", max_pages)


def acquire(max_wait_s: float, skip_filtered: bool = False) -> tuple[pd.DataFrame, dict]:
    """The WIKI rows dated >= START and a description of where they came from."""
    found = existing_zip()
    if found is not None:
        print(f"using cached {found.name}", flush=True)
        return read_export_zip(found), {"method": "export_zip", "raw_path": str(found)}
    pages_dir = RAW_WIKI / "pages"
    if pages_dir.exists() and any(pages_dir.glob("*.json.gz")):
        print("resuming cursor paging from cached pages", flush=True)
        paths = fetch_pages()
        return read_pages(paths), {"method": "cursor_pages", "raw_path": str(pages_dir), "pages": len(paths)}
    api_key = _api_key()
    attempts = [] if skip_filtered else [True]
    attempts.append(False)
    for filtered in attempts:
        try:
            status = request_export(api_key, filtered, max_wait_s)
        except Exception as exc:  # noqa: BLE001 - try the next method, but say why
            print(f"export ({'filtered' if filtered else 'full'}) failed: {redact(str(exc))}", flush=True)
            continue
        if status is None:
            print(f"export ({'filtered' if filtered else 'full'}) not ready after {max_wait_s:.0f}s", flush=True)
            continue
        try:
            path = download_export(status)
        except Exception as exc:  # noqa: BLE001
            print(f"export download failed: {redact(str(exc))}", flush=True)
            continue
        return read_export_zip(path), {"method": "export_zip", "raw_path": str(path), "filtered": filtered}
    paths = fetch_pages()
    return read_pages(paths), {"method": "cursor_pages", "raw_path": str(pages_dir), "pages": len(paths)}


def fetch_prior_rows(tickers: list[str], start: str = START) -> tuple[pd.DataFrame, dict]:
    """The last WIKI row dated before ``start`` for each ticker, used only as a prior close.

    One query for the whole table on the previous XNAS session; tickers in ``tickers`` that have
    no row that day get one more query over the ``PRIOR_WINDOW_DAYS`` calendar days before ``start``.
    """
    session = previous_session(start)
    paths = fetch_paged({"date": session, "qopts.per_page": PER_PAGE}, RAW_WIKI / "prior" / f"date_{session}",
                        max_pages=20)
    frames = [read_pages(paths, start=None)]
    facts = {"session": session, "session_pages": len(paths), "session_rows": int(len(frames[0]))}
    missing = sorted(set(tickers) - set(frames[0]["ticker"]))
    window_start = (pd.Timestamp(start) - pd.Timedelta(days=PRIOR_WINDOW_DAYS)).strftime("%Y-%m-%d")
    for offset in range(0, len(missing), 100):
        chunk = missing[offset:offset + 100]
        tag = hashlib.sha256(",".join(chunk).encode()).hexdigest()[:12]
        params = {"ticker": ",".join(chunk), "date.gte": window_start, "date.lt": start, "qopts.per_page": PER_PAGE}
        paths = fetch_paged(params, RAW_WIKI / "prior" / f"window_{window_start}_{tag}", max_pages=20)
        frames.append(read_pages(paths, start=None))
    rows = pd.concat(frames, ignore_index=True)
    rows = rows[rows["date"] < start].sort_values(["ticker", "date"]).groupby("ticker").tail(1)
    facts.update({"window_start": window_start, "tickers_needing_window": len(missing),
                  "tickers_with_prior_row": int(rows["ticker"].nunique()),
                  "tickers_asked_without_prior_row": int(len(set(tickers) - set(rows["ticker"])))})
    return rows.reset_index(drop=True), facts


# --------------------------------------------------------------------------- data checks

def xnas_sessions(start: str = START, end: str = WIKI_LAST_DATE) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNAS", start=start, end=end)
    return pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)


def previous_session(day: str = START) -> str:
    """The XNAS session before ``day``."""
    import exchange_calendars as xcals

    first = (pd.Timestamp(day) - pd.Timedelta(days=PRIOR_WINDOW_DAYS)).strftime("%Y-%m-%d")
    sessions = pd.DatetimeIndex(xcals.get_calendar("XNAS", start=first, end=day).sessions).tz_localize(None)
    return sessions[sessions < pd.Timestamp(day)][-1].strftime("%Y-%m-%d")


def adjustment_factor_breaks(group: pd.DataFrame, tol: float = ADJ_FACTOR_TOL) -> pd.Series:
    """True where WIKI's own factor adj_close/close moves by other than that day's split and dividend.

    With CRSP-style multiplicative adjustment, A_{t-1} / A_t = (1 / S_t) * (1 - D_t / C_{t-1}),
    so on a day with no split and no dividend the factor must not move. Many WIKI series carry
    adjusted prices rounded to the cent, so the allowance adds half a cent on each adjusted close
    (for a $2 stock that is 0.5%); what is left is a real break, not rounding.
    """
    close, adj = group["close"], group["adj_close"]
    factor = adj / close
    split = group["split_ratio"].fillna(1.0).replace(0.0, 1.0)
    dividend = group["ex-dividend"].fillna(0.0)
    expected = (1.0 / split) * (1.0 - dividend / close.shift(1))
    observed = factor.shift(1) / factor
    error = (observed / expected - 1.0).abs()
    allowance = tol + CENT_ROUNDING / adj.abs() + CENT_ROUNDING / adj.shift(1).abs()
    out = error > allowance
    out.iloc[:1] = False
    return out.fillna(False)


def flat_runs(close: pd.Series, length: int = FLAT_RUN) -> list[tuple[int, int]]:
    """(start position, run length) of runs of >= ``length`` identical non-null closes."""
    runs, start = [], 0
    values = close.to_numpy()
    for position in range(1, len(values) + 1):
        if position < len(values) and values[position] == values[position - 1] and not np.isnan(values[position]):
            continue
        if position - start >= length and not np.isnan(values[start]):
            runs.append((start, position - start))
        start = position
    return runs


def check_ticker(group: pd.DataFrame, sessions: pd.DatetimeIndex, prior: dict | None = None
                 ) -> tuple[dict, list[dict], list[dict], list[dict], list[dict]]:
    """Per-ticker summary row, flag rows, split rows, dividend rows and missing-session rows.

    ``group`` is one ticker's rows sorted by date. ``prior`` ({"date", "close"}) is the ticker's
    last row before START; it is the previous close of a first row dated START.
    """
    ticker = group["ticker"].iloc[0]
    dates = pd.to_datetime(group["date"])
    close = group["close"]
    flags: list[dict] = []

    def flag(mask, rule, detail=None):
        for position in np.flatnonzero(np.asarray(mask)):
            flags.append({"ticker": ticker, "date": group["date"].iloc[position], "rule": rule,
                          "detail": "" if detail is None else detail(position)})

    duplicated = group["date"].duplicated(keep="first")
    flag(duplicated, "duplicate_date")
    null_close = close.isna()
    flag(null_close, "null_close")
    null_fields = group[["open", "high", "low"]].isna()
    null_ohlc = null_fields.any(axis=1) & ~null_close
    flag(null_ohlc, "null_ohlc",
         lambda p: "missing=" + ",".join(c for c in ("open", "high", "low") if null_fields[c].iloc[p])
                   + f" close={close.iloc[p]}")
    nonpositive = close <= 0
    flag(nonpositive, "nonpositive_close")
    high = group["high"]
    low = group["low"]
    top = group[["open", "close", "low"]].max(axis=1)
    bottom = group[["open", "close", "high"]].min(axis=1)
    ohlc_bad = (high < top * (1 - 1e-9)) | (low > bottom * (1 + 1e-9))
    flag(ohlc_bad, "ohlc_inconsistent",
         lambda p: f"o={group['open'].iloc[p]} h={high.iloc[p]} l={low.iloc[p]} c={close.iloc[p]}")
    zero_volume = group["volume"].fillna(0) <= 0
    flag(zero_volume, "zero_volume")

    split = group["split_ratio"].fillna(1.0)
    dividend = group["ex-dividend"].fillna(0.0)
    previous = close.shift(1)
    previous_date = group["date"].shift(1).astype(object)
    previous_source = pd.Series("same_ticker", index=group.index, dtype=object)
    previous_source.iloc[0] = "no_prior_row" if group["date"].iloc[0] == START else "none_first_row"
    if prior is not None and group["date"].iloc[0] == START and pd.notna(prior.get("close")):
        previous.iloc[0] = prior["close"]
        previous_date.iloc[0] = prior["date"]
        previous_source.iloc[0] = "prior_request"
    ratio = close / previous
    big = ((ratio >= BIG_RATIO_HI) | (ratio <= BIG_RATIO_LO)) & (split == 1.0)
    flag(big, "raw_close_ratio_no_split", lambda p: f"prev={previous.iloc[p]} close={close.iloc[p]}")
    breaks = adjustment_factor_breaks(group)
    flag(breaks, "adj_factor_moves_off_event",
         lambda p: f"adj/close prev={group['adj_close'].iloc[p - 1] / close.iloc[p - 1]:.8g} "
                   f"now={group['adj_close'].iloc[p] / close.iloc[p]:.8g}")

    in_span = sessions[(sessions >= dates.iloc[0]) & (sessions <= dates.iloc[-1])]
    present = pd.DatetimeIndex(dates.unique())
    off_calendar = ~dates.isin(sessions)
    flag(off_calendar, "not_an_xnas_session")
    missing = in_span.difference(present)
    session_positions = pd.Series(np.arange(len(in_span)), index=in_span)
    missing_rows: list[dict] = []
    if len(missing):
        positions = session_positions.loc[missing].to_numpy()
        cuts = np.flatnonzero(np.diff(positions) != 1)
        for first, last in zip(np.r_[0, cuts + 1], np.r_[cuts, len(positions) - 1]):
            run_start, run_end = missing[first].strftime("%Y-%m-%d"), missing[last].strftime("%Y-%m-%d")
            missing_rows.extend({"ticker": ticker, "date": day.strftime("%Y-%m-%d"), "run_start": run_start,
                                 "run_end": run_end, "run_sessions": int(last - first + 1)}
                                for day in missing[first:last + 1])
    on_sessions = present[present.isin(in_span)].sort_values()
    max_gap = 0
    if len(on_sessions) > 1:
        positions = session_positions.loc[on_sessions].to_numpy()
        gaps = np.diff(positions) - 1
        max_gap = int(gaps.max()) if len(gaps) else 0
        for index in np.flatnonzero(gaps > GAP_SESSIONS):
            resume = on_sessions[index + 1].strftime("%Y-%m-%d")
            before = group.loc[group["date"] == on_sessions[index].strftime("%Y-%m-%d"), "close"].iloc[0]
            after = group.loc[group["date"] == resume, "close"].iloc[0]
            flags.append({"ticker": ticker, "date": resume, "rule": "gap_gt_10_sessions",
                          "detail": f"missing={int(gaps[index])} close_before={before} close_after={after}"})
    runs = flat_runs(close)
    for start, length in runs:
        flags.append({"ticker": ticker, "date": group["date"].iloc[start], "rule": "flat_close_run",
                      "detail": f"length={length} close={close.iloc[start]}"})

    def previous_fields(p):
        source = previous_source.iloc[p]
        if pd.isna(previous.iloc[p]) and source == "same_ticker":
            source = "prev_close_missing"
        return {"close_prev_raw": previous.iloc[p], "prev_date": previous_date.iloc[p], "prev_source": source}

    split_rows = [{"ticker": ticker, "date": group["date"].iloc[p], "split_ratio": split.iloc[p],
                   "close_raw": close.iloc[p], **previous_fields(p)}
                  for p in np.flatnonzero((split != 1.0).to_numpy())]
    dividend_rows = [{"ticker": ticker, "date": group["date"].iloc[p], "ex_dividend": dividend.iloc[p],
                      **previous_fields(p),
                      "pct_of_prior_close": (dividend.iloc[p] / previous.iloc[p]
                                             if pd.notna(previous.iloc[p]) and previous.iloc[p] > 0 else np.nan)}
                     for p in np.flatnonzero((dividend != 0.0).to_numpy())]
    traded = np.flatnonzero((group["volume"].fillna(0) > 0).to_numpy())
    dollar_volume = (close * group["volume"]).median()
    summary = {
        "ticker": ticker,
        "first_date": group["date"].iloc[0],
        "last_date": group["date"].iloc[-1],
        "last_traded_date": group["date"].iloc[traded[-1]] if len(traded) else "",
        "rows_after_last_traded": int(len(group) - 1 - traded[-1]) if len(traded) else int(len(group)),
        "rows": len(group),
        "xnas_sessions_in_span": len(in_span),
        "missing_sessions": len(missing),
        "missing_session_runs": len({row["run_start"] for row in missing_rows}),
        "max_gap_sessions": max_gap,
        "off_calendar_rows": int(off_calendar.sum()),
        "duplicate_dates": int(duplicated.sum()),
        "null_close": int(null_close.sum()),
        "null_open": int(null_fields["open"].sum()),
        "null_high": int(null_fields["high"].sum()),
        "null_low": int(null_fields["low"].sum()),
        "null_volume": int(group["volume"].isna().sum()),
        "nonpositive_close": int(nonpositive.sum()),
        "ohlc_inconsistent": int(ohlc_bad.sum()),
        "zero_volume_days": int(zero_volume.sum()),
        "splits": len(split_rows),
        "dividends": len(dividend_rows),
        "raw_ratio_flags": int(big.sum()),
        "adj_factor_breaks": int(breaks.sum()),
        "flat_runs_ge3": len(runs),
        "median_dollar_volume_raw": dollar_volume,
        "ends_before_wiki_end": group["date"].iloc[-1] < WIKI_LAST_DATE,
    }
    return summary, flags, split_rows, dividend_rows, missing_rows


def _csv_gz_bytes(frame: pd.DataFrame) -> bytes:
    return gzip.compress(frame.to_csv(index=False).encode("utf-8"), mtime=0)


def _remove_stale(directory: Path, written: set[str]) -> dict:
    """Delete per-ticker files from an older build that no ticker writes now.

    Names are compared without case, because APFS is case-insensitive: a file written as ABC.csv.gz
    may be listed under an older spelling such as Abc.csv.gz, and deleting that would delete the new
    file. Such a file is the same file (checked by inode) and is renamed to the current spelling instead.
    """
    by_lower = {name.lower(): name for name in written}
    removed, renamed = [], []
    for path in sorted(directory.glob("*.csv.gz")):
        if path.name in written:
            continue
        current = by_lower.get(path.name.lower())
        target = directory / current if current else None
        if target is not None and target.exists() and path.samefile(target):
            temporary = path.with_name(path.name + ".case")
            path.rename(temporary)
            temporary.rename(target)
            renamed.append(current)
        else:
            path.unlink()
            removed.append(path.name)
    return {"removed": removed, "renamed": renamed}


def build_outputs(frame: pd.DataFrame, origin: dict, prior: pd.DataFrame | None = None) -> dict:
    """Write per-ticker files and the data-check tables; the build facts."""
    frame = frame.sort_values(["ticker", "date"], kind="stable").reset_index(drop=True)
    sessions = xnas_sessions()
    prior_by_ticker = {} if prior is None else {
        row.ticker: {"date": row.date, "close": row.close} for row in prior.itertuples(index=False)}
    summaries, flags, splits, dividends, missing = [], [], [], [], []
    BY_TICKER.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    lower_names: dict[str, str] = {}
    for ticker, group in frame.groupby("ticker", sort=True):
        group = group.reset_index(drop=True)
        name = f"{safe_ticker(ticker)}.csv.gz"
        if name.lower() in lower_names:  # one file on a case-insensitive file system
            raise ValueError(f"tickers {lower_names[name.lower()]!r} and {ticker!r} map to the file name {name}")
        lower_names[name.lower()] = ticker
        written.add(name)
        atomic_write(BY_TICKER / name, _csv_gz_bytes(group))
        summary, ticker_flags, split_rows, dividend_rows, missing_rows = check_ticker(
            group, sessions, prior_by_ticker.get(ticker))
        summaries.append(summary)
        flags.extend(ticker_flags)
        splits.extend(split_rows)
        dividends.extend(dividend_rows)
        missing.extend(missing_rows)
    cleanup = _remove_stale(BY_TICKER, written)

    summary_frame = pd.DataFrame(summaries)
    atomic_write(OUT / "wiki_ticker_summary.csv", summary_frame.to_csv(index=False).encode())
    flag_frame = pd.DataFrame(flags, columns=["ticker", "date", "rule", "detail"])
    atomic_write(OUT / "wiki_flags.csv.gz", _csv_gz_bytes(flag_frame))
    missing_frame = pd.DataFrame(missing, columns=["ticker", "date", "run_start", "run_end", "run_sessions"])
    atomic_write(OUT / "wiki_missing_sessions.csv.gz", _csv_gz_bytes(missing_frame))
    previous_columns = ["close_prev_raw", "prev_date", "prev_source"]
    atomic_write(OUT / "wiki_split_events.csv", pd.DataFrame(
        splits, columns=["ticker", "date", "split_ratio", "close_raw", *previous_columns]).to_csv(index=False).encode())
    dividend_frame = pd.DataFrame(
        dividends, columns=["ticker", "date", "ex_dividend", *previous_columns, "pct_of_prior_close"])
    atomic_write(OUT / "wiki_dividends.csv", dividend_frame.to_csv(index=False).encode())
    if prior is not None:
        atomic_write(OUT / "wiki_prior_rows.csv", prior[COLUMNS].to_csv(index=False).encode())

    counts = frame.groupby("date")["ticker"].nunique().rename("n_tickers").reset_index()
    session_text = set(sessions.strftime("%Y-%m-%d"))
    counts["is_xnas_session"] = counts["date"].isin(session_text)
    missing_sessions = sorted(session_text - set(counts["date"]))
    counts = pd.concat([counts, pd.DataFrame({"date": missing_sessions, "n_tickers": 0,
                                              "is_xnas_session": True})]).sort_values("date")
    in_span_missing = missing_frame["date"].value_counts()
    counts["n_tickers_missing_in_span"] = counts["date"].map(in_span_missing).fillna(0).astype(int)
    atomic_write(OUT / "wiki_date_counts.csv", counts.to_csv(index=False).encode())

    by_last_year = summary_frame["last_date"].str.slice(0, 4).value_counts().sort_index()
    rule_counts = flag_frame["rule"].value_counts().to_dict()
    run_lengths = missing_frame.drop_duplicates(["ticker", "run_start"])["run_sessions"]
    no_prior = dividend_frame[dividend_frame["pct_of_prior_close"].isna()]
    worst_days = counts[counts["is_xnas_session"]].nlargest(10, "n_tickers_missing_in_span")
    facts = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": origin,
        "rows": int(len(frame)),
        "tickers": int(frame["ticker"].nunique()),
        "first_date": frame["date"].min(),
        "last_date": frame["date"].max(),
        "tickers_reaching_wiki_end": int((summary_frame["last_date"] >= WIKI_LAST_DATE).sum()),
        "tickers_by_last_date_year": {k: int(v) for k, v in by_last_year.items()},
        "min_last_date": summary_frame["last_date"].min(),
        "dates": int(counts["n_tickers"].gt(0).sum()),
        "xnas_sessions_in_range": int(len(sessions)),
        "xnas_sessions_without_rows": len(missing_sessions),
        "xnas_sessions_without_rows_list": missing_sessions,
        "dates_not_xnas_sessions": sorted(counts.loc[~counts["is_xnas_session"], "date"].tolist()),
        "flag_counts": {k: int(v) for k, v in rule_counts.items()},
        "tickers_with_flags": {rule: int(flag_frame.loc[flag_frame["rule"] == rule, "ticker"].nunique())
                               for rule in rule_counts},
        "null_fields": {
            "rows_null_open": int(frame["open"].isna().sum()),
            "rows_null_high": int(frame["high"].isna().sum()),
            "rows_null_low": int(frame["low"].isna().sum()),
            "rows_null_close": int(frame["close"].isna().sum()),
            "rows_null_volume": int(frame["volume"].isna().sum()),
            "rows_any_null_ohlc": int(frame[["open", "high", "low", "close"]].isna().any(axis=1).sum()),
            "tickers_any_null_ohlc": int(frame.loc[frame[["open", "high", "low", "close"]].isna().any(axis=1),
                                                   "ticker"].nunique()),
            "years": {k: int(v) for k, v in frame.loc[frame[["open", "high", "low"]].isna().any(axis=1), "date"]
                      .str.slice(0, 4).value_counts().sort_index().items()},
        },
        "missing_sessions_in_span": {
            "rows": int(len(missing_frame)),
            "tickers": int(missing_frame["ticker"].nunique()),
            "runs": int(len(run_lengths)),
            "runs_by_length": {label: int(n) for label, n in pd.cut(
                run_lengths, [0, 1, 2, 5, GAP_SESSIONS, np.inf],
                labels=["1", "2", "3-5", f"6-{GAP_SESSIONS}", f">{GAP_SESSIONS}"]).value_counts().sort_index().items()},
            "worst_sessions": {row.date: int(row.n_tickers_missing_in_span) for row in worst_days.itertuples()},
            "file": "wiki_missing_sessions.csv.gz (one row per missing in-span XNAS session)",
        },
        "split_events": len(splits),
        "dividend_events": len(dividends),
        "dividends_without_prior_close": [
            {"ticker": r.ticker, "date": r.date, "ex_dividend": r.ex_dividend, "prev_source": r.prev_source,
             "reason": {"none_first_row": "first row of the ticker in WIKI; no earlier row under this ticker",
                        "no_prior_row": f"no WIKI row in the {PRIOR_WINDOW_DAYS} days before {START}",
                        "prev_close_missing": "the previous row has no close"}.get(r.prev_source, "")}
            for r in no_prior.itertuples(index=False)],
        "prior_close_sources": {k: int(v) for k, v in pd.concat(
            [dividend_frame["prev_source"], pd.Series([s["prev_source"] for s in splits], dtype=object)]
        ).value_counts().items()},
        "rows_by_year": {k: int(v) for k, v in frame["date"].str.slice(0, 4).value_counts().sort_index().items()},
        "dividend_events_by_month": {k: int(v) for k, v in pd.Series(
            [row["date"][:7] for row in dividends], dtype=str).value_counts().sort_index().items()},
        "per_ticker_files": len(written),
        "per_ticker_bytes": int(sum((BY_TICKER / n).stat().st_size for n in written)),
        "per_ticker_cleanup": cleanup,
        "reading": {
            "pandas": 'pd.read_csv(path, dtype={"ticker": str, "date": str}, keep_default_na=False, na_values=[""])',
            "why": "default pandas reads the ticker TRUE (by_ticker/TRUE.csv.gz) as the boolean True, and would read "
                   "NA or NULL as missing; or take the ticker from the file name",
            "tickers_misread_by_default_pandas": misread_by_default(sorted(summary_frame["ticker"])),
        },
    }
    return facts


# --------------------------------------------------------------------------- survivorship and probes

def _traded_dates(frame: pd.DataFrame, tickers: set[str]) -> dict[str, np.ndarray]:
    rows = frame.loc[frame["ticker"].isin(tickers) & (frame["volume"].fillna(0) > 0), ["ticker", "date"]]
    return {t: np.sort(g.to_numpy(dtype="datetime64[D]")) for t, g in rows.groupby("ticker")["date"]}


def form25_coverage(form25: pd.DataFrame, master: pd.DataFrame | None, frame: pd.DataFrame,
                    first_dates: dict[str, str]) -> pd.DataFrame:
    """One row per Form 25 Nasdaq common delisting and whether WIKI traded one of its tickers near it.

    A delisting is covered when WIKI has a row with volume > 0 for one of the security's tickers
    (security_master ``tickers_observed`` matched by accession, plus the SEC tickers on the form)
    from COVER_BEFORE_DAYS before to COVER_AFTER_DAYS after the effective date. ``wiki_rows_before``
    says whether WIKI has any row for those tickers on or before the effective date at all (rows
    only after it belong to a later user of the ticker). ``trades_on`` marks a covering ticker that
    still trades more than COVER_BEFORE_DAYS after the effective date (a holding-company
    reorganisation or a successor on the same ticker), so WIKI does not hold a last trading day.
    """
    common = form25[(form25["classification"] == "common_delisting") & (form25["class_kind"] == "common")]
    tickers_by_accession: dict[str, set[str]] = {}
    foreign_by_accession: dict[str, str] = {}
    if master is not None:
        linked = master[master["delist_form25_accession"] != ""]
        for accession, group in linked.groupby("delist_form25_accession"):
            tickers_by_accession[accession] = {wiki_symbol(t) for v in group["tickers_observed"] for t in v.split()}
            foreign_by_accession[accession] = "/".join(sorted(set(group["foreign_filer"])))
    wanted = set().union(*tickers_by_accession.values()) if tickers_by_accession else set()
    wanted |= {wiki_symbol(t) for v in common["subject_tickers_sec"] for t in v.split()}
    traded = _traded_dates(frame, wanted)
    rows = []
    for record in common.itertuples(index=False):
        tickers = tickers_by_accession.get(record.accession, set()) | {
            wiki_symbol(t) for t in record.subject_tickers_sec.split()}
        effective = np.datetime64(record.effective_date, "D")
        low = effective - np.timedelta64(COVER_BEFORE_DAYS, "D")
        high = effective + np.timedelta64(COVER_AFTER_DAYS, "D")
        in_wiki = sorted(t for t in tickers if t in first_dates)
        covering, last_near, trades_on = [], "", False
        for ticker in in_wiki:
            days = traded.get(ticker)
            if days is None:
                continue
            near = days[(days >= low) & (days <= high)]
            if len(near):
                covering.append(ticker)
                last_near = max(last_near, str(near[-1]))
                trades_on |= bool((days > effective + np.timedelta64(COVER_BEFORE_DAYS, "D")).any())
        rows.append({
            "accession": record.accession, "effective_date": record.effective_date,
            "subject_name": record.subject_name, "tickers": " ".join(sorted(tickers)),
            "foreign_filer": foreign_by_accession.get(record.accession, ""),
            "tickers_in_wiki": " ".join(in_wiki),
            "wiki_rows_before": any(first_dates[t] <= record.effective_date for t in in_wiki),
            "covering_tickers": " ".join(covering), "wiki_last_traded_near": last_near,
            "covered": bool(covering), "trades_on": trades_on,
        })
    return pd.DataFrame(rows, columns=["accession", "effective_date", "subject_name", "tickers", "foreign_filer",
                                       "tickers_in_wiki", "wiki_rows_before", "covering_tickers",
                                       "wiki_last_traded_near", "covered", "trades_on"])


def _coverage_counts(table: pd.DataFrame) -> dict:
    domestic = table["foreign_filer"] == "N"
    return {
        "form25_common_delistings": int(len(table)),
        "domestic_filer": int(domestic.sum()),
        "with_tickers": int(table["tickers"].ne("").sum()),
        "ticker_in_wiki": int(table["tickers_in_wiki"].ne("").sum()),
        "wiki_rows_on_or_before_effective": int(table["wiki_rows_before"].sum()),
        "covered": int(table["covered"].sum()),
        "covered_domestic": int((table["covered"] & domestic).sum()),
        "covered_ticker_trades_on": int((table["covered"] & table["trades_on"]).sum()),
        "covered_last_trading_days": int((table["covered"] & ~table["trades_on"]).sum()),
    }


def survivorship(frame: pd.DataFrame, summary: pd.DataFrame, probes: dict, prior: pd.DataFrame | None = None,
                 form25_path: Path = FORM25_PATH, master_path: Path = MASTER_PATH) -> dict:
    """How far WIKI's ticker set reaches back: last dates, filler tails, Form 25 coverage, probes.

    ``prior`` (the whole table on the session before START) shows whether WIKI has tickers on that
    day that the export lacks, i.e. whether the export or WIKI itself drops the early delistings.
    """
    last = summary["last_date"]
    traded_last = summary["last_traded_date"].replace("", np.nan)
    early_traded = summary[traded_last.lt(SURVIVOR_CUTOFF).fillna(False)]
    out = {
        "cutoff": SURVIVOR_CUTOFF,
        "min_last_date": last.min(),
        "tickers_with_min_last_date": sorted(summary.loc[last == last.min(), "ticker"]),
        "tickers_last_date_before_cutoff": int(last.lt(SURVIVOR_CUTOFF).sum()),
        "tickers_by_last_date_year": {k: int(v) for k, v in last.str.slice(0, 4).value_counts().sort_index().items()},
        "tickers_last_traded_before_cutoff": [
            {"ticker": r.ticker, "last_traded_date": r.last_traded_date, "last_date": r.last_date,
             "rows_after_last_traded": int(r.rows_after_last_traded)} for r in early_traded.itertuples(index=False)],
        "tickers_last_traded_2011_06_to_2014_03": int(len(early_traded)),
    }
    if prior is not None and len(prior):
        on_session = prior[prior["date"] == prior["date"].max()]
        absent_from_export = sorted(set(on_session["ticker"]) - set(summary["ticker"]))
        out["prior_session_check"] = {"session": on_session["date"].max(), "tickers": int(len(on_session)),
                                      "tickers_not_in_export": len(absent_from_export),
                                      "examples": absent_from_export[:20]}
    if form25_path.exists():
        form25 = pd.read_csv(form25_path, dtype=str, keep_default_na=False)
        master = pd.read_csv(master_path, dtype=str, keep_default_na=False) if master_path.exists() else None
        first_dates = dict(zip(summary["ticker"], summary["first_date"]))
        table = form25_coverage(form25, master, frame, first_dates)
        atomic_write(OUT / "wiki_form25_coverage.csv", table.to_csv(index=False).encode())
        periods = {}
        for label, (low, high) in PERIODS.items():
            part = table[(table["effective_date"] >= low) & (table["effective_date"] <= high)]
            periods[label] = _coverage_counts(part)
            periods[label]["by_year"] = {year: _coverage_counts(g) for year, g in
                                         part.groupby(part["effective_date"].str.slice(0, 4))}
        out["form25"] = {
            "inputs": {str(form25_path): sha256_file(form25_path),
                       **({str(master_path): sha256_file(master_path)} if master is not None else {})},
            "definition": f"classification=common_delisting, class_kind=common, by effective_date; covered = WIKI "
                          f"row with volume > 0 for one of the security's tickers within {COVER_BEFORE_DAYS} days "
                          f"before to {COVER_AFTER_DAYS} days after the effective date",
            "periods": periods,
            "covered_2012_01_to_2014_03": table[table["effective_date"].between(*PERIODS["2012-01..2014-03"])
                                                & table["covered"]][["effective_date", "subject_name",
                                                                     "covering_tickers", "wiki_last_traded_near",
                                                                     "trades_on"]]
            .to_dict(orient="records"),
            "file": "wiki_form25_coverage.csv",
        }
    else:
        out["form25"] = {"note": f"{form25_path} not present; Form 25 coverage not computed"}
    out["probes"] = probes
    first = out.get("form25", {}).get("periods", {}).get("2012-01..2014-03")
    after = out.get("form25", {}).get("periods", {}).get("2014-04..2018-03")
    absent = [t for t, p in probes.get("delisted", {}).items() if p.get("rows_any_date") == 0]
    if out["tickers_last_date_before_cutoff"] == 0:
        note = (f"WIKI has no ticker whose rows end before {SURVIVOR_CUTOFF}: the earliest last date is "
                f"{out['min_last_date']} ({', '.join(out['tickers_with_min_last_date'])}), and only "
                f"{out['tickers_last_traded_2011_06_to_2014_03']} tickers stop trading earlier and then carry "
                f"zero-volume filler rows. Names that stopped trading from 2011-06 to 2014-03 are absent. ")
    else:
        note = (f"{out['tickers_last_date_before_cutoff']} WIKI tickers have rows ending before {SURVIVOR_CUTOFF} "
                f"(earliest last date {out['min_last_date']}); compare the Form 25 coverage below. ")
    if first:
        last_days = ", ".join(r["covering_tickers"] for r in out["form25"]["covered_2012_01_to_2014_03"]
                              if not r["trades_on"])
        note += (f"Of {first['form25_common_delistings']} Form 25 Nasdaq common delistings effective 2012-01 to "
                 f"2014-03 ({first['domestic_filer']} domestic filers), WIKI trades a ticker of {first['covered']} "
                 f"near the effective date; in {first['covered_ticker_trades_on']} of these the ticker trades on "
                 f"(a reorganisation on the same ticker), so WIKI holds the last trading days of only "
                 f"{first['covered_last_trading_days']} ({last_days or 'none'}). ")
    if after:
        note += (f"For 2014-04 to 2018-03 WIKI trades {after['covered']} of {after['form25_common_delistings']} near "
                 f"the effective date ({after['covered_last_trading_days']} with their last trading days). ")
    check = out.get("prior_session_check")
    if check:
        note += (f"The whole table on {check['session']} has {check['tickers']} tickers, "
                 f"{check['tickers_not_in_export']} of them missing from the export. ")
    if absent:
        note += (f"Whole-history probes of {', '.join(absent)} return no rows at any date, so the gap is in WIKI "
                 f"itself, not in the filtered export. ")
    note += ("Step 6 must send 2012-2014Q1 delisted candidates to another source or to unfillable.csv, and the "
             "data report must count them as survivor bias for 2012-2014Q1.")
    out["note"] = note
    return out


def _compare_with_export(rows: pd.DataFrame, export: pd.DataFrame) -> dict:
    rows = rows[rows["date"] >= START]
    joined = rows.merge(export, on=["ticker", "date"], how="left", suffixes=("", "_export"), indicator=True)
    matched = joined[joined["_merge"] == "both"]
    diffs = [(matched[c] - matched[f"{c}_export"]).abs().max() for c in NUMERIC] if len(matched) else []
    return {"rows_on_or_after_start": int(len(rows)), "rows_in_export": int(len(matched)),
            "rows_missing_from_export": int((joined["_merge"] == "left_only").sum()),
            "max_abs_diff_vs_export": float(np.nanmax(diffs)) if len(diffs) else None}


def run_probes(frame: pd.DataFrame) -> dict:
    """The key probe and the delisted-name probes, each compared with the export rows."""
    out: dict = {"delisted": {}}
    try:
        rows = _coerce(parse_page(api_get(KEY_PROBE, RAW_WIKI / "probe" / KEY_PROBE_FILE, "AAPL"))[0])
        out["key_probe"] = {"params_no_key": KEY_PROBE, "cache": str(RAW_WIKI / "probe" / KEY_PROBE_FILE),
                            "purpose": "checked the key before the export (round 1); reproduced from its cache",
                            "rows": int(len(rows)),
                            **_compare_with_export(rows, frame[frame["ticker"] == "AAPL"])}
    except Exception as exc:  # noqa: BLE001 - a probe never stops the build
        out["key_probe"] = {"error": redact(str(exc))}
    for ticker in DELISTED_PROBES:
        try:
            paths = fetch_paged({"ticker": ticker, "qopts.per_page": PER_PAGE}, RAW_WIKI / "probe" / f"{ticker}_all",
                                max_pages=10, symbol=ticker)
            rows = read_pages(paths, start=None).sort_values("date")
            traded = rows[rows["volume"].fillna(0) > 0]
            out["delisted"][ticker] = {
                "pages": len(paths), "rows_any_date": int(len(rows)),
                "first_date": rows["date"].min() if len(rows) else None,
                "last_date": rows["date"].max() if len(rows) else None,
                "last_traded_date": traded["date"].max() if len(traded) else None,
                **_compare_with_export(rows, frame[frame["ticker"] == ticker]),
            }
        except Exception as exc:  # noqa: BLE001
            out["delisted"][ticker] = {"error": redact(str(exc))}
    return out


# --------------------------------------------------------------------------- stored-file comparison

def basis_ratios(wiki: pd.DataFrame, stored: pd.DataFrame) -> pd.DataFrame:
    """Daily price-level ratios between WIKI and a stored (split- and dividend-adjusted) file.

    ``ratio_split_only`` = (close / S_after) / stored_close, where S_after is the product of WIKI split
    ratios after the date: removes the split basis only, so it drifts with dividends.
    ``ratio_full`` = close * F_after / S_after / stored_close, where F_after is the product of
    (1 - D_u / C_{u-1}) over WIKI ex-dividend days after the date: should be flat if the stored file
    uses the same dividend method; its level is the stored file's basis after 2018-03-27.
    ``ratio_adj`` = adj_close / stored_close (WIKI's own adjusted series): should also be flat.
    ``wiki_internal`` = close * F_after / S_after / adj_close: WIKI's raw and adjusted columns agreeing.
    These are level ratios on the same date, not returns.
    """
    wiki = wiki.sort_values("date").reset_index(drop=True)
    split = wiki["split_ratio"].fillna(1.0).replace(0.0, 1.0)
    dividend = wiki["ex-dividend"].fillna(0.0)
    step_factor = (1.0 - dividend / wiki["close"].shift(1)).fillna(1.0)
    # product over events strictly after each date
    s_after = split[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    f_after = step_factor[::-1].cumprod()[::-1].shift(-1).fillna(1.0)
    wiki = wiki.assign(s_after=s_after, f_after=f_after)
    joined = wiki.merge(stored[["date", "close"]].rename(columns={"close": "stored_close"}), on="date", how="inner")
    joined = joined[joined["stored_close"] > 0]
    return pd.DataFrame({
        "date": joined["date"],
        "ticker": joined["ticker"],
        "close_raw": joined["close"],
        "stored_close": joined["stored_close"],
        "s_after": joined["s_after"],
        "f_after": joined["f_after"],
        "ratio_split_only": joined["close"] / joined["s_after"] / joined["stored_close"],
        "ratio_full": joined["close"] * joined["f_after"] / joined["s_after"] / joined["stored_close"],
        "ratio_adj": joined["adj_close"] / joined["stored_close"],
        "wiki_internal": joined["close"] * joined["f_after"] / joined["s_after"] / joined["adj_close"],
    }).reset_index(drop=True)


def ratio_steps(daily: pd.DataFrame, column: str = "ratio_full", window: int = STEP_WINDOW,
                threshold: float = STEP_THRESHOLD) -> list[tuple[str, float]]:
    """Dates where a level ratio shifts and stays shifted: (first date at the new level, relative step).

    The step at t compares the median of the ``window`` ratios from t on with the median of the
    ``window`` ratios before t, so a one-day spike does not count. Neighbouring candidates (within
    ``window`` days) are one step, placed where a two-level fit (each side at its median) has the
    smallest absolute error, which is the first date at the new level.
    """
    values = daily[column].reset_index(drop=True)
    before = values.shift(1).rolling(window).median()
    after = values[::-1].rolling(window).median()[::-1]
    step = (after / before - 1.0)
    candidates = np.flatnonzero((step.abs() > threshold).to_numpy())
    array = values.to_numpy()

    def split_cost(low, split, high):
        left, right = array[low:split], array[split:high]
        return np.abs(left - np.median(left)).sum() + np.abs(right - np.median(right)).sum()

    steps, group = [], []
    for position in list(candidates) + [None]:
        if group and (position is None or position - group[-1] > window):
            low, high = max(0, group[0] - window), min(len(array), group[-1] + window + 1)
            best = min(group, key=lambda p: (split_cost(low, p, high), p))
            steps.append((daily["date"].iloc[best], float(step.iloc[best])))
            group = []
        if position is not None:
            group.append(position)
    return steps


def summarise_ratios(daily: pd.DataFrame) -> dict:
    full = daily["ratio_full"]
    median = full.median()
    deviation = (full / median - 1.0).abs()
    worst = daily.assign(dev=deviation).nlargest(3, "dev")
    first_year = daily["date"] < (pd.Timestamp(daily["date"].min()) + pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    last_year = daily["date"] > (pd.Timestamp(daily["date"].max()) - pd.DateOffset(years=1)).strftime("%Y-%m-%d")
    return {
        "overlap_days": int(len(daily)),
        "first_date": daily["date"].min(),
        "last_date": daily["date"].max(),
        "split_only_median_first_year": daily.loc[first_year, "ratio_split_only"].median(),
        "split_only_median_last_year": daily.loc[last_year, "ratio_split_only"].median(),
        "full_median": median,
        "full_p01": full.quantile(0.01),
        "full_p99": full.quantile(0.99),
        "full_spread_p99_over_p01": full.quantile(0.99) / full.quantile(0.01) - 1.0,
        "full_max_abs_dev": deviation.max(),
        "days_dev_gt_0p5pct": int((deviation > 0.005).sum()),
        "days_dev_gt_1pct": int((deviation > 0.01).sum()),
        "worst_dates": ";".join(f"{d}:{v:+.4f}" for d, v in zip(worst["date"], worst["ratio_full"] / median - 1.0)),
        "full_median_by_year": " ".join(f"{year}:{value:.5f}" for year, value in
                                        daily.groupby(daily["date"].str.slice(0, 4))["ratio_full"].median().items()),
        "full_steps": ";".join(f"{d}:{v:+.4f}" for d, v in ratio_steps(daily)),
        "adj_median": daily["ratio_adj"].median(),
        "adj_spread_p99_over_p01": daily["ratio_adj"].quantile(0.99) / daily["ratio_adj"].quantile(0.01) - 1.0,
        "wiki_internal_max_abs_dev": (daily["wiki_internal"] / daily["wiki_internal"].median() - 1.0).abs().max(),
    }


def stored_consistency(tickers: list[str] = CHECK_TICKERS) -> pd.DataFrame:
    rows, daily_parts = [], []
    for ticker in tickers:
        wiki_path = BY_TICKER / f"{safe_ticker(ticker)}.csv.gz"
        stored_path = STORED_PRICES / f"{ticker.lower()}.csv"
        if not wiki_path.exists() or not stored_path.exists():
            rows.append({"ticker": ticker, "note": f"missing {'wiki' if not wiki_path.exists() else 'stored'} file"})
            continue
        wiki = read_ticker_file(wiki_path)
        stored = pd.read_csv(stored_path, dtype={"date": str})
        daily = basis_ratios(wiki, stored)
        daily_parts.append(daily)
        rows.append({"ticker": ticker, **summarise_ratios(daily),
                     "wiki_splits": int((wiki["split_ratio"].fillna(1.0) != 1.0).sum()),
                     "wiki_dividends": int((wiki["ex-dividend"].fillna(0.0) != 0.0).sum()), "note": ""})
    result = pd.DataFrame(rows)
    atomic_write(OUT / "stored_consistency_10.csv", result.to_csv(index=False).encode())
    if daily_parts:
        atomic_write(OUT / "stored_consistency_10_daily.csv.gz", _csv_gz_bytes(pd.concat(daily_parts)))
    return result


# --------------------------------------------------------------------------- main

def known_issues(frame: pd.DataFrame, facts: dict) -> list[str]:
    """Data findings for steps 6 and 9, with the counts read from this build."""
    issues = []
    true = frame[(frame["ticker"] == "TRUE") & (frame["date"] < "2014-05-16")]
    if len(true):
        issues.append(
            f"Entity mix with no gap or ratio flag: TRUE has {len(true)} rows from {true['date'].min()} to "
            f"{true['date'].max()} (raw close {true['close'].min():.2f} to {true['close'].max():.2f}) before "
            f"TrueCar's 2014-05-16 IPO. Step 9 rule R9 must cut WIKI rows to ticker_intervals spans and not "
            f"rely on gap flags.")
    flags = read_ticker_file(OUT / "wiki_flags.csv.gz")
    reused = flags[(flags["rule"] == "gap_gt_10_sessions") & (flags["date"] == "2018-03-01")]
    if len(reused):
        issues.append(f"{len(reused)} tickers resume on 2018-03-01 after gaps of more than {GAP_SESSIONS} sessions "
                      f"(reused tickers, e.g. {', '.join(reused['ticker'].head(5))}); R9 has to cut them.")
    months = facts.get("dividend_events_by_month", {})
    late = {m: months.get(m, 0) for m in ("2017-11", "2017-12", "2018-01", "2018-02", "2018-03")}
    earlier = {m: months.get(m, 0) for m in ("2016-11", "2016-12", "2017-01", "2017-02", "2017-03")}
    issues.append(f"WIKI's ex-dividend column is mostly empty from about 2017-11 to 2018-03: rows by month {late}, "
                  f"against {earlier} a year earlier. It also misses some special dividends before that (COST "
                  f"2017-05-08). The adj_* columns inherit this; use the raw columns and another dividend source.")
    worst = {d: n for d, n in facts.get("missing_sessions_in_span", {}).get("worst_sessions", {}).items() if n >= 25}
    if worst:
        issues.append(f"XNAS sessions missing for many tickers at once (tickers whose span covers the day): {worst}. "
                      f"{', '.join(facts.get('xnas_sessions_without_rows_list', []))} has no WIKI rows at all. "
                      f"Rule R6 in step 9 needs another source for these days; wiki_missing_sessions.csv.gz lists "
                      f"every missing ticker-session.")
    nulls = facts.get("null_fields", {})
    if nulls.get("rows_any_null_ohlc"):
        issues.append(f"{nulls['rows_any_null_ohlc']} rows over {nulls['tickers_any_null_ohlc']} tickers miss open, "
                      f"high, low or close (null_ohlc / null_close flags; years {nulls.get('years')}). The OHLC "
                      f"consistency check skips missing fields, so these rows are flagged separately.")
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--max-wait", type=float, default=1800, help="seconds to wait for an export file")
    parser.add_argument("--skip-filtered-export", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="only rerun the stored-file comparison")
    args = parser.parse_args(argv)
    if not args.check_only:
        frame, origin = acquire(args.max_wait, args.skip_filtered_export)
        print(f"{len(frame):,} rows, {frame['ticker'].nunique():,} tickers", flush=True)
        starts_on_start = sorted(frame.loc[frame["date"] == START, "ticker"].unique())
        try:
            prior, prior_facts = fetch_prior_rows(starts_on_start)
        except Exception as exc:  # noqa: BLE001 - the build still runs; the prior closes stay missing
            prior, prior_facts = None, {"error": redact(str(exc))}
        facts = build_outputs(frame, origin, prior)
        facts["prior_rows"] = prior_facts
        raw_path = Path(origin["raw_path"])
        if raw_path.is_file():
            facts["raw_bytes"] = raw_path.stat().st_size
            facts["raw_sha256"] = sha256_file(raw_path)
        else:
            facts["raw_bytes"] = int(sum(p.stat().st_size for p in raw_path.glob("*.json.gz")))
        probes = run_probes(frame)
        facts["survivorship"] = survivorship(frame, read_ticker_file(OUT / "wiki_ticker_summary.csv"), probes, prior)
        facts["known_issues"] = known_issues(frame, facts)
        del frame
    else:
        facts = json.loads((OUT / "wiki_build.json").read_text())
    consistency = stored_consistency()
    facts["stored_consistency"] = json.loads(consistency.to_json(orient="records"))
    facts["output_bytes"] = {p.name: p.stat().st_size for p in sorted(OUT.glob("*")) if p.is_file()}
    atomic_write(OUT / "wiki_build.json", (json.dumps(facts, indent=2, default=str) + "\n").encode())
    print(json.dumps({k: v for k, v in facts.items() if k not in ("stored_consistency", "dividend_events_by_month",
                                                                   "dates_not_xnas_sessions")},
                     indent=2, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(consistency.drop(columns=["worst_dates"], errors="ignore").to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
