#!/usr/bin/env python3
"""Reparse isolated candidate payloads and merge them into v14-only data."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import (
    cached_companyfacts_cik_map,
    merge_fundamentals,
    reparse_companyfacts_cache,
)
from src.research.companyfacts_overrides import (
    RESEARCH_CONCEPT_OVERRIDES,
    RESEARCH_CURRENCY_OVERRIDES,
    RESEARCH_HISTORICAL_CIK_OVERRIDES,
    RESEARCH_CONCEPT_CUTOVER_OVERRIDES,
    RESEARCH_TRANSITION_OVERRIDES,
    research_currency_override_rows,
)


DEFAULT_CACHE = Path("output/research_only/v14/companyfacts_cache")
DEFAULT_BACKFILL = Path(
    "output/research_only/v14/candidate_companyfacts_backfill.json"
)
DEFAULT_BASE_QUARTERLY = Path(
    "output/data_provenance/companyfacts_proven_only_manifest-"
    "6c8a87fcc71cfcd5-recipe-6f0998be-q1-fp-guard-bank-duration-v3/quarterly.csv"
)
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/candidate_fundamentals")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def mapped_tickers(
    backfill: dict, available_tickers: set[str] | None = None
) -> list[str]:
    failed = {
        row["ticker"]
        for row in backfill.get("cache_refresh", {}).get("failures", [])
    }
    mapped = (
        set(backfill["requested_tickers"])
        - set(backfill.get("unresolved_tickers", []))
        - failed
    )
    if available_tickers is not None:
        mapped &= set(available_tickers)
    return sorted(mapped)


def reparse_and_merge(
    *,
    cache_dir: Path = DEFAULT_CACHE,
    backfill_path: Path = DEFAULT_BACKFILL,
    base_quarterly: Path = DEFAULT_BASE_QUARTERLY,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    filing_supplements: tuple[Path, ...] = (),
) -> dict:
    backfill = json.loads(backfill_path.read_text(encoding="utf-8"))
    available = set(cached_companyfacts_cik_map(cache_dir))
    mapped = mapped_tickers(backfill)
    tickers = sorted(
        set(mapped_tickers(backfill, available))
        | (set(RESEARCH_CURRENCY_OVERRIDES) & available)
        | (set(RESEARCH_CONCEPT_OVERRIDES) & available)
        | (set(RESEARCH_TRANSITION_OVERRIDES) & available)
        | (set(RESEARCH_HISTORICAL_CIK_OVERRIDES) & available)
        | (set(RESEARCH_CONCEPT_CUTOVER_OVERRIDES) & available)
        | ({"DMRC"} & available)
    )
    unavailable_payload_tickers = sorted(set(mapped) - set(tickers))
    output_dir.mkdir(parents=True, exist_ok=True)
    supplemental_annual = output_dir / "supplemental_annual.csv"
    supplemental_quarterly = output_dir / "supplemental_quarterly.csv"
    issuer_override_quarterly = output_dir / "issuer_override_quarterly.csv"
    merged_annual = output_dir / "annual.csv"
    merged_quarterly = output_dir / "quarterly.csv"
    base_annual = base_quarterly.with_name("annual.csv")
    cache_manifest = cache_dir / "manifest.json"
    base_hashes_before = {
        "annual": _sha256(base_annual),
        "quarterly": _sha256(base_quarterly),
    }
    reparse = reparse_companyfacts_cache(
        cache_dir=cache_dir,
        output=supplemental_annual,
        quarterly_output=supplemental_quarterly,
        tickers=tickers,
        include_validated_foreign_quarters=False,
        skip_unchanged=False,
        expected_cache_manifest_sha256=_sha256(cache_manifest),
    )
    override_rows, override_evidence = research_currency_override_rows(
        cache_dir
    )
    override_rows.to_csv(issuer_override_quarterly, index=False)
    combined_supplemental_quarterly = merge_fundamentals(
        pd.read_csv(supplemental_quarterly), override_rows
    )
    filing_supplement_evidence = []
    for path in filing_supplements:
        filing_rows = pd.read_csv(path)
        combined_supplemental_quarterly = merge_fundamentals(
            combined_supplemental_quarterly, filing_rows
        )
        filing_supplement_evidence.append({
            "path": str(path),
            "rows": len(filing_rows),
            "sha256": _sha256(path),
        })
    annual = merge_fundamentals(
        pd.read_csv(base_annual), pd.read_csv(supplemental_annual)
    )
    quarterly = merge_fundamentals(
        pd.read_csv(base_quarterly), combined_supplemental_quarterly
    )
    annual.to_csv(merged_annual, index=False)
    quarterly.to_csv(merged_quarterly, index=False)
    base_hashes_after = {
        "annual": _sha256(base_annual),
        "quarterly": _sha256(base_quarterly),
    }
    if base_hashes_after != base_hashes_before:
        raise RuntimeError("base proven fundamentals changed during v14 merge")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_fundamentals_modified": False,
        "cache_manifest": {
            "path": str(cache_manifest), "sha256": _sha256(cache_manifest)
        },
        "backfill": {"path": str(backfill_path), "sha256": _sha256(backfill_path)},
        "requested_reparse_ticker_count": len(tickers),
        "mapped_without_cached_payload": unavailable_payload_tickers,
        "reparse": reparse,
        "base": {
            "annual": {"path": str(base_annual), "sha256": base_hashes_after["annual"]},
            "quarterly": {"path": str(base_quarterly), "sha256": base_hashes_after["quarterly"]},
        },
        "supplemental": {
            "annual": {
                "path": str(supplemental_annual),
                "rows": len(pd.read_csv(supplemental_annual)),
                "sha256": _sha256(supplemental_annual),
            },
            "quarterly": {
                "path": str(supplemental_quarterly),
                "rows": len(pd.read_csv(supplemental_quarterly)),
                "sha256": _sha256(supplemental_quarterly),
            },
            "issuer_override_quarterly": {
                "path": str(issuer_override_quarterly),
                "rows": len(override_rows),
                "sha256": _sha256(issuer_override_quarterly),
                "evidence": override_evidence,
            },
            "filing_quarterly": filing_supplement_evidence,
            "combined_quarterly_rows": len(combined_supplemental_quarterly),
        },
        "merged": {
            "annual": {
                "path": str(merged_annual), "rows": len(annual),
                "sha256": _sha256(merged_annual),
            },
            "quarterly": {
                "path": str(merged_quarterly), "rows": len(quarterly),
                "sha256": _sha256(merged_quarterly),
            },
        },
    }
    report_path = output_dir / "manifest.json"
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    report["manifest"] = str(report_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--backfill", type=Path, default=DEFAULT_BACKFILL)
    parser.add_argument("--base-quarterly", type=Path, default=DEFAULT_BASE_QUARTERLY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--filing-supplement",
        type=Path,
        action="append",
        default=[],
        help="SHA-bind and merge a proven issuer filing-quarter artifact",
    )
    args = parser.parse_args()
    report = reparse_and_merge(
        cache_dir=args.cache_dir,
        backfill_path=args.backfill,
        base_quarterly=args.base_quarterly,
        output_dir=args.output_dir,
        filing_supplements=tuple(args.filing_supplement),
    )
    print(json.dumps({
        "manifest": report["manifest"],
        "requested_reparse_ticker_count": report["requested_reparse_ticker_count"],
        "supplemental_annual_rows": report["supplemental"]["annual"]["rows"],
        "supplemental_quarterly_rows": report["supplemental"]["quarterly"]["rows"],
        "merged_annual_rows": report["merged"]["annual"]["rows"],
        "merged_quarterly_rows": report["merged"]["quarterly"]["rows"],
        "formal_fundamentals_modified": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
