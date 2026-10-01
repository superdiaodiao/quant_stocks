"""Step 5 of docs/reversal_2012_2026_data_plan.md: the free Nasdaq Data Link WIKI/PRICES table.

Data only. This script downloads the WIKI end-of-day table (it stopped on
2018-03-27) for dates on or after 2011-06-01, keeps every ticker (NYSE ones
too; the universe step filters later) and every column, raw and adjusted:
``open, high, low, close, volume, ex-dividend, split_ratio`` as traded and
``adj_open, adj_high, adj_low, adj_close, adj_volume``.

It never computes strategy or portfolio returns, signals or rankings by
return. The checks it writes are per-stock data checks (row counts, date
ranges, calendar gaps, OHLC consistency, whether WIKI's own adjustment factor
moves only on its split and dividend days) and a level-basis comparison with
the repo's stored price files, reported as price ratios.

Order of attempts (each request goes through ``cached_get``, so a rerun
resumes from the cache and every request is logged with the key redacted):

1. bulk export with the date filter: ``qopts.export=true&date.gte=2011-06-01``,
   polled until the file is ready, then the zip it links to;
2. if the filtered export never becomes ready, the unfiltered export (the
   whole table, filtered locally);
3. if neither works, cursor paging: ``date.gte=2011-06-01``, 10,000 rows a
   page, following ``meta.next_cursor_id``.

Outputs (local only, under research_cache/reversal_2012_2026):
  raw/wiki/WIKI_PRICES_{fetched_utc}.zip (+ .json sidecar)  or raw/wiki/pages/NNNNN.json.gz
  raw/wiki/export/status_{utc}.json      every export poll response
  wiki/by_ticker/{TICKER}.csv.gz         one file per ticker, rows dated >= 2011-06-01
  wiki/wiki_ticker_summary.csv           per-ticker coverage and data-error counts
  wiki/wiki_flags.csv.gz                 one row per flagged ticker-day
  wiki/wiki_split_events.csv, wiki/wiki_dividends.csv
  wiki/wiki_date_counts.csv              tickers per date, against the XNAS calendar
  wiki/stored_consistency_10.csv (+ _daily.csv.gz)   ratios against cleaned_stocks_data/price
  wiki/wiki_build.json                   counts, date range, file sizes and hashes
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
import zipfile

import numpy as np
import pandas as pd

from scripts.reversal_data_common import (
    CACHE,
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
CHECK_TICKERS = ["AAPL", "MSFT", "INTC", "CSCO", "NFLX", "SBUX", "MNST", "CTSH", "COST", "KLAC"]
COLUMNS = ["ticker", "date", "open", "high", "low", "close", "volume", "ex-dividend", "split_ratio",
           "adj_open", "adj_high", "adj_low", "adj_close", "adj_volume"]
NUMERIC = COLUMNS[2:]
PER_PAGE = 10_000
# Free-key limits are documented as 300 calls / 10 s and 2,000 / 10 min; stay far under them.
LIMITER = SlidingWindowLimiter({10: 10, 600: 300})
# Tolerances for the data checks.
ADJ_FACTOR_TOL = 1e-4      # relative error allowed in WIKI's own adjustment-factor step
CENT_ROUNDING = 0.005      # half a cent: many WIKI adjusted series are rounded to cents
BIG_RATIO_HI, BIG_RATIO_LO = 2.0, 0.5   # raw close ratio with no split that day (rule R1)
GAP_SESSIONS = 10          # rule R9
FLAT_RUN = 3               # rule R4


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_url(params: dict, api_key: str, fmt: str = "json") -> str:
    """The request URL; the key goes last so redact() leaves the rest readable."""
    query = urlencode({**params, "api_key": api_key})
    return f"{BASE_URL}.{fmt}?{query}"


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
                         dtype={"ticker": str, "date": str}, chunksize=chunksize, low_memory=False,
                         keep_default_na=False, na_values=[""])
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


# --------------------------------------------------------------------------- acquisition

def _api_key() -> str:
    return read_env_key(ENV_PATH, KEY_NAME)


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


def fetch_pages(api_key: str, max_pages: int = 5000) -> list[Path]:
    """Cursor paging fallback; cached pages are re-read so a stopped run resumes where it was."""
    paths, cursor = [], None
    for number in range(max_pages):
        params = {"date.gte": START, "qopts.per_page": PER_PAGE}
        if cursor:
            params["qopts.cursor_id"] = cursor
        path = RAW_WIKI / "pages" / f"{number:05d}.json.gz"
        payload = cached_get(build_url(params, api_key), path, source=SOURCE, limiter=LIMITER)
        frame, cursor = parse_page(payload)
        paths.append(path)
        if number % 25 == 0:
            print(f"page {number}: {len(frame)} rows", flush=True)
        if not cursor:
            return paths
    raise RuntimeError(f"paging did not finish within {max_pages} pages")


def read_pages(paths: list[Path]) -> pd.DataFrame:
    frames = [parse_page(gzip.decompress(p.read_bytes()))[0] for p in paths]
    frame = _coerce(pd.concat(frames, ignore_index=True))
    return frame[frame["date"] >= START].reset_index(drop=True)


def acquire(max_wait_s: float, skip_filtered: bool = False) -> tuple[pd.DataFrame, dict]:
    """The WIKI rows dated >= START and a description of where they came from."""
    found = existing_zip()
    if found is not None:
        print(f"using cached {found.name}", flush=True)
        return read_export_zip(found), {"method": "export_zip", "raw_path": str(found)}
    pages_dir = RAW_WIKI / "pages"
    if pages_dir.exists() and any(pages_dir.glob("*.json.gz")):
        print("resuming cursor paging from cached pages", flush=True)
        paths = fetch_pages(_api_key())
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
    paths = fetch_pages(api_key)
    return read_pages(paths), {"method": "cursor_pages", "raw_path": str(pages_dir), "pages": len(paths)}


# --------------------------------------------------------------------------- data checks

def xnas_sessions(start: str = START, end: str = WIKI_LAST_DATE) -> pd.DatetimeIndex:
    import exchange_calendars as xcals

    calendar = xcals.get_calendar("XNAS", start=start, end=end)
    return pd.DatetimeIndex(calendar.sessions_in_range(start, end)).tz_localize(None)


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


def check_ticker(group: pd.DataFrame, sessions: pd.DatetimeIndex) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Per-ticker summary row, flag rows, split rows and dividend rows (rows sorted by date)."""
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

    split_rows = [{"ticker": ticker, "date": group["date"].iloc[p], "split_ratio": split.iloc[p],
                   "close_prev_raw": previous.iloc[p], "close_raw": close.iloc[p]}
                  for p in np.flatnonzero((split != 1.0).to_numpy())]
    dividend_rows = [{"ticker": ticker, "date": group["date"].iloc[p], "ex_dividend": dividend.iloc[p],
                      "close_prev_raw": previous.iloc[p],
                      "pct_of_prior_close": dividend.iloc[p] / previous.iloc[p] if previous.iloc[p] else np.nan}
                     for p in np.flatnonzero((dividend != 0.0).to_numpy())]
    dollar_volume = (close * group["volume"]).median()
    summary = {
        "ticker": ticker,
        "first_date": group["date"].iloc[0],
        "last_date": group["date"].iloc[-1],
        "rows": len(group),
        "xnas_sessions_in_span": len(in_span),
        "missing_sessions": len(missing),
        "max_gap_sessions": max_gap,
        "off_calendar_rows": int(off_calendar.sum()),
        "duplicate_dates": int(duplicated.sum()),
        "null_close": int(null_close.sum()),
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
    return summary, flags, split_rows, dividend_rows


def _csv_gz_bytes(frame: pd.DataFrame) -> bytes:
    return gzip.compress(frame.to_csv(index=False).encode("utf-8"), mtime=0)


def build_outputs(frame: pd.DataFrame, origin: dict) -> dict:
    """Write per-ticker files and the data-check tables; the build facts."""
    frame = frame.sort_values(["ticker", "date"], kind="stable").reset_index(drop=True)
    sessions = xnas_sessions()
    summaries, flags, splits, dividends = [], [], [], []
    BY_TICKER.mkdir(parents=True, exist_ok=True)
    written = set()
    for ticker, group in frame.groupby("ticker", sort=True):
        group = group.reset_index(drop=True)
        name = safe_ticker(ticker)
        if name in written:
            raise ValueError(f"two tickers map to the file name {name}")
        written.add(name)
        atomic_write(BY_TICKER / f"{name}.csv.gz", _csv_gz_bytes(group))
        summary, ticker_flags, split_rows, dividend_rows = check_ticker(group, sessions)
        summaries.append(summary)
        flags.extend(ticker_flags)
        splits.extend(split_rows)
        dividends.extend(dividend_rows)
    stale = set(p.name for p in BY_TICKER.glob("*.csv.gz")) - {f"{n}.csv.gz" for n in written}
    for name in stale:  # a ticker no longer in the source would otherwise linger from an older build
        (BY_TICKER / name).unlink()

    summary_frame = pd.DataFrame(summaries)
    atomic_write(OUT / "wiki_ticker_summary.csv", summary_frame.to_csv(index=False).encode())
    flag_frame = pd.DataFrame(flags, columns=["ticker", "date", "rule", "detail"])
    atomic_write(OUT / "wiki_flags.csv.gz", _csv_gz_bytes(flag_frame))
    atomic_write(OUT / "wiki_split_events.csv", pd.DataFrame(
        splits, columns=["ticker", "date", "split_ratio", "close_prev_raw", "close_raw"]).to_csv(index=False).encode())
    atomic_write(OUT / "wiki_dividends.csv", pd.DataFrame(
        dividends, columns=["ticker", "date", "ex_dividend", "close_prev_raw", "pct_of_prior_close"]
    ).to_csv(index=False).encode())

    counts = frame.groupby("date")["ticker"].nunique().rename("n_tickers").reset_index()
    session_text = set(sessions.strftime("%Y-%m-%d"))
    counts["is_xnas_session"] = counts["date"].isin(session_text)
    missing_sessions = sorted(session_text - set(counts["date"]))
    counts = pd.concat([counts, pd.DataFrame({"date": missing_sessions, "n_tickers": 0,
                                              "is_xnas_session": True})]).sort_values("date")
    atomic_write(OUT / "wiki_date_counts.csv", counts.to_csv(index=False).encode())

    by_last_year = summary_frame["last_date"].str.slice(0, 4).value_counts().sort_index()
    rule_counts = flag_frame["rule"].value_counts().to_dict()
    facts = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "origin": origin,
        "rows": int(len(frame)),
        "tickers": int(frame["ticker"].nunique()),
        "first_date": frame["date"].min(),
        "last_date": frame["date"].max(),
        "tickers_reaching_wiki_end": int((summary_frame["last_date"] >= WIKI_LAST_DATE).sum()),
        "tickers_by_last_date_year": {k: int(v) for k, v in by_last_year.items()},
        "dates": int(counts["n_tickers"].gt(0).sum()),
        "xnas_sessions_in_range": int(len(sessions)),
        "xnas_sessions_without_rows": len(missing_sessions),
        "dates_not_xnas_sessions": sorted(counts.loc[~counts["is_xnas_session"], "date"].tolist()),
        "flag_counts": {k: int(v) for k, v in rule_counts.items()},
        "tickers_with_flags": {rule: int(flag_frame.loc[flag_frame["rule"] == rule, "ticker"].nunique())
                               for rule in rule_counts},
        "split_events": len(splits),
        "dividend_events": len(dividends),
        "rows_by_year": {k: int(v) for k, v in frame["date"].str.slice(0, 4).value_counts().sort_index().items()},
        "dividend_events_by_month": {k: int(v) for k, v in pd.Series(
            [row["date"][:7] for row in dividends], dtype=str).value_counts().sort_index().items()},
        "per_ticker_files": len(written),
        "per_ticker_bytes": int(sum((BY_TICKER / f"{n}.csv.gz").stat().st_size for n in written)),
    }
    return facts


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
        wiki = pd.read_csv(wiki_path, dtype={"ticker": str, "date": str})
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

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--max-wait", type=float, default=1800, help="seconds to wait for an export file")
    parser.add_argument("--skip-filtered-export", action="store_true")
    parser.add_argument("--check-only", action="store_true", help="only rerun the stored-file comparison")
    args = parser.parse_args(argv)
    if not args.check_only:
        frame, origin = acquire(args.max_wait, args.skip_filtered_export)
        print(f"{len(frame):,} rows, {frame['ticker'].nunique():,} tickers", flush=True)
        facts = build_outputs(frame, origin)
        raw_path = Path(origin["raw_path"])
        if raw_path.is_file():
            facts["raw_bytes"] = raw_path.stat().st_size
            facts["raw_sha256"] = sha256_file(raw_path)
        else:
            facts["raw_bytes"] = int(sum(p.stat().st_size for p in raw_path.glob("*.json.gz")))
        del frame
    else:
        facts = json.loads((OUT / "wiki_build.json").read_text())
    consistency = stored_consistency()
    facts["stored_consistency"] = json.loads(consistency.to_json(orient="records"))
    facts["output_bytes"] = {p.name: p.stat().st_size for p in sorted(OUT.glob("*")) if p.is_file()}
    atomic_write(OUT / "wiki_build.json", (json.dumps(facts, indent=2, default=str) + "\n").encode())
    print(json.dumps({k: v for k, v in facts.items() if k != "stored_consistency"}, indent=2, default=str))
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(consistency.drop(columns=["worst_dates"], errors="ignore").to_string(index=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
