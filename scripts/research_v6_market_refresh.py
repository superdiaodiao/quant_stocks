#!/usr/bin/env python3
"""Refresh an isolated research-only market cache for v6 forward signals."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import shutil

import pandas as pd

from scripts.research_v5_trend_core_satellite import refresh_core_price
from scripts.research_v6_data_readiness import build_readiness
from src.conf import CLEANED_PRICE_DATA_DIR, NASDAQ_INDEX_FILE
from src.io.nasdaq_update import (
    fetch_closed_index_snapshot,
    fetch_history,
    refresh_universe,
    update_all,
)
from src.research.universe_history import load_universe_snapshots, universe_as_of


DEFAULT_ROOT = Path("output/research_only/v6_market")
DEFAULT_QQQ = Path("output/research_only/qqq_nasdaq_history.csv")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile_research_index(
    expected: pd.Timestamp,
    *,
    index_path: Path,
    provenance_path: Path,
) -> dict:
    """Prefer historical rows, with an audited official close fallback."""
    previous_provenance = {}
    if provenance_path.is_file():
        previous_provenance = json.loads(
            provenance_path.read_text(encoding="utf-8")
        )
    records = previous_provenance.get("records", {})
    prior_index_sha_verified = (
        not previous_provenance
        or previous_provenance.get("index_file_sha256") == _sha256(index_path)
    )
    existing = pd.read_csv(index_path, parse_dates=["date"])
    overlap_start = (expected - pd.Timedelta(days=10)).date()
    historical = fetch_history(
        "COMP", overlap_start, expected.date(), asset_class="index"
    )
    historical_dates = set(pd.to_datetime(historical["date"]).dt.normalize())
    combined = pd.concat([existing, historical], ignore_index=True)
    combined = combined.drop_duplicates("date", keep="last").sort_values("date")
    source = "nasdaq_historical"
    fallback = None
    expected_key = expected.strftime("%Y-%m-%d")
    existing_expected = combined.loc[
        pd.to_datetime(combined["date"]).dt.normalize().eq(expected)
    ]
    retained = records.get(expected_key)
    retained_close = pd.to_numeric(
        retained.get("close") if retained else None, errors="coerce"
    )
    retained_verified = bool(
        prior_index_sha_verified
        and retained
        and retained.get("date") == expected_key
        and retained.get("source") == "nasdaq_official_closed_chart_info"
        and retained.get("market_status") == "Closed"
        and pd.notna(retained_close)
        and len(existing_expected) == 1
        and abs(
            float(existing_expected.iloc[0]["close"])
            - float(retained_close)
        ) <= 1e-9
    )
    if expected not in historical_dates and not retained_verified:
        fallback = fetch_closed_index_snapshot("COMP", expected.date())
        row = pd.DataFrame([{
            "date": expected,
            "open": None,
            "high": None,
            "low": None,
            "close": fallback["close"],
            "volume": None,
        }])
        combined = pd.concat([combined, row], ignore_index=True)
        combined = combined.drop_duplicates("date", keep="last").sort_values("date")
        source = fallback["source"]
    elif expected not in historical_dates:
        source = "retained_official_close_fallback"
    combined["change_rate"] = pd.to_numeric(
        combined["close"], errors="coerce"
    ).pct_change()
    temporary = index_path.with_suffix(index_path.suffix + ".tmp")
    combined.to_csv(temporary, index=False)
    os.replace(temporary, index_path)

    for stamp in historical_dates:
        records.pop(stamp.strftime("%Y-%m-%d"), None)
    if fallback is not None:
        records[fallback["date"]] = fallback
    payload = {
        "schema_version": 1,
        "records": records,
        "index_file_sha256": _sha256(index_path),
    }
    provenance_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )
    return {
        "expected_session": expected.strftime("%Y-%m-%d"),
        "source": source,
        "source_verified": True,
        "fallback_used": fallback is not None,
        "historical_latest_date": (
            max(historical_dates).strftime("%Y-%m-%d")
            if historical_dates else None
        ),
        "index_file_sha256": payload["index_file_sha256"],
        "provenance_path": str(provenance_path),
    }


def seed_cache(
    symbols: list[str],
    *,
    price_dir: Path,
    index_path: Path,
) -> dict:
    price_dir.mkdir(parents=True, exist_ok=True)
    copied = missing_baseline = 0
    source_dir = Path(CLEANED_PRICE_DATA_DIR)
    for ticker in symbols:
        target = price_dir / f"{ticker.lower()}.csv"
        if target.is_file():
            continue
        source = source_dir / f"{ticker.lower()}.csv"
        if source.is_file():
            shutil.copy2(source, target)
            copied += 1
        else:
            missing_baseline += 1
    if not index_path.is_file():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(NASDAQ_INDEX_FILE, index_path)
    return {
        "symbols": len(symbols),
        "copied_price_files": copied,
        "missing_baseline_price_files": missing_baseline,
        "formal_market_files_modified": False,
        "formal_financial_files_modified": False,
    }


def refresh(
    *,
    expected_session: str | pd.Timestamp,
    summary_path: Path,
    root: Path = DEFAULT_ROOT,
    qqq_path: Path = DEFAULT_QQQ,
    workers: int = 16,
    limit: int | None = None,
) -> dict:
    expected = pd.Timestamp(expected_session).normalize()
    current_universe_path = root / "current_universe.csv"
    universe_refresh = refresh_universe(
        expected.date(), min_market_cap=0, target_path=current_universe_path,
        common_equities_only=True,
    )
    current = pd.read_csv(current_universe_path, keep_default_na=False)
    symbols = sorted(current["Symbol"].dropna().astype(str).str.upper().unique())
    if limit is not None:
        symbols = symbols[:limit]
    price_dir = root / "prices"
    index_path = root / "nasdaq_index.csv"
    seed = seed_cache(symbols, price_dir=price_dir, index_path=index_path)
    update = update_all(
        end=expected.date(),
        workers=workers,
        tickers=symbols,
        price_dir=price_dir,
        index_path=index_path,
    )
    index_refresh = reconcile_research_index(
        expected,
        index_path=index_path,
        provenance_path=root / "index_close_provenance.json",
    )
    refresh_core_price(qqq_path)
    readiness = build_readiness(
        expected_session=expected,
        summary_path=summary_path,
        price_dir=price_dir,
        index_path=index_path,
        qqq_path=qqq_path,
        universe=symbols,
    )
    payload = {
        "schema_version": 1,
        "research_only": True,
        "expected_session": expected.strftime("%Y-%m-%d"),
        "seed": seed,
        "universe_refresh": universe_refresh,
        "update": update,
        "index_refresh": index_refresh,
        "readiness": readiness,
        "formal_market_files_modified": False,
        "formal_financial_files_modified": False,
        "release_status": "BLOCKED",
        "broker_action_authorized": False,
    }
    manifest = root / "refresh_manifest.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-session", required=True)
    parser.add_argument(
        "--summary", type=Path,
        default=Path("output/research_v6_walkforward_defensive_ensemble_shadow_summary.json"),
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--qqq", type=Path, default=DEFAULT_QQQ)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(json.dumps(refresh(
        expected_session=args.expected_session,
        summary_path=args.summary,
        root=args.root,
        qqq_path=args.qqq,
        workers=args.workers,
        limit=args.limit,
    ), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
