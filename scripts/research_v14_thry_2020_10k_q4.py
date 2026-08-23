#!/usr/bin/env python3
"""Recover THRY 2020Q4 net income from its contemporaneous 10-K."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CACHE = Path("output/research_only/v14/companyfacts_cache/CIK0001556739.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/thry_2020_10k_q4")
ANNUAL_ACCESSION = "0001628280-21-005660"
ANNUAL_FILED = "2021-03-25"
ANNUAL_NET_INCOME = 149_221_000.0
NINE_MONTH_NET_INCOME = 39_421_000.0
NINE_MONTH_ACCESSION = "0001628280-20-016327"
NINE_MONTH_FILED = "2020-11-12"
EXPECTED_Q4 = 109_800_000.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_fact(payload: dict, start: str, end: str, accession: str, filed: str, form: str) -> float:
    values = payload["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
    matches = {
        float(item["val"])
        for item in values
        if item.get("start") == start
        and item.get("end") == end
        and item.get("accn") == accession
        and item.get("filed") == filed
        and item.get("form") == form
    }
    if len(matches) != 1:
        raise RuntimeError(f"THRY exact NetIncomeLoss fact changed for {end}: {matches}")
    return matches.pop()


def extract(payload: dict) -> tuple[float, dict]:
    annual = _exact_fact(
        payload, "2020-01-01", "2020-12-31", ANNUAL_ACCESSION, ANNUAL_FILED, "10-K"
    )
    if annual != ANNUAL_NET_INCOME:
        raise RuntimeError(f"THRY annual net income changed: {annual}")
    nine_month = _exact_fact(
        payload,
        "2020-01-01",
        "2020-09-30",
        NINE_MONTH_ACCESSION,
        NINE_MONTH_FILED,
        "10-Q",
    )
    if nine_month != NINE_MONTH_NET_INCOME:
        raise RuntimeError(f"THRY nine-month net income changed: {nine_month}")
    q4 = annual - nine_month
    if q4 != EXPECTED_Q4:
        raise RuntimeError(f"THRY 2020Q4 residual changed: {q4}")
    return q4, {"annual": annual, "nine_month": nine_month}


def run(cache_path: Path = CACHE, output_dir: Path = OUTPUT_DIR) -> dict:
    with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)["payload"]
    if int(payload["cik"]) != 1_556_739:
        raise RuntimeError(f"unexpected THRY CIK: {payload.get('cik')}")
    q4, reconciliation = extract(payload)
    facts = pd.DataFrame([{
        "ticker": "THRY",
        "fiscal_end": "2020-12-31",
        "available_date": ANNUAL_FILED,
        "metric": "net_income",
        "value": q4,
        "taxonomy": "us-gaap",
        "concept": "strict_10k_q4:NetIncomeLoss",
        "form": "10-K:FY_MINUS_Q1_Q2_Q3",
        "accession": ANNUAL_ACCESSION,
        "fetched_at": pd.Timestamp.now("UTC").tz_localize(None).normalize(),
    }], columns=OUTPUT_COLUMNS)
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
        "ticker": "THRY",
        "cik": 1_556_739,
        "accepted_quarter_count": 1,
        "accepted_fact_count": 1,
        "quarter": {"fiscal_end": "2020-12-31", "available_date": ANNUAL_FILED, "net_income": q4},
        "source": {"path": str(cache_path), "sha256": _sha256(cache_path), "annual_accession": ANNUAL_ACCESSION},
        "validation": {"method": "contemporaneous_fiscal_year_minus_three_quarters", **reconciliation},
        "outputs": {"strict_quarterly_facts": {"path": str(facts_path), "sha256": _sha256(facts_path)}},
        "guardrail": "Restores the original 2021-03-25 PIT availability; it does not use the 2022 filing date.",
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
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
