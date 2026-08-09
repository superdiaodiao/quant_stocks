"""Cache a canonical Nasdaq historical-price response for offline replay."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlencode

from scripts.historicaldata_price_import import PRICE_COLUMNS, _atomic_write_json, _frame_sha256
from src.io.nasdaq_update import API, fetch_history


def create_snapshot(ticker: str, start: str, end: str, output: str | Path) -> dict:
    ticker = ticker.upper().strip()
    start_date, end_date = date.fromisoformat(start), date.fromisoformat(end)
    frame = fetch_history(ticker, start_date, end_date)
    frame.insert(1, "ticker", ticker)
    frame = frame[PRICE_COLUMNS].sort_values("date").reset_index(drop=True)
    source_url = API.format(symbol=ticker) + "?" + urlencode({
        "assetclass": "stocks", "fromdate": start, "todate": end, "limit": 5000,
    })
    records = json.loads(frame.to_json(orient="records", date_format="iso"))
    report = {
        "schema_version": 1,
        "research_only": True,
        "provider": "Nasdaq public historical API",
        "ticker": ticker,
        "requested_start": start,
        "requested_end": end,
        "source_url": source_url,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": len(frame),
        "first_date": frame["date"].min().strftime("%Y-%m-%d"),
        "last_date": frame["date"].max().strftime("%Y-%m-%d"),
        "frame_sha256": _frame_sha256(frame),
        "records": records,
    }
    _atomic_write_json(Path(output), report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(create_snapshot(args.ticker, args.start, args.end, args.output), indent=2))


if __name__ == "__main__":
    main()
