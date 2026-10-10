#!/usr/bin/env python3
"""Build an isolated 2013+ QQQ price/dividend history for v14 training."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path
from io import StringIO
from urllib.request import Request, urlopen

import pandas as pd

from scripts.research_v5_trend_core_satellite import _nasdaq_dividend_history
from src.io.nasdaq_update import fetch_history


DEFAULT_OUTPUT = Path("output/research_only/v14/qqq_nasdaq_history_2013.csv")
DEFAULT_NASDAQ_BASELINE = Path("output/research_only/qqq_nasdaq_history.csv")
ARCHIVED_QQQ_URL = (
    "https://raw.githubusercontent.com/lvrusu/QQQ_price_data/"
    "e6be97d8326b30f7c737204f4a5a586afdbb1600/"
    "QQQDailyfrom99to29J25.csv"
)


def fetch_price_chunks(
    start: date,
    end: date,
    *,
    chunk_years: int = 5,
    fetcher=fetch_history,
) -> pd.DataFrame:
    if chunk_years <= 0:
        raise ValueError("chunk_years must be positive")
    pieces = []
    chunk_start = pd.Timestamp(start)
    final = pd.Timestamp(end)
    while chunk_start <= final:
        chunk_end = min(
            final,
            chunk_start + pd.DateOffset(years=chunk_years) - pd.Timedelta(days=1),
        )
        pieces.append(fetcher(
            "QQQ", chunk_start.date(), chunk_end.date(),
            asset_class="etf", retries=3,
        ))
        chunk_start = chunk_end + pd.Timedelta(days=1)
    return pd.concat(pieces, ignore_index=True).drop_duplicates("date", keep="last")


def build_history(prices: pd.DataFrame, dividends: pd.DataFrame) -> pd.DataFrame:
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = set(required) - set(prices.columns)
    if missing:
        raise ValueError(f"QQQ price history is missing columns: {sorted(missing)}")
    frame = prices[required].copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    frame = frame.sort_values("date").drop_duplicates("date", keep="last")
    if frame.empty or pd.to_numeric(frame["close"], errors="coerce").le(0).any():
        raise ValueError("QQQ price history requires positive closes")
    cash = dividends[["date", "cash_dividend"]].copy()
    cash["date"] = pd.to_datetime(cash["date"], errors="raise").dt.normalize()
    cash = cash.drop_duplicates("date", keep="last")
    frame = frame.merge(cash, on="date", how="left", validate="one_to_one")
    frame["cash_dividend"] = frame["cash_dividend"].fillna(0.0)
    return frame


def fetch_archived_history(start: date, end: date) -> tuple[bytes, str, pd.DataFrame]:
    with urlopen(
        Request(ARCHIVED_QQQ_URL, headers={"User-Agent": "quant-stocks-v14/1.0"}),
        timeout=60,
    ) as response:
        payload = response.read()
    downloaded = pd.read_csv(StringIO(payload.decode("utf-8")))
    frame = downloaded.rename(columns={
        "ds": "date", "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })[["date", "open", "high", "low", "close", "volume"]]
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    frame = frame.loc[
        frame["date"].between(pd.Timestamp(start), pd.Timestamp(end))
    ].dropna(subset=["date", "close"])
    if frame.empty:
        raise ValueError("pinned archive returned an empty QQQ price frame")
    return payload, ARCHIVED_QQQ_URL, frame


def validate_overlap(
    yahoo: pd.DataFrame,
    nasdaq: pd.DataFrame,
    *,
    minimum_sessions: int = 500,
    relative_tolerance: float = 0.001,
) -> dict:
    overlap = yahoo[["date", "close"]].merge(
        nasdaq[["date", "close"]], on="date", suffixes=("_yahoo", "_nasdaq")
    ).dropna()
    if len(overlap) < minimum_sessions:
        raise ValueError(
            f"insufficient Yahoo/Nasdaq QQQ overlap: {len(overlap)} < {minimum_sessions}"
        )
    relative = (
        overlap["close_yahoo"] / overlap["close_nasdaq"] - 1.0
    ).abs()
    within = float(relative.le(relative_tolerance).mean())
    if within < 0.995:
        raise ValueError(
            f"Yahoo/Nasdaq QQQ overlap mismatch: {within:.6f} within tolerance"
        )
    return {
        "sessions": len(overlap),
        "relative_tolerance": relative_tolerance,
        "within_tolerance_fraction": within,
        "maximum_relative_close_difference": float(relative.max()),
    }


def run(
    output: Path = DEFAULT_OUTPUT,
    start: date = date(2013, 1, 1),
    end: date | None = None,
    nasdaq_baseline_path: Path = DEFAULT_NASDAQ_BASELINE,
) -> dict:
    end = end or date.today()
    nasdaq = pd.read_csv(nasdaq_baseline_path, parse_dates=["date"])
    archive_payload, archive_url, archive = fetch_archived_history(start, end)
    overlap = validate_overlap(archive, nasdaq)
    baseline_start = nasdaq["date"].min().normalize()
    prices = pd.concat([
        archive.loc[archive["date"].lt(baseline_start)],
        nasdaq.drop(columns=["cash_dividend"], errors="ignore"),
    ], ignore_index=True)
    dividend_payload, dividends = _nasdaq_dividend_history()
    frame = build_history(prices, dividends)
    requested_start_reached = bool(
        frame["date"].min() <= pd.Timestamp(start) + pd.Timedelta(days=7)
    )
    if not requested_start_reached:
        raise ValueError(
            "QQQ source did not reach the requested start: "
            f"{frame['date'].min():%Y-%m-%d} > {start.isoformat()}"
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    frame.to_csv(temporary, index=False)
    os.replace(temporary, output)
    payload = {
        "schema_version": 1,
        "research_only": True,
        "ticker": "QQQ",
        "requested_start": start.isoformat(),
        "requested_end": end.isoformat(),
        "minimum_date": frame["date"].min().strftime("%Y-%m-%d"),
        "maximum_date": frame["date"].max().strftime("%Y-%m-%d"),
        "rows": len(frame),
        "requested_start_reached": requested_start_reached,
        "cash_dividend_rows": int(frame["cash_dividend"].gt(0).sum()),
        "price_source": "Nasdaq public historical API",
        "pre_nasdaq_price_source": "Pinned GitHub OHLCV archive cross-validated against Nasdaq",
        "pre_nasdaq_source_url": archive_url,
        "pre_nasdaq_payload_sha256": hashlib.sha256(archive_payload).hexdigest(),
        "pre_nasdaq_overlap_validation": overlap,
        "nasdaq_baseline": {
            "path": str(nasdaq_baseline_path),
            "sha256": hashlib.sha256(nasdaq_baseline_path.read_bytes()).hexdigest(),
            "minimum_date": baseline_start.strftime("%Y-%m-%d"),
        },
        "dividend_source": "Nasdaq public dividend API",
        "dividend_payload_sha256": hashlib.sha256(dividend_payload).hexdigest(),
        "output": str(output),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "formal_market_files_modified": False,
        "release_status": "BLOCKED",
    }
    manifest = output.with_suffix(".provenance.json")
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    payload["provenance_output"] = str(manifest)
    return payload


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
