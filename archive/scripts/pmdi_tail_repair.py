"""Repair the short PMD tail before the issuer's PMDI ticker transition.

Yahoo's PMDI chart is used only after validating a stable scale against the
pinned Stooq PMDI history. Existing PMD dates are never replaced; the repair
is limited to dates through 2024-12-31, before the sourced 2025 ticker change.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from src.conf import CLEANED_PRICE_DATA_DIR, PROJECT_PATH


ANALYSIS_END = pd.Timestamp("2024-12-31")
SOURCE_TICKER = "PMDI"
TARGET_TICKER = "PMD"
SOURCE_URL = "https://query2.finance.yahoo.com/v8/finance/chart/PMDI?period1=1609459200&period2=1784332800&interval=1d&events=div%2Csplits&includeAdjustedClose=true"
PRICE_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def repair(
    price_dir: str | Path = CLEANED_PRICE_DATA_DIR,
    provenance: str | Path = Path(PROJECT_PATH) / "output/data_provenance/pmdi_tail_repair.json",
) -> dict:
    request = Request(SOURCE_URL, headers={"User-Agent": "Mozilla/5.0"})
    payload = urlopen(request, timeout=60).read()
    envelope = json.loads(payload.decode("utf-8"))
    result = ((envelope.get("chart") or {}).get("result") or [None])[0]
    if not result:
        raise ValueError("Yahoo PMDI chart returned no result")
    quote = (result.get("indicators") or {}).get("quote", [{}])[0]
    adjusted = ((result.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose", [])
    incoming = pd.DataFrame({
        "date": pd.to_datetime(result.get("timestamp", []), unit="s").normalize(),
        "open": quote.get("open", []), "high": quote.get("high", []),
        "low": quote.get("low", []), "close": quote.get("close", []),
        "volume": quote.get("volume", []),
    }).dropna(subset=["date", "close"])
    adjusted_frame = pd.DataFrame({"date": pd.to_datetime(result.get("timestamp", []), unit="s").normalize(), "adjusted_close": adjusted})
    incoming = incoming.merge(adjusted_frame, on="date", how="left")
    incoming["corporate_action_factor"] = incoming["adjusted_close"] / incoming["close"]
    for column in ("open", "high", "low", "close"):
        incoming[column] = incoming[column] * incoming["corporate_action_factor"]
    incoming = incoming.drop(columns=["adjusted_close", "corporate_action_factor"])
    target = Path(price_dir) / "pmd.csv"
    local = pd.read_csv(target, parse_dates=["date"])
    overlap = local.merge(incoming, on="date", suffixes=("_local", "_source"))
    overlap = overlap.loc[overlap["close_local"].gt(0) & overlap["close_source"].gt(0)]
    if len(overlap) < 20:
        raise ValueError(f"insufficient PMD/PMDI overlap: {len(overlap)}")
    price_factor = float((overlap["close_local"] / overlap["close_source"]).median())
    volume_factor = float((overlap["volume_local"] / overlap["volume_source"]).median())
    price_ratios = overlap["close_local"] / overlap["close_source"]
    if float((price_ratios / price_factor - 1).abs().le(0.01).mean()) < 0.95:
        raise ValueError("PMD/PMDI price scale is not stable on overlap")
    for column in ("open", "high", "low", "close"):
        incoming[column] = incoming[column] * price_factor
    incoming["volume"] = (incoming["volume"] * volume_factor).round()
    incoming = incoming.loc[
        incoming["date"].le(ANALYSIS_END)
        & incoming["date"].gt(local["date"].max())
    ].copy()
    incoming["ticker"] = TARGET_TICKER
    merged = pd.concat([local, incoming], ignore_index=True).drop_duplicates("date", keep="first").sort_values("date")
    temporary = target.with_suffix(target.suffix + ".tmp")
    merged.to_csv(temporary, index=False)
    os.replace(temporary, target)
    report = {
        "research_only": True, "formal_release_eligible": False,
        "source_ticker": SOURCE_TICKER, "target_ticker": TARGET_TICKER,
        "source_url": SOURCE_URL, "source_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "overlap_sessions": int(len(overlap)), "price_factor": price_factor,
        "volume_factor": volume_factor, "rows_added": int(len(incoming)),
        "first_added_date": incoming["date"].min().strftime("%Y-%m-%d") if len(incoming) else None,
        "last_added_date": incoming["date"].max().strftime("%Y-%m-%d") if len(incoming) else None,
        "identity_evidence_url": "https://www.sec.gov/Archives/edgar/data/806517/000117184325001658/pmd20241231_10k.htm",
        "note": "Limited to the pre-2025 ticker boundary; source rights remain unverified and release stays blocked.",
    }
    path = Path(provenance)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(repair(), indent=2))
