#!/usr/bin/env python3
"""Reparse proven-cache candidate gaps into an isolated v14 supplement."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import (
    SEC_COMPANYFACTS_CACHE_DIR,
    cached_companyfacts_cik_map,
    merge_fundamentals,
    reparse_companyfacts_cache,
)


DEFAULT_PRIORITY = Path(
    "output/research_only/v14/"
    "candidate_path_audit_after_companyfacts_financial_priorities.csv"
)
DEFAULT_BASE_DIR = Path("output/research_only/v14/candidate_fundamentals")
DEFAULT_OUTPUT_DIR = Path(
    "output/research_only/v14/candidate_fundamentals_reparsed"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_reparse_tickers(priority_path: Path) -> list[str]:
    frame = pd.read_csv(priority_path, keep_default_na=False)
    selected = frame.loc[
        (
            frame["recommended_data_action"].eq("REVIEW_US_GAAP_PARSER")
            | (
                frame["recommended_data_action"].eq(
                    "REPARSE_OR_ACCEPT_HISTORY_LIMIT"
                )
                & frame["reporting_profile"].isin(
                    {
                        "SEC_ANNUAL_ONLY_OR_UNMAPPED_QUARTERLY",
                        "SEC_QUARTERLY_PARTIAL",
                        "FOREIGN_ANNUAL_ONLY_NEEDS_QUARTERLY_SOURCE",
                    }
                )
            )
        )
        & frame["raw_sec_cache_profile"].eq("US_GAAP_WITH_10Q"),
        "ticker",
    ]
    return sorted(set(selected.astype(str).str.upper().str.strip()) - {""})


def run(
    *,
    priority_path: Path = DEFAULT_PRIORITY,
    base_dir: Path = DEFAULT_BASE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = Path(SEC_COMPANYFACTS_CACHE_DIR),
) -> dict:
    requested_tickers = select_reparse_tickers(priority_path)
    available = cached_companyfacts_cik_map(cache_dir)
    tickers = sorted(set(requested_tickers) & set(available))
    unavailable_tickers = sorted(set(requested_tickers) - set(tickers))
    output_dir.mkdir(parents=True, exist_ok=True)
    supplemental_annual = output_dir / "supplemental_annual.csv"
    supplemental_quarterly = output_dir / "supplemental_quarterly.csv"
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    cache_manifest = cache_dir / "manifest.json"
    frozen_hashes_before = {
        "cache_manifest": _sha256(cache_manifest),
        "base_annual": _sha256(base_annual),
        "base_quarterly": _sha256(base_quarterly),
    }
    reparse = reparse_companyfacts_cache(
        cache_dir=cache_dir,
        output=supplemental_annual,
        quarterly_output=supplemental_quarterly,
        tickers=tickers,
        include_validated_foreign_quarters=False,
        skip_unchanged=False,
        expected_cache_manifest_sha256=frozen_hashes_before["cache_manifest"],
    )
    annual = merge_fundamentals(
        pd.read_csv(base_annual), pd.read_csv(supplemental_annual)
    )
    quarterly = merge_fundamentals(
        pd.read_csv(base_quarterly), pd.read_csv(supplemental_quarterly)
    )
    annual.to_csv(annual_output, index=False)
    quarterly.to_csv(quarterly_output, index=False)
    frozen_hashes_after = {
        "cache_manifest": _sha256(cache_manifest),
        "base_annual": _sha256(base_annual),
        "base_quarterly": _sha256(base_quarterly),
    }
    if frozen_hashes_after != frozen_hashes_before:
        raise RuntimeError("frozen cache or v14 base changed during isolated reparse")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_cache_modified": False,
        "selected_ticker_count": len(tickers),
        "selected_tickers": tickers,
        "unavailable_in_selected_cache": unavailable_tickers,
        "priority_input": {
            "path": str(priority_path), "sha256": _sha256(priority_path)
        },
        "frozen_input_hashes": frozen_hashes_after,
        "reparse": reparse,
        "supplemental": {
            "annual_rows": len(pd.read_csv(supplemental_annual)),
            "quarterly_rows": len(pd.read_csv(supplemental_quarterly)),
        },
        "merged": {
            "annual": {
                "path": str(annual_output), "rows": len(annual),
                "sha256": _sha256(annual_output),
            },
            "quarterly": {
                "path": str(quarterly_output), "rows": len(quarterly),
                "sha256": _sha256(quarterly_output),
            },
        },
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--priority", type=Path, default=DEFAULT_PRIORITY)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=Path(SEC_COMPANYFACTS_CACHE_DIR))
    args = parser.parse_args()
    report = run(
        priority_path=args.priority,
        base_dir=args.base_dir,
        output_dir=args.output_dir,
        cache_dir=args.cache_dir,
    )
    print(json.dumps({
        "manifest": report["manifest"],
        "selected_ticker_count": report["selected_ticker_count"],
        "supplemental_annual_rows": report["supplemental"]["annual_rows"],
        "supplemental_quarterly_rows": report["supplemental"]["quarterly_rows"],
        "merged_annual_rows": report["merged"]["annual"]["rows"],
        "merged_quarterly_rows": report["merged"]["quarterly"]["rows"],
        "formal_cache_modified": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
