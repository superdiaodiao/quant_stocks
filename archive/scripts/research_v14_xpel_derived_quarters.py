#!/usr/bin/env python3
"""Recover XPEL 2018-2019 Q1/Q4 from exact PIT duration facts."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


CIK = 1_767_258
RAW_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001767258.json.gz"
)
RAW_SHA256 = "4d51c0eee86dfaf9a6878049bbace6d79b2a9e01e4c6df94440a3693fbe7d880"
OUTPUT_DIR = Path("output/research_only/v14/xpel_derived_quarters")
METRICS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "net_income": "NetIncomeLoss",
}
EXPECTED = {
    (2018, "revenue", 1): 25_121_519.0,
    (2018, "revenue", 4): 26_792_879.0,
    (2018, "net_income", 1): 2_097_358.0,
    (2018, "net_income", 4): 1_893_874.0,
    (2019, "revenue", 1): 24_725_446.0,
    (2019, "revenue", 4): 39_495_283.0,
    (2019, "net_income", 1): 1_858_587.0,
    (2019, "net_income", 4): 4_610_338.0,
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _one(rows: list[dict], **expected) -> dict:
    matches = [row for row in rows if all(row.get(k) == v for k, v in expected.items())]
    if len(matches) != 1:
        raise RuntimeError(f"XPEL duration fact is not unique: {expected}")
    return matches[0]


def derived_quarters(payload: dict) -> tuple[list[dict], list[dict]]:
    facts = payload.get("payload", payload)["facts"]["us-gaap"]
    recovered, bindings = [], []
    for metric, concept in METRICS.items():
        rows = facts[concept]["units"]["USD"]
        for year in (2018, 2019):
            h1 = _one(
                rows, start=f"{year}-01-01", end=f"{year}-06-30",
                filed="2019-08-21", form="10-Q",
                accn="0001767258-19-000018",
            )
            q2 = _one(
                rows, start=f"{year}-04-01", end=f"{year}-06-30",
                filed="2019-08-21", form="10-Q",
                accn="0001767258-19-000018",
            )
            q3 = _one(
                rows, start=f"{year}-07-01", end=f"{year}-09-30",
                filed="2019-11-08", form="10-Q",
                accn="0001767258-19-000030",
            )
            annual = _one(
                rows, start=f"{year}-01-01", end=f"{year}-12-31",
                filed="2020-03-16", form="10-K",
                accn="0001767258-20-000011",
            )
            values = {
                1: float(h1["val"] - q2["val"]),
                4: float(annual["val"] - h1["val"] - q3["val"]),
            }
            for quarter, value in values.items():
                if value != EXPECTED[(year, metric, quarter)]:
                    raise RuntimeError("XPEL derived quarter value changed")
                source_rows = (h1, q2) if quarter == 1 else (annual, h1, q3)
                available = max(row["filed"] for row in source_rows)
                accession = (
                    "0001767258-19-000018" if quarter == 1
                    else "0001767258-20-000011"
                )
                recovered.append({
                    "ticker": "XPEL",
                    "fiscal_end": f"{year}-{'03-31' if quarter == 1 else '12-31'}",
                    "available_date": available, "metric": metric,
                    "value": value, "taxonomy": "us-gaap",
                    "concept": f"derived_{'h1_minus_q2' if quarter == 1 else 'annual_minus_h1_q3'}:{concept}",
                    "form": "10-Q" if quarter == 1 else "10-K",
                    "accession": accession,
                })
                bindings.append({
                    "year": year, "quarter": quarter, "metric": metric,
                    "available_date": available,
                    "source_accessions": sorted({row["accn"] for row in source_rows}),
                    "source_values": [float(row["val"]) for row in source_rows],
                })
    return recovered, bindings


def recover(raw_path: Path = RAW_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    raw_path, output_dir = Path(raw_path), Path(output_dir)
    if _sha256(raw_path) != RAW_SHA256:
        raise RuntimeError("XPEL raw Company Facts SHA256 changed")
    with gzip.open(raw_path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    rows, bindings = derived_quarters(payload)
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
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "XPEL", "cik": CIK,
        "accepted_quarter_count": 4, "accepted_fact_count": len(facts),
        "raw_payload": {"path": str(raw_path), "sha256": _sha256(raw_path),
                        "source_url": payload.get("source_url")},
        "bindings": bindings,
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Q1 equals exact H1 minus exact Q2; Q4 equals exact annual minus "
            "exact H1 and Q3. Every duration fact and accession is unique, "
            "and availability is the latest filing date among its inputs."
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
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
