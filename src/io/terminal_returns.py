"""Load independently verified terminal returns for ended stock histories."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.conf import PROJECT_PATH


TERMINAL_RETURNS_FILE = Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/terminal_returns.csv"
REQUIRED_COLUMNS = {
    "ticker",
    "last_price_date",
    "terminal_return",
    "source_url",
    "verified_at",
}


def load_observed_terminal_returns(path: str | Path = TERMINAL_RETURNS_FILE) -> pd.DataFrame:
    """Return verified terminal returns keyed by ticker and final price date.

    A row is accepted only when it carries source provenance.  The terminal
    return is the total value received after the final observed close divided
    by that close, minus one; it may therefore be positive for a cash merger.
    """
    path = Path(path)
    if not path.exists():
        return pd.DataFrame(columns=sorted(REQUIRED_COLUMNS))
    frame = pd.read_csv(path, dtype={"ticker": str, "source_url": str})
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"terminal return file is missing columns: {sorted(missing)}")
    frame = frame.copy()
    frame["ticker"] = frame["ticker"].str.upper().str.strip()
    frame["last_price_date"] = pd.to_datetime(frame["last_price_date"], errors="raise").dt.normalize()
    frame["verified_at"] = pd.to_datetime(frame["verified_at"], errors="raise", utc=True)
    frame["terminal_return"] = pd.to_numeric(frame["terminal_return"], errors="raise")
    if frame["ticker"].eq("").any() or frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("terminal return rows require a ticker and source_url")
    if (frame["terminal_return"] < -1.0).any():
        raise ValueError("terminal_return cannot be below -100%")
    keys = ["ticker", "last_price_date"]
    if frame.duplicated(keys).any():
        raise ValueError("terminal return file contains duplicate ticker/date rows")
    return frame.sort_values(keys).reset_index(drop=True)


def observed_terminal_return_map(
    path: str | Path = TERMINAL_RETURNS_FILE,
) -> dict[tuple[str, pd.Timestamp], float]:
    frame = load_observed_terminal_returns(path)
    return {
        (row.ticker, row.last_price_date): float(row.terminal_return)
        for row in frame.itertuples(index=False)
    }
