"""Append a cross-symbol tail ending at an SEC-confirmed trading suspension.

This is for provider archives that relabel a security after it moves off its
original exchange (for example an OTC ``Q`` suffix).  The source must agree
with the existing historical file for at least 20 sessions, continue within
seven calendar days, and end immediately before the SEC suspension date.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
from io import StringIO
import json
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from scripts.historicaldata_price_import import (
    PRICE_COLUMNS, _atomic_write, _atomic_write_json, _frame_sha256,
    _read_local, _sha256, _validate_overlap,
)
from scripts.sec_terminal_filing_evidence import _filing_text
from src.conf import CLEANED_PRICE_DATA_DIR


def _read_stooq_text(path: Path, ticker: str) -> pd.DataFrame:
    names = ["source_ticker", "frequency", "date", "time", "open", "high",
             "low", "close", "volume", "open_interest"]
    frame = pd.read_csv(path, names=names, header=0)
    frame["date"] = pd.to_datetime(frame["date"].astype(str), format="%Y%m%d")
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame.insert(1, "ticker", ticker)
    return frame[PRICE_COLUMNS].dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")


def _load_or_fetch_sec(cache_path: Path, source_url: str, refresh: bool) -> bytes:
    if cache_path.exists() and not refresh:
        envelope = json.loads(gzip.decompress(cache_path.read_bytes()))
        payload = bytes.fromhex(envelope["payload_hex"])
        if hashlib.sha256(payload).hexdigest() != envelope["payload_sha256"]:
            raise ValueError("SEC cache payload hash mismatch")
        return payload
    request = Request(source_url, headers={"User-Agent": "quant-stocks-research contact@example.com"})
    payload = urlopen(request, timeout=30).read()
    envelope = {
        "source_url": source_url,
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "payload_hex": payload.hex(),
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(gzip.compress(json.dumps(envelope, sort_keys=True).encode(), mtime=0))
    return payload


def import_tail(*, historical_ticker: str, successor_ticker: str,
                source_path: str | Path, source_url: str,
                sec_cache_path: str | Path, sec_source_url: str,
                expected_filing_phrase: str, suspension_date: str,
                output: str | Path, price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
                refresh_sec: bool = False, apply: bool = False) -> dict:
    historical, successor = historical_ticker.upper(), successor_ticker.upper()
    source_path, sec_cache_path = Path(source_path), Path(sec_cache_path)
    local_path = Path(price_dir) / f"{historical.lower()}.csv"
    local = _read_local(local_path)
    source = _read_stooq_text(source_path, successor)
    overlap = _validate_overlap(local, source)
    if not overlap.get("passed"):
        raise ValueError("successor source does not validate against historical prices")
    payload = _load_or_fetch_sec(sec_cache_path, sec_source_url, refresh_sec)
    filing_text = _filing_text(payload)
    if expected_filing_phrase.lower() not in filing_text.lower():
        raise ValueError("SEC filing is missing the required suspension phrase")
    suspension = pd.Timestamp(suspension_date)
    source_last = source["date"].max()
    if not 0 < (suspension - source_last).days <= 4:
        raise ValueError("source does not end immediately before the SEC suspension")
    tail = source.loc[source["date"] > local["date"].max()].copy()
    if tail.empty or not 0 < (tail["date"].min() - local["date"].max()).days <= 7:
        raise ValueError("successor tail is not contiguous with the local history")
    tail["ticker"] = historical
    report = {
        "schema_version": 1, "research_only": True,
        "status": "UPDATED" if apply else "DRY_RUN_ELIGIBLE", "applied": apply,
        "historical_ticker": historical, "successor_ticker": successor,
        "source_path": str(source_path), "source_url": source_url,
        "source_sha256": _sha256(source_path), "source_frame_sha256": _frame_sha256(source),
        "overlap_validation": overlap, "suspension_date": suspension.strftime("%Y-%m-%d"),
        "sec_source_url": sec_source_url, "sec_cache_path": str(sec_cache_path),
        "sec_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_filing_phrase": expected_filing_phrase,
        "rows_added": len(tail), "first_added_date": tail["date"].min().strftime("%Y-%m-%d"),
        "last_added_date": tail["date"].max().strftime("%Y-%m-%d"),
        "appended_rows_sha256": _frame_sha256(tail), "price_path": str(local_path),
        "local_sha256_before": _sha256(local_path), "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
    }
    if apply:
        merged = pd.concat([local, tail], ignore_index=True).sort_values("date").drop_duplicates("date", keep="first")
        _atomic_write(local_path, merged[PRICE_COLUMNS])
    report["local_sha256_after"] = _sha256(local_path)
    _atomic_write_json(Path(output), report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical", required=True); parser.add_argument("--successor", required=True)
    parser.add_argument("--source", required=True); parser.add_argument("--source-url", required=True)
    parser.add_argument("--sec-cache", required=True); parser.add_argument("--sec-source-url", required=True)
    parser.add_argument("--expected-filing-phrase", required=True); parser.add_argument("--suspension-date", required=True)
    parser.add_argument("--output", required=True); parser.add_argument("--price-dir", default=str(CLEANED_PRICE_DATA_DIR))
    parser.add_argument("--refresh-sec", action="store_true"); parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_tail(
        historical_ticker=args.historical, successor_ticker=args.successor,
        source_path=args.source, source_url=args.source_url,
        sec_cache_path=args.sec_cache, sec_source_url=args.sec_source_url,
        expected_filing_phrase=args.expected_filing_phrase, suspension_date=args.suspension_date,
        output=args.output, price_dir=args.price_dir, refresh_sec=args.refresh_sec, apply=args.apply,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
