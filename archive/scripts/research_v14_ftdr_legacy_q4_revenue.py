#!/usr/bin/env python3
"""Recover FTDR 2017-2018 Q4 revenue from exact PIT duration facts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CIK = 1_727_263
RAW_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001727263.json.gz"
)
RAW_SHA256 = "64327d532b8a3ed371cec44a7c2ad0e6d82c16561d7244d6c0141994917bd4ba"
OUTPUT_DIR = Path("output/research_only/v14/ftdr_legacy_q4_revenue")
CONCEPT = "RevenueFromContractWithCustomerExcludingAssessedTax"
ACCESSION = "0001727263-19-000003"
AVAILABLE_DATE = "2019-02-28"
EXPECTED = {
    2017: 258_000_000.0,
    2018: 279_000_000.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one(rows: list[dict], **expected) -> dict:
    matches = [row for row in rows if all(row.get(k) == v for k, v in expected.items())]
    if len(matches) != 1:
        raise RuntimeError(f"FTDR duration fact is not unique: {expected}")
    return matches[0]


def derived_q4_revenue(payload: dict) -> tuple[list[dict], list[dict]]:
    rows = payload.get("payload", payload)["facts"]["us-gaap"][CONCEPT][
        "units"
    ]["USD"]
    recovered, bindings = [], []
    for year in (2017, 2018):
        nine_months = _one(
            rows,
            start=f"{year}-01-01",
            end=f"{year}-09-30",
            filed="2018-11-06",
            form="10-Q",
            accn="0001727263-18-000018",
        )
        annual = _one(
            rows,
            start=f"{year}-01-01",
            end=f"{year}-12-31",
            filed=AVAILABLE_DATE,
            form="10-K",
            accn=ACCESSION,
        )
        value = float(annual["val"] - nine_months["val"])
        if value != EXPECTED[year]:
            raise RuntimeError("FTDR derived Q4 revenue changed")
        recovered.append(
            {
                "ticker": "FTDR",
                "fiscal_end": f"{year}-12-31",
                "available_date": max(annual["filed"], nine_months["filed"]),
                "metric": "revenue",
                "value": value,
                "taxonomy": "us-gaap",
                "concept": f"derived_annual_minus_nine_months:{CONCEPT}",
                "form": "10-K",
                "accession": ACCESSION,
            }
        )
        bindings.append(
            {
                "year": year,
                "metric": "revenue",
                "available_date": AVAILABLE_DATE,
                "annual_value": float(annual["val"]),
                "nine_month_value": float(nine_months["val"]),
                "source_accessions": [nine_months["accn"], annual["accn"]],
            }
        )
    return recovered, bindings


def recover(raw_path: Path = RAW_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    raw_path, output_dir = Path(raw_path), Path(output_dir)
    if _sha256(raw_path) != RAW_SHA256:
        raise RuntimeError("FTDR raw Company Facts SHA256 changed")
    with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows, bindings = derived_q4_revenue(payload)
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    for row in rows:
        row["fetched_at"] = fetched_at
    facts = pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["fiscal_end", "metric"]
    )
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
        "ticker": "FTDR",
        "cik": CIK,
        "accepted_quarter_count": len(facts),
        "accepted_fact_count": len(facts),
        "raw_payload": {
            "path": str(raw_path),
            "sha256": _sha256(raw_path),
            "source_url": payload.get("source_url"),
        },
        "bindings": bindings,
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Each Q4 equals the exact annual revenue filed on 2019-02-28 "
            "minus the exact nine-month revenue filed on 2018-11-06. "
            "Both duration facts and accessions must be unique; no later "
            "comparative or revision is used."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-path", type=Path, default=RAW_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.raw_path, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
