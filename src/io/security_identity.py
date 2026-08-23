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
    if "identity_type" not in frame:
        frame["identity_type"] = "ticker_reuse"
    allowed_identity_types = {
        "ticker_reuse", "issuer_rename", "reverse_merger",
    }
    invalid_identity_types = set(frame["identity_type"]) - allowed_identity_types
    if invalid_identity_types:
        raise ValueError(
            "unsupported security identity types: "
            f"{sorted(invalid_identity_types)}"
        )
    for column in ("provider_ticker", "historical_ticker"):
        frame[column] = frame[column].astype(str).str.upper().str.strip()
    for column in ("last_historical_date", "current_ticker_first_date"):
        frame[column] = pd.to_datetime(frame[column], errors="raise").dt.normalize()
    if (frame["last_historical_date"] >= frame["current_ticker_first_date"]).any():
        raise ValueError("historical and current security date ranges overlap")
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("security identity rows require source_url")
    return frame


def issuer_rename_transitions(
    path: str | Path = SECURITY_IDENTITY_FILE,
) -> pd.DataFrame:
    """Return sourced 1:1 ticker changes for the same underlying security."""
    frame = load_security_identity(path)
    return frame.loc[frame["identity_type"].eq("issuer_rename")].copy()


def normalize_universe_symbols(
    symbols: set[str],
    observed_at: pd.Timestamp,
    identities: pd.DataFrame,
) -> set[str]:
    """Map a snapshot's reused provider ticker to its PIT security identity."""
    result = {str(symbol).upper() for symbol in symbols}
    observed_at = pd.Timestamp(observed_at).normalize()
    for row in identities.itertuples(index=False):
        if (
            row.identity_type in {"ticker_reuse", "reverse_merger"}
            and observed_at <= row.last_historical_date
            and row.provider_ticker in result
        ):
            result.remove(row.provider_ticker)
            result.add(row.historical_ticker)
    return result


def remap_weights_after_issuer_rename(
    weights: pd.Series,
    as_of: pd.Timestamp,
    transitions: pd.DataFrame,
) -> pd.Series:
    """Express target weights under the tradable ticker effective at ``as_of``."""
    result = weights.copy()
    for row in transitions.itertuples(index=False):
        if pd.Timestamp(as_of) < row.current_ticker_first_date:
            continue
        old = row.historical_ticker
        new = row.provider_ticker
        if old not in result.index or new not in result.index:
            continue
        result.loc[new] += result.loc[old]
        result.loc[old] = 0.0
    return result


def normalize_point_in_time_tickers(
    frame: pd.DataFrame,
    path: str | Path = SECURITY_IDENTITY_FILE,
) -> pd.DataFrame:
    """Make provider-labelled histories usable under their PIT ticker.

    A reused ticker moves old facts to the historical issuer. A same-issuer
    rename additionally retains those facts under the current ticker so TTM
    continuity remains available after the rename.
    """
    result = frame.copy()
    period_end = pd.to_datetime(result["period_end"], errors="coerce")
    available_date = (
        pd.to_datetime(result["available_date"], errors="coerce")
        if "available_date" in result
        else None
    )
    renamed_history = []
    for row in load_security_identity(path).itertuples(index=False):
        provider_mask = result["ticker"].astype(str).str.upper().eq(
            row.provider_ticker
        )
        if row.identity_type == "reverse_merger":
            if available_date is None:
                raise ValueError(
                    "reverse-merger financial normalization requires "
                    "available_date"
                )
            # A reverse merger changes the accounting predecessor at the
            # transaction cutover.  Period-end comparisons are unsafe here:
            # post-close filings can legitimately restate pre-close periods
            # for the new accounting acquirer.  Filing availability identifies
            # whether a fact came from the old listed shell or the successor.
            mask = provider_mask & available_date.le(row.last_historical_date)
        else:
            mask = provider_mask & period_end.le(row.last_historical_date)
        if row.identity_type == "issuer_rename":
            historical = result.loc[mask].copy()
            historical["ticker"] = row.historical_ticker
            renamed_history.append(historical)
        else:
            result.loc[mask, "ticker"] = row.historical_ticker
    return pd.concat([result, *renamed_history], ignore_index=True)


def split_reused_ticker_price_histories(
    path: str | Path = SECURITY_IDENTITY_FILE,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    provider_tickers: set[str] | None = None,
) -> list[dict]:
    """Split a provider's continuous current-ticker file at sourced rename dates.

    Existing historical files are merged so rerunning the repair cannot erase
    an already separated old-ticker history.
    """
    price_dir = Path(price_dir)
    results = []
    identities = load_security_identity(path)
    if provider_tickers is not None:
        requested = {str(ticker).upper() for ticker in provider_tickers}
        identities = identities.loc[
            identities["provider_ticker"].isin(requested)
        ]
    for row in identities.itertuples(index=False):
        current_path = price_dir / f"{row.provider_ticker.lower()}.csv"
        historical_path = price_dir / f"{row.historical_ticker.lower()}.csv"
        current = pd.read_csv(current_path, parse_dates=["date"])
        extracted = current.loc[
            current["date"] <= row.last_historical_date
        ].copy()
        existing = (
            pd.read_csv(historical_path, parse_dates=["date"])
            if historical_path.exists()
            else pd.DataFrame(columns=current.columns)
        )
        historical = (
            pd.concat([existing, extracted], ignore_index=True)
            .sort_values("date")
            .drop_duplicates("date", keep="last")
        )
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
            "historical_rows_extracted": len(extracted),
            "current_rows": len(current),
            "source_url": row.source_url,
        })
    return results


if __name__ == "__main__":
    print(split_reused_ticker_price_histories())
