"""Compare local provider price files with Yahoo adjusted-close history.

This is an offline-auditable research check. It does not modify prices; it
reports overlap size and stable scale ratios for ticker-transition providers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


def audit_one(ticker: str, price_dir: str | Path = CLEANED_PRICE_DATA_DIR) -> dict:
    ticker = ticker.upper().strip()
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{ticker}?period1=1609459200&period2=1784332800&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
    try:
        payload = urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30).read()
        result = ((json.loads(payload.decode("utf-8")).get("chart") or {}).get("result") or [None])[0]
        if not result:
            return {"ticker": ticker, "status": "no_data", "source_url": url}
        adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose", [])
        yahoo = pd.DataFrame({"date": pd.to_datetime(result.get("timestamp", []), unit="s").normalize(), "close": adjusted}).dropna()
        local_path = Path(price_dir) / f"{ticker.lower()}.csv"
        local = pd.read_csv(local_path, usecols=["date", "close"], parse_dates=["date"])
        overlap = local.merge(yahoo, on="date", suffixes=("_local", "_yahoo")).dropna()
        ratio = overlap["close_local"] / overlap["close_yahoo"]
        median = float(ratio.median()) if len(ratio) else None
        return {"ticker": ticker, "status": "audited", "overlap_sessions": int(len(overlap)), "median_ratio": median, "within_1pct": float((ratio / median - 1).abs().le(0.01).mean()) if median else None, "source_url": url, "payload_sha256": hashlib.sha256(payload).hexdigest()}
    except Exception as exc:
        return {"ticker": ticker, "status": "failed", "source_url": url, "error": repr(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--output", default=str(Path(PROJECT_PATH) / "output/data_provenance/provider_overlap_audit.json"))
    args = parser.parse_args()
    tickers = sorted({item.upper().strip() for item in args.tickers.split(",") if item.strip()})
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = [future.result() for future in as_completed({pool.submit(audit_one, t): t for t in tickers})]
    results.sort(key=lambda item: item["ticker"])
    report = {"research_only": True, "results": results}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
