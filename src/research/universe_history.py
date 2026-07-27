"""Point-in-time Nasdaq universe snapshots and coverage diagnostics."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.conf import NASDAQ_300M_STOCK_LIST_FILE
from src.io.financial_update import investable_common_equities


def snapshot_directory() -> Path:
    return Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"


def load_universe_snapshots(directory: str | Path | None = None) -> dict[pd.Timestamp, set[str]]:
    root = Path(directory) if directory is not None else snapshot_directory()
    snapshots: dict[pd.Timestamp, set[str]] = {}
    listed_paths = sorted(root.glob("nasdaq_listed_*.csv"))
    paths = listed_paths or sorted(root.glob("nasdaq_300M_*.csv"))
    for path in paths:
        raw_date = path.stem.removeprefix("nasdaq_300M_").removeprefix("nasdaq_listed_")
        try:
            available_date = pd.Timestamp(raw_date)
        except ValueError:
            continue
        frame = pd.read_csv(path)
        if not {"Symbol", "Name"}.issubset(frame.columns):
            continue
        common = investable_common_equities(frame)
        symbols = set(common["Symbol"].dropna().astype(str).str.upper())
        snapshots[available_date] = symbols
    return snapshots


def load_security_master(directory: str | Path | None = None) -> pd.DataFrame:
    """Return the latest observed name/type record for every known symbol."""
    root = Path(directory) if directory is not None else snapshot_directory()
    frames = []
    paths = [*root.glob("nasdaq_300M_*.csv"), *root.glob("nasdaq_listed_*.csv")]
    for path in sorted(paths):
        raw_date = path.stem.removeprefix("nasdaq_300M_").removeprefix("nasdaq_listed_")
        try:
            observed_at = pd.Timestamp(raw_date)
        except ValueError:
            continue
        frame = pd.read_csv(path)
        if not {"Symbol", "Name"}.issubset(frame.columns):
            continue
        frame = frame.copy()
        frame["observed_at"] = observed_at
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["Symbol", "Name", "observed_at", "is_common_equity"])
    master = pd.concat(frames, ignore_index=True).sort_values("observed_at")
    master["Symbol"] = master["Symbol"].astype(str).str.upper()
    master = master.drop_duplicates("Symbol", keep="last")
    common = set(investable_common_equities(master)["Symbol"])
    master["is_common_equity"] = master["Symbol"].isin(common)
    return master


def known_non_common_symbols(directory: str | Path | None = None) -> set[str]:
    master = load_security_master(directory)
    return set(master.loc[~master["is_common_equity"], "Symbol"])


def universe_as_of(
    snapshots: dict[pd.Timestamp, set[str]], as_of: pd.Timestamp
) -> set[str] | None:
    available = [date for date in snapshots if date <= pd.Timestamp(as_of)]
    return snapshots[max(available)] if available else None


def snapshot_coverage(
    snapshots: dict[pd.Timestamp, set[str]], start: str, end: str | None,
    maximum_snapshot_gap_days: int = 40,
) -> dict:
    if not snapshots:
        return {
            "snapshot_count": 0,
            "earliest_snapshot": None,
            "latest_snapshot": None,
            "maximum_snapshot_gap_days": None,
            "allowed_snapshot_gap_days": maximum_snapshot_gap_days,
            "full_period_covered": False,
        }
    dates = sorted(snapshots)
    requested_start = pd.Timestamp(start)
    requested_end = pd.Timestamp(end) if end else pd.Timestamp.today().normalize()
    relevant = [date for date in dates if requested_start <= date <= requested_end]
    boundaries = [requested_start, *relevant, requested_end]
    gaps = [int((right - left).days) for left, right in zip(boundaries, boundaries[1:])]
    maximum_gap = max(gaps, default=int((requested_end - requested_start).days))
    return {
        "snapshot_count": len(dates),
        "earliest_snapshot": dates[0].strftime("%Y-%m-%d"),
        "latest_snapshot": dates[-1].strftime("%Y-%m-%d"),
        "maximum_snapshot_gap_days": maximum_gap,
        "allowed_snapshot_gap_days": maximum_snapshot_gap_days,
        "full_period_covered": bool(
            dates[0] <= requested_start
            and (requested_end - dates[-1]).days <= maximum_snapshot_gap_days
            and maximum_gap <= maximum_snapshot_gap_days
        ),
    }
