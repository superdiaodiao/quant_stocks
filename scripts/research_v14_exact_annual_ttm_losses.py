#!/usr/bin/env python3
"""Expose exact annual filing losses as exclusion-only TTM observations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


OUTPUT_DIR = Path("output/research_only/v14/exact_annual_ttm_losses")
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A"}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def exact_annual_ttm_losses(annual: pd.DataFrame) -> pd.DataFrame:
    """Convert only exact, nonpositive annual net income into TTM evidence."""
    required = set(OUTPUT_COLUMNS)
    missing = sorted(required - set(annual.columns))
    if missing:
        raise ValueError(f"annual input missing columns: {missing}")
    selected = annual.loc[
        annual["metric"].eq("net_income")
        & annual["form"].isin(ANNUAL_FORMS)
        & pd.to_numeric(annual["value"], errors="coerce").le(0)
    ].copy()
    selected["metric"] = "net_income_ttm"
    selected["concept"] = (
        "exact_annual_ttm_loss:" + selected["concept"].astype(str)
    )
    selected = selected[OUTPUT_COLUMNS].drop_duplicates(
        ["ticker", "fiscal_end", "available_date", "metric", "accession"],
        keep="last",
    )
    return selected.sort_values(
        ["ticker", "available_date", "fiscal_end", "accession"]
    ).reset_index(drop=True)


def build(
    annual_path: Path,
    output_dir: Path = OUTPUT_DIR,
) -> dict:
    annual_path = Path(annual_path)
    before = _sha256(annual_path)
    annual = pd.read_csv(annual_path)
    facts = exact_annual_ttm_losses(annual)
    after = _sha256(annual_path)
    if after != before:
        raise RuntimeError("annual source changed while exact losses were derived")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    facts.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1,
        "research_only": True,
        "point_in_time_proven": True,
        "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED",
        "promotion_eligible": False,
        "formal_financials_modified": False,
        "input": {"annual": str(annual_path), "sha256": before},
        "accepted_exact_ttm_count": len(facts),
        "ticker_count": int(facts["ticker"].nunique()),
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path)
        }},
        "guardrail": (
            "Only nonpositive net income from exact annual 10-K or 20-F "
            "observations is exposed as TTM evidence. No quarterly value, "
            "revenue, growth rate, or positive eligibility is created."
        ),
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annual", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.annual, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
