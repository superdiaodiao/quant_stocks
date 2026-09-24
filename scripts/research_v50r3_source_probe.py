#!/usr/bin/env python3
"""Read-only probe: are the SIGNAL data sources ready for a session right now?

The SIGNAL window opens 30 minutes after the official close, while Nasdaq's
after-hours session is still trading, and closes when pre-market trading opens
on the next session.  Staging needs, for the signal session, a Nasdaq
Composite close (historical row, or the official-close fallback that requires
the info endpoint to report ``Closed``), a QQQ historical row, and the session
row for at least 98% of the universe.  This
probe records what the public sources return at this moment without staging
anything, so the window can be checked before a run starts.

It writes nothing except an optional JSON line to ``--log``.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import random
import sys
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import NASDAQ_300M_STOCK_LIST_FILE
from src.io import nasdaq_update
from src.io.security_universe import investable_common_equities
from src.research import prospective_schedule as schedule


REPO_ROOT = Path(__file__).resolve().parents[1]
EXACT_FRACTION_GATE = 0.98
# Used only when the formal universe file is absent (e.g. a fresh checkout).
FALLBACK_SAMPLE = (
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "COST",
    "NFLX", "AMD", "PEP", "ADBE", "CSCO", "TMUS", "INTC", "QCOM", "TXN",
    "AMGN", "INTU", "ISRG", "BKNG", "HON", "AMAT", "SBUX", "MDLZ", "GILD",
    "ADP", "VRTX", "REGN", "LRCX", "PANW", "MU", "KLAC", "SNPS", "CDNS",
    "MAR", "ORLY", "CTAS", "ABNB", "CRWD", "FTNT", "DDOG", "ZS", "TEAM",
    "WDAY", "MRVL", "ODFL", "PAYX", "FAST", "ROST", "IDXX", "EXC", "XEL",
    "CEG", "SMCI", "STX", "JAZZ", "CATY", "VIAV",
)


def latest_completed_close(now: datetime) -> pd.Timestamp:
    today = pd.Timestamp(now.date())
    sessions = [
        session
        for session in schedule.sessions_between(today - pd.Timedelta(days=10), today)
        if schedule.session_close_utc(session) <= now
    ]
    return sessions[-1]


def _latest_row(symbol: str, asset_class: str, session: pd.Timestamp) -> str | None:
    try:
        frame = nasdaq_update.fetch_history(
            symbol,
            (session - pd.Timedelta(days=10)).date(),
            session.date(),
            asset_class=asset_class,
            retries=1,
        )
    except Exception as exc:  # recorded, never raised: this is a probe
        return f"ERROR: {exc}"[:160]
    if frame.empty:
        return None
    return f"{pd.to_datetime(frame['date']).max():%Y-%m-%d}"


def _quote_fields(symbol: str, asset_class: str) -> dict:
    fields = {}
    for label, api in (
        ("chart", nasdaq_update.CHART_API),
        ("info", nasdaq_update.INFO_API),
    ):
        url = api.format(symbol=symbol) + f"?assetclass={asset_class}"
        try:
            with urlopen(
                Request(url, headers=nasdaq_update.HEADERS), timeout=30
            ) as response:
                data = json.load(response).get("data") or {}
        except Exception as exc:
            fields[label] = {"error": str(exc)[:160]}
            continue
        primary = data.get("primaryData") or {}
        fields[label] = {
            "marketStatus": data.get("marketStatus"),
            "timeAsOf": data.get("timeAsOf"),
            "lastSalePrice": data.get("lastSalePrice"),
            "lastTradeTimestamp": primary.get("lastTradeTimestamp"),
            "primaryLastSalePrice": primary.get("lastSalePrice"),
        }
    return fields


def sample_tickers(size: int, universe_path: Path) -> list[str]:
    if not universe_path.is_file():
        return list(FALLBACK_SAMPLE[:size])
    current = investable_common_equities(
        pd.read_csv(universe_path, keep_default_na=False)
    )
    symbols = sorted(set(current["Symbol"].astype(str).str.upper()) - {""})
    random.Random(20260924).shuffle(symbols)
    return symbols[:size]


def probe(session: pd.Timestamp, *, sample: list[str], now: datetime) -> dict:
    key = f"{session:%Y-%m-%d}"
    record = {
        "probed_at_utc": now.isoformat(timespec="seconds"),
        "session": key,
        "minutes_after_official_close": round(
            (now - schedule.session_close_utc(session)).total_seconds() / 60, 1
        ),
        "comp_historical_latest": _latest_row("COMP", "index", session),
        "qqq_historical_latest": _latest_row("QQQ", "etf", session),
        "comp_quote": _quote_fields("COMP", "index"),
    }
    try:
        snapshot = nasdaq_update.fetch_closed_index_snapshot("COMP", session.date())
        record["comp_official_close_fallback"] = {
            "ok": True,
            "close": snapshot["close"],
            "market_status": snapshot["market_status"],
        }
    except Exception as exc:
        record["comp_official_close_fallback"] = {"ok": False, "error": str(exc)[:200]}
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(
            lambda ticker: _latest_row(ticker, "stocks", session), sample
        ))
    latest = dict(zip(sample, rows, strict=True))
    exact = sum(value == key for value in latest.values())
    fraction = exact / len(sample) if sample else 0.0
    record["stock_sample"] = {
        "size": len(sample),
        "exact_session_rows": exact,
        "exact_fraction": round(fraction, 4),
        "not_exact": {ticker: value for ticker, value in latest.items() if value != key},
    }
    comp_ready = (
        record["comp_historical_latest"] == key
        or record["comp_official_close_fallback"]["ok"]
    )
    record["ready"] = {
        "comp_close": comp_ready,
        "qqq_row": record["qqq_historical_latest"] == key,
        "stock_rows_sample_at_least_98pct": fraction >= EXACT_FRACTION_GATE,
    }
    record["signal_sources_ready"] = all(record["ready"].values())
    return record


def main(argv: list[str] | None = None) -> int:
    os.chdir(REPO_ROOT)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", help="default: latest session past its close")
    parser.add_argument("--sample", type=int, default=120)
    parser.add_argument("--universe", type=Path, default=Path(NASDAQ_300M_STOCK_LIST_FILE))
    parser.add_argument("--log", type=Path)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    session = (
        pd.Timestamp(args.session).normalize()
        if args.session else latest_completed_close(now)
    )
    record = probe(session, sample=sample_tickers(args.sample, args.universe), now=now)
    if args.log:
        args.log.parent.mkdir(parents=True, exist_ok=True)
        with args.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0 if record["signal_sources_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
