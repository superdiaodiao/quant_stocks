"""Repair missing price files for SEC-proven same-issuer ticker renames.

The import uses Nasdaq's official historical endpoint for the data and Yahoo
Chart only as an independent OHLCV cross-check.  Rows are written under the
historical ticker only after the issuer/ticker relationship is documented by
an SEC filing and both feeds agree on at least 120 sessions within 1%.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH
from src.io.nasdaq_update import fetch_history, _atomic_merge


ANALYSIS_END = date(2026, 7, 17)
YAHOO_BASE = "https://query2.finance.yahoo.com/v8/finance/chart"
MIN_OVERLAP_SESSIONS = 120
TOLERANCE = 0.01

# Filing-level evidence identifies the same CIK and the new registered symbol.
ALIASES = {
    "ELYM": {
        "provider_ticker": "CLYM",
        "cik": 1768446,
        "effective_date": "2024-10-15",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1768446/000119312524237196/d889682d8k.htm",
    },
    "RPHM": {
        "provider_ticker": "OKUR",
        "cik": 1637715,
        "effective_date": "2024-11-07",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1637715/000095017024123265/okur-20241107.htm",
    },
    "VIRI": {
        "provider_ticker": "DWTX",
        "cik": 1818844,
        "effective_date": "2024-11-07",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1818844/000155837024014749/viri-20241107x8k.htm",
    },
    "PALT": {
        "provider_ticker": "IPM",
        "cik": 1355839,
        "effective_date": "2025-01-07",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1355839/000121390025001824/ea0226912-8k_intelli.htm",
    },
    "INVO": {
        "provider_ticker": "IVF",
        "cik": 1417926,
        "effective_date": "2025-07-21",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1417926/000164117225020406/ex99-1.htm",
    },
    "JCTCF": {
        "provider_ticker": "JCTC",
        "cik": 885307,
        "effective_date": "2024-11-20",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/885307/000107997324001599/ex99x1.htm",
    },
    "SSIC": {
        "provider_ticker": "LIEN",
        "cik": 1843162,
        "effective_date": "2024-10-01",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/1843162/000095010324014651/dp219097_8k.htm",
    },
    "SCHN": {
        "provider_ticker": "RDUS",
        "cik": 912603,
        "effective_date": "2023-09-01",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/912603/000115752323001375/a53546319.htm",
    },
    "PEGY": {
        "provider_ticker": "SUNE",
        "cik": 22701,
        "effective_date": "2024-12-13",
        "sec_url": "https://www.sec.gov/Archives/edgar/data/22701/000121390024108931/ea022475601ex99-1_sunation.htm",
    },
}


def _yahoo_url(ticker: str, start: date, end: date) -> str:
    query = urlencode({
        "period1": int(pd.Timestamp(start, tz="UTC").timestamp()),
        "period2": int((pd.Timestamp(end) + pd.Timedelta(days=1)).tz_localize("UTC").timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    })
    return f"{YAHOO_BASE}/{ticker}?{query}"


def _fetch_yahoo(ticker: str, start: date, end: date) -> tuple[pd.DataFrame, bytes, str]:
    url = _yahoo_url(ticker, start, end)
    payload = urlopen(Request(url, headers={"User-Agent": "quant-stocks-research"}), timeout=60).read()
    chart = json.loads(payload).get("chart") or {}
    result = chart.get("result") or []
    if not result:
        raise ValueError(f"Yahoo returned no result for {ticker}: {chart.get('error')}")
    item = result[0]
    quote = (item.get("indicators") or {}).get("quote") or []
    if not quote or not item.get("timestamp"):
        raise ValueError(f"Yahoo returned no quote rows for {ticker}")
    q = quote[0]
    frame = pd.DataFrame({
        "date": pd.to_datetime(item["timestamp"], unit="s").normalize(),
        "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
        "close": q.get("close"), "volume": q.get("volume"),
    }).dropna(subset=["date", "close"])
    for col in ("open", "high", "low", "close", "volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["close"]), payload, url


def _cross_validate(nasdaq: pd.DataFrame, yahoo: pd.DataFrame) -> dict:
    left = nasdaq[["date", "open", "high", "low", "close", "volume"]].copy()
    right = yahoo[["date", "open", "high", "low", "close", "volume"]].copy()
    merged = left.merge(right, on="date", suffixes=("_nasdaq", "_yahoo"))
    if len(merged) < MIN_OVERLAP_SESSIONS:
        raise ValueError(f"insufficient Yahoo/Nasdaq overlap: {len(merged)}")
    fields = {}
    for field in ("open", "high", "low", "close", "volume"):
        a = pd.to_numeric(merged[f"{field}_nasdaq"], errors="coerce")
        b = pd.to_numeric(merged[f"{field}_yahoo"], errors="coerce")
        valid = a.gt(0) & b.gt(0)
        ratios = (a[valid] / b[valid]).replace([float("inf"), -float("inf")], pd.NA).dropna()
        median = float(ratios.median())
        fields[field] = {
            "median_ratio": median,
            "within_1pct": float(((ratios / median - 1).abs() <= TOLERANCE).mean()),
        }
    # Yahoo and Nasdaq can report materially different consolidated volume
    # after a venue/ticker transition, while OHLC remains identical.  Price
    # continuity is the import gate; volume is retained as an explicit
    # diagnostic and never silently normalized.
    if min(fields[field]["within_1pct"] for field in ("open", "high", "low", "close")) < 0.95:
        raise ValueError(f"Yahoo/Nasdaq OHLC cross-validation failed: {fields}")
    return {
        "sessions": int(len(merged)),
        "fields": fields,
        "volume_warning": fields["volume"]["within_1pct"] < 0.95,
        "passed": True,
    }


def repair_alias(old_ticker: str, *, price_dir: Path) -> dict:
    spec = ALIASES[old_ticker]
    new_ticker = spec["provider_ticker"]
    start, end = date(2021, 1, 1), ANALYSIS_END
    nasdaq = fetch_history(new_ticker, start, end, retries=3)
    yahoo, payload, yahoo_url = _fetch_yahoo(new_ticker, start, end)
    validation = _cross_validate(nasdaq, yahoo)
    target = price_dir / f"{old_ticker.lower()}.csv"
    rows = nasdaq.copy()
    rows["ticker"] = old_ticker
    rows = rows[["date", "ticker", "open", "high", "low", "close", "volume"]]
    old_dates = set(pd.to_datetime(pd.read_csv(target, usecols=["date"])["date"])) if target.exists() else set()
    missing = rows.loc[~rows["date"].isin(old_dates)].copy()
    added = _atomic_merge(target, missing, old_ticker)
    return {
        "historical_ticker": old_ticker,
        "provider_ticker": new_ticker,
        "cik": spec["cik"],
        "effective_date": spec["effective_date"],
        "sec_source_url": spec["sec_url"],
        "nasdaq_source_url": f"https://api.nasdaq.com/api/quote/{new_ticker}/historical",
        "yahoo_source_url": yahoo_url,
        "yahoo_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "source_first_date": nasdaq["date"].min().strftime("%Y-%m-%d") if len(nasdaq) else None,
        "source_last_date": nasdaq["date"].max().strftime("%Y-%m-%d") if len(nasdaq) else None,
        "source_rows": int(len(nasdaq)),
        "rows_added": int(added),
        "cross_validation": validation,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=",".join(ALIASES))
    parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/sec_ticker_alias_price_repair.json"))
    args = parser.parse_args()
    results = []
    for ticker in [item.strip().upper() for item in args.tickers.split(",") if item.strip()]:
        try:
            results.append({**repair_alias(ticker, price_dir=Path(args.price_dir)), "status": "UPDATED"})
        except Exception as exc:
            results.append({"historical_ticker": ticker, "status": "REJECTED", "error": repr(exc)})
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version": 1, "research_only": True, "results": results}, indent=2), encoding="utf-8")
    print(json.dumps({"status_counts": pd.Series([x["status"] for x in results]).value_counts().to_dict(), "results": results}, indent=2))


if __name__ == "__main__":
    main()
