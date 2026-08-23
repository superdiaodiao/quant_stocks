#!/usr/bin/env python3
"""Fetch missing candidate Company Facts into an isolated v14 cache."""

from __future__ import annotations

import argparse
from datetime import date
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    cached_companyfacts_cik_map,
    fetch_sec_ticker_map_snapshot,
    load_historical_ticker_ciks,
    populate_missing_companyfacts_cache,
    resolve_historical_ticker_ciks,
)


DEFAULT_PRIORITY = Path(
    "output/research_only/v14/"
    "candidate_path_audit_financial_priorities.csv"
)
DEFAULT_CACHE = Path("output/research_only/v14/companyfacts_cache")
DEFAULT_REPORT = Path(
    "output/research_only/v14/candidate_companyfacts_backfill.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch_tickers(priority_path: Path) -> list[str]:
    frame = pd.read_csv(priority_path, keep_default_na=False)
    required = {
        "ticker", "recommended_data_action", "raw_sec_cache_profile"
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"priority file is missing columns: {sorted(missing)}")
    selected = frame.loc[
        frame["recommended_data_action"].eq("FETCH_SEC_COMPANYFACTS")
        & frame["raw_sec_cache_profile"].eq("NOT_CACHED"),
        "ticker",
    ]
    return sorted(set(selected.astype(str).str.upper().str.strip()) - {""})


def reparse_tickers(priority_path: Path) -> list[str]:
    """Select cached US issuers whose candidate path needs a fresh parse."""
    frame = pd.read_csv(priority_path, keep_default_na=False)
    required = {
        "ticker", "recommended_data_action", "raw_sec_cache_profile"
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"priority file is missing columns: {sorted(missing)}")
    selected = frame.loc[
        frame["recommended_data_action"].eq(
            "REPARSE_OR_ACCEPT_HISTORY_LIMIT"
        )
        & frame["raw_sec_cache_profile"].eq("US_GAAP_WITH_10Q"),
        "ticker",
    ]
    return sorted(set(selected.astype(str).str.upper().str.strip()) - {""})


def backfill(
    *,
    priority_path: Path = DEFAULT_PRIORITY,
    cache_dir: Path = DEFAULT_CACHE,
    report_path: Path = DEFAULT_REPORT,
    workers: int = 4,
    limit: int | None = None,
) -> dict:
    tickers = sorted(
        set(fetch_tickers(priority_path)) | set(reparse_tickers(priority_path))
    )
    if limit is not None:
        tickers = tickers[:limit]
    current_map, current_map_evidence = fetch_sec_ticker_map_snapshot(cache_dir)
    formal_map = cached_companyfacts_cik_map(Path(SEC_COMPANYFACTS_CACHE_DIR))
    local_historical_map = load_historical_ticker_ciks(cache_dir)
    known = {
        ticker: (
            local_historical_map.get(ticker)
            or formal_map.get(ticker)
            or current_map.get(ticker)
        )
        for ticker in tickers
    }
    known = {ticker: int(cik) for ticker, cik in known.items() if cik}
    atom_requested = sorted(set(tickers) - set(known))
    atom_resolution = resolve_historical_ticker_ciks(
        atom_requested, cache_dir=cache_dir, retries=1
    )
    resolved = {**known, **atom_resolution["resolved"]}
    resolution = {
        "resolved": resolved,
        "resolved_count": len(resolved),
        "reused_verified_binding_count": len(known),
        "atom_requested_count": len(atom_requested),
        "atom_resolution": atom_resolution,
        "current_ticker_map_evidence": current_map_evidence,
    }
    mapped_tickers = sorted(resolved)
    unresolved_tickers = sorted(set(tickers) - set(mapped_tickers))
    result = populate_missing_companyfacts_cache(
        date.today(),
        workers=workers,
        tickers=mapped_tickers,
        cik_overrides=resolved,
        cache_dir=cache_dir,
        refresh_after_days=36500,
    )
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_companyfacts_cache_modified": False,
        "priority_input": {
            "path": str(priority_path), "sha256": _sha256(priority_path)
        },
        "cache_dir": str(cache_dir),
        "requested_ticker_count": len(tickers),
        "requested_tickers": tickers,
        "mapped_ticker_count": len(mapped_tickers),
        "unresolved_tickers": unresolved_tickers,
        "historical_cik_resolution": resolution,
        "cache_refresh": result,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report["report"] = str(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--priority", type=Path, default=DEFAULT_PRIORITY)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    report = backfill(
        priority_path=args.priority,
        cache_dir=args.cache_dir,
        report_path=args.report,
        workers=args.workers,
        limit=args.limit,
    )
    refresh = report["cache_refresh"]
    print(json.dumps({
        "report": report["report"],
        "requested_ticker_count": report["requested_ticker_count"],
        "resolved_historical_cik_count": report[
            "historical_cik_resolution"
        ]["resolved_count"],
        "cache_refresh": {
            "cached_ciks": refresh.get("cached_ciks"),
            "cached_symbol_count_after": refresh.get(
                "cached_symbol_count_after"
            ),
            "failure_count": len(refresh.get("failures", [])),
            "manifest": refresh.get("manifest"),
            "manifest_verified": refresh.get("manifest_verified"),
            "refresh_state_status": refresh.get("refresh_state_status"),
        },
        "formal_companyfacts_cache_modified": False,
    }, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
