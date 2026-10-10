"""Backfill missing historical price heads from Nasdaq's public API.

This is a research-only repair. Existing dates are never replaced; only dates
before the current local history are appended, and every run is recorded.
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH
from src.io.nasdaq_update import _atomic_merge, _exclude_existing_price_dates, fetch_history


def backfill(
    tickers: list[str],
    start: date,
    end: date,
    workers: int = 4,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    provenance: str | Path = Path(PROJECT_PATH) / "output/data_provenance/nasdaq_historical_head_repair.json",
) -> dict:
    directory = Path(price_dir)

    def one(ticker: str) -> dict:
        ticker = ticker.upper().strip()
        target = directory / f"{ticker.lower()}.csv"
        if not target.exists():
            return {"ticker": ticker, "status": "missing_local_file", "rows_added": 0}
        old = pd.read_csv(target, parse_dates=["date"])
        incoming = fetch_history(ticker, start, end, retries=3)
        missing = _exclude_existing_price_dates(target, incoming)
        rows = _atomic_merge(target, missing, ticker)
        updated = pd.read_csv(target, parse_dates=["date"])
        return {
            "ticker": ticker,
            "status": "updated" if rows else "no_new_rows",
            "rows_added": int(rows),
            "rows_before": int(len(old)),
            "rows_after": int(len(updated)),
            "first_date_before": old["date"].min().strftime("%Y-%m-%d") if len(old) else None,
            "first_date_after": updated["date"].min().strftime("%Y-%m-%d") if len(updated) else None,
            "last_date_after": updated["date"].max().strftime("%Y-%m-%d") if len(updated) else None,
            "source": "Nasdaq historical API",
            "source_url": f"https://api.nasdaq.com/api/quote/{ticker}/historical",
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
        }

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, ticker): ticker for ticker in sorted(set(tickers)) if ticker.strip()}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:  # report per-ticker network failures
                results.append({"ticker": ticker, "status": "failed", "error": repr(exc)})
    results.sort(key=lambda item: item["ticker"])
    run = {
        "observed_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "research_only": True,
        "formal_financial_files_modified": False,
        "terminal_returns_modified": False,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "results": results,
    }
    path = Path(provenance)
    path.parent.mkdir(parents=True, exist_ok=True)
    previous: list[dict] = []
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    path.write_text(json.dumps({"runs": [*previous, run]}, indent=2), encoding="utf-8")
    return run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="Comma-separated existing local price files")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    result = backfill(
        args.tickers.split(","), date.fromisoformat(args.start), date.fromisoformat(args.end), args.workers
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
