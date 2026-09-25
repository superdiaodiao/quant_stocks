"""Stage one stock's prices for record-sourced-event on a fresh machine.

record-sourced-event checks an event against the stock's staged price file,
which a GitHub runner does not have: every run starts from the data release.
This stages that one file as a staging would, from the latest valued MARK
bundle for a stock a mark has priced, otherwise from the formal baseline, and
then through the Nasdaq update with its provider reconciliation, up to the
latest completed session.  It writes only into the r3 staging work directory
and is not part of the r3 code closure.

    PYTHONPATH=. python scripts/v50r3_event_prices.py ABCD
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys

import pandas as pd

from scripts import research_v43_isolated_prospective_v28_observation as v43
from scripts import research_v50r3_corrected_v47 as r3
from scripts.research_v6_market_refresh import seed_cache
from src.io import nasdaq_update
from src.research import prospective_schedule as schedule

TAIL_ROWS = 8


def stage_event_prices(ticker: str, now: datetime | None = None) -> dict:
    ticker = str(ticker).strip().upper()
    if not ticker:
        raise ValueError("a ticker is required")
    now = schedule.as_utc(now or datetime.now(timezone.utc))
    end = schedule.latest_completed_session(now)
    if end is None:
        raise RuntimeError("no completed Nasdaq session yet")
    market = r3.resolve(r3.WORK_DIR) / "market"
    prices = market / "prices"
    prices.mkdir(parents=True, exist_ok=True)
    index_path = market / "nasdaq_index.csv"
    with r3._runtime():
        prior = r3._latest_valued_bundle(
            v43.read_ledger(r3.resolve(r3.LEDGER_PATH)),
            r3.resolve(r3.BUNDLES_DIR),
            r3.valued_bundle_copy_dir(r3.LEDGER_PATH),
        )
    seeded = r3._seed_from_valued_bundle(prior, [ticker], prices, index_path)
    baseline = seed_cache([ticker], price_dir=prices, index_path=index_path)
    update = nasdaq_update.update_all(
        end=end.date(),
        workers=1,
        tickers=[ticker],
        price_dir=prices,
        index_path=index_path,
    )
    path = prices / f"{ticker.lower()}.csv"
    tail = []
    if path.is_file():
        frame = pd.read_csv(path)
        tail = frame[["date", "close", "volume"]].tail(TAIL_ROWS).to_dict("records")
    return {
        "ticker": ticker,
        "through": f"{end:%Y-%m-%d}",
        "price_file": r3._portable_path(path),
        "price_file_present": path.is_file(),
        "seeded_from_valued_bundle": ticker in seeded,
        "copied_from_formal_baseline": int(baseline["copied_price_files"]),
        "update_failures": update.get("failures", []),
        "provider_adjustments_recorded": update.get("provider_adjustments_recorded", []),
        "latest_rows": tail,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ticker")
    args = parser.parse_args(argv)
    result = stage_event_prices(args.ticker)
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["price_file_present"] else 1


if __name__ == "__main__":
    sys.exit(main())
