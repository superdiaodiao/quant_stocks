#!/usr/bin/env python3
"""Expose source-locked QFIN 20-F annual TTM growth to the quarterly path."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


SOURCE_PATH = Path(
    "cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0001741530.json.gz"
)
SOURCE_SHA256 = "823907445a91b52b63948f89ebc08a7d427e864a0077f0ba2c37431a89f6b2a9"
OUTPUT_DIR = Path("output/research_only/v14/qfin_annual_growth")
CIK = 1741530
TICKER = "QFIN"
CURRENCY = "CNY"

FILINGS = (
    {
        "fiscal_end": "2019-12-31",
        "available_date": "2020-04-30",
        "accession": "0001104659-20-054414",
        "prior_end": "2018-12-31",
    },
    {
        "fiscal_end": "2020-12-31",
        "available_date": "2021-04-21",
        "accession": "0001104659-21-052802",
        "prior_end": "2019-12-31",
    },
)

CONCEPTS = {
    "revenue": "RevenueFromContractWithCustomerExcludingAssessedTax",
    "net_income": "ProfitLoss",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_payload(source_path: Path) -> tuple[dict, dict]:
    source_path = Path(source_path)
    actual_sha = _sha256(source_path)
    if actual_sha != SOURCE_SHA256:
        raise ValueError(
            f"QFIN Company Facts SHA mismatch: {actual_sha} != {SOURCE_SHA256}"
        )
    with gzip.open(source_path, "rt", encoding="utf-8") as handle:
        envelope = json.load(handle)
    payload = envelope["payload"]
    if int(payload["cik"]) != CIK:
        raise ValueError(f"unexpected QFIN CIK: {payload['cik']}")
    return envelope, payload


def _exact_fact(
    payload: dict,
    *,
    concept: str,
    start: str,
    end: str,
    accession: str,
    filed: str,
) -> float:
    records = payload["facts"]["us-gaap"][concept]["units"][CURRENCY]
    matches = [
        record
        for record in records
        if record.get("start") == start
        and record.get("end") == end
        and record.get("accn") == accession
        and record.get("filed") == filed
        and record.get("form") == "20-F"
    ]
    values = {float(record["val"]) for record in matches}
    if len(values) != 1:
        raise ValueError(
            f"expected one QFIN {concept} value for {end}/{accession}, got {values}"
        )
    return values.pop()


def qfin_direct_growth_facts(payload: dict, fetched_at: str) -> pd.DataFrame:
    """Build two complete annual TTM/growth packages from original 20-F facts."""
    rows: list[dict] = []
    for filing in FILINGS:
        values: dict[str, tuple[float, float]] = {}
        for metric, concept in CONCEPTS.items():
            current = _exact_fact(
                payload,
                concept=concept,
                start=f"{filing['fiscal_end'][:4]}-01-01",
                end=filing["fiscal_end"],
                accession=filing["accession"],
                filed=filing["available_date"],
            )
            prior = _exact_fact(
                payload,
                concept=concept,
                start=f"{filing['prior_end'][:4]}-01-01",
                end=filing["prior_end"],
                accession=filing["accession"],
                filed=filing["available_date"],
            )
            values[metric] = (current, prior)

        for metric, (current, prior) in values.items():
            concept = CONCEPTS[metric]
            rows.extend(
                [
                    {
                        "ticker": TICKER,
                        "fiscal_end": filing["fiscal_end"],
                        "available_date": filing["available_date"],
                        "metric": f"{metric}_ttm",
                        "value": current,
                        "taxonomy": "us-gaap",
                        "concept": f"qfin_exact_annual_ttm:{concept}:{CURRENCY}",
                        "form": "20-F",
                        "accession": filing["accession"],
                        "fetched_at": fetched_at,
                    },
                    {
                        "ticker": TICKER,
                        "fiscal_end": filing["fiscal_end"],
                        "available_date": filing["available_date"],
                        "metric": f"{metric}_growth",
                        "value": (current - prior) / abs(prior),
                        "taxonomy": "us-gaap",
                        "concept": f"qfin_exact_annual_growth:{concept}:{CURRENCY}",
                        "form": "20-F",
                        "accession": filing["accession"],
                        "fetched_at": fetched_at,
                    },
                ]
            )
    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS).sort_values(
        ["available_date", "metric"]
    ).reset_index(drop=True)


def build(source_path: Path = SOURCE_PATH, output_dir: Path = OUTPUT_DIR) -> dict:
    source_path = Path(source_path)
    envelope, payload = _load_payload(source_path)
    fetched_at = str(envelope["fetched_at"])[:10]
    facts = qfin_direct_growth_facts(payload, fetched_at)
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
        "ticker": TICKER,
        "cik": CIK,
        "currency": CURRENCY,
        "source": {
            "path": str(source_path),
            "sha256": SOURCE_SHA256,
            "source_url": envelope["source_url"],
        },
        "accepted_direct_growth_package_count": len(FILINGS),
        "accepted_fact_count": len(facts),
        "outputs": {
            "strict_quarterly_facts": {
                "path": str(facts_path),
                "sha256": _sha256(facts_path),
            }
        },
        "guardrail": (
            "Each growth package uses current and comparative CNY facts from the "
            "same original 20-F accession. No quarterly period is manufactured, "
            "USD convenience translation is excluded, and available_date equals "
            "the filing date."
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
    parser.add_argument("--source", type=Path, default=SOURCE_PATH)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = build(args.source, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir,
            supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
