#!/usr/bin/env python3
"""Overlay strict issuer overrides onto an existing research-only dataset."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from src.io.fundamentals_update import merge_fundamentals
from src.research.companyfacts_overrides import (
    RESEARCH_HISTORICAL_CIK_OVERRIDES,
    research_companyfacts_override_rows,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def overlay(*, base_dir: Path, cache_dir: Path, output_dir: Path) -> dict:
    base_dir = Path(base_dir)
    cache_dir = Path(cache_dir)
    output_dir = Path(output_dir)
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    base_manifest = base_dir / "manifest.json"
    cache_manifest = cache_dir / "manifest.json"
    before = {path: _sha256(path) for path in (base_annual, base_quarterly)}
    rows, evidence = research_companyfacts_override_rows(cache_dir)
    base_rows = pd.read_csv(base_quarterly)
    replaced = []
    for ticker, rule in sorted(RESEARCH_HISTORICAL_CIK_OVERRIDES.items()):
        fiscal_end = pd.to_datetime(base_rows["fiscal_end"], errors="coerce")
        mask = base_rows["ticker"].eq(ticker) & fiscal_end.between(
            pd.Timestamp(rule["minimum_fiscal_end"]),
            pd.Timestamp(rule["maximum_fiscal_end"]),
        )
        replaced.append({"ticker": ticker, "base_rows_removed": int(mask.sum())})
        base_rows = base_rows.loc[~mask].copy()
    merged = merge_fundamentals(base_rows, rows)
    output_dir.mkdir(parents=True, exist_ok=True)
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    override_output = output_dir / "issuer_override_quarterly.csv"
    pd.read_csv(base_annual).to_csv(annual_output, index=False)
    merged.to_csv(quarterly_output, index=False)
    rows.to_csv(override_output, index=False)
    after = {path: _sha256(path) for path in (base_annual, base_quarterly)}
    if after != before:
        raise RuntimeError("base research-only fundamentals changed during overlay")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "base": {
            "directory": str(base_dir),
            "manifest_sha256": _sha256(base_manifest),
            "annual_sha256": before[base_annual],
            "quarterly_sha256": before[base_quarterly],
        },
        "cache_manifest": {
            "path": str(cache_manifest),
            "sha256": _sha256(cache_manifest),
        },
        "override_evidence": evidence,
        "historical_cik_replacements": replaced,
        "outputs": {
            "annual": {"path": str(annual_output), "sha256": _sha256(annual_output)},
            "quarterly": {
                "path": str(quarterly_output), "rows": len(merged),
                "sha256": _sha256(quarterly_output),
            },
            "issuer_override": {
                "path": str(override_output), "rows": len(rows),
                "sha256": _sha256(override_output),
            },
        },
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument(
        "--cache-dir", type=Path,
        default=Path("output/research_only/v14/companyfacts_cache"),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = overlay(
        base_dir=args.base_dir, cache_dir=args.cache_dir, output_dir=args.output_dir
    )
    print(json.dumps({
        "manifest": report["manifest"],
        "quarterly_rows": report["outputs"]["quarterly"]["rows"],
        "override_rows": report["outputs"]["issuer_override"]["rows"],
        "release_status": report["release_status"],
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
