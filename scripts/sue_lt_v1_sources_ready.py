"""Whether Nasdaq has published a session's closes for a sue-lt-v1 SIGNAL or MARK.

Nasdaq's historical API adds a session's stock and ETF rows four to five
hours after the close (the 2026-09-24 rows were missing at 00:16 UTC and
present at 01:00 UTC). A SIGNAL started earlier spends hours downloading and
then fails its coverage check; a MARK would wait on its own ("retry
later"). The scheduler asks this first and skips a run whose session is not
published yet; the window waker waits for it before it starts a SIGNAL.
Outside the frozen code closure: the scheduler fetches it from master and
runs it in a checkout of the live branch, so it imports nothing but the
Nasdaq client.

    PYTHONPATH=. python scripts/sue_lt_v1_sources_ready.py --as-of 2026-10-30

Exit 0: published; 10: not yet; any other code: the check itself failed.
"""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
import sys

NOT_YET = 10
STOCK_SAMPLE = ("AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "AVGO", "COST", "NFLX", "TSLA")
MINIMUM_STOCK_FRACTION = 0.8
# Nasdaq drops rows from short ranges that end months back; long ones come whole.
REQUEST_DAYS = 830


def has_row(symbol: str, session, asset_class: str) -> bool | str:
    import pandas as pd
    from src.io import nasdaq_update

    try:
        frame = nasdaq_update.fetch_history(
            symbol, (session - timedelta(days=REQUEST_DAYS)).date(), session.date(),
            asset_class=asset_class, retries=1)
    except Exception as exc:  # a refused or failed request is "not yet"
        return f"{type(exc).__name__}: {exc}"
    return bool(pd.to_datetime(frame["date"]).dt.normalize().eq(session).any())


def sources_ready(session) -> dict:
    """The stock sample and QQQ for ``session``."""
    import pandas as pd

    session = pd.Timestamp(session).normalize()
    stocks = {symbol: has_row(symbol, session, "stocks") for symbol in STOCK_SAMPLE}
    priced = sum(value is True for value in stocks.values())
    qqq = has_row("QQQ", session, "etf")
    checks = {"stock_sample": priced >= MINIMUM_STOCK_FRACTION * len(STOCK_SAMPLE), "qqq": qqq is True}
    return {
        "session": f"{session:%Y-%m-%d}",
        "ready": all(checks.values()),
        "checks": checks,
        "stock_sample_priced": f"{priced}/{len(STOCK_SAMPLE)}",
        "stock_sample": {symbol: value for symbol, value in stocks.items() if value is not True},
        "qqq": qqq,
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
    return 0 if result["ready"] else NOT_YET


if __name__ == "__main__":
    sys.exit(main())
