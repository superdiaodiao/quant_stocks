#!/usr/bin/env python3
"""Recover RGLD 2017 fiscal-Q4 revenue across its SEC taxonomy transition."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from scripts.research_v14_adpt_preipo_quarters import integrate_candidate
from src.io.fundamentals_update import OUTPUT_COLUMNS


RAW = Path("cleaned_stocks_data/financial/sec_companyfacts_cache/CIK0000085535.json.gz")
OUTPUT_DIR = Path("output/research_only/v14/rgld_legacy_revenue")
CIK = 85_535
EXPECTED = {
    "2017-06-30": {
        "start": "2017-04-01", "filed": "2017-08-10", "form": "10-K",
        "accession": "0001558370-17-006462", "value": 108_934_000.0,
    },
    "2018-06-30": {
        "start": "2018-04-01", "filed": "2018-08-09", "form": "10-K",
        "accession": "0001558370-18-006805", "value": 116_235_000.0,
    },
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        wrapper = json.load(handle)
    payload = wrapper.get("payload", {})
    if int(wrapper.get("cik", 0)) != CIK or int(payload.get("cik", 0)) != CIK:
        raise ValueError("RGLD Company Facts CIK mismatch")
    if "ROYAL GOLD" not in str(payload.get("entityName", "")).upper():
        raise ValueError("RGLD Company Facts issuer mismatch")
    return wrapper


def strict_facts(payload: dict) -> list[dict]:
    units = payload["facts"]["us-gaap"]["RoyaltyRevenue"]["units"]
    if set(units) != {"USD"}:
        raise ValueError("RGLD RoyaltyRevenue must contain only USD facts")
    selected = []
    for end, expected in EXPECTED.items():
        matches = [
            fact for fact in units["USD"]
            if fact.get("start") == expected["start"]
            and fact.get("end") == end
            and fact.get("filed") == expected["filed"]
            and fact.get("form") == expected["form"]
            and fact.get("accn") == expected["accession"]
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"RGLD legacy three-month revenue fact is not unique for {end}"
            )
        fact = matches[0]
        if float(fact["val"]) != expected["value"]:
            raise RuntimeError(f"RGLD legacy revenue value changed for {end}")
        selected.append(fact)
    return selected


def recover(raw_path: Path = RAW, output_dir: Path = OUTPUT_DIR) -> dict:
    wrapper = _load(raw_path)
    facts = strict_facts(wrapper["payload"])
    fetched_at = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    rows = pd.DataFrame([
        {
            "ticker": "RGLD", "fiscal_end": end,
            "available_date": expected["filed"], "metric": "revenue",
            "value": float(fact["val"]), "taxonomy": "us-gaap",
            "concept": "RoyaltyRevenue", "form": fact["form"],
            "accession": fact["accn"], "fetched_at": fetched_at,
        }
        for (end, expected), fact in zip(EXPECTED.items(), facts, strict=True)
    ], columns=OUTPUT_COLUMNS)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    facts_path = output_dir / "strict_quarterly_facts.csv"
    rows.to_csv(facts_path, index=False)
    report = {
        "schema_version": 1, "research_only": True,
        "point_in_time_proven": True, "parameters_frozen": False,
        "policy_status": "RESEARCH_PRETRAINING_ONLY_UNFROZEN",
        "release_status": "BLOCKED", "promotion_eligible": False,
        "formal_financials_modified": False, "ticker": "RGLD", "cik": CIK,
        "accepted_quarter_count": 2, "accepted_fact_count": 2,
        "raw_payload": {
            "path": str(raw_path), "sha256": _sha256(raw_path),
            "source_url": wrapper.get("source_url"),
            "fetched_at": wrapper.get("fetched_at"),
        },
        "outputs": {"strict_quarterly_facts": {
            "path": str(facts_path), "sha256": _sha256(facts_path),
        }},
        "guardrail": (
            "Only the original exact three-month USD RoyaltyRevenue fact is "
            "accepted. Annual, cumulative and later comparative facts are "
            "excluded. This bridges the pre-ASC-606 taxonomy without changing "
            "formal fundamentals."
        ),
    }
    manifest = output_dir / "manifest.json"
    manifest.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    report["manifest"] = str(manifest)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=RAW)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--base-dir", type=Path)
    parser.add_argument("--candidate-output-dir", type=Path)
    args = parser.parse_args()
    if (args.base_dir is None) != (args.candidate_output_dir is None):
        parser.error("--base-dir and --candidate-output-dir must be used together")
    report = recover(args.raw, args.output_dir)
    if args.base_dir is not None:
        report["candidate"] = integrate_candidate(
            base_dir=args.base_dir, supplement_dir=args.output_dir,
            output_dir=args.candidate_output_dir,
        )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
