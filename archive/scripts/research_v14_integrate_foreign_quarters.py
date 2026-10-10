#!/usr/bin/env python3
"""Integrate diagnostically eligible foreign quarters into v14 only."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from src.io.fundamentals_update import SEC_COMPANYFACTS_CACHE_DIR, merge_fundamentals
from src.research.foreign_quarterly_diagnostics import (
    foreign_quarters_to_point_in_time,
)
from src.research.foreign_quarterly_impact import _payloads_for_symbols


DEFAULT_DIAGNOSTIC = Path("output/research_only/v14/foreign_quarterly_diagnostics.csv")
DEFAULT_BASE_DIR = Path("output/research_only/v14/candidate_fundamentals_reparsed")
DEFAULT_OUTPUT_DIR = Path("output/research_only/v14/candidate_fundamentals_foreign")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def eligible_symbols(diagnostic: pd.DataFrame) -> list[str]:
    selected = diagnostic.loc[
        diagnostic["eligible_for_parser_research"].fillna(False), "ticker"
    ]
    return sorted(set(selected.astype(str).str.upper()))


def run(
    *,
    diagnostic_path: Path = DEFAULT_DIAGNOSTIC,
    base_dir: Path = DEFAULT_BASE_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    cache_dir: Path = Path(SEC_COMPANYFACTS_CACHE_DIR),
) -> dict:
    diagnostic = pd.read_csv(diagnostic_path)
    symbols = eligible_symbols(diagnostic)
    for column in (
        "selected_revenue_concept", "selected_net_income_concept"
    ):
        if column not in diagnostic:
            diagnostic[column] = ""
    selections = diagnostic.set_index("ticker")[[
        "selected_currency",
        "selected_revenue_concept",
        "selected_net_income_concept",
    ]].to_dict("index")
    payloads = _payloads_for_symbols(set(symbols), cache_dir)
    if set(payloads) != set(symbols):
        raise ValueError(f"eligible payloads missing: {sorted(set(symbols) - set(payloads))}")
    rows = pd.concat([
        foreign_quarters_to_point_in_time(
            symbol,
            payload,
            fetched_at,
            selections[symbol]["selected_currency"],
            selections[symbol]["selected_revenue_concept"],
            selections[symbol]["selected_net_income_concept"],
        )
        for symbol, (payload, fetched_at) in sorted(payloads.items())
    ], ignore_index=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_annual = base_dir / "annual.csv"
    base_quarterly = base_dir / "quarterly.csv"
    annual_output = output_dir / "annual.csv"
    quarterly_output = output_dir / "quarterly.csv"
    supplement_output = output_dir / "supplemental_foreign_quarterly.csv"
    frozen_before = {
        "annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)
    }
    quarterly = merge_fundamentals(pd.read_csv(base_quarterly), rows)
    rows.to_csv(supplement_output, index=False)
    quarterly.to_csv(quarterly_output, index=False)
    shutil.copy2(base_annual, annual_output)
    frozen_after = {
        "annual": _sha256(base_annual), "quarterly": _sha256(base_quarterly)
    }
    if frozen_after != frozen_before:
        raise RuntimeError("v14 base changed during foreign-quarter integration")
    report = {
        "schema_version": 1,
        "research_only": True,
        "release_status": "BLOCKED",
        "formal_foreign_registry_modified": False,
        "eligible_symbols": symbols,
        "supplemental_row_count": len(rows),
        "merged_quarterly_row_count": len(quarterly),
        "diagnostic": {
            "path": str(diagnostic_path), "sha256": _sha256(diagnostic_path)
        },
        "base_hashes": frozen_after,
        "outputs": {
            "annual": {"path": str(annual_output), "sha256": _sha256(annual_output)},
            "quarterly": {"path": str(quarterly_output), "sha256": _sha256(quarterly_output)},
            "supplemental": {"path": str(supplement_output), "sha256": _sha256(supplement_output)},
        },
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diagnostic", type=Path, default=DEFAULT_DIAGNOSTIC)
    parser.add_argument("--base-dir", type=Path, default=DEFAULT_BASE_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--cache-dir", type=Path, default=Path(SEC_COMPANYFACTS_CACHE_DIR))
    args = parser.parse_args()
    report = run(
        diagnostic_path=args.diagnostic, base_dir=args.base_dir,
        output_dir=args.output_dir, cache_dir=args.cache_dir,
    )
    print(json.dumps({
        "manifest": report["manifest"],
        "eligible_symbols": report["eligible_symbols"],
        "supplemental_row_count": report["supplemental_row_count"],
        "merged_quarterly_row_count": report["merged_quarterly_row_count"],
        "formal_foreign_registry_modified": False,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
