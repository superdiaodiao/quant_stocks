"""Apply sourced symbol changes and stock-merger conversions to price histories."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


CORPORATE_ACTIONS_FILE = Path(PROJECT_PATH) / "stocks_list_dir/nasdaq/corporate_actions.csv"


def load_corporate_actions(path: str | Path = CORPORATE_ACTIONS_FILE) -> pd.DataFrame:
    frame = pd.read_csv(path)
    required = {
        "predecessor", "last_price_date", "successor", "effective_date",
        "share_ratio", "cash_per_share", "source_url", "verified_at",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"corporate action file is missing columns: {sorted(missing)}")
    frame = frame.copy()
    for column in ("predecessor", "successor"):
        frame[column] = frame[column].astype(str).str.upper().str.strip()
    for column in ("last_price_date", "effective_date"):
        frame[column] = pd.to_datetime(frame[column], errors="raise").dt.normalize()
    frame["share_ratio"] = pd.to_numeric(frame["share_ratio"], errors="raise")
    frame["cash_per_share"] = pd.to_numeric(frame["cash_per_share"], errors="raise")
    if (frame["share_ratio"] < 0).any() or (frame["cash_per_share"] < 0).any():
        raise ValueError("corporate action consideration cannot be negative")
    if frame["source_url"].fillna("").str.strip().eq("").any():
        raise ValueError("corporate action rows require source_url")
    return frame.sort_values(["effective_date", "predecessor"]).reset_index(drop=True)


def extend_predecessor_price_histories(
    path: str | Path = CORPORATE_ACTIONS_FILE,
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
) -> dict:
    """Continue a held predecessor through its sourced successor consideration."""
    price_dir = Path(price_dir)
    results = []
    for action in load_corporate_actions(path).itertuples(index=False):
        predecessor_path = price_dir / f"{action.predecessor.lower()}.csv"
        successor_path = price_dir / f"{action.successor.lower()}.csv"
        predecessor = pd.read_csv(predecessor_path, parse_dates=["date"])
        successor = pd.read_csv(successor_path, parse_dates=["date"])
        observed_last = predecessor["date"].max().normalize()
        if action.last_price_date not in set(predecessor["date"].dt.normalize()):
            raise ValueError(
                f"{action.predecessor}: expected source date "
                f"{action.last_price_date.date()} is absent"
            )
        continuation = successor.loc[successor["date"] >= action.effective_date].copy()
        continuation = continuation.loc[continuation["date"] > observed_last]
        for column in ("open", "high", "low", "close"):
            continuation[column] = (
                action.cash_per_share + action.share_ratio * continuation[column]
            )
        continuation["ticker"] = action.predecessor
        combined = pd.concat([predecessor, continuation], ignore_index=True)
        combined = combined.drop_duplicates("date", keep="first").sort_values("date")
        tmp = predecessor_path.with_suffix(".csv.tmp")
        combined.to_csv(tmp, index=False)
        os.replace(tmp, predecessor_path)
        results.append({
            "predecessor": action.predecessor,
            "successor": action.successor,
            "rows_added": len(continuation),
            "last_date": combined["date"].max().strftime("%Y-%m-%d"),
            "source_url": action.source_url,
        })
    report = {"actions": len(results), "results": results}
    output = Path(PROJECT_PATH) / "output/data_provenance/corporate_action_import.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(extend_predecessor_price_histories(), indent=2))
