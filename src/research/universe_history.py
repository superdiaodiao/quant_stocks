"""Point-in-time Nasdaq universe snapshots and coverage diagnostics."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.conf import NASDAQ_300M_STOCK_LIST_FILE, PROJECT_PATH
from src.io.security_universe import investable_common_equities
from src.io.security_identity import (
    SECURITY_IDENTITY_FILE,
    load_security_identity,
    normalize_universe_symbols,
)


EXPLICIT_COMMON_EQUITY_PATTERN = (
    r"\bCommon Stock\b|\bCommon Shares?\b|\bOrdinary Shares?\b|"
    r"\bAmerican Depositary Shares?\b"
)


def snapshot_directory() -> Path:
    return Path(NASDAQ_300M_STOCK_LIST_FILE).parent / "snapshots"


def load_universe_snapshots(
    directory: str | Path | None = None,
    carry_forward_confirmed_types: bool = True,
    security_identity_path: str | Path = SECURITY_IDENTITY_FILE,
) -> dict[pd.Timestamp, set[str]]:
    """Load PIT members, carrying only previously observed type evidence."""
    root = Path(directory) if directory is not None else snapshot_directory()
    snapshots: dict[pd.Timestamp, set[str]] = {}
    listed_paths = sorted(root.glob("nasdaq_listed_*.csv"))
    paths = listed_paths or sorted(root.glob("nasdaq_300M_*.csv"))
    known_non_common: set[str] = set()
    identities = load_security_identity(security_identity_path)
    for path in paths:
        raw_date = path.stem.removeprefix("nasdaq_300M_").removeprefix("nasdaq_listed_")
        try:
            available_date = pd.Timestamp(raw_date)
        except ValueError:
            continue
        frame = pd.read_csv(path)
        if not {"Symbol", "Name"}.issubset(frame.columns):
            continue
        frame = frame.copy()
        frame["Symbol"] = frame["Symbol"].astype(str).str.upper()
        common = investable_common_equities(frame)
        raw_symbols = set(frame["Symbol"].dropna())
        directly_common = set(common["Symbol"].dropna())
        directly_non_common = raw_symbols - directly_common
        explicit_common = set(
            common.loc[
                common["Name"].astype(str).str.contains(
                    EXPLICIT_COMMON_EQUITY_PATTERN,
                    case=False,
                    na=False,
                    regex=True,
                ),
                "Symbol",
            ]
        )
        # Some historical sources truncate security names. Once a symbol has
        # been identified as non-common, an ambiguous later name must not
        # silently re-admit it. A later explicit common/ordinary/ADR label can
        # reclassify it prospectively without rewriting older snapshots.
        if carry_forward_confirmed_types:
            known_non_common.update(directly_non_common)
            known_non_common.difference_update(explicit_common)
            symbols = directly_common - known_non_common
        else:
            symbols = directly_common
        symbols = normalize_universe_symbols(
            symbols, available_date, identities
        )
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
    symbols = set(master.loc[~master["is_common_equity"], "Symbol"])
    if directory is None:
        symbols.update(sourced_non_common_symbols())
    return symbols


NON_COMMON_SECURITY_EVIDENCE_FILE = Path(PROJECT_PATH) / (
    "stocks_list_dir/nasdaq/non_common_security_evidence.csv"
)


def sourced_non_common_symbols(
    path: str | Path = NON_COMMON_SECURITY_EVIDENCE_FILE,
) -> set[str]:
    """Load source-bound non-common types not recoverable from listing names."""
    path = Path(path)
    if not path.exists():
        return set()
    frame = pd.read_csv(path, dtype={"ticker": str, "payload_sha256": str})
    required = {
        "ticker", "security_category", "source_url", "payload_sha256", "verified_at"
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"non-common evidence is missing columns: {sorted(missing)}")
    if frame["ticker"].fillna("").str.strip().eq("").any():
        raise ValueError("non-common evidence requires tickers")
    if not frame["source_url"].fillna("").str.startswith("https://").all():
        raise ValueError("non-common evidence requires HTTPS source URLs")
    if not frame["payload_sha256"].str.fullmatch(r"[0-9a-f]{64}").all():
        raise ValueError("non-common evidence requires payload SHA-256 values")
    pd.to_datetime(frame["verified_at"], errors="raise", utc=True)
    return set(frame["ticker"].str.upper().str.strip())


def universe_as_of(
    snapshots: dict[pd.Timestamp, set[str]], as_of: pd.Timestamp,
    maximum_age_days: int | None = None,
) -> set[str] | None:
    as_of = pd.Timestamp(as_of)
    available = [date for date in snapshots if date <= as_of]
    if not available:
        return None
    snapshot_date = max(available)
    if (
        maximum_age_days is not None
        and (as_of - snapshot_date).days > maximum_age_days
    ):
        return None
    return snapshots[snapshot_date]


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
