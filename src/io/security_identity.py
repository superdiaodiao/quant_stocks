"""Normalize provider histories when a ticker has been reused by another issuer."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


SECURITY_IDENTITY_FILE = Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/security_identity.csv"


def load_security_identity(path: str | Path = SECURITY_IDENTITY_FILE) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "provider_ticker", "historical_ticker", "last_historical_date",
        "current_ticker_first_date", "source_url", "verified_at",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"security identity file is missing columns: {sorted(missing)}")
    frame = frame.copy()
    for column in ("provider_ticker", "historical_ticker"):
        frame[column] = frame[column].astype(str).str.upper().str.strip()
    for column in ("last_historical_date", "current_ticker_first_date"):
        frame[column] = pd.to_datetime(frame[column], errors="raise").dt.normalize()
    if (frame["last_historical_date"] >= frame["current_ticker_first_date"]).any():
        raise ValueError("historical and current security date ranges overlap")
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("security identity rows require source_url")
    return frame


def normalize_point_in_time_tickers(
    frame: pd.DataFrame,
    path: str | Path = SECURITY_IDENTITY_FILE,
) -> pd.DataFrame:
    """Map old issuer observations that a provider labels with today's ticker."""
    result = frame.copy()
    period_end = pd.to_datetime(result["period_end"], errors="coerce")
    for row in load_security_identity(path).itertuples(index=False):
        mask = (
            result["ticker"].astype(str).str.upper().eq(row.provider_ticker)
            & period_end.le(row.last_historical_date)
        )
        result.loc[mask, "ticker"] = row.historical_ticker
    return result


def split_reused_ticker_price_histories(
    path: str | Path = SECURITY_IDENTITY_FILE,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> list[dict]:
    """Split a provider's continuous current-ticker file at sourced rename dates."""
    price_dir = Path(price_dir)
    results = []
    for row in load_security_identity(path).itertuples(index=False):
        current_path = price_dir / f"{row.provider_ticker.lower()}.csv"
        historical_path = price_dir / f"{row.historical_ticker.lower()}.csv"
        current = pd.read_csv(current_path, parse_dates=["date"])
        historical = current.loc[current["date"] <= row.last_historical_date].copy()
        current = current.loc[current["date"] >= row.current_ticker_first_date].copy()
        historical["ticker"] = row.historical_ticker
        current["ticker"] = row.provider_ticker
        for target, data in ((historical_path, historical), (current_path, current)):
            tmp = target.with_suffix(".csv.tmp")
            data.to_csv(tmp, index=False)
            os.replace(tmp, target)
        results.append({
            "provider_ticker": row.provider_ticker,
            "historical_ticker": row.historical_ticker,
            "historical_rows": len(historical),
            "current_rows": len(current),
            "source_url": row.source_url,
        })
    return results


if __name__ == "__main__":
    print(split_reused_ticker_price_histories())
