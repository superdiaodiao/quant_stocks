#!/usr/bin/env python3
"""Recover ZS fiscal 2018Q4 from contemporaneous 10-K and nine-month 10-Q."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS

CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001713683.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/zs_2018_10k_q4")
ANNUAL_ACCESSION = "0001628280-18-011833"
ANNUAL_FILED = "2018-09-13"
NINE_MONTH_ACCESSION = "0001713683-18-000007"
NINE_MONTH_FILED = "2018-06-07"
CONCEPTS = {
    "net_income": "NetIncomeLoss",
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
}
ANNUAL = {"net_income": -33_646_000.0, "revenue": 190_174_000.0}
NINE_MONTH = {"net_income": -26_684_000.0, "revenue": 134_000_000.0}
EXPECTED_Q4 = {"net_income": -6_962_000.0, "revenue": 56_174_000.0}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fact(payload: dict, concept: str, end: str, accession: str, filed: str, form: str) -> float:
    values = payload["facts"]["us-gaap"][concept]["units"]["USD"]
    matches = {
        float(item["val"])
        for item in values
        if item.get("start") == "2017-08-01"
        and item.get("end") == end
        and item.get("accn") == accession
        and item.get("filed") == filed
        and item.get("form") == form
    }
    if len(matches) != 1:
        raise RuntimeError(f"ZS exact fact changed for {concept}/{end}: {matches}")
    return matches.pop()


def extract(payload: dict) -> dict[str, float]:
    annual = {
        metric: _fact(payload, concept, "2018-07-31", ANNUAL_ACCESSION, ANNUAL_FILED, "10-K")
        for metric, concept in CONCEPTS.items()
    }
    nine_month = {
        metric: _fact(payload, concept, "2018-04-30", NINE_MONTH_ACCESSION, NINE_MONTH_FILED, "10-Q")
        for metric, concept in CONCEPTS.items()
    }
    if annual != ANNUAL or nine_month != NINE_MONTH:
        raise RuntimeError(f"ZS annual/nine-month facts changed: {annual}/{nine_month}")
    q4 = {metric: annual[metric] - nine_month[metric] for metric in CONCEPTS}
    if q4 != EXPECTED_Q4:
        raise RuntimeError(f"ZS fiscal 2018Q4 residual changed: {q4}")
    return q4


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)["payload"]
    if int(payload["cik"]) != 1_713_683:
        raise RuntimeError(f"unexpected ZS CIK: {payload.get('cik')}")
    q4 = extract(payload)
    facts = pd.DataFrame([
        {
            "ticker": "ZS",
            "fiscal_end": "2018-07-31",
            "available_date": ANNUAL_FILED,
            "metric": metric,
            "value": value,
            "taxonomy": "us-gaap",
            "concept": f"strict_10k_q4:{CONCEPTS[metric]}",
            "form": "10-K:FY_MINUS_9M",
            "accession": ANNUAL_ACCESSION,
            "fetched_at": pd.Timestamp.now("UTC").tz_localize(None).normalize(),
        }
        for metric, value in sorted(q4.items())
    ], columns=OUTPUT_COLUMNS)
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
        "ticker": "ZS",
        "cik": 1_713_683,
        "accepted_quarter_count": 1,
        "accepted_fact_count": 2,
        "quarter": {"fiscal_end": "2018-07-31", "available_date": ANNUAL_FILED, **q4},
        "source": {"path": str(cache_path), "sha256": _sha256(cache_path), "annual_accession": ANNUAL_ACCESSION, "nine_month_accession": NINE_MONTH_ACCESSION},
        "validation": {"method": "contemporaneous_fiscal_year_minus_nine_months", "annual": ANNUAL, "nine_month": NINE_MONTH},
        "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}},
        "guardrail": "Uses only the 2018 10-Q and 10-K; later comparative filings are not required for PIT availability.",
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report["manifest"] = str(manifest_path)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    report = run(args.cache, args.output_dir)
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(base_dir=args.base_dir, supplement_dir=args.output_dir, output_dir=args.candidate_output_dir)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
