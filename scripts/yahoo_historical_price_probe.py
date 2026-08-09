"""Probe Yahoo chart history for research-only gap evidence.

The probe never writes formal price files. It stores the raw payload hash and
basic coverage so a later importer can require an independent overlap check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import PROJECT_PATH


def probe_one(ticker: str, start: str, end: str) -> dict:
    ticker = ticker.upper().strip()
    params = {
        "period1": int(pd.Timestamp(start, tz="UTC").timestamp()),
        "period2": int((pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)).timestamp()),
        "interval": "1d", "events": "div,splits", "includeAdjustedClose": "true",
    }
    query = "&".join(f"{key}={value}" for key, value in params.items())
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?{query}"
    try:
        request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        payload = urlopen(request, timeout=30).read()
        envelope = json.loads(payload.decode("utf-8"))
        error = (envelope.get("chart") or {}).get("error")
        result = ((envelope.get("chart") or {}).get("result") or [None])[0]
        timestamps = (result or {}).get("timestamp") or []
        return {
            "ticker": ticker, "status": "available" if timestamps and not error else "no_data",
            "rows": len(timestamps),
            "first_date": pd.to_datetime(timestamps[0], unit="s").strftime("%Y-%m-%d") if timestamps else None,
            "last_date": pd.to_datetime(timestamps[-1], unit="s").strftime("%Y-%m-%d") if timestamps else None,
            "source_url": url, "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "error": error,
        }
    except Exception as exc:
        return {"ticker": ticker, "status": "failed", "source_url": url, "error": repr(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default="2026-07-17")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/yahoo_historical_price_probe.json"))
    args = parser.parse_args()
    tickers = sorted({item.upper().strip() for item in args.tickers.split(",") if item.strip()})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(probe_one, ticker, args.start, args.end): ticker for ticker in tickers}
        results = [future.result() for future in as_completed(futures)]
    results.sort(key=lambda item: item["ticker"])
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    run = {"research_only": True, "start": args.start, "end": args.end, "results": results}
    previous: list[dict] = []
    if target.exists():
        try:
            previous = json.loads(target.read_text(encoding="utf-8")).get("runs", [])
        except (OSError, json.JSONDecodeError):
            previous = []
    target.write_text(json.dumps({"runs": [*previous, run]}, indent=2), encoding="utf-8")
    print(json.dumps({"counts": pd.Series([item["status"] for item in results]).value_counts().to_dict(), "results": results}, indent=2))


if __name__ == "__main__":
    main()
