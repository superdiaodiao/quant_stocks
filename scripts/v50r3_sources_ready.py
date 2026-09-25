"""Whether Nasdaq has published what a v50r3 SIGNAL or MARK needs for a session.

Nasdaq's historical API adds a session's stock and ETF rows hours after the
close: the 2026-09-24 rows were missing at 00:16 UTC and present at 01:00 UTC.
Its Composite close is official once after-hours trading ends at 20:00 New
York time (until the history adds it).  A SIGNAL or MARK started earlier
fails after its downloads, on a GitHub runner two hours in for a SIGNAL.  The
scheduler workflow asks this first and skips a run whose inputs are not
published yet; the window waker waits for it before it starts a SIGNAL.  It
reads public quotes only and is not part of the r3 code closure.

    PYTHONPATH=. python scripts/v50r3_sources_ready.py --as-of 2026-09-30

Exit 0: published; 1: not yet; 3: the check itself failed.

This module is research-only.  It cannot connect to a broker or create orders.
"""

from __future__ import annotations

import argparse
import json
import sys

import pandas as pd

from src.io import nasdaq_update

STOCK_SAMPLE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "COST", "NFLX", "TSLA",
)
MINIMUM_STOCK_FRACTION = 0.8


def _has_row(symbol: str, session: pd.Timestamp, asset_class: str) -> bool | str:
    try:
        frame = nasdaq_update.fetch_history(
            symbol, session.date(), session.date(), asset_class=asset_class, retries=1
        )
    except Exception as exc:  # a refused or failed request is "not yet"
        return f"{type(exc).__name__}: {exc}"
    dates = pd.to_datetime(frame["date"]).dt.normalize()
    return bool(dates.eq(session).any())


def sources_ready(session: str | pd.Timestamp) -> dict:
    """The stock sample, QQQ and the Composite close for ``session``."""
    session = pd.Timestamp(session).normalize()
    stocks = {symbol: _has_row(symbol, session, "stocks") for symbol in STOCK_SAMPLE}
    priced = sum(value is True for value in stocks.values())
    qqq = _has_row("QQQ", session, "etf")
    composite = _has_row("COMP", session, "index")
    composite_source = "history" if composite is True else None
    if composite is not True:
        try:
            nasdaq_update.fetch_closed_index_snapshot("COMP", session.date())
            composite, composite_source = True, "official_closed_snapshot"
        except Exception as exc:
            composite = f"{type(exc).__name__}: {exc}"
    checks = {
        "stock_sample": priced >= MINIMUM_STOCK_FRACTION * len(STOCK_SAMPLE),
        "qqq": qqq is True,
        "nasdaq_composite": composite is True,
    }
    return {
        "session": f"{session:%Y-%m-%d}",
        "ready": all(checks.values()),
        "checks": checks,
        "stock_sample_priced": f"{priced}/{len(STOCK_SAMPLE)}",
        "stock_sample": {
            symbol: value for symbol, value in stocks.items() if value is not True
        },
        "qqq": qqq,
        "nasdaq_composite": composite,
        "nasdaq_composite_source": composite_source,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--as-of", required=True, help="Nasdaq session, YYYY-MM-DD")
    args = parser.parse_args(argv)
    try:
        result = sources_ready(args.as_of)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
        return 3
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
